#!/usr/bin/env python3
"""Hybrid exact + Chinese bigram BM25 + embedding retrieval for Hermes."""

from __future__ import annotations

import argparse
import json
import re
import sqlite3
import sys
from collections import defaultdict
from pathlib import Path
from typing import Any

from rag_common import lexical_tokens, unique_strings


DEFAULT_QUERY_INSTRUCTION = "为这个句子生成表示以用于检索相关文章："


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("query", nargs="?", help="Natural-language query")
    parser.add_argument("--query", dest="query_option", help="Natural-language query (PowerShell-friendly)")
    parser.add_argument("--index", type=Path, default=Path("gov-wiki/rag"))
    parser.add_argument("--top-pages", type=int, default=2)
    parser.add_argument("--chunks-per-page", type=int, default=3)
    parser.add_argument("--candidate-limit", type=int, default=100)
    parser.add_argument("--model", help="Override model from index_manifest.json")
    parser.add_argument("--device", default=None)
    parser.add_argument("--no-vector", action="store_true")
    parser.add_argument("--query-instruction", default=DEFAULT_QUERY_INSTRUCTION)
    parser.add_argument("--pretty", action="store_true")
    return parser.parse_args()


def fts_ranking(connection: sqlite3.Connection, query: str, limit: int) -> list[int]:
    tokens = unique_strings(lexical_tokens(query))
    if not tokens:
        return []
    # Tokens are produced internally; retain only the expected safe character set.
    tokens = [re.sub(r"[^\w\u3400-\u9fff.:/-]", "", token) for token in tokens]
    tokens = [token for token in tokens if token]
    expression = " OR ".join(f'"{token}"' for token in tokens)
    rows = connection.execute(
        """
        SELECT c.row_index
        FROM chunks_fts f
        JOIN chunks c ON c.chunk_id = f.chunk_id
        WHERE chunks_fts MATCH ?
        ORDER BY bm25(chunks_fts, 0.0, 4.0, 2.0, 1.0)
        LIMIT ?
        """,
        (expression, limit),
    ).fetchall()
    return [int(row[0]) for row in rows]


def exact_ranking(connection: sqlite3.Connection, query: str, limit: int) -> list[int]:
    normalized = re.sub(r"\s+", "", query).casefold()
    if not normalized:
        return []
    rows = connection.execute(
        "SELECT row_index, title, service_id, aliases_json, questions_json FROM chunks"
    ).fetchall()
    scored_by_doc: dict[str, tuple[float, int]] = {}
    for row_index, title, service_id, aliases_json, questions_json in rows:
        title_n = re.sub(r"\s+", "", title or "").casefold()
        service_n = re.sub(r"\s+", "", service_id or "").casefold()
        aliases = json.loads(aliases_json or "[]")
        questions = json.loads(questions_json or "[]")
        score = 0.0
        if normalized == title_n:
            score = 10.0
        elif normalized and normalized in title_n:
            score = 7.0
        elif title_n and title_n in normalized:
            score = 6.0
        if service_n and (normalized == service_n or service_n in normalized):
            score = max(score, 9.0)
        for alias in aliases:
            alias_n = re.sub(r"\s+", "", str(alias)).casefold()
            if normalized == alias_n:
                score = max(score, 8.0)
            elif alias_n and (alias_n in normalized or normalized in alias_n):
                score = max(score, 5.0)
        for question in questions:
            question_n = re.sub(r"\s+", "", str(question)).casefold()
            if normalized == question_n:
                score = max(score, 4.0)
        if score:
            # Exact matching boosts the page, not all of its chunks. Adding every
            # chunk would make early sections outrank the section matching intent.
            doc_key = f"{title}\x00{service_id or ''}"
            old = scored_by_doc.get(doc_key)
            candidate = (score, int(row_index))
            if old is None or candidate[0] > old[0] or (candidate[0] == old[0] and candidate[1] < old[1]):
                scored_by_doc[doc_key] = candidate
    scored = sorted(scored_by_doc.values(), key=lambda item: (-item[0], item[1]))
    return [row_index for _, row_index in scored[:limit]]


def vector_ranking(index: Path, query: str, model_name: str, device: str | None, limit: int, instruction: str) -> list[int]:
    try:
        import numpy as np
        from sentence_transformers import SentenceTransformer
    except ImportError as exc:
        raise RuntimeError("Vector dependencies missing. Run: pip install -r requirements-rag.txt") from exc
    embeddings_path = index / "embeddings.npy"
    if not embeddings_path.exists():
        return []
    embeddings = np.load(embeddings_path, mmap_mode="r")
    kwargs = {"device": device} if device else {}
    model = SentenceTransformer(model_name, **kwargs)
    query_text = f"{instruction}{query}" if instruction else query
    query_vector = model.encode(
        [query_text], convert_to_numpy=True, normalize_embeddings=True, show_progress_bar=False
    )[0].astype(np.float32, copy=False)
    scores = embeddings @ query_vector
    count = min(limit, len(scores))
    if count <= 0:
        return []
    candidate_indices = np.argpartition(-scores, count - 1)[:count]
    candidate_indices = candidate_indices[np.argsort(-scores[candidate_indices])]
    return [int(index_value) for index_value in candidate_indices]


def reciprocal_rank_fusion(rankings: dict[str, list[int]], k: int = 60) -> dict[int, float]:
    weights = {"exact": 2.5, "fts": 1.2, "vector": 1.0}
    scores: dict[int, float] = defaultdict(float)
    for name, ranking in rankings.items():
        weight = weights.get(name, 1.0)
        for rank, row_index in enumerate(ranking, 1):
            scores[row_index] += weight / (k + rank)
    return dict(scores)


def fetch_rows(connection: sqlite3.Connection, row_indices: list[int]) -> dict[int, dict[str, Any]]:
    if not row_indices:
        return {}
    placeholders = ",".join("?" for _ in row_indices)
    rows = connection.execute(
        f"""
        SELECT row_index, chunk_id, doc_id, title, section, path, page_type, answerable,
               service_id, official_url, char_count, text, aliases_json, questions_json
        FROM chunks WHERE row_index IN ({placeholders})
        """,
        row_indices,
    ).fetchall()
    result = {}
    for row in rows:
        result[int(row[0])] = {
            "row_index": int(row[0]), "chunk_id": row[1], "doc_id": row[2], "title": row[3],
            "section": row[4], "path": row[5], "page_type": row[6], "answerable": bool(row[7]),
            "service_id": row[8], "official_url": row[9], "char_count": int(row[10]), "text": row[11],
            "aliases": json.loads(row[12] or "[]"), "common_questions": json.loads(row[13] or "[]"),
        }
    return result


def section_intent_boost(query: str, section: str) -> float:
    rules = [
        (("材料", "资料", "证件"), ("申请材料", "材料")),
        (("条件", "要求", "资格", "能不能"), ("受理条件", "条件")),
        (("流程", "步骤", "怎么办", "如何办"), ("办理流程", "流程")),
        (("多久", "时间", "时限"), ("时限",)),
        (("费用", "收费", "多少钱"), ("费用",)),
        (("结果", "证书", "拿到什么"), ("办理结果",)),
        (("依据", "法律", "法规"), ("法律依据",)),
    ]
    for query_words, section_words in rules:
        if any(word in query for word in query_words) and any(word in section for word in section_words):
            return 0.025
    return 0.0


def merge_pages(rows: dict[int, dict[str, Any]], scores: dict[int, float], query: str, top_pages: int, chunks_per_page: int) -> list[dict[str, Any]]:
    grouped: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row_index, score in scores.items():
        if row_index not in rows:
            continue
        item = dict(rows[row_index])
        item["score"] = score + section_intent_boost(query, item["section"])
        grouped[item["doc_id"]].append(item)
    pages: list[dict[str, Any]] = []
    for doc_id, items in grouped.items():
        items.sort(key=lambda item: (-item["score"], item["row_index"]))
        best = items[0]
        selected = items[:chunks_per_page]
        page_score = sum(item["score"] / (position + 1) for position, item in enumerate(selected))
        pages.append(
            {
                "doc_id": doc_id,
                "title": best["title"],
                "path": best["path"],
                "page_type": best["page_type"],
                "answerable": best["answerable"],
                "service_id": best["service_id"],
                "official_url": best["official_url"],
                "score": round(page_score, 8),
                "chunks": [
                    {
                        "chunk_id": item["chunk_id"],
                        "section": item["section"],
                        "score": round(item["score"], 8),
                        "text": item["text"],
                    }
                    for item in selected
                ],
            }
        )
    pages.sort(key=lambda page: (-page["score"], page["title"]))
    return pages[:top_pages]


def main() -> int:
    args = parse_args()
    query = (args.query_option or args.query or "").strip()
    if not query:
        raise SystemExit("Provide a query, e.g. --query '身份证到期如何换领'")
    index = args.index.resolve()
    manifest_path = index / "index_manifest.json"
    database_path = index / "keyword_index.db"
    if not manifest_path.is_file() or not database_path.is_file():
        raise SystemExit(f"Invalid index directory: {index}")
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    model_name = args.model or manifest.get("embedding_model")

    connection = sqlite3.connect(database_path)
    try:
        rankings = {
            "exact": exact_ranking(connection, query, args.candidate_limit),
            "fts": fts_ranking(connection, query, args.candidate_limit),
        }
        vector_warning = None
        if not args.no_vector and model_name and (index / "embeddings.npy").exists():
            try:
                rankings["vector"] = vector_ranking(
                    index, query, model_name, args.device, args.candidate_limit, args.query_instruction
                )
            except Exception as exc:
                vector_warning = str(exc)
        scores = reciprocal_rank_fusion(rankings)
        rows = fetch_rows(connection, list(scores))
        pages = merge_pages(rows, scores, query, args.top_pages, args.chunks_per_page)
    finally:
        connection.close()

    output = {
        "query": query,
        "retrieval": {"methods": list(rankings), "candidate_counts": {key: len(value) for key, value in rankings.items()}},
        "warning": vector_warning,
        "pages": pages,
        "answer_policy": (
            "Use only returned Wiki text. Cite official_url. If answerable is false, do not infer requirements; "
            "direct the user to the official page."
        ),
    }
    print(json.dumps(output, ensure_ascii=False, indent=2 if args.pretty else None))
    return 0 if pages else 2


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except KeyboardInterrupt:
        print("Interrupted", file=sys.stderr)
        raise SystemExit(130)
