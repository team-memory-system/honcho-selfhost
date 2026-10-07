"""Jev judgment gate for the Honcho MCP bridge.

Teammates reach this memory through one tool (`chat`). The gate reads the query
before it is forwarded and refuses the ones that are not asking about shared work.

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

MESSAGE = os.environ.get(
    "HONCHO_JEV_MESSAGE",
    "This query was refused: it asks for information outside the shared work scope "
    "of this memory. Ask about projects, code, decisions or documents instead.",
)

#: The hub's code for a team that has no Jev key.
NO_KEY = "no_key"

# Error codes the hub sends are copied into reasons; anything else is dropped.
_ERROR_CODE = re.compile(r"[a-z0-9_]{1,64}")

_lock = threading.Lock()
_client: Any = None
_guard_client: httpx.Client | None = None


@dataclass(frozen=True)
class Verdict:
    """Outcome of one judgment. `score` is None when Jev was not consulted.

    `failed` is True when Jev (or the hub's guard) was asked and gave no answer,
    whichever way `FAIL_MODE` then decided. `no_key` is True when the hub let the
    call through without asking Jev because the team has no Jev key.
    """

    allowed: bool
    score: float | None
    reason: str
    failed: bool = False
    no_key: bool = False


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

    state = {
        "tool": tool,
        "caller": caller,
        "workspace": workspace_id or "",
        "query": query,
    }
    if GUARD_URL:
        return _ask_team_guard(GUARD_URL, state)

    try:
        from typesafe_sdk import Noul, TypeSafeError
    except ImportError as exc:
        return _on_failure(f"typesafe_sdk unavailable: {exc}")

    try:
        client = _get_client()
        response = client.system_one(
            state=state,
            questions={"out_of_scope": Noul(instructions=QUESTION, criteria=CRITERIA)},
        )
        score = float(dict(response.answers)["out_of_scope"].noul)
    except TypeSafeError as exc:
        return _on_failure(f"{type(exc).__name__}: {exc}")
    except Exception as exc:  # noqa: BLE001 - the gate must not crash a tool call
        return _on_failure(f"{type(exc).__name__}: {exc}")

    if score >= THRESHOLD:
        return Verdict(allowed=False, score=score, reason="out of scope")
    return Verdict(allowed=True, score=score, reason="in scope")


def _ask_team_guard(url: str, state: dict[str, str]) -> Verdict:
    """Ask the team hub's guard about one query and follow its verdict.

    Reasons carry only the HTTP status, the hub's error code and exception type
    names: an exception's text can quote a header value, and that is the token.
    """
    token = GUARD_TOKEN
    if not token:
        return _on_failure("team guard: no HONCHO_JEV_GUARD_TOKEN")

    try:
        response = _get_guard_client().post(
            url, json=state, headers={"Authorization": f"Bearer {token}"}
        )
    except Exception as exc:  # noqa: BLE001 - the gate must not crash a tool call
        return _on_failure(f"team guard: {type(exc).__name__}")

    if response.status_code != 200:
        detail = f"team guard: HTTP {response.status_code}{_error_code(response)}"
        return _on_failure(_without(token, detail))
    try:
        answer = response.json()
    except ValueError:
        return _on_failure("team guard: HTTP 200 with a body that is not JSON")
    if not isinstance(answer, dict):
        return _on_failure("team guard: HTTP 200 with an answer that is not an object")

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
        return _on_failure("team guard: HTTP 200 with a malformed answer")

    if allowed:
        return Verdict(allowed=True, score=float(score), reason="in scope")
    return Verdict(allowed=False, score=float(score), reason="out of scope")


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


def _on_failure(detail: str) -> Verdict:
    logger.warning("jev gate failed (%s mode): %s", FAIL_MODE, detail)
    reason = f"jev unavailable: {detail}"
    if FAIL_MODE == "closed":
        return Verdict(allowed=False, score=None, reason=reason, failed=True)
    return Verdict(allowed=True, score=None, reason=reason, failed=True)
