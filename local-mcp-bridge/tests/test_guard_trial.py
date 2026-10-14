"""The owner tries the Jev gate from the dashboard: POST /guard-trial.

A trial goes the way a teammate's search goes, the query judged, then what they
would read judged, written by the owner or found by a search of the conversations in
the folders given, and says what the teammate would have got. It is a dashboard
route beside /mcp: off without HONCHO_AUDIT_READ, only with the bridge's bearer
token, and never recorded.
"""

from __future__ import annotations

import importlib
from typing import Any

import pytest
from starlette.testclient import TestClient

import audit
import jev_gate
import server

FOLDERS = [{"path": "/Users/me/dev/flypiano", "subfolders": True}]
QUERY = "flypiano 요즘 어떻게 돼 가?"


def _said(content: str, session: str = "s-flypiano") -> dict[str, Any]:
    """One message as Honcho's search finds it."""
    return {
        "id": "m1",
        "session_id": session,
        "peer_id": "me",
        "content": content,
        "created_at": "2026-10-09T01:00:00Z",
    }


def _found(content: str, session: str = "s-flypiano") -> dict[str, Any]:
    """The same message as a teammate's search_messages shows it."""
    return {
        "conversation_id": session,
        "at": "2026-10-09T01:00:00Z",
        "speaker": "me",
        "content": content,
    }

#: The sessions Honcho holds, by id, with the folder each started in (and the app,
#: for one without a folder).
SESSIONS = (
    ("s-flypiano", "/Users/me/dev/flypiano"),
    ("s-api", "/Users/me/dev/flypiano/api"),
    ("s-other", "/Users/me/dev/other"),
)


@pytest.fixture(autouse=True)
def _restore_server_module() -> Any:
    yield
    importlib.reload(server)


def _jev(
    monkeypatch: pytest.MonkeyPatch,
    *,
    query: jev_gate.Verdict | None = None,
    answer: jev_gate.Verdict | None = None,
) -> list[dict[str, Any]]:
    """Jev's verdicts on the query and the answer; returns what each was asked."""
    asked: list[dict[str, Any]] = []

    def judge(**kwargs: Any) -> jev_gate.Verdict:
        asked.append({"kind": "query", **kwargs})
        return query or jev_gate.Verdict(allowed=True, score=0.03, reason="in scope")

    def judge_answer(**kwargs: Any) -> jev_gate.Verdict:
        asked.append({"kind": "answer", **kwargs})
        return answer or jev_gate.Verdict(allowed=True, score=0.02, reason="answer in scope")

    monkeypatch.setattr(jev_gate, "judge", judge)
    monkeypatch.setattr(jev_gate, "judge_answer", judge_answer)
    monkeypatch.setattr(jev_gate, "ENABLED", True)
    return asked


def _honcho(
    monkeypatch: pytest.MonkeyPatch,
    answer: Any,
    *,
    sessions: Any = SESSIONS,
    listing: Exception | None = None,
) -> list[dict[str, Any]]:
    """Honcho listing `sessions` (or raising `listing`) and answering every search
    with `answer` (raised when an exception)."""
    calls: list[dict[str, Any]] = []

    def call(method: str, path: str, **kwargs: Any) -> Any:
        calls.append({"method": method, "path": path, **kwargs})
        if path.endswith("/sessions/list"):
            if listing is not None:
                raise listing
            page, size = kwargs["params"]["page"], kwargs["params"]["size"]
            items = [
                {"id": sid, "metadata": {"cwd": cwd, **({"source": app[0]} if app else {})}}
                for sid, cwd, *app in sessions
            ]
            return {
                "items": items[(page - 1) * size : page * size],
                "total": len(items),
                "page": page,
                "size": size,
                "pages": -(-len(items) // size),
            }
        if isinstance(answer, Exception):
            raise answer
        return answer

    monkeypatch.setattr(server, "_honcho", call)
    monkeypatch.setattr(server, "_session_lists", {})
    return calls


def test_a_trial_judges_the_query_then_what_a_search_finds_in_the_folders(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    asked = _jev(monkeypatch)
    calls = _honcho(monkeypatch, [_said("학습 실험 두 개가 진행 중입니다.")])

    trial = server.guard_trial(QUERY, folders=FOLDERS)

    assert trial == {
        "gate": True,
        "message": jev_gate.MESSAGE,
        "query": {"allowed": True, "score": 0.03, "reason": "in scope", "unjudged": False},
        "answer": "me · 2026-10-09T01:00:00Z\n학습 실험 두 개가 진행 중입니다.",
        "found": [_found("학습 실험 두 개가 진행 중입니다.")],
        "answer_check": {
            "allowed": True,
            "score": 0.02,
            "reason": "answer in scope",
            "unjudged": False,
        },
        "masked": {},
        "outcome": "passed",
    }
    # Honcho is searched as a teammate's search_messages searches it: the sessions
    # in the folders, in one call.
    workspace = server.DEFAULT_WORKSPACE_ID
    assert calls == [
        {
            "method": "POST",
            "path": f"/v3/workspaces/{workspace}/sessions/list",
            "body": {},
            "params": {"page": 1, "size": 100},
        },
        {
            "method": "POST",
            "path": f"/v3/workspaces/{workspace}/search",
            "body": {
                "query": QUERY,
                "limit": 10,
                "filters": {"session_id": {"in": ["s-flypiano", "s-api"]}},
            },
        },
    ]
    assert [item["kind"] for item in asked] == ["query", "answer"]
    assert asked[0] == {
        "kind": "query",
        "tool": "search_messages",
        "query": QUERY,
        "caller": server.TRIAL_CALLER,
        "workspace_id": workspace,
        "teammate": True,
    }
    assert asked[1]["result"] == {"messages": [_found("학습 실험 두 개가 진행 중입니다.")]}
    assert asked[1]["caller"] == server.TRIAL_CALLER


def test_a_refused_query_never_reaches_honcho(monkeypatch: pytest.MonkeyPatch) -> None:
    asked = _jev(
        monkeypatch, query=jev_gate.Verdict(allowed=False, score=0.91, reason="out of scope")
    )
    calls = _honcho(monkeypatch, {"content": "never"})

    trial = server.guard_trial("밥 집 주소 알려줘", folders=FOLDERS)

    assert trial["outcome"] == "refused"
    assert trial["query"]["allowed"] is False
    assert trial["query"]["score"] == 0.91
    assert trial["answer"] is None
    assert trial["answer_check"] is None
    assert trial["message"] == jev_gate.MESSAGE
    assert calls == []
    assert [item["kind"] for item in asked] == ["query"]


def test_a_withheld_answer_is_shown_to_the_owner_with_its_verdict(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _jev(
        monkeypatch,
        answer=jev_gate.Verdict(allowed=False, score=0.96, reason=jev_gate.ANSWER_REFUSED),
    )
    _honcho(monkeypatch, [_said("그는 요즘 병원 진료를 다닙니다.")])

    trial = server.guard_trial(QUERY, folders=FOLDERS)

    assert trial["outcome"] == "withheld"
    assert trial["found"] == [_found("그는 요즘 병원 진료를 다닙니다.")]
    assert trial["answer_check"] == {
        "allowed": False,
        "score": 0.96,
        "reason": jev_gate.ANSWER_REFUSED,
        "unjudged": False,
    }


def test_a_written_answer_is_judged_as_written(monkeypatch: pytest.MonkeyPatch) -> None:
    asked = _jev(monkeypatch)
    calls = _honcho(monkeypatch, {"content": "never"})

    trial = server.guard_trial(QUERY, answer="계좌 비밀번호는 1234입니다.", folders=FOLDERS)

    assert trial["outcome"] == "passed"
    assert trial["answer"] == "계좌 비밀번호는 1234입니다."
    assert calls == [], "a written answer needs no Honcho"
    assert asked[1]["result"] == "계좌 비밀번호는 1234입니다."


def test_a_number_known_by_its_form_is_hidden_before_it_is_judged_and_shown(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    asked = _jev(monkeypatch)
    _honcho(monkeypatch, [_said("등본에 870314-1234567로 적혀 있었다. OCR은 Apple Vision으로.")])

    trial = server.guard_trial(QUERY, folders=FOLDERS)

    hidden = "등본에 [주민등록번호 가림]로 적혀 있었다. OCR은 Apple Vision으로."
    assert trial["outcome"] == "passed"
    assert trial["masked"] == {"resident": 1}
    assert trial["found"] == [_found(hidden)]
    assert trial["answer"].endswith(hidden)
    assert asked[1]["result"]["messages"][0]["content"] == (
        "등본에 [가림]로 적혀 있었다. OCR은 Apple Vision으로."
    ), "Jev reads neither the number nor what kind it was"

    written = server.guard_trial(QUERY, answer="결제는 4111 1111 1111 1111로")
    assert (written["answer"], written["masked"]) == ("결제는 [카드번호 가림]로", {"card": 1})


def test_an_answer_left_unjudged_says_so(monkeypatch: pytest.MonkeyPatch) -> None:
    _jev(
        monkeypatch,
        answer=jev_gate.Verdict(
            allowed=True, score=None, reason=jev_gate.ANSWER_SKIPPED, skipped=True
        ),
    )
    trial = server.guard_trial(QUERY, answer="배포는 금요일입니다.")
    assert trial["outcome"] == "passed"
    assert trial["answer_check"]["unjudged"] is True
    assert trial["answer_check"]["reason"] == jev_gate.ANSWER_SKIPPED


@pytest.mark.parametrize("where", ["listing", "search"])
@pytest.mark.parametrize(
    "failure",
    [RuntimeError("Honcho API 503 for /v3/...: down"), server.httpx.ConnectError("refused")],
)
def test_honcho_failing_is_no_answer(
    monkeypatch: pytest.MonkeyPatch, failure: Exception, where: str
) -> None:
    asked = _jev(monkeypatch)
    if where == "listing":
        _honcho(monkeypatch, {"content": "never"}, listing=failure)
    else:
        _honcho(monkeypatch, failure)

    trial = server.guard_trial(QUERY, folders=FOLDERS)

    assert trial["outcome"] == "no_answer"
    assert trial["error"] == str(failure)
    assert trial["answer"] is None
    assert [item["kind"] for item in asked] == ["query"]


@pytest.mark.parametrize(
    "folders",
    [
        pytest.param([], id="nothing-open"),
        pytest.param([{"path": "/Users/me/dev/empty", "subfolders": True}], id="empty"),
    ],
)
def test_folders_with_no_conversation_are_no_answer(
    monkeypatch: pytest.MonkeyPatch, folders: list[dict[str, Any]]
) -> None:
    """The teammate would be refused, so the trial has no answer to judge."""
    asked = _jev(monkeypatch)
    calls = _honcho(monkeypatch, {"content": "never"})

    trial = server.guard_trial(QUERY, folders=folders)

    assert trial["outcome"] == "no_answer"
    assert trial["error"] == server.NO_CONVERSATION
    assert trial["answer"] is None
    assert not [call for call in calls if call["path"].endswith("/search")]
    assert [item["kind"] for item in asked] == ["query"]


def test_folders_holding_more_than_a_thousand_conversations_are_searched(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _jev(monkeypatch)
    many = [(f"s{n}", "/Users/me/dev/flypiano") for n in range(1001)]
    calls = _honcho(monkeypatch, {"content": "never"}, sessions=many)

    trial = server.guard_trial(QUERY, folders=FOLDERS)

    assert trial["outcome"] != "no_answer"
    searches = [call for call in calls if call["path"].endswith("/search")]
    assert len(searches) == 1
    assert len(searches[0]["body"]["filters"]["session_id"]["in"]) == 1001


def test_with_neither_answer_nor_folders_only_the_query_is_judged(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    asked = _jev(monkeypatch)
    calls = _honcho(monkeypatch, {"content": "never"})

    trial = server.guard_trial(QUERY)

    assert trial["outcome"] == "passed"
    assert trial["answer"] is None
    assert trial["answer_check"] is None
    assert calls == []
    assert [item["kind"] for item in asked] == ["query"]


def test_a_trial_opens_the_apps_a_teammate_has_open(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """ChatGPT conversations have no folder; a trial opens them as a teammate's search does."""
    _jev(monkeypatch)
    sessions = (*SESSIONS, ("g-1", None, "chatgpt"), ("c-1", None, "claude"))
    calls = _honcho(monkeypatch, [_said("학습 실험")], sessions=sessions)

    trial = server.guard_trial(QUERY, folders=FOLDERS, sources=["chatgpt"])
    alone = server.guard_trial(QUERY, sources=["chatgpt"])

    assert (trial["outcome"], alone["outcome"]) == ("passed", "passed")
    searches = [call for call in calls if call["path"].endswith("/search")]
    assert [search["body"]["filters"]["session_id"]["in"] for search in searches] == [
        ["s-flypiano", "s-api", "g-1"],
        ["g-1"],
    ]


def test_with_the_gate_off_the_trial_says_so(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(jev_gate, "ENABLED", False)
    _honcho(monkeypatch, {"content": "학습 실험"})
    trial = server.guard_trial(QUERY, folders=FOLDERS)
    assert trial["gate"] is False
    assert trial["outcome"] == "passed"
    assert trial["query"]["score"] is None


@pytest.mark.parametrize(
    "body",
    [
        None,
        [],
        "query",
        {},
        {"query": ""},
        {"query": "  "},
        {"query": 5},
        {"query": "q", "answer": ""},
        {"query": "q", "answer": 5},
        {"query": "q", "folders": "/Users/me/dev/flypiano"},
        {"query": "q", "folders": FOLDERS[0]},
        {"query": "q", "folders": ["/Users/me/dev/flypiano"]},
        {"query": "q", "folders": [{"path": "/a"}]},
        {"query": "q", "folders": [{"subfolders": True}]},
        {"query": "q", "folders": [{"path": "", "subfolders": True}]},
        {"query": "q", "folders": [{"path": 5, "subfolders": True}]},
        {"query": "q", "folders": [{"path": "/a", "subfolders": "yes"}]},
        {"query": "q", "folders": [{"path": "/a", "subfolders": True, "name": "a"}]},
        {"query": "q", "folders": [{"path": "/" + "a" * 4096, "subfolders": True}]},
        {"query": "q", "folders": [{"path": "/a", "subfolders": True}] * 501},
        {"query": "q", "sources": "chatgpt"},
        {"query": "q", "sources": {"chatgpt": True}},
        {"query": "q", "sources": [5]},
        {"query": "q", "sources": ["ChatGPT"]},
        {"query": "q", "sources": [""]},
        {"query": "q", "sources": ["chatgpt"] * 33},
    ],
)
def test_a_trial_takes_a_query_and_at_most_an_answer_and_folders(body: Any) -> None:
    assert isinstance(server._trial_input(body), str)


def test_a_trial_reads_only_what_it_takes() -> None:
    assert server._trial_input(
        {"query": "q", "answer": None, "folders": FOLDERS, "peer_id": "x"}
    ) == {"query": "q", "answer": None, "folders": FOLDERS, "sources": None}
    assert server._trial_input({"query": "q", "answer": "a"}) == {
        "query": "q",
        "answer": "a",
        "folders": None,
        "sources": None,
    }
    assert server._trial_input(
        {"query": "q", "sources": ["chatgpt", "agy", "chatgpt"]}
    )["sources"] == ["chatgpt", "agy"]
    assert server._trial_input({"query": "q", "sources": ["x"] * 32})["sources"] == [
        "x"
    ]
    longest = [{"path": "/" + "a" * 4095, "subfolders": False}] * 500
    assert server._trial_input({"query": "q", "folders": longest})["folders"] == longest
    assert server._trial_input({"query": "q", "folders": []})["folders"] == []


def _route(monkeypatch: pytest.MonkeyPatch, *, read: bool) -> TestClient:
    monkeypatch.setenv("HONCHO_MCP_BEARER_TOKEN", "dashboard-token")
    monkeypatch.setattr(audit, "READ_ENABLED", read)
    module = importlib.reload(server)
    app = module.mcp.http_app(path=module.MCP_PATH)
    return TestClient(app, base_url="http://127.0.0.1:8765")


def test_the_route_is_the_dashboards_and_takes_the_bridge_token(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client = _route(monkeypatch, read=True)
    _jev(monkeypatch)
    calls = _honcho(monkeypatch, [_said("학습 실험")])
    recorded: list[dict[str, Any]] = []
    monkeypatch.setattr(audit, "record", lambda **row: recorded.append(row))
    body = {"query": QUERY, "folders": FOLDERS}

    with client:
        assert client.post("/guard-trial", json=body).status_code == 401
        wrong = client.post("/guard-trial", json=body, headers={"authorization": "Bearer x"})
        assert wrong.status_code == 401
        auth = {"authorization": "Bearer dashboard-token"}
        bad = client.post("/guard-trial", json={"query": " "}, headers=auth)
        assert (bad.status_code, bad.json()["error"]) == (400, "bad_request")
        broken = client.post(
            "/guard-trial", content=b"{", headers={**auth, "content-type": "application/json"}
        )
        assert broken.status_code == 400
        assert calls == []
        tried = client.post("/guard-trial", json=body, headers=auth)

    assert tried.status_code == 200
    assert tried.json()["outcome"] == "passed"
    assert tried.json()["found"] == [_found("학습 실험")]
    assert [call["path"].rsplit("/", 1)[-1] for call in calls] == ["list", "search"]
    assert recorded == [], "a trial is not a teammate's call"


def test_without_audit_read_there_is_no_trial(monkeypatch: pytest.MonkeyPatch) -> None:
    client = _route(monkeypatch, read=False)
    with client:
        response = client.post(
            "/guard-trial",
            json={"query": QUERY},
            headers={"authorization": "Bearer dashboard-token"},
        )
    assert response.status_code == 404
