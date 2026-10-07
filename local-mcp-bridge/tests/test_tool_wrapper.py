"""The audit log and the Jev gate both hang off `register_tool`.

These tests hold that choke point closed: every tool goes through it, a refused
query never reaches Honcho, and a tool still works when the audit log does not.
"""

from __future__ import annotations

import asyncio
import importlib
import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

import audit
import jev_gate
import server

#: Every tool the bridge exposes. A new tool must be added here deliberately, so
#: that nobody widens a shared bridge by accident.
ALL_TOOLS = frozenset(
    {
        "add_messages_to_session",
        "add_peers_to_session",
        "chat",
        "clone_session",
        "create_conclusions",
        "create_peer",
        "create_session",
        "delete_conclusion",
        "delete_session",
        "get_metadata",
        "get_peer_card",
        "get_peer_context",
        "get_queue_status",
        "get_representation",
        "get_session_context",
        "get_session_message",
        "get_session_messages",
        "get_session_peers",
        "inspect_session",
        "inspect_workspace",
        "list_conclusions",
        "list_peers",
        "list_sessions",
        "list_workspaces",
        "query_conclusions",
        "remove_peers_from_session",
        "schedule_dream",
        "search",
        "server_info",
        "set_metadata",
        "set_peer_card",
    }
)


def _listed_tools(module: Any) -> set[str]:
    tools = asyncio.run(module.mcp.list_tools())
    return {getattr(tool, "name", None) or tool.key for tool in tools}


def _reload_server(monkeypatch: pytest.MonkeyPatch, env: dict[str, str]) -> Any:
    """Import a fresh `server` under a different environment.

    Registration happens at import time, so a tool-list question is an import
    question. The module is restored afterwards by the autouse fixture.
    """
    for key, value in env.items():
        monkeypatch.setenv(key, value)
    return importlib.reload(server)


@pytest.fixture(autouse=True)
def _restore_server_module() -> Any:
    yield
    importlib.reload(server)


@pytest.fixture
def audited(monkeypatch: pytest.MonkeyPatch) -> list[dict[str, Any]]:
    """Capture audit rows without a database."""
    rows: list[dict[str, Any]] = []

    def fake_record(**kwargs: Any) -> None:
        rows.append(kwargs)

    monkeypatch.setattr(server.audit, "record", fake_record)
    return rows


@pytest.fixture
def no_upstream(monkeypatch: pytest.MonkeyPatch) -> list[tuple[str, str]]:
    """Answer every Honcho call locally, and record that it happened."""
    seen: list[tuple[str, str]] = []

    def fake_request(method: str, path: str, **kwargs: Any) -> Any:
        seen.append((method, path))
        # Enough shape for the tools that read an item out of a list response.
        return {"items": [{"id": server.DEFAULT_WORKSPACE_ID, "metadata": {}}], "ok": True}

    monkeypatch.setattr(server, "_request", fake_request)
    return seen


# --------------------------------------------------------------- registration


def test_every_tool_is_still_registered(monkeypatch: pytest.MonkeyPatch) -> None:
    """The wrapper must not swallow a tool. 31 names, plus nothing extra."""
    fresh = _reload_server(
        monkeypatch,
        {
            "HONCHO_MCP_TOOL_CONFIG": "/nonexistent/tool-config.json",
            "HONCHO_MCP_REQUIRE_TOOL_CONFIG": "0",
            "HONCHO_MCP_ENABLED_TOOLS": "",
            "HONCHO_MCP_HIDE_PEER_CARDS": "0",
        },
    )
    listed = _listed_tools(fresh)
    assert listed == ALL_TOOLS, {
        "missing": sorted(ALL_TOOLS - listed),
        "unexpected": sorted(listed - ALL_TOOLS),
    }


def test_the_tool_count_is_pinned() -> None:
    """A tool added upstream has to be added here too, so nobody widens a bridge
    by accident when the denylist is the only thing standing in the way."""
    assert len(ALL_TOOLS) == 31


def test_an_allowlist_beats_the_config_file(monkeypatch: pytest.MonkeyPatch) -> None:
    """A shared bridge names what it exposes, instead of listing what it hides."""
    fresh = _reload_server(monkeypatch, {"HONCHO_MCP_ENABLED_TOOLS": "chat"})
    assert _listed_tools(fresh) == {"chat"}


def test_an_allowlist_ignores_a_denylist(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    config = tmp_path / "tool-config.json"
    config.write_text(json.dumps({"version": 1, "disabled_tools": ["chat"]}))
    fresh = _reload_server(
        monkeypatch,
        {
            "HONCHO_MCP_ENABLED_TOOLS": "chat,search",
            "HONCHO_MCP_TOOL_CONFIG": str(config),
        },
    )
    assert _listed_tools(fresh) == {"chat", "search"}


def test_a_denylist_still_works(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    config = tmp_path / "tool-config.json"
    config.write_text(json.dumps({"version": 1, "disabled_tools": ["delete_session"]}))
    fresh = _reload_server(monkeypatch, {"HONCHO_MCP_TOOL_CONFIG": str(config)})
    assert "delete_session" not in _listed_tools(fresh)
    assert "chat" in _listed_tools(fresh)


# ---------------------------------------------------------------- audit trail


def test_a_tool_call_is_recorded_with_its_query(
    monkeypatch: pytest.MonkeyPatch,
    audited: list[dict[str, Any]],
    no_upstream: list[tuple[str, str]],
) -> None:
    monkeypatch.setattr(
        server,
        "get_http_request",
        lambda: SimpleNamespace(
            headers={"cf-access-authenticated-user-email": "mate@example.com"},
            client=SimpleNamespace(host="10.0.0.9"),
        ),
    )

    server.search("지난주 배포 결정", limit=5)

    assert len(audited) == 1
    row = audited[0]
    assert row["tool"] == "search"
    assert row["caller"] == "mate@example.com"
    assert row["caller_source"] == "cf-access"
    assert row["status"] == "ok"
    assert row["arguments"]["query"] == "지난주 배포 결정"
    assert row["workspace_id"] == "memory"
    assert row["duration_ms"] >= 0


def test_a_failing_tool_is_recorded_as_an_error(
    monkeypatch: pytest.MonkeyPatch, audited: list[dict[str, Any]]
) -> None:
    def boom(*args: Any, **kwargs: Any) -> Any:
        raise RuntimeError("Honcho API 500")

    monkeypatch.setattr(server, "_request", boom)
    monkeypatch.setattr(
        server, "get_http_request", lambda: SimpleNamespace(headers={}, client=None)
    )

    with pytest.raises(RuntimeError, match="Honcho API 500"):
        server.search("q")

    assert audited[0]["status"] == "error"
    assert "Honcho API 500" in audited[0]["error"]


def test_a_nested_call_is_recorded_once(
    monkeypatch: pytest.MonkeyPatch,
    audited: list[dict[str, Any]],
    no_upstream: list[tuple[str, str]],
) -> None:
    """`get_metadata` reaches for `inspect_workspace`; that is one caller action."""
    monkeypatch.setattr(
        server, "get_http_request", lambda: SimpleNamespace(headers={}, client=None)
    )

    server.get_metadata(scope="workspace")

    assert [row["tool"] for row in audited] == ["get_metadata"]


@pytest.mark.parametrize(
    ("headers", "caller", "source"),
    [
        (
            {
                "cf-access-authenticated-user-email": "a@b.c",
                "x-honcho-user-name": "spoofed",
            },
            "a@b.c",
            "cf-access",
        ),
        ({"cf-access-client-id": "token.access"}, "token.access", "cf-service-token"),
        ({"x-honcho-user-name": "mate"}, "mate", "x-honcho-user-name"),
        ({}, "10.0.0.9", "address"),
    ],
)
def test_identity_prefers_what_the_caller_cannot_choose(
    monkeypatch: pytest.MonkeyPatch,
    headers: dict[str, str],
    caller: str,
    source: str,
) -> None:
    monkeypatch.setattr(
        server,
        "get_http_request",
        lambda: SimpleNamespace(headers=headers, client=SimpleNamespace(host="10.0.0.9")),
    )
    assert server._caller_identity() == (caller, source)


def test_a_stdio_client_is_labelled(monkeypatch: pytest.MonkeyPatch) -> None:
    def no_request() -> Any:
        raise RuntimeError("no http request")

    monkeypatch.setattr(server, "get_http_request", no_request)
    assert server._caller_identity() == ("local-stdio", "stdio")


def test_a_broken_audit_log_does_not_break_the_tool(
    monkeypatch: pytest.MonkeyPatch, no_upstream: list[tuple[str, str]]
) -> None:
    """Auditing is bookkeeping. Losing it must not cost a caller their answer."""
    monkeypatch.setattr(
        audit, "DSN", "postgresql://127.0.0.1:1/definitely-not-there"
    )
    monkeypatch.setattr(audit, "_writer", audit._Writer())
    monkeypatch.setattr(
        server, "get_http_request", lambda: SimpleNamespace(headers={}, client=None)
    )

    assert server.search("q")["ok"] is True
    assert no_upstream == [("POST", "/v3/workspaces/memory/search")]


# ------------------------------------------------------------------ jev gate


def test_a_refused_query_never_reaches_honcho(
    monkeypatch: pytest.MonkeyPatch, audited: list[dict[str, Any]]
) -> None:
    def unexpected(*args: Any, **kwargs: Any) -> Any:
        pytest.fail("A refused query was forwarded to Honcho")

    monkeypatch.setattr(server, "_request", unexpected)
    monkeypatch.setattr(
        server,
        "jev_gate",
        SimpleNamespace(
            judge=lambda **_: jev_gate.Verdict(
                allowed=False, score=0.95, reason="out of scope"
            ),
            MESSAGE="refused",
        ),
    )
    monkeypatch.setattr(
        server, "get_http_request", lambda: SimpleNamespace(headers={}, client=None)
    )

    with pytest.raises(RuntimeError, match="refused"):
        server.chat("집 주소")

    assert audited[0]["status"] == "denied"
    assert audited[0]["jev_score"] == pytest.approx(0.95)
    assert audited[0]["arguments"]["query"] == "집 주소"


def test_jev_reads_the_whole_of_a_long_query(
    monkeypatch: pytest.MonkeyPatch,
    audited: list[dict[str, Any]],
    no_upstream: list[tuple[str, str]],
) -> None:
    """A question past the audit log's cut must still reach the gate."""
    judged: list[str] = []

    def judge(**kwargs: Any) -> jev_gate.Verdict:
        judged.append(kwargs["query"])
        return jev_gate.Verdict(allowed=True, score=0.02, reason="in scope")

    monkeypatch.setattr(
        server, "jev_gate", SimpleNamespace(judge=judge, MESSAGE="refused")
    )
    monkeypatch.setattr(
        server, "get_http_request", lambda: SimpleNamespace(headers={}, client=None)
    )
    long = "배포 " * 3000 + "그리고 집 주소"

    server.chat(long)
    assert judged == [long]


def test_an_allowed_query_carries_its_score(
    monkeypatch: pytest.MonkeyPatch,
    audited: list[dict[str, Any]],
    no_upstream: list[tuple[str, str]],
) -> None:
    monkeypatch.setattr(
        server,
        "jev_gate",
        SimpleNamespace(
            judge=lambda **_: jev_gate.Verdict(allowed=True, score=0.02, reason="in scope"),
            MESSAGE="refused",
        ),
    )
    monkeypatch.setattr(
        server, "get_http_request", lambda: SimpleNamespace(headers={}, client=None)
    )

    server.chat("지난주 배포")
    assert audited[0]["status"] == "ok"
    assert audited[0]["jev_score"] == pytest.approx(0.02)
    assert audited[0]["error"] is None


def test_a_query_jev_failed_to_judge_says_so_on_its_row(
    monkeypatch: pytest.MonkeyPatch,
    audited: list[dict[str, Any]],
    no_upstream: list[tuple[str, str]],
) -> None:
    monkeypatch.setattr(
        server,
        "jev_gate",
        SimpleNamespace(
            judge=lambda **_: jev_gate.Verdict(
                allowed=True,
                score=None,
                reason="jev unavailable: TypeSafeAPIConnectionError: no route",
                failed=True,
            ),
            MESSAGE="refused",
        ),
    )
    monkeypatch.setattr(
        server, "get_http_request", lambda: SimpleNamespace(headers={}, client=None)
    )

    server.chat("지난주 배포")
    assert audited[0]["status"] == "ok"
    assert audited[0]["jev_score"] is None
    assert audited[0]["error"] == "jev unavailable: TypeSafeAPIConnectionError: no route"


def test_a_query_the_team_hub_let_through_without_a_key_says_so_on_its_row(
    monkeypatch: pytest.MonkeyPatch,
    audited: list[dict[str, Any]],
    no_upstream: list[tuple[str, str]],
) -> None:
    monkeypatch.setattr(
        server,
        "jev_gate",
        SimpleNamespace(
            judge=lambda **_: jev_gate.Verdict(
                allowed=True,
                score=None,
                reason="not judged: the team hub has no Jev key",
                no_key=True,
            ),
            MESSAGE="refused",
        ),
    )
    monkeypatch.setattr(
        server, "get_http_request", lambda: SimpleNamespace(headers={}, client=None)
    )

    server.chat("지난주 배포")
    assert audited[0]["status"] == "ok"
    assert audited[0]["jev_score"] is None
    assert audited[0]["error"] == "not judged: the team hub has no Jev key"


# ------------------------------------------------------- runtime reconfiguration


def test_a_tool_disabled_after_boot_starts_refusing(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    audited: list[dict[str, Any]],
) -> None:
    """In a container nothing restarts, so a dashboard edit has to bite at call time."""
    config = tmp_path / "tool-config.json"
    config.write_text(json.dumps({"version": 1, "disabled_tools": []}))
    monkeypatch.setattr(server, "TOOL_CONFIG_PATH", config)
    server._tool_config_cache["mtime"] = None
    monkeypatch.setattr(
        server, "get_http_request", lambda: SimpleNamespace(headers={}, client=None)
    )
    monkeypatch.setattr(server, "_request", lambda *a, **k: {"ok": True})

    assert server.search("q") == {"ok": True}

    config.write_text(json.dumps({"version": 1, "disabled_tools": ["search"]}))
    import os

    os.utime(config, (0, 0))

    with pytest.raises(RuntimeError, match="disabled on this MCP server"):
        server.search("q")
    assert audited[-1]["status"] == "denied"


def test_pinned_defaults_ignore_client_headers(monkeypatch: pytest.MonkeyPatch) -> None:
    """A shared bridge must not let its callers aim at another workspace."""
    headers = {"x-honcho-workspace-id": "someone-else", "x-honcho-user-name": "mate"}
    monkeypatch.setattr(
        server, "get_http_request", lambda: SimpleNamespace(headers=headers, client=None)
    )

    monkeypatch.setattr(server, "PIN_DEFAULTS", False)
    assert server._resolve_defaults()["workspace_id"] == "someone-else"

    monkeypatch.setattr(server, "PIN_DEFAULTS", True)
    resolved = server._resolve_defaults()
    assert resolved["workspace_id"] == server.DEFAULT_WORKSPACE_ID
    assert resolved["user_name"] == server.DEFAULT_USER_NAME


# ------------------------------------------------------------ pinned arguments

#: Every tool registered, so each one's arguments can be tried against the pin.
_ALL_TOOLS_ENV = {
    "HONCHO_MCP_TOOL_CONFIG": "/nonexistent/tool-config.json",
    "HONCHO_MCP_REQUIRE_TOOL_CONFIG": "0",
    "HONCHO_MCP_ENABLED_TOOLS": "",
    "HONCHO_MCP_HIDE_PEER_CARDS": "0",
}


@pytest.fixture
def pinned(monkeypatch: pytest.MonkeyPatch) -> Any:
    """A shared bridge with every tool registered.

    Patch `_request` in the test body, not with `no_upstream`: this reload would
    undo a patch applied before it.
    """
    fresh = _reload_server(
        monkeypatch, {**_ALL_TOOLS_ENV, "HONCHO_MCP_PIN_DEFAULTS": "1"}
    )
    monkeypatch.setattr(
        fresh, "get_http_request", lambda: SimpleNamespace(headers={}, client=None)
    )
    return fresh


@pytest.mark.parametrize(
    ("call", "argument"),
    [
        pytest.param(
            lambda m: m.chat("q", workspace_id="different-workspace"),
            "workspace_id",
            id="chat-workspace",
        ),
        pytest.param(
            lambda m: m.chat("q", peer_id="different-peer"),
            "peer_id",
            id="chat-observer",
        ),
        pytest.param(
            lambda m: m.chat("q", target_peer_id="different-peer"),
            "target_peer_id",
            id="chat-target",
        ),
        pytest.param(
            lambda m: m.get_representation("different-peer"),
            "peer_id",
            id="positional-peer",
        ),
        pytest.param(
            lambda m: m.query_conclusions(
                "q",
                observer_id=m.DEFAULT_USER_NAME,
                observed_id=m.DEFAULT_USER_NAME,
                filters={"observer": "different-peer"},
            ),
            "filters",
            id="filters-override-observer",
        ),
        pytest.param(
            lambda m: m.remove_peers_from_session("s", ["different-peer"]),
            "peer_ids",
            id="peer-id-list",
        ),
        pytest.param(
            lambda m: m.create_session("s", peers={"different-peer": {}}),
            "peers",
            id="peer-map",
        ),
        pytest.param(
            lambda m: m.add_messages_to_session(
                "s", [{"peer_id": "different-peer", "content": "x"}]
            ),
            "messages",
            id="message-peer",
        ),
    ],
)
def test_a_pinned_bridge_refuses_arguments_that_point_elsewhere(
    monkeypatch: pytest.MonkeyPatch,
    pinned: Any,
    audited: list[dict[str, Any]],
    call: Any,
    argument: str,
) -> None:
    """Ignoring the headers is not enough when the same values can be arguments."""

    def unexpected(*args: Any, **kwargs: Any) -> Any:
        pytest.fail("A call aimed past the pin was forwarded to Honcho")

    monkeypatch.setattr(pinned, "_request", unexpected)

    with pytest.raises(RuntimeError, match=f"^{argument} cannot"):
        call(pinned)

    assert audited[-1]["status"] == "denied"
    assert audited[-1]["error"].startswith(argument)


def test_a_pinned_bridge_accepts_its_own_values(
    monkeypatch: pytest.MonkeyPatch, pinned: Any, audited: list[dict[str, Any]]
) -> None:
    seen: list[str] = []
    monkeypatch.setattr(
        pinned, "_request", lambda method, path, **_: seen.append(path) or {"ok": True}
    )

    pinned.chat("q")
    pinned.chat(
        "q",
        workspace_id=pinned.DEFAULT_WORKSPACE_ID,
        peer_id=pinned.DEFAULT_ASSISTANT_NAME,
        target_peer_id=pinned.DEFAULT_USER_NAME,
    )

    own = (
        f"/v3/workspaces/{pinned.DEFAULT_WORKSPACE_ID}"
        f"/peers/{pinned.DEFAULT_ASSISTANT_NAME}/chat"
    )
    assert seen == [own, own]
    assert [row["status"] for row in audited] == ["ok", "ok"]


def test_an_unpinned_bridge_still_follows_arguments(
    monkeypatch: pytest.MonkeyPatch, audited: list[dict[str, Any]]
) -> None:
    """The owner's own bridge keeps reaching any workspace and peer."""
    seen: list[str] = []
    monkeypatch.setattr(server, "PIN_DEFAULTS", False)
    monkeypatch.setattr(
        server, "_request", lambda method, path, **_: seen.append(path) or {"ok": True}
    )
    monkeypatch.setattr(
        server, "get_http_request", lambda: SimpleNamespace(headers={}, client=None)
    )

    server.chat("q", workspace_id="other-workspace", peer_id="other-peer")

    assert seen == ["/v3/workspaces/other-workspace/peers/other-peer/chat"]


def test_every_peer_argument_is_pinned(monkeypatch: pytest.MonkeyPatch) -> None:
    """A tool argument that names a peer is only safe on a shared bridge if the pin
    checks it. A new one, from upstream or from us, has to be added deliberately."""
    fresh = _reload_server(monkeypatch, _ALL_TOOLS_ENV)
    named = {
        argument
        for tool in asyncio.run(fresh.mcp.list_tools())
        for argument in tool.parameters.get("properties", {})
        if any(word in argument for word in ("peer", "observe", "sender", "target"))
    }
    # peer_card is the card's text, not a peer id.
    unpinned = named - {"peer_card"} - fresh.PEER_ARGUMENTS
    assert not unpinned, sorted(unpinned)
