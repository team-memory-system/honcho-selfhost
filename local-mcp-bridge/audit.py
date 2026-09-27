"""Tool-call audit log for the Honcho MCP bridge.

Records who asked what, through which tool, with the query text intact. Writes to
a schema of its own inside the Honcho database, so it never collides with the
alembic-managed tables the Honcho API owns (`src/config.py` keeps `DB.SCHEMA`
configurable for the same reason).

Nothing here connects at import time: the bridge's own tests import `server`
without a database, and a tool call must succeed even when the audit log is
unreachable.
"""

from __future__ import annotations

import json
import logging
import os
import threading
import time
from typing import Any

logger = logging.getLogger("honcho.mcp.audit")

#: Parameter names that carry the text a caller actually typed, best first.
QUERY_PARAMS = (
    "query",
    "search_query",
    "queries",
    "question",
    "prompt",
    "text",
)

#: Parameter values never copied into the log.
REDACTED_PARAMS = frozenset({"token", "bearer_token", "api_key", "password"})

DSN = os.environ.get("HONCHO_AUDIT_DSN", "").strip()
SCHEMA = os.environ.get("HONCHO_AUDIT_SCHEMA", "honcho_audit").strip() or "honcho_audit"
BRIDGE_NAME = os.environ.get("HONCHO_AUDIT_BRIDGE", "").strip() or "bridge"
RETENTION_DAYS = int(os.environ.get("HONCHO_AUDIT_RETENTION_DAYS", "0") or "0")
STORE_QUERY_TEXT = (
    os.environ.get("HONCHO_AUDIT_STORE_QUERY_TEXT", "1").strip().lower()
    not in {"0", "false", "no", "off"}
)

_PRUNE_INTERVAL_SECONDS = 3600.0

#: Seconds to wait for the audit database. Without a limit, an unreachable host
#: (dropped packets rather than a refused port) would hang every tool call.
CONNECT_TIMEOUT_SECONDS = int(os.environ.get("HONCHO_AUDIT_CONNECT_TIMEOUT", "3") or "3")

#: How long to stop trying after a failure, so a tool call does not pay the
#: connection timeout again on every request while the database is down.
_RETRY_AFTER_SECONDS = 30.0

if not SCHEMA.replace("_", "").isalnum():
    raise RuntimeError(f"HONCHO_AUDIT_SCHEMA must be a bare identifier: {SCHEMA!r}")

_DDL = f"""
CREATE SCHEMA IF NOT EXISTS {SCHEMA};

CREATE TABLE IF NOT EXISTS {SCHEMA}.tool_calls (
    id            bigserial PRIMARY KEY,
    at            timestamptz NOT NULL DEFAULT now(),
    bridge        text        NOT NULL,
    caller        text        NOT NULL,
    caller_source text        NOT NULL,
    tool          text        NOT NULL,
    workspace_id  text,
    query_text    text,
    arguments     jsonb,
    status        text        NOT NULL,
    error         text,
    duration_ms   integer,
    jev_score     double precision
);

CREATE INDEX IF NOT EXISTS tool_calls_at_idx     ON {SCHEMA}.tool_calls (at DESC);
CREATE INDEX IF NOT EXISTS tool_calls_caller_idx ON {SCHEMA}.tool_calls (caller, at DESC);
CREATE INDEX IF NOT EXISTS tool_calls_tool_idx   ON {SCHEMA}.tool_calls (tool, at DESC);
"""

_INSERT = f"""
INSERT INTO {SCHEMA}.tool_calls
    (bridge, caller, caller_source, tool, workspace_id,
     query_text, arguments, status, error, duration_ms, jev_score)
VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
"""


def enabled() -> bool:
    """True when a DSN is configured. Without one the bridge simply does not log."""
    return bool(DSN)


class _Writer:
    """One lazily-opened connection, guarded by a lock and reopened on failure."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._conn: Any = None
        self._ready = False
        self._last_prune = 0.0
        self._warned = False
        self._retry_at = 0.0

    def _connect(self) -> Any:
        import psycopg  # imported here so the module loads without the driver

        conn = psycopg.connect(DSN, autocommit=True, connect_timeout=CONNECT_TIMEOUT_SECONDS)
        with conn.cursor() as cur:
            cur.execute(_DDL)
        return conn

    def _conn_ready(self) -> Any:
        if self._conn is None or self._conn.closed:
            self._conn = self._connect()
            self._ready = True
        return self._conn

    def write(self, row: tuple[Any, ...]) -> None:
        with self._lock:
            if self._conn is None and time.monotonic() < self._retry_at:
                return
            for attempt in (1, 2):
                try:
                    conn = self._conn_ready()
                    with conn.cursor() as cur:
                        cur.execute(_INSERT, row)
                    self._warned = False
                    self._retry_at = 0.0
                    self._maybe_prune(conn)
                    return
                except Exception as exc:  # noqa: BLE001 - logging must never raise
                    self._conn = None
                    if attempt == 2:
                        self._retry_at = time.monotonic() + _RETRY_AFTER_SECONDS
                        if not self._warned:
                            logger.warning("audit write failed: %s: %s", type(exc).__name__, exc)
                            self._warned = True
                        return

    def _maybe_prune(self, conn: Any) -> None:
        if RETENTION_DAYS <= 0:
            return
        now = time.monotonic()
        if now - self._last_prune < _PRUNE_INTERVAL_SECONDS:
            return
        self._last_prune = now
        with conn.cursor() as cur:
            cur.execute(
                f"DELETE FROM {SCHEMA}.tool_calls WHERE at < now() - %s * interval '1 day'",
                (RETENTION_DAYS,),
            )


_writer = _Writer()


def _truncate(value: str, limit: int = 8000) -> str:
    return value if len(value) <= limit else value[:limit] + "…[truncated]"


def query_text_of(arguments: dict[str, Any]) -> str | None:
    """The caller's own words, pulled from whichever parameter carries them."""
    for name in QUERY_PARAMS:
        value = arguments.get(name)
        if isinstance(value, str) and value.strip():
            return _truncate(value)
        if isinstance(value, (list, tuple)) and value:
            joined = " | ".join(str(item) for item in value)
            if joined.strip():
                return _truncate(joined)
    return None


def sanitize(arguments: dict[str, Any]) -> dict[str, Any]:
    """Arguments as stored: secrets dropped, unserializable values stringified."""
    out: dict[str, Any] = {}
    for key, value in arguments.items():
        if key in REDACTED_PARAMS:
            out[key] = "[redacted]"
            continue
        try:
            json.dumps(value)
        except (TypeError, ValueError):
            value = repr(value)
        if isinstance(value, str):
            value = _truncate(value, 2000)
        out[key] = value
    return out


def record(
    *,
    tool: str,
    caller: str,
    caller_source: str,
    arguments: dict[str, Any],
    workspace_id: str | None,
    status: str,
    error: str | None = None,
    duration_ms: int | None = None,
    jev_score: float | None = None,
) -> None:
    """Append one call. Never raises: a broken audit log must not break a tool."""
    if not enabled():
        return
    try:
        cleaned = sanitize(arguments)
        query = query_text_of(arguments) if STORE_QUERY_TEXT else None
        row = (
            BRIDGE_NAME,
            caller,
            caller_source,
            tool,
            workspace_id,
            query,
            json.dumps(cleaned, ensure_ascii=False),
            status,
            _truncate(error, 2000) if error else None,
            duration_ms,
            jev_score,
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning("audit row build failed: %s: %s", type(exc).__name__, exc)
        return
    _writer.write(row)


READ_ENABLED = (
    os.environ.get("HONCHO_AUDIT_READ", "").strip().lower() in {"1", "true", "yes", "on"}
)

_SELECT = f"""
SELECT id, at, bridge, caller, caller_source, tool, workspace_id,
       query_text, arguments, status, error, duration_ms, jev_score
  FROM {SCHEMA}.tool_calls
 WHERE (%(caller)s::text IS NULL OR caller = %(caller)s::text)
   AND (%(tool)s::text   IS NULL OR tool   = %(tool)s::text)
   AND (%(status)s::text IS NULL OR status = %(status)s::text)
   AND (%(bridge)s::text IS NULL OR bridge = %(bridge)s::text)
   AND (%(hours)s::int   IS NULL OR at >= now() - %(hours)s::int * interval '1 hour')
 ORDER BY at DESC, id DESC
 LIMIT %(limit)s::int
"""

_COLUMNS = (
    "id",
    "at",
    "bridge",
    "caller",
    "caller_source",
    "tool",
    "workspace_id",
    "query_text",
    "arguments",
    "status",
    "error",
    "duration_ms",
    "jev_score",
)

_SUMMARY = f"""
SELECT status, count(*) AS n FROM {SCHEMA}.tool_calls
 WHERE (%(hours)s::int IS NULL OR at >= now() - %(hours)s::int * interval '1 hour')
 GROUP BY status
"""


def read(
    *,
    limit: int = 100,
    caller: str | None = None,
    tool: str | None = None,
    status: str | None = None,
    bridge: str | None = None,
    hours: int | None = None,
) -> dict[str, Any]:
    """Recent calls, newest first, for the owner's dashboard.

    Raises on a database failure: unlike `record`, a read that silently returns
    nothing would look exactly like "nobody asked anything".
    """
    if not enabled():
        raise RuntimeError("Audit log is not configured (HONCHO_AUDIT_DSN is unset)")
    params = {
        "limit": max(1, min(int(limit), 1000)),
        "caller": caller or None,
        "tool": tool or None,
        "status": status or None,
        "bridge": bridge or None,
        "hours": hours if hours and hours > 0 else None,
    }
    with _writer._lock:  # one connection, one lock
        # A read is the owner asking a question and waiting for it, so the back-off
        # the writer applies does not apply here.
        conn = _writer._conn_ready()
        with conn.cursor() as cur:
            cur.execute(_SELECT, params)
            rows = [dict(zip(_COLUMNS, record, strict=True)) for record in cur.fetchall()]
            cur.execute(_SUMMARY, {"hours": params["hours"]})
            summary = {str(name): int(count) for name, count in cur.fetchall()}
    for row in rows:
        at = row.get("at")
        if at is not None:
            row["at"] = at.isoformat()
    return {"rows": rows, "summary": summary, "schema": SCHEMA}
