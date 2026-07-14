"""Prepare, rehearse, and execute the local 768 -> 1536 embedding cutover.

The live cutover is intentionally guarded.  ``rehearse`` exercises the same
rename/default/index path in an isolated schema; ``prepare`` only makes the
shadow message table schema-compatible with Honcho.  A public cutover or
rollback additionally requires ``--confirm-public-cutover``.
"""

from __future__ import annotations

import argparse
import os
from dataclasses import dataclass
from typing import Any, Final

import psycopg
from psycopg import sql

# pyright: reportImplicitStringConcatenation=false

REHEARSAL_SCHEMA: Final = "embedding_cutover_rehearsal"
SHADOW_TABLE: Final = "message_embeddings_v1536_shadow"
BACKUP_TABLE: Final = "message_embeddings_v768_backup"


@dataclass(frozen=True)
class Rename:
    current: str
    backup: str
    shadow: str


MESSAGE_INDEXES: Final = (
    Rename(
        "ix_message_embeddings_created_at",
        "ix_message_embeddings_created_at_v768_backup",
        "ix_message_embeddings_v1536_shadow_created_at",
    ),
    Rename(
        "ix_message_embeddings_embedding_hnsw",
        "ix_message_embeddings_embedding_hnsw_v768_backup",
        "ix_message_embeddings_v1536_shadow_embedding_hnsw",
    ),
    Rename(
        "ix_message_embeddings_message_id",
        "ix_message_embeddings_message_id_v768_backup",
        "ix_message_embeddings_v1536_shadow_source",
    ),
    Rename(
        "ix_message_embeddings_peer_name",
        "ix_message_embeddings_peer_name_v768_backup",
        "ix_message_embeddings_v1536_shadow_peer_name",
    ),
    Rename(
        "ix_message_embeddings_session_name",
        "ix_message_embeddings_session_name_v768_backup",
        "ix_message_embeddings_v1536_shadow_session_name",
    ),
    Rename(
        "ix_message_embeddings_sync_state",
        "ix_message_embeddings_sync_state_v768_backup",
        "ix_message_embeddings_v1536_shadow_sync_state",
    ),
    Rename(
        "ix_message_embeddings_sync_state_last_sync_at",
        "ix_message_embeddings_sync_state_last_sync_at_v768_backup",
        "ix_message_embeddings_v1536_shadow_sync_state_last_sync_at",
    ),
    Rename(
        "ix_message_embeddings_workspace_name",
        "ix_message_embeddings_workspace_name_v768_backup",
        "ix_message_embeddings_v1536_shadow_workspace_name",
    ),
)

MESSAGE_CONSTRAINTS: Final = (
    Rename(
        "pk_message_embeddings",
        "pk_message_embeddings_v768_backup",
        "message_embeddings_v1536_shadow_pkey",
    ),
    Rename(
        "fk_message_embeddings_message_id_messages",
        "fk_message_embeddings_message_id_messages_v768_backup",
        "fk_message_embeddings_v1536_message_id_messages",
    ),
    Rename(
        "fk_message_embeddings_peer_name_workspace_name_peers",
        "fk_message_embeddings_peer_workspace_v768_backup",
        "fk_message_embeddings_v1536_peer_workspace",
    ),
    Rename(
        "fk_message_embeddings_session_name_workspace_name_sessions",
        "fk_message_embeddings_session_workspace_v768_backup",
        "fk_message_embeddings_v1536_session_workspace",
    ),
    Rename(
        "fk_message_embeddings_workspace_name_workspaces",
        "fk_message_embeddings_workspace_v768_backup",
        "fk_message_embeddings_v1536_workspace",
    ),
)


def connect() -> psycopg.Connection[Any]:
    uri = os.environ["DB_CONNECTION_URI"].replace(
        "postgresql+psycopg://", "postgresql://"
    )
    return psycopg.connect(uri, autocommit=True)


def qname(schema: str, name: str) -> sql.Composed:
    return sql.SQL("{}.{}").format(sql.Identifier(schema), sql.Identifier(name))


def relation_exists(cursor: psycopg.Cursor[Any], schema: str, name: str) -> bool:
    cursor.execute("SELECT to_regclass(%s)", (f"{schema}.{name}",))
    row = cursor.fetchone()
    return bool(row and row[0])


def vector_dim(
    cursor: psycopg.Cursor[Any], schema: str, table: str, column: str
) -> int | None:
    cursor.execute(
        """
        SELECT a.atttypmod
        FROM pg_attribute a
        JOIN pg_class c ON c.oid = a.attrelid
        JOIN pg_namespace n ON n.oid = c.relnamespace
        WHERE n.nspname = %s AND c.relname = %s AND a.attname = %s
          AND NOT a.attisdropped
        """,
        (schema, table, column),
    )
    row = cursor.fetchone()
    return None if row is None else int(row[0])


def rename_index(cursor: psycopg.Cursor[Any], schema: str, old: str, new: str) -> None:
    if not relation_exists(cursor, schema, old):
        raise RuntimeError(f"Missing index {schema}.{old}")
    cursor.execute(
        sql.SQL("ALTER INDEX {} RENAME TO {}").format(
            qname(schema, old), sql.Identifier(new)
        )
    )


def constraint_exists(
    cursor: psycopg.Cursor[Any], schema: str, table: str, name: str
) -> bool:
    cursor.execute(
        """
        SELECT EXISTS (
          SELECT 1 FROM pg_constraint con
          JOIN pg_class c ON c.oid = con.conrelid
          JOIN pg_namespace n ON n.oid = c.relnamespace
          WHERE n.nspname = %s AND c.relname = %s AND con.conname = %s
        )
        """,
        (schema, table, name),
    )
    row = cursor.fetchone()
    return bool(row and row[0])


def rename_constraint(
    cursor: psycopg.Cursor[Any], schema: str, table: str, old: str, new: str
) -> None:
    if not constraint_exists(cursor, schema, table, old):
        raise RuntimeError(f"Missing constraint {schema}.{table}.{old}")
    cursor.execute(
        sql.SQL("ALTER TABLE {} RENAME CONSTRAINT {} TO {}").format(
            qname(schema, table), sql.Identifier(old), sql.Identifier(new)
        )
    )


def _prepare_indexes(cursor: psycopg.Cursor[Any], schema: str) -> None:
    definitions = {
        "ix_message_embeddings_v1536_shadow_created_at": "created_at",
        "ix_message_embeddings_v1536_shadow_peer_name": "peer_name",
        "ix_message_embeddings_v1536_shadow_session_name": "session_name",
        "ix_message_embeddings_v1536_shadow_sync_state": "sync_state",
        "ix_message_embeddings_v1536_shadow_workspace_name": "workspace_name",
    }
    for name, column in definitions.items():
        cursor.execute(
            sql.SQL("CREATE INDEX CONCURRENTLY IF NOT EXISTS {} ON {} ({})").format(
                sql.Identifier(name),
                qname(schema, SHADOW_TABLE),
                sql.Identifier(column),
            )
        )
    cursor.execute(
        sql.SQL(
            "CREATE INDEX CONCURRENTLY IF NOT EXISTS {} ON {}"
            " (sync_state, last_sync_at)"
        ).format(
            sql.Identifier(
                "ix_message_embeddings_v1536_shadow_sync_state_last_sync_at"
            ),
            qname(schema, SHADOW_TABLE),
        )
    )


def _add_fk(
    cursor: psycopg.Cursor[Any],
    schema: str,
    name: str,
    columns: str,
    reference: str,
) -> None:
    if constraint_exists(cursor, schema, SHADOW_TABLE, name):
        return
    cursor.execute(
        sql.SQL(
            "ALTER TABLE {} ADD CONSTRAINT {} FOREIGN KEY ({}) {} NOT VALID"
        ).format(
            qname(schema, SHADOW_TABLE),
            sql.Identifier(name),
            sql.SQL(columns),  # pyright: ignore[reportArgumentType]
            sql.SQL(reference),  # pyright: ignore[reportArgumentType]
        )
    )


def prepare_shadow(connection: psycopg.Connection[Any], schema: str) -> None:
    with connection.cursor() as cursor:
        if not relation_exists(cursor, schema, SHADOW_TABLE):
            raise RuntimeError(f"Missing {schema}.{SHADOW_TABLE}")
        cursor.execute(
            sql.SQL(
                "ALTER TABLE {}"
                " ALTER COLUMN embedding DROP NOT NULL,"
                " ALTER COLUMN created_at SET DEFAULT now(),"
                " ALTER COLUMN sync_state SET DEFAULT 'pending',"
                " ALTER COLUMN chunk_index DROP NOT NULL"
            ).format(qname(schema, SHADOW_TABLE))
        )
        _prepare_indexes(cursor, schema)
        _add_fk(
            cursor,
            schema,
            "fk_message_embeddings_v1536_message_id_messages",
            "message_id",
            "REFERENCES public.messages(public_id) ON DELETE CASCADE",
        )
        _add_fk(
            cursor,
            schema,
            "fk_message_embeddings_v1536_workspace",
            "workspace_name",
            "REFERENCES public.workspaces(name)",
        )
        _add_fk(
            cursor,
            schema,
            "fk_message_embeddings_v1536_session_workspace",
            "session_name, workspace_name",
            "REFERENCES public.sessions(name, workspace_name)",
        )
        _add_fk(
            cursor,
            schema,
            "fk_message_embeddings_v1536_peer_workspace",
            "peer_name, workspace_name",
            "REFERENCES public.peers(name, workspace_name)",
        )
        for item in MESSAGE_CONSTRAINTS[1:]:
            cursor.execute(
                sql.SQL("ALTER TABLE {} VALIDATE CONSTRAINT {}").format(
                    qname(schema, SHADOW_TABLE), sql.Identifier(item.shadow)
                )
            )


def _assert_cutover_ready(
    cursor: psycopg.Cursor[Any], schema: str, require_caught_up: bool
) -> None:
    expected = {
        ("message_embeddings", "embedding"): 768,
        (SHADOW_TABLE, "embedding"): 1536,
        ("documents", "embedding"): 768,
        ("documents", "embedding_v1536"): 1536,
    }
    for (table, column), dimension in expected.items():
        actual = vector_dim(cursor, schema, table, column)
        if actual != dimension:
            raise RuntimeError(
                f"Expected {schema}.{table}.{column} vector({dimension}), got {actual}"
            )
    if relation_exists(cursor, schema, BACKUP_TABLE):
        raise RuntimeError(f"{schema}.{BACKUP_TABLE} already exists")
    for item in (*MESSAGE_INDEXES,):
        if not relation_exists(cursor, schema, item.current):
            raise RuntimeError(f"Missing index {schema}.{item.current}")
        if not relation_exists(cursor, schema, item.shadow):
            raise RuntimeError(f"Missing index {schema}.{item.shadow}")
    for item in MESSAGE_CONSTRAINTS:
        if not constraint_exists(cursor, schema, "message_embeddings", item.current):
            raise RuntimeError(f"Missing constraint {item.current}")
        if not constraint_exists(cursor, schema, SHADOW_TABLE, item.shadow):
            raise RuntimeError(f"Missing constraint {item.shadow}")
    cursor.execute(
        """
        SELECT count(*) FROM pg_index i
        JOIN pg_class c ON c.oid = i.indexrelid
        JOIN pg_namespace n ON n.oid = c.relnamespace
        WHERE n.nspname = %s AND NOT i.indisvalid
        """,
        (schema,),
    )
    invalid_row = cursor.fetchone()
    if invalid_row is None:
        raise RuntimeError("Failed to inspect invalid indexes")
    invalid = int(invalid_row[0])
    if invalid:
        raise RuntimeError(f"Schema {schema} has {invalid} invalid indexes")
    if require_caught_up:
        cursor.execute(
            """
            SELECT
              (SELECT count(*) FROM messages WHERE btrim(content) <> '')
                - (SELECT count(DISTINCT message_id)
                   FROM message_embeddings_v1536_shadow),
              (SELECT count(*) FROM documents)
                - (SELECT count(embedding_v1536) FROM documents)
            """
        )
        pending_row = cursor.fetchone()
        if pending_row is None:
            raise RuntimeError("Failed to inspect backfill counts")
        message_pending, document_pending = map(int, pending_row)
        if message_pending or document_pending:
            raise RuntimeError(
                "Backfill is not caught up: "
                f"messages={message_pending}, documents={document_pending}"
            )


def cutover(
    connection: psycopg.Connection[Any], schema: str, *, require_caught_up: bool
) -> None:
    with connection.cursor() as cursor:
        _assert_cutover_ready(cursor, schema, require_caught_up)
    with connection.transaction(), connection.cursor() as cursor:
        cursor.execute("SET LOCAL lock_timeout = '5s'")
        cursor.execute(
            sql.SQL("LOCK TABLE {}, {}, {} IN ACCESS EXCLUSIVE MODE").format(
                qname(schema, "message_embeddings"),
                qname(schema, SHADOW_TABLE),
                qname(schema, "documents"),
            )
        )
        for item in MESSAGE_CONSTRAINTS:
            rename_constraint(
                cursor,
                schema,
                "message_embeddings",
                item.current,
                item.backup,
            )
            rename_constraint(cursor, schema, SHADOW_TABLE, item.shadow, item.current)
        for item in MESSAGE_INDEXES:
            rename_index(cursor, schema, item.current, item.backup)
            rename_index(cursor, schema, item.shadow, item.current)
        cursor.execute(
            sql.SQL("ALTER TABLE {} RENAME TO {}").format(
                qname(schema, "message_embeddings"), sql.Identifier(BACKUP_TABLE)
            )
        )
        cursor.execute(
            sql.SQL("ALTER TABLE {} RENAME TO message_embeddings").format(
                qname(schema, SHADOW_TABLE)
            )
        )
        rename_index(
            cursor,
            schema,
            "ix_documents_embedding_hnsw",
            "ix_documents_embedding_hnsw_v768_backup",
        )
        rename_index(
            cursor,
            schema,
            "ix_documents_embedding_v1536_hnsw",
            "ix_documents_embedding_hnsw",
        )
        cursor.execute(
            sql.SQL(
                "ALTER TABLE {} RENAME COLUMN embedding TO embedding_v768_backup"
            ).format(qname(schema, "documents"))
        )
        cursor.execute(
            sql.SQL("ALTER TABLE {} RENAME COLUMN embedding_v1536 TO embedding").format(
                qname(schema, "documents")
            )
        )


def rollback(connection: psycopg.Connection[Any], schema: str) -> None:
    with connection.cursor() as cursor:
        if vector_dim(cursor, schema, "message_embeddings", "embedding") != 1536:
            raise RuntimeError("Current message embedding table is not vector(1536)")
        if vector_dim(cursor, schema, BACKUP_TABLE, "embedding") != 768:
            raise RuntimeError("768 backup message table is missing")
        if vector_dim(cursor, schema, "documents", "embedding") != 1536:
            raise RuntimeError("Current document embedding column is not vector(1536)")
    with connection.transaction(), connection.cursor() as cursor:
        cursor.execute("SET LOCAL lock_timeout = '5s'")
        cursor.execute(
            sql.SQL("LOCK TABLE {}, {}, {} IN ACCESS EXCLUSIVE MODE").format(
                qname(schema, "message_embeddings"),
                qname(schema, BACKUP_TABLE),
                qname(schema, "documents"),
            )
        )
        for item in MESSAGE_CONSTRAINTS:
            rename_constraint(
                cursor,
                schema,
                "message_embeddings",
                item.current,
                item.shadow,
            )
            rename_constraint(cursor, schema, BACKUP_TABLE, item.backup, item.current)
        for item in MESSAGE_INDEXES:
            rename_index(cursor, schema, item.current, item.shadow)
            rename_index(cursor, schema, item.backup, item.current)
        cursor.execute(
            sql.SQL("ALTER TABLE {} RENAME TO {}").format(
                qname(schema, "message_embeddings"), sql.Identifier(SHADOW_TABLE)
            )
        )
        cursor.execute(
            sql.SQL("ALTER TABLE {} RENAME TO message_embeddings").format(
                qname(schema, BACKUP_TABLE)
            )
        )
        rename_index(
            cursor,
            schema,
            "ix_documents_embedding_hnsw",
            "ix_documents_embedding_v1536_hnsw",
        )
        rename_index(
            cursor,
            schema,
            "ix_documents_embedding_hnsw_v768_backup",
            "ix_documents_embedding_hnsw",
        )
        cursor.execute(
            sql.SQL("ALTER TABLE {} RENAME COLUMN embedding TO embedding_v1536").format(
                qname(schema, "documents")
            )
        )
        cursor.execute(
            sql.SQL(
                "ALTER TABLE {} RENAME COLUMN embedding_v768_backup TO embedding"
            ).format(qname(schema, "documents"))
        )


def _clone_rehearsal_tables(cursor: psycopg.Cursor[Any], schema: str) -> None:
    cursor.execute(
        sql.SQL("DROP SCHEMA IF EXISTS {} CASCADE").format(sql.Identifier(schema))
    )
    cursor.execute(sql.SQL("CREATE SCHEMA {}").format(sql.Identifier(schema)))
    for table in ("message_embeddings", SHADOW_TABLE, "documents"):
        cursor.execute(
            sql.SQL("CREATE TABLE {} (LIKE public.{} INCLUDING ALL)").format(
                qname(schema, table), sql.Identifier(table)
            )
        )
    # LIKE preserves index definitions but PostgreSQL generates new names.
    # Normalize them so rehearsal exercises the exact production rename path.
    rename_constraint(
        cursor,
        schema,
        "message_embeddings",
        "message_embeddings_pkey",
        "pk_message_embeddings",
    )
    cloned_index_names = {
        "message_embeddings_created_at_idx": "ix_message_embeddings_created_at",
        "message_embeddings_embedding_idx": "ix_message_embeddings_embedding_hnsw",
        "message_embeddings_message_id_idx": "ix_message_embeddings_message_id",
        "message_embeddings_peer_name_idx": "ix_message_embeddings_peer_name",
        "message_embeddings_session_name_idx": "ix_message_embeddings_session_name",
        "message_embeddings_sync_state_idx": "ix_message_embeddings_sync_state",
        "message_embeddings_sync_state_last_sync_at_idx": (
            "ix_message_embeddings_sync_state_last_sync_at"
        ),
        "message_embeddings_workspace_name_idx": (
            "ix_message_embeddings_workspace_name"
        ),
        "message_embeddings_v1536_shadow_embedding_idx": (
            "ix_message_embeddings_v1536_shadow_embedding_hnsw"
        ),
        "message_embeddings_v1536_shadow_message_id_idx": (
            "ix_message_embeddings_v1536_shadow_source"
        ),
        "documents_embedding_idx": "ix_documents_embedding_hnsw",
        "documents_embedding_v1536_idx": "ix_documents_embedding_v1536_hnsw",
    }
    for generated, production in cloned_index_names.items():
        rename_index(cursor, schema, generated, production)
    old_foreign_keys = (
        (
            "fk_message_embeddings_message_id_messages",
            "FOREIGN KEY (message_id) REFERENCES public.messages(public_id)"
            " ON DELETE CASCADE",
        ),
        (
            "fk_message_embeddings_workspace_name_workspaces",
            "FOREIGN KEY (workspace_name) REFERENCES public.workspaces(name)",
        ),
        (
            "fk_message_embeddings_session_name_workspace_name_sessions",
            "FOREIGN KEY (session_name, workspace_name)"
            " REFERENCES public.sessions(name, workspace_name)",
        ),
        (
            "fk_message_embeddings_peer_name_workspace_name_peers",
            "FOREIGN KEY (peer_name, workspace_name)"
            " REFERENCES public.peers(name, workspace_name)",
        ),
    )
    for name, definition in old_foreign_keys:
        cursor.execute(
            sql.SQL("ALTER TABLE {} ADD CONSTRAINT {} {}").format(
                qname(schema, "message_embeddings"),
                sql.Identifier(name),
                sql.SQL(definition),  # pyright: ignore[reportArgumentType]
            )
        )
    cursor.execute(
        sql.SQL(
            "INSERT INTO {} SELECT old.* FROM public.message_embeddings old "
            "WHERE EXISTS (SELECT 1 FROM public.{} new WHERE new.message_id=old.message_id) "
            "ORDER BY old.id LIMIT 200"
        ).format(qname(schema, "message_embeddings"), sql.Identifier(SHADOW_TABLE))
    )
    cursor.execute(
        sql.SQL(
            "INSERT INTO {} SELECT new.* FROM public.{} new "
            "WHERE EXISTS (SELECT 1 FROM {} old WHERE old.message_id=new.message_id) "
            "ORDER BY new.id LIMIT 200"
        ).format(
            qname(schema, SHADOW_TABLE),
            sql.Identifier(SHADOW_TABLE),
            qname(schema, "message_embeddings"),
        )
    )
    cursor.execute(
        sql.SQL(
            "INSERT INTO {} SELECT * FROM public.documents "
            "WHERE embedding IS NOT NULL AND embedding_v1536 IS NOT NULL "
            "ORDER BY id LIMIT 200"
        ).format(qname(schema, "documents"))
    )
    for table in ("message_embeddings", SHADOW_TABLE):
        cursor.execute(
            sql.SQL(
                "SELECT setval(pg_get_serial_sequence(%s, 'id'),"
                " COALESCE((SELECT max(id) FROM {}), 1), true)"
            ).format(qname(schema, table)),
            (f"{schema}.{table}",),
        )


def _assert_dimensions(cursor: psycopg.Cursor[Any], schema: str, expected: int) -> None:
    for table in ("message_embeddings", "documents"):
        actual = vector_dim(cursor, schema, table, "embedding")
        if actual != expected:
            raise RuntimeError(
                f"{schema}.{table}.embedding is {actual}, expected {expected}"
            )


def rehearse(connection: psycopg.Connection[Any], keep_schema: bool) -> None:
    schema = REHEARSAL_SCHEMA
    with connection.cursor() as cursor:
        _clone_rehearsal_tables(cursor, schema)
    prepare_shadow(connection, schema)
    cutover(connection, schema, require_caught_up=False)
    with connection.cursor() as cursor:
        _assert_dimensions(cursor, schema, 1536)
        cursor.execute(
            sql.SQL(
                "INSERT INTO {}"
                " (content, embedding, message_id, workspace_name, session_name, peer_name) "
                "SELECT 'cutover rehearsal', NULL, m.public_id, m.workspace_name,"
                " m.session_name, m.peer_name FROM public.messages m "
                "WHERE btrim(m.content) <> '' LIMIT 1 RETURNING id, created_at, sync_state"
            ).format(qname(schema, "message_embeddings"))
        )
        row = cursor.fetchone()
        if row is None or row[1] is None or row[2] != "pending":
            raise RuntimeError("Post-cutover message insert defaults are incompatible")
        cursor.execute(
            sql.SQL("DELETE FROM {} WHERE id=%s").format(
                qname(schema, "message_embeddings")
            ),
            (row[0],),
        )
        cursor.execute("SET enable_seqscan = off")
        cursor.execute(
            sql.SQL(
                "EXPLAIN (FORMAT JSON) SELECT id FROM {}"
                " WHERE embedding IS NOT NULL ORDER BY embedding <=>"
                " (SELECT embedding FROM {} WHERE embedding IS NOT NULL LIMIT 1) LIMIT 10"
            ).format(qname(schema, "documents"), qname(schema, "documents"))
        )
        plan = cursor.fetchone()
        cursor.execute("RESET enable_seqscan")
        if plan is None or "ix_documents_embedding_hnsw" not in str(plan[0]):
            raise RuntimeError("Post-cutover document search did not use HNSW")
    rollback(connection, schema)
    with connection.cursor() as cursor:
        _assert_dimensions(cursor, schema, 768)
        if not keep_schema:
            cursor.execute(
                sql.SQL("DROP SCHEMA {} CASCADE").format(sql.Identifier(schema))
            )


def status(connection: psycopg.Connection[Any], schema: str) -> None:
    with connection.cursor() as cursor:
        for table, column in (
            ("message_embeddings", "embedding"),
            (SHADOW_TABLE, "embedding"),
            (BACKUP_TABLE, "embedding"),
            ("documents", "embedding"),
            ("documents", "embedding_v1536"),
            ("documents", "embedding_v768_backup"),
        ):
            dimension = vector_dim(cursor, schema, table, column)
            if dimension is not None:
                print(f"{schema}.{table}.{column}=vector({dimension})")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "action",
        choices=("status", "check", "prepare", "rehearse", "cutover", "rollback"),
    )
    parser.add_argument("--schema", default="public")
    parser.add_argument("--keep-rehearsal", action="store_true")
    parser.add_argument("--allow-pending", action="store_true")
    parser.add_argument("--confirm-public-cutover", action="store_true")
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    if (
        args.action in {"cutover", "rollback"}
        and args.schema == "public"
        and not args.confirm_public_cutover
    ):
        raise SystemExit(
            "Refusing public cutover/rollback without --confirm-public-cutover"
        )
    with connect() as connection:
        if args.action == "status":
            status(connection, args.schema)
        elif args.action == "check":
            with connection.cursor() as cursor:
                _assert_cutover_ready(
                    cursor,
                    args.schema,
                    require_caught_up=not args.allow_pending,
                )
        elif args.action == "prepare":
            prepare_shadow(connection, args.schema)
        elif args.action == "rehearse":
            rehearse(connection, args.keep_rehearsal)
        elif args.action == "cutover":
            cutover(
                connection,
                args.schema,
                require_caught_up=args.schema == "public",
            )
        else:
            rollback(connection, args.schema)


if __name__ == "__main__":
    main()
