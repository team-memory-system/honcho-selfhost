"""A teammate's `chat` answers only from the conversations in their open folders.

Behind a team server's gate (`HONCHO_MCP_SCOPE_FROM_GATE`), two headers say who is
calling: the owner (`all`) or a teammate (`folders`, with the folders the owner
opened to them). These tests hold the rules: the owner is answered as before, a
teammate's question is one Honcho chat confined to the sessions whose folder is
open, and anything the bridge cannot read is refused before it reaches Honcho.
"""

from __future__ import annotations

import base64
import importlib
import inspect
import json
import threading
import time
from collections.abc import Callable
from types import SimpleNamespace
from typing import Any

import httpx
import pytest
from starlette.datastructures import Headers
from starlette.testclient import TestClient

import jev_gate
import server

OWNER = {
    "cf-access-authenticated-user-email": "me@example.com",
    "x-honcho-scope-mode": "all",
}

NO_CONVERSATION = "No conversation in the folders open to you on this MCP server"
NOT_LISTED = (
    "The conversations in the folders open to you could not be listed on this MCP "
    "server; try again later"
)


def _base64url(data: bytes, *, padded: bool = False) -> str:
    text = base64.urlsafe_b64encode(data).decode()
    return text if padded else text.rstrip("=")


def _encoded(folders: Any, *, padded: bool = False) -> str:
    return _base64url(json.dumps(folders).encode(), padded=padded)


def _folder(path: str, subfolders: bool = False) -> dict[str, Any]:
    return {"path": path, "subfolders": subfolders}


def _teammate(*folders: dict[str, Any]) -> dict[str, str]:
    return {
        "cf-access-authenticated-user-email": "mate@example.com",
        "x-honcho-scope-mode": "folders",
        "x-honcho-allowed-folders": _encoded(list(folders)),
    }


def _chat_path(peer: str) -> str:
    return f"/v3/workspaces/{server.DEFAULT_WORKSPACE_ID}/peers/{peer}/chat"


def _allowlist(call: dict[str, Any]) -> list[str]:
    return call["body"]["filters"]["session_id"]["in"]


class FakeHoncho:
    """Honcho's session list and chat answered locally, with every call kept."""

    def __init__(self) -> None:
        self.workspaces: dict[str, list[Any]] = {}
        self.calls: list[dict[str, Any]] = []
        self.answer: Any = {"content": "배포는 금요일에 했습니다.", "evidence": None}
        #: An exception the session list raises, or a page it returns, instead.
        self.listing: Any = None
        #: Run as each page of sessions is asked for.
        self.before_listing: Callable[[], Any] | None = None

    def add(
        self, session_id: str, cwd: str | None, workspace: str | None = None
    ) -> None:
        """A session that started in `cwd`, as the collector records it."""
        metadata = {} if cwd is None else {"cwd": cwd}
        self.add_item({"id": session_id, "metadata": metadata}, workspace)

    def add_item(self, item: Any, workspace: str | None = None) -> None:
        workspace = workspace or server.DEFAULT_WORKSPACE_ID
        self.workspaces.setdefault(workspace, []).append(item)

    def __call__(
        self, method: str, path: str, *, body: Any = None, params: Any = None
    ) -> Any:
        self.calls.append(
            {"method": method, "path": path, "body": body, "params": params}
        )
        if path.endswith("/sessions/list"):
            if self.before_listing is not None:
                self.before_listing()
            if isinstance(self.listing, Exception):
                raise self.listing
            if self.listing is not None:
                return self.listing
            items = self.workspaces.get(path.split("/")[3], [])
            page, size = params["page"], params["size"]
            return {
                "items": items[(page - 1) * size : page * size],
                "total": len(items),
                "page": page,
                "size": size,
                "pages": -(-len(items) // size),
            }
        if path.endswith("/chat"):
            return self.answer
        return {"ok": True}

    @property
    def listings(self) -> list[dict[str, Any]]:
        return [call for call in self.calls if call["path"].endswith("/sessions/list")]

    @property
    def chats(self) -> list[dict[str, Any]]:
        return [call for call in self.calls if call["path"].endswith("/chat")]


@pytest.fixture(autouse=True)
def honcho(monkeypatch: pytest.MonkeyPatch) -> FakeHoncho:
    """Every Honcho call answered locally, with no session list kept from before.

    The fake sits under `_request`, so `_require_auth` runs; the bearer token is
    tested apart (test_bearer_gate.py), and the shell running these tests may
    carry one of its own.
    """
    fake = FakeHoncho()
    monkeypatch.setattr(server, "_honcho", fake)
    monkeypatch.setattr(server, "_session_lists", {})
    monkeypatch.setattr(server, "OPTIONAL_BEARER_TOKEN", "")
    return fake


@pytest.fixture
def clock(monkeypatch: pytest.MonkeyPatch) -> list[float]:
    """The session lists' clock, standing still until a test moves it."""
    now = [1000.0]
    monkeypatch.setattr(server, "_clock", lambda: now[0])
    return now


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
    monkeypatch: pytest.MonkeyPatch, audited: list[dict[str, Any]], honcho: FakeHoncho
) -> None:
    """The gate's headers mean nothing to a bridge that does not take them."""
    monkeypatch.setattr(server, "SCOPE_FROM_GATE", False)
    monkeypatch.setattr(
        server,
        "get_http_request",
        lambda: SimpleNamespace(headers=_teammate(_folder("/work", True)), client=None),
    )
    honcho.add("s1", "/work")

    server.chat("q", session_id="s1")

    assert honcho.calls == [
        {
            "method": "POST",
            "path": _chat_path(server.DEFAULT_ASSISTANT_NAME),
            "body": {"query": "q", "session_id": "s1", "reasoning_level": "low"},
            "params": None,
        }
    ]
    assert audited[0]["status"] == "ok"
    assert not {"folders", "sessions"} & set(audited[0]["arguments"])


def test_project_is_gone(monkeypatch: pytest.MonkeyPatch, honcho: FakeHoncho) -> None:
    """One call answers from every open folder, so there is nothing to pick."""
    monkeypatch.setattr(
        server, "get_http_request", lambda: SimpleNamespace(headers={}, client=None)
    )
    assert "project" not in inspect.signature(server.chat).parameters
    with pytest.raises(TypeError):
        server.chat("q", project="honcho")
    assert honcho.calls == []


# -------------------------------------------------------------------- the owner


def test_the_owner_is_answered_as_before(
    gate: Any, audited: list[dict[str, Any]], honcho: FakeHoncho
) -> None:
    gate(OWNER)

    server.chat("q", target_peer_id="someone", session_id="s1")

    assert honcho.calls == [
        {
            "method": "POST",
            "path": _chat_path(server.DEFAULT_ASSISTANT_NAME),
            "body": {
                "query": "q",
                "target": "someone",
                "session_id": "s1",
                "reasoning_level": "low",
            },
            "params": None,
        }
    ]
    assert audited[0]["status"] == "ok"
    assert not {"folders", "sessions"} & set(audited[0]["arguments"])


def test_the_owner_keeps_every_tool(gate: Any, honcho: FakeHoncho) -> None:
    gate(OWNER)
    server.search("q")
    assert [call["path"] for call in honcho.calls] == [
        f"/v3/workspaces/{server.DEFAULT_WORKSPACE_ID}/search"
    ]


def test_a_teammates_reach_does_not_outlive_their_call(
    gate: Any, honcho: FakeHoncho
) -> None:
    """The next call on the same thread, the owner's here, is not confined."""
    honcho.add("s1", "/work")
    gate(_teammate(_folder("/work")))
    server.chat("q")
    assert server._call_reach.get() is server._UNRESOLVED

    gate(OWNER)
    server.chat("q")

    assert [call["path"] for call in honcho.chats] == [
        _chat_path(server.DEFAULT_USER_NAME),
        _chat_path(server.DEFAULT_ASSISTANT_NAME),
    ]
    assert "filters" not in honcho.chats[1]["body"]


# --------------------------------------------------------------- a teammate's chat


def test_a_teammates_question_is_one_chat_confined_to_the_open_folders(
    gate: Any, audited: list[dict[str, Any]], honcho: FakeHoncho
) -> None:
    honcho.add("s-team", "/work/team")
    honcho.add("s-api", "/work/team/api")
    honcho.add("s-private", "/work/private")
    honcho.add("s-nowhere", None)
    opened = [_folder("/work/team", True)]
    gate(_teammate(*opened))

    assert server.chat("지난주 배포 어땠어?") is honcho.answer

    assert honcho.listings == [
        {
            "method": "POST",
            "path": f"/v3/workspaces/{server.DEFAULT_WORKSPACE_ID}/sessions/list",
            "body": {},
            "params": {"page": 1, "size": 100},
        }
    ]
    assert honcho.chats == [
        {
            "method": "POST",
            "path": _chat_path(server.DEFAULT_USER_NAME),
            "body": {
                "query": "지난주 배포 어땠어?",
                "reasoning_level": "low",
                "filters": {"session_id": {"in": ["s-team", "s-api"]}},
            },
            "params": None,
        }
    ]
    assert len(audited) == 1, "one call, one row"
    row = audited[0]
    assert (row["status"], row["caller"]) == ("ok", "mate@example.com")
    assert row["arguments"]["folders"] == opened
    assert row["arguments"]["sessions"] == 2


def test_the_owner_is_asked_about_unless_a_peer_is_named(
    gate: Any, honcho: FakeHoncho
) -> None:
    """The allowlist confines recall; the path names only whom the question is about."""
    honcho.add("s1", "/work")
    gate(_teammate(_folder("/work")))

    server.chat("q")
    server.chat("q", peer_id="assistant_codex")
    server.chat("q", target_peer_id="assistant_codex", reasoning_level="high")

    assert [
        (call["path"], call["body"].get("target"), call["body"]["reasoning_level"])
        for call in honcho.chats
    ] == [
        (_chat_path(server.DEFAULT_USER_NAME), None, "low"),
        (_chat_path("assistant_codex"), None, "low"),
        (_chat_path(server.DEFAULT_USER_NAME), "assistant_codex", "high"),
    ]
    for call in honcho.chats:
        assert _allowlist(call) == ["s1"]
        assert not {"scope", "session_id"} & set(call["body"])


def test_every_page_of_sessions_is_read(gate: Any, honcho: FakeHoncho) -> None:
    for n in range(250):
        honcho.add(f"s{n:03}", "/work" if n % 2 == 0 else "/elsewhere")
    gate(_teammate(_folder("/work")))

    server.chat("q")

    assert [call["params"] for call in honcho.listings] == [
        {"page": page, "size": 100} for page in (1, 2, 3)
    ]
    assert _allowlist(honcho.chats[0]) == [f"s{n:03}" for n in range(0, 250, 2)]


def test_a_session_listed_twice_is_asked_once(gate: Any, honcho: FakeHoncho) -> None:
    """A session that moves across a page boundary while the pages are read."""
    honcho.add("s1", "/work")
    honcho.add("s1", "/work")
    gate(_teammate(_folder("/work")))

    server.chat("q")

    assert _allowlist(honcho.chats[0]) == ["s1"]


@pytest.mark.parametrize(
    "opened",
    [
        pytest.param([], id="nothing-open"),
        pytest.param([_folder("/work/empty", True)], id="an-empty-folder"),
    ],
)
def test_no_conversation_in_the_open_folders_is_refused(
    gate: Any,
    audited: list[dict[str, Any]],
    honcho: FakeHoncho,
    opened: list[dict[str, Any]],
) -> None:
    """The same words either way: the refusal says nothing about the memory."""
    honcho.add("s1", "/work/full")
    gate(_teammate(*opened))

    with pytest.raises(RuntimeError) as refused:
        server.chat("q")

    assert str(refused.value) == NO_CONVERSATION
    assert honcho.chats == []
    # With nothing open there is nothing to list.
    assert len(honcho.listings) == (1 if opened else 0)
    row = audited[0]
    assert (row["status"], row["error"]) == ("denied", NO_CONVERSATION)
    assert row["arguments"]["folders"] == opened
    assert row["arguments"]["sessions"] == 0


def test_more_sessions_than_one_chat_takes_are_refused(
    gate: Any, audited: list[dict[str, Any]], honcho: FakeHoncho
) -> None:
    """Honcho takes at most 1000 sessions in one allowlist."""
    for n in range(1001):
        honcho.add(f"s{n}", "/work")
    gate(_teammate(_folder("/work")))

    with pytest.raises(RuntimeError) as refused:
        server.chat("q")

    assert str(refused.value) == (
        "The folders open to you on this MCP server hold more than 1000 "
        "conversations, more than one answer can draw on; the owner has to open "
        "fewer folders"
    )
    assert honcho.chats == []
    row = audited[0]
    assert (row["status"], row["error"]) == ("denied", str(refused.value))
    assert row["arguments"]["sessions"] == 1001


def test_as_many_sessions_as_one_chat_takes_are_asked(
    gate: Any, honcho: FakeHoncho
) -> None:
    for n in range(1000):
        honcho.add(f"s{n}", "/work")
    gate(_teammate(_folder("/work")))

    server.chat("q")

    assert len(_allowlist(honcho.chats[0])) == 1000


@pytest.mark.parametrize(
    "failure",
    [
        pytest.param(RuntimeError("Honcho API 503 for /v3/..."), id="honcho-error"),
        pytest.param(httpx.ConnectError("connection refused"), id="unreachable"),
        pytest.param({"items": "nothing", "pages": 1}, id="not-a-page"),
        pytest.param({"items": [], "pages": None}, id="no-page-count"),
        pytest.param({"items": [{"metadata": {}}], "pages": 1}, id="no-id"),
        pytest.param({"items": [], "pages": 1001}, id="too-many-pages"),
    ],
)
def test_a_session_list_that_does_not_read_is_refused_and_recorded(
    gate: Any, audited: list[dict[str, Any]], honcho: FakeHoncho, failure: Any
) -> None:
    """The teammate is told it could not be read; the owner's log keeps why."""
    honcho.listing = failure
    gate(_teammate(_folder("/work")))

    with pytest.raises(RuntimeError) as refused:
        server.chat("q")

    assert str(refused.value) == NOT_LISTED
    assert honcho.chats == []
    row = audited[0]
    assert row["status"] == "error"
    assert row["error"].startswith("listing the sessions failed: ")
    assert row["arguments"]["folders"] == [_folder("/work")]
    assert "sessions" not in row["arguments"]


def test_a_failed_list_is_not_kept(
    gate: Any, honcho: FakeHoncho, clock: list[float]
) -> None:
    honcho.add("s1", "/work")
    honcho.listing = RuntimeError("Honcho API 503")
    gate(_teammate(_folder("/work")))
    with pytest.raises(RuntimeError, match="could not be listed"):
        server.chat("q")

    honcho.listing = None
    server.chat("q")

    assert len(honcho.listings) == 2
    assert _allowlist(honcho.chats[0]) == ["s1"]


def test_chat_outside_the_wrapper_still_confines_a_teammate(
    gate: Any, honcho: FakeHoncho
) -> None:
    """The wrapper works the reach out once per call; without it, chat does."""
    honcho.add("s1", "/work")
    honcho.add("s2", "/elsewhere")
    gate(_teammate(_folder("/work")))

    server.chat.__wrapped__("q")
    assert _allowlist(honcho.chats[0]) == ["s1"]

    gate(_teammate(_folder("/nothing-here")))
    with pytest.raises(RuntimeError, match=f"^{NO_CONVERSATION}$"):
        server.chat.__wrapped__("q")
    assert len(honcho.chats) == 1


# ------------------------------------------------------------ matching folders


@pytest.mark.parametrize(
    ("cwd", "folder", "opened"),
    [
        pytest.param("/work/team", _folder("/work/team"), True, id="exact"),
        pytest.param("/work/team/api", _folder("/work/team"), False, id="sub-closed"),
        pytest.param("/work/team/api", _folder("/work/team", True), True, id="sub"),
        pytest.param("/work/team/a/b", _folder("/work/team", True), True, id="deep"),
        pytest.param("/work/teammate", _folder("/work/team", True), False, id="twin"),
        pytest.param("/work", _folder("/work/team", True), False, id="parent"),
        pytest.param("/work/x", _folder("/", True), True, id="root-and-below"),
        pytest.param("/", _folder("/"), True, id="root-itself"),
        pytest.param("/work", _folder("/"), False, id="root-without-subfolders"),
        pytest.param("/work/team/", _folder("/work/team"), True, id="cwd-trailing"),
        pytest.param("/work/team", _folder("/work/team//"), True, id="folder-trailing"),
        pytest.param("/work//team//x", _folder("/work/team", True), True, id="repeats"),
        pytest.param("/Work/Team", _folder("/work/team"), False, id="posix-keeps-case"),
        pytest.param(
            "C:\\Users\\me\\proj", _folder("C:/Users/me/proj"), True, id="backslashes"
        ),
        pytest.param(
            "C:\\Users\\me\\proj\\api",
            _folder("C:\\Users\\me\\proj", True),
            True,
            id="windows-subfolder",
        ),
        pytest.param(
            "c:\\users\\ME\\Proj", _folder("C:\\Users\\me\\proj"), True, id="drive-case"
        ),
        pytest.param(
            "C:\\Users\\me\\proj\\",
            _folder("c:/users/me/proj/"),
            True,
            id="drive-trailing-slash",
        ),
        pytest.param("D:\\x\\y", _folder("D:\\", True), True, id="drive-root"),
        pytest.param("D:\\x", _folder("d:/", True), True, id="drive-root-case"),
        pytest.param("C:\\x", _folder("D:\\", True), False, id="other-drive"),
        pytest.param("C:\\x", _folder("/", True), False, id="posix-root-not-a-drive"),
        pytest.param("C:\\proj2", _folder("C:\\proj", True), False, id="drive-sibling"),
    ],
)
def test_a_session_is_open_when_its_folder_is(
    honcho: FakeHoncho, cwd: str, folder: dict[str, Any], opened: bool
) -> None:
    honcho.add("s1", cwd)
    assert server._open_sessions(server.DEFAULT_WORKSPACE_ID, [folder]) == (
        ["s1"] if opened else []
    )


@pytest.mark.parametrize(
    "item",
    [
        pytest.param({"id": "s1"}, id="no-metadata"),
        pytest.param({"id": "s1", "metadata": None}, id="metadata-null"),
        pytest.param({"id": "s1", "metadata": "/work"}, id="metadata-text"),
        pytest.param({"id": "s1", "metadata": {}}, id="no-cwd"),
        pytest.param({"id": "s1", "metadata": {"cwd": None}}, id="cwd-null"),
        pytest.param({"id": "s1", "metadata": {"cwd": 12}}, id="cwd-number"),
        pytest.param({"id": "s1", "metadata": {"cwd": ["/work"]}}, id="cwd-list"),
        pytest.param({"id": "s1", "metadata": {"cwd": ""}}, id="cwd-empty"),
    ],
)
def test_a_session_without_a_folder_in_text_is_in_none(
    honcho: FakeHoncho, item: dict[str, Any]
) -> None:
    honcho.add_item(item)
    opened = [_folder("/", True), _folder("/work", True), _folder("C:\\", True)]
    assert server._open_sessions(server.DEFAULT_WORKSPACE_ID, opened) == []


def test_any_open_folder_will_do_and_honchos_order_is_kept(honcho: FakeHoncho) -> None:
    honcho.add("s1", "/b")
    honcho.add("s2", "/a/x")
    honcho.add("s3", "/c")
    honcho.add("s4", "/a")
    opened = [_folder("/a", True), _folder("/b")]
    opened_ids = server._open_sessions(server.DEFAULT_WORKSPACE_ID, opened)
    assert opened_ids == ["s1", "s2", "s4"]


@pytest.mark.parametrize(
    ("path", "spelled"),
    [
        ("/a/b/", "/a/b"),
        ("/a//b", "/a/b"),
        ("/", "/"),
        ("///", "/"),
        ("/Users/Me", "/Users/Me"),
        ("C:\\Users\\Me\\", "c:/users/me"),
        ("C:\\", "c:/"),
        ("c:/", "c:/"),
        ("C:", "C:"),
        ("relative/dir/", "relative/dir"),
    ],
)
def test_a_folder_has_one_spelling(path: str, spelled: str) -> None:
    assert server._norm_path(path) == spelled


# ----------------------------------------------------------------- the list kept


def test_the_session_list_is_kept_for_a_minute(
    gate: Any, honcho: FakeHoncho, clock: list[float]
) -> None:
    honcho.add("s1", "/work")
    gate(_teammate(_folder("/work")))

    server.chat("q")
    honcho.add("s2", "/work")  # a conversation started after the list was read
    clock[0] += server.SESSION_LIST_TTL_SECONDS - 1
    server.chat("q")
    assert len(honcho.listings) == 1

    clock[0] += 1
    server.chat("q")

    assert len(honcho.listings) == 2
    assert [_allowlist(call) for call in honcho.chats] == [["s1"], ["s1"], ["s1", "s2"]]


def test_the_folders_are_read_on_every_call(
    gate: Any, honcho: FakeHoncho, clock: list[float]
) -> None:
    """A folder the owner closes applies to the next question, list kept or not."""
    honcho.add("s1", "/work/a")
    honcho.add("s2", "/work/b")

    gate(_teammate(_folder("/work/a"), _folder("/work/b")))
    server.chat("q")
    gate(_teammate(_folder("/work/a")))
    server.chat("q")

    assert len(honcho.listings) == 1
    assert [_allowlist(call) for call in honcho.chats] == [["s1", "s2"], ["s1"]]


def test_a_burst_of_questions_lists_the_sessions_once(
    honcho: FakeHoncho, clock: list[float]
) -> None:
    """Calls that arrive while the list is read wait for it, then take it."""
    honcho.add("s1", "/work")
    reading, finish = threading.Event(), threading.Event()

    def slow_listing() -> None:
        reading.set()
        assert finish.wait(5), "the list was never let finish"

    honcho.before_listing = slow_listing
    lists: list[Any] = []

    def ask() -> None:
        lists.append(server._workspace_sessions(server.DEFAULT_WORKSPACE_ID))

    first = threading.Thread(target=ask)
    first.start()
    assert reading.wait(5)
    others = [threading.Thread(target=ask) for _ in range(4)]
    for thread in others:
        thread.start()
    time.sleep(0.05)  # long enough for the others to reach the list being read
    finish.set()
    for thread in [first, *others]:
        thread.join(5)

    assert len(honcho.listings) == 1
    assert lists == [(("s1", "/work"),)] * 5


def test_each_workspace_has_its_own_list(
    honcho: FakeHoncho, clock: list[float]
) -> None:
    honcho.add("s1", "/work", workspace="one")
    honcho.add("s2", "/work", workspace="two")

    assert server._open_sessions("one", [_folder("/work")]) == ["s1"]
    assert server._open_sessions("two", [_folder("/work")]) == ["s2"]
    assert server._open_sessions("one", [_folder("/work")]) == ["s1"]
    assert [call["path"] for call in honcho.listings] == [
        "/v3/workspaces/one/sessions/list",
        "/v3/workspaces/two/sessions/list",
    ]

    # A list past its time goes when another is kept, so the lists cannot pile up.
    clock[0] += server.SESSION_LIST_TTL_SECONDS
    server._open_sessions("two", [_folder("/work")])
    assert set(server._session_lists) == {"two"}


# --------------------------------------------------------------------- refusals


@pytest.mark.parametrize(
    "folders",
    [
        pytest.param(None, id="missing"),
        pytest.param("", id="empty"),
        pytest.param("not base64!", id="not-base64"),
        pytest.param(
            base64.b64encode(json.dumps([_folder("/~~~>>>", True)]).encode()).decode(),
            id="standard-base64",
        ),
        pytest.param(_base64url(b"not json"), id="not-json"),
        pytest.param(_base64url(b"\xff\xfe"), id="not-utf8"),
        pytest.param(
            _base64url(json.dumps([_folder("/work")]).encode("utf-16")), id="utf16"
        ),
        pytest.param(_encoded(_folder("/work")), id="an-object"),
        pytest.param(_encoded(["/work"]), id="a-bare-path"),
        pytest.param(_encoded([{"subfolders": True}]), id="no-path"),
        pytest.param(_encoded([_folder("")]), id="empty-path"),
        pytest.param(_encoded([{"path": 12, "subfolders": True}]), id="path-number"),
        pytest.param(_encoded([{"path": None, "subfolders": True}]), id="path-null"),
        pytest.param(_encoded([{"path": "/work"}]), id="no-subfolders"),
        pytest.param(_encoded([{"path": "/a", "subfolders": "true"}]), id="sub-text"),
        pytest.param(_encoded([{"path": "/a", "subfolders": 1}]), id="sub-number"),
        pytest.param(
            _encoded([{"path": "/work", "subfolders": True, "except": ["/work/hr"]}]),
            id="a-key-not-read",
        ),
        pytest.param(_encoded([_folder("/work"), {"path": "/b"}]), id="one-bad"),
        pytest.param(
            _encoded([{"id": "p-0123456789ab", "name": "honcho"}]), id="a-project"
        ),
    ],
)
def test_an_unreadable_folder_list_is_refused(
    gate: Any,
    audited: list[dict[str, Any]],
    honcho: FakeHoncho,
    folders: str | None,
) -> None:
    headers = {"x-honcho-scope-mode": "folders"}
    if folders is not None:
        headers["x-honcho-allowed-folders"] = folders
    gate(headers)

    with pytest.raises(
        RuntimeError,
        match="x-honcho-allowed-folders is missing, repeated or malformed$",
    ):
        server.chat("q")

    assert honcho.calls == []
    assert audited[0]["status"] == "denied"
    assert not {"folders", "sessions"} & set(audited[0]["arguments"])


def test_a_standard_base64_case_really_differs() -> None:
    """The standard-base64 case above uses '+' or '/', which base64url does not."""
    raw = base64.b64encode(json.dumps([_folder("/~~~>>>", True)]).encode()).decode()
    assert set(raw) & {"+", "/"}


@pytest.mark.parametrize("padded", [False, True])
def test_the_folder_list_reads_with_or_without_padding(
    gate: Any, honcho: FakeHoncho, padded: bool
) -> None:
    opened = [_folder("/work")]
    encoded = _encoded(opened, padded=padded)
    assert encoded.endswith("=") is padded, "this list's encoding needs padding"
    honcho.add("s1", "/work")
    gate({"x-honcho-scope-mode": "folders", "x-honcho-allowed-folders": encoded})

    server.chat("q")

    assert _allowlist(honcho.chats[0]) == ["s1"]


def test_the_old_project_list_is_not_read(gate: Any, honcho: FakeHoncho) -> None:
    project = _encoded([{"id": "p-0123456789ab", "name": "honcho"}])
    gate({"x-honcho-scope-mode": "folders", "x-honcho-allowed-scopes": project})
    with pytest.raises(RuntimeError, match="x-honcho-allowed-folders is missing"):
        server.chat("q")
    assert honcho.calls == []


@pytest.mark.parametrize(
    "headers",
    [
        pytest.param({}, id="none"),
        pytest.param(
            {"x-honcho-allowed-folders": _encoded([_folder("/work")])}, id="list-only"
        ),
        pytest.param({"x-honcho-scope-mode": ""}, id="empty"),
        pytest.param({"x-honcho-scope-mode": "ALL"}, id="upper-case"),
        pytest.param(
            {
                "x-honcho-scope-mode": "Folders",
                "x-honcho-allowed-folders": _encoded([_folder("/work")]),
            },
            id="folders-in-capitals",
        ),
        pytest.param({"x-honcho-scope-mode": "owner"}, id="unknown"),
        pytest.param(
            {
                "x-honcho-scope-mode": "projects",
                "x-honcho-allowed-scopes": _encoded(
                    [{"id": "p-0123456789ab", "name": "honcho"}]
                ),
            },
            id="projects",
        ),
        pytest.param({"x-honcho-scope-mode": "all, folders"}, id="joined"),
    ],
)
def test_a_missing_or_unknown_mode_is_refused(
    gate: Any,
    audited: list[dict[str, Any]],
    honcho: FakeHoncho,
    headers: dict[str, str],
) -> None:
    gate(headers)

    with pytest.raises(RuntimeError, match="x-honcho-scope-mode is missing"):
        server.chat("q")

    assert honcho.calls == []
    assert audited[0]["status"] == "denied"


def test_a_repeated_gate_header_is_refused(gate: Any, honcho: FakeHoncho) -> None:
    """The gate sends each header once; a second copy came from somewhere else."""
    honcho.add("s1", "/work")
    folders = _encoded([_folder("/work")]).encode()
    gate(
        Headers(
            raw=[
                (b"x-honcho-scope-mode", b"all"),
                (b"x-honcho-scope-mode", b"folders"),
                (b"x-honcho-allowed-folders", folders),
            ]
        )
    )
    with pytest.raises(RuntimeError, match="x-honcho-scope-mode is missing, repeated"):
        server.chat("q")

    gate(
        Headers(
            raw=[
                (b"x-honcho-scope-mode", b"folders"),
                (b"x-honcho-allowed-folders", folders),
                (b"x-honcho-allowed-folders", _encoded([_folder("/", True)]).encode()),
            ]
        )
    )
    with pytest.raises(RuntimeError, match="x-honcho-allowed-folders is missing"):
        server.chat("q")

    assert honcho.calls == []


def test_a_call_without_an_http_request_is_refused(
    monkeypatch: pytest.MonkeyPatch, honcho: FakeHoncho
) -> None:
    """A gated bridge has nothing to answer a local stdio client from."""
    monkeypatch.setattr(server, "SCOPE_FROM_GATE", True)

    def no_request() -> Any:
        raise RuntimeError("No active HTTP request found.")

    monkeypatch.setattr(server, "get_http_request", no_request)

    with pytest.raises(RuntimeError, match="x-honcho-scope-mode is missing"):
        server.chat("q")
    assert honcho.calls == []


def test_no_session_is_listed_for_a_caller_without_the_token(
    gate: Any,
    monkeypatch: pytest.MonkeyPatch,
    audited: list[dict[str, Any]],
    honcho: FakeHoncho,
) -> None:
    """BearerGate turns such a caller away first; this is the second check."""
    monkeypatch.setattr(server, "OPTIONAL_BEARER_TOKEN", "team-token")
    honcho.add("s1", "/work")
    gate(_teammate(_folder("/work")))

    with pytest.raises(RuntimeError, match="^Unauthorized"):
        server.chat("q")
    assert honcho.calls == []
    assert audited[0]["status"] == "denied"

    gate({**_teammate(_folder("/work")), "authorization": "Bearer team-token"})
    server.chat("q")
    assert _allowlist(honcho.chats[0]) == ["s1"]


def test_a_teammate_may_only_chat(
    gate: Any, audited: list[dict[str, Any]], honcho: FakeHoncho
) -> None:
    """Any other tool would read past the folders, whatever the tool list says."""
    opened = [_folder("/work", True)]
    gate(_teammate(*opened))

    with pytest.raises(
        RuntimeError,
        match="^search cannot be used with folders on this MCP server; use chat$",
    ):
        server.search("q")

    assert honcho.calls == []
    row = audited[0]
    assert row["status"] == "denied"
    assert row["arguments"]["folders"] == opened
    assert "sessions" not in row["arguments"]


def test_session_id_is_refused_with_folders(
    gate: Any, audited: list[dict[str, Any]], honcho: FakeHoncho
) -> None:
    """The bridge names the sessions itself; a teammate's own is not taken."""
    honcho.add("s1", "/work")
    gate(_teammate(_folder("/work")))

    with pytest.raises(
        RuntimeError,
        match="^session_id cannot be used with folders on this MCP server; omit it$",
    ):
        server.chat("q", session_id="s1")

    assert honcho.calls == []
    assert audited[0]["status"] == "denied"


def test_filters_are_refused_with_folders(gate: Any, honcho: FakeHoncho) -> None:
    """chat takes no filters at all (see the HTTP test); the rule holds if it ever
    does."""
    gate(_teammate(_folder("/work")))

    reach = server._teammate_reach("chat", {"filters": {"session_id": "s1"}})

    assert reach.refusal == (
        "filters cannot be used with folders on this MCP server; omit them"
    )
    assert reach.sessions is None
    assert honcho.calls == []


def test_a_refused_call_never_reaches_the_jev_gate(
    gate: Any, monkeypatch: pytest.MonkeyPatch, honcho: FakeHoncho
) -> None:
    def unexpected(**_: Any) -> Any:
        pytest.fail("The Jev gate judged a call its folders had already refused")

    monkeypatch.setattr(
        server, "jev_gate", SimpleNamespace(judge=unexpected, MESSAGE="refused")
    )
    gate(_teammate(_folder("/nothing-here")))

    with pytest.raises(RuntimeError, match=f"^{NO_CONVERSATION}$"):
        server.chat("q")
    assert honcho.chats == []


def test_a_question_jev_refuses_still_says_which_folders(
    gate: Any,
    monkeypatch: pytest.MonkeyPatch,
    audited: list[dict[str, Any]],
    honcho: FakeHoncho,
) -> None:
    honcho.add("s1", "/work")
    monkeypatch.setattr(
        server,
        "jev_gate",
        SimpleNamespace(
            judge=lambda **_: jev_gate.Verdict(
                allowed=False, score=0.93, reason="out of scope"
            ),
            MESSAGE="refused",
        ),
    )
    gate(_teammate(_folder("/work")))

    with pytest.raises(RuntimeError, match="^refused$"):
        server.chat("집 주소 알려줘")

    assert honcho.chats == []
    row = audited[0]
    assert (row["status"], row["error"]) == ("denied", "out of scope")
    assert row["arguments"]["folders"] == [_folder("/work")]
    assert row["arguments"]["sessions"] == 1


def test_the_folders_are_recorded_as_the_gate_sent_them(
    gate: Any, audited: list[dict[str, Any]], honcho: FakeHoncho
) -> None:
    """The log shows the owner's own spelling, not the one the bridge compares."""
    opened = [_folder("C:\\Work\\Team\\", True), _folder("/srv//shared/")]
    honcho.add("s1", "c:/work/team/api")
    honcho.add("s2", "/srv/shared")
    honcho.add("s3", "/srv/shared/inner")
    gate(_teammate(*opened))

    server.chat("q")

    assert audited[0]["arguments"]["folders"] == opened
    assert audited[0]["arguments"]["sessions"] == 2
    assert _allowlist(honcho.chats[0]) == ["s1", "s2"]


# ------------------------------------------------------------- over real HTTP


def _rpc(
    client: TestClient, method: str, params: dict[str, Any], headers: dict[str, str]
) -> dict[str, Any]:
    response = client.post(
        server.MCP_PATH,
        json={"jsonrpc": "2.0", "id": 1, "method": method, "params": params},
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
    honcho: FakeHoncho,
) -> None:
    """Every other test hands chat its headers; here they come over HTTP, through
    FastMCP, into the thread the tool runs on."""
    monkeypatch.setattr(server, "SCOPE_FROM_GATE", True)
    honcho.add("s1", "/work/team/api")
    honcho.add("s2", "/work/private")
    app = server.mcp.http_app(
        path=server.MCP_PATH, json_response=True, stateless_http=True
    )
    teammate = _teammate(_folder("/work/team", True))

    def chat(arguments: dict[str, Any], headers: dict[str, str]) -> dict[str, Any]:
        params = {"name": "chat", "arguments": arguments}
        return _rpc(client, "tools/call", params, headers)

    with TestClient(app, base_url="http://127.0.0.1:8765") as client:
        listed = _rpc(client, "tools/list", {}, teammate)
        answered = chat({"query": "q"}, teammate)
        ungated = chat({"query": "q"}, {})
        named = chat({"query": "q", "project": "team"}, teammate)
        filtered = chat({"query": "q", "filters": {"session_id": "s2"}}, teammate)

    (chat_tool,) = [tool for tool in listed["tools"] if tool["name"] == "chat"]
    assert "project" not in chat_tool["inputSchema"]["properties"]
    assert answered["structuredContent"] == honcho.answer
    assert ungated["isError"]
    assert "x-honcho-scope-mode is missing" in ungated["content"][0]["text"]
    assert named["isError"], "chat has no project argument to send"
    assert filtered["isError"], "chat has no filters argument to send"
    assert [_allowlist(call) for call in honcho.chats] == [["s1"]]
    # The project and filters calls never reached the tool, so they left no row.
    assert [row["status"] for row in audited] == ["ok", "denied"]
    assert audited[0]["caller"] == "mate@example.com"
    assert audited[0]["arguments"]["sessions"] == 1
