"""The tools a teammate's own agent reads the open conversations with.

list_conversations, search_messages, read_conversation and search_conclusions: what
each asks Honcho, what it gives back, and how each holds a teammate to the sessions
open to them while the owner reaches every one. test_folder_scopes.py holds the
rules for which sessions are open.
"""

from __future__ import annotations

import base64
import json
from types import SimpleNamespace
from typing import Any

import pytest

import audit
import jev_gate
import server

WS = server.DEFAULT_WORKSPACE_ID
OWNER_PEER = server.DEFAULT_USER_NAME
OWNER = {
    "cf-access-authenticated-user-email": "me@example.com",
    "x-honcho-scope-mode": "all",
}


def _teammate(*paths: str) -> dict[str, str]:
    folders = [{"path": path, "subfolders": True} for path in paths]
    return {
        "cf-access-authenticated-user-email": "mate@example.com",
        "x-honcho-scope-mode": "folders",
        "x-honcho-allowed-folders": base64.urlsafe_b64encode(json.dumps(folders).encode())
        .decode()
        .rstrip("="),
    }


def _message(peer: str, content: str, at: str) -> dict[str, Any]:
    return {"id": f"m-{at}", "peer_id": peer, "content": content, "created_at": at}


class FakeHoncho:
    """Sessions with their messages, a search and a conclusion search, answered here."""

    def __init__(self) -> None:
        self.sessions: list[dict[str, Any]] = []
        self.messages: dict[str, list[dict[str, Any]]] = {}
        self.found: list[dict[str, Any]] = []
        self.conclusions: list[dict[str, Any]] = []
        self.calls: list[dict[str, Any]] = []

    def add(
        self,
        session_id: str,
        cwd: str,
        messages: list[dict[str, Any]],
        **metadata: Any,
    ) -> None:
        self.sessions.append({"id": session_id, "metadata": {"cwd": cwd, **metadata}})
        self.messages[session_id] = messages

    def __call__(
        self, method: str, path: str, *, body: Any = None, params: Any = None
    ) -> Any:
        self.calls.append({"method": method, "path": path, "body": body, "params": params})
        if path.endswith("/sessions/list"):
            return {"items": self.sessions, "total": len(self.sessions), "page": 1, "pages": 1}
        if path.endswith("/messages/list"):
            session_id = path.split("/")[5]
            items = list(self.messages.get(session_id, []))
            if params.get("reverse") == "true":
                items.reverse()
            page, size = params["page"], params["size"]
            return {
                "items": items[(page - 1) * size : page * size],
                "total": len(items),
                "page": page,
                "size": size,
                "pages": max(1, -(-len(items) // size)),
            }
        if path.endswith("/search"):
            return self.found
        if path.endswith("/conclusions/query"):
            return self.conclusions
        return {"ok": True}

    def to(self, ending: str) -> list[dict[str, Any]]:
        return [call for call in self.calls if call["path"].endswith(ending)]


@pytest.fixture(autouse=True)
def honcho(monkeypatch: pytest.MonkeyPatch) -> FakeHoncho:
    fake = FakeHoncho()
    monkeypatch.setattr(server, "_honcho", fake)
    monkeypatch.setattr(server, "_session_lists", {})
    monkeypatch.setattr(server, "_heads", {})
    monkeypatch.setattr(server, "OPTIONAL_BEARER_TOKEN", "")
    monkeypatch.setattr(server.audit, "record", lambda **_: None)
    return fake


@pytest.fixture
def gate(monkeypatch: pytest.MonkeyPatch) -> Any:
    monkeypatch.setattr(server, "SCOPE_FROM_GATE", True)

    def send(headers: dict[str, str]) -> None:
        monkeypatch.setattr(
            server, "get_http_request", lambda: SimpleNamespace(headers=headers, client=None)
        )

    return send


@pytest.fixture
def three(honcho: FakeHoncho) -> FakeHoncho:
    """Two conversations in /work and one in /private, each with a different last turn."""
    honcho.add(
        "s-old",
        "/work/api",
        [
            _message("assistant_codex", "started", "2026-10-01T00:00:00Z"),
            _message(OWNER_PEER, "API 응답이 느려요", "2026-10-01T00:00:05Z"),
        ],
        last_turn_at="2026-10-02T09:00:00Z",
        project_name="api",
        git_branch="main",
    )
    honcho.add(
        "s-new",
        "/work/web",
        [_message(OWNER_PEER, "로그인 화면 고치자", "2026-10-11T00:00:00Z")],
        last_turn_at="2026-10-12T03:00:00Z",
    )
    honcho.add(
        "s-private",
        "/private",
        [_message(OWNER_PEER, "병원 예약", "2026-10-12T00:00:00Z")],
        last_turn_at="2026-10-12T05:00:00Z",
    )
    return honcho


# ---------------------------------------------------------- list_conversations


def test_a_teammate_lists_the_open_conversations_latest_first(
    gate: Any, three: FakeHoncho
) -> None:
    gate(_teammate("/work"))

    listed = server.list_conversations()

    assert listed["total"] == listed["shown"] == 2
    assert listed["conversations"] == [
        {
            "id": "s-new",
            "started_at": "2026-10-11T00:00:00Z",
            "last_at": "2026-10-12T03:00:00Z",
            "messages": 1,
            "folder": "web",
            "app": "unknown",
            "project": None,
            "branch": None,
            "opening": "로그인 화면 고치자",
        },
        {
            "id": "s-old",
            "started_at": "2026-10-01T00:00:00Z",
            "last_at": "2026-10-02T09:00:00Z",
            "messages": 2,
            "folder": "api",
            "app": "unknown",
            "project": "api",
            "branch": "main",
            # The owner's own first words, not the agent's.
            "opening": "API 응답이 느려요",
        },
    ]
    assert "s-private" not in json.dumps(three.to("/messages/list"))


def test_the_owner_lists_every_conversation(gate: Any, three: FakeHoncho) -> None:
    gate(OWNER)
    assert [item["id"] for item in server.list_conversations()["conversations"]] == [
        "s-private",
        "s-new",
        "s-old",
    ]


def test_since_keeps_those_active_after_and_until_those_started_before(
    gate: Any, three: FakeHoncho
) -> None:
    gate(OWNER)

    def ids(**kwargs: Any) -> list[str]:
        return [item["id"] for item in server.list_conversations(**kwargs)["conversations"]]

    assert ids(since="2026-10-12") == ["s-private", "s-new"]
    # s-new started on 10-11 and ran on 10-12: it was active on 10-11.
    assert ids(since="2026-10-11", until="2026-10-11") == ["s-new"]
    assert ids(until="2026-10-01") == ["s-old"]
    assert ids(since="2026-10-12T04:00:00+00:00") == ["s-private"]
    # A day in Korea starts 9 hours before the same day in UTC.
    assert ids(until="2026-10-11T08:59:59+09:00") == ["s-old"]


def test_contains_looks_in_the_folder_project_and_opening(
    gate: Any, three: FakeHoncho
) -> None:
    gate(_teammate("/work"))

    def ids(words: str) -> list[str]:
        return [
            item["id"] for item in server.list_conversations(contains=words)["conversations"]
        ]

    assert ids("web") == ["s-new"]
    assert ids("API 느려요") == ["s-old"]
    assert ids("main") == ["s-old"]
    assert ids("병원") == []


def test_limit_cuts_the_list_and_total_counts_all(gate: Any, three: FakeHoncho) -> None:
    gate(OWNER)
    listed = server.list_conversations(limit=1)
    assert (listed["total"], listed["shown"]) == (3, 1)
    # Only the conversation shown had its start looked up.
    assert len(three.to("/messages/list")) == 1


def test_a_start_is_looked_up_again_only_after_new_messages(
    gate: Any, three: FakeHoncho, monkeypatch: pytest.MonkeyPatch
) -> None:
    gate(OWNER)
    server.list_conversations()
    server.list_conversations()
    assert len(three.to("/messages/list")) == 3

    three.sessions[1]["metadata"]["last_turn_at"] = "2026-10-13T00:00:00Z"
    monkeypatch.setattr(server, "_session_lists", {})
    server.list_conversations()
    looked = [call["path"].split("/")[5] for call in three.to("/messages/list")]
    assert looked[3:] == ["s-new"]


def test_a_conversation_without_a_last_turn_goes_by_its_import(
    gate: Any, honcho: FakeHoncho
) -> None:
    honcho.add("s-a", "/work", [], last_imported_at="2026-10-05T00:00:00Z")
    honcho.sessions.append(
        {"id": "s-b", "metadata": {"cwd": "/work"}, "created_at": "2026-10-06T00:00:00Z"}
    )
    gate(OWNER)
    listed = server.list_conversations()["conversations"]
    assert [(item["id"], item["last_at"]) for item in listed] == [
        ("s-b", "2026-10-06T00:00:00Z"),
        ("s-a", "2026-10-05T00:00:00Z"),
    ]
    assert listed[1]["opening"] == "" and listed[1]["started_at"] is None


def test_a_bad_date_is_refused_in_so_many_words(gate: Any, three: FakeHoncho) -> None:
    gate(OWNER)
    with pytest.raises(RuntimeError, match="^since takes a date or a time"):
        server.list_conversations(since="어제")


# ---------------------------------------------------------------- search_messages


def test_a_search_within_dates_and_one_conversation(gate: Any, three: FakeHoncho) -> None:
    three.found = [
        {**_message(OWNER_PEER, "x" * 2000, "2026-10-11T00:00:00Z"), "session_id": "s-new"}
    ]
    gate(_teammate("/work"))

    found = server.search_messages(
        "로그인", since="2026-10-11", until="2026-10-11", conversation_id="s-new", limit=99
    )

    assert three.to("/search")[0]["body"] == {
        "query": "로그인",
        "limit": 30,
        "filters": {
            "session_id": "s-new",
            "created_at": {
                "gte": "2026-10-11T00:00:00+00:00",
                "lte": "2026-10-11T23:59:59.999999+00:00",
            },
        },
    }
    (message,) = found["messages"]
    assert message["conversation_id"] == "s-new"
    assert message["content"].endswith("…[500 more characters]")


# -------------------------------------------------------------- read_conversation


def test_a_teammate_reads_an_open_conversation_a_page_at_a_time(
    gate: Any, three: FakeHoncho
) -> None:
    gate(_teammate("/work"))

    first = server.read_conversation("s-old", size=1)
    latest = server.read_conversation("s-old", size=1, newest_first=True)

    assert first == {
        "id": "s-old",
        "folder": "api",
        "app": "unknown",
        "total": 2,
        "page": 1,
        "pages": 2,
        "newest_first": False,
        "messages": [
            {"at": "2026-10-01T00:00:00Z", "speaker": "assistant_codex", "content": "started"}
        ],
    }
    assert latest["messages"][0]["content"] == "API 응답이 느려요"
    assert [call["params"] for call in three.to("/messages/list")] == [
        {"page": 1, "size": 1, "reverse": "false"},
        {"page": 1, "size": 1, "reverse": "true"},
    ]


def test_a_conversation_id_is_sent_as_one_path_segment(
    gate: Any, honcho: FakeHoncho
) -> None:
    honcho.add("a/b c", "/work", [])
    gate(OWNER)
    server.read_conversation("a/b c")
    assert honcho.to("/messages/list")[0]["path"] == (
        f"/v3/workspaces/{WS}/sessions/a%2Fb%20c/messages/list"
    )


# ------------------------------------------------------------- search_conclusions


def test_a_teammate_gets_only_facts_drawn_from_one_open_conversation(
    gate: Any, three: FakeHoncho
) -> None:
    three.conclusions = [{"session_id": "s-new", "content": "로그인 화면을 고치고 있다"}]
    gate(_teammate("/work"))

    found = server.search_conclusions("로그인", limit=50)

    assert three.to("/conclusions/query")[0]["body"] == {
        "query": "로그인",
        "top_k": 30,
        "filters": {
            "observer": OWNER_PEER,
            "observed": OWNER_PEER,
            "level": "explicit",
            "session_id": {"in": ["s-old", "s-new"]},
        },
    }
    assert found == {
        "conclusions": [
            {
                "conversation_id": "s-new",
                "conversation_last_at": "2026-10-12T03:00:00Z",
                "content": "로그인 화면을 고치고 있다",
            }
        ]
    }


def test_the_owner_searches_every_fact(gate: Any, three: FakeHoncho) -> None:
    gate(OWNER)
    server.search_conclusions("로그인")
    assert three.to("/conclusions/query")[0]["body"]["filters"] == {
        "observer": OWNER_PEER,
        "observed": OWNER_PEER,
    }


# ------------------------------------------------------------------- the question


def test_the_teammates_question_is_logged_and_judged_with_the_search(
    gate: Any, three: FakeHoncho, monkeypatch: pytest.MonkeyPatch
) -> None:
    rows: list[dict[str, Any]] = []
    judged: list[str] = []
    monkeypatch.setattr(server.audit, "record", lambda **row: rows.append(row))

    def judge(**kwargs: Any) -> jev_gate.Verdict:
        judged.append(kwargs["query"])
        return jev_gate.Verdict(allowed=True, score=0.01, reason="in scope")

    monkeypatch.setattr(
        server,
        "jev_gate",
        SimpleNamespace(
            judge=judge,
            judge_answer=lambda **_: jev_gate.Verdict(allowed=True, score=0.0, reason="ok"),
            MESSAGE="refused",
        ),
    )
    gate(_teammate("/work"))

    server.search_messages("로그인", question="첸징 요즘 로그인 작업 어디까지 했어?")

    assert judged == ["첸징 요즘 로그인 작업 어디까지 했어?\n로그인"]
    assert audit.query_text_of(rows[0]["arguments"]) == "첸징 요즘 로그인 작업 어디까지 했어?"


def test_the_gate_judges_the_teammate_tools_by_default() -> None:
    assert set(jev_gate.TEAMMATE_TOOLS.split(",")) == set(server.TEAMMATE_TOOLS)


# ------------------------------------------------------------ what is hidden


def test_a_teammate_gets_numbers_hidden_and_the_owner_gets_them_as_written(
    gate: Any, three: FakeHoncho, monkeypatch: pytest.MonkeyPatch
) -> None:
    rows: list[dict[str, Any]] = []
    judged: list[Any] = []
    monkeypatch.setattr(server.audit, "record", lambda **row: rows.append(row))
    monkeypatch.setattr(
        server,
        "jev_gate",
        SimpleNamespace(
            judge=lambda **_: jev_gate.Verdict(allowed=True, score=0.01, reason="in scope"),
            judge_answer=lambda **kwargs: (
                judged.append(kwargs["result"])
                or jev_gate.Verdict(allowed=True, score=0.0, reason="ok")
            ),
            MESSAGE="refused",
        ),
    )
    said = "결제는 4111 1111 1111 1111로, 담당자 010-2345-6789"
    three.found = [{**_message(OWNER_PEER, said, "2026-10-11T00:00:00Z"), "session_id": "s-new"}]

    gate(_teammate("/work"))
    (message,) = server.search_messages("결제")["messages"]
    assert message["content"] == "결제는 [카드번호 가림]로, 담당자 [휴대폰 번호 가림]"
    assert judged[-1]["messages"][0]["content"] == "결제는 [가림]로, 담당자 [가림]"
    assert rows[-1]["arguments"]["masked"] == {"card": 1, "phone": 1}

    gate(OWNER)
    (mine,) = server.search_messages("결제")["messages"]
    assert mine["content"] == said
    assert "masked" not in rows[-1]["arguments"]


# -------------------------------------------------- where a conversation came from


def test_a_teammate_reads_a_folder_by_its_name_and_the_owner_by_its_path(
    gate: Any, three: FakeHoncho
) -> None:
    gate(_teammate("/work"))
    listed = server.list_conversations()
    assert [item["folder"] for item in listed["conversations"]] == ["web", "api"]
    assert server.read_conversation("s-old")["folder"] == "api"
    # `contains` matches the name a teammate reads, not the path above it.
    assert server.list_conversations(contains="work")["total"] == 0
    assert server.list_conversations(contains="api")["total"] == 1

    gate(OWNER)
    listed = server.list_conversations()
    assert [item["folder"] for item in listed["conversations"]][:2] == [
        "/private",
        "/work/web",
    ]
    assert server.read_conversation("s-old")["folder"] == "/work/api"
    assert server.list_conversations(contains="work")["total"] == 2


def test_a_teammate_reads_no_paths_in_metadata_and_the_owner_reads_them(
    gate: Any, honcho: FakeHoncho, monkeypatch: pytest.MonkeyPatch
) -> None:
    judged: list[Any] = []
    monkeypatch.setattr(
        server,
        "jev_gate",
        SimpleNamespace(
            judge=lambda **_: jev_gate.Verdict(allowed=True, score=0.01, reason="in scope"),
            judge_answer=lambda **kwargs: (
                judged.append(kwargs["result"])
                or jev_gate.Verdict(allowed=True, score=0.0, reason="ok")
            ),
            MESSAGE="refused",
        ),
    )
    said = {
        **_message(OWNER_PEER, "폴더 정리하자", "2026-10-16T00:00:00Z"),
        "session_id": "s-work",
        "metadata": {
            "source": "claude",
            "claude_role": "user",
            "direct_user": True,
            "transcript_path": "/Users/chenjing/.claude/projects/-Users-laonpeople-dev-x/ab.jsonl",
            "source_turn_hash": "6dc919848fe0761d7fa369363baf02cf",
            "claude_line_index": 750,
        },
    }
    honcho.add(
        "s-work",
        "/Users/laonpeople/dev/x",
        [said],
        source="claude",
        file_path="/Users/chenjing/.claude/projects/-Users-laonpeople-dev-x/ab.jsonl",
        original_session_id="ab",
        project_name="x",
    )
    honcho.found = [said]

    gate(_teammate("/Users/laonpeople"))
    (session,) = server.list_sessions()["items"]
    assert session["metadata"] == {"cwd": "x", "source": "claude", "project_name": "x"}
    (message,) = server.get_session_messages("s-work", page=1, size=20)["items"]
    assert message["metadata"] == {
        "source": "claude",
        "claude_role": "user",
        "direct_user": True,
    }
    (found,) = server.search("폴더")
    assert found["metadata"] == message["metadata"]
    # Jev reads what the teammate reads: no path and no hash to mistake for a number.
    assert judged[-1][0]["metadata"] == message["metadata"]

    gate(OWNER)
    (mine,) = server.list_sessions()["items"]
    assert mine["metadata"]["file_path"].endswith("ab.jsonl")
    assert mine["metadata"]["cwd"] == "/Users/laonpeople/dev/x"
    (message,) = server.get_session_messages("s-work", page=1, size=20)["items"]
    assert message["metadata"]["transcript_path"].endswith("ab.jsonl")


# ------------------------------------------------- what an app put into a turn


def test_a_teammate_reads_the_owners_words_without_what_an_app_put_in(
    gate: Any, honcho: FakeHoncho, monkeypatch: pytest.MonkeyPatch
) -> None:
    reminder = (
        "<system-reminder>\nContents of /Users/me/.claude/CLAUDE.md:\n"
        "- This machine is the user's home computer.\n</system-reminder>"
    )
    summary = "This session is being continued from a previous conversation that ran out of context."
    messages = [
        _message(OWNER_PEER, f"{reminder}\n\n{summary}", "2026-10-19T00:00:00Z"),
        _message(
            OWNER_PEER, f"{reminder}\n\nAPI 응답이 느려요", "2026-10-19T00:00:05Z"
        ),
        _message("assistant_claude", "캐시를 보겠습니다.", "2026-10-19T00:00:09Z"),
    ]
    honcho.add("s-work", "/work/api", messages, last_turn_at="2026-10-19T00:00:09Z")
    honcho.found = [{**message, "session_id": "s-work"} for message in messages]
    answer = honcho.__call__

    def with_context(
        method: str, path: str, *, body: Any = None, params: Any = None
    ) -> Any:
        if path.endswith("/context"):
            return {"id": "s-work", "messages": messages, "summary": None}
        return answer(method, path, body=body, params=params)

    monkeypatch.setattr(server, "_honcho", with_context)
    words = ["API 응답이 느려요", "캐시를 보겠습니다."]

    gate(_teammate("/work"))
    (listed,) = server.list_conversations()["conversations"]
    assert listed["opening"] == "API 응답이 느려요"
    assert [found["content"] for found in server.search("API")] == words
    page = server.get_session_messages("s-work", page=1, size=20)
    assert [message["content"] for message in page["items"]] == words
    context = server.get_session_context("s-work")
    assert [message["content"] for message in context["messages"]] == words
    assert [
        found["content"] for found in server.search_messages("API")["messages"]
    ] == words
    assert [
        said["content"] for said in server.read_conversation("s-work")["messages"]
    ] == words

    gate(OWNER)
    page = server.get_session_messages("s-work", page=1, size=20)
    assert [message["content"] for message in page["items"]] == [
        m["content"] for m in messages
    ]
    assert [found["content"] for found in server.search("API")] == [
        m["content"] for m in messages
    ]
