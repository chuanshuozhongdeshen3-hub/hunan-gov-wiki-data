#!/usr/bin/env python3
"""补充智能搜索中存在、但个人/法人分类目录中缺失的普通政务事项。

默认只处理 ``search_only_candidates.json`` 中 ``type == "approve"`` 的项目：

- 尝试抓取办理指南详情并写入 ``raw/details``；
- 合并到 ``catalog/full_catalog.json`` 和 ``enriched_catalog.json``；
- 合并附件链接到 ``catalog/asset_links.json``；
- 生成独立的补充报告，不改写原始搜索审计证据；
- 跳过 ``type == "theme"`` 的“高效办成一件事”项目。

脚本可重复执行。已经成功进入目录且详情文件存在的项目会被跳过；搜索
索引中已经失效、详情接口明确返回“查询为空”的事项只记入报告，不会合并。
"""

from __future__ import annotations

import argparse
import copy
import sys
from pathlib import Path
from typing import Any

from hunan_gov_full_crawler import (
    BASE_URL,
    Config,
    FullCrawler,
    VERSION,
    add_unique,
    atomic_write_json,
    extract_asset_links,
    official_guide_url,
    read_json,
    safe_identifier,
    utc_now,
)


SUPPLEMENT_VERSION = "1.0.0"


def nonempty(value: Any) -> bool:
    return value not in (None, "", [], {})


def infer_service_types(service_object: Any) -> list[str]:
    """把详情接口的中文服务对象转换成主目录使用的内部类型。"""
    text = str(service_object or "").replace("／", "/").strip()
    service_types: list[str] = []
    if "个人" in text or "自然人" in text:
        service_types.append("personal")
    if "法人" in text or "企业" in text or "组织" in text:
        service_types.append("legal_entity")
    if not service_types:
        raise ValueError(f"无法识别服务对象：{service_object!r}")
    return service_types


def build_catalog_record(
    candidate: dict[str, Any], detail: dict[str, Any]
) -> dict[str, Any]:
    service_id = str(candidate.get("id") or "").strip()
    safe_identifier(service_id, "候选事项 ID")
    returned_id = str(detail.get("unifyId") or "").strip()
    if returned_id != service_id:
        raise ValueError(
            f"详情 ID 不一致：请求 {service_id!r}，返回 {returned_id!r}"
        )

    candidate_title = str(candidate.get("name") or candidate.get("_title") or "").strip()
    detail_title = str(detail.get("unifyName") or "").strip()
    if not detail_title:
        raise ValueError(f"事项 {service_id} 的详情缺少名称")

    conflicts: list[dict[str, Any]] = []
    if candidate_title and candidate_title != detail_title:
        conflicts.append(
            {
                "field": "title",
                "existing": candidate_title,
                "observed": detail_title,
                "resolution": "detail_title_used",
            }
        )

    record: dict[str, Any] = {
        "service_id": service_id,
        "title": detail_title,
        "service_types": infer_service_types(detail.get("serveObject")),
        "categories": [],
        "rights_codes": [],
        "rights_ids": [],
        "business_codes": [],
        "list_flags": {
            "is_online": [],
            "transact_levels": [],
            "minimum_on_site_visits": [],
        },
        "observations": [
            {
                "source": "smart_search_supplement",
                "search_type": candidate.get("type"),
                "search_type_name": candidate.get("type_name"),
            }
        ],
        "conflicts": conflicts,
    }

    for value in (candidate.get("rights_code"), detail.get("rightsCode")):
        add_unique(record["rights_codes"], value)
    add_unique(record["rights_ids"], detail.get("rightsId"))
    for value in (candidate.get("yw_code"), detail.get("ywCode")):
        add_unique(record["business_codes"], value)
    add_unique(record["list_flags"]["is_online"], detail.get("isonline"))
    add_unique(
        record["list_flags"]["transact_levels"], detail.get("transactLevel")
    )
    add_unique(
        record["list_flags"]["minimum_on_site_visits"], detail.get("minSeq")
    )
    return record


def detail_summary(
    record: dict[str, Any], detail: dict[str, Any], asset_count: int
) -> dict[str, Any]:
    service_id = record["service_id"]
    return {
        "status": "ok",
        "raw_file": f"raw/details/{service_id[:2]}/{service_id}.json",
        "official_guide_url": official_guide_url(record),
        "title": detail.get("unifyName"),
        "service_object": detail.get("serveObject"),
        "matter_type": detail.get("typeName"),
        "implementation_level": detail.get("schemeLevels"),
        "organization": detail.get("orgName"),
        "department": detail.get("deptName") or detail.get("blbm"),
        "material_count": len(detail.get("materialList") or []),
        "result_count": len(detail.get("resultList") or []),
        "asset_link_count": asset_count,
        "has_acceptance_condition": bool(detail.get("acceptanceCondition")),
        "has_legal_basis": bool(detail.get("according")),
    }


def load_items_payload(path: Path, label: str) -> tuple[dict[str, Any], dict[str, dict[str, Any]]]:
    if not path.exists():
        raise FileNotFoundError(f"缺少{label}：{path}")
    payload = read_json(path)
    items = payload.get("items") if isinstance(payload, dict) else None
    if not isinstance(items, list):
        raise ValueError(f"{label}格式异常：items 不是数组")
    by_id: dict[str, dict[str, Any]] = {}
    for item in items:
        if not isinstance(item, dict) or not item.get("service_id"):
            raise ValueError(f"{label}中存在缺少 service_id 的项目")
        service_id = str(item["service_id"])
        if service_id in by_id:
            raise ValueError(f"{label}中 service_id 重复：{service_id}")
        by_id[service_id] = item
    return payload, by_id


def is_complete(
    service_id: str,
    full_items: dict[str, dict[str, Any]],
    enriched_items: dict[str, dict[str, Any]],
    output: Path,
) -> bool:
    enriched = enriched_items.get(service_id) or {}
    raw_file = output / "raw" / "details" / service_id[:2] / f"{service_id}.json"
    return (
        service_id in full_items
        and enriched.get("detail", {}).get("status") == "ok"
        and raw_file.exists()
    )


def merge_assets(
    existing: list[dict[str, Any]],
    replacements: dict[str, list[dict[str, Any]]],
) -> list[dict[str, Any]]:
    replaced_ids = set(replacements)
    merged = [item for item in existing if item.get("service_id") not in replaced_ids]
    for service_id in sorted(replacements):
        merged.extend(replacements[service_id])
    return merged


def build_config(args: argparse.Namespace) -> Config:
    return Config(
        output=args.output.resolve(),
        stages=("details",),
        list_rows=50,
        search_rows=50,
        delay=args.delay,
        jitter=args.jitter,
        timeout=args.timeout,
        retries=args.retries,
        max_categories_per_type=0,
        max_pages_per_scope=0,
        max_details=0,
        max_search_pages=0,
        include_unfiltered=True,
        refresh_categories=False,
        refresh_lists=False,
        refresh_details=args.refresh_details,
        refresh_search=False,
        fail_fast=args.fail_fast,
    )


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="补充智能搜索中缺失的普通政务事项详情"
    )
    parser.add_argument(
        "--output",
        type=Path,
        default=Path.cwd() / "gov-wiki",
        help="数据目录，默认当前运行目录下的 .\\gov-wiki",
    )
    parser.add_argument("--delay", type=float, default=1.0, help="请求基础间隔秒数")
    parser.add_argument("--jitter", type=float, default=0.5, help="随机附加间隔秒数")
    parser.add_argument("--timeout", type=float, default=25.0, help="请求超时秒数")
    parser.add_argument("--retries", type=int, default=2, help="失败重试次数")
    parser.add_argument(
        "--limit", type=int, default=0, help="本次最多处理数量，0表示处理全部"
    )
    parser.add_argument(
        "--dry-run", action="store_true", help="只列出目标，不请求、不修改文件"
    )
    parser.add_argument(
        "--refresh-details", action="store_true", help="忽略已有详情缓存重新请求"
    )
    parser.add_argument(
        "--fail-fast", action="store_true", help="遇到第一个请求错误时立即停止"
    )
    args = parser.parse_args()
    for name in ("delay", "jitter", "retries", "limit"):
        if getattr(args, name) < 0:
            parser.error(f"--{name.replace('_', '-')} 不能为负数")
    if args.timeout <= 0:
        parser.error("--timeout 必须大于0")
    return args


def main() -> int:
    args = parse_args()
    output = args.output.resolve()
    candidates_path = output / "audit" / "search_only_candidates.json"
    full_path = output / "catalog" / "full_catalog.json"
    enriched_path = output / "catalog" / "enriched_catalog.json"
    assets_path = output / "catalog" / "asset_links.json"

    try:
        candidates_payload = read_json(candidates_path)
        candidates = candidates_payload.get("items")
        if not isinstance(candidates, list):
            raise ValueError("候选文件格式异常：items 不是数组")
        full_payload, full_items = load_items_payload(full_path, "主目录")
        enriched_payload, enriched_items = load_items_payload(enriched_path, "详情目录")
        assets_payload = read_json(assets_path)
        existing_assets = assets_payload.get("items")
        if not isinstance(existing_assets, list):
            raise ValueError("附件索引格式异常：items 不是数组")
    except (OSError, ValueError, TypeError) as exc:
        print(f"[错误] 无法加载输入数据：{exc}", file=sys.stderr)
        return 2

    ordinary = sorted(
        (
            item
            for item in candidates
            if isinstance(item, dict)
            and str(item.get("type") or "").lower() == "approve"
            and item.get("id")
        ),
        key=lambda item: str(item["id"]),
    )
    theme_count = sum(
        1
        for item in candidates
        if isinstance(item, dict)
        and str(item.get("type") or "").lower() == "theme"
    )
    pending = [
        item
        for item in ordinary
        if not is_complete(str(item["id"]), full_items, enriched_items, output)
    ]
    already_complete = len(ordinary) - len(pending)
    targets = pending[: args.limit] if args.limit else pending

    print(f"数据目录：{output}")
    print(f"普通候选事项：{len(ordinary)}")
    print(f"已完整合并：{already_complete}")
    print(f"仍待处理：{len(pending)}")
    print(f"本次处理：{len(targets)}")
    print(f"跳过主题服务：{theme_count}")

    if args.dry_run:
        print("\n[预览] 本次不会请求或修改文件")
        for index, item in enumerate(targets, start=1):
            print(f"{index:02d}. {item['id']}  {item.get('name') or item.get('_title')}")
        return 0
    if not targets:
        print("无需补充，目录已经包含全部普通候选事项。")
        return 0

    crawler = FullCrawler(build_config(args))
    succeeded: list[str] = []
    unavailable: list[dict[str, str]] = []
    failed: list[dict[str, str]] = []
    replacement_assets: dict[str, list[dict[str, Any]]] = {}

    for index, candidate in enumerate(targets, start=1):
        service_id = str(candidate["id"])
        title = candidate.get("name") or candidate.get("_title") or ""
        print(f"[{index}/{len(targets)}] {title} ({service_id})")
        try:
            payload = crawler.fetch_detail(service_id)
            detail = payload["data"]
            record = build_catalog_record(candidate, detail)
            item_assets = extract_asset_links(detail, service_id)
            enriched_record = copy.deepcopy(record)
            enriched_record["detail"] = detail_summary(
                record, detail, len(item_assets)
            )
            full_items[service_id] = record
            enriched_items[service_id] = enriched_record
            replacement_assets[service_id] = item_assets
            succeeded.append(service_id)
        except Exception as exc:  # 逐项记录，允许其余事项继续
            issue = {
                "service_id": service_id,
                "title": str(title),
                "error_type": type(exc).__name__,
                "message": str(exc),
            }
            if "统一业务办理项查询为空" in str(exc):
                unavailable.append(issue)
                print("  [不可用] 搜索索引存在，但详情接口查询为空")
            else:
                failed.append(issue)
                print(f"  [错误] {exc}", file=sys.stderr)
                if args.fail_fast:
                    break

    finished_at = utc_now()
    if succeeded:
        source = full_payload.setdefault("source", {})
        if isinstance(source, dict):
            source.setdefault(
                "smart_search",
                f"{BASE_URL}/ywtb/gov-search/index.html#/things",
            )
        supplement_metadata = {
            "version": SUPPLEMENT_VERSION,
            "updated_at": finished_at,
            "candidate_source": "audit/search_only_candidates.json",
            "ordinary_candidate_count": len(ordinary),
            "theme_candidate_count_excluded": theme_count,
        }
        full_payload["generated_at"] = finished_at
        full_payload["supplement"] = supplement_metadata
        full_payload["items"] = sorted(
            full_items.values(), key=lambda item: item["service_id"]
        )
        enriched_payload["generated_at"] = finished_at
        enriched_payload["supplement"] = supplement_metadata
        enriched_payload["items"] = sorted(
            enriched_items.values(), key=lambda item: item["service_id"]
        )
        assets_payload["generated_at"] = finished_at
        assets_payload["items"] = merge_assets(
            existing_assets, replacement_assets
        )

        # 先准备所有内存结果，再分别原子替换，避免写出半个 JSON 文件。
        atomic_write_json(full_path, full_payload)
        atomic_write_json(enriched_path, enriched_payload)
        atomic_write_json(assets_path, assets_payload)

    remaining_after_run = max(0, len(pending) - len(succeeded))
    report = {
        "schema": "hunan-gov-search-supplement-report/1.0",
        "supplement_version": SUPPLEMENT_VERSION,
        "crawler_version": VERSION,
        "generated_at": finished_at,
        "candidate_file": "audit/search_only_candidates.json",
        "policy": "approve_items_merged_theme_items_excluded",
        "ordinary_candidate_count": len(ordinary),
        "theme_candidate_count_excluded": theme_count,
        "already_complete_before_run": already_complete,
        "selected_this_run": len(targets),
        "succeeded_count": len(succeeded),
        "unavailable_count": len(unavailable),
        "failed_count": len(failed),
        "remaining_count": remaining_after_run,
        "network_requests": crawler.network_requests,
        "cache_hits": crawler.cache_hits,
        "succeeded_ids": succeeded,
        "unavailable_items": unavailable,
        "failures": failed,
        "catalog_item_count_after_run": len(full_items),
    }
    atomic_write_json(output / "reports" / "supplement_search_items.json", report)

    print("\n补充完成")
    print(
        f"成功：{len(succeeded)}；详情不可用：{len(unavailable)}；"
        f"其他失败：{len(failed)}；未合并：{remaining_after_run}"
    )
    print(f"补充后目录事项数：{len(full_items)}")
    print(f"报告：{output / 'reports' / 'supplement_search_items.json'}")
    return 0 if not failed else 2


if __name__ == "__main__":
    raise SystemExit(main())
