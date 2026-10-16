from __future__ import annotations

import base64
import contextvars
import functools
import inspect
import json
import logging
import os
import re
import secrets
import threading
import time
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, NamedTuple
from urllib.parse import quote

import httpx
from fastmcp import FastMCP
from fastmcp.server.dependencies import get_http_request

import audit
import jev_gate
import provenance
import redact

logger = logging.getLogger("honcho.mcp.bridge")

DEFAULT_CONFIG_HOME = Path(
    os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config")
).expanduser()
DEFAULT_RUNTIME_DIR = DEFAULT_CONFIG_HOME / "honcho" / "mcp-bridge"


def _env_flag(name: str, *, default: bool = False) -> bool:
    value = os.environ.get(name)
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


def _load_bearer_token() -> str:
    direct_token = os.environ.get("HONCHO_MCP_BEARER_TOKEN", "").strip()
    if direct_token:
        return direct_token

    token_file = os.environ.get("HONCHO_MCP_BEARER_TOKEN_FILE", "").strip()
    if not token_file:
        return ""
    path = Path(token_file).expanduser()
    try:
        return path.read_text().strip()
    except OSError as exc:
        raise RuntimeError(f"Unable to read MCP bearer token file: {path}") from exc


HONCHO_BASE_URL = os.environ.get("HONCHO_BASE_URL", "http://127.0.0.1:8001").rstrip("/")
DEFAULT_WORKSPACE_ID = os.environ.get("HONCHO_WORKSPACE_ID", "memory")
DEFAULT_USER_NAME = os.environ.get("HONCHO_USER_NAME", "user_chen")
DEFAULT_ASSISTANT_NAME = os.environ.get("HONCHO_ASSISTANT_NAME", "assistant")
OPTIONAL_BEARER_TOKEN = _load_bearer_token()
REQUIRE_AUTH = _env_flag("HONCHO_MCP_REQUIRE_AUTH")
REQUIRE_TOOL_CONFIG = _env_flag("HONCHO_MCP_REQUIRE_TOOL_CONFIG")
LISTEN_HOST = os.environ.get("HONCHO_MCP_HOST", "127.0.0.1")
LISTEN_PORT = int(os.environ.get("HONCHO_MCP_PORT", "8765"))
MCP_PATH = os.environ.get("HONCHO_MCP_PATH", "/mcp")
MCP_TRANSPORT = os.environ.get("HONCHO_MCP_TRANSPORT", "streamable-http")
TIMEOUT_SECONDS = float(os.environ.get("HONCHO_MCP_TIMEOUT", "300"))
HIDE_PEER_CARDS = _env_flag("HONCHO_MCP_HIDE_PEER_CARDS")

TOOL_CONFIG_PATH = Path(
    os.environ.get("HONCHO_MCP_TOOL_CONFIG", DEFAULT_RUNTIME_DIR / "tool-config.json")
).expanduser()

#: When set, only these tools are registered. An allowlist survives new upstream
#: tools; a denylist silently exposes each one that gets added.
ENABLED_TOOL_NAMES: frozenset[str] | None = (
    frozenset(
        name.strip()
        for name in os.environ["HONCHO_MCP_ENABLED_TOOLS"].split(",")
        if name.strip()
    )
    if os.environ.get("HONCHO_MCP_ENABLED_TOOLS", "").strip()
    else None
)

#: Ignore the x-honcho-* request headers and refuse tool arguments that name another
#: workspace or peer. A shared bridge must not let its callers point themselves
#: elsewhere, by header or by argument.
PIN_DEFAULTS = _env_flag("HONCHO_MCP_PIN_DEFAULTS")

#: Tool arguments that name peers, as one id, a list of ids, or peer and message
#: objects. On a pinned bridge each must be omitted or name only the bridge's own
#: peers. `test_every_peer_argument_is_pinned` fails when a tool gains one that is
#: not listed here.
PEER_ARGUMENTS = frozenset(
    {
        "messages",
        "observed_id",
        "observer_id",
        "peer_id",
        "peer_ids",
        "peer_perspective",
        "peer_target",
        "peers",
        "sender_id",
        "target_peer_id",
    }
)

#: Take each caller's reach from the gate of a team server. The gate checks the
#: caller's Cloudflare Access login, drops every x-honcho-* header the caller sent,
#: and says who is calling: the server's owner (`all`), a teammate (`folders`, with
#: the folders the owner opened to them, the apps whose conversations without a
#: folder they opened, and the owner's tools they may call), or a teammate the owner
#: turned off for now (`off`). Anything else is refused.
SCOPE_FROM_GATE = _env_flag("HONCHO_MCP_SCOPE_FROM_GATE")
SCOPE_MODE_HEADER = "x-honcho-scope-mode"
ALLOWED_FOLDERS_HEADER = "x-honcho-allowed-folders"
ALLOWED_SOURCES_HEADER = "x-honcho-allowed-sources"

#: What a teammate the owner turned off hears, whatever they call.
TURNED_OFF = (
    "The owner of this memory has turned your access off for now. You can ask again "
    "once they turn it back on."
)

ALLOWED_TOOLS_HEADER = "x-honcho-allowed-tools"

#: The owner's tools the owner may open to a teammate, each one only to read, in
#: three kinds. The first answer only from the conversations in the folders open to
#: them: a session named that is not open is refused, and a search or list is held
#: to the open ones.
FOLDER_TOOLS = (
    "search",
    "list_sessions",
    "get_session_messages",
    "get_session_message",
    "get_session_context",
    "inspect_session",
    "get_session_peers",
    "query_conclusions",
    "list_conclusions",
)
#: These answer from the whole memory, which no folder divides: what Honcho drew
#: about a peer, and its model answering from all of it. Opened, they read past the
#: folders, and only the Jev gate stands between them and private matters.
WHOLE_MEMORY_TOOLS = (
    "chat",
    "get_representation",
    "get_peer_context",
    "get_peer_card",
)
#: These say how the server is set up and who is in it, no conversation.
SERVER_INFO_TOOLS = (
    "server_info",
    "list_peers",
    "get_metadata",
    "inspect_workspace",
    "list_workspaces",
    "get_queue_status",
)
OPENABLE_TOOLS = FOLDER_TOOLS + WHOLE_MEMORY_TOOLS + SERVER_INFO_TOOLS
#: Open to a teammate whose gate names none: a grant from before tools were chosen.
DEFAULT_TEAMMATE_TOOLS = FOLDER_TOOLS

#: The tools a teammate's agent called before it had the owner's: each open while
#: the tool of the owner's it stands for is.
TEAMMATE_TOOLS = {
    "list_conversations": "list_sessions",
    "search_messages": "search",
    "read_conversation": "get_session_messages",
    "search_conclusions": "query_conclusions",
}

#: The most sessions one search names in its `session_id` filter. Honcho caps that
#: filter only on chat and representation (MAX_SESSION_ALLOWLIST_ENTRIES, 1000, in
#: its src/utils/filter.py), not on message or conclusion search; there each id is a
#: bound parameter of one Postgres query, which takes at most 32,767. A teammate
#: with more sessions open is searched a share at a time, the shares at once, and
#: the answers merged (_scoped_items).
MAX_SESSIONS_PER_SEARCH = 20_000
#: Shares searched at the same time.
SEARCH_SHARE_WORKERS = 4

#: How long a workspace's sessions, as listed, answer for which folder each one is
#: in. A burst of questions lists them once, and a conversation started now is
#: open within a minute.
SESSION_LIST_TTL_SECONDS = 60.0
#: Sessions asked for per page, the most Honcho's pagination returns.
SESSION_PAGE_SIZE = 100
#: Pages read before giving up, so a listing that never ends cannot hold a call.
MAX_SESSION_PAGES = 1000

if REQUIRE_AUTH and not OPTIONAL_BEARER_TOKEN:
    raise RuntimeError(
        "HONCHO_MCP_REQUIRE_AUTH is enabled, but no MCP bearer token is configured"
    )


def _disabled_tool_names(
    path: Path | None = None,
    *,
    required: bool | None = None,
) -> set[str]:
    config_path = path or TOOL_CONFIG_PATH
    config_required = REQUIRE_TOOL_CONFIG if required is None else required
    try:
        payload = json.loads(config_path.read_text())
    except FileNotFoundError as exc:
        if config_required:
            raise RuntimeError(
                f"Required MCP tool config is missing: {config_path}"
            ) from exc
        return set()
    except (json.JSONDecodeError, OSError) as exc:
        if config_required:
            raise RuntimeError(
                f"Required MCP tool config is invalid: {config_path}"
            ) from exc
        return set()

    if not isinstance(payload, dict):
        if config_required:
            raise RuntimeError(
                f"Required MCP tool config must be an object: {config_path}"
            )
        return set()
    names = payload.get("disabled_tools", [])
    if not isinstance(names, list):
        if config_required:
            raise RuntimeError(
                f"Required MCP tool config needs a disabled_tools list: {config_path}"
            )
        return set()
    return {str(name) for name in names}


DISABLED_TOOL_NAMES = _disabled_tool_names()
if HIDE_PEER_CARDS:
    DISABLED_TOOL_NAMES |= {"get_peer_card", "get_peer_context", "set_peer_card"}

mcp = FastMCP(
    name="Local Honcho MCP",
    instructions=(
        "This MCP server exposes a local self-hosted Honcho instance. "
        "Tool availability is controlled by the dashboard tool configuration. "
        "Use search/get_peer_context/get_representation/get_session_context for recall."
    ),
)


_tool_config_lock = threading.Lock()
_tool_config_cache: dict[str, Any] = {"mtime": None, "disabled": DISABLED_TOOL_NAMES}
_call_depth = threading.local()

#: What the wrapper resolved for the call in progress: a teammate's reach, or None
#: when the call is not confined. The tool body reads it instead of listing the
#: sessions a second time; outside a wrapped call it holds `_UNRESOLVED`.
_UNRESOLVED = object()
_call_reach: contextvars.ContextVar[Any] = contextvars.ContextVar(
    "honcho_mcp_call_reach", default=_UNRESOLVED
)


def _tool_is_registered(name: str) -> bool:
    if ENABLED_TOOL_NAMES is not None:
        return name in ENABLED_TOOL_NAMES
    return name not in DISABLED_TOOL_NAMES


def _currently_disabled() -> set[str]:
    """Disabled tools as of now, so a dashboard edit applies without a restart.

    Registration is fixed at import time, which keeps the advertised tool list
    minimal. This re-read only ever adds refusals to tools already listed.
    """
    try:
        mtime = TOOL_CONFIG_PATH.stat().st_mtime
    except OSError:
        return DISABLED_TOOL_NAMES
    with _tool_config_lock:
        if _tool_config_cache["mtime"] != mtime:
            try:
                names = _disabled_tool_names(required=False)
            except RuntimeError:
                names = set(DISABLED_TOOL_NAMES)
            if HIDE_PEER_CARDS:
                names |= {"get_peer_card", "get_peer_context", "set_peer_card"}
            _tool_config_cache["disabled"] = names
            _tool_config_cache["mtime"] = mtime
        return _tool_config_cache["disabled"]


def _caller_identity() -> tuple[str, str]:
    """Who is calling, and how that was established.

    Cloudflare Access is the only source the caller cannot choose for itself, so it
    wins. The header and the address are recorded with their weaker provenance
    rather than dropped, so the log is still readable before Access is in place.
    """
    try:
        request = get_http_request()
    except RuntimeError:
        return ("local-stdio", "stdio")
    headers = request.headers
    email = headers.get("cf-access-authenticated-user-email", "").strip()
    if email:
        return (email, "cf-access")
    client_id = headers.get("cf-access-client-id", "").strip()
    if client_id:
        return (client_id, "cf-service-token")
    name = headers.get("x-honcho-user-name", "").strip()
    if name:
        return (name, "x-honcho-user-name")
    host = getattr(getattr(request, "client", None), "host", "") or ""
    return (host or "unknown", "address")


def _bind_arguments(
    fn: Any, args: tuple[Any, ...], kwargs: dict[str, Any]
) -> dict[str, Any]:
    try:
        return dict(inspect.signature(fn).bind_partial(*args, **kwargs).arguments)
    except TypeError:
        return dict(kwargs)


def _audit_workspace(arguments: dict[str, Any]) -> str | None:
    try:
        return _resolve_defaults(workspace_id=arguments.get("workspace_id"))[
            "workspace_id"
        ]
    except Exception:  # noqa: BLE001 - never let bookkeeping break a tool
        return arguments.get("workspace_id")


def _judged_text(arguments: dict[str, Any]) -> str:
    """What the Jev gate reads of a call: every piece of the caller's own words.

    A teammate's tool takes their question beside the words searched for, and both
    are judged, so neither can carry what the other would not.
    """
    texts: list[str] = []
    for name in audit.QUERY_PARAMS:
        text = audit.query_text_of({name: arguments.get(name)}, limit=None)
        if text and text not in texts:
            texts.append(text)
    return "\n".join(texts)


def _unjudged(*verdicts: jev_gate.Verdict) -> str | None:
    """Why a call let through went without a judgment, once per distinct reason."""
    reasons: list[str] = []
    for verdict in verdicts:
        if verdict.unjudged and verdict.reason not in reasons:
            reasons.append(verdict.reason)
    return "; ".join(reasons) or None


def register_tool(*, name: str):
    """Register one tool, routed through the audit log and the Jev gate.

    Every tool goes through here, so this is the single place that records what was
    asked and the single place a query can be refused: a disabled tool, a pinned
    bridge's other workspace or peer, a call outside a teammate's folders, then the
    Jev gate on the query and, once Honcho has answered, on the answer. Nested calls
    (``get_metadata`` reaching for ``inspect_workspace``) are logged once, at the
    outermost call.
    """
    if not _tool_is_registered(name):

        def _disabled(fn):
            return fn

        return _disabled

    def _decorator(fn: Any) -> Any:
        @functools.wraps(fn)
        def wrapper(*args: Any, **kwargs: Any) -> Any:
            depth = getattr(_call_depth, "value", 0)
            if depth:
                return fn(*args, **kwargs)
            started = time.perf_counter()

            def elapsed_ms() -> int:
                return int((time.perf_counter() - started) * 1000)

            reached: contextvars.Token[Any] | None = None
            # Set inside the try, so that a failure while gathering the call's own
            # details still restores the depth. A thread left at depth 1 would skip
            # the audit log for every later call that lands on it.
            try:
                _call_depth.value = depth + 1
                arguments = _bind_arguments(fn, args, kwargs)
                caller, caller_source = _caller_identity()
                workspace_id = _audit_workspace(arguments)
                if name in _currently_disabled():
                    audit.record(
                        tool=name,
                        caller=caller,
                        caller_source=caller_source,
                        arguments=arguments,
                        workspace_id=workspace_id,
                        status="denied",
                        error="tool disabled by configuration",
                        duration_ms=elapsed_ms(),
                    )
                    raise RuntimeError(f"Tool is disabled on this MCP server: {name}")

                violation = _pin_violation(arguments)
                failure: str | None = None
                reach: TeammateReach | None = None
                if not violation:
                    try:
                        reach = _teammate_reach(name, arguments)
                    except RuntimeError as exc:
                        violation = str(exc)
                if reach is not None:
                    # Recorded on every teammate's call, refused or answered, so the
                    # log says which folders (and apps) an answer came from and how
                    # many conversations they held.
                    arguments["folders"] = reach.folders
                    if reach.sources:
                        arguments["sources"] = list(reach.sources)
                    if reach.sessions is not None:
                        arguments["sessions"] = len(reach.sessions)
                    violation, failure = reach.refusal, reach.failure
                if violation:
                    audit.record(
                        tool=name,
                        caller=caller,
                        caller_source=caller_source,
                        arguments=arguments,
                        workspace_id=workspace_id,
                        # A failure the caller hears of only as a refusal keeps its
                        # cause here, for the owner.
                        status="error" if failure else "denied",
                        error=failure or violation,
                        duration_ms=elapsed_ms(),
                    )
                    raise RuntimeError(violation)
                reached = _call_reach.set(reach)

                query = _judged_text(arguments)
                verdict = jev_gate.judge(
                    tool=name,
                    query=query,
                    caller=caller,
                    workspace_id=workspace_id,
                    teammate=reach is not None,
                )
                if not verdict.allowed:
                    audit.record(
                        tool=name,
                        caller=caller,
                        caller_source=caller_source,
                        arguments=arguments,
                        workspace_id=workspace_id,
                        status="denied",
                        error=verdict.reason,
                        duration_ms=elapsed_ms(),
                        jev_score=verdict.score,
                    )
                    raise RuntimeError(jev_gate.MESSAGE)

                # A call let through without a judgment (Jev failed, or the team
                # hub has no Jev key) says so on its row; otherwise it would read
                # like a call with the gate off.
                unjudged = _unjudged(verdict)
                try:
                    result = fn(*args, **kwargs)
                except Exception as exc:
                    failure = f"{type(exc).__name__}: {exc}"
                    audit.record(
                        tool=name,
                        caller=caller,
                        caller_source=caller_source,
                        arguments=arguments,
                        workspace_id=workspace_id,
                        status="error",
                        error=f"{failure}; {unjudged}" if unjudged else failure,
                        duration_ms=elapsed_ms(),
                        jev_score=verdict.score,
                    )
                    raise

                # Numbers and secrets known by their form never reach a teammate,
                # nor Jev: they are hidden in place, so the work around them still
                # goes out, and the log says what was hidden.
                if reach is not None:
                    # Where each conversation and message came from on the owner's
                    # computer (the transcript file and its line, the turn's hash,
                    # the folder's path) stays here too: a teammate reads the
                    # memory, not the owner's disk.
                    result = provenance.hidden(result)
                    result, masked = redact.redact(result)
                    if masked:
                        arguments["masked"] = masked

                # The answer is read before it leaves: a harmless question can
                # still draw private matters out of the memory. A withheld answer
                # gets the very refusal a refused question gets, so the refusal
                # says nothing about what the memory holds.
                checked = jev_gate.judge_answer(
                    tool=name,
                    query=query,
                    result=redact.unnamed(result),
                    caller=caller,
                    workspace_id=workspace_id,
                    teammate=reach is not None,
                )
                if not checked.allowed:
                    audit.record(
                        tool=name,
                        caller=caller,
                        caller_source=caller_source,
                        arguments=arguments,
                        workspace_id=workspace_id,
                        status="denied",
                        error=checked.reason,
                        duration_ms=elapsed_ms(),
                        jev_score=verdict.score,
                        answer_score=checked.score,
                    )
                    raise RuntimeError(jev_gate.MESSAGE)
                audit.record(
                    tool=name,
                    caller=caller,
                    caller_source=caller_source,
                    arguments=arguments,
                    workspace_id=workspace_id,
                    status="ok",
                    error=_unjudged(verdict, checked),
                    duration_ms=elapsed_ms(),
                    jev_score=verdict.score,
                    answer_score=checked.score,
                )
                return result
            finally:
                if reached is not None:
                    _call_reach.reset(reached)
                _call_depth.value = depth

        return mcp.tool(name=name)(wrapper)

    return _decorator


class BearerGate:
    """Refuse the MCP endpoint without the bearer token, before any message is read.

    `_require_auth` only runs when a tool reaches Honcho, so without this a wrong
    token still completed `initialize` and listed every tool. A client checking its
    connection then saw success and failed on the first real question.
    `_require_auth` stays as the second check.
    """

    def __init__(self, app: Any) -> None:
        self.app = app

    async def __call__(self, scope: Any, receive: Any, send: Any) -> None:
        if (
            scope["type"] == "http"
            and OPTIONAL_BEARER_TOKEN
            and scope.get("path", "").rstrip("/") == MCP_PATH.rstrip("/")
        ):
            presented = dict(scope.get("headers") or []).get(b"authorization", b"")
            expected = f"Bearer {OPTIONAL_BEARER_TOKEN}".encode()
            if not secrets.compare_digest(presented, expected):
                from starlette.responses import JSONResponse

                response = JSONResponse(
                    {"error": "Unauthorized: missing or invalid bearer token"},
                    status_code=401,
                )
                await response(scope, receive, send)
                return
        await self.app(scope, receive, send)


def _require_auth() -> None:
    if MCP_TRANSPORT == "stdio":
        return
    if not OPTIONAL_BEARER_TOKEN:
        return
    request = get_http_request()
    auth = request.headers.get("authorization", "")
    expected = f"Bearer {OPTIONAL_BEARER_TOKEN}"
    if not secrets.compare_digest(auth, expected):
        raise RuntimeError("Unauthorized: missing or invalid bearer token")


def _resolve_defaults(
    workspace_id: str | None = None,
    user_name: str | None = None,
    assistant_name: str | None = None,
) -> dict[str, str]:
    if PIN_DEFAULTS:
        # The wrapper already refused a caller's other values. Ignoring them here
        # as well keeps a nested call from reopening that.
        return {
            "workspace_id": DEFAULT_WORKSPACE_ID,
            "user_name": DEFAULT_USER_NAME,
            "assistant_name": DEFAULT_ASSISTANT_NAME,
        }
    try:
        headers = get_http_request().headers
    except RuntimeError:
        # Local stdio clients have no HTTP request context and use env defaults.
        headers = {}
    return {
        "workspace_id": workspace_id
        or headers.get("x-honcho-workspace-id")
        or DEFAULT_WORKSPACE_ID,
        "user_name": user_name
        or headers.get("x-honcho-user-name")
        or DEFAULT_USER_NAME,
        "assistant_name": assistant_name
        or headers.get("x-honcho-assistant-name")
        or DEFAULT_ASSISTANT_NAME,
    }


def _named_peers(value: Any) -> set[str]:
    """Every peer id in one argument: an id, ids, a peer map, or peer/message items."""
    if not value:
        return set()
    if isinstance(value, str):
        return {value}
    if isinstance(value, dict):
        return {str(key) for key in value}
    names: set[str] = set()
    for item in value:
        if isinstance(item, dict):
            item = item.get("peer_id") or item.get("id")
        if item:
            names.add(str(item))
    return names


def _pin_violation(arguments: dict[str, Any]) -> str | None:
    """Why a call on a pinned bridge reaches past its workspace or peers, if it does."""
    if not PIN_DEFAULTS:
        return None
    workspace_id = arguments.get("workspace_id")
    if workspace_id and workspace_id != DEFAULT_WORKSPACE_ID:
        return "workspace_id cannot be changed on this MCP server; omit it"
    # Honcho filters can name any peer, nested at any depth, and query_conclusions
    # lets them override its observer_id. Checking them is not worth the risk.
    if arguments.get("filters"):
        return "filters cannot be used on this MCP server; omit them"
    own_peers = {DEFAULT_USER_NAME, DEFAULT_ASSISTANT_NAME}
    for key in sorted(PEER_ARGUMENTS):
        if _named_peers(arguments.get(key)) - own_peers:
            return f"{key} cannot name another peer on this MCP server; omit it"
    return None


def _gate_values(headers: Any, name: str) -> list[str]:
    """Every copy of one header, in the order sent."""
    getlist = getattr(headers, "getlist", None)
    if callable(getlist):
        return list(getlist(name))
    value = headers.get(name)
    return [] if value is None else [value]


def _gate_header(headers: Any, name: str) -> str | None:
    """One header the gate sets, or None when it is missing or repeated.

    The gate sends each exactly once, so a second copy did not come from the gate.
    """
    values = _gate_values(headers, name)
    return values[0] if len(values) == 1 else None


#: base64url's alphabet, with or without its padding.
_BASE64URL = re.compile(r"[A-Za-z0-9_-]*={0,2}")


def _folder_entry(item: Any) -> dict[str, Any]:
    """One open folder as the gate or the owner sends it, or TypeError/ValueError.

    Exactly {"path": non-empty text, "subfolders": true or false}. A key the bridge
    does not read is refused rather than ignored: it could only have been meant to
    narrow the folder, and ignoring it would open more than the owner meant.
    """
    if not isinstance(item, dict):
        raise TypeError("not a folder")
    if set(item) != {"path", "subfolders"}:
        raise ValueError("not exactly path and subfolders")
    if not isinstance(item["path"], str) or not isinstance(item["subfolders"], bool):
        raise TypeError("path is not text, or subfolders is not true or false")
    if not item["path"]:
        raise ValueError("no path")
    return item


def _decode_folders(raw: str | None) -> list[dict[str, Any]]:
    """The gate's open folders, kept as sent. An empty list opens nothing.

    The header is base64url, padded or not, of UTF-8 JSON [{"path", "subfolders"}].
    Raises ValueError or TypeError for anything else.
    """
    if raw is None or not _BASE64URL.fullmatch(raw):
        raise ValueError("not base64url")
    text = base64.urlsafe_b64decode(raw + "=" * (-len(raw) % 4)).decode("utf-8")
    folders = json.loads(text)
    if not isinstance(folders, list):
        raise TypeError("not a list")
    return [_folder_entry(item) for item in folders]


#: An app's name as a session records it (`metadata.source`, "chatgpt" for a
#: ChatGPT import). A session with anything else there is of the app "unknown".
_SOURCE = re.compile(r"[a-z0-9][a-z0-9_.-]{0,31}")
UNKNOWN_SOURCE = "unknown"


def _session_source(metadata: Any) -> str:
    """The app a session came from, by the rule the owner's app counts them by."""
    source = metadata.get("source") if isinstance(metadata, dict) else None
    if isinstance(source, str) and _SOURCE.fullmatch(source):
        return source
    return UNKNOWN_SOURCE


def _source_entry(item: Any) -> str:
    """One app whose conversations without a folder are open, or TypeError/ValueError."""
    if not isinstance(item, str):
        raise TypeError("not an app name")
    if not _SOURCE.fullmatch(item):
        raise ValueError("not an app name")
    return item


def _decode_sources(raw: str | None) -> list[str]:
    """The apps whose conversations without a folder the gate opened, once each.

    The header is base64url, padded or not, of UTF-8 JSON ["chatgpt", ...]. A gate
    from before it sends none, which opens none. Raises ValueError or TypeError for
    anything else.
    """
    if raw is None:
        return []
    if not _BASE64URL.fullmatch(raw):
        raise ValueError("not base64url")
    text = base64.urlsafe_b64decode(raw + "=" * (-len(raw) % 4)).decode("utf-8")
    sources = json.loads(text)
    if not isinstance(sources, list):
        raise TypeError("not a list")
    return list(dict.fromkeys(_source_entry(item) for item in sources))


def _decode_tools(raw: str | None) -> frozenset[str]:
    """The owner's tools the gate opened, of OPENABLE_TOOLS; names past them are dropped.

    The header is base64url, padded or not, of UTF-8 JSON ["search", ...]. A gate
    from before it sends none, which opens DEFAULT_TEAMMATE_TOOLS. Raises ValueError
    or TypeError for anything else.
    """
    if raw is None:
        return frozenset(DEFAULT_TEAMMATE_TOOLS)
    if not _BASE64URL.fullmatch(raw):
        raise ValueError("not base64url")
    text = base64.urlsafe_b64decode(raw + "=" * (-len(raw) % 4)).decode("utf-8")
    tools = json.loads(text)
    if not isinstance(tools, list) or not all(isinstance(item, str) for item in tools):
        raise TypeError("not a list of names")
    return frozenset(tools) & frozenset(OPENABLE_TOOLS)


class GateScope(NamedTuple):
    """What the gate opened to a teammate."""

    #: The folders, as the gate sent them.
    folders: list[dict[str, Any]]
    #: The apps whose conversations without a folder are open.
    sources: list[str]
    #: The owner turned this teammate off for now: every call is refused.
    off: bool = False
    #: The owner's tools open to them.
    tools: frozenset[str] = frozenset(DEFAULT_TEAMMATE_TOOLS)


def _gate_scope() -> GateScope | None:
    """What the gate opened to this caller, or None when it opened everything.

    Fails closed: without an HTTP request, with the mode or the folders missing or
    repeated, an unknown mode, a repeated list of apps, or a list that does not
    read, the call is refused (RuntimeError).
    """
    try:
        headers = get_http_request().headers
    except RuntimeError:
        headers = {}
    mode = _gate_header(headers, SCOPE_MODE_HEADER)
    if mode == "all":
        return None
    if mode == "off":
        return GateScope([], [], off=True)
    if mode != "folders":
        raise RuntimeError(
            "This MCP server takes its scope from its gate, and "
            f"{SCOPE_MODE_HEADER} is missing, repeated or unknown"
        )
    try:
        folders = _decode_folders(_gate_header(headers, ALLOWED_FOLDERS_HEADER))
    except (TypeError, ValueError, RecursionError) as exc:
        raise RuntimeError(
            "This MCP server takes its scope from its gate, and "
            f"{ALLOWED_FOLDERS_HEADER} is missing, repeated or malformed"
        ) from exc
    sent = _gate_values(headers, ALLOWED_SOURCES_HEADER)
    try:
        if len(sent) > 1:
            raise ValueError("repeated")
        sources = _decode_sources(sent[0] if sent else None)
    except (TypeError, ValueError, RecursionError) as exc:
        raise RuntimeError(
            "This MCP server takes its scope from its gate, and "
            f"{ALLOWED_SOURCES_HEADER} is repeated or malformed"
        ) from exc
    sent = _gate_values(headers, ALLOWED_TOOLS_HEADER)
    try:
        if len(sent) > 1:
            raise ValueError("repeated")
        tools = _decode_tools(sent[0] if sent else None)
    except (TypeError, ValueError, RecursionError) as exc:
        raise RuntimeError(
            "This MCP server takes its scope from its gate, and "
            f"{ALLOWED_TOOLS_HEADER} is repeated or malformed"
        ) from exc
    return GateScope(folders, sources, tools=tools)


#: A path that starts at a Windows drive, once its backslashes read as "/".
_DRIVE_PATH = re.compile(r"[A-Za-z]:/")
_REPEATED_SLASHES = re.compile(r"/{2,}")


def _norm_path(path: str) -> str:
    """One spelling per folder, so a session's folder and an open folder compare.

    Backslashes read as "/", repeated "/" as one, and a trailing "/" is dropped
    except from a root ("/" or "C:/"). A path on a Windows drive ignores case, as
    Windows does; any other path keeps it.
    """
    text = _REPEATED_SLASHES.sub("/", path.replace("\\", "/"))
    drive = _DRIVE_PATH.match(text) is not None
    root = 3 if drive else 1
    if len(text) > root and text.endswith("/"):
        text = text[:-1]
    return text.casefold() if drive else text


def _folder_matcher(folders: list[dict[str, Any]]) -> Callable[[str | None], bool]:
    """Whether a session's folder, spelled by `_norm_path`, is open under `folders`.

    A session is open when its folder is one of them, or lies under one that opens
    its subfolders. Each folder above the session's is looked up once, so a long
    list of folders costs no more per session than a short one.
    """
    exact = {_norm_path(folder["path"]) for folder in folders}
    trees = {_norm_path(folder["path"]) for folder in folders if folder["subfolders"]}

    def holds(cwd: str | None) -> bool:
        if cwd is None:
            return False
        if cwd in exact:
            return True
        if not trees:
            return False
        # The folders above cwd end where one of its "/" starts, /a above /a/b, and
        # a root keeps its "/": "/" above /a, "c:/" above c:/a.
        cut = cwd.find("/")
        while cut != -1:
            if cwd[:cut] in trees or cwd[: cut + 1] in trees:
                return True
            cut = cwd.find("/", cut + 1)
        return False

    return holds


#: The clock the session lists are timed by. Tests replace it.
_clock = time.monotonic

_session_lists_lock = threading.Lock()


def _text_or_none(value: Any) -> str | None:
    return value if isinstance(value, str) and value else None


class ListedSession(NamedTuple):
    """One session as listed."""

    id: str
    #: The folder it started in, spelled by `_norm_path`, or None.
    folder: str | None
    #: For a session without a folder, the app it came from; None for one with.
    source: str | None
    #: The folder as the session recorded it, for showing.
    cwd: str | None = None
    #: The app it came from, folder or not (`_session_source`).
    app: str | None = None
    #: When its last turn was, as the collector recorded it, else when it last
    #: brought messages in, else when Honcho made the session.
    last_at: str | None = None
    #: The project and git branch the collector recorded, if any.
    project: str | None = None
    branch: str | None = None


#: Each workspace's sessions as last listed, and when: (listed_at, sessions).
_session_lists: dict[str, tuple[float, tuple[ListedSession, ...]]] = {}


def _listed_session(session_id: str, item: dict[str, Any]) -> ListedSession:
    metadata = item.get("metadata")
    metadata = metadata if isinstance(metadata, dict) else {}
    cwd = _text_or_none(metadata.get("cwd"))
    folder = _norm_path(cwd) if cwd else None
    app = _session_source(metadata)
    return ListedSession(
        id=session_id,
        folder=folder,
        source=None if folder is not None else app,
        cwd=cwd,
        app=app,
        last_at=_text_or_none(metadata.get("last_turn_at"))
        or _text_or_none(metadata.get("last_imported_at"))
        or _text_or_none(item.get("created_at")),
        project=_text_or_none(metadata.get("project_name")),
        branch=_text_or_none(metadata.get("git_branch")),
    )


def _list_sessions(workspace_id: str) -> tuple[ListedSession, ...]:
    """Every session in the workspace, by id, with the folder it started in.

    The folder is the session's `metadata.cwd`, which the collector records; a
    session without one in text is in no folder, and is known by its app instead
    (`_session_source`). Raises TypeError or ValueError for a listing that does not
    read, and whatever `_honcho` raises for one that fails.
    """
    path = f"/v3/workspaces/{workspace_id}/sessions/list"
    sessions: dict[str, ListedSession] = {}
    page = 1
    while True:
        listed = _honcho(
            "POST", path, body={}, params={"page": page, "size": SESSION_PAGE_SIZE}
        )
        items = listed.get("items") if isinstance(listed, dict) else None
        pages = listed.get("pages") if isinstance(listed, dict) else None
        if not isinstance(items, list) or not isinstance(pages, int):
            raise TypeError(f"Honcho's sessions, page {page}, do not read as a page")
        if pages > MAX_SESSION_PAGES:
            raise ValueError(
                f"Honcho lists {pages} pages of sessions, more than the "
                f"{MAX_SESSION_PAGES} this MCP server reads"
            )
        for item in items:
            session_id = item.get("id") if isinstance(item, dict) else None
            if not isinstance(session_id, str) or not session_id:
                raise ValueError("Honcho listed a session without an id")
            # A session that moves across a page boundary while the pages are read
            # shows up twice; it is one session.
            if session_id not in sessions:
                sessions[session_id] = _listed_session(session_id, item)
        if page >= pages:
            return tuple(sessions.values())
        page += 1


def _workspace_sessions(workspace_id: str) -> tuple[ListedSession, ...]:
    """The workspace's sessions as listed at most SESSION_LIST_TTL_SECONDS ago.

    One listing at a time: a call that arrives while one runs waits for it and
    takes its result, so a burst of questions lists the sessions once. A listing
    that fails is not kept, and the next call lists again.
    """
    with _session_lists_lock:
        started = _clock()
        kept = _session_lists.get(workspace_id)
        if kept is not None and started - kept[0] < SESSION_LIST_TTL_SECONDS:
            return kept[1]
        sessions = _list_sessions(workspace_id)
        # Lists past their time are dropped here, so naming many workspaces does
        # not grow the cache. Each is timed from when its listing began, so nothing
        # it missed is held back longer than the time-to-live.
        for name, (listed_at, _) in list(_session_lists.items()):
            if started - listed_at >= SESSION_LIST_TTL_SECONDS:
                del _session_lists[name]
        _session_lists[workspace_id] = (started, sessions)
        return sessions


def _open_sessions(
    workspace_id: str,
    folders: list[dict[str, Any]],
    sources: list[str] | tuple[str, ...] = (),
) -> list[str]:
    """The sessions in `folders`, and those without a folder from `sources`, by id.

    In the order Honcho lists them. Worked out when each call arrives, against the
    sessions as listed at most a minute ago: a folder the owner opens or closes
    applies to the next question, and a conversation started in an open folder, or
    one more ChatGPT conversation brought in, joins within a minute.
    """
    if not folders and not sources:
        return []
    holds = _folder_matcher(folders)
    apps = set(sources)
    return [
        session.id
        for session in _workspace_sessions(workspace_id)
        if holds(session.folder) or (session.folder is None and session.source in apps)
    ]


#: The refusal when the open folders hold no conversation. The same words whether
#: nothing is open or what is open is empty, so it says nothing about the memory.
NO_CONVERSATION = "No conversation in the folders open to you on this MCP server"

#: The refusal of a conversation named by id that is not open to the caller. The same
#: words whether it is closed or there is no such conversation.
NOT_OPEN = "No conversation with that id is open to you on this MCP server"


def _allowlist_refusal(sessions: list[str]) -> str | None:
    """Why a search cannot be confined to `sessions`, or None when it can."""
    return None if sessions else NO_CONVERSATION


def _scoped_items(
    sessions: list[str] | None,
    ask: Callable[[dict[str, Any] | None], Any],
    limit: int,
) -> list[dict[str, Any]]:
    """What `ask` finds, confined to `sessions` (None: not confined).

    `ask` makes one Honcho search with the `session_id` filter it is given. Up to
    MAX_SESSIONS_PER_SEARCH sessions that is one search; more are searched a share
    at a time, all at once, each share asked for `limit`, and the answers merged
    best first: every share's first, then every share's second, and so on, since
    Honcho returns no score to compare them by. The first `limit` are kept.
    """
    if sessions is None:
        return _items(ask(None))
    shares = [
        sessions[start : start + MAX_SESSIONS_PER_SEARCH]
        for start in range(0, len(sessions), MAX_SESSIONS_PER_SEARCH)
    ]
    if len(shares) == 1:
        return _items(ask({"in": shares[0]}))
    with ThreadPoolExecutor(max_workers=min(SEARCH_SHARE_WORKERS, len(shares))) as pool:
        answers = list(
            pool.map(
                lambda share: _items(contextvars.copy_context().run(ask, {"in": share})),
                shares,
            )
        )
    merged: list[dict[str, Any]] = []
    seen: set[Any] = set()
    for rank in range(max(len(answer) for answer in answers)):
        for answer in answers:
            if rank >= len(answer):
                continue
            item = answer[rank]
            key = item.get("id") or (item.get("session_id"), item.get("content"))
            if key in seen:
                continue
            seen.add(key)
            merged.append(item)
    return merged[:limit]


class TeammateReach(NamedTuple):
    """What one teammate's call may draw on, and why it is refused if it is."""

    #: The folders the gate opened to them, as it sent them.
    folders: list[dict[str, Any]]
    #: The sessions open to them, by id, or None when they were not listed.
    sessions: list[str] | None = None
    #: Why the call is refused, if it is.
    refusal: str | None = None
    #: The failure a refusal stands in for, kept for the audit log alone.
    failure: str | None = None
    #: The apps whose conversations without a folder the gate opened to them.
    sources: tuple[str, ...] = ()
    #: The owner's tools open to them.
    tools: frozenset[str] = frozenset()


def _teammate_reach(tool: str, arguments: dict[str, Any]) -> TeammateReach | None:
    """What one call may draw on, or None when it is not confined.

    Only a bridge behind a team server's gate confines calls, and only a teammate's:
    they may call the owner's tools the gate opened to them (and TEAMMATE_TOOLS, each
    while the tool it stands for is open), in the server's own workspace. A
    FOLDER_TOOLS one, or one that names a session, is held to the sessions in the
    folders the owner opened to them and those without a folder from the apps they
    opened. A teammate the owner turned off is refused whatever they call. A gate
    header that does not read raises RuntimeError. Once the folders are known, a
    refusal comes back in the reach instead, so the audit log can say which folders
    and how many conversations it was about.
    """
    scope = _gate_scope() if SCOPE_FROM_GATE else None
    if scope is None:
        return None
    folders, sources, tools = scope.folders, tuple(scope.sources), scope.tools
    if scope.off:
        return TeammateReach(folders, refusal=TURNED_OFF)
    if TEAMMATE_TOOLS.get(tool, tool) not in tools:
        opened = [name for name in OPENABLE_TOOLS if name in tools]
        return TeammateReach(
            folders,
            refusal=(
                f"{tool} is not open to you on this MCP server; "
                + (f"use {', '.join(opened)}" if opened else "no tool is")
            ),
            sources=sources,
            tools=tools,
        )
    asked_workspace = arguments.get("workspace_id")
    if asked_workspace and asked_workspace != DEFAULT_WORKSPACE_ID:
        return TeammateReach(
            folders,
            refusal="workspace_id cannot be changed on this MCP server; omit it",
            sources=sources,
            tools=tools,
        )
    asked = arguments.get("conversation_id") or arguments.get("session_id")
    if tool not in FOLDER_TOOLS and tool not in TEAMMATE_TOOLS and not asked:
        # The whole memory, or the server's setup: no session to hold it to.
        return TeammateReach(folders, sources=sources, tools=tools)
    # Listing the sessions is a Honcho call too, made only for a caller let in.
    _require_auth()
    workspace_id = DEFAULT_WORKSPACE_ID
    try:
        sessions = _open_sessions(workspace_id, folders, sources)
    except Exception as exc:  # noqa: BLE001 - refused and recorded, never a crash
        # The teammate hears only that the list could not be read; the cause goes
        # to the owner's audit log.
        return TeammateReach(
            folders,
            refusal=(
                "The conversations in the folders open to you could not be listed "
                "on this MCP server; try again later"
            ),
            failure=f"listing the sessions failed: {type(exc).__name__}: {exc}",
            sources=sources,
            tools=tools,
        )
    refusal = _allowlist_refusal(sessions)
    if refusal is None and asked and asked not in sessions:
        refusal = NOT_OPEN
    return TeammateReach(folders, sessions, refusal, sources=sources, tools=tools)


def _require_card_access(path: str, params: dict[str, Any] | None) -> None:
    if not HIDE_PEER_CARDS:
        return
    peer_card_route = "/peers/" in path and path.endswith(("/card", "/context"))
    session_card_route = (
        "/sessions/" in path
        and path.endswith("/context")
        and bool((params or {}).get("peer_target"))
    )
    if peer_card_route or session_card_route:
        raise RuntimeError(
            "Peer cards are disabled on this MCP server. "
            "Use get_representation, or get_session_context without peer_target."
        )


def _request(
    method: str,
    path: str,
    *,
    body: dict[str, Any] | list[Any] | None = None,
    params: dict[str, Any] | None = None,
) -> Any:
    _require_auth()
    _require_card_access(path, params)
    return _honcho(method, path, body=body, params=params)


def _honcho(
    method: str,
    path: str,
    *,
    body: dict[str, Any] | list[Any] | None = None,
    params: dict[str, Any] | None = None,
) -> Any:
    """One Honcho API call, for a caller already let in."""
    url = f"{HONCHO_BASE_URL}{path}"
    with httpx.Client(timeout=TIMEOUT_SECONDS) as client:
        resp = client.request(method, url, json=body, params=params)
    if resp.status_code >= 400:
        raise RuntimeError(f"Honcho API {resp.status_code} for {path}: {resp.text}")
    if resp.status_code == 204 or not resp.content:
        return {"ok": True, "status_code": resp.status_code}
    content_type = resp.headers.get("content-type", "")
    if "application/json" in content_type:
        return resp.json()
    return {"text": resp.text, "status_code": resp.status_code}


def _clean_none(d: dict[str, Any]) -> dict[str, Any]:
    return {k: v for k, v in d.items() if v is not None}


def _clamp(value: float | None, minimum: float, maximum: float) -> float | None:
    if value is None:
        return None
    return max(minimum, min(value, maximum))


def _normalize_message_list_options(
    filters: dict[str, Any] | None,
    *,
    page: int | None,
    size: int | None,
    reverse: bool,
) -> tuple[dict[str, Any] | None, dict[str, Any]]:
    """Keep pagination controls out of Honcho's database-column filters."""
    cleaned_filters = dict(filters or {})
    legacy_page = cleaned_filters.pop("page", None)
    legacy_size = cleaned_filters.pop("size", cleaned_filters.pop("limit", None))
    legacy_reverse = cleaned_filters.pop("reverse", None)
    resolved_page = page if page is not None else legacy_page
    resolved_size = size if size is not None else legacy_size
    if legacy_reverse is None:
        resolved_reverse = reverse
    elif isinstance(legacy_reverse, str):
        resolved_reverse = legacy_reverse.strip().lower() in {"1", "true", "yes", "on"}
    else:
        resolved_reverse = bool(legacy_reverse)
    params = {
        "reverse": str(resolved_reverse).lower(),
        "page": int(_clamp(int(resolved_page), 1, 1_000_000))
        if resolved_page is not None
        else None,
        "size": int(_clamp(int(resolved_size), 1, 100))
        if resolved_size is not None
        else None,
    }
    return cleaned_filters or None, _clean_none(params)


def _find_item_by_id(items: list[dict[str, Any]], item_id: str) -> dict[str, Any]:
    for item in items:
        if item.get("id") == item_id:
            return item
    raise RuntimeError(f"Resource not found: {item_id}")


def _coerce_session_peers(
    peers: list[dict[str, Any]] | dict[str, dict[str, Any]],
) -> dict[str, dict[str, Any]]:
    if isinstance(peers, dict):
        return peers
    result: dict[str, dict[str, Any]] = {}
    for item in peers:
        peer_id = item.get("peer_id") or item.get("id")
        if not peer_id:
            raise RuntimeError("Each peer item must include peer_id or id")
        result[str(peer_id)] = {
            "observe_me": bool(item.get("observe_me", True)),
            "observe_others": bool(item.get("observe_others", True)),
        }
    return result


def _coerce_messages(
    messages: list[dict[str, Any]],
    *,
    user_name: str,
    assistant_name: str,
) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for item in messages:
        peer_id = item.get("peer_id")
        role = item.get("role")
        if not peer_id:
            if role == "user":
                peer_id = user_name
            elif role == "assistant":
                peer_id = assistant_name
        if not peer_id:
            raise RuntimeError("Each message needs peer_id, or role=user/assistant")
        content = item.get("content")
        if content is None:
            raise RuntimeError("Each message needs content")
        payload = {"peer_id": str(peer_id), "content": str(content)}
        if item.get("metadata") is not None:
            payload["metadata"] = item["metadata"]
        if item.get("configuration") is not None:
            payload["configuration"] = item["configuration"]
        if item.get("created_at") is not None:
            payload["created_at"] = item["created_at"]
        out.append(payload)
    return out


def _held(*filters: dict[str, Any] | None) -> dict[str, Any] | None:
    """Every filter given, all of them to hold: the caller's and a teammate's limits."""
    parts = [item for item in filters if item]
    if not parts:
        return None
    return parts[0] if len(parts) == 1 else {"AND": parts}


def _clipped(answer: Any, size: int) -> Any:
    """Messages as a teammate gets them: each one's content cut to `size`.

    A long conversation read whole could outgrow what the Jev gate reads of an
    answer, which withholds it; cut, it stays readable and says how much is left.
    """
    if isinstance(answer, dict) and isinstance(answer.get("items"), list):
        return {**answer, "items": _clipped(answer["items"], size)}
    if isinstance(answer, list):
        return [_clipped(item, size) for item in answer]
    if isinstance(answer, dict) and isinstance(answer.get("content"), str):
        return {**answer, "content": _clip(answer["content"], size)}
    return answer


def _teammate_session(tool: str, session_id: str | None) -> TeammateReach | None:
    """A teammate's reach, once the session they named is one open to them."""
    reach = _reach_of(tool)
    if reach is not None and session_id:
        _open_conversation(reach, session_id)
    return reach


@register_tool(name="server_info")
def server_info(question: str | None = None) -> dict[str, Any]:
    """Inspect local MCP/Honcho bridge defaults and upstream health."""
    defaults = _resolve_defaults()
    health = _request("GET", "/health")
    return {
        "mcp_name": "Local Honcho MCP",
        "honcho_base_url": HONCHO_BASE_URL,
        "listen_host": LISTEN_HOST,
        "listen_port": LISTEN_PORT,
        "mcp_path": MCP_PATH,
        "auth_enabled": bool(OPTIONAL_BEARER_TOKEN),
        "auth_required": REQUIRE_AUTH,
        "read_only_mode": False,
        "exposed_tools": "all except disabled_tools",
        "tool_config_path": str(TOOL_CONFIG_PATH),
        "tool_config_required": REQUIRE_TOOL_CONFIG,
        "upstream_timeout_seconds": TIMEOUT_SECONDS,
        "disabled_tools": sorted(DISABLED_TOOL_NAMES),
        **defaults,
        "upstream_health": health,
    }


@register_tool(name="inspect_workspace")
def inspect_workspace(
    workspace_id: str | None = None, question: str | None = None
) -> dict[str, Any]:
    """Inspect one workspace by ID."""
    ws = _resolve_defaults(workspace_id=workspace_id)["workspace_id"]
    result = _request("POST", "/v3/workspaces/list", body={})
    return _find_item_by_id(result.get("items", []), ws)


@register_tool(name="list_workspaces")
def list_workspaces(
    filters: dict[str, Any] | None = None, question: str | None = None
) -> dict[str, Any]:
    """List all available workspaces. A teammate sees only this server's own."""
    reach = _reach_of("list_workspaces")
    body = _clean_none({"filters": filters})
    listed = _request("POST", "/v3/workspaces/list", body=body)
    if reach is not None and isinstance(listed, dict):
        items = [
            item
            for item in listed.get("items") or []
            if isinstance(item, dict) and item.get("id") == DEFAULT_WORKSPACE_ID
        ]
        return {**listed, "items": items, "total": len(items)}
    return listed


@register_tool(name="search")
def search(
    query: str,
    *,
    workspace_id: str | None = None,
    peer_id: str | None = None,
    session_id: str | None = None,
    limit: int = 10,
    filters: dict[str, Any] | None = None,
    question: str | None = None,
) -> Any:
    """Search messages at workspace scope, or scope to a peer or session.

    Words and meaning both: a message that holds the words, and one that says
    something like them. A teammate searches the conversations open to them.
    """
    ws = _resolve_defaults(workspace_id=workspace_id)["workspace_id"]
    size = int(_clamp(limit, 1, 100))
    reach = _teammate_session("search", session_id)
    if reach is not None:
        mine = _held(
            filters,
            {"peer_id": peer_id} if peer_id else None,
        )

        def ask(scope: dict[str, Any] | None) -> Any:
            return _honcho(
                "POST",
                f"/v3/workspaces/{ws}/search",
                body=_clean_none(
                    {
                        "query": query,
                        "limit": size,
                        "filters": _held(mine, {"session_id": scope or session_id}),
                    }
                ),
            )

        found = _scoped_items(None if session_id else _reach_sessions(reach), ask, size)
        return _clipped(found, SEARCH_MESSAGE_CHARS)
    body = _clean_none({"query": query, "limit": size, "filters": filters})
    if session_id:
        return _request(
            "POST", f"/v3/workspaces/{ws}/sessions/{session_id}/search", body=body
        )
    if peer_id:
        return _request(
            "POST", f"/v3/workspaces/{ws}/peers/{peer_id}/search", body=body
        )
    return _request("POST", f"/v3/workspaces/{ws}/search", body=body)


@register_tool(name="get_metadata")
def get_metadata(
    *,
    scope: str = "workspace",
    workspace_id: str | None = None,
    peer_id: str | None = None,
    session_id: str | None = None,
    question: str | None = None,
) -> dict[str, Any]:
    """Get metadata for a workspace, peer, or session."""
    _teammate_session("get_metadata", session_id if scope == "session" else None)
    ws = _resolve_defaults(workspace_id=workspace_id)["workspace_id"]
    if scope == "workspace":
        item = inspect_workspace(workspace_id=ws)
        return {"scope": scope, "id": ws, "metadata": item.get("metadata", {})}
    if scope == "peer":
        if not peer_id:
            raise RuntimeError("peer_id is required when scope='peer'")
        items = _request("POST", f"/v3/workspaces/{ws}/peers/list", body={}).get(
            "items", []
        )
        item = _find_item_by_id(items, peer_id)
        return {"scope": scope, "id": peer_id, "metadata": item.get("metadata", {})}
    if scope == "session":
        if not session_id:
            raise RuntimeError("session_id is required when scope='session'")
        items = _request("POST", f"/v3/workspaces/{ws}/sessions/list", body={}).get(
            "items", []
        )
        item = _find_item_by_id(items, session_id)
        return {"scope": scope, "id": session_id, "metadata": item.get("metadata", {})}
    raise RuntimeError("scope must be one of: workspace, peer, session")


@register_tool(name="set_metadata")
def set_metadata(
    metadata: dict[str, Any],
    *,
    scope: str = "workspace",
    workspace_id: str | None = None,
    peer_id: str | None = None,
    session_id: str | None = None,
    configuration: dict[str, Any] | None = None,
) -> Any:
    """Set metadata for a workspace, peer, or session."""
    ws = _resolve_defaults(workspace_id=workspace_id)["workspace_id"]
    body = _clean_none({"metadata": metadata, "configuration": configuration})
    if scope == "workspace":
        return _request("PUT", f"/v3/workspaces/{ws}", body=body)
    if scope == "peer":
        if not peer_id:
            raise RuntimeError("peer_id is required when scope='peer'")
        return _request("PUT", f"/v3/workspaces/{ws}/peers/{peer_id}", body=body)
    if scope == "session":
        if not session_id:
            raise RuntimeError("session_id is required when scope='session'")
        return _request("PUT", f"/v3/workspaces/{ws}/sessions/{session_id}", body=body)
    raise RuntimeError("scope must be one of: workspace, peer, session")


@register_tool(name="create_peer")
def create_peer(
    peer_id: str,
    *,
    workspace_id: str | None = None,
    metadata: dict[str, Any] | None = None,
    configuration: dict[str, Any] | None = None,
) -> Any:
    """Create or get a peer."""
    ws = _resolve_defaults(workspace_id=workspace_id)["workspace_id"]
    body = _clean_none(
        {"id": peer_id, "metadata": metadata, "configuration": configuration}
    )
    return _request("POST", f"/v3/workspaces/{ws}/peers", body=body)


@register_tool(name="list_peers")
def list_peers(
    *,
    workspace_id: str | None = None,
    filters: dict[str, Any] | None = None,
    question: str | None = None,
) -> Any:
    """List peers in a workspace."""
    ws = _resolve_defaults(workspace_id=workspace_id)["workspace_id"]
    return _request(
        "POST",
        f"/v3/workspaces/{ws}/peers/list",
        body=_clean_none({"filters": filters}),
    )


@register_tool(name="chat")
def chat(
    query: str,
    *,
    peer_id: str | None = None,
    target_peer_id: str | None = None,
    session_id: str | None = None,
    reasoning_level: str = "low",
    workspace_id: str | None = None,
    question: str | None = None,
) -> Any:
    """Ask Honcho what it knows about a peer using natural language.

    It answers from the whole memory, so a teammate the owner opened it to reads
    past their folders (WHOLE_MEMORY_TOOLS); a session named is still one open to
    them.
    """
    _teammate_session("chat", session_id)
    defaults = _resolve_defaults(workspace_id=workspace_id)
    ws = defaults["workspace_id"]
    observer = peer_id or defaults["assistant_name"]
    body = _clean_none(
        {
            "query": query,
            "target": target_peer_id,
            "session_id": session_id,
            "reasoning_level": reasoning_level,
        }
    )
    return _request("POST", f"/v3/workspaces/{ws}/peers/{observer}/chat", body=body)


@register_tool(name="get_peer_card")
def get_peer_card(
    peer_id: str,
    *,
    observer_id: str | None = None,
    workspace_id: str | None = None,
    question: str | None = None,
) -> Any:
    """Get a peer card for a target peer, defaulting to the assistant's perspective."""
    defaults = _resolve_defaults(workspace_id=workspace_id)
    ws = defaults["workspace_id"]
    observer = observer_id or defaults["assistant_name"]
    return _request(
        "GET", f"/v3/workspaces/{ws}/peers/{observer}/card", params={"target": peer_id}
    )


@register_tool(name="set_peer_card")
def set_peer_card(
    peer_id: str,
    peer_card: list[str],
    *,
    observer_id: str | None = None,
    workspace_id: str | None = None,
) -> Any:
    """Set a peer card for a target peer, defaulting to the assistant's perspective."""
    defaults = _resolve_defaults(workspace_id=workspace_id)
    ws = defaults["workspace_id"]
    observer = observer_id or defaults["assistant_name"]
    return _request(
        "PUT",
        f"/v3/workspaces/{ws}/peers/{observer}/card",
        body={"peer_card": peer_card},
        params={"target": peer_id},
    )


@register_tool(name="get_peer_context")
def get_peer_context(
    peer_id: str,
    *,
    observer_id: str | None = None,
    workspace_id: str | None = None,
    search_query: str | None = None,
    search_top_k: int | None = None,
    search_max_distance: float | None = None,
    include_most_frequent: bool = True,
    max_conclusions: int | None = None,
    question: str | None = None,
) -> Any:
    """Get representation + peer card for a target peer."""
    defaults = _resolve_defaults(workspace_id=workspace_id)
    ws = defaults["workspace_id"]
    observer = observer_id or defaults["assistant_name"]
    params = _clean_none(
        {
            "target": peer_id,
            "search_query": search_query,
            "search_top_k": _clamp(search_top_k, 1, 100),
            "search_max_distance": _clamp(search_max_distance, 0.0, 1.0),
            "include_most_frequent": include_most_frequent,
            "max_conclusions": _clamp(max_conclusions, 1, 100),
        }
    )
    return _request(
        "GET", f"/v3/workspaces/{ws}/peers/{observer}/context", params=params
    )


@register_tool(name="get_representation")
def get_representation(
    peer_id: str,
    *,
    observer_id: str | None = None,
    workspace_id: str | None = None,
    session_id: str | None = None,
    search_query: str | None = None,
    search_top_k: int | None = None,
    search_max_distance: float | None = None,
    include_most_frequent: bool | None = None,
    max_conclusions: int | None = None,
    question: str | None = None,
) -> Any:
    """Get a textual representation for a target peer."""
    _teammate_session("get_representation", session_id)
    defaults = _resolve_defaults(workspace_id=workspace_id)
    ws = defaults["workspace_id"]
    observer = observer_id or defaults["assistant_name"]
    body = _clean_none(
        {
            "target": peer_id,
            "session_id": session_id,
            "search_query": search_query,
            "search_top_k": _clamp(search_top_k, 1, 100),
            "search_max_distance": _clamp(search_max_distance, 0.0, 1.0),
            "include_most_frequent": include_most_frequent,
            "max_conclusions": _clamp(max_conclusions, 1, 100),
        }
    )
    return _request(
        "POST", f"/v3/workspaces/{ws}/peers/{observer}/representation", body=body
    )


@register_tool(name="create_session")
def create_session(
    session_id: str,
    *,
    workspace_id: str | None = None,
    metadata: dict[str, Any] | None = None,
    configuration: dict[str, Any] | None = None,
    peers: list[dict[str, Any]] | dict[str, dict[str, Any]] | None = None,
) -> Any:
    """Create or get a session."""
    ws = _resolve_defaults(workspace_id=workspace_id)["workspace_id"]
    body: dict[str, Any] = {"id": session_id}
    if metadata is not None:
        body["metadata"] = metadata
    if configuration is not None:
        body["configuration"] = configuration
    if peers is not None:
        body["peers"] = _coerce_session_peers(peers)
    return _request("POST", f"/v3/workspaces/{ws}/sessions", body=body)


@register_tool(name="list_sessions")
def list_sessions(
    *,
    workspace_id: str | None = None,
    filters: dict[str, Any] | None = None,
    page: int | None = None,
    size: int | None = None,
    question: str | None = None,
) -> Any:
    """List sessions in a workspace, a page at a time. A teammate lists the open ones."""
    ws = _resolve_defaults(workspace_id=workspace_id)["workspace_id"]
    reach = _reach_of("list_sessions")
    if reach is not None:
        # Past this many, the first share: one Postgres query takes no more ids.
        opened = _reach_sessions(reach)[:MAX_SESSIONS_PER_SEARCH]
        filters = _held(filters, {"id": {"in": opened}})
    params = _clean_none(
        {
            "page": int(_clamp(page, 1, 1_000_000)) if page is not None else None,
            "size": int(_clamp(size, 1, 100)) if size is not None else None,
        }
    )
    return _request(
        "POST",
        f"/v3/workspaces/{ws}/sessions/list",
        body=_clean_none({"filters": filters}),
        params=params or None,
    )


@register_tool(name="delete_session")
def delete_session(session_id: str, *, workspace_id: str | None = None) -> Any:
    """Delete a session."""
    ws = _resolve_defaults(workspace_id=workspace_id)["workspace_id"]
    return _request("DELETE", f"/v3/workspaces/{ws}/sessions/{session_id}")


@register_tool(name="clone_session")
def clone_session(
    session_id: str,
    *,
    workspace_id: str | None = None,
    message_id: str | None = None,
) -> Any:
    """Clone a session, optionally cutting off at a specific message ID."""
    ws = _resolve_defaults(workspace_id=workspace_id)["workspace_id"]
    params = _clean_none({"message_id": message_id})
    return _request(
        "POST", f"/v3/workspaces/{ws}/sessions/{session_id}/clone", params=params
    )


@register_tool(name="add_peers_to_session")
def add_peers_to_session(
    session_id: str,
    peers: list[dict[str, Any]] | dict[str, dict[str, Any]],
    *,
    workspace_id: str | None = None,
) -> Any:
    """Add peers to a session. Accepts either a peer->config map or a list of peer config objects."""
    ws = _resolve_defaults(workspace_id=workspace_id)["workspace_id"]
    return _request(
        "POST",
        f"/v3/workspaces/{ws}/sessions/{session_id}/peers",
        body=_coerce_session_peers(peers),
    )


@register_tool(name="remove_peers_from_session")
def remove_peers_from_session(
    session_id: str,
    peer_ids: list[str],
    *,
    workspace_id: str | None = None,
) -> Any:
    """Remove peers from a session."""
    ws = _resolve_defaults(workspace_id=workspace_id)["workspace_id"]
    return _request(
        "DELETE", f"/v3/workspaces/{ws}/sessions/{session_id}/peers", body=peer_ids
    )


@register_tool(name="get_session_peers")
def get_session_peers(
    session_id: str, *, workspace_id: str | None = None, question: str | None = None
) -> Any:
    """List peers in a session."""
    _teammate_session("get_session_peers", session_id)
    ws = _resolve_defaults(workspace_id=workspace_id)["workspace_id"]
    return _request("GET", f"/v3/workspaces/{ws}/sessions/{session_id}/peers")


@register_tool(name="inspect_session")
def inspect_session(
    session_id: str, *, workspace_id: str | None = None, question: str | None = None
) -> Any:
    """Inspect one session by ID."""
    _teammate_session("inspect_session", session_id)
    ws = _resolve_defaults(workspace_id=workspace_id)["workspace_id"]
    result = _request("POST", f"/v3/workspaces/{ws}/sessions/list", body={})
    return _find_item_by_id(result.get("items", []), session_id)


@register_tool(name="add_messages_to_session")
def add_messages_to_session(
    session_id: str,
    messages: list[dict[str, Any]],
    *,
    workspace_id: str | None = None,
) -> Any:
    """Add messages to a session. Each message needs peer_id+content, or role=user/assistant+content."""
    defaults = _resolve_defaults(workspace_id=workspace_id)
    ws = defaults["workspace_id"]
    body = {
        "messages": _coerce_messages(
            messages,
            user_name=defaults["user_name"],
            assistant_name=defaults["assistant_name"],
        )
    }
    return _request(
        "POST", f"/v3/workspaces/{ws}/sessions/{session_id}/messages", body=body
    )


@register_tool(name="get_session_messages")
def get_session_messages(
    session_id: str,
    *,
    workspace_id: str | None = None,
    filters: dict[str, Any] | None = None,
    page: int | None = None,
    size: int | None = None,
    reverse: bool = False,
    question: str | None = None,
) -> Any:
    """List messages in a session."""
    reach = _teammate_session("get_session_messages", session_id)
    ws = _resolve_defaults(workspace_id=workspace_id)["workspace_id"]
    normalized_filters, params = _normalize_message_list_options(
        filters, page=page, size=size, reverse=reverse
    )
    if reach is not None:
        params["size"] = min(params.get("size", MAX_READ_MESSAGES), MAX_READ_MESSAGES)
    listed = _request(
        "POST",
        f"/v3/workspaces/{ws}/sessions/{session_id}/messages/list",
        body=_clean_none({"filters": normalized_filters}),
        params=params,
    )
    return listed if reach is None else _clipped(listed, READ_MESSAGE_CHARS)


@register_tool(name="get_session_message")
def get_session_message(
    session_id: str,
    message_id: str,
    *,
    workspace_id: str | None = None,
    question: str | None = None,
) -> Any:
    """Get one message from a session."""
    reach = _teammate_session("get_session_message", session_id)
    ws = _resolve_defaults(workspace_id=workspace_id)["workspace_id"]
    found = _request(
        "GET", f"/v3/workspaces/{ws}/sessions/{session_id}/messages/{message_id}"
    )
    return found if reach is None else _clipped(found, READ_MESSAGE_CHARS)


@register_tool(name="get_session_context")
def get_session_context(
    session_id: str,
    *,
    workspace_id: str | None = None,
    tokens: int | None = None,
    summary: bool = True,
    search_query: str | None = None,
    peer_target: str | None = None,
    peer_perspective: str | None = None,
    limit_to_session: bool = False,
    search_top_k: int | None = None,
    search_max_distance: float | None = None,
    include_most_frequent: bool = False,
    max_conclusions: int | None = None,
    question: str | None = None,
) -> Any:
    """Get LLM-ready context for a session, optionally including peer representation/card.

    A peer's representation is drawn from the whole memory: a teammate gets it only
    while get_representation is open to them too.
    """
    reach = _teammate_session("get_session_context", session_id)
    if (
        reach is not None
        and (peer_target or peer_perspective)
        and "get_representation" not in reach.tools
    ):
        raise RuntimeError(
            "peer_target and peer_perspective draw on the whole memory, and "
            "get_representation is not open to you on this MCP server; omit them"
        )
    ws = _resolve_defaults(workspace_id=workspace_id)["workspace_id"]
    params = _clean_none(
        {
            "tokens": tokens,
            "summary": str(summary).lower(),
            "search_query": search_query,
            "peer_target": peer_target,
            "peer_perspective": peer_perspective,
            "limit_to_session": str(limit_to_session).lower(),
            "search_top_k": _clamp(search_top_k, 1, 100),
            "search_max_distance": _clamp(search_max_distance, 0.0, 1.0),
            "include_most_frequent": str(include_most_frequent).lower(),
            "max_conclusions": _clamp(max_conclusions, 1, 100),
        }
    )
    return _request(
        "GET", f"/v3/workspaces/{ws}/sessions/{session_id}/context", params=params
    )


@register_tool(name="list_conclusions")
def list_conclusions(
    *,
    workspace_id: str | None = None,
    filters: dict[str, Any] | None = None,
    reverse: bool = False,
    question: str | None = None,
) -> Any:
    """List conclusions in a workspace.

    A teammate gets only those drawn from one conversation open to them: one that
    combines several could carry what is not open.
    """
    ws = _resolve_defaults(workspace_id=workspace_id)["workspace_id"]
    reach = _reach_of("list_conclusions")
    if reach is not None:
        opened = _reach_sessions(reach)[:MAX_SESSIONS_PER_SEARCH]
        filters = _held(filters, {"level": "explicit"}, {"session_id": {"in": opened}})
    params = {"reverse": str(reverse).lower()}
    body = _clean_none({"filters": filters})
    return _request(
        "POST", f"/v3/workspaces/{ws}/conclusions/list", body=body, params=params
    )


@register_tool(name="query_conclusions")
def query_conclusions(
    query: str,
    *,
    observer_id: str,
    observed_id: str,
    workspace_id: str | None = None,
    top_k: int = 10,
    distance: float | None = None,
    filters: dict[str, Any] | None = None,
    question: str | None = None,
) -> Any:
    """Semantic search across derived conclusions.

    A teammate gets only those drawn from one conversation open to them: one that
    combines several could carry what is not open.
    """
    ws = _resolve_defaults(workspace_id=workspace_id)["workspace_id"]
    merged_filters = dict(filters or {})
    merged_filters.setdefault("observer", observer_id)
    merged_filters.setdefault("observed", observed_id)
    reach = _reach_of("query_conclusions")
    if reach is not None:
        # Honcho reads the observer and observed at the top; the rest must all hold.
        pair = {
            key: merged_filters.pop(key)
            for key in ("observer", "observed", "observer_id", "observed_id")
            if key in merged_filters
        }
        size = int(_clamp(top_k, 1, MAX_FOUND_CONCLUSIONS))

        def ask(scope: dict[str, Any] | None) -> Any:
            return _honcho(
                "POST",
                f"/v3/workspaces/{ws}/conclusions/query",
                body=_clean_none(
                    {
                        "query": query,
                        "top_k": size,
                        "distance": distance,
                        "filters": {
                            **pair,
                            **_held(
                                merged_filters or None,
                                {"level": "explicit"},
                                {"session_id": scope},
                            ),
                        },
                    }
                ),
            )

        return _scoped_items(_reach_sessions(reach), ask, size)
    body = _clean_none(
        {
            "query": query,
            "top_k": top_k,
            "distance": distance,
            "filters": merged_filters,
        }
    )
    return _request("POST", f"/v3/workspaces/{ws}/conclusions/query", body=body)


@register_tool(name="create_conclusions")
def create_conclusions(
    conclusions: list[dict[str, Any]],
    *,
    workspace_id: str | None = None,
) -> Any:
    """Create one or more conclusions."""
    ws = _resolve_defaults(workspace_id=workspace_id)["workspace_id"]
    return _request(
        "POST", f"/v3/workspaces/{ws}/conclusions", body={"conclusions": conclusions}
    )


@register_tool(name="delete_conclusion")
def delete_conclusion(conclusion_id: str, *, workspace_id: str | None = None) -> Any:
    """Delete a conclusion by ID."""
    ws = _resolve_defaults(workspace_id=workspace_id)["workspace_id"]
    return _request("DELETE", f"/v3/workspaces/{ws}/conclusions/{conclusion_id}")


@register_tool(name="schedule_dream")
def schedule_dream(
    observer_id: str,
    *,
    workspace_id: str | None = None,
    observed_id: str | None = None,
    dream_type: str = "omni",
    session_id: str | None = None,
) -> Any:
    """Trigger dream/consolidation work for a peer pair."""
    ws = _resolve_defaults(workspace_id=workspace_id)["workspace_id"]
    body = _clean_none(
        {
            "observer": observer_id,
            "observed": observed_id,
            "dream_type": dream_type,
            "session_id": session_id,
        }
    )
    return _request("POST", f"/v3/workspaces/{ws}/schedule_dream", body=body)


@register_tool(name="get_queue_status")
def get_queue_status(
    *,
    workspace_id: str | None = None,
    observer_id: str | None = None,
    sender_id: str | None = None,
    session_id: str | None = None,
    question: str | None = None,
) -> Any:
    """Inspect the Honcho derivation queue."""
    ws = _resolve_defaults(workspace_id=workspace_id)["workspace_id"]
    params = _clean_none(
        {
            "observer_id": observer_id,
            "sender_id": sender_id,
            "session_id": session_id,
        }
    )
    return _request("GET", f"/v3/workspaces/{ws}/queue/status", params=params)


# ------------------------------------------------ conversations, for a teammate
#
# What a teammate's own agent reads to answer from the conversations open to them,
# the way the owner's agent reads its own memory: the list of conversations, a
# search over their messages, one conversation in order, and the facts Honcho drew
# from them. On a team server each holds a teammate to the sessions open to them;
# the owner, and a bridge outside a team, reach every session. Times are Honcho's,
# in UTC: the asking side shows them in its own time zone.

#: The longest message a search shows, and a read; the rest is cut and counted.
SEARCH_MESSAGE_CHARS = 1500
READ_MESSAGE_CHARS = 3000
#: How much of a conversation's first message its row in the list shows.
OPENING_CHARS = 200
#: The most conversations one list shows, messages one search finds, messages one
#: read shows, and facts one search of them finds.
MAX_LISTED_CONVERSATIONS = 100
MAX_FOUND_MESSAGES = 30
MAX_READ_MESSAGES = 50
MAX_FOUND_CONCLUSIONS = 30
#: Conversations whose start is looked up at once.
HEAD_WORKERS = 8
#: Conversation starts kept at most; all are dropped when there are more.
MAX_KEPT_HEADS = 20_000

_EPOCH = datetime.min.replace(tzinfo=UTC)
_DATE_ONLY = re.compile(r"\d{4}-\d{2}-\d{2}")


def _clip(text: Any, size: int) -> str:
    flat = "" if text is None else str(text)
    if len(flat) <= size:
        return flat
    return f"{flat[:size]} …[{len(flat) - size} more characters]"


def _moment(value: Any) -> datetime | None:
    """A time as Honcho or the collector wrote it, or None."""
    if not isinstance(value, str) or not value:
        return None
    try:
        moment = datetime.fromisoformat(value)
    except ValueError:
        return None
    return moment if moment.tzinfo else moment.replace(tzinfo=UTC)


def _bound(value: str | None, name: str, *, end: bool = False) -> datetime | None:
    """A time the caller gave, or None. A bare date is that whole day in UTC."""
    if value is None or not str(value).strip():
        return None
    text = str(value).strip()
    moment = _moment(text)
    if moment is None:
        raise RuntimeError(
            f"{name} takes a date or a time, such as 2026-10-12 or "
            "2026-10-12T09:00:00+09:00"
        )
    if end and _DATE_ONLY.fullmatch(text):
        moment += timedelta(days=1, microseconds=-1)
    return moment


def _reach_of(tool: str) -> TeammateReach | None:
    """The reach the wrapper worked out for this call, or the same worked out here."""
    reach = _call_reach.get()
    if reach is _UNRESOLVED:
        reach = _teammate_reach(tool, {})
    if reach is not None and reach.refusal:
        raise RuntimeError(reach.refusal)
    return reach


def _open_conversation(reach: TeammateReach | None, conversation_id: str) -> None:
    if reach is not None and conversation_id not in (reach.sessions or ()):
        raise RuntimeError(NOT_OPEN)


def _reach_sessions(reach: TeammateReach | None) -> list[str] | None:
    """The sessions a teammate is held to, or None for a call that is not confined."""
    return None if reach is None else list(reach.sessions or ())


def _items(answer: Any) -> list[dict[str, Any]]:
    """The items of a Honcho answer, as a list or a page of one."""
    items = answer.get("items") if isinstance(answer, dict) else answer
    return [item for item in items if isinstance(item, dict)] if isinstance(items, list) else []


def _said(item: dict[str, Any], size: int) -> dict[str, Any]:
    """One message as a teammate reads it: when, who, and what, cut to `size`."""
    return {
        "at": item.get("created_at"),
        "speaker": item.get("peer_id"),
        "content": _clip(item.get("content"), size),
    }


def _folder_shown(cwd: str | None, reach: TeammateReach | None) -> str | None:
    """A conversation's folder as the caller reads it: its path for the owner, its
    name alone for a teammate (provenance.folder_name)."""
    if cwd is None or reach is None:
        return cwd
    return provenance.folder_name(cwd)


_heads_lock = threading.Lock()
#: Each conversation's start as last looked up, by (workspace, session):
#: (the session's last_at then, the start). A session that brings in more
#: messages changes its last_at, and its start is looked up again.
_heads: dict[tuple[str, str], tuple[str | None, dict[str, Any]]] = {}


def _conversation_head(
    workspace_id: str, session: ListedSession, owner: str
) -> dict[str, Any]:
    """When a conversation started, how many messages it has, and how it opened."""
    key = (workspace_id, session.id)
    with _heads_lock:
        kept = _heads.get(key)
    if kept is not None and kept[0] == session.last_at:
        return kept[1]
    try:
        page = _honcho(
            "POST",
            f"/v3/workspaces/{workspace_id}/sessions/{quote(session.id, safe='')}"
            "/messages/list",
            body={},
            params={"page": 1, "size": 5},
        )
    except Exception as exc:  # noqa: BLE001 - one unreadable conversation is shown bare
        logger.warning("conversation start not read: %s", type(exc).__name__)
        return {"started_at": None, "messages": None, "opening": ""}
    items = _items(page)
    opening = next((item for item in items if item.get("peer_id") == owner), None)
    opening = opening or (items[0] if items else None)
    total = page.get("total") if isinstance(page, dict) else None
    head = {
        "started_at": items[0].get("created_at") if items else None,
        "messages": total if isinstance(total, int) else None,
        "opening": _clip(" ".join(str(opening.get("content") or "").split()), OPENING_CHARS)
        if opening
        else "",
    }
    with _heads_lock:
        if len(_heads) >= MAX_KEPT_HEADS:
            _heads.clear()
        _heads[key] = (session.last_at, head)
    return head


def _heads_of(
    workspace_id: str, sessions: list[ListedSession], owner: str
) -> dict[str, dict[str, Any]]:
    if not sessions:
        return {}
    with ThreadPoolExecutor(max_workers=min(HEAD_WORKERS, len(sessions))) as pool:
        heads = list(
            pool.map(lambda item: _conversation_head(workspace_id, item, owner), sessions)
        )
    return {session.id: head for session, head in zip(sessions, heads, strict=True)}


@register_tool(name="list_conversations")
def list_conversations(
    *,
    since: str | None = None,
    until: str | None = None,
    contains: str | None = None,
    limit: int = 30,
    question: str | None = None,
) -> dict[str, Any]:
    """The conversations you may read, the most recently active first.

    Each comes with its id, when it started and last ran, how many messages it has,
    its folder, app, project and git branch, and how it opened. `since` keeps those
    active on or after a time and `until` those started on or before one (a date or
    an ISO time; a bare date is a whole day in UTC); `contains` keeps those whose
    folder, project, branch or opening holds every word given. `question` is what
    your user asked, in their own words: the memory's owner sees it in their log.
    """
    reach = _reach_of("list_conversations")
    _require_auth()
    defaults = _resolve_defaults()
    workspace_id, owner = defaults["workspace_id"], defaults["user_name"]
    start = _bound(since, "since")
    end = _bound(until, "until", end=True)
    words = [word.casefold() for word in (contains or "").split()]
    sessions = list(_workspace_sessions(workspace_id))
    if reach is not None:
        allowed = set(reach.sessions or ())
        sessions = [session for session in sessions if session.id in allowed]
    if start is not None:
        sessions = [
            session
            for session in sessions
            if (_moment(session.last_at) or start) >= start
        ]
    sessions.sort(key=lambda session: _moment(session.last_at) or _EPOCH, reverse=True)
    shown_size = int(_clamp(limit, 1, MAX_LISTED_CONVERSATIONS))
    if end is not None or words:
        # Both need each conversation's start: looked up for every one left, and
        # kept until the conversation brings in more messages.
        heads = _heads_of(workspace_id, sessions, owner)

        def kept(session: ListedSession) -> bool:
            head = heads[session.id]
            started = _moment(head["started_at"]) or _moment(session.last_at)
            if end is not None and started is not None and started > end:
                return False
            said = " ".join(
                [
                    _folder_shown(session.cwd, reach) or "",
                    session.project or "",
                    session.branch or "",
                    head["opening"],
                ]
            ).casefold()
            return all(word in said for word in words)

        sessions = [session for session in sessions if kept(session)]
        shown = sessions[:shown_size]
    else:
        shown = sessions[:shown_size]
        heads = _heads_of(workspace_id, shown, owner)
    return {
        "conversations": [
            {
                "id": session.id,
                "started_at": heads[session.id]["started_at"],
                "last_at": session.last_at,
                "messages": heads[session.id]["messages"],
                "folder": _folder_shown(session.cwd, reach),
                "app": session.app,
                "project": session.project,
                "branch": session.branch,
                "opening": heads[session.id]["opening"],
            }
            for session in shown
        ],
        "total": len(sessions),
        "shown": len(shown),
    }


@register_tool(name="search_messages")
def search_messages(
    query: str,
    *,
    limit: int = 10,
    since: str | None = None,
    until: str | None = None,
    conversation_id: str | None = None,
    question: str | None = None,
) -> dict[str, Any]:
    """Search the messages of the conversations you may read, by meaning.

    Both the user's messages and their agents' are searched; each match comes with
    its conversation, when it was said and by whom. `since` and `until` hold the
    search to a stretch of time (a date or an ISO time; a bare date is a whole day
    in UTC), and `conversation_id` to one conversation. `question` is what your user
    asked, in their own words: the memory's owner sees it in their log.
    """
    reach = _reach_of("search_messages")
    if conversation_id:
        _open_conversation(reach, conversation_id)
    workspace_id = _resolve_defaults()["workspace_id"]
    filters: dict[str, Any] = {}
    times = {
        name: moment.isoformat()
        for name, moment in (
            ("gte", _bound(since, "since")),
            ("lte", _bound(until, "until", end=True)),
        )
        if moment is not None
    }
    if times:
        filters["created_at"] = times
    if conversation_id:
        filters["session_id"] = conversation_id
    size = int(_clamp(limit, 1, MAX_FOUND_MESSAGES))
    _require_auth()

    def ask(scope: dict[str, Any] | None) -> Any:
        return _honcho(
            "POST",
            f"/v3/workspaces/{workspace_id}/search",
            body=_clean_none(
                {
                    "query": query,
                    "limit": size,
                    "filters": ({**filters, "session_id": scope} if scope else filters) or None,
                }
            ),
        )

    found = _scoped_items(None if conversation_id else _reach_sessions(reach), ask, size)
    return {
        "messages": [
            {
                "conversation_id": item.get("session_id"),
                **_said(item, SEARCH_MESSAGE_CHARS),
            }
            for item in found
        ]
    }


@register_tool(name="read_conversation")
def read_conversation(
    conversation_id: str,
    *,
    page: int = 1,
    size: int = 20,
    newest_first: bool = False,
    question: str | None = None,
) -> dict[str, Any]:
    """Read one conversation you may read, in order, a page at a time.

    Page 1 is its start, or with `newest_first` its latest messages. `question` is
    what your user asked, in their own words: the memory's owner sees it in their
    log.
    """
    reach = _reach_of("read_conversation")
    _open_conversation(reach, conversation_id)
    workspace_id = _resolve_defaults()["workspace_id"]
    listed = _request(
        "POST",
        f"/v3/workspaces/{workspace_id}/sessions/{quote(conversation_id, safe='')}"
        "/messages/list",
        body={},
        params={
            "page": int(_clamp(page, 1, 1_000_000)),
            "size": int(_clamp(size, 1, MAX_READ_MESSAGES)),
            "reverse": str(bool(newest_first)).lower(),
        },
    )
    session = next(
        (item for item in _workspace_sessions(workspace_id) if item.id == conversation_id),
        None,
    )
    page_of = listed if isinstance(listed, dict) else {}
    return {
        "id": conversation_id,
        "folder": _folder_shown(session.cwd if session else None, reach),
        "app": session.app if session else None,
        "total": page_of.get("total"),
        "page": page_of.get("page"),
        "pages": page_of.get("pages"),
        "newest_first": bool(newest_first),
        "messages": [_said(item, READ_MESSAGE_CHARS) for item in _items(listed)],
    }


@register_tool(name="search_conclusions")
def search_conclusions(
    query: str,
    *,
    limit: int = 10,
    question: str | None = None,
) -> dict[str, Any]:
    """Search the facts Honcho drew about the memory's owner from the conversations
    you may read, by meaning: one sentence each, with the conversation it came from
    and when that conversation last ran.

    A teammate gets only the facts drawn from one conversation at a time: those that
    combine several could carry what is not open to them. `question` is what your
    user asked, in their own words: the memory's owner sees it in their log.
    """
    reach = _reach_of("search_conclusions")
    defaults = _resolve_defaults()
    workspace_id, owner = defaults["workspace_id"], defaults["user_name"]
    filters: dict[str, Any] = {"observer": owner, "observed": owner}
    if reach is not None:
        filters["level"] = "explicit"
    size = int(_clamp(limit, 1, MAX_FOUND_CONCLUSIONS))
    _require_auth()

    def ask(scope: dict[str, Any] | None) -> Any:
        return _honcho(
            "POST",
            f"/v3/workspaces/{workspace_id}/conclusions/query",
            body={
                "query": query,
                "top_k": size,
                "filters": {**filters, "session_id": scope} if scope else filters,
            },
        )

    found = _scoped_items(_reach_sessions(reach), ask, size)
    last_ran = {
        session.id: session.last_at for session in _workspace_sessions(workspace_id)
    }
    return {
        "conclusions": [
            {
                "conversation_id": item.get("session_id"),
                "conversation_last_at": last_ran.get(item.get("session_id")),
                "content": item.get("content"),
            }
            for item in _items(found)
        ]
    }


# ------------------------------------------------------------- the dashboard's routes
#
# Two routes for the owner's dashboard, beside /mcp and never under it, so the
# server's gate, which passes on /mcp alone, never lets a teammate reach them: the
# audit log, and a trial of the Jev gate. HONCHO_AUDIT_READ turns both on, and both
# take the bridge's own bearer token.


def _dashboard_authorized(request: Any) -> bool:
    if not OPTIONAL_BEARER_TOKEN:
        return False
    auth = request.headers.get("authorization", "")
    return secrets.compare_digest(auth, f"Bearer {OPTIONAL_BEARER_TOKEN}")


#: The caller a trial names to Jev, in place of a teammate's email.
TRIAL_CALLER = "guard-trial"


def _verdict_view(verdict: jev_gate.Verdict) -> dict[str, Any]:
    return {
        "allowed": verdict.allowed,
        "score": verdict.score,
        "reason": verdict.reason,
        "unjudged": verdict.unjudged,
    }


def _found_text(found: list[dict[str, Any]]) -> str:
    """A trial's found messages as the owner reads them: who and when, then what."""
    return "\n\n".join(
        f"{item.get('speaker') or '?'} · {item.get('at') or '?'}\n{item.get('content') or ''}"
        for item in found
    )


#: The tool a trial goes through: what a teammate's question first runs.
TRIAL_TOOL = "search_messages"
#: The messages a trial's search finds, as a teammate's search finds by default.
TRIAL_FOUND = 10


def guard_trial(
    query: str,
    *,
    answer: str | None = None,
    folders: list[dict[str, Any]] | None = None,
    sources: list[str] | None = None,
) -> dict[str, Any]:
    """What a teammate asking `query` would get, step by step, for the owner to try.

    The query is judged as a teammate's question is. If it may go on, what they
    would read is `answer` as the owner wrote it, else the messages a teammate's
    search for it finds in the conversations in `folders` and those without a
    folder of the apps in `sources` (`found`), and that is judged as what a
    teammate reads is. `outcome` is "refused" (the query), "withheld" (what they
    would read), "passed", or "no_answer" when the search could not run, or what is
    open holds no conversation; with neither an
    answer nor folders or apps only the query is judged. Nothing is recorded: a
    trial is not a teammate's call.
    """
    defaults = _resolve_defaults()
    workspace = defaults["workspace_id"]
    verdict = jev_gate.judge(
        tool=TRIAL_TOOL,
        query=query,
        caller=TRIAL_CALLER,
        workspace_id=workspace,
        teammate=True,
    )
    trial: dict[str, Any] = {
        "gate": jev_gate.enabled_for(TRIAL_TOOL, teammate=True),
        "message": jev_gate.MESSAGE,
        "query": _verdict_view(verdict),
        "answer": None,
        "answer_check": None,
    }
    if not verdict.allowed:
        return {**trial, "outcome": "refused"}
    result: Any
    found: list[dict[str, Any]] | None = None
    if answer is not None:
        result = answer
    elif folders is not None or sources is not None:
        # Searched as a teammate's search_messages searches: the sessions open,
        # refused where a teammate's search is refused.
        try:
            sessions = _open_sessions(workspace, folders or [], sources or [])
            refusal = _allowlist_refusal(sessions)
            if refusal is not None:
                return {**trial, "outcome": "no_answer", "error": refusal}
            searched = _scoped_items(
                sessions,
                lambda scope: _honcho(
                    "POST",
                    f"/v3/workspaces/{workspace}/search",
                    body={"query": query, "limit": TRIAL_FOUND, "filters": {"session_id": scope}},
                ),
                TRIAL_FOUND,
            )
        except (RuntimeError, httpx.HTTPError, TypeError, ValueError) as exc:
            return {**trial, "outcome": "no_answer", "error": str(exc) or type(exc).__name__}
        found = [
            {"conversation_id": item.get("session_id"), **_said(item, SEARCH_MESSAGE_CHARS)}
            for item in searched
        ]
        result = {"messages": found}
    else:
        return {**trial, "outcome": "passed"}
    # Hidden as a teammate's answer is, before it is judged and shown.
    result, masked = redact.redact(result)
    if found is not None:
        found = result["messages"]
    checked = jev_gate.judge_answer(
        tool=TRIAL_TOOL,
        query=query,
        result=redact.unnamed(result),
        caller=TRIAL_CALLER,
        workspace_id=workspace,
        teammate=True,
    )
    return {
        **trial,
        "answer": jev_gate.text_of(result) if found is None else _found_text(found),
        **({"found": found} if found is not None else {}),
        "masked": masked,
        "answer_check": _verdict_view(checked),
        "outcome": "passed" if checked.allowed else "withheld",
    }


#: The most folders a trial takes, the longest path in one, and the most apps.
MAX_TRIAL_FOLDERS = 500
MAX_TRIAL_PATH = 4096
MAX_TRIAL_SOURCES = 32


def _trial_input(body: Any) -> dict[str, Any] | str:
    """guard_trial's arguments from a request body, or why they cannot be read."""
    if not isinstance(body, dict):
        return "the body is a JSON object"
    query = body.get("query")
    if not isinstance(query, str) or not query.strip():
        return "query takes the question to try"
    answer = body.get("answer")
    if answer is not None and (not isinstance(answer, str) or not answer.strip()):
        return "answer takes the answer to try, or is left out"
    folders = body.get("folders")
    if folders is not None:
        try:
            if not isinstance(folders, list) or len(folders) > MAX_TRIAL_FOLDERS:
                raise ValueError("not a list of folders")
            for item in folders:
                if len(_folder_entry(item)["path"]) > MAX_TRIAL_PATH:
                    raise ValueError("a path too long")
        except (TypeError, ValueError):
            return (
                f"folders takes at most {MAX_TRIAL_FOLDERS} folders, each "
                f'{{"path": at most {MAX_TRIAL_PATH} characters, "subfolders": '
                "true or false}, or is left out"
            )
    sources = body.get("sources")
    if sources is not None:
        try:
            if not isinstance(sources, list) or len(sources) > MAX_TRIAL_SOURCES:
                raise ValueError("not a list of apps")
            sources = list(dict.fromkeys(_source_entry(item) for item in sources))
        except (TypeError, ValueError):
            return (
                f"sources takes at most {MAX_TRIAL_SOURCES} app names such as "
                '"chatgpt", or is left out'
            )
    return {"query": query, "answer": answer, "folders": folders, "sources": sources}


def _int_param(params: Any, name: str, default: int | None) -> int | None:
    raw = params.get(name)
    if raw is None or str(raw).strip() == "":
        return default
    try:
        return int(raw)
    except ValueError:
        return default


if audit.READ_ENABLED:

    @mcp.custom_route("/audit", methods=["GET"])
    async def audit_read(request: Any) -> Any:
        """The owner's read of the audit log.

        Off unless HONCHO_AUDIT_READ is set, so the bridge teammates talk to never
        serves it. The dashboard is the only intended caller.
        """
        import asyncio

        from starlette.responses import JSONResponse

        if not _dashboard_authorized(request):
            return JSONResponse({"error": "unauthorized"}, status_code=401)
        params = request.query_params
        try:
            payload = await asyncio.to_thread(
                audit.read,
                limit=_int_param(params, "limit", 100) or 100,
                caller=params.get("caller") or None,
                tool=params.get("tool") or None,
                status=params.get("status") or None,
                bridge=params.get("bridge") or None,
                hours=_int_param(params, "hours", None),
            )
        except Exception as exc:  # noqa: BLE001 - reported to the dashboard as JSON
            logger.warning("audit read failed: %s: %s", type(exc).__name__, exc)
            return JSONResponse(
                {"error": "audit read failed", "detail": str(exc)}, status_code=500
            )
        return JSONResponse(payload)

    @mcp.custom_route("/guard-trial", methods=["POST"])
    async def guard_trial_route(request: Any) -> Any:
        """The owner's trial of the Jev gate (see guard_trial), from the dashboard."""
        import asyncio

        from starlette.responses import JSONResponse

        if not _dashboard_authorized(request):
            return JSONResponse({"error": "unauthorized"}, status_code=401)
        try:
            body = await request.json()
        except ValueError:
            body = None
        trial = _trial_input(body)
        if isinstance(trial, str):
            return JSONResponse({"error": "bad_request", "detail": trial}, status_code=400)
        try:
            payload = await asyncio.to_thread(
                guard_trial,
                trial["query"],
                answer=trial["answer"],
                folders=trial["folders"],
                sources=trial["sources"],
            )
        except Exception as exc:  # noqa: BLE001 - reported to the dashboard as JSON
            logger.warning("guard trial failed: %s", type(exc).__name__)
            return JSONResponse(
                {"error": "guard trial failed", "detail": type(exc).__name__},
                status_code=500,
            )
        return JSONResponse(payload)


if __name__ == "__main__":
    if MCP_TRANSPORT == "stdio":
        mcp.run(transport="stdio", show_banner=False)
    else:
        from starlette.middleware import Middleware

        mcp.run(
            transport="streamable-http",
            host=LISTEN_HOST,
            port=LISTEN_PORT,
            path=MCP_PATH,
            show_banner=False,
            middleware=[Middleware(BearerGate)],
        )
