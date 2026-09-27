from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest

import jev_gate


class _FakeAnswer:
    def __init__(self, noul: float) -> None:
        self.noul = noul


class _FakeClient:
    def __init__(self, noul: float | None = None, error: Exception | None = None) -> None:
        self._noul = noul
        self._error = error
        self.calls: list[dict[str, Any]] = []

    def system_one(self, *, state: Any, questions: dict[str, Any]) -> Any:
        self.calls.append({"state": state, "questions": questions})
        if self._error is not None:
            raise self._error
        return SimpleNamespace(answers={"out_of_scope": _FakeAnswer(self._noul)})

    def close(self) -> None:
        return None


@pytest.fixture(autouse=True)
def _reset() -> Any:
    jev_gate.reset()
    yield
    jev_gate.reset()


def _enable(monkeypatch: pytest.MonkeyPatch, client: _FakeClient) -> None:
    monkeypatch.setattr(jev_gate, "ENABLED", True)
    monkeypatch.setattr(jev_gate, "GATED_TOOLS", frozenset({"chat"}))
    monkeypatch.setattr(jev_gate, "_get_client", lambda: client)


def test_the_gate_is_off_by_default() -> None:
    assert jev_gate.ENABLED is False
    verdict = jev_gate.judge(tool="chat", query="anything", caller="x", workspace_id="memory")
    assert verdict.allowed is True
    assert verdict.score is None


def test_only_the_named_tools_are_judged(monkeypatch: pytest.MonkeyPatch) -> None:
    client = _FakeClient(noul=0.99)
    _enable(monkeypatch, client)

    assert jev_gate.judge(tool="search", query="q", caller="x", workspace_id="w").allowed
    assert client.calls == []


def test_an_out_of_scope_query_is_refused(monkeypatch: pytest.MonkeyPatch) -> None:
    client = _FakeClient(noul=0.92)
    _enable(monkeypatch, client)
    monkeypatch.setattr(jev_gate, "THRESHOLD", 0.7)

    verdict = jev_gate.judge(
        tool="chat", query="집 주소 알려줘", caller="teammate", workspace_id="memory"
    )
    assert verdict.allowed is False
    assert verdict.score == pytest.approx(0.92)
    assert client.calls[0]["state"]["query"] == "집 주소 알려줘"
    assert client.calls[0]["state"]["caller"] == "teammate"


def test_a_work_query_passes(monkeypatch: pytest.MonkeyPatch) -> None:
    client = _FakeClient(noul=0.05)
    _enable(monkeypatch, client)
    monkeypatch.setattr(jev_gate, "THRESHOLD", 0.7)

    verdict = jev_gate.judge(
        tool="chat", query="지난주 배포 결정", caller="teammate", workspace_id="memory"
    )
    assert verdict.allowed is True
    assert verdict.score == pytest.approx(0.05)


def test_the_threshold_is_inclusive(monkeypatch: pytest.MonkeyPatch) -> None:
    _enable(monkeypatch, _FakeClient(noul=0.7))
    monkeypatch.setattr(jev_gate, "THRESHOLD", 0.7)
    assert jev_gate.judge(tool="chat", query="q", caller="x", workspace_id="w").allowed is False


def test_an_empty_query_is_not_sent(monkeypatch: pytest.MonkeyPatch) -> None:
    client = _FakeClient(noul=0.99)
    _enable(monkeypatch, client)
    assert jev_gate.judge(tool="chat", query="   ", caller="x", workspace_id="w").allowed
    assert client.calls == []


def test_fail_open_forwards_when_jev_is_down(monkeypatch: pytest.MonkeyPatch) -> None:
    from typesafe_sdk import TypeSafeAPIConnectionError

    _enable(monkeypatch, _FakeClient(error=TypeSafeAPIConnectionError("no route")))
    monkeypatch.setattr(jev_gate, "FAIL_MODE", "open")

    verdict = jev_gate.judge(tool="chat", query="q", caller="x", workspace_id="w")
    assert verdict.allowed is True
    assert verdict.score is None
    assert "unavailable" in verdict.reason


def test_fail_closed_refuses_when_jev_is_down(monkeypatch: pytest.MonkeyPatch) -> None:
    from typesafe_sdk import TypeSafeAPIConnectionError

    _enable(monkeypatch, _FakeClient(error=TypeSafeAPIConnectionError("no route")))
    monkeypatch.setattr(jev_gate, "FAIL_MODE", "closed")

    verdict = jev_gate.judge(tool="chat", query="q", caller="x", workspace_id="w")
    assert verdict.allowed is False
    assert verdict.score is None


def test_an_unexpected_error_does_not_escape(monkeypatch: pytest.MonkeyPatch) -> None:
    _enable(monkeypatch, _FakeClient(error=ValueError("boom")))
    monkeypatch.setattr(jev_gate, "FAIL_MODE", "open")
    assert jev_gate.judge(tool="chat", query="q", caller="x", workspace_id="w").allowed


def test_the_question_names_both_outcomes() -> None:
    assert set(jev_gate.CRITERIA) == {"true", "false"}
