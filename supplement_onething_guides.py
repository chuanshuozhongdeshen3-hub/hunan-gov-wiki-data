#!/usr/bin/env python3
"""补抓“高效办成一件事”的地区实施清单和完整办事指南。

数据发现链路：

1. 从 ``audit/search_only_candidates.json`` 读取 ``type == theme`` 的主题；
2. 递归调用 ``implementation_lists/area_list`` 获取行政区划树；
3. 对 ``benji == 1`` 的本级节点调用 ``implementation_lists/info``；
4. 从 ``data.yjsSsqdId`` 取得 ``onethingChecklistId``；
5. 调用 ``/onething/v1/guide/info/{id}`` 保存完整指南。

脚本只补充原始 JSON 和结构化目录，不直接改写 Wiki。所有请求均为官网公开的
匿名接口；默认低频串行，支持缓存、断点续跑、样本上限和失败后继续。
"""

from __future__ import annotations

import argparse
import json
import os
import random
import sys
import time
from dataclasses import dataclass
from http.cookiejar import CookieJar
from pathlib import Path
from typing import Any, Callable
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import (
    HTTPCookieProcessor,
    Request,
    build_opener,
)

from hunan_gov_full_crawler import (
    BASE_URL,
    atomic_write_json,
    read_json,
    safe_identifier,
    utc_now,
)


VERSION = "1.0.0"
ANONY_PROXY = f"{BASE_URL}/hnywtb/anony"
ROOT_ORG_LEVEL = "1"
ROOT_PARENT_ID = "1"


@dataclass
class CrawlOptions:
    output: Path
    delay: float
    jitter: float
    timeout: float
    retries: int
    max_themes: int
    max_guides: int
    max_depth: int
    include_zero_approve: bool
    refresh_areas: bool
    refresh_info: bool
    refresh_guides: bool
    fail_fast: bool


def relative_to_output(path: Path, output: Path) -> str:
    return path.relative_to(output).as_posix()


def normalize_json(raw: bytes, charset: str) -> Any:
    """解析响应；兼容代理偶尔返回被 JSON 字符串包裹的 JSON。"""
    value: Any = json.loads(raw.decode(charset, errors="replace"))
    if isinstance(value, str):
        stripped = value.strip()
        if stripped.startswith(("{", "[")):
            value = json.loads(stripped)
    return value


def require_success(payload: Any, label: str) -> None:
    if not isinstance(payload, dict):
        raise RuntimeError(f"{label} 返回值不是 JSON 对象")
    if payload.get("success") is False or payload.get("error") is True:
        raise RuntimeError(f"{label} 返回失败：{payload.get('msg', payload)}")
    code = payload.get("code")
    if code not in (None, 0, "0"):
        raise RuntimeError(f"{label} 返回异常代码 {code!r}：{payload.get('msg', '')}")


def as_records(value: Any, label: str) -> list[dict[str, Any]]:
    """把接口的单对象/对象数组统一为对象数组。"""
    if value is None:
        return []
    if isinstance(value, dict):
        return [value]
    if isinstance(value, list) and all(isinstance(item, dict) for item in value):
        return value
    raise RuntimeError(f"{label} data 既不是对象、对象数组，也不是 null")


def is_self_node(node: dict[str, Any]) -> bool:
    return str(node.get("benji") or "").strip() == "1"


def is_zero_approve(node: dict[str, Any]) -> bool:
    value = node.get("approveId")
    if value is None or isinstance(value, bool):
        return False
    try:
        return float(str(value).strip()) == 0
    except ValueError:
        return False


def node_name(node: dict[str, Any]) -> str:
    return str(node.get("orgShowName") or node.get("orgName") or "").strip()


def load_themes(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        raise FileNotFoundError(f"缺少主题候选文件：{path}")
    payload = read_json(path)
    items = payload.get("items") if isinstance(payload, dict) else None
    if not isinstance(items, list):
        raise ValueError("主题候选文件格式异常：items 不是数组")

    themes: dict[str, dict[str, Any]] = {}
    for item in items:
        if not isinstance(item, dict):
            continue
        if str(item.get("type") or "").lower() != "theme":
            continue
        code = str(item.get("id") or "").strip()
        if not code:
            continue
        safe_identifier(code, "一件事编码")
        if code in themes:
            raise ValueError(f"主题编码重复：{code}")
        themes[code] = item
    return sorted(themes.values(), key=lambda item: str(item["id"]))


class OneThingCrawler:
    def __init__(self, options: CrawlOptions) -> None:
        self.options = options
        self.opener = build_opener(HTTPCookieProcessor(CookieJar()))
        self.last_request_at = 0.0
        self.network_requests = 0
        self.cache_hits = 0
        self.errors: list[dict[str, Any]] = []
        self.warnings: list[dict[str, Any]] = []
        self.started_at = utc_now()
        self._fetched_guides: set[tuple[str, str]] = set()

    def warn(self, stage: str, target: str, message: str) -> None:
        issue = {
            "time": utc_now(),
            "stage": stage,
            "target": target,
            "message": message,
        }
        self.warnings.append(issue)
        print(f"  [警告] {stage} / {target}：{message}", file=sys.stderr)

    def error(self, stage: str, target: str, exc: Exception) -> None:
        issue = {
            "time": utc_now(),
            "stage": stage,
            "target": target,
            "error_type": type(exc).__name__,
            "message": str(exc),
        }
        self.errors.append(issue)
        print(f"  [错误] {stage} / {target}：{exc}", file=sys.stderr)
        if self.options.fail_fast:
            raise exc

    def checkpoint(self, theme_code: str, current: str, **progress: Any) -> None:
        atomic_write_json(
            self.options.output / "state" / "onething_checkpoint.json",
            {
                "crawler": "supplement_onething_guides",
                "crawler_version": VERSION,
                "updated_at": utc_now(),
                "theme_code": theme_code,
                "current": current,
                "progress": progress,
                "network_requests": self.network_requests,
                "cache_hits": self.cache_hits,
                "error_count": len(self.errors),
                "warning_count": len(self.warnings),
            },
        )

    def _throttle(self) -> None:
        if not self.last_request_at:
            return
        interval = self.options.delay + random.uniform(0, self.options.jitter)
        elapsed = time.monotonic() - self.last_request_at
        if elapsed < interval:
            time.sleep(interval - elapsed)

    def _request_json(
        self,
        url: str,
        data: bytes,
        content_type: str | None,
        referer: str,
    ) -> Any:
        headers = {
            "Accept": "application/json, text/plain, */*",
            "Origin": BASE_URL,
            "Referer": referer,
            "User-Agent": (
                f"HunanGovWikiOneThingCrawler/{VERSION} "
                "(public-government-data; low-rate; resumable)"
            ),
            "X-Requested-With": "XMLHttpRequest",
        }
        if content_type:
            headers["Content-Type"] = content_type

        for attempt in range(self.options.retries + 1):
            self._throttle()
            request = Request(url, data=data, headers=headers, method="POST")
            try:
                with self.opener.open(request, timeout=self.options.timeout) as response:
                    raw = response.read()
                    charset = response.headers.get_content_charset() or "utf-8"
                self.last_request_at = time.monotonic()
                self.network_requests += 1
                return normalize_json(raw, charset)
            except HTTPError as exc:
                self.last_request_at = time.monotonic()
                retryable = exc.code in {408, 429, 500, 502, 503, 504}
                if not retryable or attempt >= self.options.retries:
                    raise RuntimeError(f"HTTP {exc.code}：{url}") from exc
            except (URLError, TimeoutError, json.JSONDecodeError) as exc:
                self.last_request_at = time.monotonic()
                if attempt >= self.options.retries:
                    raise RuntimeError(f"请求或 JSON 解析失败：{url}：{exc}") from exc

            wait_seconds = min(2**attempt, 8)
            print(
                f"  请求失败，{wait_seconds} 秒后重试 "
                f"({attempt + 1}/{self.options.retries})"
            )
            time.sleep(wait_seconds)
        raise AssertionError("unreachable")

    def _cached(
        self,
        path: Path,
        refresh: bool,
        fetcher: Callable[[], Any],
    ) -> Any:
        if path.exists() and not refresh:
            self.cache_hits += 1
            return read_json(path)
        payload = fetcher()
        atomic_write_json(path, payload)
        return payload

    @staticmethod
    def _overview_referer() -> str:
        return f"{BASE_URL}/ywtb/gov-search/index.html#/things"

    @staticmethod
    def event_guide_url(theme_code: str, checklist_id: str) -> str:
        return (
            f"{BASE_URL}/hnywtb/onething/html/eventGuideline.html?"
            + urlencode(
                {
                    "onethingCode": theme_code,
                    "onethingChecklistId": checklist_id,
                }
            )
        )

    def _post_form_proxy(self, internal_url: str) -> Any:
        proxy_url = f"{ANONY_PROXY}?{urlencode({'action': 'toHttpGet'})}"
        form = urlencode({"url": internal_url}).encode("utf-8")
        return self._request_json(
            proxy_url,
            form,
            "application/x-www-form-urlencoded",
            self._overview_referer(),
        )

    def fetch_area_list(
        self,
        theme_code: str,
        org_level: str,
        parent_id: str,
    ) -> tuple[dict[str, Any], Path]:
        safe_theme = safe_identifier(theme_code, "一件事编码")
        safe_parent = safe_identifier(parent_id, "地区 parentId")
        safe_level = safe_identifier(org_level, "地区层级")
        path = (
            self.options.output
            / "raw"
            / "onething"
            / "area_lists"
            / safe_theme
            / f"level_{safe_level}"
            / f"parent_{safe_parent}.json"
        )
        internal_url = "/onething_catalog/v1/implementation_lists/area_list?" + urlencode(
            {
                "orgLevel": org_level,
                "parentId": parent_id,
                "onethingCode": theme_code,
            }
        )
        payload = self._cached(
            path,
            self.options.refresh_areas,
            lambda: self._post_form_proxy(internal_url),
        )
        require_success(payload, f"{theme_code} 地区列表 {org_level}/{parent_id}")
        data = payload.get("data")
        if data is None:
            payload["data"] = []
        elif not isinstance(data, list) or not all(
            isinstance(item, dict) for item in data
        ):
            raise RuntimeError("地区列表 data 不是对象数组")
        return payload, path

    def fetch_implementation_info(
        self,
        theme_code: str,
        area_code: str,
    ) -> tuple[dict[str, Any], Path]:
        safe_theme = safe_identifier(theme_code, "一件事编码")
        safe_area = safe_identifier(area_code, "地区编码")
        path = (
            self.options.output
            / "raw"
            / "onething"
            / "implementations"
            / safe_theme
            / f"{safe_area}.json"
        )
        internal_url = "/onething_catalog/v1/implementation_lists/info?" + urlencode(
            {"onethingCode": theme_code, "areaCode": area_code}
        )
        payload = self._cached(
            path,
            self.options.refresh_info,
            lambda: self._post_form_proxy(internal_url),
        )
        require_success(payload, f"{theme_code}/{area_code} 实施清单")
        as_records(payload.get("data"), "实施清单")
        return payload, path

    def fetch_guide(
        self,
        theme_code: str,
        checklist_id: str,
    ) -> tuple[dict[str, Any], Path]:
        safe_theme = safe_identifier(theme_code, "一件事编码")
        safe_checklist = safe_identifier(checklist_id, "onethingChecklistId")
        path = (
            self.options.output
            / "raw"
            / "onething"
            / "guides"
            / safe_theme
            / f"{safe_checklist}.json"
        )
        internal_url = f"/onething/v1/guide/info/{checklist_id}"
        proxy_url = f"{ANONY_PROXY}?" + urlencode(
            {"action": "toCatalogHttpGet", "url": internal_url}
        )
        payload = self._cached(
            path,
            self.options.refresh_guides,
            lambda: self._request_json(
                proxy_url,
                b"",
                None,
                self.event_guide_url(theme_code, checklist_id),
            ),
        )
        require_success(payload, f"{theme_code}/{checklist_id} 完整指南")
        if not isinstance(payload.get("data"), dict):
            raise RuntimeError("完整指南 data 不是对象")
        return payload, path

    @staticmethod
    def guide_summary(data: dict[str, Any]) -> dict[str, Any]:
        basic = data.get("basicInfo") or {}
        if not isinstance(basic, dict):
            basic = {}
        guide = data.get("onethingCatalogStandServiceGuide") or {}
        if not isinstance(guide, dict):
            guide = {}
        return {
            "title": data.get("sxmc"),
            "implementation_code": data.get("yjsSsqdbm"),
            "area_name": basic.get("areaName"),
            "lead_department": basic.get("qtbmmc"),
            "cooperating_departments": basic.get("cooperateName"),
            "completed_limit": basic.get("completedLimit"),
            "is_charge": basic.get("isCharge"),
            "is_intermediary": basic.get("isIntermediary"),
            "is_logistics": basic.get("isLogistics"),
            "material_count": len(data.get("materialList") or []),
            "condition_count": len(data.get("guideCondition") or []),
            "linked_matter_count": len(data.get("approveList") or []),
            "faq_count": len(data.get("questionInfo") or []),
            "service_point_count": len(data.get("guidePoint") or []),
            "attachment_count": len(data.get("attachInfo") or []),
            "has_process_description": bool(guide.get("bllctsm")),
        }

    def _implementation_entries(
        self,
        theme: dict[str, Any],
        node: dict[str, Any],
        lineage: list[str],
    ) -> list[dict[str, Any]]:
        theme_code = str(theme["id"])
        area_code = str(node.get("areaCode") or "").strip()
        if not area_code:
            self.warn("implementation", theme_code, "本级节点缺少 areaCode")
            return []

        info_payload, info_path = self.fetch_implementation_info(theme_code, area_code)
        info_records = as_records(info_payload.get("data"), "实施清单")
        entries: list[dict[str, Any]] = []
        for info in info_records:
            checklist_id = str(info.get("yjsSsqdId") or "").strip()
            if not checklist_id:
                if info:
                    self.warn(
                        "implementation",
                        f"{theme_code}/{area_code}",
                        "实施清单存在，但缺少 yjsSsqdId",
                    )
                continue
            safe_identifier(checklist_id, "onethingChecklistId")
            returned_code = str(info.get("sxbm") or "").strip()
            if returned_code and returned_code != theme_code:
                self.warn(
                    "implementation",
                    f"{theme_code}/{area_code}",
                    f"返回主题编码为 {returned_code}",
                )

            guide_payload, guide_path = self.fetch_guide(theme_code, checklist_id)
            self._fetched_guides.add((theme_code, checklist_id))
            entries.append(
                {
                    "theme_code": theme_code,
                    "theme_title": theme.get("name") or theme.get("_title"),
                    "area_code": area_code,
                    "area_name": node_name(node),
                    "area_path": lineage + [node_name(node)],
                    "org_level": node.get("orgLevel"),
                    "org_id": node.get("orgId"),
                    "parent_id": node.get("parentId"),
                    "approve_id": node.get("approveId"),
                    "checklist_id": checklist_id,
                    "implementation_name": info.get("sxmc"),
                    "implementation_code": info.get("sxbm"),
                    "info_raw_file": relative_to_output(info_path, self.options.output),
                    "guide_raw_file": relative_to_output(guide_path, self.options.output),
                    "official_guide_url": self.event_guide_url(
                        theme_code, checklist_id
                    ),
                    "guide_summary": self.guide_summary(guide_payload["data"]),
                }
            )
        return entries

    def crawl_theme(self, theme: dict[str, Any]) -> dict[str, Any]:
        theme_code = str(theme["id"])
        title = str(theme.get("name") or theme.get("_title") or theme_code)
        print(f"\n[主题] {title} ({theme_code})")

        queue: list[tuple[str, str, list[str], int]] = [
            (ROOT_ORG_LEVEL, ROOT_PARENT_ID, [], 1)
        ]
        visited_requests: set[tuple[str, str]] = set()
        seen_nodes: set[tuple[str, str, str]] = set()
        implementations: list[dict[str, Any]] = []
        pruned_zero = 0
        area_request_count = 0

        while queue:
            org_level, parent_id, lineage, depth = queue.pop(0)
            request_key = (org_level, parent_id)
            if request_key in visited_requests:
                self.warn(
                    "area_tree",
                    theme_code,
                    f"检测到重复地区请求 {org_level}/{parent_id}，已跳过",
                )
                continue
            visited_requests.add(request_key)

            try:
                payload, _ = self.fetch_area_list(theme_code, org_level, parent_id)
                nodes = payload.get("data") or []
                area_request_count += 1
            except Exception as exc:
                self.error("area_list", f"{theme_code}/{org_level}/{parent_id}", exc)
                continue

            for node in nodes:
                key = (
                    str(node.get("orgId") or ""),
                    str(node.get("areaCode") or ""),
                    str(node.get("benji") or ""),
                )
                if key in seen_nodes:
                    continue
                seen_nodes.add(key)

                if is_zero_approve(node) and not self.options.include_zero_approve:
                    pruned_zero += 1
                    continue

                if is_self_node(node):
                    try:
                        implementations.extend(
                            self._implementation_entries(theme, node, lineage)
                        )
                    except Exception as exc:
                        target = f"{theme_code}/{node.get('areaCode') or node_name(node)}"
                        self.error("implementation_or_guide", target, exc)
                    if (
                        self.options.max_guides
                        and len({item["checklist_id"] for item in implementations})
                        >= self.options.max_guides
                    ):
                        queue.clear()
                        break
                    continue

                if depth >= self.options.max_depth:
                    self.warn(
                        "area_tree",
                        f"{theme_code}/{node_name(node)}",
                        f"达到最大递归深度 {self.options.max_depth}",
                    )
                    continue
                child_id = str(node.get("orgId") or "").strip()
                child_level = str(node.get("orgLevel") or "").strip()
                if not child_id or not child_level:
                    self.warn(
                        "area_tree",
                        f"{theme_code}/{node_name(node)}",
                        "下级地区节点缺少 orgId 或 orgLevel",
                    )
                    continue
                queue.append(
                    (child_level, child_id, lineage + [node_name(node)], depth + 1)
                )

            self.checkpoint(
                theme_code,
                f"area:{org_level}/{parent_id}",
                area_requests=area_request_count,
                area_nodes=len(seen_nodes),
                implementations=len(implementations),
                queued=len(queue),
            )

        unique_implementations: dict[tuple[str, str], dict[str, Any]] = {}
        for item in implementations:
            unique_implementations[(item["area_code"], item["checklist_id"])] = item
        result_items = sorted(
            unique_implementations.values(),
            key=lambda item: (item["area_code"], item["checklist_id"]),
        )
        status = "ok" if result_items else "no_available_guide"
        print(
            f"  地区请求 {area_request_count}；地区节点 {len(seen_nodes)}；"
            f"实施清单 {len(result_items)}；跳过零可用节点 {pruned_zero}"
        )
        return {
            "theme_code": theme_code,
            "theme_title": title,
            "search_type_name": theme.get("type_name"),
            "status": status,
            "area_request_count": area_request_count,
            "area_node_count": len(seen_nodes),
            "pruned_zero_approve_count": pruned_zero,
            "implementation_count": len(result_items),
            "unique_guide_count": len(
                {item["checklist_id"] for item in result_items}
            ),
            "implementations": result_items,
        }


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="补抓湖南政务服务网‘高效办成一件事’地区版指南"
    )
    parser.add_argument(
        "--output",
        type=Path,
        default=Path.cwd() / "gov-wiki",
        help="数据目录，默认当前项目下的 .\\gov-wiki",
    )
    parser.add_argument(
        "--theme-code",
        action="append",
        default=[],
        help="只抓指定主题编码，可重复使用，例如 43PCN0009",
    )
    parser.add_argument(
        "--max-themes", type=int, default=0, help="最多处理主题数，0表示全部"
    )
    parser.add_argument(
        "--max-guides",
        type=int,
        default=0,
        help="每个主题最多成功抓取的指南数，0表示不限制；用于样本测试",
    )
    parser.add_argument(
        "--max-depth",
        type=int,
        default=4,
        help="地区树最大层数，默认4（省、市、县区、街道）",
    )
    parser.add_argument("--delay", type=float, default=1.0, help="请求基础间隔秒数")
    parser.add_argument("--jitter", type=float, default=0.5, help="随机附加间隔秒数")
    parser.add_argument("--timeout", type=float, default=25.0, help="请求超时秒数")
    parser.add_argument("--retries", type=int, default=2, help="失败重试次数")
    parser.add_argument(
        "--include-zero-approve",
        action="store_true",
        help="也查询 approveId=0 的节点；用于完整性复核，会显著增加请求",
    )
    parser.add_argument("--refresh-areas", action="store_true")
    parser.add_argument("--refresh-info", action="store_true")
    parser.add_argument("--refresh-guides", action="store_true")
    parser.add_argument(
        "--refresh-all", action="store_true", help="刷新地区、实施清单和指南缓存"
    )
    parser.add_argument(
        "--dry-run", action="store_true", help="只显示主题，不发请求、不写数据"
    )
    parser.add_argument(
        "--fail-fast", action="store_true", help="遇到第一个错误立即停止"
    )
    args = parser.parse_args()

    for name in ("delay", "jitter", "retries", "max_themes", "max_guides"):
        if getattr(args, name) < 0:
            parser.error(f"--{name.replace('_', '-')} 不能为负数")
    if args.timeout <= 0:
        parser.error("--timeout 必须大于0")
    if args.max_depth <= 0:
        parser.error("--max-depth 必须大于0")
    for code in args.theme_code:
        try:
            safe_identifier(code, "一件事编码")
        except ValueError as exc:
            parser.error(str(exc))
    return args


def choose_themes(
    themes: list[dict[str, Any]], requested_codes: list[str], max_themes: int
) -> list[dict[str, Any]]:
    by_code = {str(item["id"]): item for item in themes}
    if requested_codes:
        missing = sorted(set(requested_codes) - set(by_code))
        if missing:
            raise ValueError(f"候选文件中找不到主题：{', '.join(missing)}")
        selected = [by_code[code] for code in dict.fromkeys(requested_codes)]
    else:
        selected = themes
    return selected[:max_themes] if max_themes else selected


def write_catalog(
    crawler: OneThingCrawler,
    themes: list[dict[str, Any]],
    results: list[dict[str, Any]],
    status: str,
) -> None:
    output = crawler.options.output
    catalog_path = output / "catalog" / "onething_guides.json"
    merged_by_code: dict[str, dict[str, Any]] = {}
    if catalog_path.exists():
        try:
            previous = read_json(catalog_path)
            previous_themes = previous.get("themes") if isinstance(previous, dict) else None
            if isinstance(previous_themes, list):
                merged_by_code = {
                    str(item["theme_code"]): item
                    for item in previous_themes
                    if isinstance(item, dict) and item.get("theme_code")
                }
        except (OSError, ValueError, TypeError, json.JSONDecodeError) as exc:
            crawler.warn("catalog", str(catalog_path), f"旧汇总目录无法读取，将重建：{exc}")
    for item in results:
        merged_by_code[str(item["theme_code"])] = item
    catalog_results = sorted(merged_by_code.values(), key=lambda item: item["theme_code"])

    implementation_count = sum(
        item["implementation_count"] for item in catalog_results
    )
    unique_guides = {
        (item["theme_code"], impl["checklist_id"])
        for item in catalog_results
        for impl in item["implementations"]
    }
    payload = {
        "schema": "hunan-gov-onething-catalog/1.0",
        "crawler_version": VERSION,
        "generated_at": utc_now(),
        "status": status,
        "source": "audit/search_only_candidates.json",
        "run_selected_theme_count": len(themes),
        "run_completed_theme_count": len(results),
        "catalog_theme_count": len(catalog_results),
        "implementation_count": implementation_count,
        "unique_guide_count": len(unique_guides),
        "themes": catalog_results,
    }
    report = {
        "schema": "hunan-gov-onething-run-report/1.0",
        "crawler_version": VERSION,
        "status": status,
        "started_at": crawler.started_at,
        "finished_at": utc_now(),
        "network_requests": crawler.network_requests,
        "cache_hits": crawler.cache_hits,
        "error_count": len(crawler.errors),
        "warning_count": len(crawler.warnings),
        "errors": crawler.errors,
        "warnings": crawler.warnings,
        "config": {
            "output": str(output),
            "delay": crawler.options.delay,
            "jitter": crawler.options.jitter,
            "timeout": crawler.options.timeout,
            "retries": crawler.options.retries,
            "max_themes": crawler.options.max_themes,
            "max_guides": crawler.options.max_guides,
            "max_depth": crawler.options.max_depth,
            "include_zero_approve": crawler.options.include_zero_approve,
            "refresh_areas": crawler.options.refresh_areas,
            "refresh_info": crawler.options.refresh_info,
            "refresh_guides": crawler.options.refresh_guides,
            "fail_fast": crawler.options.fail_fast,
        },
    }
    atomic_write_json(catalog_path, payload)
    atomic_write_json(output / "reports" / "onething_latest_run.json", report)


def main() -> int:
    args = parse_args()
    output = args.output.expanduser().resolve()
    candidates = output / "audit" / "search_only_candidates.json"
    try:
        all_themes = load_themes(candidates)
        themes = choose_themes(all_themes, args.theme_code, args.max_themes)
    except (OSError, ValueError, TypeError) as exc:
        print(f"[错误] 无法加载主题：{exc}", file=sys.stderr)
        return 2

    print(f"数据目录：{output}")
    print(f"可用主题：{len(all_themes)}；本次处理：{len(themes)}")
    if args.dry_run:
        print("\n[预览] 本次不会请求或写入文件")
        for index, item in enumerate(themes, start=1):
            print(f"{index:02d}. {item['id']}  {item.get('name') or item.get('_title')}")
        return 0
    if not themes:
        print("没有需要处理的主题。")
        return 0

    refresh_all = args.refresh_all
    options = CrawlOptions(
        output=output,
        delay=args.delay,
        jitter=args.jitter,
        timeout=args.timeout,
        retries=args.retries,
        max_themes=args.max_themes,
        max_guides=args.max_guides,
        max_depth=args.max_depth,
        include_zero_approve=args.include_zero_approve,
        refresh_areas=args.refresh_areas or refresh_all,
        refresh_info=args.refresh_info or refresh_all,
        refresh_guides=args.refresh_guides or refresh_all,
        fail_fast=args.fail_fast,
    )
    crawler = OneThingCrawler(options)
    results: list[dict[str, Any]] = []
    status = "succeeded"
    try:
        for index, theme in enumerate(themes, start=1):
            print(f"\n进度：{index}/{len(themes)}")
            try:
                results.append(crawler.crawl_theme(theme))
            except Exception as exc:
                crawler.error("theme", str(theme.get("id")), exc)
            crawler.checkpoint(
                str(theme["id"]),
                "theme_completed",
                completed_themes=index,
                total_themes=len(themes),
            )
        if crawler.errors:
            status = "completed_with_errors"
    except KeyboardInterrupt:
        status = "interrupted"
        crawler.warn("run", "keyboard", "用户中断；已有原始缓存可继续使用")
    except Exception as exc:
        status = "failed"
        crawler.error("run", "main", exc)
    finally:
        write_catalog(crawler, themes, results, status)

    print("\n运行结束")
    print(f"状态：{status}")
    print(f"网络请求：{crawler.network_requests}；缓存命中：{crawler.cache_hits}")
    print(f"错误：{len(crawler.errors)}；警告：{len(crawler.warnings)}")
    print(f"汇总目录：{output / 'catalog' / 'onething_guides.json'}")
    print(f"运行报告：{output / 'reports' / 'onething_latest_run.json'}")
    return 0 if status == "succeeded" else 2


if __name__ == "__main__":
    raise SystemExit(main())
