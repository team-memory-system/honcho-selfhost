from __future__ import annotations

import json
import os
import secrets
from pathlib import Path
from typing import Any

import httpx
from fastmcp import FastMCP
from fastmcp.server.dependencies import get_http_request

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
TIMEOUT_SECONDS = float(os.environ.get("HONCHO_MCP_TIMEOUT", "300"))

TOOL_CONFIG_PATH = Path(
    os.environ.get("HONCHO_MCP_TOOL_CONFIG", DEFAULT_RUNTIME_DIR / "tool-config.json")
).expanduser()

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

mcp = FastMCP(
    name="Local Honcho MCP",
    instructions=(
        "This MCP server exposes a local self-hosted Honcho instance. "
        "Tool availability is controlled by the dashboard tool configuration. "
        "Use search/get_peer_context/get_representation/get_session_context for recall."
    ),
)


def register_tool(*, name: str):
    if name in DISABLED_TOOL_NAMES:

        def _disabled(fn):
            return fn

        return _disabled
    return mcp.tool(name=name)


def _require_auth() -> None:
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
    request = get_http_request()
    headers = request.headers
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


def _request(
    method: str,
    path: str,
    *,
    body: dict[str, Any] | list[Any] | None = None,
    params: dict[str, Any] | None = None,
) -> Any:
    _require_auth()
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
    body = _clean_none({"query": query, "limit": limit, "filters": filters})
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
) -> Any:
    """Ask Honcho what it knows about a peer using natural language."""
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
            "search_top_k": search_top_k,
            "search_max_distance": search_max_distance,
            "include_most_frequent": include_most_frequent,
            "max_conclusions": max_conclusions,
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
            "search_top_k": search_top_k,
            "search_max_distance": search_max_distance,
            "include_most_frequent": include_most_frequent,
            "max_conclusions": max_conclusions,
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
) -> Any:
    """List messages in a session."""
    ws = _resolve_defaults(workspace_id=workspace_id)["workspace_id"]
    return _request(
        "POST",
        f"/v3/workspaces/{ws}/sessions/{session_id}/messages/list",
        body=_clean_none({"filters": filters}),
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
            "search_top_k": search_top_k,
            "search_max_distance": search_max_distance,
            "include_most_frequent": str(include_most_frequent).lower(),
            "max_conclusions": max_conclusions,
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


if __name__ == "__main__":
    mcp.run(
        transport="streamable-http",
        host=LISTEN_HOST,
        port=LISTEN_PORT,
        path=MCP_PATH,
        show_banner=False,
    )
