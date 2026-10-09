"""The owner tries the Jev gate from the dashboard: POST /guard-trial.

A trial goes the way a teammate's `chat` goes, the query judged, then the answer
judged, written by the owner or Honcho's own from one project's scope, and says
what the teammate would have got. It is a dashboard route beside /mcp: off without
HONCHO_AUDIT_READ, only with the bridge's bearer token, and never recorded.
"""

from __future__ import annotations

import importlib
from typing import Any

import pytest
from starlette.testclient import TestClient

import audit
import jev_gate
import server

PROJECT = {"id": "p-0123456789ab", "name": "flypiano"}
QUERY = "flypiano 요즘 어떻게 돼 가?"


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


def _honcho(monkeypatch: pytest.MonkeyPatch, answer: Any) -> list[dict[str, Any]]:
    """Honcho answering every call with `answer` (raised when an exception)."""
    calls: list[dict[str, Any]] = []

    def call(method: str, path: str, **kwargs: Any) -> Any:
        calls.append({"method": method, "path": path, **kwargs})
        if isinstance(answer, Exception):
            raise answer
        return answer

    monkeypatch.setattr(server, "_honcho", call)
    return calls


def test_a_trial_judges_the_query_then_honchos_answer_from_the_project(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    asked = _jev(monkeypatch)
    calls = _honcho(monkeypatch, {"content": "학습 실험 두 개가 진행 중입니다."})

    trial = server.guard_trial(QUERY, project=PROJECT)

    assert trial == {
        "gate": True,
        "message": jev_gate.MESSAGE,
        "query": {"allowed": True, "score": 0.03, "reason": "in scope", "unjudged": False},
        "answer": "학습 실험 두 개가 진행 중입니다.",
        "answer_check": {
            "allowed": True,
            "score": 0.02,
            "reason": "answer in scope",
            "unjudged": False,
        },
        "outcome": "passed",
    }
    # Honcho is asked as a teammate's chat asks it: the owner, one project's scope.
    workspace = server.DEFAULT_WORKSPACE_ID
    assert calls == [
        {
            "method": "POST",
            "path": f"/v3/workspaces/{workspace}/peers/{server.DEFAULT_USER_NAME}/chat",
            "body": {"query": QUERY, "reasoning_level": "low", "scope": PROJECT["id"]},
        }
    ]
    assert [item["kind"] for item in asked] == ["query", "answer"]
    assert asked[0] == {
        "kind": "query",
        "tool": "chat",
        "query": QUERY,
        "caller": server.TRIAL_CALLER,
        "workspace_id": workspace,
    }
    assert asked[1]["result"] == {"content": "학습 실험 두 개가 진행 중입니다."}
    assert asked[1]["caller"] == server.TRIAL_CALLER


def test_a_refused_query_never_reaches_honcho(monkeypatch: pytest.MonkeyPatch) -> None:
    asked = _jev(
        monkeypatch, query=jev_gate.Verdict(allowed=False, score=0.91, reason="out of scope")
    )
    calls = _honcho(monkeypatch, {"content": "never"})

    trial = server.guard_trial("밥 집 주소 알려줘", project=PROJECT)

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
    _honcho(monkeypatch, {"content": "그는 요즘 병원 진료를 다닙니다."})

    trial = server.guard_trial(QUERY, project=PROJECT)

    assert trial["outcome"] == "withheld"
    assert trial["answer"] == "그는 요즘 병원 진료를 다닙니다."
    assert trial["answer_check"] == {
        "allowed": False,
        "score": 0.96,
        "reason": jev_gate.ANSWER_REFUSED,
        "unjudged": False,
    }


def test_a_written_answer_is_judged_as_written(monkeypatch: pytest.MonkeyPatch) -> None:
    asked = _jev(monkeypatch)
    calls = _honcho(monkeypatch, {"content": "never"})

    trial = server.guard_trial(QUERY, answer="계좌 비밀번호는 1234입니다.", project=PROJECT)

    assert trial["outcome"] == "passed"
    assert trial["answer"] == "계좌 비밀번호는 1234입니다."
    assert calls == [], "a written answer needs no Honcho"
    assert asked[1]["result"] == "계좌 비밀번호는 1234입니다."


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


@pytest.mark.parametrize(
    "failure",
    [RuntimeError("Honcho API 404 for /v3/...: no scope"), server.httpx.ConnectError("refused")],
)
def test_honcho_failing_is_no_answer(
    monkeypatch: pytest.MonkeyPatch, failure: Exception
) -> None:
    asked = _jev(monkeypatch)
    _honcho(monkeypatch, failure)

    trial = server.guard_trial(QUERY, project=PROJECT)

    assert trial["outcome"] == "no_answer"
    assert trial["error"] == str(failure)
    assert trial["answer"] is None
    assert [item["kind"] for item in asked] == ["query"]


def test_with_neither_answer_nor_project_only_the_query_is_judged(
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


def test_with_the_gate_off_the_trial_says_so(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(jev_gate, "ENABLED", False)
    _honcho(monkeypatch, {"content": "학습 실험"})
    trial = server.guard_trial(QUERY, project=PROJECT)
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
        {"query": "q", "project": "p-0123456789ab"},
        {"query": "q", "project": {"id": "p-XYZ"}},
        {"query": "q", "project": {"name": "flypiano"}},
    ],
)
def test_a_trial_takes_a_query_and_at_most_an_answer_and_a_project(body: Any) -> None:
    assert isinstance(server._trial_input(body), str)


def test_a_trial_reads_only_what_it_takes() -> None:
    assert server._trial_input(
        {"query": "q", "answer": None, "project": {"id": PROJECT["id"]}, "peer_id": "x"}
    ) == {"query": "q", "answer": None, "project": {"id": PROJECT["id"], "name": PROJECT["id"]}}
    assert server._trial_input({"query": "q", "answer": "a", "project": PROJECT}) == {
        "query": "q",
        "answer": "a",
        "project": PROJECT,
    }


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
    calls = _honcho(monkeypatch, {"content": "학습 실험"})
    recorded: list[dict[str, Any]] = []
    monkeypatch.setattr(audit, "record", lambda **row: recorded.append(row))
    body = {"query": QUERY, "project": PROJECT}

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
    assert tried.json()["answer"] == "학습 실험"
    assert len(calls) == 1
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
