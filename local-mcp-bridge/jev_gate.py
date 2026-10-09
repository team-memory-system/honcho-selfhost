"""Jev judgment gate for the Honcho MCP bridge.

Teammates reach this memory through one tool (`chat`). The gate reads the query
before it is forwarded and refuses the ones that are not asking about shared work.
Then it reads the answer before it leaves, because a harmless question can still
draw private matters out of the memory: an answer that discloses them is withheld
behind the same refusal, so the refusal says nothing about what the memory holds.

It asks one of two backends:

- the Team Memory hub's guard, when `HONCHO_JEV_GUARD_URL` is set. The bridge
  posts the query there with `HONCHO_JEV_GUARD_TOKEN` and follows the hub's
  verdict; the hub holds the team's Jev key and applies its own threshold, so
  `HONCHO_JEV_THRESHOLD` does not apply. A team without a Jev key gets the call
  through unjudged, which is not a failure.
- Jev itself, through the official `typesafe_sdk` client, otherwise.

Off unless `HONCHO_JEV_GATE` is set, so a bridge without a Jev key behaves exactly
as it did before. `typesafe_sdk` is imported on first use, not at module load, and
never in hub mode.
"""

from __future__ import annotations

import logging
import math
import os
import re
import threading
from dataclasses import dataclass
from typing import Any

import httpx

logger = logging.getLogger("honcho.mcp.jev")


def _flag(name: str, *, default: bool = False) -> bool:
    value = os.environ.get(name)
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


ENABLED = _flag("HONCHO_JEV_GATE")
MODEL = os.environ.get("HONCHO_JEV_MODEL", "").strip() or None
TIMEOUT_SECONDS = float(os.environ.get("HONCHO_JEV_TIMEOUT", "10"))

#: Refuse at or above this probability that the query is out of scope.
THRESHOLD = float(os.environ.get("HONCHO_JEV_THRESHOLD", "0.7"))

#: What to do when Jev itself fails. "open" forwards the call, "closed" refuses it.
FAIL_MODE = (os.environ.get("HONCHO_JEV_FAIL_MODE", "open").strip().lower() or "open")

#: The same for the answer. Closed unless set: an answer is the memory itself, and
#: it is the last check before a teammate reads it.
ANSWER_FAIL_MODE = (
    os.environ.get("HONCHO_JEV_ANSWER_FAIL_MODE", "closed").strip().lower() or "closed"
)

#: The longest query or answer judged in one piece, in characters; the team hub
#: refuses longer ones. A longer query is refused unjudged, since padding a
#: question past the limit must not walk it past the gate. A longer answer is
#: judged in overlapping pieces, at most ANSWER_PIECES of them.
TEXT_LIMIT = 16_000
ANSWER_OVERLAP = 400
ANSWER_PIECES = 8

#: The team hub's guard endpoint. Set, the gate asks the hub instead of Jev.
GUARD_URL = os.environ.get("HONCHO_JEV_GUARD_URL", "").strip() or None

#: This member's bearer token for the hub. Never logged, never in a reason.
GUARD_TOKEN = os.environ.get("HONCHO_JEV_GUARD_TOKEN", "").strip() or None

#: Tools the gate judges. Everything else passes untouched.
GATED_TOOLS = frozenset(
    name.strip()
    for name in os.environ.get("HONCHO_JEV_TOOLS", "chat").split(",")
    if name.strip()
)

QUESTION = os.environ.get(
    "HONCHO_JEV_QUESTION",
    "Is this query asking for private personal life, credentials, financial or health "
    "details about the memory owner, rather than shared work context "
    "(projects, code, decisions, schedules, documents)?",
)

CRITERIA: dict[str, str] = {
    "true": "The query targets private personal matters, secrets, or credentials.",
    "false": "The query is about work the team shares, or is general and harmless.",
}

ANSWER_QUESTION = os.environ.get(
    "HONCHO_JEV_ANSWER_QUESTION",
    "A teammate asked the memory owner's work memory a question and will read this "
    "answer. Does the answer disclose private personal life, credentials or secrets, "
    "financial or health details about the owner or any person, rather than shared "
    "work context (projects, code, decisions, schedules, documents)?",
)

ANSWER_CRITERIA: dict[str, str] = {
    "true": "The answer discloses private personal matters, secrets, credentials, "
    "financial or health details.",
    "false": "The answer only covers shared work context, or is general and harmless.",
}

MESSAGE = os.environ.get(
    "HONCHO_JEV_MESSAGE",
    "This query was refused: it asks for information outside the shared work scope "
    "of this memory. Ask about projects, code, decisions or documents instead.",
)

#: The hub's code for a team that has no Jev key.
NO_KEY = "no_key"

#: Reasons the audit log keeps, which the owner's screens put into words.
QUERY_TOO_LONG = "query too long to judge"
ANSWER_TOO_LONG = "answer too long to judge"
ANSWER_REFUSED = "answer withheld: it discloses private matters"
ANSWER_SKIPPED = "answer not judged: the team hub does not judge answers"

# Error codes the hub sends are copied into reasons; anything else is dropped.
_ERROR_CODE = re.compile(r"[a-z0-9_]{1,64}")

_lock = threading.Lock()
_client: Any = None
_guard_client: httpx.Client | None = None


@dataclass(frozen=True)
class Verdict:
    """Outcome of one judgment. `score` is None when Jev was not consulted.

    `failed` is True when Jev (or the hub's guard) was asked and gave no answer,
    whichever way the fail mode then decided. `no_key` is True when the hub let the
    call through without asking Jev because the team has no Jev key. `skipped` is
    True when an answer went out unjudged because the team hub does not judge
    answers yet.
    """

    allowed: bool
    score: float | None
    reason: str
    failed: bool = False
    no_key: bool = False
    skipped: bool = False

    @property
    def unjudged(self) -> bool:
        """Let through without a judgment the owner should know about."""
        return self.failed or self.no_key or self.skipped


def enabled_for(tool: str) -> bool:
    return ENABLED and tool in GATED_TOOLS


def _get_client() -> Any:
    global _client
    with _lock:
        if _client is None:
            from typesafe_sdk import TypeSafeClient

            kwargs: dict[str, Any] = {"timeout": TIMEOUT_SECONDS}
            if MODEL:
                kwargs["model"] = MODEL
            _client = TypeSafeClient(**kwargs)
        return _client


def _open_guard_client() -> httpx.Client:
    return httpx.Client(timeout=TIMEOUT_SECONDS)


def _get_guard_client() -> httpx.Client:
    global _guard_client
    with _lock:
        if _guard_client is None:
            _guard_client = _open_guard_client()
        return _guard_client


def reset() -> None:
    """Drop the cached clients. Used by tests."""
    global _client, _guard_client
    with _lock:
        if _client is not None:
            close = getattr(_client, "close", None)
            if callable(close):
                close()
        _client = None
        if _guard_client is not None:
            _guard_client.close()
        _guard_client = None


def judge(*, tool: str, query: str, caller: str, workspace_id: str | None) -> Verdict:
    """Decide whether one query may be forwarded to Honcho."""
    if not enabled_for(tool):
        return Verdict(allowed=True, score=None, reason="gate off")
    if not query.strip():
        return Verdict(allowed=True, score=None, reason="no query text")
    if len(query) > TEXT_LIMIT:
        return Verdict(allowed=False, score=None, reason=QUERY_TOO_LONG)

    state = {
        "tool": tool,
        "caller": caller,
        "workspace": workspace_id or "",
        "query": query,
    }
    return _ask(state, "query")


def judge_answer(
    *, tool: str, query: str, result: Any, caller: str, workspace_id: str | None
) -> Verdict:
    """Decide whether a tool's result may go back to the teammate who asked.

    Every piece of text in the result is read, so an answer nested in a list of
    projects is judged like a single one. A long answer is judged in overlapping
    pieces, and the first piece refused or left unjudged decides.
    """
    if not enabled_for(tool):
        return Verdict(allowed=True, score=None, reason="gate off")
    try:
        answer = text_of(result)
    except Exception as exc:  # noqa: BLE001 - the gate must not crash a tool call
        return _on_failure(f"unreadable result: {type(exc).__name__}", "answer")
    if not answer.strip():
        return Verdict(allowed=True, score=None, reason="no answer text")
    pieces = _pieces(answer)
    if len(pieces) > ANSWER_PIECES:
        return Verdict(allowed=False, score=None, reason=ANSWER_TOO_LONG)

    state = {"tool": tool, "caller": caller, "workspace": workspace_id or ""}
    if query.strip():
        state["query"] = query[:TEXT_LIMIT]
    highest: Verdict | None = None
    for piece in pieces:
        if not piece.strip():
            # A stretch of blanks says nothing, and the hub turns away a blank answer.
            continue
        verdict = _ask({**state, "answer": piece}, "answer")
        if not verdict.allowed or verdict.unjudged:
            return verdict
        if highest is None or (verdict.score or 0.0) > (highest.score or 0.0):
            highest = verdict
    return highest or Verdict(allowed=True, score=None, reason="no answer text")


def text_of(result: Any) -> str:
    """Every piece of text in a tool's result, in order: what the caller reads."""
    parts: list[str] = []

    def walk(value: Any) -> None:
        if isinstance(value, str):
            if value.strip():
                parts.append(value)
        elif isinstance(value, dict):
            for item in value.values():
                walk(item)
        elif isinstance(value, (list, tuple)):
            for item in value:
                walk(item)
        elif value is not None and not isinstance(value, (bool, int, float)):
            walk(str(value))

    walk(result)
    return "\n".join(parts)


def _pieces(text: str) -> list[str]:
    """`text` cut into pieces of at most TEXT_LIMIT, each overlapping the last."""
    if len(text) <= TEXT_LIMIT:
        return [text]
    step = TEXT_LIMIT - ANSWER_OVERLAP
    return [
        text[start : start + TEXT_LIMIT]
        for start in range(0, len(text) - ANSWER_OVERLAP, step)
    ]


def _ask(state: dict[str, str], kind: str) -> Verdict:
    if GUARD_URL:
        return _ask_team_guard(GUARD_URL, state, kind)
    return _ask_jev(state, kind)


def _ask_jev(state: dict[str, str], kind: str) -> Verdict:
    """Ask Jev itself, through the official SDK, about a query or an answer."""
    try:
        from typesafe_sdk import Noul, TypeSafeError
    except ImportError as exc:
        return _on_failure(f"typesafe_sdk unavailable: {exc}", kind)

    if kind == "answer":
        name, question, criteria = "sensitive_answer", ANSWER_QUESTION, ANSWER_CRITERIA
    else:
        name, question, criteria = "out_of_scope", QUESTION, CRITERIA
    try:
        client = _get_client()
        response = client.system_one(
            state=state,
            questions={name: Noul(instructions=question, criteria=criteria)},
        )
        score = float(dict(response.answers)[name].noul)
    except TypeSafeError as exc:
        return _on_failure(f"{type(exc).__name__}: {exc}", kind)
    except Exception as exc:  # noqa: BLE001 - the gate must not crash a tool call
        return _on_failure(f"{type(exc).__name__}: {exc}", kind)

    return _decided(score >= THRESHOLD, score, kind)


def _decided(refused: bool, score: float, kind: str) -> Verdict:
    if kind == "answer":
        reason = ANSWER_REFUSED if refused else "answer in scope"
    else:
        reason = "out of scope" if refused else "in scope"
    return Verdict(allowed=not refused, score=score, reason=reason)


def _ask_team_guard(url: str, state: dict[str, str], kind: str = "query") -> Verdict:
    """Ask the team hub's guard about one query or answer and follow its verdict.

    Reasons carry only the HTTP status, the hub's error code and exception type
    names: an exception's text can quote a header value, and that is the token.
    """
    token = GUARD_TOKEN
    if not token:
        return _on_failure("team guard: no HONCHO_JEV_GUARD_TOKEN", kind)

    try:
        response = _get_guard_client().post(
            url, json=state, headers={"Authorization": f"Bearer {token}"}
        )
    except Exception as exc:  # noqa: BLE001 - the gate must not crash a tool call
        return _on_failure(f"team guard: {type(exc).__name__}", kind)

    if response.status_code != 200:
        detail = f"team guard: HTTP {response.status_code}{_error_code(response)}"
        return _on_failure(_without(token, detail), kind)
    try:
        answer = response.json()
    except ValueError:
        return _on_failure("team guard: HTTP 200 with a body that is not JSON", kind)
    if not isinstance(answer, dict):
        return _on_failure("team guard: HTTP 200 with an answer that is not an object", kind)

    judged = answer.get("judged")
    allowed = answer.get("allowed")
    if judged is False and allowed is True and answer.get("reason") == NO_KEY:
        return Verdict(
            allowed=True,
            score=None,
            reason="not judged: the team hub has no Jev key",
            no_key=True,
        )
    score = answer.get("score")
    if (
        judged is not True
        or not isinstance(allowed, bool)
        or isinstance(score, bool)
        or not isinstance(score, (int, float))
        or not math.isfinite(score)
    ):
        return _on_failure("team guard: HTTP 200 with a malformed answer", kind)
    if kind == "answer" and answer.get("checked") != "answer":
        # A hub from before answers were judged reads the query alone and ignores
        # the answer, so its verdict says nothing about what is about to go out.
        return Verdict(allowed=True, score=None, reason=ANSWER_SKIPPED, skipped=True)

    return _decided(not allowed, float(score), kind)


def _error_code(response: httpx.Response) -> str:
    """The hub's `error` code from a refusal body, with a leading space, or ""."""
    try:
        body = response.json()
    except ValueError:
        return ""
    code = body.get("error") if isinstance(body, dict) else None
    if isinstance(code, str) and _ERROR_CODE.fullmatch(code):
        return f" {code}"
    return ""


def _without(secret: str, text: str) -> str:
    return text.replace(secret, "***") if secret in text else text


def _on_failure(detail: str, kind: str = "query") -> Verdict:
    mode = ANSWER_FAIL_MODE if kind == "answer" else FAIL_MODE
    logger.warning("jev gate failed on the %s (%s mode): %s", kind, mode, detail)
    prefix = "jev unavailable for the answer" if kind == "answer" else "jev unavailable"
    reason = f"{prefix}: {detail}"
    if mode == "closed":
        return Verdict(allowed=False, score=None, reason=reason, failed=True)
    return Verdict(allowed=True, score=None, reason=reason, failed=True)
