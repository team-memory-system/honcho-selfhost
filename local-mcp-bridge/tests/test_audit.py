from __future__ import annotations

from typing import Any

import pytest

import audit


def test_audit_is_off_without_a_dsn(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(audit, "DSN", "")
    assert audit.enabled() is False

    calls: list[Any] = []
    monkeypatch.setattr(audit._writer, "write", lambda row: calls.append(row))
    audit.record(
        tool="search",
        caller="someone",
        caller_source="address",
        arguments={"query": "지난주 결정"},
        workspace_id="memory",
        status="ok",
    )
    assert calls == []


def test_record_keeps_the_query_text_verbatim(monkeypatch: pytest.MonkeyPatch) -> None:
    rows: list[tuple[Any, ...]] = []
    monkeypatch.setattr(audit, "DSN", "postgresql://unused")
    monkeypatch.setattr(audit, "STORE_QUERY_TEXT", True)
    monkeypatch.setattr(audit._writer, "write", lambda row: rows.append(row))

    audit.record(
        tool="chat",
        caller="teammate@example.com",
        caller_source="cf-access",
        arguments={"query": "연봉 얼마야?", "reasoning_level": "low"},
        workspace_id="memory",
        status="denied",
        error="out of scope",
        duration_ms=12,
        jev_score=0.91,
        answer_score=0.04,
    )

    assert len(rows) == 1
    (
        _bridge, caller, source, tool, ws, query, args_json, status, error, ms, score,
        answer_score,
    ) = rows[0]
    assert tool == "chat"
    assert caller == "teammate@example.com"
    assert source == "cf-access"
    assert ws == "memory"
    assert query == "연봉 얼마야?"
    assert "reasoning_level" in args_json
    assert status == "denied"
    assert error == "out of scope"
    assert ms == 12
    assert score == pytest.approx(0.91)
    assert answer_score == pytest.approx(0.04)


def test_the_answer_score_is_a_column_an_older_log_gains() -> None:
    """A table made before answers were judged has no answer_score; the DDL run on
    every connection adds it, so the insert does not fail on an older log."""
    assert "ADD COLUMN IF NOT EXISTS answer_score" in audit._DDL
    assert audit._INSERT.count("%s") == 12
    assert audit._COLUMNS[-1] == "answer_score"
    assert "answer_score" in audit._SELECT


def test_query_text_can_be_withheld(monkeypatch: pytest.MonkeyPatch) -> None:
    rows: list[tuple[Any, ...]] = []
    monkeypatch.setattr(audit, "DSN", "postgresql://unused")
    monkeypatch.setattr(audit, "STORE_QUERY_TEXT", False)
    monkeypatch.setattr(audit._writer, "write", lambda row: rows.append(row))

    audit.record(
        tool="search",
        caller="x",
        caller_source="address",
        arguments={"query": "비밀"},
        workspace_id=None,
        status="ok",
    )
    assert rows[0][5] is None


@pytest.mark.parametrize(
    ("arguments", "expected"),
    [
        ({"query": "a"}, "a"),
        ({"search_query": "b"}, "b"),
        ({"queries": ["c", "d"]}, "c | d"),
        ({"query": "   ", "search_query": "e"}, "e"),
        ({"limit": 10}, None),
        ({}, None),
    ],
)
def test_query_text_of_reads_every_query_parameter(
    arguments: dict[str, Any], expected: str | None
) -> None:
    assert audit.query_text_of(arguments) == expected


def test_sanitize_drops_secrets_and_stringifies_the_unserializable() -> None:
    cleaned = audit.sanitize(
        {"query": "ok", "token": "shhh", "obj": object(), "n": 3}
    )
    assert cleaned["query"] == "ok"
    assert cleaned["token"] == "[redacted]"
    assert cleaned["n"] == 3
    assert isinstance(cleaned["obj"], str)


def test_long_values_are_truncated() -> None:
    long = "가" * 9000
    assert audit.query_text_of({"query": long}).endswith("[truncated]")
    assert len(audit.sanitize({"query": long})["query"]) < 3000


def test_the_whole_query_is_read_without_a_limit() -> None:
    long = "가" * 9000 + " 집 주소"
    assert audit.query_text_of({"query": long}, limit=None) == long


def test_a_broken_database_never_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    """A tool call must survive an unreachable audit log."""
    monkeypatch.setattr(audit, "DSN", "postgresql://127.0.0.1:1/definitely-not-there")
    writer = audit._Writer()
    monkeypatch.setattr(audit, "_writer", writer)

    audit.record(
        tool="search",
        caller="x",
        caller_source="address",
        arguments={"query": "q"},
        workspace_id="memory",
        status="ok",
    )
    assert writer._conn is None


def test_a_bad_schema_name_is_rejected() -> None:
    """The schema name is interpolated into DDL, so it has to be an identifier."""
    assert audit.SCHEMA.replace("_", "").isalnum()


def test_a_hanging_database_is_bounded_by_a_connect_timeout() -> None:
    """A refused port fails instantly; a host that drops packets does not. Without
    a limit every tool call would wait on it."""
    assert audit.CONNECT_TIMEOUT_SECONDS > 0


def test_a_failure_backs_off_instead_of_retrying_every_call(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Paying the connection timeout on every request while the database is down
    would slow each tool call down for as long as the outage lasts."""
    monkeypatch.setattr(audit, "DSN", "postgresql://127.0.0.1:1/definitely-not-there")
    writer = audit._Writer()
    attempts = 0

    def counting_connect() -> Any:
        nonlocal attempts
        attempts += 1
        raise OSError("connection refused")

    monkeypatch.setattr(writer, "_connect", counting_connect)
    row = ("bridge", "caller", "address", "search", "memory", "q", "{}", "ok", None, 1, None, None)

    writer.write(row)
    first = attempts
    assert first == 2, "one retry, then give up"

    writer.write(row)
    assert attempts == first, "the second call is skipped while the back-off holds"

    writer._retry_at = 0.0
    writer.write(row)
    assert attempts > first, "once the back-off expires it tries again"
