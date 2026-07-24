import copy
import json
import tempfile
import unittest
from pathlib import Path

from onething_json_to_wiki import business_fingerprint, convert_onething


def guide(area, department, condition="共同条件"):
    return {
        "sxmc": "测试一件事",
        "yjsSsqdbm": f"IMPL-{area}",
        "basicInfo": {
            "areaName": area,
            "qtbmmc": department,
            "cooperateName": "协同部门",
            "zxdh": "12345",
            "completedLimit": "5",
            "isCharge": "0",
            "isIntermediary": "0",
            "isLogistics": "0",
            "reasonLink": "可网办",
        },
        "guideCondition": [{"processingConditions": condition, "pxh": 1}],
        "materialList": [
            {
                "materialTitle": "身份证明",
                "clbyx": "1",
                "attachList": [{"fjmc": "示例.pdf", "fjlj": f"/{area}/sample"}],
            }
        ],
        "approveList": [{"lbsxfwmc": "联办事项", "cnsxsx": 1}],
        "questionInfo": [{"problem": "怎么办？", "answer": "按指南办理。"}],
        "guidePoint": [
            {
                "pointName": department,
                "xxdz": f"{area}测试地址",
                "processingTime": "工作日",
            }
        ],
        "attachInfo": [{"fjmc": "流程图.png", "fjlj": f"/{area}/flow"}],
        "chargelist": [],
        "intermediarylist": "",
        "onethingCatalogStandServiceGuide": {
            "bllctsm": "申请、受理、办结",
            "cnbjsx": "5",
            "hjsxz": "1",
            "zxdh": "12345",
        },
    }


class OneThingWikiTest(unittest.TestCase):
    def test_fingerprint_excludes_region_fields(self):
        first = guide("甲区", "甲区部门")
        second = guide("乙区", "乙区部门")
        self.assertEqual(business_fingerprint(first)[0], business_fingerprint(second)[0])

        changed = copy.deepcopy(second)
        changed["guideCondition"][0]["processingConditions"] = "不同条件"
        self.assertNotEqual(business_fingerprint(first)[0], business_fingerprint(changed)[0])

    def test_converts_overview_variant_and_region_layers(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "data"
            wiki = Path(directory) / "wiki"
            raw = root / "raw" / "onething" / "guides" / "THEME1"
            raw.mkdir(parents=True)
            implementations = []
            for area_code, area_name, checklist in (
                ("430101000000", "甲区", "CHECK-A"),
                ("430102000000", "乙区", "CHECK-B"),
            ):
                raw_rel = f"raw/onething/guides/THEME1/{checklist}.json"
                (root / raw_rel).write_text(
                    json.dumps(
                        {
                            "code": 0,
                            "success": True,
                            "error": False,
                            "data": guide(area_name, f"{area_name}部门"),
                        },
                        ensure_ascii=False,
                    ),
                    encoding="utf-8",
                )
                implementations.append(
                    {
                        "theme_code": "THEME1",
                        "theme_title": "测试一件事",
                        "area_code": area_code,
                        "area_name": area_name,
                        "area_path": ["测试市", area_name],
                        "checklist_id": checklist,
                        "guide_raw_file": raw_rel,
                        "official_guide_url": f"https://example.invalid/{checklist}",
                    }
                )
            catalog = {
                "themes": [
                    {
                        "theme_code": "THEME1",
                        "theme_title": "测试一件事",
                        "status": "ok",
                        "implementations": implementations,
                    },
                    {
                        "theme_code": "THEME2",
                        "theme_title": "暂无指南一件事",
                        "status": "no_available_guide",
                        "implementations": [],
                    },
                ]
            }
            catalog_path = root / "catalog" / "onething_guides.json"
            catalog_path.parent.mkdir(parents=True)
            catalog_path.write_text(
                json.dumps(catalog, ensure_ascii=False),
                encoding="utf-8",
            )
            themes = [
                {"id": "THEME1", "name": "测试一件事", "type_name": "高效办成一件事"},
                {"id": "THEME2", "name": "暂无指南一件事", "type_name": "高效办成一件事"},
            ]
            result = convert_onething(
                root,
                wiki,
                themes,
                today="2026-07-24",
                enrich_min_areas=2,
            )
            manifest = result["manifest"]
            self.assertEqual(manifest["theme_count"], 2)
            self.assertEqual(manifest["variant_count"], 1)
            self.assertEqual(manifest["region_page_count"], 2)
            self.assertEqual(manifest["rag_record_count"], 5)
            self.assertEqual(manifest["llm_enrichment_priority_count"], 2)
            self.assertEqual(manifest["skipped_count"], 0)
            for relative in result["generated_pages"]:
                self.assertTrue((wiki / relative).is_file(), relative)
            region_rows = [
                row for row in result["rag_records"] if row["page_type"] == "theme_region"
            ]
            self.assertTrue(all(row["enrich_with_llm"] is False for row in region_rows))
            self.assertTrue(all(row["related_doc_ids"] for row in region_rows))


if __name__ == "__main__":
    unittest.main()
