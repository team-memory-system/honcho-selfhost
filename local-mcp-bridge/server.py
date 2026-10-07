from __future__ import annotations

import base64
import functools
import inspect
import json
import logging
import os
import re
import secrets
import threading
import time
from pathlib import Path
from typing import Any

import httpx
from fastmcp import FastMCP
from fastmcp.server.dependencies import get_http_request

import audit
import jev_gate

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

#: Take each caller's projects from the gate of a team server. The gate checks the
#: caller's Cloudflare Access login, drops every x-honcho-* header the caller sent,
#: and says who is calling: the server's owner (`all`) or a teammate (`projects`,
#: with the projects the owner opened to them). Anything else is refused.
SCOPE_FROM_GATE = _env_flag("HONCHO_MCP_SCOPE_FROM_GATE")
SCOPE_MODE_HEADER = "x-honcho-scope-mode"
ALLOWED_SCOPES_HEADER = "x-honcho-allowed-scopes"
PROJECT_ID = re.compile(r"p-[0-9a-f]{12}")

#: A teammate's `chat` that names no project asks each open project in turn, up to
#: this many.
MAX_PROJECTS_ASKED = 5

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


def register_tool(*, name: str):
    """Register one tool, routed through the audit log and the Jev gate.

    Every tool goes through here, so this is the single place that records what was
    asked and the single place a query can be refused: a disabled tool, a pinned
    bridge's other workspace or peer, a call outside a teammate's projects, then the
    Jev gate. Nested calls (``get_metadata`` reaching for ``inspect_workspace``) are
    logged once, at the outermost call.
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
                if not violation:
                    try:
                        projects = _projects_asked(name, arguments)
                    except RuntimeError as exc:
                        violation = str(exc)
                    else:
                        if projects is not None:
                            # Recorded whether or not the caller named one, so the
                            # log says which projects each answer came from.
                            arguments["projects"] = projects
                if violation:
                    audit.record(
                        tool=name,
                        caller=caller,
                        caller_source=caller_source,
                        arguments=arguments,
                        workspace_id=workspace_id,
                        status="denied",
                        error=violation,
                        duration_ms=elapsed_ms(),
                    )
                    raise RuntimeError(violation)

                verdict = jev_gate.judge(
                    tool=name,
                    query=audit.query_text_of(arguments) or "",
                    caller=caller,
                    workspace_id=workspace_id,
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

                try:
                    result = fn(*args, **kwargs)
                except Exception as exc:
                    audit.record(
                        tool=name,
                        caller=caller,
                        caller_source=caller_source,
                        arguments=arguments,
                        workspace_id=workspace_id,
                        status="error",
                        error=f"{type(exc).__name__}: {exc}",
                        duration_ms=elapsed_ms(),
                        jev_score=verdict.score,
                    )
                    raise
                audit.record(
                    tool=name,
                    caller=caller,
                    caller_source=caller_source,
                    arguments=arguments,
                    workspace_id=workspace_id,
                    status="ok",
                    duration_ms=elapsed_ms(),
                    jev_score=verdict.score,
                )
                return result
            finally:
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


def _gate_header(headers: Any, name: str) -> str | None:
    """One header the gate sets, or None when it is missing or repeated.

    The gate sends each exactly once, so a second copy did not come from the gate.
    """
    getlist = getattr(headers, "getlist", None)
    if callable(getlist):
        values = list(getlist(name))
    else:
        value = headers.get(name)
        values = [] if value is None else [value]
    return values[0] if len(values) == 1 else None


def _decode_projects(raw: str | None) -> list[dict[str, str]]:
    """The gate's allowed list: base64url, padded or not, of JSON [{"id", "name"}].

    Raises ValueError or TypeError for anything else, including an id that is not a
    project id.
    """
    if raw is None:
        raise ValueError("no list")
    padded = raw + "=" * (-len(raw) % 4)
    items = json.loads(base64.b64decode(padded, altchars=b"-_", validate=True))
    if not isinstance(items, list):
        raise TypeError("not a list")
    projects: dict[str, dict[str, str]] = {}
    for item in items:
        if not isinstance(item, dict):
            raise TypeError("not an object")
        project_id, name = item.get("id"), item.get("name")
        if not isinstance(project_id, str) or not PROJECT_ID.fullmatch(project_id):
            raise ValueError("not a project id")
        if not isinstance(name, str) or not name.strip():
            raise ValueError("no name")
        projects.setdefault(project_id, {"id": project_id, "name": name})
    return list(projects.values())


def _gate_projects() -> list[dict[str, str]] | None:
    """The projects the gate opened to this caller, or None when it opened them all.

    Fails closed: without an HTTP request, with a header missing or repeated, an
    unknown mode, or a list that does not read, the call is refused (RuntimeError).
    """
    try:
        headers = get_http_request().headers
    except RuntimeError:
        headers = {}
    mode = _gate_header(headers, SCOPE_MODE_HEADER)
    if mode == "all":
        return None
    if mode != "projects":
        raise RuntimeError(
            "This MCP server takes its scope from its gate, and "
            f"{SCOPE_MODE_HEADER} is missing, repeated or unknown"
        )
    try:
        return _decode_projects(_gate_header(headers, ALLOWED_SCOPES_HEADER))
    except (TypeError, ValueError) as exc:
        raise RuntimeError(
            "This MCP server takes its scope from its gate, and "
            f"{ALLOWED_SCOPES_HEADER} is missing, repeated or malformed"
        ) from exc


def _find_project(opened: list[dict[str, str]], wanted: str) -> dict[str, str]:
    """The open project that `wanted` names, by id or by name, in any case."""
    key = wanted.strip().casefold()
    matches = [project for project in opened if project["id"] == key] or [
        project for project in opened if project["name"].strip().casefold() == key
    ]
    if len(matches) == 1:
        return matches[0]
    if matches:
        ids = ", ".join(project["id"] for project in matches)
        raise RuntimeError(
            "Several projects open to you on this MCP server have that name; "
            f"name one by id: {ids}"
        )
    names = ", ".join(project["name"] for project in opened)
    raise RuntimeError(
        f"That project is not open to you on this MCP server. Open to you: {names}"
    )


def _projects_asked(
    tool: str, arguments: dict[str, Any]
) -> list[dict[str, str]] | None:
    """The projects one call answers from, or None when it is not confined to any.

    Only a bridge behind a team server's gate confines calls, and only a teammate's:
    they may `chat`, about the project they name or about each open project when
    they name none. Raises RuntimeError, with the reason, for a refused call.
    """
    project = arguments.get("project")
    opened = _gate_projects() if SCOPE_FROM_GATE else None
    if opened is None:
        if project and not SCOPE_FROM_GATE:
            raise RuntimeError("project cannot be used on this MCP server; omit it")
        if project:
            raise RuntimeError(
                "project is not needed: every project on this MCP server is open "
                "to you; omit it"
            )
        return None
    if tool != "chat":
        raise RuntimeError(
            f"{tool} cannot be used with projects on this MCP server; use chat"
        )
    if not opened:
        raise RuntimeError("No project on this MCP server is open to you")
    # Honcho takes a scope instead of these, never with them.
    if arguments.get("session_id"):
        raise RuntimeError(
            "session_id cannot be used with projects on this MCP server; omit it"
        )
    if arguments.get("filters"):
        raise RuntimeError(
            "filters cannot be used with projects on this MCP server; omit them"
        )
    if project:
        return [_find_project(opened, project)]
    if len(opened) > MAX_PROJECTS_ASKED:
        names = ", ".join(item["name"] for item in opened)
        raise RuntimeError(
            f"{len(opened)} projects are open to you on this MCP server, more than "
            f"{MAX_PROJECTS_ASKED} to ask at once; name one with project: {names}"
        )
    return opened


def _ask_each_project(
    path: str, body: dict[str, Any], projects: list[dict[str, str]]
) -> Any:
    """Ask Honcho once per project, each call confined to that project's scope.

    Never a list of scopes: Honcho answers one scope as the scope itself, but for a
    list it keeps the path peer as the observer and mixes that peer's whole card
    into the answer. Several projects are asked one by one.
    """

    def ask(project: dict[str, str]) -> Any:
        return _request("POST", path, body={**body, "scope": project["id"]})

    if len(projects) == 1:
        return ask(projects[0])
    answers: list[dict[str, Any]] = []
    failures: list[RuntimeError] = []
    for project in projects:
        try:
            answers.append({"project": project["name"], "answer": ask(project)})
        except RuntimeError as exc:
            # Honcho refused this project alone, typically one with no conversation
            # yet, which has no scope. The others still answer.
            failures.append(exc)
            answers.append({"project": project["name"], "error": str(exc)})
    if len(failures) == len(projects):
        raise failures[0]
    return {"answers": answers}


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


@register_tool(name="server_info")
def server_info() -> dict[str, Any]:
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
def inspect_workspace(workspace_id: str | None = None) -> dict[str, Any]:
    """Inspect one workspace by ID."""
    ws = _resolve_defaults(workspace_id=workspace_id)["workspace_id"]
    result = _request("POST", "/v3/workspaces/list", body={})
    return _find_item_by_id(result.get("items", []), ws)


@register_tool(name="list_workspaces")
def list_workspaces(filters: dict[str, Any] | None = None) -> dict[str, Any]:
    """List all available workspaces."""
    body = _clean_none({"filters": filters})
    return _request("POST", "/v3/workspaces/list", body=body)


@register_tool(name="search")
def search(
    query: str,
    *,
    workspace_id: str | None = None,
    peer_id: str | None = None,
    session_id: str | None = None,
    limit: int = 10,
    filters: dict[str, Any] | None = None,
) -> Any:
    """Search messages at workspace scope, or scope to a peer or session."""
    ws = _resolve_defaults(workspace_id=workspace_id)["workspace_id"]
    body = _clean_none(
        {"query": query, "limit": int(_clamp(limit, 1, 100)), "filters": filters}
    )
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
) -> dict[str, Any]:
    """Get metadata for a workspace, peer, or session."""
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
    project: str | None = None,
) -> Any:
    """Ask Honcho what it knows about a peer using natural language.

    On a team server a teammate is answered only from the projects its owner opened
    to them, about the owner unless peer_id or target_peer_id names another peer.
    `project` picks one of them, by id or by name, in any case. Without it, a single
    open project is used, and two to five are each asked and returned as
    {"answers": [{"project": name, "answer": ...}]} (a project Honcho could not
    answer has "error" instead of "answer"); with more, name one. session_id cannot
    be used with projects. Anywhere else, omit project.
    """
    projects = _projects_asked("chat", {"project": project, "session_id": session_id})
    defaults = _resolve_defaults(workspace_id=workspace_id)
    ws = defaults["workspace_id"]
    if projects is not None:
        # With a scope Honcho answers as the scope itself, and the peer in the path
        # is only the one asked about. That is the server's owner, whose work the
        # projects hold, unless the caller names another peer.
        subject = peer_id or defaults["user_name"] or defaults["assistant_name"]
        body = _clean_none(
            {
                "query": query,
                "target": target_peer_id,
                "reasoning_level": reasoning_level,
            }
        )
        return _ask_each_project(
            f"/v3/workspaces/{ws}/peers/{subject}/chat", body, projects
        )
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
) -> Any:
    """Get a textual representation for a target peer."""
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
) -> Any:
    """List sessions in a workspace."""
    ws = _resolve_defaults(workspace_id=workspace_id)["workspace_id"]
    return _request(
        "POST",
        f"/v3/workspaces/{ws}/sessions/list",
        body=_clean_none({"filters": filters}),
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
def get_session_peers(session_id: str, *, workspace_id: str | None = None) -> Any:
    """List peers in a session."""
    ws = _resolve_defaults(workspace_id=workspace_id)["workspace_id"]
    return _request("GET", f"/v3/workspaces/{ws}/sessions/{session_id}/peers")


@register_tool(name="inspect_session")
def inspect_session(session_id: str, *, workspace_id: str | None = None) -> Any:
    """Inspect one session by ID."""
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
) -> Any:
    """List messages in a session."""
    ws = _resolve_defaults(workspace_id=workspace_id)["workspace_id"]
    normalized_filters, params = _normalize_message_list_options(
        filters, page=page, size=size, reverse=reverse
    )
    return _request(
        "POST",
        f"/v3/workspaces/{ws}/sessions/{session_id}/messages/list",
        body=_clean_none({"filters": normalized_filters}),
        params=params,
    )


@register_tool(name="get_session_message")
def get_session_message(
    session_id: str,
    message_id: str,
    *,
    workspace_id: str | None = None,
) -> Any:
    """Get one message from a session."""
    ws = _resolve_defaults(workspace_id=workspace_id)["workspace_id"]
    return _request(
        "GET", f"/v3/workspaces/{ws}/sessions/{session_id}/messages/{message_id}"
    )


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
) -> Any:
    """Get LLM-ready context for a session, optionally including peer representation/card."""
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
) -> Any:
    """List conclusions in a workspace."""
    ws = _resolve_defaults(workspace_id=workspace_id)["workspace_id"]
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
) -> Any:
    """Semantic search across derived conclusions."""
    ws = _resolve_defaults(workspace_id=workspace_id)["workspace_id"]
    merged_filters = dict(filters or {})
    merged_filters.setdefault("observer", observer_id)
    merged_filters.setdefault("observed", observed_id)
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


# --------------------------------------------------------------------- audit read


def _audit_read_authorized(request: Any) -> bool:
    if not OPTIONAL_BEARER_TOKEN:
        return False
    auth = request.headers.get("authorization", "")
    return secrets.compare_digest(auth, f"Bearer {OPTIONAL_BEARER_TOKEN}")


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

        if not _audit_read_authorized(request):
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
