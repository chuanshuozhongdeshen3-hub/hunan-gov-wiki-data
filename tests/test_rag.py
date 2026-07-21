from __future__ import annotations

import unittest

from rag_common import chunk_markdown, lexical_tokens, split_hard


class ChunkingTests(unittest.TestCase):
    def test_hard_split_never_exceeds_limit(self) -> None:
        pieces = split_hard("这是一段很长的政务说明。" * 100, max_chars=120, overlap=20)
        self.assertGreater(len(pieces), 1)
        self.assertTrue(all(0 < len(piece) <= 120 for piece in pieces))

    def test_markdown_chunk_includes_context_and_respects_600(self) -> None:
        markdown = """---
title: 测试事项
---
# 测试事项

## 申请材料

### 身份材料

申请人应当提交有效身份证明。\n\n""" + "补充说明。" * 300
        chunks = chunk_markdown(markdown, "测试事项", max_chars=600, overlap=80)
        self.assertGreater(len(chunks), 1)
        self.assertTrue(all(chunk["char_count"] <= 600 for chunk in chunks))
        self.assertTrue(all(chunk["text"].startswith("事项：测试事项") for chunk in chunks))
        self.assertTrue(any("申请材料 / 身份材料" in chunk["text"] for chunk in chunks))

    def test_navigation_sections_are_excluded(self) -> None:
        markdown = "# 事项\n\n## 条件\n\n真实条件\n\n## 相关页面\n\n无关链接\n\n## 数据说明\n\n路径"
        text = "\n".join(item["text"] for item in chunk_markdown(markdown, "事项"))
        self.assertIn("真实条件", text)
        self.assertNotIn("无关链接", text)
        self.assertNotIn("路径", text)


class TokenTests(unittest.TestCase):
    def test_chinese_bigrams_and_codes(self) -> None:
        tokens = lexical_tokens("身份证换领 000709107003 ABC-12")
        self.assertIn("身份", tokens)
        self.assertIn("换领", tokens)
        self.assertIn("000709107003", tokens)
        self.assertIn("abc-12", tokens)


if __name__ == "__main__":
    unittest.main()
