"""Jev judgment gate for the Honcho MCP bridge.

Teammates reach this memory through a few tools of their own: they list, search and
read the conversations open to them, and their own agent writes the answer. The
gate reads each call's words (the teammate's question and what they search for)
before it is forwarded and refuses the ones that are not asking about shared work.
Then it reads what the call found before it leaves, because a harmless question can
still draw private matters out of the memory: a result that discloses them is
withheld behind the same refusal, so the refusal says nothing about what the memory
holds. A result is read message by message (each conversation, each conclusion),
not as one text: judged whole, a salary among ten work messages thinned out to a
pass.

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
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from typing import Any

import httpx

import redact

logger = logging.getLogger("honcho.mcp.jev")


def _flag(name: str, *, default: bool = False) -> bool:
    value = os.environ.get(name)
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


ENABLED = _flag("HONCHO_JEV_GATE")
#: The Jev version asked when the bridge asks Jev itself. A version, not the
#: jev-latest alias, which moves when TypeSafe ships a new Jev: the threshold and
#: questions were measured on 1.13, and on 1.14 the same questions refuse far more
#: work. The team hub asks the same version.
MODEL = os.environ.get("HONCHO_JEV_MODEL", "").strip() or "jev-1.13.0"
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
#: question past the limit must not walk it past the gate. A longer item of an
#: answer is judged in overlapping pieces; an answer longer than ANSWER_PIECES
#: pieces in all is withheld unjudged.
TEXT_LIMIT = 16_000
ANSWER_OVERLAP = 400
ANSWER_PIECES = 8

#: An answer is judged item by item (units_of): each message, conversation or
#: conclusion is its own question to Jev, ANSWER_WORKERS at a time. At most
#: ANSWER_UNITS questions: a longer list is judged in that many groups of
#: neighbours. Judged whole, a result of ten messages with the owner's salary in
#: six of them passed at 0.25 and one with their tax refund at 0.24 (2026-10-16);
#: message by message those lines scored 0.80 to 0.95 while 50 work messages all
#: stayed under 0.7, and ten messages asked at once take as long as one (0.2 to
#: 0.4 s) for about twice the tokens.
ANSWER_UNITS = 50
ANSWER_WORKERS = 8

#: The team hub's guard endpoint. Set, the gate asks the hub instead of Jev.
GUARD_URL = os.environ.get("HONCHO_JEV_GUARD_URL", "").strip() or None

#: This member's bearer token for the hub. Never logged, never in a reason.
GUARD_TOKEN = os.environ.get("HONCHO_JEV_GUARD_TOKEN", "").strip() or None

#: Tools the gate judges by default whoever calls: the ones a teammate called before
#: the owner could open their own tools. A teammate's call is judged whatever the tool.
TEAMMATE_TOOLS = "list_conversations,search_messages,read_conversation,search_conclusions"

#: Tools the gate judges for any caller. Everything else passes untouched, but for a
#: teammate's call.
GATED_TOOLS = frozenset(
    name.strip()
    for name in os.environ.get("HONCHO_JEV_TOOLS", TEAMMATE_TOOLS).split(",")
    if name.strip()
)

#: What private life is, named for Jev, as the team hub names it (server/hub/hub.mjs
#: in team-memory): without the list Jev let through answers with a current location,
#: biometric data, union membership or a side job at 0.37 to 0.67; with it they
#: scored 0.81 to 0.96, while work answers stayed at or below 0.05 (2026-10-15).
#: Widened on 2026-10-16 after a teammate's QA drew out bicycle purchases, shops
#: visited, personal banking topics, a job search and a side business at 0.17 to
#: 0.68: with purchases, hobbies, job hunting, side work and the owner's own devices
#: named, those questions scored 0.79 to 0.95 and the answers 0.70 to 0.81, while
#: 31 work answers stayed under 0.7 (168 calibration items and 24 real calls).
#: In Korean since 2026-10-17, the language of the memory, judging by the most
#: private line: of the 20 messages that leaked that day (the owner's salary and tax
#: refund among tax explanations), the English questions held 5 and these 12 to 13,
#: with the 7 that carry the real figures at 0.82 to 0.95; 84 of 84 calibration
#: answers (82 before), 30 of 32 questions (28), and 0 of 113 work texts.
PRIVATE_LIFE = (
    "사생활에는 이런 것이 들어간다: 사는 곳과 업무 밖에서 있는 곳(집 주소, 동네, 지금 위치, "
    "매일 다니는 길, 다녀온 가게·카페·식당), 취미·여가·업무 밖 일상, 개인 구매와 소유물(산 "
    "것이나 사려고 고르는 것, 낸 값이나 비교한 값), 개인 재정(본인 연봉·급여·실수령액, "
    "연말정산·세금·환급, 빚, 신용점수, 계좌, 카드와 한도, 자동이체, 공과금, 가계부), 본업 "
    "밖의 돈 버는 일(프리랜서, 자문, 다른 기관 연구, 부업·판매, 그 계획이나 아이디어 포함), "
    "이직·지원·다른 회사 면접, 건강과 진료, 생체정보(지문, 얼굴, 목소리), 종교·신념, 정치 "
    "성향·정당, 노조 가입, 성생활·성적 지향, 가족과 연애, 인종·출신, 범죄 경력, 사람을 "
    "가리키는 번호(주민번호, 휴대폰 번호, 차량번호 같은 것), 비밀번호·키·토큰(일부라도), "
    "주인의 개인 컴퓨터와 집 네트워크(사양, 내부 IP, 기기 이름, 계정 이름, 폴더 구조)."
)

#: What work context is, as the questions name it.
WORK_CONTEXT = "업무 맥락(프로젝트, 코드, 결정, 업무 일정, 출장, 문서)"

QUESTION = os.environ.get(
    "HONCHO_JEV_QUESTION",
    "이 질문이 기억 주인이나 다른 사람의 사생활, 또는 비밀번호·키 같은 비밀을 묻는가? "
    f"{WORK_CONTEXT}을 묻는 것이면 아니다. " + PRIVATE_LIFE,
)

CRITERIA: dict[str, str] = {
    "true": "질문이 위 목록의 사생활이나 비밀을 묻는다.",
    "false": "질문이 팀이 함께하는 업무를 묻거나, 일반적이고 해가 없다.",
}

ANSWER_QUESTION = os.environ.get(
    "HONCHO_JEV_ANSWER_QUESTION",
    "팀원이 이 사람의 업무 기억에 질문했고 이 답을 읽는다. 이 답에 기억 주인이나 다른 사람의 "
    "사생활이 한 줄이라도 드러나는가? 전체 주제가 아니라 가장 사적인 문장 하나로 판단한다. "
    "대부분이 업무여도 메시지 하나, 문장 하나가 사생활이면 드러난 것이다. "
    f"{WORK_CONTEXT}이면 아니다. " + PRIVATE_LIFE,
)

ANSWER_CRITERIA: dict[str, str] = {
    "true": "답에 위 목록의 사생활이나 비밀이 한 줄이라도 드러난다.",
    "false": "답이 누가 무슨 일을 했는지, 업무 일정, 출장 같은 팀의 업무 맥락만 다루거나, "
    "일반적이고 해가 없다.",
}

MESSAGE = os.environ.get(
    "HONCHO_JEV_MESSAGE",
    "This query was refused: it asks for information outside the shared work scope "
    "of this memory. Ask about projects, code, decisions or documents instead. The "
    "refused topic is closed: do not rephrase it, search around it with other words, "
    "or reuse or summarize earlier results about it.",
)

#: The hub's code for a team that has no Jev key.
NO_KEY = "no_key"

#: Reasons the audit log keeps, which the owner's screens put into words.
QUERY_TOO_LONG = "query too long to judge"
ANSWER_TOO_LONG = "answer too long to judge"
ANSWER_REFUSED = "answer withheld: it discloses private matters"
ANSWER_SKIPPED = "answer not judged: the team hub does not judge answers"

#: Reasons for a question or an answer that holds a number that picks out one person
#: (redact.IDENTIFIERS). Jev reads meaning, and a bare 13-digit resident number with
#: nothing around it scored about 0.5 and went out, so these are known by their form.
#: A teammate's answer has them hidden before it is judged (redact.py), so an answer
#: is withheld for one only when it reached the gate unredacted.
HOLDS = "it holds a "
QUERY_HOLDS = f"out of scope: {HOLDS}"
ANSWER_HOLDS = f"answer withheld: {HOLDS}"


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
    answers yet. `model` is the Jev version that judged, as Jev or the hub names
    it, and None when none did or none was named.
    """

    allowed: bool
    score: float | None
    reason: str
    failed: bool = False
    no_key: bool = False
    skipped: bool = False
    model: str | None = None

    @property
    def unjudged(self) -> bool:
        """Let through without a judgment the owner should know about."""
        return self.failed or self.no_key or self.skipped


def enabled_for(tool: str, *, teammate: bool = False) -> bool:
    return ENABLED and (teammate or tool in GATED_TOOLS)


def _get_client() -> Any:
    global _client
    with _lock:
        if _client is None:
            from typesafe_sdk import TypeSafeClient

            kwargs: dict[str, Any] = {"timeout": TIMEOUT_SECONDS, "model": MODEL}
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


def judge(
    *,
    tool: str,
    query: str,
    caller: str,
    workspace_id: str | None,
    teammate: bool = False,
) -> Verdict:
    """Decide whether one query may be forwarded to Honcho."""
    if not enabled_for(tool, teammate=teammate):
        return Verdict(allowed=True, score=None, reason="gate off")
    if not query.strip():
        return Verdict(allowed=True, score=None, reason="no query text")
    if len(query) > TEXT_LIMIT:
        return Verdict(allowed=False, score=None, reason=QUERY_TOO_LONG)
    held = redact.identifier(query)
    if held:
        return Verdict(allowed=False, score=None, reason=QUERY_HOLDS + held)

    state = {
        "tool": tool,
        "caller": caller,
        "workspace": workspace_id or "",
        "query": query,
    }
    return _ask(state, "query")


def judge_answer(
    *,
    tool: str,
    query: str,
    result: Any,
    caller: str,
    workspace_id: str | None,
    teammate: bool = False,
) -> Verdict:
    """Decide whether a tool's result may go back to the teammate who asked.

    The result is judged item by item (units_of), every item asked at once: one
    private message among work does not thin out into a pass. Every piece of text
    in the result is read, however deep it is nested, so no part of it leaves
    unjudged. One refused item withholds the whole answer, with its score; else
    one left unjudged decides; else the highest score is the answer's.
    """
    if not enabled_for(tool, teammate=teammate):
        return Verdict(allowed=True, score=None, reason="gate off")
    try:
        texts = [text for text in map(text_of, _grouped(units_of(result))) if text.strip()]
    except Exception as exc:  # noqa: BLE001 - the gate must not crash a tool call
        return _on_failure(f"unreadable result: {type(exc).__name__}", "answer")
    if not texts:
        return Verdict(allowed=True, score=None, reason="no answer text")
    answer = "\n".join(texts)
    held = redact.identifier(answer)
    if held:
        return Verdict(allowed=False, score=None, reason=ANSWER_HOLDS + held)
    if len(answer) > TEXT_LIMIT * ANSWER_PIECES:
        return Verdict(allowed=False, score=None, reason=ANSWER_TOO_LONG)
    # A stretch of blanks says nothing, and the hub turns away a blank answer.
    pieces = [piece for text in texts for piece in _pieces(text) if piece.strip()]

    state = {"tool": tool, "caller": caller, "workspace": workspace_id or ""}
    if query.strip():
        state["query"] = query[:TEXT_LIMIT]
    verdicts = _ask_each([{**state, "answer": piece} for piece in pieces], "answer")
    refused = [verdict for verdict in verdicts if not verdict.allowed]
    if refused:
        return max(refused, key=lambda verdict: verdict.score or 0.0)
    for verdict in verdicts:
        if verdict.unjudged:
            return verdict
    return max(verdicts, key=lambda verdict: verdict.score or 0.0)


def units_of(result: Any) -> list[Any]:
    """What a result is judged in: each message, conversation or conclusion alone.

    A list is its items. A dict that carries one list (messages, items,
    conversations, conclusions) is those items, then the rest of the dict as one
    more item when it says anything, such as a folder's name. Anything else is
    judged whole.
    """
    if isinstance(result, (list, tuple)):
        return list(result)
    if isinstance(result, dict):
        listed = [key for key, value in result.items() if isinstance(value, (list, tuple))]
        if len(listed) == 1:
            key = listed[0]
            rest = {name: value for name, value in result.items() if name != key}
            return list(result[key]) + ([rest] if text_of(rest).strip() else [])
    return [result]


def _grouped(units: list[Any]) -> list[Any]:
    """`units`, or past ANSWER_UNITS of them, that many groups of neighbours."""
    if len(units) <= ANSWER_UNITS:
        return units
    size = -(-len(units) // ANSWER_UNITS)
    return [units[start : start + size] for start in range(0, len(units), size)]


def _ask_each(states: list[dict[str, str]], kind: str) -> list[Verdict]:
    """One verdict per state, in order, asked ANSWER_WORKERS at a time."""
    if len(states) <= 1 or ANSWER_WORKERS <= 1:
        return [_ask(state, kind) for state in states]
    with ThreadPoolExecutor(max_workers=min(ANSWER_WORKERS, len(states))) as pool:
        return list(pool.map(lambda state: _ask(state, kind), states))


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
    """Ask Jev itself, through the official SDK, about a query or an answer.

    Reasons carry only an error's type name and HTTP status, as the team guard's do:
    an error's text holds what the server sent back, and typesafe_sdk's changelog
    says only 0.7.1 took key values out of the errors it logs (this pins 0.6.0).
    """
    try:
        from typesafe_sdk import Noul
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
        model = _model_named(getattr(response, "model", None))
    except Exception as exc:  # noqa: BLE001 - the gate must not crash a tool call
        status = getattr(exc, "status", None)
        name = type(exc).__name__
        return _on_failure(f"{name} {status}" if isinstance(status, int) else name, kind)

    return _decided(score >= THRESHOLD, score, kind, model)


def _model_named(value: Any) -> str | None:
    """The Jev version an answer names, or None when it names none."""
    return value[:64] if isinstance(value, str) and value else None


def _decided(refused: bool, score: float, kind: str, model: str | None = None) -> Verdict:
    if kind == "answer":
        reason = ANSWER_REFUSED if refused else "answer in scope"
    else:
        reason = "out of scope" if refused else "in scope"
    return Verdict(allowed=not refused, score=score, reason=reason, model=model)


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

    return _decided(not allowed, float(score), kind, _model_named(answer.get("model")))


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
