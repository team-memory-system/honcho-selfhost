"""Paired retrieval benchmark for the live 768-d and shadow 1536-d stores.

The benchmark uses real user-query -> immediate assistant-answer pairs.  The
query message itself is excluded from retrieval, and only message IDs present
in both stores are scored so both systems see the same relevance universe.
"""

from __future__ import annotations

import argparse
import json
import math
import os
import statistics
import time
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

import httpx
import numpy as np
import psycopg
from pgvector.psycopg import register_vector
from psycopg import sql

OLD_MODEL = "openai/text-embedding-3-small"
NEW_MODEL = "qwen3-embedding:8b"
QUERY_INSTRUCTION = (
    "Given a personal memory search query, retrieve relevant conversation "
    "passages that answer or contextualize the query"
)
DEFAULT_EF_SEARCH = 40
SCORED_RANK_LIMIT = 10


@dataclass(frozen=True)
class Pair:
    query_id: str
    query: str
    target_id: str
    target: str
    created_at: str


@dataclass
class ArmResult:
    name: str
    ranks: list[int | None]
    search_seconds: list[float]
    embedding_seconds: float


def connect() -> psycopg.Connection[Any]:
    uri = os.environ["DB_CONNECTION_URI"].replace(
        "postgresql+psycopg://", "postgresql://"
    )
    connection = psycopg.connect(uri)
    register_vector(connection)
    return connection


def fetch_pairs(cursor: psycopg.Cursor[Any], sample_size: int) -> list[Pair]:
    cursor.execute(
        """
        WITH ordered AS (
          SELECT public_id, content, peer_name, session_name, created_at,
                 lead(public_id) OVER (
                   PARTITION BY session_name ORDER BY id
                 ) AS target_id,
                 lead(peer_name) OVER (
                   PARTITION BY session_name ORDER BY id
                 ) AS target_peer,
                 lead(content) OVER (
                   PARTITION BY session_name ORDER BY id
                 ) AS target_content
          FROM messages
          WHERE peer_name NOT LIKE 'automation_%%'
        )
        SELECT public_id, content, target_id, target_content, created_at
        FROM ordered
        WHERE peer_name = 'user_chen'
          AND target_peer LIKE 'assistant_%%'
          AND length(content) BETWEEN 30 AND 500
          AND length(target_content) BETWEEN 100 AND 5000
          AND content NOT LIKE '[Image:%%'
          AND EXISTS (
            SELECT 1 FROM message_embeddings old
            WHERE old.message_id = ordered.public_id
          )
          AND EXISTS (
            SELECT 1 FROM message_embeddings old
            WHERE old.message_id = ordered.target_id
          )
          AND EXISTS (
            SELECT 1 FROM message_embeddings_v1536_shadow new
            WHERE new.message_id = ordered.public_id
          )
          AND EXISTS (
            SELECT 1 FROM message_embeddings_v1536_shadow new
            WHERE new.message_id = ordered.target_id
          )
        ORDER BY md5(public_id || 'honcho-embedding-benchmark-v1')
        LIMIT %s
        """,
        (sample_size * 4,),
    )
    pairs: list[Pair] = []
    seen_queries: set[str] = set()
    for query_id, query, target_id, target, created_at in cursor.fetchall():
        normalized = " ".join(query.lower().split())
        if normalized in seen_queries:
            continue
        seen_queries.add(normalized)
        pairs.append(
            Pair(
                query_id=query_id,
                query=query,
                target_id=target_id,
                target=target,
                created_at=created_at.isoformat(),
            )
        )
        if len(pairs) == sample_size:
            break
    if len(pairs) != sample_size:
        raise RuntimeError(f"Requested {sample_size} pairs but found only {len(pairs)}")
    return pairs


def fetch_overlap_ids(cursor: psycopg.Cursor[Any]) -> set[str]:
    cursor.execute(
        """
        SELECT old.message_id
        FROM (SELECT DISTINCT message_id FROM message_embeddings) old
        JOIN (
          SELECT DISTINCT message_id FROM message_embeddings_v1536_shadow
        ) new USING (message_id)
        """
    )
    return {row[0] for row in cursor.fetchall()}


def embed_batches(
    client: httpx.Client,
    model: str,
    dimensions: int,
    texts: list[str],
    batch_size: int,
) -> tuple[list[np.ndarray[Any, np.dtype[np.float32]]], float]:
    vectors: list[np.ndarray[Any, np.dtype[np.float32]]] = []
    started = time.perf_counter()
    for offset in range(0, len(texts), batch_size):
        response = client.post(
            "http://127.0.0.1:11434/v1/embeddings",
            json={
                "model": model,
                "input": texts[offset : offset + batch_size],
                "dimensions": dimensions,
            },
        )
        response.raise_for_status()
        rows = sorted(response.json()["data"], key=lambda row: row["index"])
        vectors.extend(np.asarray(row["embedding"], dtype=np.float32) for row in rows)
    return vectors, time.perf_counter() - started


def retrieve_rank(
    cursor: psycopg.Cursor[Any],
    table: str,
    vector: np.ndarray[Any, np.dtype[np.float32]],
    pair: Pair,
    overlap_ids: set[str],
    search_limit: int,
) -> tuple[int | None, float]:
    if table not in {
        "message_embeddings",
        "message_embeddings_v1536_shadow",
    }:
        raise ValueError(f"Unexpected table: {table}")
    started = time.perf_counter()
    cursor.execute(
        sql.SQL(
            """
        SELECT message_id, embedding <=> %s AS distance
        FROM {}
        WHERE message_id <> %s
        ORDER BY embedding <=> %s
        LIMIT %s
        """
        ).format(sql.Identifier(table)),
        (vector, pair.query_id, vector, search_limit),
    )
    rows = cursor.fetchall()
    elapsed = time.perf_counter() - started
    ranked_ids: list[str] = []
    seen: set[str] = set()
    for message_id, _distance in rows:
        if message_id not in overlap_ids or message_id in seen:
            continue
        seen.add(message_id)
        ranked_ids.append(message_id)
        if len(ranked_ids) == SCORED_RANK_LIMIT:
            break
    try:
        return ranked_ids.index(pair.target_id) + 1, elapsed
    except ValueError:
        return None, elapsed


def summarize(result: ArmResult) -> dict[str, float | int | str]:
    count = len(result.ranks)
    reciprocal_ranks = [0.0 if rank is None else 1.0 / rank for rank in result.ranks]
    summary: dict[str, float | int | str] = {
        "name": result.name,
        "queries": count,
        "mrr_at_10": statistics.fmean(reciprocal_ranks),
        "ndcg_at_10": statistics.fmean(
            0.0 if rank is None or rank > 10 else 1.0 / math.log2(rank + 1)
            for rank in result.ranks
        ),
        "embedding_total_seconds": result.embedding_seconds,
        "embedding_ms_per_query": 1000 * result.embedding_seconds / count,
        "search_p50_ms": 1000 * statistics.median(result.search_seconds),
        "search_p95_ms": 1000
        * sorted(result.search_seconds)[max(0, math.ceil(0.95 * count) - 1)],
    }
    for cutoff in (1, 5, 10):
        summary[f"recall_at_{cutoff}"] = (
            sum(rank is not None and rank <= cutoff for rank in result.ranks) / count
        )
    return summary


def paired_comparison(
    baseline: ArmResult, challenger: ArmResult
) -> dict[str, float | int | list[float]]:
    baseline_scores = np.asarray(
        [0.0 if rank is None else 1.0 / rank for rank in baseline.ranks]
    )
    challenger_scores = np.asarray(
        [0.0 if rank is None else 1.0 / rank for rank in challenger.ranks]
    )
    baseline_hit = np.asarray(
        [rank is not None and rank <= 10 for rank in baseline.ranks], dtype=float
    )
    challenger_hit = np.asarray(
        [rank is not None and rank <= 10 for rank in challenger.ranks], dtype=float
    )
    rng = np.random.default_rng(20260713)
    indexes = rng.integers(0, len(baseline.ranks), size=(5000, len(baseline.ranks)))
    mrr_deltas = (challenger_scores - baseline_scores)[indexes].mean(axis=1)
    recall_deltas = (challenger_hit - baseline_hit)[indexes].mean(axis=1)
    baseline_only = int(np.sum((baseline_hit == 1) & (challenger_hit == 0)))
    challenger_only = int(np.sum((baseline_hit == 0) & (challenger_hit == 1)))
    discordant = baseline_only + challenger_only
    if discordant:
        tail = min(baseline_only, challenger_only)
        p_value = min(
            1.0,
            2
            * sum(math.comb(discordant, value) for value in range(tail + 1))
            / (2**discordant),
        )
    else:
        p_value = 1.0
    baseline_rank = [rank or SCORED_RANK_LIMIT + 1 for rank in baseline.ranks]
    challenger_rank = [rank or SCORED_RANK_LIMIT + 1 for rank in challenger.ranks]
    return {
        "mrr_delta": float(np.mean(challenger_scores - baseline_scores)),
        "mrr_delta_ci95": [
            float(np.quantile(mrr_deltas, 0.025)),
            float(np.quantile(mrr_deltas, 0.975)),
        ],
        "recall_at_10_delta": float(np.mean(challenger_hit - baseline_hit)),
        "recall_at_10_delta_ci95": [
            float(np.quantile(recall_deltas, 0.025)),
            float(np.quantile(recall_deltas, 0.975)),
        ],
        "challenger_rank_wins": sum(
            challenger < old
            for old, challenger in zip(baseline_rank, challenger_rank, strict=True)
        ),
        "baseline_rank_wins": sum(
            old < challenger
            for old, challenger in zip(baseline_rank, challenger_rank, strict=True)
        ),
        "rank_ties": sum(
            old == challenger
            for old, challenger in zip(baseline_rank, challenger_rank, strict=True)
        ),
        "challenger_only_recall_at_10": challenger_only,
        "baseline_only_recall_at_10": baseline_only,
        "mcnemar_exact_p": p_value,
    }


def run(
    sample_size: int,
    output: Path | None,
    ef_search: int,
) -> dict[str, Any]:
    search_limit = max(ef_search, SCORED_RANK_LIMIT)
    with connect() as connection:
        with connection.cursor() as cursor:
            cursor.execute(
                "SELECT set_config('hnsw.ef_search', %s, false)",
                (str(ef_search),),
            )
            pairs = fetch_pairs(cursor, sample_size)
            overlap_ids = fetch_overlap_ids(cursor)

        raw_queries = [pair.query for pair in pairs]
        instructed_queries = [
            f"Instruct: {QUERY_INSTRUCTION}\nQuery: {pair.query}" for pair in pairs
        ]
        with httpx.Client(timeout=180) as client:
            embed_batches(client, OLD_MODEL, 768, ["warmup"], 1)
            embed_batches(client, NEW_MODEL, 1536, ["warmup"], 1)
            old_vectors, old_embedding_seconds = embed_batches(
                client, OLD_MODEL, 768, raw_queries, 32
            )
            new_raw_vectors, new_raw_embedding_seconds = embed_batches(
                client, NEW_MODEL, 1536, raw_queries, 32
            )
            new_instructed_vectors, new_instructed_embedding_seconds = embed_batches(
                client, NEW_MODEL, 1536, instructed_queries, 32
            )

        arms = [
            (
                ArmResult("old_768_raw", [], [], old_embedding_seconds),
                "message_embeddings",
                old_vectors,
            ),
            (
                ArmResult("qwen_1536_raw", [], [], new_raw_embedding_seconds),
                "message_embeddings_v1536_shadow",
                new_raw_vectors,
            ),
            (
                ArmResult(
                    "qwen_1536_instructed",
                    [],
                    [],
                    new_instructed_embedding_seconds,
                ),
                "message_embeddings_v1536_shadow",
                new_instructed_vectors,
            ),
        ]
        with connection.cursor() as cursor:
            for pair_index, pair in enumerate(pairs):
                rotation = pair_index % len(arms)
                ordered_arms = arms[rotation:] + arms[:rotation]
                for result, table, vectors in ordered_arms:
                    rank, elapsed = retrieve_rank(
                        cursor,
                        table,
                        vectors[pair_index],
                        pair,
                        overlap_ids,
                        search_limit,
                    )
                    result.ranks.append(rank)
                    result.search_seconds.append(elapsed)

    summaries = {result.name: summarize(result) for result, _table, _v in arms}
    old_result = arms[0][0]
    raw_result = arms[1][0]
    instructed_result = arms[2][0]
    report = {
        "method": {
            "sample_size": sample_size,
            "pair_definition": "real user message -> immediate assistant reply",
            "query_message_excluded": True,
            "candidate_universe": "message IDs present in both stores",
            "hnsw_ef_search": ef_search,
            "search_limit": search_limit,
            "scored_rank_limit": SCORED_RANK_LIMIT,
            "query_instruction": QUERY_INSTRUCTION,
        },
        "summaries": summaries,
        "comparisons": {
            "qwen_raw_vs_old": paired_comparison(old_result, raw_result),
            "qwen_instructed_vs_old": paired_comparison(old_result, instructed_result),
            "qwen_instructed_vs_qwen_raw": paired_comparison(
                raw_result, instructed_result
            ),
        },
        "pairs": [asdict(pair) for pair in pairs],
        "ranks": {result.name: result.ranks for result, _table, _v in arms},
    }
    if output is not None:
        output.write_text(
            json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8"
        )
    return report


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--sample-size", type=int, default=200)
    parser.add_argument("--ef-search", type=int, default=DEFAULT_EF_SEARCH)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    if not 20 <= args.sample_size <= 1000:
        raise SystemExit("--sample-size must be between 20 and 1000")
    if not 10 <= args.ef_search <= 1000:
        raise SystemExit("--ef-search must be between 10 and 1000")
    report = run(args.sample_size, args.output, args.ef_search)
    print(
        json.dumps(
            {
                "method": report["method"],
                "summaries": report["summaries"],
                "comparisons": report["comparisons"],
            },
            ensure_ascii=False,
            indent=2,
        )
    )


if __name__ == "__main__":
    main()
