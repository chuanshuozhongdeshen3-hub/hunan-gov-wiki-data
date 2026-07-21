#!/usr/bin/env python3
"""Build Markdown chunks, a SQLite FTS5 index, and optional BGE embeddings."""

from __future__ import annotations

import argparse
import json
import shutil
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from rag_common import chunk_markdown, fts_text, read_jsonl, sha256_text, unique_strings, write_jsonl


DEFAULT_MODEL = "BAAI/bge-small-zh-v1.5"


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--wiki", type=Path, default=Path("gov-wiki/wiki"), help="Wiki root")
    parser.add_argument("--catalog", type=Path, help="Catalog JSONL; defaults to enriched then base catalog")
    parser.add_argument("--output", type=Path, default=Path("gov-wiki/rag"), help="RAG output directory")
    parser.add_argument("--max-chars", type=int, default=600, help="Hard maximum characters per chunk")
    parser.add_argument("--overlap", type=int, default=80, help="Overlap used only for long indivisible blocks")
    parser.add_argument("--model", default=DEFAULT_MODEL, help="Sentence Transformers model id or local path")
    parser.add_argument("--batch-size", type=int, default=32)
    parser.add_argument("--device", default=None, help="cpu, cuda, mps; auto-selected when omitted")
    parser.add_argument("--skip-embeddings", action="store_true", help="Build chunks/FTS only")
    parser.add_argument("--force", action="store_true", help="Replace an existing output directory")
    return parser.parse_args()


def resolve_catalog(wiki: Path, explicit: Path | None) -> Path:
    if explicit:
        return explicit
    enriched = wiki / "_meta" / "rag_catalog_enriched.jsonl"
    return enriched if enriched.exists() else wiki / "_meta" / "rag_catalog.jsonl"


def create_sqlite(path: Path, chunks: list[dict[str, Any]]) -> None:
    connection = sqlite3.connect(path)
    try:
        connection.executescript(
            """
            PRAGMA journal_mode = WAL;
            CREATE TABLE chunks (
                chunk_id TEXT PRIMARY KEY,
                row_index INTEGER NOT NULL UNIQUE,
                doc_id TEXT NOT NULL,
                title TEXT NOT NULL,
                section TEXT NOT NULL,
                path TEXT NOT NULL,
                page_type TEXT NOT NULL,
                answerable INTEGER NOT NULL,
                service_id TEXT,
                official_url TEXT,
                char_count INTEGER NOT NULL,
                text TEXT NOT NULL,
                aliases_json TEXT NOT NULL,
                questions_json TEXT NOT NULL
            );
            CREATE INDEX idx_chunks_doc_id ON chunks(doc_id);
            CREATE INDEX idx_chunks_service_id ON chunks(service_id);
            CREATE VIRTUAL TABLE chunks_fts USING fts5(
                chunk_id UNINDEXED,
                title_tokens,
                metadata_tokens,
                body_tokens,
                tokenize='unicode61'
            );
            """
        )
        rows = []
        fts_rows = []
        for chunk in chunks:
            rows.append(
                (
                    chunk["chunk_id"], chunk["row_index"], chunk["doc_id"], chunk["title"],
                    chunk["section"], chunk["path"], chunk["page_type"], int(chunk["answerable"]),
                    chunk.get("service_id"), chunk.get("official_url"), chunk["char_count"], chunk["text"],
                    json.dumps(chunk.get("aliases", []), ensure_ascii=False),
                    json.dumps(chunk.get("common_questions", []), ensure_ascii=False),
                )
            )
            metadata = " ".join(
                unique_strings(
                    chunk.get("aliases", [])
                    + chunk.get("common_questions", [])
                    + chunk.get("keywords", [])
                    + chunk.get("categories", [])
                    + [chunk.get("organization"), chunk.get("matter_type"), chunk.get("service_id")]
                )
            )
            fts_rows.append(
                (
                    chunk["chunk_id"],
                    fts_text(chunk["title"]),
                    fts_text(metadata),
                    fts_text(chunk["text"]),
                )
            )
        connection.executemany("INSERT INTO chunks VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)", rows)
        connection.executemany("INSERT INTO chunks_fts VALUES (?, ?, ?, ?)", fts_rows)
        connection.commit()
        connection.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    finally:
        connection.close()


def encode_embeddings(
    chunks: list[dict[str, Any]], model_name: str, output: Path, batch_size: int, device: str | None
) -> tuple[list[int], str]:
    try:
        import numpy as np
        from sentence_transformers import SentenceTransformer
    except ImportError as exc:
        raise RuntimeError(
            "Embedding dependencies are missing. Run: pip install -r requirements-rag.txt"
        ) from exc
    kwargs = {"device": device} if device else {}
    model = SentenceTransformer(model_name, **kwargs)
    texts = [chunk["embedding_text"] for chunk in chunks]
    embeddings = model.encode(
        texts,
        batch_size=batch_size,
        show_progress_bar=True,
        convert_to_numpy=True,
        normalize_embeddings=True,
    ).astype(np.float32, copy=False)
    np.save(output / "embeddings.npy", embeddings)
    return list(embeddings.shape), str(getattr(model, "device", device or "auto"))


def main() -> int:
    args = parse_args()
    wiki = args.wiki.resolve()
    catalog_path = resolve_catalog(wiki, args.catalog)
    output = args.output.resolve()
    if not wiki.is_dir():
        raise SystemExit(f"Wiki directory not found: {wiki}")
    if not catalog_path.is_file():
        raise SystemExit(f"Catalog not found: {catalog_path}")
    if output.exists():
        if not args.force:
            raise SystemExit(f"Output already exists: {output}; pass --force to replace it")
        shutil.rmtree(output)
    output.mkdir(parents=True)

    chunks: list[dict[str, Any]] = []
    skipped: list[dict[str, str]] = []
    for catalog_row in read_jsonl(catalog_path):
        relative_path = str(catalog_row.get("path", "")).replace("\\", "/")
        page_path = wiki / relative_path
        if not page_path.is_file():
            skipped.append({"doc_id": str(catalog_row.get("doc_id", "")), "reason": f"missing: {relative_path}"})
            continue
        markdown = page_path.read_text(encoding="utf-8")
        title = str(catalog_row.get("title") or page_path.stem)
        aliases = unique_strings(catalog_row.get("aliases", []), 8)
        questions = unique_strings(catalog_row.get("common_questions", []), 8)
        summary = str(catalog_row.get("search_summary") or catalog_row.get("summary") or "").strip()
        metadata_prefix = "\n".join(
            line for line in [
                f"检索摘要：{summary}" if summary else "",
                f"别名：{'；'.join(aliases)}" if aliases else "",
                f"常见问法：{'；'.join(questions)}" if questions else "",
            ] if line
        )
        doc_chunks = chunk_markdown(markdown, title, args.max_chars, args.overlap)
        for local_index, item in enumerate(doc_chunks):
            chunk_id = f"{catalog_row['doc_id']}#{local_index:03d}"
            embedding_text = item["text"]
            if metadata_prefix:
                # Always reserve some room for aliases/questions. The displayed
                # authoritative chunk remains complete in ``text``.
                metadata = metadata_prefix[: min(180, args.max_chars // 3)]
                body_budget = args.max_chars - len(metadata) - 2
                embedding_text = f"{metadata}\n\n{item['text'][:body_budget]}"
            # The indexed/displayed chunk itself is always the authoritative Wiki text.
            chunk = {
                "chunk_id": chunk_id,
                "row_index": len(chunks),
                "doc_id": catalog_row["doc_id"],
                "title": title,
                "section": item["section"],
                "path": relative_path,
                "page_type": catalog_row.get("page_type", "unknown"),
                "answerable": bool(catalog_row.get("answerable", True)),
                "service_id": catalog_row.get("service_id"),
                "official_url": catalog_row.get("official_url"),
                "service_objects": catalog_row.get("service_objects", []),
                "matter_type": catalog_row.get("matter_type"),
                "categories": catalog_row.get("categories", []),
                "organization": catalog_row.get("organization"),
                "keywords": catalog_row.get("keywords", []),
                "aliases": aliases,
                "common_questions": questions,
                "summary": summary,
                "text": item["text"],
                "embedding_text": embedding_text,
                "char_count": item["char_count"],
                "source_sha256": sha256_text(markdown),
            }
            if len(chunk["text"]) > args.max_chars or len(chunk["embedding_text"]) > args.max_chars:
                raise AssertionError(f"{chunk_id}: max_chars invariant violated")
            chunks.append(chunk)

    # Keep this audit artifact compact: embeddings and FTS source metadata are
    # already represented by embeddings.npy and keyword_index.db.
    audit_chunks = [
        {key: value for key, value in row.items() if key not in {"embedding_text", "keywords"}}
        for row in chunks
    ]
    write_jsonl(output / "documents.jsonl", audit_chunks)
    create_sqlite(output / "keyword_index.db", chunks)
    shape = None
    device = None
    if not args.skip_embeddings:
        shape, device = encode_embeddings(chunks, args.model, output, args.batch_size, args.device)

    manifest = {
        "format_version": 1,
        "created_at": datetime.now(timezone.utc).isoformat(),
        "wiki": str(wiki),
        "catalog": str(catalog_path),
        "catalog_sha256": sha256_text(catalog_path.read_text(encoding="utf-8")),
        "document_count": len({row["doc_id"] for row in chunks}),
        "chunk_count": len(chunks),
        "max_chunk_chars": max((row["char_count"] for row in chunks), default=0),
        "configured_max_chars": args.max_chars,
        "overlap": args.overlap,
        "embedding_model": None if args.skip_embeddings else args.model,
        "embedding_shape": shape,
        "embedding_device": device,
        "skipped": skipped,
    }
    (output / "index_manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    print(json.dumps(manifest, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except KeyboardInterrupt:
        print("Interrupted", file=sys.stderr)
        raise SystemExit(130)
