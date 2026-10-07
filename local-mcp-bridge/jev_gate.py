"""Jev judgment gate for the Honcho MCP bridge.

Teammates reach this memory through one tool (`chat`). The gate reads the query
before it is forwarded and refuses the ones that are not asking about shared work.
It calls Jev through the official `typesafe_sdk` client directly.

Off unless `HONCHO_JEV_GATE` is set, so a bridge without a Jev key behaves exactly
as it did before. `typesafe_sdk` is imported on first use, not at module load.
"""

from __future__ import annotations

import logging
import os
import threading
from dataclasses import dataclass
from typing import Any

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

_lock = threading.Lock()
_client: Any = None


@dataclass(frozen=True)
class Verdict:
    """Outcome of one judgment. `score` is None when Jev was not consulted.

    `failed` is True when Jev was asked and gave no answer, whichever way
    `FAIL_MODE` then decided.
    """

    allowed: bool
    score: float | None
    reason: str
    failed: bool = False


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


def reset() -> None:
    """Drop the cached client. Used by tests."""
    global _client
    with _lock:
        if _client is not None:
            close = getattr(_client, "close", None)
            if callable(close):
                close()
        _client = None


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


def _on_failure(detail: str) -> Verdict:
    logger.warning("jev gate failed (%s mode): %s", FAIL_MODE, detail)
    reason = f"jev unavailable: {detail}"
    if FAIL_MODE == "closed":
        return Verdict(allowed=False, score=None, reason=reason, failed=True)
    return Verdict(allowed=True, score=None, reason=reason, failed=True)
