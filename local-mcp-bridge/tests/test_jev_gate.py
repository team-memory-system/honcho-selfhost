from __future__ import annotations

import json
import logging
import sys
from collections.abc import Callable
from types import SimpleNamespace
from typing import Any

import httpx
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
        # Jev answers every question it is asked, under the name it was asked by.
        return SimpleNamespace(
            answers={name: _FakeAnswer(self._noul) for name in questions},
            model="jev-1.13.0",
        )

    def close(self) -> None:
        return None


@pytest.fixture(autouse=True)
def _reset() -> Any:
    jev_gate.reset()
    yield
    jev_gate.reset()


def _enable(monkeypatch: pytest.MonkeyPatch, client: _FakeClient, workers: int = 1) -> None:
    monkeypatch.setattr(jev_gate, "ENABLED", True)
    monkeypatch.setattr(jev_gate, "GATED_TOOLS", frozenset({"chat"}))
    monkeypatch.setattr(jev_gate, "GUARD_URL", None)
    monkeypatch.setattr(jev_gate, "_get_client", lambda: client)
    # One at a time unless a test is about asking at once, so calls keep their order.
    monkeypatch.setattr(jev_gate, "ANSWER_WORKERS", workers)


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


def test_jev_is_asked_by_its_version_not_the_alias(monkeypatch: pytest.MonkeyPatch) -> None:
    """jev-latest moves when TypeSafe ships a new Jev; the threshold was measured on 1.13."""
    made: dict[str, Any] = {}

    class _Client:
        def __init__(self, **kwargs: Any) -> None:
            made.update(kwargs)

    monkeypatch.setitem(sys.modules, "typesafe_sdk", SimpleNamespace(TypeSafeClient=_Client))
    jev_gate._get_client()
    assert jev_gate.MODEL == "jev-1.13.0"
    assert made["model"] == "jev-1.13.0"


def test_the_version_that_answered_goes_with_the_verdict(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _enable(monkeypatch, _FakeClient(noul=0.1))
    verdict = jev_gate.judge(tool="chat", query="지난주 배포", caller="t", workspace_id="w")
    assert (verdict.allowed, verdict.model) == (True, "jev-1.13.0")


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
    assert verdict.failed is True


def test_fail_closed_refuses_when_jev_is_down(monkeypatch: pytest.MonkeyPatch) -> None:
    from typesafe_sdk import TypeSafeAPIConnectionError

    _enable(monkeypatch, _FakeClient(error=TypeSafeAPIConnectionError("no route")))
    monkeypatch.setattr(jev_gate, "FAIL_MODE", "closed")

    verdict = jev_gate.judge(tool="chat", query="q", caller="x", workspace_id="w")
    assert verdict.allowed is False
    assert verdict.score is None
    assert verdict.failed is True


def test_an_unexpected_error_does_not_escape(monkeypatch: pytest.MonkeyPatch) -> None:
    _enable(monkeypatch, _FakeClient(error=ValueError("boom")))
    monkeypatch.setattr(jev_gate, "FAIL_MODE", "open")
    assert jev_gate.judge(tool="chat", query="q", caller="x", workspace_id="w").allowed


def test_a_jev_error_reaches_the_reason_and_the_log_by_its_name_and_status_only(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    from typesafe_sdk import TypeSafeAPIConnectionError, TypeSafeAPIError

    caplog.set_level(logging.DEBUG)
    key = "ts_test_" + "k" * 32
    monkeypatch.setattr(jev_gate, "FAIL_MODE", "open")
    for error, reason in (
        (
            TypeSafeAPIConnectionError(f"Connection error: Bearer {key}"),
            "jev unavailable: TypeSafeAPIConnectionError",
        ),
        (
            TypeSafeAPIError(402, {"detail": f"no credits for {key}"}, {}),
            "jev unavailable: TypeSafeAPIError 402",
        ),
        (ValueError(f"something about {key}"), "jev unavailable: ValueError"),
    ):
        jev_gate.reset()
        _enable(monkeypatch, _FakeClient(error=error))
        verdict = jev_gate.judge(tool="chat", query="q", caller="x", workspace_id="w")
        assert verdict.reason == reason
        assert verdict.failed is True
    assert key not in caplog.text


def test_the_question_names_both_outcomes() -> None:
    assert set(jev_gate.CRITERIA) == {"true", "false"}
    assert set(jev_gate.ANSWER_CRITERIA) == {"true", "false"}


def test_a_query_longer_than_the_limit_is_refused_unjudged(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Padding a question past what Jev reads must not walk it past the gate."""
    client = _FakeClient(noul=0.01)
    _enable(monkeypatch, client)
    monkeypatch.setattr(jev_gate, "FAIL_MODE", "open")

    long = "배포 " * (jev_gate.TEXT_LIMIT // 3) + "그리고 집 주소"
    verdict = jev_gate.judge(tool="chat", query=long, caller="x", workspace_id="w")

    assert len(long) > jev_gate.TEXT_LIMIT
    assert verdict.allowed is False
    assert verdict.reason == jev_gate.QUERY_TOO_LONG
    assert client.calls == []
    at_limit = "가" * jev_gate.TEXT_LIMIT
    assert jev_gate.judge(tool="chat", query=at_limit, caller="x", workspace_id="w").allowed


# --------------------------------------------------------------------- answers


def _check(result: Any, query: str = "지난주 배포 어땠어?") -> Any:
    return jev_gate.judge_answer(
        tool="chat", query=query, result=result, caller="teammate", workspace_id="memory"
    )


def test_an_answer_is_judged_with_its_own_question(monkeypatch: pytest.MonkeyPatch) -> None:
    client = _FakeClient(noul=0.95)
    _enable(monkeypatch, client)
    monkeypatch.setattr(jev_gate, "THRESHOLD", 0.7)

    verdict = _check({"content": "그날은 병원 진료 때문에 쉬었습니다.", "evidence": None})

    assert verdict.allowed is False
    assert verdict.score == pytest.approx(0.95)
    assert verdict.reason == jev_gate.ANSWER_REFUSED
    (call,) = client.calls
    assert list(call["questions"]) == ["sensitive_answer"]
    assert call["questions"]["sensitive_answer"].instructions == jev_gate.ANSWER_QUESTION
    assert call["state"]["answer"] == "그날은 병원 진료 때문에 쉬었습니다."
    assert call["state"]["query"] == "지난주 배포 어땠어?"


def test_a_work_answer_passes(monkeypatch: pytest.MonkeyPatch) -> None:
    _enable(monkeypatch, _FakeClient(noul=0.03))
    verdict = _check({"content": "배포는 금요일에 했습니다."})
    assert verdict.allowed is True
    assert verdict.score == pytest.approx(0.03)
    assert verdict.reason == "answer in scope"
    assert verdict.unjudged is False


def test_a_number_that_picks_out_one_person_is_refused_before_jev_is_asked(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # A bare number scored about 0.5 with Jev and went out; its form decides now.
    # A teammate's answer has it hidden before this (redact.py); an answer that
    # still holds one is withheld.
    client = _FakeClient(noul=0.01)
    _enable(monkeypatch, client)

    for text, held in [
        ("900101-1234567", "resident registration number"),
        ("850315-5123456", "foreign registration number"),
        ("여권 M12345678", "passport number"),
        ("11-23-456789-01", "driver's license number"),
        ("결제는 4111 1111 1111 1111로", "card number"),
    ]:
        asked = jev_gate.judge(tool="chat", query=text, caller="x", workspace_id="w")
        assert (asked.allowed, asked.score, asked.reason) == (
            False,
            None,
            jev_gate.QUERY_HOLDS + held,
        ), text
        assert asked.unjudged is False
        verdict = _check({"messages": [{"content": "배포 메모"}, {"content": text}]})
        assert (verdict.allowed, verdict.reason) == (False, jev_gate.ANSWER_HOLDS + held), text
    assert client.calls == []


def test_numbers_of_other_forms_go_on_to_jev(monkeypatch: pytest.MonkeyPatch) -> None:
    client = _FakeClient(noul=0.01)
    _enable(monkeypatch, client)

    for text in [
        "010-1234-5678",  # a phone number: Jev judges it by what is around it
        "minsu@example.com",
        "1760432400000",  # a time in milliseconds: month 60
        "4111 1111 1111 1112",  # the check digit is wrong
        "2026-10-14 12:34:56, PR #1234, build 20261014",
    ]:
        assert jev_gate.judge(tool="chat", query=text, caller="x", workspace_id="w").allowed
        assert _check({"content": text}).allowed is True, text
    assert len(client.calls) == 10


def test_every_piece_of_text_in_a_result_is_read() -> None:
    result = {
        "content": "학습 실험",
        "evidence": {
            "messages": [
                {"session": "flypiano", "text": "실험 두 개"},
                {"session": "cmux", "text": None},
            ],
            "count": 2,
        },
        "ok": True,
    }
    assert jev_gate.text_of(result) == "학습 실험\nflypiano\n실험 두 개\ncmux"
    assert jev_gate.text_of("그대로") == "그대로"
    assert jev_gate.text_of(None) == ""


def test_an_answer_with_no_text_is_not_sent(monkeypatch: pytest.MonkeyPatch) -> None:
    client = _FakeClient(noul=0.99)
    _enable(monkeypatch, client)
    assert _check({"content": "  ", "evidence": None}).allowed is True
    assert _check(None).allowed is True
    assert client.calls == []


def test_answers_are_judged_only_for_the_named_tools(monkeypatch: pytest.MonkeyPatch) -> None:
    client = _FakeClient(noul=0.99)
    _enable(monkeypatch, client)
    verdict = jev_gate.judge_answer(
        tool="search", query="q", result={"content": "x"}, caller="x", workspace_id="w"
    )
    assert verdict.allowed is True
    monkeypatch.setattr(jev_gate, "ENABLED", False)
    assert _check({"content": "x"}).allowed is True
    assert client.calls == []


def test_a_long_answer_is_judged_in_overlapping_pieces(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client = _FakeClient(noul=0.04)
    _enable(monkeypatch, client)
    answer = "가" * (jev_gate.TEXT_LIMIT * 2)

    assert _check({"content": answer}).allowed is True

    pieces = [call["state"]["answer"] for call in client.calls]
    assert len(pieces) == 3
    assert all(len(piece) <= jev_gate.TEXT_LIMIT for piece in pieces)
    step = jev_gate.TEXT_LIMIT - jev_gate.ANSWER_OVERLAP
    assert "".join(piece[:step] for piece in pieces[:-1]) + pieces[-1] == answer


def test_a_blank_piece_of_a_long_answer_is_not_sent(monkeypatch: pytest.MonkeyPatch) -> None:
    client = _FakeClient(noul=0.03)
    _enable(monkeypatch, client)
    answer = "배포 결과 정리" + " " * (jev_gate.TEXT_LIMIT * 2)

    assert _check({"content": answer}).allowed is True

    assert len(jev_gate._pieces(answer)) == 3
    assert [call["state"]["answer"].strip() for call in client.calls] == ["배포 결과 정리"]


class _Scoring(_FakeClient):
    """Jev scoring each answer by the text it holds: `scores` maps a snippet to a score."""

    def __init__(self, scores: dict[str, float], default: float = 0.02) -> None:
        super().__init__()
        self._scores = scores
        self._default = default

    def system_one(self, *, state: Any, questions: dict[str, Any]) -> Any:
        self.calls.append({"state": state, "questions": questions})
        score = next(
            (score for snippet, score in self._scores.items() if snippet in state["answer"]),
            self._default,
        )
        return SimpleNamespace(answers={name: _FakeAnswer(score) for name in questions})


def test_one_refused_piece_withholds_the_whole_answer(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client = _Scoring({"연봉": 0.96})
    _enable(monkeypatch, client)
    step = jev_gate.TEXT_LIMIT - jev_gate.ANSWER_OVERLAP
    answer = "업무 " * (step // 2) + "연봉은 7천" + " 업무" * (step // 3)

    verdict = _check({"content": answer})

    assert verdict.allowed is False
    assert verdict.score == pytest.approx(0.96)
    assert len(client.calls) == len(jev_gate._pieces(answer)) == 3, "every piece is judged"


# --------------------------------------------------- an answer, item by item


def _search_result() -> list[dict[str, Any]]:
    return [
        {"conversation_id": "s1", "speaker": "chen", "content": "결제 모듈 배포는 금요일"},
        {"conversation_id": "s2", "speaker": "chen", "content": "내 연봉은 5,400이야"},
        {"conversation_id": "s3", "speaker": "codex", "content": "스프린트 리뷰는 수요일"},
    ]


def test_a_result_is_judged_message_by_message(monkeypatch: pytest.MonkeyPatch) -> None:
    client = _FakeClient(noul=0.04)
    _enable(monkeypatch, client)

    verdict = _check(_search_result())

    assert verdict.allowed is True
    assert verdict.score == pytest.approx(0.04)
    assert [call["state"]["answer"] for call in client.calls] == [
        jev_gate.text_of(message) for message in _search_result()
    ]
    assert all(call["state"]["query"] == "지난주 배포 어땠어?" for call in client.calls)


def test_a_private_message_among_work_withholds_the_answer_with_its_score(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Judged whole, the salary thinned out among work messages and passed."""
    client = _Scoring({"연봉": 0.91, "스프린트": 0.3})
    _enable(monkeypatch, client)

    verdict = _check(_search_result())

    assert verdict.allowed is False
    assert verdict.score == pytest.approx(0.91)
    assert verdict.reason == jev_gate.ANSWER_REFUSED
    assert len(client.calls) == 3, "every message is judged, in one round"


def test_a_dict_with_one_list_is_judged_by_its_items_and_then_the_rest(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client = _FakeClient(noul=0.05)
    _enable(monkeypatch, client)

    read = {
        "folder": "agrune",
        "app": "codex",
        "total": 2,
        "newest_first": False,
        "messages": [{"content": "배포 메모"}, {"content": "회의 일정"}],
    }
    assert _check(read).allowed is True
    assert [call["state"]["answer"] for call in client.calls] == [
        "배포 메모",
        "회의 일정",
        "agrune\ncodex",
    ]

    client.calls.clear()
    listed = {"items": [{"id": "s1", "cwd": "~"}, {"id": "s2", "cwd": "agrune"}], "total": 2}
    assert _check(listed).allowed is True
    assert [call["state"]["answer"] for call in client.calls] == ["s1\n~", "s2\nagrune"]

    assert jev_gate.units_of("그대로") == ["그대로"]
    assert jev_gate.units_of({"a": [1], "b": [2]}) == [{"a": [1], "b": [2]}]
    assert jev_gate.units_of({"content": "x", "evidence": {"messages": ["y"]}}) == [
        {"content": "x", "evidence": {"messages": ["y"]}}
    ]


def test_a_long_list_is_judged_in_groups_of_neighbours(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client = _FakeClient(noul=0.01)
    _enable(monkeypatch, client)
    monkeypatch.setattr(jev_gate, "ANSWER_UNITS", 4)
    messages = [{"content": f"메시지 {index}"} for index in range(10)]

    assert _check(messages).allowed is True

    asked = [call["state"]["answer"] for call in client.calls]
    assert asked == [
        "메시지 0\n메시지 1\n메시지 2",
        "메시지 3\n메시지 4\n메시지 5",
        "메시지 6\n메시지 7\n메시지 8",
        "메시지 9",
    ]


def test_messages_are_asked_at_once(monkeypatch: pytest.MonkeyPatch) -> None:
    import time

    class Slow(_FakeClient):
        def system_one(self, *, state: Any, questions: dict[str, Any]) -> Any:
            time.sleep(0.15)
            return super().system_one(state=state, questions=questions)

    client = Slow(noul=0.02)
    _enable(monkeypatch, client, workers=8)
    messages = [{"content": f"메시지 {index}"} for index in range(8)]

    started = time.monotonic()
    verdict = _check(messages)
    elapsed = time.monotonic() - started

    assert verdict.allowed is True
    assert len(client.calls) == 8
    assert elapsed < 0.15 * 4, f"eight messages took {elapsed:.2f}s one after another"


def test_a_message_jev_cannot_judge_withholds_the_answer(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from typesafe_sdk import TypeSafeAPIConnectionError

    class Flaky(_FakeClient):
        def system_one(self, *, state: Any, questions: dict[str, Any]) -> Any:
            if "연봉" in state["answer"]:
                self.calls.append({"state": state, "questions": questions})
                raise TypeSafeAPIConnectionError("no route")
            return super().system_one(state=state, questions=questions)

    client = Flaky(noul=0.02)
    _enable(monkeypatch, client)
    assert jev_gate.ANSWER_FAIL_MODE == "closed"

    verdict = _check(_search_result())

    assert verdict.allowed is False
    assert verdict.failed is True
    assert len(client.calls) == 3

    monkeypatch.setattr(jev_gate, "ANSWER_FAIL_MODE", "open")
    let_through = _check(_search_result())
    assert let_through.allowed is True
    assert let_through.unjudged is True


def test_an_answer_too_long_to_judge_is_withheld(monkeypatch: pytest.MonkeyPatch) -> None:
    client = _FakeClient(noul=0.01)
    _enable(monkeypatch, client)
    answer = "가" * (jev_gate.TEXT_LIMIT * (jev_gate.ANSWER_PIECES + 1))

    verdict = _check({"content": answer})

    assert verdict.allowed is False
    assert verdict.reason == jev_gate.ANSWER_TOO_LONG
    assert client.calls == []


def test_an_answer_jev_cannot_judge_is_withheld_by_default(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from typesafe_sdk import TypeSafeAPIConnectionError

    _enable(monkeypatch, _FakeClient(error=TypeSafeAPIConnectionError("no route")))
    # The query side forwards on a failure; the answer side has its own mode.
    monkeypatch.setattr(jev_gate, "FAIL_MODE", "open")
    assert jev_gate.ANSWER_FAIL_MODE == "closed"

    verdict = _check({"content": "배포는 금요일"})

    assert verdict.allowed is False
    assert verdict.failed is True
    assert verdict.reason.startswith("jev unavailable for the answer: ")

    monkeypatch.setattr(jev_gate, "ANSWER_FAIL_MODE", "open")
    let_through = _check({"content": "배포는 금요일"})
    assert let_through.allowed is True
    assert let_through.unjudged is True


def test_an_unreadable_result_is_a_failure_not_a_crash(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _enable(monkeypatch, _FakeClient(noul=0.01))

    class Unprintable:
        def __str__(self) -> str:
            raise ValueError("no text")

    verdict = _check({"content": Unprintable()})
    assert verdict.allowed is False
    assert verdict.reason == "jev unavailable for the answer: unreadable result: ValueError"


# ------------------------------------------------------------- team hub guard

HUB_URL = "https://hub.test/v1/jev/guard"
# A stand-in, not a real token. Lowercase so a hub echoing it as an error code
# would pass the code filter and has to be caught by the scrub.
FAKE_TOKEN = "tm_fake_guard_token_0000"

Handler = Callable[[httpx.Request], httpx.Response]


def _answer(**fields: Any) -> Handler:
    return lambda request: httpx.Response(200, json=fields)


def _refusal(status: int, code: str) -> Handler:
    return lambda request: httpx.Response(
        status, json={"error": code, "detail": "said by the hub"}
    )


def _raise(exc: Exception) -> Handler:
    def handler(request: httpx.Request) -> httpx.Response:
        raise exc

    return handler


def _hub(
    monkeypatch: pytest.MonkeyPatch,
    handler: Handler,
    *,
    token: str | None = FAKE_TOKEN,
    fail_mode: str = "open",
) -> list[httpx.Request]:
    """Gate on in hub mode, the hub played by `handler`, the SDK out of reach."""
    seen: list[httpx.Request] = []

    def record(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return handler(request)

    def no_sdk() -> Any:
        pytest.fail("The SDK client was used in hub mode")

    monkeypatch.setattr(jev_gate, "ENABLED", True)
    monkeypatch.setattr(jev_gate, "GATED_TOOLS", frozenset({"chat"}))
    monkeypatch.setattr(jev_gate, "GUARD_URL", HUB_URL)
    monkeypatch.setattr(jev_gate, "GUARD_TOKEN", token)
    monkeypatch.setattr(jev_gate, "FAIL_MODE", fail_mode)
    monkeypatch.setattr(
        jev_gate,
        "_open_guard_client",
        lambda: httpx.Client(transport=httpx.MockTransport(record)),
    )
    monkeypatch.setattr(jev_gate, "_get_client", no_sdk)
    # Any `import typesafe_sdk` now raises ImportError.
    monkeypatch.setitem(sys.modules, "typesafe_sdk", None)
    return seen


def _ask(query: str = "지난주 배포 결정", workspace_id: str | None = "memory") -> Any:
    return jev_gate.judge(
        tool="chat", query=query, caller="teammate", workspace_id=workspace_id
    )


def test_the_hub_gets_the_query_with_the_bearer_token(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    seen = _hub(
        monkeypatch, _answer(judged=True, allowed=True, score=0.1, threshold=0.7)
    )

    _ask()

    assert len(seen) == 1
    request = seen[0]
    assert request.method == "POST"
    assert str(request.url) == HUB_URL
    assert request.headers["authorization"] == f"Bearer {FAKE_TOKEN}"
    assert request.headers["content-type"] == "application/json"
    assert json.loads(request.content) == {
        "tool": "chat",
        "caller": "teammate",
        "workspace": "memory",
        "query": "지난주 배포 결정",
    }


def test_the_hub_gets_an_empty_workspace_when_there_is_none(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    seen = _hub(
        monkeypatch, _answer(judged=True, allowed=True, score=0.1, threshold=0.7)
    )

    _ask(workspace_id=None)

    assert json.loads(seen[0].content)["workspace"] == ""


def test_the_hub_allowing_a_query_lets_it_through(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _hub(monkeypatch, _answer(judged=True, allowed=True, score=0.12, threshold=0.7))
    # The hub applies its own threshold; the local one does not apply.
    monkeypatch.setattr(jev_gate, "THRESHOLD", 0.05)

    verdict = _ask()

    assert verdict.allowed is True
    assert verdict.score == pytest.approx(0.12)
    assert verdict.reason == "in scope"
    assert verdict.failed is False


def test_the_version_the_hub_names_goes_with_the_verdict(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _hub(
        monkeypatch,
        _answer(judged=True, allowed=True, score=0.1, threshold=0.7, model="jev-1.13.0"),
    )
    assert _ask().model == "jev-1.13.0"
    jev_gate.reset()
    # A hub from before it named the version: the verdict stands, with no version.
    _hub(monkeypatch, _answer(judged=True, allowed=True, score=0.1, threshold=0.7))
    verdict = _ask()
    assert (verdict.allowed, verdict.model) == (True, None)


def test_the_hub_refusing_a_query_refuses_it(monkeypatch: pytest.MonkeyPatch) -> None:
    _hub(monkeypatch, _answer(judged=True, allowed=False, score=0.3, threshold=0.7))
    monkeypatch.setattr(jev_gate, "THRESHOLD", 0.99)

    verdict = _ask("집 주소 알려줘")

    assert verdict.allowed is False
    assert verdict.score == pytest.approx(0.3)
    assert verdict.reason == "out of scope"
    assert verdict.failed is False


@pytest.mark.parametrize("fail_mode", ["open", "closed"])
def test_a_team_without_a_jev_key_lets_the_call_through_unjudged(
    monkeypatch: pytest.MonkeyPatch, fail_mode: str
) -> None:
    _hub(
        monkeypatch,
        _answer(judged=False, allowed=True, score=None, reason="no_key"),
        fail_mode=fail_mode,
    )

    verdict = _ask()

    assert verdict.allowed is True
    assert verdict.score is None
    assert verdict.failed is False
    assert verdict.no_key is True
    assert verdict.reason == "not judged: the team hub has no Jev key"


FAILURES = [
    pytest.param(
        _refusal(401, "bad_token"), "team guard: HTTP 401 bad_token", id="401"
    ),
    pytest.param(
        _refusal(403, "not_member"), "team guard: HTTP 403 not_member", id="403"
    ),
    pytest.param(
        _refusal(429, "rate_limited"), "team guard: HTTP 429 rate_limited", id="429"
    ),
    pytest.param(
        _refusal(502, "jev_failed"), "team guard: HTTP 502 jev_failed", id="502"
    ),
    pytest.param(
        lambda request: httpx.Response(500, text="Internal Server Error"),
        "team guard: HTTP 500",
        id="500-text",
    ),
    pytest.param(
        _refusal(400, "Not A Code: anything at all"),
        "team guard: HTTP 400",
        id="400-free-text-code",
    ),
    pytest.param(
        lambda request: httpx.Response(
            307, headers={"location": "https://elsewhere.test/"}
        ),
        "team guard: HTTP 307",
        id="redirect-not-followed",
    ),
    pytest.param(
        lambda request: httpx.Response(200, text="<html>hub</html>"),
        "team guard: HTTP 200 with a body that is not JSON",
        id="non-json",
    ),
    pytest.param(
        lambda request: httpx.Response(200, json=[True]),
        "team guard: HTTP 200 with an answer that is not an object",
        id="not-an-object",
    ),
    pytest.param(
        _answer(judged=True, score=0.1, threshold=0.7),
        "team guard: HTTP 200 with a malformed answer",
        id="missing-allowed",
    ),
    pytest.param(
        _answer(judged=True, allowed="yes", score=0.1, threshold=0.7),
        "team guard: HTTP 200 with a malformed answer",
        id="allowed-not-bool",
    ),
    pytest.param(
        _answer(judged=True, allowed=True, threshold=0.7),
        "team guard: HTTP 200 with a malformed answer",
        id="missing-score",
    ),
    pytest.param(
        _answer(judged=True, allowed=True, score="0.1", threshold=0.7),
        "team guard: HTTP 200 with a malformed answer",
        id="score-not-number",
    ),
    pytest.param(
        _answer(judged=True, allowed=True, score=True, threshold=0.7),
        "team guard: HTTP 200 with a malformed answer",
        id="score-bool",
    ),
    pytest.param(
        _answer(allowed=True, score=0.1, threshold=0.7),
        "team guard: HTTP 200 with a malformed answer",
        id="missing-judged",
    ),
    pytest.param(
        _answer(judged=False, allowed=True, score=None, reason="something_else"),
        "team guard: HTTP 200 with a malformed answer",
        id="unjudged-other-reason",
    ),
    pytest.param(
        _answer(judged=False, allowed=False, score=None, reason="no_key"),
        "team guard: HTTP 200 with a malformed answer",
        id="unjudged-refusal",
    ),
    pytest.param(
        _raise(httpx.ConnectError("connection refused")),
        "team guard: ConnectError",
        id="network",
    ),
    pytest.param(
        _raise(httpx.ConnectTimeout("timed out")),
        "team guard: ConnectTimeout",
        id="connect-timeout",
    ),
    pytest.param(
        _raise(httpx.ReadTimeout("timed out")),
        "team guard: ReadTimeout",
        id="read-timeout",
    ),
]


@pytest.mark.parametrize(("handler", "detail"), FAILURES)
def test_a_hub_failure_fails_open(
    monkeypatch: pytest.MonkeyPatch, handler: Handler, detail: str
) -> None:
    _hub(monkeypatch, handler, fail_mode="open")

    verdict = _ask()

    assert verdict.allowed is True
    assert verdict.score is None
    assert verdict.failed is True
    assert verdict.reason == f"jev unavailable: {detail}"


@pytest.mark.parametrize(("handler", "detail"), FAILURES)
def test_a_hub_failure_fails_closed(
    monkeypatch: pytest.MonkeyPatch, handler: Handler, detail: str
) -> None:
    _hub(monkeypatch, handler, fail_mode="closed")

    verdict = _ask()

    assert verdict.allowed is False
    assert verdict.score is None
    assert verdict.failed is True
    assert verdict.reason == f"jev unavailable: {detail}"


@pytest.mark.parametrize("fail_mode", ["open", "closed"])
def test_a_hub_url_without_a_token_is_a_failure(
    monkeypatch: pytest.MonkeyPatch, fail_mode: str
) -> None:
    seen = _hub(
        monkeypatch,
        _answer(judged=True, allowed=True, score=0.1, threshold=0.7),
        token=None,
        fail_mode=fail_mode,
    )

    verdict = _ask()

    assert seen == []
    assert verdict.allowed is (fail_mode == "open")
    assert verdict.failed is True
    assert verdict.reason == "jev unavailable: team guard: no HONCHO_JEV_GUARD_TOKEN"


@pytest.mark.parametrize(
    "handler",
    [
        pytest.param(
            _raise(
                httpx.LocalProtocolError(f"Illegal header value b'Bearer {FAKE_TOKEN}'")
            ),
            id="exception-quoting-the-header",
        ),
        pytest.param(
            _raise(ValueError(f"something about Bearer {FAKE_TOKEN}")),
            id="unexpected-exception",
        ),
        pytest.param(_refusal(401, FAKE_TOKEN), id="token-echoed-as-code"),
        pytest.param(
            lambda request: httpx.Response(
                401, json={"error": "bad_token", "detail": f"token {FAKE_TOKEN}"}
            ),
            id="token-echoed-in-detail",
        ),
        pytest.param(
            lambda request: httpx.Response(200, text=f"Bearer {FAKE_TOKEN}"),
            id="token-echoed-in-a-non-json-body",
        ),
    ],
)
def test_the_token_never_reaches_a_reason_or_a_log(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture, handler: Handler
) -> None:
    caplog.set_level(logging.DEBUG)
    _hub(monkeypatch, handler)

    verdict = _ask()

    assert verdict.failed is True
    assert FAKE_TOKEN not in verdict.reason
    assert caplog.records, "the failure should be logged"
    assert FAKE_TOKEN not in caplog.text


def test_the_hub_client_is_made_once_and_closed_by_reset(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _hub(monkeypatch, _answer(judged=True, allowed=True, score=0.1, threshold=0.7))
    opened: list[httpx.Client] = []
    make = jev_gate._open_guard_client

    def counting() -> httpx.Client:
        opened.append(make())
        return opened[-1]

    monkeypatch.setattr(jev_gate, "_open_guard_client", counting)

    _ask()
    _ask()
    assert len(opened) == 1

    jev_gate.reset()
    assert opened[0].is_closed


def test_the_hub_client_uses_the_jev_timeout(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(jev_gate, "TIMEOUT_SECONDS", 3.5)
    client = jev_gate._open_guard_client()
    try:
        assert client.timeout == httpx.Timeout(3.5)
    finally:
        client.close()


def test_the_hub_judges_the_answer_with_the_query(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    seen = _hub(
        monkeypatch,
        _answer(judged=True, allowed=False, score=0.97, threshold=0.7, checked="answer"),
    )

    verdict = _check({"content": "연봉은 7천입니다."}, query="지난주 배포")

    assert json.loads(seen[0].content) == {
        "tool": "chat",
        "caller": "teammate",
        "workspace": "memory",
        "query": "지난주 배포",
        "answer": "연봉은 7천입니다.",
    }
    assert verdict.allowed is False
    assert verdict.score == pytest.approx(0.97)
    assert verdict.reason == jev_gate.ANSWER_REFUSED


def test_the_hub_passing_an_answer_lets_it_out(monkeypatch: pytest.MonkeyPatch) -> None:
    _hub(
        monkeypatch,
        _answer(judged=True, allowed=True, score=0.03, threshold=0.7, checked="answer"),
    )
    verdict = _check({"content": "배포는 금요일"})
    assert verdict.allowed is True
    assert verdict.score == pytest.approx(0.03)
    assert verdict.unjudged is False


def test_a_hub_that_does_not_judge_answers_lets_them_out_unjudged(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A hub from before answers were judged reads the query alone, so its score
    is not taken for the answer's."""
    _hub(monkeypatch, _answer(judged=True, allowed=False, score=0.99, threshold=0.7))

    verdict = _check({"content": "배포는 금요일"})

    assert verdict.allowed is True
    assert verdict.score is None
    assert verdict.skipped is True
    assert verdict.reason == jev_gate.ANSWER_SKIPPED


def test_a_team_without_a_jev_key_lets_answers_out_unjudged(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _hub(monkeypatch, _answer(judged=False, allowed=True, score=None, reason="no_key"))
    verdict = _check({"content": "배포는 금요일"})
    assert verdict.allowed is True
    assert verdict.no_key is True


@pytest.mark.parametrize(("handler", "detail"), FAILURES)
def test_a_hub_failure_on_an_answer_withholds_it(
    monkeypatch: pytest.MonkeyPatch, handler: Handler, detail: str
) -> None:
    _hub(monkeypatch, handler, fail_mode="open")

    verdict = _check({"content": "배포는 금요일"})

    assert verdict.allowed is False
    assert verdict.failed is True
    assert verdict.reason == f"jev unavailable for the answer: {detail}"


def test_a_long_query_never_reaches_the_hub(monkeypatch: pytest.MonkeyPatch) -> None:
    seen = _hub(
        monkeypatch, _answer(judged=True, allowed=True, score=0.01, threshold=0.7)
    )
    verdict = _ask("가" * (jev_gate.TEXT_LIMIT + 1))
    assert verdict.allowed is False
    assert verdict.reason == jev_gate.QUERY_TOO_LONG
    assert seen == []


def test_the_hub_is_not_asked_when_the_gate_is_off_or_the_query_empty(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    seen = _hub(
        monkeypatch, _answer(judged=True, allowed=False, score=0.9, threshold=0.7)
    )

    assert _ask("   ").allowed is True
    assert jev_gate.judge(
        tool="search", query="q", caller="x", workspace_id="w"
    ).allowed
    monkeypatch.setattr(jev_gate, "ENABLED", False)
    assert _ask().allowed is True
    assert seen == []
