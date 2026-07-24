from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from rag_common import chunk_markdown, lexical_tokens, split_hard
from search_rag import (
    material_query,
    material_section,
    prepare_output_pages,
    url_query,
)


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


class CompleteSectionRecallTests(unittest.TestCase):
    def test_detects_material_queries(self) -> None:
        self.assertTrue(material_query("办理这个事项需要哪些材料"))
        self.assertTrue(material_query("去现场要带什么证件"))
        self.assertFalse(material_query("这个事项需要多长时间"))

    def test_matches_only_material_section_tree(self) -> None:
        self.assertTrue(material_section("申请材料"))
        self.assertTrue(material_section("申请材料 / 1. 身份证"))
        self.assertFalse(material_section("材料依据"))
        self.assertFalse(material_section("办理流程"))

    def test_complete_ordinary_materials_keep_only_target_page(self) -> None:
        pages = [
            {
                "doc_id": "service:target",
                "page_type": "government_service",
                "official_url": "https://example.test/target",
                "chunks": [{"text": "完整材料"}],
            },
            {
                "doc_id": "service:similar",
                "page_type": "government_service",
                "official_url": "https://example.test/similar",
                "chunks": [{"text": "相似事项材料"}],
            },
        ]
        recall = {"complete": True, "doc_id": "service:target"}
        result = prepare_output_pages(pages, recall, "需要哪些材料")
        self.assertEqual([page["doc_id"] for page in result], ["service:target"])
        self.assertNotIn("official_url", result[0])

    def test_onething_keeps_region_and_variant(self) -> None:
        pages = [
            {
                "doc_id": "region:1",
                "page_type": "theme_region",
                "official_url": "https://example.test/region",
            },
            {
                "doc_id": "variant:1",
                "page_type": "theme_variant",
                "official_url": "https://example.test/variant",
            },
        ]
        recall = {"complete": True, "doc_id": "variant:1"}
        result = prepare_output_pages(pages, recall, "芙蓉区需要什么材料")
        self.assertEqual([page["doc_id"] for page in result], ["region:1", "variant:1"])
        self.assertTrue(all("official_url" not in page for page in result))

    def test_url_is_returned_only_when_requested_or_forced(self) -> None:
        pages = [
            {
                "doc_id": "service:1",
                "page_type": "government_service",
                "official_url": "https://example.test/service",
            }
        ]
        self.assertTrue(url_query("请给我这个事项的官网链接"))
        self.assertFalse(url_query("这个事项怎么办"))
        requested = prepare_output_pages(pages, None, "请给我官网链接")
        self.assertEqual(requested[0]["official_url"], "https://example.test/service")
        hidden = prepare_output_pages(pages, None, "这个事项怎么办")
        self.assertNotIn("official_url", hidden[0])
        forced = prepare_output_pages(pages, None, "这个事项怎么办", include_urls=True)
        self.assertEqual(forced[0]["official_url"], "https://example.test/service")


if __name__ == "__main__":
    unittest.main()
