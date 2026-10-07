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
    monkeypatch.setattr(jev_gate, "GUARD_URL", None)
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


def test_the_question_names_both_outcomes() -> None:
    assert set(jev_gate.CRITERIA) == {"true", "false"}


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
