"""What a teammate's answer never shows: numbers and secrets known by their form.

The memory keeps every message as it was written; these are hidden only on the way
out to a teammate, so the rules can change later without touching what is stored.
Each one is replaced in place by a short mark ("[주민등록번호 가림]"), so the work
around it still reaches the teammate, and the Jev gate then judges the answer with
the numbers already gone, so they never reach Jev either.

Only what its form gives away is here. A phone number, an e-mail address or a car's
plate is often what people work with, so those go out as written and the Jev gate
judges them by what is said around them, as it judges a name, an address or a
private matter.
"""

from __future__ import annotations

import re
from collections import Counter
from collections.abc import Callable
from typing import Any

#: What each kind is called in the mark that replaces it.
NAMES: dict[str, str] = {
    "resident": "주민등록번호",
    "foreign": "외국인등록번호",
    "passport": "여권번호",
    "driver": "운전면허번호",
    "card": "카드번호",
    "account": "계좌번호",
    "coordinate": "좌표",
    "api_key": "API 키",
    "private_key": "비밀 키",
    "password": "비밀번호",
}

#: The kinds that pick out one person; a question holding one is refused, since
#: searching for it tells whether the memory has it. Their names, for the log.
IDENTIFIERS: dict[str, str] = {
    "resident": "resident registration number",
    "foreign": "foreign registration number",
    "passport": "passport number",
    "driver": "driver's license number",
    "card": "card number",
}


def mark(kind: str) -> str:
    return f"[{NAMES[kind]} 가림]"


# A Korean resident registration number: birth date, a digit for the century and
# sex, six more, with or without the dash. 5–8 are given to foreigners. Numbers
# given since October 2020 carry no check digit, so none is tested.
_RESIDENT = re.compile(r"(?<!\d)(\d{2})(\d{2})(\d{2})([- ]?)([0-9])(\d{6})(?!\d)")
# A Korean passport: a letter and eight digits before 2021, a letter, three digits,
# a letter and four digits since. The older form is a plain serial number, so only
# when 여권 or passport comes just before it.
_PASSPORT_NEW = re.compile(r"(?<![A-Za-z0-9])[MSRODG]\d{3}[A-Z]\d{4}(?![A-Za-z0-9])")
_PASSPORT_OLD = re.compile(
    r"(?i:여권|passport)[^\n]{0,20}?(?<![A-Za-z0-9])([MSRODG]\d{8})(?![A-Za-z0-9])"
)
# A Korean driver's license: region (11–28), year, serial, check, always dashed.
_DRIVER = re.compile(r"(?<![\d-])(1[1-9]|2[0-8])-\d{2}-\d{6}-\d{2}(?![\d-])")
# A payment card: 13 to 19 digits, in groups or not, starting as the networks do
# (3 Amex/JCB/Diners, 4 Visa, 5 Mastercard, 6 Discover/UnionPay, 9 Korean domestic),
# with a valid Luhn check digit.
_CARD = re.compile(r"(?<!\d)[345693](?:[ -]?\d){12,18}(?!\d)")
# Latitude and longitude, four decimals or more. A pair of scores below 1 is not a
# place, so both need a whole part.
_COORDINATE = re.compile(
    r"(?<![\d.])(-?[1-9]\d?\.\d{4,})\s*,\s*(-?[1-9]\d{0,2}\.\d{4,})(?![\d.])"
)
# Keys and tokens whose start says what they are.
_API_KEY = re.compile(
    r"(?<![A-Za-z0-9_-])(?:"
    r"sk-[A-Za-z0-9_-]{20,}"
    r"|gh[pousr]_[A-Za-z0-9]{30,}|github_pat_[A-Za-z0-9_]{30,}"
    r"|xox[abposr]-[A-Za-z0-9-]{10,}"
    r"|glpat-[A-Za-z0-9_-]{20,}|npm_[A-Za-z0-9]{30,}|hf_[A-Za-z0-9]{30,}"
    r"|ops_[A-Za-z0-9_-]{20,}"
    r"|AKIA[0-9A-Z]{16}|AIza[0-9A-Za-z_-]{35}"
    r"|eyJ[A-Za-z0-9_-]{8,}\.eyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}"
    r")"
)
# A private key file, to its end or to the end of a text clipped inside it.
_PRIVATE_KEY = re.compile(
    r"-----BEGIN (?:[A-Z0-9]+ )*PRIVATE KEY-----.*?(?:-----END (?:[A-Z0-9]+ )*PRIVATE KEY-----|\Z)",
    re.DOTALL,
)
# The password in an address such as https://user:pass@host.
_URL_PASSWORD = re.compile(r"(?i)(\b[a-z][a-z0-9+.-]*://[^\s:/@]+:)([^\s@/]+)(@)")
# An account number: a bank or the word 계좌 just before a dashed or long number.
_ACCOUNT = re.compile(
    r"(?i:계좌|은행|뱅크|입금|송금|이체|예금주|account|acct"
    r"|국민|신한|농협|우체국|새마을금고|IBK|NH|KB|SC제일|씨티)"
    r"[^\d\n]{0,12}?(?<!\d)(\d{2,6}(?:-\d{2,7}){1,3}|\d{10,14})(?![\d-])"
)
# A password or token named just before its value.
_SECRET_VALUE = re.compile(
    r"(?i:비밀번호|비번|패스워드|암호|password|passwd|pwd|passphrase|secret|토큰|token"
    r"|api[_ -]?key|access[_ -]?key|client[_ -]?secret)"
    r"[\"']?(?:\s*[:=]\s*|\s+|은\s*|는\s*|이\s*|가\s*)[\"'`]?([^\s\"'`,;]+)"
)
_HANGUL = re.compile(r"[가-힣]")
_PLAIN_WORDS = frozenset(
    {"true", "false", "null", "none", "required", "optional", "hidden"}
)


def _luhn(digits: str) -> bool:
    total = 0
    for index, char in enumerate(reversed(digits)):
        value = int(char) * (2 if index % 2 else 1)
        total += value - 9 if value > 9 else value
    return total % 10 == 0


def _resident_kind(match: re.Match[str]) -> str | None:
    month, day, century = int(match.group(2)), int(match.group(3)), match.group(5)
    if not (1 <= month <= 12 and 1 <= day <= 31):
        return None
    if century in "1234":
        return "resident"
    if century in "5678":
        return "foreign"
    return None


def _card(match: re.Match[str]) -> bool:
    digits = re.sub(r"[ -]", "", match.group(0))
    return 13 <= len(digits) <= 19 and _luhn(digits)


def _account(number: str) -> bool:
    digits = number.replace("-", "")
    # A phone number keeps its shape, whatever word comes before it.
    return len(digits) >= 10 and not re.fullmatch(r"0\d{1,2}-\d{3,4}-\d{4}", number)


def _secret_value(value: str) -> bool:
    value = value.rstrip(".)]}>")
    if len(value) < 4 or value.startswith(("$", "{", "<", "[", "%")):
        return False
    if _HANGUL.search(value) or value.lower() in _PLAIN_WORDS:
        return False
    has_digit = any(char.isdigit() for char in value)
    has_symbol = any(not char.isalnum() for char in value)
    mixed = any(char.isupper() for char in value) and any(
        char.islower() for char in value
    )
    return has_digit or has_symbol or mixed


def _whole(
    pattern: re.Pattern[str], kind: str | Callable[[re.Match[str]], str | None]
) -> Callable[[str, Counter[str]], str]:
    """A rule that replaces the whole match, when `kind` says what it is."""

    def apply(text: str, counts: Counter[str]) -> str:
        def replace(match: re.Match[str]) -> str:
            found = kind(match) if callable(kind) else kind
            if not found:
                return match.group(0)
            counts[found] += 1
            return mark(found)

        return pattern.sub(replace, text)

    return apply


def _part(
    pattern: re.Pattern[str],
    kind: str,
    group: int,
    keep: Callable[[str], bool] = lambda _: True,
) -> Callable[[str, Counter[str]], str]:
    """A rule that replaces only one group of the match, the value after its name."""

    def apply(text: str, counts: Counter[str]) -> str:
        def replace(match: re.Match[str]) -> str:
            value = match.group(group)
            if not keep(value):
                return match.group(0)
            # What follows the value (a full stop, a closing bracket) stays.
            tail = ""
            if kind == "password":
                stripped = value.rstrip(".)]}>")
                value, tail = stripped, value[len(stripped) :]
            counts[kind] += 1
            start, end = match.span(group)
            whole = match.group(0)
            offset = match.start(0)
            return whole[: start - offset] + mark(kind) + tail + whole[end - offset :]

        return pattern.sub(replace, text)

    return apply


# In this order: a key or token first, so a named password whose value is one is
# not counted twice, and the identifying numbers before the card, whose wider
# pattern would also take a dashed resident number.
_RULES: list[Callable[[str, Counter[str]], str]] = [
    _whole(_PRIVATE_KEY, "private_key"),
    _whole(_API_KEY, "api_key"),
    _part(_URL_PASSWORD, "password", 2),
    _whole(_RESIDENT, _resident_kind),
    _whole(_DRIVER, "driver"),
    _whole(_PASSPORT_NEW, "passport"),
    _part(_PASSPORT_OLD, "passport", 1),
    _whole(_CARD, lambda match: "card" if _card(match) else None),
    _part(_ACCOUNT, "account", 1, _account),
    _whole(_COORDINATE, "coordinate"),
    _part(_SECRET_VALUE, "password", 1, _secret_value),
]


def redact_text(text: str, counts: Counter[str] | None = None) -> str:
    """`text` with every number and secret known by its form replaced by its mark."""
    counts = counts if counts is not None else Counter()
    for rule in _RULES:
        text = rule(text, counts)
    return text


def redact(value: Any) -> tuple[Any, dict[str, int]]:
    """A tool's result with every piece of text in it redacted, and what was hidden.

    The result is copied, not changed: dictionaries keep their keys, and lists and
    tuples come back as lists. Whatever is not text stays as it is.
    """
    counts: Counter[str] = Counter()

    def walk(item: Any) -> Any:
        if isinstance(item, str):
            return redact_text(item, counts)
        if isinstance(item, dict):
            return {key: walk(inner) for key, inner in item.items()}
        if isinstance(item, (list, tuple)):
            return [walk(inner) for inner in item]
        if hasattr(item, "model_dump"):
            # A Pydantic model goes out as its JSON, so it is read as that.
            return walk(item.model_dump(mode="json"))
        return item

    return walk(value), dict(counts)


_MARK = re.compile(
    r"\[(?:" + "|".join(re.escape(name) for name in NAMES.values()) + r") 가림\]"
)
#: What Jev reads in place of every mark. Named, a mark alone drew Jev: with the
#: number gone, "출장 항공권은 여권 [여권번호 가림]로 끊었다" still scored 0.76 and
#: was withheld; "여권 [가림]" scored 0.56.
PLAIN_MARK = "[가림]"


def unnamed(value: Any) -> Any:
    """A redacted result as Jev reads it: every mark is the same, naming no kind."""
    if isinstance(value, str):
        return _MARK.sub(PLAIN_MARK, value)
    if isinstance(value, dict):
        return {key: unnamed(inner) for key, inner in value.items()}
    if isinstance(value, list):
        return [unnamed(inner) for inner in value]
    return value


def identifier(text: str) -> str | None:
    """The name of the first number in `text` that picks out one person, or None."""
    counts: Counter[str] = Counter()
    redact_text(text, counts)
    for kind, name in IDENTIFIERS.items():
        if counts[kind]:
            return name
    return None
