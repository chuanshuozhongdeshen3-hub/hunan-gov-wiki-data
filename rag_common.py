#!/usr/bin/env python3
"""Shared helpers for the Hunan government-service RAG scripts."""

from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path
from typing import Any, Iterable, Iterator


HAN_RE = re.compile(r"[\u3400-\u9fff]")
ALNUM_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.:/-]*")
HEADING_RE = re.compile(r"^(#{1,6})\s+(.+?)\s*$")


def read_jsonl(path: Path) -> Iterator[dict[str, Any]]:
    with path.open("r", encoding="utf-8") as handle:
        for line_number, line in enumerate(handle, 1):
            if not line.strip():
                continue
            try:
                value = json.loads(line)
            except json.JSONDecodeError as exc:
                raise ValueError(f"{path}:{line_number}: invalid JSON: {exc}") from exc
            if not isinstance(value, dict):
                raise ValueError(f"{path}:{line_number}: JSONL row must be an object")
            yield value


def write_jsonl(path: Path, rows: Iterable[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="\n") as handle:
        for row in rows:
            handle.write(json.dumps(row, ensure_ascii=False, sort_keys=False) + "\n")


def sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def unique_strings(values: Iterable[Any], limit: int | None = None) -> list[str]:
    result: list[str] = []
    seen: set[str] = set()
    for value in values:
        text = re.sub(r"\s+", " ", str(value or "")).strip()
        key = text.casefold()
        if not text or key in seen:
            continue
        seen.add(key)
        result.append(text)
        if limit is not None and len(result) >= limit:
            break
    return result


def strip_frontmatter(markdown: str) -> str:
    if not markdown.startswith("---"):
        return markdown
    lines = markdown.splitlines()
    for index in range(1, len(lines)):
        if lines[index].strip() == "---":
            return "\n".join(lines[index + 1 :]).lstrip()
    return markdown


def normalize_markdown(markdown: str) -> str:
    """Remove low-value navigation/source sections while keeping official facts."""
    markdown = strip_frontmatter(markdown).replace("\r\n", "\n").replace("\r", "\n")
    output: list[str] = []
    skip_level: int | None = None
    excluded = {"相关页面", "数据说明"}
    for line in markdown.splitlines():
        match = HEADING_RE.match(line)
        if match:
            level = len(match.group(1))
            heading = match.group(2).strip()
            if skip_level is not None and level <= skip_level:
                skip_level = None
            if heading in excluded:
                skip_level = level
                continue
        if skip_level is None:
            output.append(line.rstrip())
    text = "\n".join(output)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


def lexical_tokens(text: str) -> list[str]:
    """Tokenize Chinese as overlapping bigrams and retain ASCII/code tokens."""
    compact_han = "".join(ch for ch in text if HAN_RE.match(ch))
    if len(compact_han) == 1:
        han_tokens = [compact_han]
    else:
        han_tokens = [compact_han[i : i + 2] for i in range(len(compact_han) - 1)]
    ascii_tokens = [match.group(0).lower() for match in ALNUM_RE.finditer(text)]
    return han_tokens + ascii_tokens


def fts_text(text: str) -> str:
    # Repeated pre-tokenized bigrams make the SQLite file unnecessarily large.
    # Presence is more useful than term frequency for these short RAG chunks.
    return " ".join(unique_strings(lexical_tokens(text)))


def split_hard(text: str, max_chars: int, overlap: int) -> list[str]:
    """Split an indivisible long block. Every returned string is <= max_chars."""
    text = text.strip()
    if not text:
        return []
    if len(text) <= max_chars:
        return [text]
    result: list[str] = []
    start = 0
    punctuation = "。！？；\n"
    while start < len(text):
        end = min(start + max_chars, len(text))
        if end < len(text):
            search_start = start + max(max_chars // 2, 1)
            candidates = [text.rfind(mark, search_start, end) for mark in punctuation]
            best = max(candidates)
            if best >= search_start:
                end = best + 1
        piece = text[start:end].strip()
        if piece:
            result.append(piece)
        if end >= len(text):
            break
        next_start = max(end - overlap, start + 1)
        start = next_start
    return result


def chunk_markdown(
    markdown: str,
    title: str,
    max_chars: int = 600,
    overlap: int = 80,
) -> list[dict[str, Any]]:
    """Split Markdown by headings/paragraphs with a hard total-character cap."""
    if max_chars < 100:
        raise ValueError("max_chars must be at least 100")
    if overlap < 0 or overlap >= max_chars // 2:
        raise ValueError("overlap must be >= 0 and less than half max_chars")

    content = normalize_markdown(markdown)
    sections: list[tuple[str, list[str]]] = []
    heading_stack: list[tuple[int, str]] = []
    current_heading = "基本信息"
    current_lines: list[str] = []

    def flush() -> None:
        nonlocal current_lines
        body = "\n".join(current_lines).strip()
        if body:
            sections.append((current_heading, [p.strip() for p in re.split(r"\n\s*\n", body) if p.strip()]))
        current_lines = []

    for line in content.splitlines():
        match = HEADING_RE.match(line)
        if match:
            level = len(match.group(1))
            name = match.group(2).strip()
            if level == 1 and name == title:
                continue
            flush()
            heading_stack[:] = [(old_level, old_name) for old_level, old_name in heading_stack if old_level < level]
            heading_stack.append((level, name))
            current_heading = " / ".join(item[1] for item in heading_stack)
        else:
            current_lines.append(line)
    flush()

    chunks: list[dict[str, Any]] = []
    for section, paragraphs in sections:
        prefix = f"事项：{title}\n章节：{section}\n"
        if len(prefix) >= max_chars:
            prefix = f"事项：{title[: max_chars // 3]}\n"
        body_limit = max_chars - len(prefix)
        pieces: list[str] = []
        buffer = ""
        for paragraph in paragraphs:
            for atom in split_hard(paragraph, body_limit, min(overlap, max(body_limit // 4, 0))):
                candidate = atom if not buffer else f"{buffer}\n\n{atom}"
                if len(candidate) <= body_limit:
                    buffer = candidate
                else:
                    if buffer:
                        pieces.append(buffer)
                    buffer = atom
        if buffer:
            pieces.append(buffer)
        for piece in pieces:
            text = (prefix + piece).strip()
            if len(text) > max_chars:
                raise AssertionError(f"chunk exceeds hard limit: {len(text)} > {max_chars}")
            chunks.append({"section": section, "text": text, "char_count": len(text)})
    if not chunks:
        fallback = f"事项：{title}"[:max_chars]
        chunks.append({"section": "基本信息", "text": fallback, "char_count": len(fallback)})
    return chunks
