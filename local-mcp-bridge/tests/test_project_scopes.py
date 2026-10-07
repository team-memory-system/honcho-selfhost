"""A teammate's `chat` answers only from the projects the owner opened to them.

Behind a team server's gate (`HONCHO_MCP_SCOPE_FROM_GATE`), two headers say who is
calling: the owner (`all`) or a teammate (`projects`, with the projects opened to
them). These tests hold the rules: the owner is answered as before, every Honcho
call for a teammate carries one project's scope, and anything the bridge cannot
read is refused before it reaches Honcho.
"""

from __future__ import annotations

import base64
import importlib
import json
from types import SimpleNamespace
from typing import Any

import httpx
import pytest
from starlette.datastructures import Headers
from starlette.testclient import TestClient

import server

HONCHO = {"id": "p-0123456789ab", "name": "honcho"}
DESIGN = {"id": "p-abcdef012345", "name": "Design"}
INFRA = {"id": "p-00000000000f", "name": "infra"}

OWNER = {
    "cf-access-authenticated-user-email": "me@example.com",
    "x-honcho-scope-mode": "all",
}


def _base64url(data: bytes, *, padded: bool = False) -> str:
    text = base64.urlsafe_b64encode(data).decode()
    return text if padded else text.rstrip("=")


def _allowed(projects: Any, *, padded: bool = False) -> str:
    return _base64url(json.dumps(projects).encode(), padded=padded)


def _teammate(*projects: dict[str, str]) -> dict[str, str]:
    return {
        "cf-access-authenticated-user-email": "mate@example.com",
        "x-honcho-scope-mode": "projects",
        "x-honcho-allowed-scopes": _allowed(list(projects)),
    }


def _numbered(count: int) -> list[dict[str, str]]:
    return [{"id": f"p-{n:012x}", "name": f"project {n}"} for n in range(count)]


def _chat_path(peer: str) -> str:
    return f"/v3/workspaces/{server.DEFAULT_WORKSPACE_ID}/peers/{peer}/chat"


@pytest.fixture
def gate(monkeypatch: pytest.MonkeyPatch) -> Any:
    """Turn the setting on; the returned function sends one request's headers."""
    monkeypatch.setattr(server, "SCOPE_FROM_GATE", True)

    def send(headers: Any) -> None:
        monkeypatch.setattr(
            server,
            "get_http_request",
            lambda: SimpleNamespace(headers=headers, client=None),
        )

    return send


@pytest.fixture
def audited(monkeypatch: pytest.MonkeyPatch) -> list[dict[str, Any]]:
    """Capture audit rows without a database."""
    rows: list[dict[str, Any]] = []
    monkeypatch.setattr(server.audit, "record", lambda **kwargs: rows.append(kwargs))
    return rows


@pytest.fixture
def honcho(monkeypatch: pytest.MonkeyPatch) -> list[dict[str, Any]]:
    """Answer every Honcho call locally, naming the scope it was asked in."""
    calls: list[dict[str, Any]] = []

    def fake_request(method: str, path: str, **kwargs: Any) -> Any:
        calls.append({"method": method, "path": path, **kwargs})
        return {"content": f"from {(kwargs.get('body') or {}).get('scope')}"}

    monkeypatch.setattr(server, "_request", fake_request)
    return calls


# ------------------------------------------------------------------ the setting


def test_the_setting_is_read_at_startup(monkeypatch: pytest.MonkeyPatch) -> None:
    try:
        monkeypatch.setenv("HONCHO_MCP_SCOPE_FROM_GATE", "1")
        assert importlib.reload(server).SCOPE_FROM_GATE is True
        monkeypatch.delenv("HONCHO_MCP_SCOPE_FROM_GATE")
        assert importlib.reload(server).SCOPE_FROM_GATE is False
    finally:
        monkeypatch.undo()
        importlib.reload(server)


def test_without_the_setting_chat_is_unchanged(
    monkeypatch: pytest.MonkeyPatch,
    audited: list[dict[str, Any]],
    honcho: list[dict[str, Any]],
) -> None:
    """The gate's headers mean nothing to a bridge that does not take them."""
    monkeypatch.setattr(server, "SCOPE_FROM_GATE", False)
    monkeypatch.setattr(
        server,
        "get_http_request",
        lambda: SimpleNamespace(headers=_teammate(HONCHO), client=None),
    )

    server.chat("q", session_id="s1")

    assert honcho == [
        {
            "method": "POST",
            "path": _chat_path(server.DEFAULT_ASSISTANT_NAME),
            "body": {"query": "q", "session_id": "s1", "reasoning_level": "low"},
        }
    ]
    assert audited[0]["status"] == "ok"
    assert "projects" not in audited[0]["arguments"]


def test_without_the_setting_project_is_refused(
    monkeypatch: pytest.MonkeyPatch,
    audited: list[dict[str, Any]],
    honcho: list[dict[str, Any]],
) -> None:
    """Silently answering from the whole memory would pass for a project's answer."""
    monkeypatch.setattr(server, "SCOPE_FROM_GATE", False)
    monkeypatch.setattr(
        server, "get_http_request", lambda: SimpleNamespace(headers={}, client=None)
    )

    with pytest.raises(RuntimeError, match="^project cannot be used"):
        server.chat("q", project="honcho")

    assert honcho == []
    assert audited[0]["status"] == "denied"


# -------------------------------------------------------------------- the owner


def test_the_owner_is_answered_as_before(
    gate: Any, audited: list[dict[str, Any]], honcho: list[dict[str, Any]]
) -> None:
    gate(OWNER)

    server.chat("q", target_peer_id="someone", session_id="s1")

    assert honcho == [
        {
            "method": "POST",
            "path": _chat_path(server.DEFAULT_ASSISTANT_NAME),
            "body": {
                "query": "q",
                "target": "someone",
                "session_id": "s1",
                "reasoning_level": "low",
            },
        }
    ]
    assert audited[0]["status"] == "ok"
    assert "projects" not in audited[0]["arguments"]


def test_the_owner_keeps_every_tool(gate: Any, honcho: list[dict[str, Any]]) -> None:
    gate(OWNER)
    server.search("q")
    assert honcho[0]["path"] == f"/v3/workspaces/{server.DEFAULT_WORKSPACE_ID}/search"


def test_the_owner_needs_no_project(
    gate: Any, audited: list[dict[str, Any]], honcho: list[dict[str, Any]]
) -> None:
    gate(OWNER)

    with pytest.raises(RuntimeError, match="^project is not needed"):
        server.chat("q", project="honcho")

    assert honcho == []
    assert audited[0]["status"] == "denied"


# --------------------------------------------------------------- a teammate's chat


def test_a_single_open_project_is_used(
    gate: Any, audited: list[dict[str, Any]], honcho: list[dict[str, Any]]
) -> None:
    gate(_teammate(HONCHO))

    assert server.chat("q") == {"content": f"from {HONCHO['id']}"}

    assert honcho == [
        {
            "method": "POST",
            "path": _chat_path(server.DEFAULT_USER_NAME),
            "body": {"query": "q", "scope": HONCHO["id"], "reasoning_level": "low"},
        }
    ]
    row = audited[0]
    assert (row["status"], row["caller"]) == ("ok", "mate@example.com")
    assert row["arguments"]["projects"] == [HONCHO]


def test_several_projects_are_asked_one_by_one(
    gate: Any, audited: list[dict[str, Any]], honcho: list[dict[str, Any]]
) -> None:
    gate(_teammate(HONCHO, DESIGN, INFRA))

    assert server.chat("q") == {
        "answers": [
            {"project": "honcho", "answer": {"content": f"from {HONCHO['id']}"}},
            {"project": "Design", "answer": {"content": f"from {DESIGN['id']}"}},
            {"project": "infra", "answer": {"content": f"from {INFRA['id']}"}},
        ]
    }
    assert [call["body"]["scope"] for call in honcho] == [
        HONCHO["id"],
        DESIGN["id"],
        INFRA["id"],
    ]
    assert len(audited) == 1, "one call, one row"
    assert audited[0]["arguments"]["projects"] == [HONCHO, DESIGN, INFRA]


def test_every_honcho_call_carries_exactly_one_scope(
    gate: Any, honcho: list[dict[str, Any]]
) -> None:
    """A list of scopes keeps the owner as the observer and mixes in their card."""
    gate(_teammate(HONCHO, DESIGN))

    server.chat("q")
    server.chat("q", project="design")

    assert len(honcho) == 3
    for call in honcho:
        assert isinstance(call["body"]["scope"], str)
        assert server.PROJECT_ID.fullmatch(call["body"]["scope"])
        assert not {"filters", "session_id"} & set(call["body"])


def test_five_projects_are_asked(gate: Any, honcho: list[dict[str, Any]]) -> None:
    five = _numbered(5)
    gate(_teammate(*five))

    assert [answer["project"] for answer in server.chat("q")["answers"]] == [
        project["name"] for project in five
    ]
    assert len(honcho) == 5


def test_more_than_five_projects_are_refused_with_their_names(
    gate: Any, audited: list[dict[str, Any]], honcho: list[dict[str, Any]]
) -> None:
    six = _numbered(6)
    gate(_teammate(*six))

    with pytest.raises(RuntimeError, match="^6 projects are open to you") as refused:
        server.chat("q")

    assert str(refused.value).endswith(", ".join(p["name"] for p in six))
    assert honcho == []
    assert (audited[0]["status"], audited[0]["error"]) == ("denied", str(refused.value))


def test_more_than_five_projects_still_answer_one_by_name(
    gate: Any, honcho: list[dict[str, Any]]
) -> None:
    six = _numbered(6)
    gate(_teammate(*six))

    server.chat("q", project="project 3")

    assert [call["body"]["scope"] for call in honcho] == [six[3]["id"]]


@pytest.mark.parametrize(
    "wanted", ["p-abcdef012345", "P-ABCDEF012345", "Design", "design", "  DESIGN "]
)
def test_a_project_is_named_by_id_or_name_in_any_case(
    gate: Any,
    audited: list[dict[str, Any]],
    honcho: list[dict[str, Any]],
    wanted: str,
) -> None:
    gate(_teammate(HONCHO, DESIGN))

    assert server.chat("q", project=wanted) == {"content": f"from {DESIGN['id']}"}

    assert [call["body"]["scope"] for call in honcho] == [DESIGN["id"]]
    assert audited[0]["arguments"]["project"] == wanted
    assert audited[0]["arguments"]["projects"] == [DESIGN]


@pytest.mark.parametrize("wanted", ["secret", "honch", "p-999999999999"])
def test_a_project_not_open_is_refused_naming_no_project(
    gate: Any,
    audited: list[dict[str, Any]],
    honcho: list[dict[str, Any]],
    wanted: str,
) -> None:
    gate(_teammate(HONCHO, DESIGN))

    with pytest.raises(RuntimeError, match="not open to you") as refused:
        server.chat("q", project=wanted)

    assert str(refused.value) == "That project is not open to you on this MCP server"
    assert honcho == []
    assert audited[0]["status"] == "denied"
    assert audited[0]["arguments"]["project"] == wanted


def test_a_name_two_projects_share_is_refused_with_their_ids(
    gate: Any, honcho: list[dict[str, Any]]
) -> None:
    twin = {"id": "p-111111111111", "name": "HONCHO"}
    gate(_teammate(HONCHO, twin))

    with pytest.raises(RuntimeError, match=f"by id: {HONCHO['id']}, {twin['id']}$"):
        server.chat("q", project="honcho")
    server.chat("q", project=twin["id"])

    assert [call["body"]["scope"] for call in honcho] == [twin["id"]]


def test_a_project_is_asked_about_the_owner_unless_a_peer_is_named(
    gate: Any, honcho: list[dict[str, Any]]
) -> None:
    """With a scope, Honcho answers as the scope; the path names only the subject."""
    gate(_teammate(HONCHO))

    server.chat("q")
    server.chat("q", peer_id="assistant_codex")
    server.chat("q", target_peer_id="assistant_codex")

    assert [(call["path"], call["body"].get("target")) for call in honcho] == [
        (_chat_path(server.DEFAULT_USER_NAME), None),
        (_chat_path("assistant_codex"), None),
        (_chat_path(server.DEFAULT_USER_NAME), "assistant_codex"),
    ]


def test_one_project_honcho_refuses_does_not_cost_the_others(
    gate: Any, monkeypatch: pytest.MonkeyPatch, audited: list[dict[str, Any]]
) -> None:
    """A project opened before any conversation was collected has no scope yet."""
    missing = f"Honcho API 404: Scope {DESIGN['id']} not found in workspace memory"

    def fake_request(method: str, path: str, *, body: dict[str, Any]) -> Any:
        if body["scope"] == DESIGN["id"]:
            raise RuntimeError(missing)
        return {"content": "ok"}

    monkeypatch.setattr(server, "_request", fake_request)
    gate(_teammate(HONCHO, DESIGN))

    assert server.chat("q") == {
        "answers": [
            {"project": "honcho", "answer": {"content": "ok"}},
            {"project": "Design", "error": missing},
        ]
    }
    assert audited[0]["status"] == "ok"


def test_when_no_project_answers_the_call_fails(
    gate: Any, monkeypatch: pytest.MonkeyPatch, audited: list[dict[str, Any]]
) -> None:
    def fake_request(method: str, path: str, **_: Any) -> Any:
        raise RuntimeError("Honcho API 503")

    monkeypatch.setattr(server, "_request", fake_request)
    gate(_teammate(HONCHO, DESIGN))

    with pytest.raises(RuntimeError, match="Honcho API 503"):
        server.chat("q")
    assert audited[0]["status"] == "error"


def test_an_unreachable_honcho_stops_at_the_first_project(
    gate: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Asking the rest would only wait out the same timeout once per project."""
    asked: list[str] = []

    def fake_request(method: str, path: str, *, body: dict[str, Any]) -> Any:
        asked.append(body["scope"])
        raise httpx.ConnectError("connection refused")

    monkeypatch.setattr(server, "_request", fake_request)
    gate(_teammate(HONCHO, DESIGN))

    with pytest.raises(httpx.ConnectError):
        server.chat("q")
    assert asked == [HONCHO["id"]]


# --------------------------------------------------------------------- refusals


def test_no_open_project_is_refused(
    gate: Any, audited: list[dict[str, Any]], honcho: list[dict[str, Any]]
) -> None:
    gate(_teammate())

    with pytest.raises(RuntimeError, match="^No project on this MCP server is open"):
        server.chat("q")

    assert honcho == []
    assert audited[0]["status"] == "denied"


def test_session_id_is_refused_with_projects(
    gate: Any, audited: list[dict[str, Any]], honcho: list[dict[str, Any]]
) -> None:
    """Honcho takes a scope instead of a session, never with one."""
    gate(_teammate(HONCHO))

    with pytest.raises(RuntimeError, match="^session_id cannot be used with projects"):
        server.chat("q", session_id="s1")

    assert honcho == []
    assert audited[0]["status"] == "denied"


def test_filters_are_refused_with_projects(gate: Any) -> None:
    """chat takes no filters at all (see the HTTP test); the rule holds if it ever
    does."""
    gate(_teammate(HONCHO))

    with pytest.raises(RuntimeError, match="^filters cannot be used with projects"):
        server._projects_asked("chat", {"filters": {"session_id": "s1"}})


def test_a_teammate_may_only_chat(
    gate: Any, audited: list[dict[str, Any]], honcho: list[dict[str, Any]]
) -> None:
    """Any other tool would read past the projects, whatever the tool list says."""
    gate(_teammate(HONCHO))

    with pytest.raises(RuntimeError, match="^search cannot be used with projects"):
        server.search("q")

    assert honcho == []
    assert audited[0]["status"] == "denied"


@pytest.mark.parametrize(
    "allowed",
    [
        pytest.param(None, id="missing"),
        pytest.param("", id="empty"),
        pytest.param("not base64!", id="not-base64"),
        pytest.param(_base64url(b"not json"), id="not-json"),
        pytest.param(_base64url(b"\xff\xfe"), id="not-utf8"),
        pytest.param(_allowed(HONCHO), id="an-object"),
        pytest.param(_allowed(["honcho"]), id="a-bare-name"),
        pytest.param(_allowed([{"name": "honcho"}]), id="no-id"),
        pytest.param(_allowed([{"id": "p-0123", "name": "x"}]), id="short-id"),
        pytest.param(_allowed([{"id": "p-0123456789AB", "name": "x"}]), id="upper-id"),
        pytest.param(_allowed([{"id": "p-0123456789ab\n", "name": "x"}]), id="newline"),
        pytest.param(_allowed([{"id": "honcho", "name": "honcho"}]), id="name-as-id"),
        pytest.param(_allowed([{"id": 12, "name": "x"}]), id="number-id"),
        pytest.param(_allowed([{"id": HONCHO["id"]}]), id="no-name"),
        pytest.param(_allowed([{"id": HONCHO["id"], "name": " "}]), id="blank-name"),
        pytest.param(_allowed([HONCHO, {"id": "p-x", "name": "x"}]), id="one-bad"),
    ],
)
def test_an_unreadable_allowed_list_is_refused(
    gate: Any,
    audited: list[dict[str, Any]],
    honcho: list[dict[str, Any]],
    allowed: str | None,
) -> None:
    headers = {"x-honcho-scope-mode": "projects"}
    if allowed is not None:
        headers["x-honcho-allowed-scopes"] = allowed
    gate(headers)

    with pytest.raises(RuntimeError, match="x-honcho-allowed-scopes is missing"):
        server.chat("q")

    assert honcho == []
    assert audited[0]["status"] == "denied"


@pytest.mark.parametrize("padded", [False, True])
def test_the_allowed_list_reads_with_or_without_padding(
    gate: Any, honcho: list[dict[str, Any]], padded: bool
) -> None:
    allowed = _allowed([HONCHO], padded=padded)
    assert allowed.endswith("=") is padded, "this list's encoding needs padding"
    gate({"x-honcho-scope-mode": "projects", "x-honcho-allowed-scopes": allowed})

    server.chat("q")

    assert honcho[0]["body"]["scope"] == HONCHO["id"]


@pytest.mark.parametrize(
    "headers",
    [
        pytest.param({}, id="none"),
        pytest.param({"x-honcho-allowed-scopes": _allowed([HONCHO])}, id="list-only"),
        pytest.param({"x-honcho-scope-mode": ""}, id="empty"),
        pytest.param({"x-honcho-scope-mode": "ALL"}, id="upper-case"),
        pytest.param({"x-honcho-scope-mode": "owner"}, id="unknown"),
        pytest.param({"x-honcho-scope-mode": "all, projects"}, id="joined"),
    ],
)
def test_a_missing_or_unknown_mode_is_refused(
    gate: Any,
    audited: list[dict[str, Any]],
    honcho: list[dict[str, Any]],
    headers: dict[str, str],
) -> None:
    gate(headers)

    with pytest.raises(RuntimeError, match="x-honcho-scope-mode is missing"):
        server.chat("q")

    assert honcho == []
    assert audited[0]["status"] == "denied"


def test_a_repeated_gate_header_is_refused(
    gate: Any, honcho: list[dict[str, Any]]
) -> None:
    """The gate sends each header once; a second copy came from somewhere else."""
    allowed = _allowed([HONCHO]).encode()
    gate(
        Headers(
            raw=[
                (b"x-honcho-scope-mode", b"all"),
                (b"x-honcho-scope-mode", b"projects"),
                (b"x-honcho-allowed-scopes", allowed),
            ]
        )
    )
    with pytest.raises(RuntimeError, match="x-honcho-scope-mode is missing, repeated"):
        server.chat("q")

    gate(
        Headers(
            raw=[
                (b"x-honcho-scope-mode", b"projects"),
                (b"x-honcho-allowed-scopes", allowed),
                (b"x-honcho-allowed-scopes", _allowed(_numbered(3)).encode()),
            ]
        )
    )
    with pytest.raises(RuntimeError, match="x-honcho-allowed-scopes is missing"):
        server.chat("q")

    assert honcho == []


def test_a_call_without_an_http_request_is_refused(
    monkeypatch: pytest.MonkeyPatch, honcho: list[dict[str, Any]]
) -> None:
    """A gated bridge has nothing to answer a local stdio client from."""
    monkeypatch.setattr(server, "SCOPE_FROM_GATE", True)

    def no_request() -> Any:
        raise RuntimeError("No active HTTP request found.")

    monkeypatch.setattr(server, "get_http_request", no_request)

    with pytest.raises(RuntimeError, match="x-honcho-scope-mode is missing"):
        server.chat("q")
    assert honcho == []


def test_a_refused_call_never_reaches_the_jev_gate(
    gate: Any, monkeypatch: pytest.MonkeyPatch, honcho: list[dict[str, Any]]
) -> None:
    def unexpected(**_: Any) -> Any:
        pytest.fail("The Jev gate judged a call its projects had already refused")

    monkeypatch.setattr(
        server, "jev_gate", SimpleNamespace(judge=unexpected, MESSAGE="refused")
    )
    gate(_teammate(HONCHO))

    with pytest.raises(RuntimeError, match="not open to you"):
        server.chat("q", project="secret")
    assert honcho == []


# ------------------------------------------------------------- over real HTTP


def _call_chat(
    client: TestClient, arguments: dict[str, Any], headers: dict[str, str]
) -> dict[str, Any]:
    response = client.post(
        server.MCP_PATH,
        json={
            "jsonrpc": "2.0",
            "id": 1,
            "method": "tools/call",
            "params": {"name": "chat", "arguments": arguments},
        },
        headers={
            "accept": "application/json, text/event-stream",
            "content-type": "application/json",
            **headers,
        },
    )
    assert response.status_code == 200
    return response.json()["result"]


def test_a_real_tool_call_reads_the_gate_headers(
    monkeypatch: pytest.MonkeyPatch,
    audited: list[dict[str, Any]],
    honcho: list[dict[str, Any]],
) -> None:
    """Every other test hands chat its headers; here they come over HTTP, through
    FastMCP, into the thread the tool runs on."""
    monkeypatch.setattr(server, "SCOPE_FROM_GATE", True)
    app = server.mcp.http_app(
        path=server.MCP_PATH, json_response=True, stateless_http=True
    )
    teammate = _teammate(HONCHO, DESIGN)

    with TestClient(app, base_url="http://127.0.0.1:8765") as client:
        answered = _call_chat(client, {"query": "q", "project": "design"}, teammate)
        refused = _call_chat(client, {"query": "q", "project": "secret"}, teammate)
        ungated = _call_chat(client, {"query": "q"}, {})
        filtered = _call_chat(
            client, {"query": "q", "filters": {"session_id": "s1"}}, teammate
        )

    assert answered["structuredContent"] == {"content": f"from {DESIGN['id']}"}
    assert refused["isError"]
    assert "not open to you" in refused["content"][0]["text"]
    assert "design" not in refused["content"][0]["text"].casefold()
    assert ungated["isError"]
    assert "x-honcho-scope-mode is missing" in ungated["content"][0]["text"]
    assert filtered["isError"], "chat has no filters argument to send"
    assert [call["body"]["scope"] for call in honcho] == [DESIGN["id"]]
    # The filters call never reached the tool, so it left no row.
    assert [row["status"] for row in audited] == ["ok", "denied", "denied"]
    assert audited[0]["caller"] == "mate@example.com"
