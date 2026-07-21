#!/usr/bin/env python3
"""湖南省政务服务知识库全量爬虫。

范围约定：
- 仅抓湖南政务服务网省级入口公开展示的统一事项；
- 个人服务、法人服务分别按全部主题分页抓取，并额外抓取无主题过滤全集；
- 以 unifyId 为主键去重，个人/法人和多主题归属均保留；
- 办理指南详情全量抓取；附件只保存官网返回的描述、路径和可确认链接；
- 智能搜索空关键词结果只生成差异审计，不并入主目录。

特点：标准库实现、低频串行、原始 JSON 缓存、断点续传、阶段化运行、
增量刷新、测试上限、失败报告和分页异常检查。
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import random
import sys
import time
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Iterator
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen


VERSION = "1.0.1"
BASE_URL = "https://zwfw-new.hunan.gov.cn"
CATALOG_PROXY = "/hnywtb/anony"
SEARCH_PROXY = "/v1/ywtbServlet"

SERVICE_TYPES = {
    "personal": "1",
    "legal_entity": "2",
}
SERVICE_QUERY_TYPE = {
    "personal": "gr",
    "legal_entity": "fr",
}
VALID_STAGES = {"catalog", "details", "search"}


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def compact_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


def atomic_write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    with temporary.open("w", encoding="utf-8") as file:
        json.dump(value, file, ensure_ascii=False, indent=2)
        file.write("\n")
    os.replace(temporary, path)


def read_json(path: Path) -> Any:
    with path.open("r", encoding="utf-8") as file:
        return json.load(file)


def add_unique(target: list[Any], value: Any) -> None:
    if value not in (None, "") and value not in target:
        target.append(value)


def safe_identifier(value: str, label: str) -> str:
    safe = "".join(char for char in value if char.isalnum() or char in "-_")
    if not safe or safe != value:
        raise ValueError(f"不安全的 {label}: {value!r}")
    return safe


def page_digest(items: Iterable[dict[str, Any]], id_field: str) -> str:
    identifiers = [str(item.get(id_field) or "") for item in items]
    return hashlib.sha256("\n".join(identifiers).encode("utf-8")).hexdigest()[:16]


def first_value(values: list[Any]) -> Any:
    return values[0] if values else None


@dataclass
class Config:
    output: Path
    stages: tuple[str, ...]
    list_rows: int
    search_rows: int
    delay: float
    jitter: float
    timeout: float
    retries: int
    max_categories_per_type: int
    max_pages_per_scope: int
    max_details: int
    max_search_pages: int
    include_unfiltered: bool
    refresh_categories: bool
    refresh_lists: bool
    refresh_details: bool
    refresh_search: bool
    fail_fast: bool

    @property
    def is_limited_run(self) -> bool:
        return any(
            (
                self.max_categories_per_type,
                self.max_pages_per_scope,
                self.max_details,
                self.max_search_pages,
            )
        )


class FullCrawler:
    def __init__(self, config: Config) -> None:
        self.config = config
        self.last_request_at = 0.0
        self.network_requests = 0
        self.cache_hits = 0
        self.errors: list[dict[str, Any]] = []
        self.warnings: list[dict[str, Any]] = []
        self.run_started_at = utc_now()

    def checkpoint(self, stage: str, current: str, **progress: Any) -> None:
        atomic_write_json(
            self.config.output / "state" / "checkpoint.json",
            {
                "crawler_version": VERSION,
                "updated_at": utc_now(),
                "stage": stage,
                "current": current,
                "progress": progress,
                "network_requests": self.network_requests,
                "cache_hits": self.cache_hits,
                "error_count": len(self.errors),
                "warning_count": len(self.warnings),
            },
        )

    def record_error(self, stage: str, target: str, exc: Exception) -> None:
        error = {
            "time": utc_now(),
            "stage": stage,
            "target": target,
            "error_type": type(exc).__name__,
            "message": str(exc),
        }
        self.errors.append(error)
        print(f"  [错误] {stage} / {target}: {exc}", file=sys.stderr)
        if self.config.fail_fast:
            raise exc

    def record_warning(self, stage: str, target: str, message: str) -> None:
        warning = {
            "time": utc_now(),
            "stage": stage,
            "target": target,
            "message": message,
        }
        self.warnings.append(warning)
        print(f"  [警告] {stage} / {target}: {message}", file=sys.stderr)

    def _throttle(self) -> None:
        if not self.last_request_at:
            return
        target = self.config.delay + random.uniform(0, self.config.jitter)
        elapsed = time.monotonic() - self.last_request_at
        if elapsed < target:
            time.sleep(target - elapsed)

    def _post_query(self, path: str, params: dict[str, str]) -> Any:
        query = urlencode(params)
        url = f"{BASE_URL}{path}?{query}"
        headers = {
            "Accept": "application/json, text/plain, */*",
            "User-Agent": (
                f"HunanGovWikiCrawler/{VERSION} "
                "(public-government-data; low-rate; resumable)"
            ),
            "Referer": f"{BASE_URL}/hnywtb/service/index.html",
        }

        for attempt in range(self.config.retries + 1):
            self._throttle()
            request = Request(url, data=b"", headers=headers, method="POST")
            try:
                with urlopen(request, timeout=self.config.timeout) as response:
                    raw = response.read()
                    charset = response.headers.get_content_charset() or "utf-8"
                self.last_request_at = time.monotonic()
                self.network_requests += 1
                return json.loads(raw.decode(charset, errors="replace"))
            except HTTPError as exc:
                self.last_request_at = time.monotonic()
                retryable = exc.code in {408, 429, 500, 502, 503, 504}
                if not retryable or attempt >= self.config.retries:
                    raise RuntimeError(f"HTTP {exc.code}: {url}") from exc
            except (URLError, TimeoutError, json.JSONDecodeError) as exc:
                self.last_request_at = time.monotonic()
                if attempt >= self.config.retries:
                    raise RuntimeError(f"请求或 JSON 解析失败: {url}: {exc}") from exc

            wait_seconds = min(2**attempt, 8)
            print(
                f"  请求失败，{wait_seconds} 秒后重试 "
                f"({attempt + 1}/{self.config.retries})"
            )
            time.sleep(wait_seconds)

        raise AssertionError("unreachable")

    def _cached_call(
        self,
        cache_path: Path,
        path: str,
        params: dict[str, str],
        refresh: bool,
    ) -> Any:
        if cache_path.exists() and not refresh:
            self.cache_hits += 1
            return read_json(cache_path)
        payload = self._post_query(path, params)
        atomic_write_json(cache_path, payload)
        return payload

    @staticmethod
    def require_success(payload: Any, label: str) -> None:
        if not isinstance(payload, dict):
            raise RuntimeError(f"{label} 返回值不是 JSON 对象")
        if payload.get("success") is False or payload.get("error") is True:
            raise RuntimeError(f"{label} 返回失败: {payload.get('msg', payload)}")

    def fetch_categories(self, service_type: str) -> list[dict[str, Any]]:
        cache_path = (
            self.config.output / "raw" / "categories" / f"{service_type}.json"
        )
        payload = self._cached_call(
            cache_path,
            CATALOG_PROXY,
            {
                "action": "toCatalogHttpGet",
                "url": (
                    "/unifyTaskToWt/getZtflByTask"
                    f"?type={SERVICE_TYPES[service_type]}"
                ),
            },
            self.config.refresh_categories,
        )
        self.require_success(payload, f"{service_type} 主题分类")
        categories = payload.get("data") or []
        if not isinstance(categories, list):
            raise RuntimeError(f"{service_type} 主题分类格式异常")
        return categories

    def fetch_list_page(
        self,
        service_type: str,
        category_code: str,
        page: int,
    ) -> dict[str, Any]:
        safe_category = safe_identifier(category_code, "主题代码")
        cache_path = (
            self.config.output
            / "raw"
            / "lists"
            / service_type
            / safe_category
            / f"rows_{self.config.list_rows}_page_{page:05d}.json"
        )
        request_data: dict[str, str] = {
            "type": SERVICE_TYPES[service_type],
            "unifyName": "",
        }
        if category_code != "all":
            request_data["ztfl"] = category_code
        payload = self._cached_call(
            cache_path,
            CATALOG_PROXY,
            {
                "action": "toCatalogHttpPost",
                "url": (
                    "/unifyTaskToWt/getUnifyTaskByPage"
                    f"?page={page}&rows={self.config.list_rows}"
                ),
                "data": compact_json(request_data),
            },
            self.config.refresh_lists,
        )
        self.require_success(payload, f"{service_type}/{category_code}/第{page}页")
        data = payload.get("data") or {}
        if not isinstance(data.get("contents", []), list):
            raise RuntimeError("列表响应 contents 不是数组")
        return payload

    def fetch_detail(self, unify_id: str) -> dict[str, Any]:
        safe_id = safe_identifier(unify_id, "unifyId")
        cache_path = (
            self.config.output / "raw" / "details" / safe_id[:2] / f"{safe_id}.json"
        )
        payload = self._cached_call(
            cache_path,
            CATALOG_PROXY,
            {
                "action": "toCatalogHttpGet",
                "url": f"/unifyTaskToWt/getUnifyTaskInfo?unifyId={unify_id}",
            },
            self.config.refresh_details,
        )
        self.require_success(payload, f"事项详情 {unify_id}")
        if not isinstance(payload.get("data"), dict):
            raise RuntimeError("详情 data 不是对象")
        return payload

    def fetch_search_page(self, page: int) -> dict[str, Any]:
        cache_path = (
            self.config.output
            / "raw"
            / "search_audit"
            / f"rows_{self.config.search_rows}_page_{page:05d}.json"
        )
        payload = self._cached_call(
            cache_path,
            SEARCH_PROXY,
            {
                "action": "toHttpGet",
                "url": (
                    "/smart/search/v1/service/search/real_time_highlight"
                    f"?keyword=&page={page}&rows={self.config.search_rows}&type=0"
                ),
            },
            self.config.refresh_search,
        )
        if not isinstance(payload, dict) or not isinstance(payload.get("datas", []), list):
            raise RuntimeError(f"智能搜索第 {page} 页格式异常")
        return payload


def category_map(categories: Iterable[dict[str, Any]]) -> dict[str, str]:
    return {
        str(item.get("ztflCode")): str(item.get("ztflName"))
        for item in categories
        if item.get("ztflCode") is not None
    }


def new_catalog_record(item: dict[str, Any]) -> dict[str, Any]:
    return {
        "service_id": str(item.get("unifyId") or ""),
        "title": item.get("unifyName") or "",
        "service_types": [],
        "categories": [],
        "rights_codes": [],
        "rights_ids": [],
        "business_codes": [],
        "list_flags": {
            "is_online": [],
            "transact_levels": [],
            "minimum_on_site_visits": [],
        },
        "observations": [],
        "conflicts": [],
    }


def merge_list_item(
    catalog: dict[str, dict[str, Any]],
    item: dict[str, Any],
    service_type: str,
    category_code: str,
    category_name: str,
    page: int,
) -> bool:
    unify_id = str(item.get("unifyId") or "").strip()
    if not unify_id:
        return False
    record = catalog.setdefault(unify_id, new_catalog_record(item))

    title = item.get("unifyName") or ""
    if title and record["title"] and title != record["title"]:
        add_unique(
            record["conflicts"],
            {"field": "title", "existing": record["title"], "observed": title},
        )
    elif title:
        record["title"] = title

    add_unique(record["service_types"], service_type)
    if category_code != "all":
        add_unique(
            record["categories"],
            {
                "service_type": service_type,
                "code": category_code,
                "name": category_name,
            },
        )
    add_unique(record["rights_codes"], item.get("rightsCode"))
    add_unique(record["rights_ids"], item.get("rightsId"))
    add_unique(record["business_codes"], item.get("ywCode"))
    add_unique(record["list_flags"]["is_online"], item.get("isonline"))
    add_unique(record["list_flags"]["transact_levels"], item.get("transactLevel"))
    add_unique(record["list_flags"]["minimum_on_site_visits"], item.get("minSeq"))
    add_unique(
        record["observations"],
        {
            "service_type": service_type,
            "category_code": category_code,
            "page": page,
        },
    )
    return True


def official_guide_url(record: dict[str, Any]) -> str:
    query: dict[str, str] = {"id": record["service_id"]}
    service_types = record.get("service_types") or []
    if service_types:
        query["type"] = SERVICE_QUERY_TYPE[service_types[0]]
    rights_code = first_value(record.get("rights_codes") or [])
    business_code = first_value(record.get("business_codes") or [])
    if rights_code:
        query["rightsCode"] = str(rights_code)
    if business_code:
        query["ywCode"] = str(business_code)
    return f"{BASE_URL}/hnywtb/service/guide.html?{urlencode(query)}"


def asset_url(path: Any, bucket_name: Any) -> str | None:
    if not isinstance(path, str) or not path.strip():
        return None
    if not isinstance(bucket_name, str) or not bucket_name.strip():
        return None
    return f"{BASE_URL}/picPathMapping?{urlencode({'picPath': path, 'bucketName': bucket_name})}"


def extract_asset_links(detail: dict[str, Any], service_id: str) -> list[dict[str, Any]]:
    assets: list[dict[str, Any]] = []
    seen: set[tuple[str, str, str, str]] = set()

    def add(
        kind: str,
        path: Any,
        bucket: Any,
        name: Any,
        descriptor: dict[str, Any],
    ) -> None:
        path_text = str(path or "")
        bucket_text = str(bucket or "")
        name_text = str(name or "")
        key = (kind, path_text, bucket_text, name_text)
        if not path_text or key in seen:
            return
        seen.add(key)
        assets.append(
            {
                "service_id": service_id,
                "kind": kind,
                "name": name_text or None,
                "source_path": path_text,
                "bucket_name": bucket_text or None,
                "url": asset_url(path_text, bucket_text),
                "descriptor": descriptor,
            }
        )

    for material in detail.get("materialList") or []:
        if not isinstance(material, dict):
            continue
        for attachment in material.get("attachList") or []:
            if not isinstance(attachment, dict):
                continue
            add(
                "material_attachment",
                attachment.get("attachPath") or attachment.get("filePath"),
                attachment.get("bucketName"),
                attachment.get("attachName") or attachment.get("fileName"),
                {
                    "material_id": material.get("materialId"),
                    "material_title": material.get("materialTitle"),
                    "attach_code": attachment.get("attachCode"),
                },
            )

    for result in detail.get("resultList") or []:
        if isinstance(result, dict):
            add(
                "result_sample",
                result.get("resultChart") or result.get("attachPath"),
                result.get("bucketName"),
                result.get("resultSampleName") or result.get("fileName"),
                {"result_id": result.get("id")},
            )

    for process in detail.get("lctList") or []:
        if isinstance(process, dict):
            add(
                "process_diagram",
                process.get("flowc") or process.get("flowPath"),
                process.get("bucketName") or process.get("flowBucketName"),
                process.get("fileName") or "流程图",
                {"process_id": process.get("id")},
            )
    return assets


def secondary_duplicate_groups(
    catalog: dict[str, dict[str, Any]],
) -> list[dict[str, Any]]:
    groups: dict[tuple[str, str], list[str]] = {}
    for service_id, record in catalog.items():
        for rights_code in record.get("rights_codes") or []:
            for business_code in record.get("business_codes") or []:
                groups.setdefault((str(rights_code), str(business_code)), []).append(
                    service_id
                )
    return [
        {
            "rights_code": key[0],
            "business_code": key[1],
            "service_ids": sorted(set(ids)),
        }
        for key, ids in groups.items()
        if len(set(ids)) > 1
    ]


def load_catalog(path: Path) -> dict[str, dict[str, Any]]:
    if not path.exists():
        raise RuntimeError(f"缺少主目录 {path}；请先运行 catalog 阶段")
    payload = read_json(path)
    items = payload.get("items") if isinstance(payload, dict) else None
    if not isinstance(items, list):
        raise RuntimeError(f"主目录格式异常: {path}")
    return {str(item["service_id"]): item for item in items}


def limited_total(total_pages: int, limit: int) -> int:
    if total_pages <= 0:
        return 1
    return min(total_pages, limit) if limit else total_pages


def crawl_catalog(crawler: FullCrawler) -> dict[str, dict[str, Any]]:
    config = crawler.config
    print("\n[1/3] 抓取个人/法人主题和事项清单")
    catalog: dict[str, dict[str, Any]] = {}
    scope_reports: list[dict[str, Any]] = []
    global_digests: dict[tuple[str, str], list[dict[str, Any]]] = {}

    for service_type in SERVICE_TYPES:
        categories = category_map(crawler.fetch_categories(service_type))
        category_items = list(categories.items())
        if config.max_categories_per_type:
            category_items = category_items[: config.max_categories_per_type]
        scopes = category_items[:]
        if config.include_unfiltered:
            scopes.append(("all", "全部（无主题过滤）"))

        for scope_index, (code, name) in enumerate(scopes, start=1):
            target = f"{service_type}/{code} {name}"
            print(f"  {target}")
            scope_ids: list[str] = []
            scope_digests: dict[str, int] = {}
            totals_observed: list[int] = []
            pages_fetched = 0
            try:
                first = crawler.fetch_list_page(service_type, code, 1)
                first_data = first.get("data") or {}
                reported_total_pages = int(first_data.get("totalPage") or 0)
                target_pages = limited_total(
                    reported_total_pages, config.max_pages_per_scope
                )

                for page in range(1, target_pages + 1):
                    try:
                        payload = (
                            first
                            if page == 1
                            else crawler.fetch_list_page(service_type, code, page)
                        )
                        data = payload.get("data") or {}
                        returned_page = int(data.get("pageIndex") or page)
                        items = data.get("contents") or []
                        totals_observed.append(int(data.get("total") or 0))
                        digest = page_digest(items, "unifyId")
                        if digest in scope_digests and items:
                            crawler.record_warning(
                                "catalog",
                                target,
                                (
                                    f"第 {page} 页与第 {scope_digests[digest]} 页"
                                    f"事项 ID 摘要相同：{digest}"
                                ),
                            )
                        scope_digests[digest] = page
                        global_digests.setdefault((service_type, digest), []).append(
                            {"category_code": code, "page": page}
                        )
                        if returned_page != page:
                            crawler.record_warning(
                                "catalog",
                                target,
                                f"请求第 {page} 页，网站返回第 {returned_page} 页",
                            )
                        for item in items:
                            if merge_list_item(
                                catalog, item, service_type, code, name, page
                            ):
                                add_unique(scope_ids, str(item.get("unifyId")))
                        pages_fetched += 1
                        crawler.checkpoint(
                            "catalog",
                            target,
                            service_type=service_type,
                            scope_index=scope_index,
                            scope_count=len(scopes),
                            page=page,
                            target_pages=target_pages,
                            unique_items=len(catalog),
                        )
                        print(
                            f"    第 {page}/{target_pages} 页：{len(items)} 条，"
                            f"累计唯一 {len(catalog)}"
                        )
                    except Exception as exc:
                        crawler.record_error("catalog_page", f"{target}/page={page}", exc)
            except Exception as exc:
                crawler.record_error("catalog_scope", target, exc)
                reported_total_pages = 0

            scope_reports.append(
                {
                    "service_type": service_type,
                    "category_code": code,
                    "category_name": name,
                    "pages_fetched": pages_fetched,
                    "reported_total_pages": reported_total_pages,
                    "reported_totals_observed": sorted(set(totals_observed)),
                    "unique_ids_in_scope": len(scope_ids),
                    "limited": bool(
                        config.max_pages_per_scope
                        and reported_total_pages > config.max_pages_per_scope
                    ),
                }
            )

    cross_type_ids = sorted(
        service_id
        for service_id, record in catalog.items()
        if {"personal", "legal_entity"}.issubset(record["service_types"])
    )
    title_conflict_ids = sorted(
        service_id for service_id, record in catalog.items() if record["conflicts"]
    )
    repeated_across_scopes = [
        {
            "service_type": key[0],
            "digest": key[1],
            "locations": locations,
        }
        for key, locations in global_digests.items()
        if len(locations) > 1
    ]

    items = sorted(catalog.values(), key=lambda item: item["service_id"])
    payload = {
        "schema": "hunan-gov-catalog/1.0",
        "crawler_version": VERSION,
        "generated_at": utc_now(),
        "limited_run": config.is_limited_run,
        "source": {
            "personal": f"{BASE_URL}/hnywtb/service/index.html?type=gr",
            "legal_entity": f"{BASE_URL}/hnywtb/service/index.html?type=fr",
        },
        "items": items,
    }
    audit = {
        "generated_at": utc_now(),
        "limited_run": config.is_limited_run,
        "unique_service_count": len(catalog),
        "personal_and_legal_overlap_count": len(cross_type_ids),
        "personal_and_legal_overlap_ids": cross_type_ids,
        "title_conflict_count": len(title_conflict_ids),
        "title_conflict_ids": title_conflict_ids,
        "secondary_key_duplicate_groups": secondary_duplicate_groups(catalog),
        "scope_reports": scope_reports,
        "repeated_page_digests_across_scopes": repeated_across_scopes,
    }
    atomic_write_json(config.output / "catalog" / "full_catalog.json", payload)
    atomic_write_json(config.output / "audit" / "list_audit.json", audit)
    print(f"  清单完成：{len(catalog)} 个唯一事项")
    return catalog


def choose_detail_records(
    catalog: dict[str, dict[str, Any]], max_details: int
) -> list[dict[str, Any]]:
    records = list(catalog.values())
    if not max_details or len(records) <= max_details:
        return records
    chosen: list[dict[str, Any]] = []
    chosen_ids: set[str] = set()
    for service_type in SERVICE_TYPES:
        for record in records:
            if service_type in record["service_types"]:
                chosen.append(record)
                chosen_ids.add(record["service_id"])
                break
    for record in records:
        if record["service_id"] not in chosen_ids:
            chosen.append(record)
            chosen_ids.add(record["service_id"])
        if len(chosen) >= max_details:
            break
    return chosen[:max_details]


def crawl_details(
    crawler: FullCrawler,
    catalog: dict[str, dict[str, Any]],
) -> dict[str, dict[str, Any]]:
    config = crawler.config
    print("\n[2/3] 抓取办理指南详情")
    selected = choose_detail_records(catalog, config.max_details)
    assets: list[dict[str, Any]] = []

    for index, record in enumerate(selected, start=1):
        service_id = record["service_id"]
        try:
            payload = crawler.fetch_detail(service_id)
            detail = payload.get("data") or {}
            detail_assets = extract_asset_links(detail, service_id)
            assets.extend(detail_assets)
            record["detail"] = {
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
                "asset_link_count": len(detail_assets),
                "has_acceptance_condition": bool(detail.get("acceptanceCondition")),
                "has_legal_basis": bool(detail.get("according")),
            }
        except Exception as exc:
            record["detail"] = {"status": "error", "message": str(exc)}
            crawler.record_error("details", service_id, exc)

        crawler.checkpoint(
            "details",
            service_id,
            completed=index,
            total=len(selected),
            assets=len(assets),
        )
        if index == 1 or index % 25 == 0 or index == len(selected):
            print(f"  详情进度：{index}/{len(selected)}")

    enriched_payload = {
        "schema": "hunan-gov-enriched-catalog/1.0",
        "crawler_version": VERSION,
        "generated_at": utc_now(),
        "limited_run": config.is_limited_run,
        "items": sorted(catalog.values(), key=lambda item: item["service_id"]),
    }
    asset_payload = {
        "schema": "hunan-gov-asset-links/1.0",
        "generated_at": utc_now(),
        "downloaded": False,
        "note": "仅保存官网返回的附件描述、路径和可确认链接，不下载文件。",
        "items": assets,
    }
    atomic_write_json(
        config.output / "catalog" / "enriched_catalog.json", enriched_payload
    )
    atomic_write_json(config.output / "catalog" / "asset_links.json", asset_payload)
    return catalog


def crawl_search_audit(
    crawler: FullCrawler,
    catalog: dict[str, dict[str, Any]],
) -> None:
    config = crawler.config
    print("\n[3/3] 抓取智能搜索空关键词结果（仅审计）")
    search_items: dict[str, dict[str, Any]] = {}
    page_reports: list[dict[str, Any]] = []
    page_digests: dict[str, int] = {}

    first = crawler.fetch_search_page(1)
    reported_total = int(first.get("total") or 0)
    raw_page_total_field = int(first.get("pageTotal") or 0)
    # 该接口实测 pageTotal 与 total 相同，含义并非真正页数。
    # 必须使用总记录数和实际返回/请求的 pageSize 自行计算，避免大量空页请求。
    effective_page_size = int(first.get("pageSize") or config.search_rows)
    reported_total_pages = max(
        1, (reported_total + effective_page_size - 1) // effective_page_size
    )
    target_pages = limited_total(reported_total_pages, config.max_search_pages)

    for page in range(1, target_pages + 1):
        try:
            payload = first if page == 1 else crawler.fetch_search_page(page)
            items = payload.get("datas") or []
            digest = page_digest(items, "id")
            if digest in page_digests and items:
                crawler.record_warning(
                    "search",
                    f"page={page}",
                    f"与第 {page_digests[digest]} 页 ID 摘要相同：{digest}",
                )
            page_digests[digest] = page
            for item in items:
                search_id = str(item.get("id") or "").strip()
                if search_id:
                    search_items.setdefault(search_id, item)
            page_reports.append(
                {
                    "requested_page": page,
                    "returned_page": payload.get("pageIndex"),
                    "items_on_page": len(items),
                    "reported_total": payload.get("total"),
                    "digest": digest,
                }
            )
            crawler.checkpoint(
                "search",
                f"page={page}",
                page=page,
                target_pages=target_pages,
                unique_search_items=len(search_items),
            )
            if page == 1 or page % 10 == 0 or page == target_pages:
                print(f"  搜索审计进度：{page}/{target_pages}")
        except Exception as exc:
            crawler.record_error("search", f"page={page}", exc)

    catalog_ids = set(catalog)
    search_ids = set(search_items)
    overlap_ids = sorted(catalog_ids & search_ids)
    search_only_ids = sorted(search_ids - catalog_ids)
    catalog_only_ids = sorted(catalog_ids - search_ids)
    search_only = [search_items[search_id] for search_id in search_only_ids]

    audit = {
        "schema": "hunan-gov-search-audit/1.0",
        "generated_at": utc_now(),
        "limited_run": config.is_limited_run,
        "policy": "audit_only_not_merged",
        "reported_total": reported_total,
        "raw_page_total_field": raw_page_total_field,
        "effective_page_size": effective_page_size,
        "reported_total_pages": reported_total_pages,
        "pages_fetched": len(page_reports),
        "unique_search_items": len(search_items),
        "catalog_items": len(catalog),
        "overlap_count": len(overlap_ids),
        "search_only_count": len(search_only_ids),
        "catalog_only_count": len(catalog_only_ids),
        "overlap_ids": overlap_ids,
        "search_only_ids": search_only_ids,
        "catalog_only_ids": catalog_only_ids,
        "page_reports": page_reports,
    }
    atomic_write_json(config.output / "audit" / "search_audit.json", audit)
    atomic_write_json(
        config.output / "audit" / "search_only_candidates.json",
        {
            "generated_at": utc_now(),
            "policy": "candidates_only_not_merged",
            "items": search_only,
        },
    )


def parse_stages(value: str) -> tuple[str, ...]:
    stages = tuple(dict.fromkeys(part.strip() for part in value.split(",") if part.strip()))
    unknown = set(stages) - VALID_STAGES
    if unknown:
        raise argparse.ArgumentTypeError(
            f"未知阶段：{','.join(sorted(unknown))}；可选 catalog,details,search"
        )
    if not stages:
        raise argparse.ArgumentTypeError("至少选择一个阶段")
    return stages


def parse_args() -> Config:
    parser = argparse.ArgumentParser(
        description="湖南省政务服务知识库全量爬虫（省级、可续传、低频串行）"
    )
    parser.add_argument(
        "--output",
        type=Path,
        default=Path.cwd() / "gov-wiki",
        help="输出目录，默认当前运行目录下的 ./gov-wiki",
    )
    parser.add_argument(
        "--stages",
        type=parse_stages,
        default=parse_stages("catalog,details,search"),
        help="执行阶段，逗号分隔；默认 catalog,details,search",
    )
    parser.add_argument("--list-rows", type=int, default=50, help="清单每页条数")
    parser.add_argument("--search-rows", type=int, default=50, help="搜索每页条数")
    parser.add_argument("--delay", type=float, default=1.0, help="请求基础间隔秒数")
    parser.add_argument("--jitter", type=float, default=0.5, help="随机附加间隔秒数")
    parser.add_argument("--timeout", type=float, default=25.0, help="单次请求超时秒数")
    parser.add_argument("--retries", type=int, default=2, help="失败重试次数")

    test = parser.add_argument_group("安全测试上限（0 表示不限制）")
    test.add_argument("--max-categories-per-type", type=int, default=0)
    test.add_argument("--max-pages-per-scope", type=int, default=0)
    test.add_argument("--max-details", type=int, default=0)
    test.add_argument("--max-search-pages", type=int, default=0)

    parser.add_argument(
        "--no-unfiltered",
        action="store_true",
        help="不抓个人/法人无主题过滤全集；正式全量不建议使用",
    )
    parser.add_argument("--refresh-categories", action="store_true")
    parser.add_argument("--refresh-lists", action="store_true")
    parser.add_argument("--refresh-details", action="store_true")
    parser.add_argument("--refresh-search", action="store_true")
    parser.add_argument(
        "--refresh-all",
        action="store_true",
        help="刷新分类、清单、详情和搜索全部缓存",
    )
    parser.add_argument(
        "--fail-fast",
        action="store_true",
        help="遇到首个错误立即退出；默认记录错误并继续",
    )
    args = parser.parse_args()

    positive = {
        "list-rows": args.list_rows,
        "search-rows": args.search_rows,
        "timeout": args.timeout,
    }
    for label, value in positive.items():
        if value <= 0:
            parser.error(f"{label} 必须大于 0")
    nonnegative = {
        "delay": args.delay,
        "jitter": args.jitter,
        "retries": args.retries,
        "max-categories-per-type": args.max_categories_per_type,
        "max-pages-per-scope": args.max_pages_per_scope,
        "max-details": args.max_details,
        "max-search-pages": args.max_search_pages,
    }
    for label, value in nonnegative.items():
        if value < 0:
            parser.error(f"{label} 不能为负数")

    refresh_all = args.refresh_all
    return Config(
        output=args.output.expanduser().resolve(),
        stages=args.stages,
        list_rows=args.list_rows,
        search_rows=args.search_rows,
        delay=args.delay,
        jitter=args.jitter,
        timeout=args.timeout,
        retries=args.retries,
        max_categories_per_type=args.max_categories_per_type,
        max_pages_per_scope=args.max_pages_per_scope,
        max_details=args.max_details,
        max_search_pages=args.max_search_pages,
        include_unfiltered=not args.no_unfiltered,
        refresh_categories=args.refresh_categories or refresh_all,
        refresh_lists=args.refresh_lists or refresh_all,
        refresh_details=args.refresh_details or refresh_all,
        refresh_search=args.refresh_search or refresh_all,
        fail_fast=args.fail_fast,
    )


def write_run_report(crawler: FullCrawler, status: str) -> None:
    config = crawler.config
    report = {
        "crawler_version": VERSION,
        "status": status,
        "started_at": crawler.run_started_at,
        "finished_at": utc_now(),
        "config": {
            **asdict(config),
            "output": str(config.output),
            "stages": list(config.stages),
        },
        "network_requests": crawler.network_requests,
        "cache_hits": crawler.cache_hits,
        "error_count": len(crawler.errors),
        "warning_count": len(crawler.warnings),
        "errors": crawler.errors,
        "warnings": crawler.warnings,
    }
    atomic_write_json(config.output / "reports" / "latest_run.json", report)


def main() -> int:
    config = parse_args()
    config.output.mkdir(parents=True, exist_ok=True)
    crawler = FullCrawler(config)
    print(f"湖南政务服务全量爬虫 v{VERSION}")
    print(f"输出目录：{config.output}")
    print(f"执行阶段：{', '.join(config.stages)}")
    if config.is_limited_run:
        print("当前启用了测试上限；输出会标记 limited_run=true。")

    catalog_path = config.output / "catalog" / "full_catalog.json"
    catalog: dict[str, dict[str, Any]] | None = None
    status = "succeeded"
    try:
        if "catalog" in config.stages:
            catalog = crawl_catalog(crawler)
        if "details" in config.stages:
            catalog = catalog or load_catalog(catalog_path)
            catalog = crawl_details(crawler, catalog)
        if "search" in config.stages:
            catalog = catalog or load_catalog(catalog_path)
            crawl_search_audit(crawler, catalog)
        if crawler.errors:
            status = "completed_with_errors"
    except KeyboardInterrupt:
        status = "interrupted"
        crawler.record_warning("run", "keyboard", "用户中断；缓存可用于续传")
    except Exception as exc:
        status = "failed"
        crawler.record_error("run", "main", exc)
    finally:
        write_run_report(crawler, status)

    print("\n运行结束")
    print(f"状态：{status}")
    print(f"网络请求：{crawler.network_requests}；缓存命中：{crawler.cache_hits}")
    print(f"错误：{len(crawler.errors)}；警告：{len(crawler.warnings)}")
    print(f"运行报告：{config.output / 'reports' / 'latest_run.json'}")
    return 0 if status == "succeeded" else 2


if __name__ == "__main__":
    raise SystemExit(main())
