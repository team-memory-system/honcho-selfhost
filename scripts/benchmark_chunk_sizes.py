"""Compare Honcho embedding chunk sizes without touching production data.

The benchmark uses real user-message -> immediate long assistant-reply pairs.
Each candidate message is scored by its best matching chunk, which mirrors
Honcho's chunk-level retrieval followed by message de-duplication.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import sqlite3
import statistics
import time
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import httpx
import numpy as np
import psycopg
import tiktoken
from pgvector.psycopg import register_vector

MODEL = "qwen3-embedding:8b"
DIMENSIONS = 1536
INSTRUCTION = (
    "Given a personal memory search query, retrieve relevant conversation "
    "passages that answer or contextualize the query"
)
DEFAULT_CHUNK_SIZES = (1024, 2048, 4096, 8192)
ENCODING = tiktoken.get_encoding("cl100k_base")


@dataclass(frozen=True)
class Pair:
    query_id: str
    query: str
    target_id: str
    target_tokens: int


def connect() -> psycopg.Connection[Any]:
    uri = os.environ.get(
        "DB_CONNECTION_URI", "postgresql://postgres:postgres@127.0.0.1:5432/postgres"
    ).replace("postgresql+psycopg://", "postgresql://")
    connection = psycopg.connect(uri)
    register_vector(connection)
    return connection


def chunk(text: str, size: int) -> list[str]:
    tokens = ENCODING.encode(text)
    if len(tokens) <= size:
        return [text]
    step = size - int(size * 0.2)
    return [ENCODING.decode(tokens[i : i + size]) for i in range(0, len(tokens), step)]


def fetch_pairs(
    cursor: psycopg.Cursor[Any], per_bucket: int, max_target_tokens: int | None
) -> list[Pair]:
    cursor.execute(
        """
        WITH ordered AS (
          SELECT id, public_id, content, token_count, peer_name, session_name,
                 lead(public_id) OVER (PARTITION BY session_name ORDER BY id) target_id,
                 lead(content) OVER (PARTITION BY session_name ORDER BY id) target,
                 lead(token_count) OVER (PARTITION BY session_name ORDER BY id) target_tokens,
                 lead(peer_name) OVER (PARTITION BY session_name ORDER BY id) target_peer
          FROM messages
          WHERE peer_name NOT LIKE 'automation_%%'
        ), eligible AS (
          SELECT *, CASE
            WHEN target_tokens <= 2048 THEN '1025_2048'
            WHEN target_tokens <= 4096 THEN '2049_4096'
            ELSE '4097_plus'
          END bucket
          FROM ordered
          WHERE peer_name = 'user_chen'
            AND target_peer LIKE 'assistant_%%'
            AND target_tokens > 1024
            AND (%s IS NULL OR target_tokens <= %s)
            AND length(content) BETWEEN 30 AND 1000
            AND content NOT LIKE '[Image:%%'
            AND EXISTS (SELECT 1 FROM message_embeddings e WHERE e.message_id=target_id)
        ), ranked AS (
          SELECT *, row_number() OVER (
            PARTITION BY bucket ORDER BY md5(public_id || 'chunk-size-bench-v1')
          ) rn
          FROM eligible
        )
        SELECT public_id, content, target_id, target_tokens
        FROM ranked WHERE rn <= %s ORDER BY bucket, rn
        """,
        (max_target_tokens, max_target_tokens, per_bucket),
    )
    return [Pair(*row) for row in cursor.fetchall()]


def fetch_candidates(
    cursor: psycopg.Cursor[Any],
    pairs: list[Pair],
    long_candidates: int,
    short_candidates: int,
    max_candidate_tokens: int | None,
) -> dict[str, tuple[str, int]]:
    forced = {p.query_id for p in pairs} | {p.target_id for p in pairs}
    cursor.execute(
        """
        SELECT public_id, content, token_count
        FROM messages m
        WHERE peer_name NOT LIKE 'automation_%%'
          AND token_count > 1024
          AND (%s IS NULL OR token_count <= %s)
          AND EXISTS (SELECT 1 FROM message_embeddings e WHERE e.message_id=m.public_id)
        ORDER BY md5(public_id || 'chunk-size-long-candidates-v1') LIMIT %s
        """
        ,
        (max_candidate_tokens, max_candidate_tokens, long_candidates),
    )
    result = {row[0]: (row[1], row[2]) for row in cursor.fetchall()}
    cursor.execute(
        """
        SELECT public_id, content, token_count
        FROM messages m
        WHERE peer_name NOT LIKE 'automation_%%'
          AND token_count <= 1024
          AND EXISTS (SELECT 1 FROM message_embeddings e WHERE e.message_id=m.public_id)
        ORDER BY md5(public_id || 'chunk-size-candidates-v1') LIMIT %s
        """,
        (short_candidates,),
    )
    result.update({row[0]: (row[1], row[2]) for row in cursor.fetchall()})
    if forced - result.keys():
        cursor.execute(
            "SELECT public_id, content, token_count FROM messages WHERE public_id=ANY(%s)",
            (list(forced - result.keys()),),
        )
        result.update({row[0]: (row[1], row[2]) for row in cursor.fetchall()})
    return result


class VectorCache:
    def __init__(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(path)
        self.db.execute(
            "CREATE TABLE IF NOT EXISTS vectors (key TEXT PRIMARY KEY, vector BLOB NOT NULL)"
        )

    @staticmethod
    def key(text: str) -> str:
        return hashlib.sha256(text.encode()).hexdigest()

    def get(self, text: str) -> np.ndarray[Any, np.dtype[np.float32]] | None:
        row = self.db.execute("SELECT vector FROM vectors WHERE key=?", (self.key(text),)).fetchone()
        return None if row is None else np.frombuffer(row[0], dtype=np.float32).copy()

    def put_many(self, texts: list[str], vectors: list[np.ndarray[Any, np.dtype[np.float32]]]) -> None:
        self.db.executemany(
            "INSERT OR REPLACE INTO vectors(key, vector) VALUES (?, ?)",
            [(self.key(text), vector.astype(np.float32).tobytes()) for text, vector in zip(texts, vectors, strict=True)],
        )
        self.db.commit()


def _embed_batch(
    endpoint: str, batch: list[str]
) -> tuple[list[str], list[np.ndarray[Any, np.dtype[np.float32]]]]:
    error = ""
    for attempt in range(1, 4):
        with httpx.Client(timeout=300) as client:
            try:
                response = client.post(
                    f"{endpoint.rstrip('/')}/v1/embeddings",
                    json={"model": MODEL, "input": batch, "dimensions": DIMENSIONS},
                )
                if not response.is_error:
                    rows = sorted(response.json()["data"], key=lambda row: row["index"])
                    return batch, [np.asarray(row["embedding"], dtype=np.float32) for row in rows]
                error = f"status={response.status_code} body={response.text[:1000]}"
            except httpx.HTTPError as exc:
                error = repr(exc)
        print(f"retrying embedding attempt {attempt}/3: {error}", flush=True)
        time.sleep(attempt)
    raise RuntimeError(
        f"Embedding request failed at {endpoint}: {error} "
        f"cl100k_tokens={[len(ENCODING.encode(text)) for text in batch]}"
    )


def embed_missing(
    texts: list[str], cache: VectorCache, batch_size: int, endpoints: list[str]
) -> None:
    unique = list(dict.fromkeys(text for text in texts if cache.get(text) is None))
    if not unique:
        return
    batches = [unique[offset : offset + batch_size] for offset in range(0, len(unique), batch_size)]
    done = 0
    with ThreadPoolExecutor(max_workers=len(endpoints)) as executor:
        futures = [
            executor.submit(_embed_batch, endpoints[index % len(endpoints)], batch)
            for index, batch in enumerate(batches)
        ]
        for future in as_completed(futures):
            batch, vectors = future.result()
            cache.put_many(batch, vectors)
            done += len(batch)
            print(f"embedded {done}/{len(unique)}", flush=True)


def summarize(ranks: list[int | None]) -> dict[str, float | int]:
    values = [0.0 if rank is None or rank > 10 else 1.0 / rank for rank in ranks]
    out: dict[str, float | int] = {
        "queries": len(ranks),
        "mrr_at_10": statistics.fmean(values),
    }
    for cutoff in (1, 5, 10):
        out[f"recall_at_{cutoff}"] = sum(
            rank is not None and rank <= cutoff for rank in ranks
        ) / len(ranks)
    return out


def score_arm(
    size: int,
    pairs: list[Pair],
    candidates: dict[str, tuple[str, int]],
    query_vectors: np.ndarray[Any, np.dtype[np.float32]],
    cache: VectorCache,
) -> tuple[list[int | None], int, float]:
    message_ids = list(candidates)
    message_index = {message_id: i for i, message_id in enumerate(message_ids)}
    chunk_texts: list[str] = []
    chunk_owners: list[int] = []
    for message_id, (content, _tokens) in candidates.items():
        parts = chunk(content, size)
        chunk_texts.extend(parts)
        chunk_owners.extend([message_index[message_id]] * len(parts))
    matrix = np.stack([cache.get(text) for text in chunk_texts])
    matrix /= np.maximum(np.linalg.norm(matrix, axis=1, keepdims=True), 1e-12)
    owners = np.asarray(chunk_owners, dtype=np.int32)
    ranks: list[int | None] = []
    started = time.perf_counter()
    for pair, query in zip(pairs, query_vectors, strict=True):
        similarities = matrix @ query
        scores = np.full(len(message_ids), -np.inf, dtype=np.float32)
        np.maximum.at(scores, owners, similarities)
        if pair.query_id in message_index:
            scores[message_index[pair.query_id]] = -np.inf
        target_score = scores[message_index[pair.target_id]]
        rank = int(np.sum(scores > target_score)) + 1
        ranks.append(rank if rank <= 10 else None)
    elapsed = time.perf_counter() - started
    return ranks, len(chunk_texts), elapsed


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--per-bucket", type=int, default=60)
    parser.add_argument("--long-candidates", type=int, default=6000)
    parser.add_argument("--short-candidates", type=int, default=5000)
    parser.add_argument("--batch-size", type=int, default=32)
    parser.add_argument(
        "--chunk-size", action="append", dest="chunk_sizes", type=int, default=[]
    )
    parser.add_argument("--max-target-tokens", type=int)
    parser.add_argument(
        "--endpoint",
        action="append",
        dest="endpoints",
        default=[],
        help="Ollama base URL; repeat to distribute batches",
    )
    parser.add_argument("--cache", type=Path, default=Path(".bench/chunk-size-vectors.sqlite"))
    parser.add_argument("--output", type=Path, default=Path(".bench/chunk-size-report.json"))
    args = parser.parse_args()

    with connect() as connection, connection.cursor() as cursor:
        pairs = fetch_pairs(cursor, args.per_bucket, args.max_target_tokens)
        candidates = fetch_candidates(
            cursor,
            pairs,
            args.long_candidates,
            args.short_candidates,
            args.max_target_tokens,
        )
        cursor.execute(
            "SELECT content, embedding FROM message_embeddings WHERE message_id=ANY(%s)",
            (list(candidates),),
        )
        production_chunks = [
            (content, np.asarray(vector, dtype=np.float32))
            for content, vector in cursor.fetchall()
        ]
    cache = VectorCache(args.cache)
    if production_chunks:
        cache.put_many(
            [content for content, _vector in production_chunks],
            [vector for _content, vector in production_chunks],
        )
    query_texts = [f"Instruct: {INSTRUCTION}\nQuery: {pair.query}" for pair in pairs]
    chunk_sizes = tuple(args.chunk_sizes or DEFAULT_CHUNK_SIZES)
    arm_texts: dict[int, list[str]] = {
        size: [part for content, _ in candidates.values() for part in chunk(content, size)]
        for size in chunk_sizes
    }
    all_texts = query_texts + [text for size in chunk_sizes for text in arm_texts[size]]
    endpoints = args.endpoints or ["http://127.0.0.1:11434"]
    embed_missing(all_texts, cache, args.batch_size, endpoints)
    query_vectors = np.stack([cache.get(text) for text in query_texts])
    query_vectors /= np.maximum(np.linalg.norm(query_vectors, axis=1, keepdims=True), 1e-12)

    ranks_by_arm: dict[int, list[int | None]] = {}
    summaries: dict[int, dict[str, float | int]] = {}
    chunks: dict[int, int] = {}
    scoring_seconds: dict[int, float] = {}
    for size in chunk_sizes:
        ranks, chunk_count, elapsed = score_arm(
            size, pairs, candidates, query_vectors, cache
        )
        ranks_by_arm[size] = ranks
        summaries[size] = summarize(ranks)
        chunks[size] = chunk_count
        scoring_seconds[size] = elapsed
        print(size, summaries[size], flush=True)

    bucket_summaries: dict[str, dict[int, dict[str, float | int]]] = defaultdict(dict)
    for label, predicate in {
        "target_1025_2048": lambda n: n <= 2048,
        "target_2049_4096": lambda n: 2048 < n <= 4096,
        "target_4097_plus": lambda n: n > 4096,
    }.items():
        indexes = [i for i, pair in enumerate(pairs) if predicate(pair.target_tokens)]
        if not indexes:
            continue
        for size in chunk_sizes:
            bucket_summaries[label][size] = summarize([ranks_by_arm[size][i] for i in indexes])

    report = {
        "method": {
            "model": MODEL,
            "dimensions": DIMENSIONS,
            "chunk_overlap": 0.2,
            "query_instruction": INSTRUCTION,
            "ranking": "exact cosine; message score is maximum chunk score",
            "pairs": len(pairs),
            "candidates": len(candidates),
            "long_candidates": sum(tokens > 1024 for _, tokens in candidates.values()),
        },
        "summaries": summaries,
        "bucket_summaries": bucket_summaries,
        "chunk_counts": chunks,
        "scoring_seconds": scoring_seconds,
        "pairs_detail": [pair.__dict__ for pair in pairs],
        "ranks": ranks_by_arm,
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({"method": report["method"], "summaries": summaries, "bucket_summaries": bucket_summaries, "chunk_counts": chunks}, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
