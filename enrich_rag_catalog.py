#!/usr/bin/env python3
"""Use DeepSeek to add retrieval-only aliases, questions, and summaries."""

from __future__ import annotations

import argparse
import json
import os
import random
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from rag_common import normalize_markdown, read_jsonl, sha256_text, unique_strings, write_jsonl


PROMPT_VERSION = "hunan-retrieval-metadata-v1"
DEFAULT_MODEL = os.environ.get("DEEPSEEK_MODEL", "deepseek-v4-flash")
SYSTEM_PROMPT = """你是湖南政务服务知识库的检索元数据编辑器。
你只能根据用户提供的事项标题、现有元数据和Wiki正文，生成用于检索的辅助文本，不能新增、推断或更改任何办理事实。
请返回严格的json对象，不要返回Markdown。格式示例：
{
  "aliases": ["口语别名1", "简称2"],
  "common_questions": ["群众可能提出的问题1？", "问题2？"],
  "search_summary": "不超过100个汉字的检索摘要"
}
要求：
1. aliases为0至6项，只写确实等价或常用的简称/口语说法，不得创造新事项。
2. common_questions为3至5项，覆盖“怎么办、需要什么、去哪里办”等自然问法，但不得在问题里假定正文没有的事实。
3. search_summary只概括事项用途、服务对象和办理机构，不写正文没有的材料、条件、费用或时限。
4. 不要把其他相似事项混入当前事项。
5. 如果页面标记为不可回答，只生成用于找到官方入口的问法，不生成具体办理要求。
"""


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--wiki", type=Path, default=Path("gov-wiki/wiki"))
    parser.add_argument("--input", type=Path, help="Defaults to wiki/_meta/rag_catalog.jsonl")
    parser.add_argument("--output", type=Path, help="Defaults to wiki/_meta/rag_catalog_enriched.jsonl")
    parser.add_argument("--cache", type=Path, help="Append-only successful-response cache")
    parser.add_argument("--model", default=DEFAULT_MODEL)
    parser.add_argument("--base-url", default=os.environ.get("DEEPSEEK_BASE_URL", "https://api.deepseek.com"))
    parser.add_argument("--max-source-chars", type=int, default=6000)
    parser.add_argument("--max-tokens", type=int, default=700)
    parser.add_argument("--retries", type=int, default=5)
    parser.add_argument("--request-interval", type=float, default=0.2)
    parser.add_argument("--limit", type=int, default=0, help="Process at most N uncached rows; 0 means all")
    parser.add_argument("--start", type=int, default=0, help="Skip this many catalog rows")
    parser.add_argument("--dry-run", action="store_true", help="Show one prompt without calling the API")
    return parser.parse_args()


def cache_key(row: dict[str, Any], markdown: str, model: str) -> str:
    raw = "\n".join([PROMPT_VERSION, model, str(row.get("doc_id", "")), sha256_text(markdown)])
    return sha256_text(raw)


def make_user_prompt(row: dict[str, Any], markdown: str, max_chars: int) -> str:
    metadata = {
        "doc_id": row.get("doc_id"),
        "title": row.get("title"),
        "page_type": row.get("page_type"),
        "answerable": row.get("answerable", True),
        "service_objects": row.get("service_objects", []),
        "matter_type": row.get("matter_type"),
        "categories": row.get("categories", []),
        "organization": row.get("organization"),
        "existing_summary": row.get("summary"),
        "existing_keywords": row.get("keywords", [])[:30],
    }
    source = normalize_markdown(markdown)[:max_chars]
    return (
        "请为以下单个政务页面生成检索元数据并输出json。\n\n"
        f"现有元数据：\n{json.dumps(metadata, ensure_ascii=False)}\n\n"
        f"Wiki正文：\n{source}"
    )


def validate_result(value: Any) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ValueError("response must be a JSON object")
    aliases = unique_strings(value.get("aliases", []), 6)
    questions = unique_strings(value.get("common_questions", []), 5)
    summary = " ".join(str(value.get("search_summary", "")).split())[:150]
    if not summary:
        raise ValueError("search_summary is empty")
    if any(len(item) > 80 for item in aliases):
        raise ValueError("alias is unreasonably long")
    if any(len(item) > 150 for item in questions):
        raise ValueError("question is unreasonably long")
    return {"aliases": aliases, "common_questions": questions, "search_summary": summary}


def call_deepseek(client: Any, args: argparse.Namespace, prompt: str) -> tuple[dict[str, Any], dict[str, Any]]:
    last_error: Exception | None = None
    for attempt in range(args.retries + 1):
        try:
            response = client.chat.completions.create(
                model=args.model,
                messages=[
                    {"role": "system", "content": SYSTEM_PROMPT},
                    {"role": "user", "content": prompt},
                ],
                response_format={"type": "json_object"},
                temperature=0.1,
                max_tokens=args.max_tokens,
                stream=False,
            )
            content = response.choices[0].message.content
            if not content:
                raise ValueError("DeepSeek returned empty content")
            parsed = validate_result(json.loads(content))
            usage = getattr(response, "usage", None)
            usage_dict = {
                "prompt_tokens": getattr(usage, "prompt_tokens", None),
                "completion_tokens": getattr(usage, "completion_tokens", None),
                "total_tokens": getattr(usage, "total_tokens", None),
            }
            return parsed, usage_dict
        except Exception as exc:  # SDK exception classes vary by installed version.
            last_error = exc
            if attempt >= args.retries:
                break
            delay = min(2**attempt, 30) + random.random()
            print(f"API attempt {attempt + 1} failed: {exc}; retrying in {delay:.1f}s", file=sys.stderr)
            time.sleep(delay)
    assert last_error is not None
    raise last_error


def load_cache(path: Path) -> dict[str, dict[str, Any]]:
    if not path.exists():
        return {}
    result: dict[str, dict[str, Any]] = {}
    for row in read_jsonl(path):
        key = row.get("cache_key")
        if key:
            result[str(key)] = row
    return result


def append_cache(path: Path, row: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8", newline="\n") as handle:
        handle.write(json.dumps(row, ensure_ascii=False) + "\n")
        handle.flush()


def main() -> int:
    args = parse_args()
    wiki = args.wiki.resolve()
    input_path = (args.input or wiki / "_meta" / "rag_catalog.jsonl").resolve()
    output_path = (args.output or wiki / "_meta" / "rag_catalog_enriched.jsonl").resolve()
    cache_path = (args.cache or output_path.with_suffix(".cache.jsonl")).resolve()
    rows = list(read_jsonl(input_path))
    cache = load_cache(cache_path)

    if args.dry_run:
        row = rows[args.start]
        page_path = wiki / str(row["path"])
        markdown = page_path.read_text(encoding="utf-8")
        print(SYSTEM_PROMPT)
        print("\n--- USER PROMPT ---\n")
        print(make_user_prompt(row, markdown, args.max_source_chars))
        return 0

    api_key = os.environ.get("DEEPSEEK_API_KEY")
    if not api_key:
        raise SystemExit("DEEPSEEK_API_KEY is not set. In PowerShell: $env:DEEPSEEK_API_KEY='your-key'")
    try:
        from openai import OpenAI
    except ImportError as exc:
        raise SystemExit("OpenAI SDK is missing. Run: pip install -r requirements-rag.txt") from exc
    client = OpenAI(api_key=api_key, base_url=args.base_url)

    processed = 0
    failures: list[dict[str, str]] = []
    enriched_rows: list[dict[str, Any]] = []
    for index, row in enumerate(rows):
        if index < args.start:
            enriched_rows.append(row)
            continue
        page_path = wiki / str(row.get("path", ""))
        if not page_path.is_file():
            failures.append({"doc_id": str(row.get("doc_id", "")), "error": f"missing {page_path}"})
            enriched_rows.append(row)
            continue
        markdown = page_path.read_text(encoding="utf-8")
        key = cache_key(row, markdown, args.model)
        cached = cache.get(key)
        if cached:
            result = cached["result"]
        elif args.limit and processed >= args.limit:
            enriched_rows.append(row)
            continue
        else:
            try:
                prompt = make_user_prompt(row, markdown, args.max_source_chars)
                result, usage = call_deepseek(client, args, prompt)
                cached = {
                    "cache_key": key,
                    "doc_id": row.get("doc_id"),
                    "model": args.model,
                    "prompt_version": PROMPT_VERSION,
                    "created_at": datetime.now(timezone.utc).isoformat(),
                    "usage": usage,
                    "result": result,
                }
                append_cache(cache_path, cached)
                cache[key] = cached
                processed += 1
                print(f"[{index + 1}/{len(rows)}] enriched {row.get('title')}")
                if args.request_interval:
                    time.sleep(args.request_interval)
            except Exception as exc:
                failures.append({"doc_id": str(row.get("doc_id", "")), "error": str(exc)})
                enriched_rows.append(row)
                print(f"[{index + 1}/{len(rows)}] FAILED {row.get('title')}: {exc}", file=sys.stderr)
                continue
        merged = dict(row)
        merged.update(result)
        merged["enrichment"] = {
            "model": cached.get("model", args.model),
            "prompt_version": cached.get("prompt_version", PROMPT_VERSION),
            "source_sha256": sha256_text(markdown),
        }
        enriched_rows.append(merged)

    write_jsonl(output_path, enriched_rows)
    report = {
        "input_rows": len(rows),
        "output_rows": len(enriched_rows),
        "new_api_calls": processed,
        "cached_rows": sum(1 for row in enriched_rows if "enrichment" in row) - processed,
        "failures": failures,
        "output": str(output_path),
        "cache": str(cache_path),
    }
    report_path = output_path.with_suffix(".report.json")
    report_path.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 1 if failures else 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except KeyboardInterrupt:
        print("Interrupted; completed API responses remain in the cache.", file=sys.stderr)
        raise SystemExit(130)
