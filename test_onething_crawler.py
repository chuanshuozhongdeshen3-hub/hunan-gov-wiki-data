import tempfile
import unittest
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from supplement_onething_guides import (
    CrawlOptions,
    OneThingCrawler,
    as_records,
    is_self_node,
    is_zero_approve,
    write_catalog,
)
from hunan_gov_full_crawler import read_json


def success(data):
    return {"code": 0, "data": data, "error": False, "msg": "成功", "success": True}


def area_node(
    name,
    area_code,
    org_id,
    org_level,
    parent_id,
    benji,
    approve_id,
):
    return {
        "orgName": name,
        "orgShowName": name,
        "areaCode": area_code,
        "orgId": org_id,
        "orgLevel": org_level,
        "parentId": parent_id,
        "benji": benji,
        "approveId": approve_id,
    }


class FakeCrawler(OneThingCrawler):
    def _post_form_proxy(self, internal_url):
        parsed = urlparse(internal_url)
        query = parse_qs(parsed.query)
        if parsed.path.endswith("/area_list"):
            key = (query["orgLevel"][0], query["parentId"][0])
            routes = {
                ("1", "1"): [
                    area_node("省本级", "439900000000", "4", "1", "1", "1", "0"),
                    area_node("长沙市", "430100000000", "6", "2", "1", "0", "2"),
                ],
                ("2", "6"): [
                    area_node("长沙市直", "430101000000", "5", "2", "6", "1", "1"),
                    area_node("芙蓉区", "430102000000", "D", "3", "6", "0", "1"),
                ],
                ("3", "D"): [
                    area_node("芙蓉区区本级", "430102999000", "DB", "3", "D", "1", "1"),
                    area_node("荷花园街道", "430102014000", "S", "4", "D", "0", "0"),
                ],
            }
            return success(routes[key])
        if parsed.path.endswith("/info"):
            area_code = query["areaCode"][0]
            checklist = {
                "430101000000": "CITY-CHECKLIST",
                "430102999000": "DISTRICT-CHECKLIST",
            }[area_code]
            return success(
                {"sxbm": "43PCN0009", "sxmc": "新生儿出生一件事", "yjsSsqdId": checklist}
            )
        raise AssertionError(internal_url)

    def _request_json(self, url, data, content_type, referer):
        parsed = urlparse(url)
        internal_url = parse_qs(parsed.query)["url"][0]
        checklist = internal_url.rsplit("/", 1)[-1]
        return success(
            {
                "sxmc": "新生儿出生一件事",
                "yjsSsqdbm": f"IMPL-{checklist}",
                "basicInfo": {"areaName": checklist},
                "materialList": [{"materialTitle": "材料"}],
                "guideCondition": [{"processingConditions": "条件"}],
                "approveList": [],
                "questionInfo": [],
                "guidePoint": [],
                "attachInfo": [],
                "onethingCatalogStandServiceGuide": {"bllctsm": "流程"},
            }
        )


class OneThingCrawlerTest(unittest.TestCase):
    def options(self, output):
        return CrawlOptions(
            output=Path(output),
            delay=0,
            jitter=0,
            timeout=1,
            retries=0,
            max_themes=0,
            max_guides=0,
            max_depth=4,
            include_zero_approve=False,
            refresh_areas=False,
            refresh_info=False,
            refresh_guides=False,
            fail_fast=True,
        )

    def test_node_flags_and_info_shapes(self):
        self.assertTrue(is_self_node({"benji": "1"}))
        self.assertFalse(is_self_node({"benji": "0"}))
        self.assertTrue(is_zero_approve({"approveId": "0"}))
        self.assertFalse(is_zero_approve({"approveId": "2"}))
        self.assertEqual(as_records(None, "test"), [])
        self.assertEqual(as_records({"a": 1}, "test"), [{"a": 1}])

    def test_recurses_area_tree_and_fetches_guides(self):
        with tempfile.TemporaryDirectory() as directory:
            crawler = FakeCrawler(self.options(directory))
            result = crawler.crawl_theme(
                {
                    "id": "43PCN0009",
                    "name": "新生儿出生一件事",
                    "type": "theme",
                    "type_name": "高效办成一件事",
                }
            )

            self.assertEqual(result["status"], "ok")
            self.assertEqual(result["area_request_count"], 3)
            self.assertEqual(result["implementation_count"], 2)
            self.assertEqual(result["unique_guide_count"], 2)
            self.assertEqual(result["pruned_zero_approve_count"], 2)
            self.assertEqual(
                {item["checklist_id"] for item in result["implementations"]},
                {"CITY-CHECKLIST", "DISTRICT-CHECKLIST"},
            )
            district = next(
                item
                for item in result["implementations"]
                if item["area_code"] == "430102999000"
            )
            self.assertEqual(district["area_path"], ["长沙市", "芙蓉区", "芙蓉区区本级"])
            self.assertTrue((Path(directory) / district["guide_raw_file"]).exists())
            self.assertTrue((Path(directory) / district["info_raw_file"]).exists())

    def test_uses_existing_cache_without_request(self):
        with tempfile.TemporaryDirectory() as directory:
            crawler = FakeCrawler(self.options(directory))
            first, path = crawler.fetch_area_list("43PCN0009", "1", "1")
            self.assertEqual(len(first["data"]), 2)

            def fail(_):
                raise AssertionError("存在缓存时不应请求网络")

            crawler._post_form_proxy = fail
            second, second_path = crawler.fetch_area_list("43PCN0009", "1", "1")
            self.assertEqual(second, first)
            self.assertEqual(second_path, path)
            self.assertEqual(crawler.cache_hits, 1)

    def test_single_theme_update_preserves_previous_catalog_entries(self):
        with tempfile.TemporaryDirectory() as directory:
            crawler = FakeCrawler(self.options(directory))

            def result(code, checklist):
                return {
                    "theme_code": code,
                    "theme_title": code,
                    "status": "ok",
                    "implementation_count": 1,
                    "implementations": [{"checklist_id": checklist}],
                }

            write_catalog(
                crawler,
                [{"id": "THEME-A"}],
                [result("THEME-A", "GUIDE-A")],
                "succeeded",
            )
            write_catalog(
                crawler,
                [{"id": "THEME-B"}],
                [result("THEME-B", "GUIDE-B")],
                "succeeded",
            )
            payload = read_json(Path(directory) / "catalog" / "onething_guides.json")
            self.assertEqual(payload["catalog_theme_count"], 2)
            self.assertEqual(payload["unique_guide_count"], 2)
            self.assertEqual(
                {item["theme_code"] for item in payload["themes"]},
                {"THEME-A", "THEME-B"},
            )


if __name__ == "__main__":
    unittest.main()
