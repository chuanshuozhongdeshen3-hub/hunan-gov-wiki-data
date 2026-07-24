#!/usr/bin/env python3
"""Convert regional “高效办成一件事” JSON into deduplicated Wiki pages.

The converter creates three retrieval layers:

* one overview page per theme;
* one authoritative business-content page per unique normalized fingerprint;
* one small regional page per implementation, linking to its business variant.

DeepSeek enrichment is requested only for overview pages and variants covering at
least ``enrich_min_areas`` regions. Regional pages and rare variants remain fully
searchable using official text without additional LLM calls.
"""

from __future__ import annotations

import argparse
import hashlib
import html
import json
import os
import re
import unicodedata
from collections import defaultdict
from datetime import date
from pathlib import Path
from typing import Any
from urllib.parse import urlencode


VERSION = "1.0.0"
BASE_URL = "https://zwfw-new.hunan.gov.cn"
RAW_MANIFEST = "raw/manifests/hunan-government-service-dataset.md"

ROW_EXCLUDE = {
    "cjsj",
    "gxsj",
    "pxh",
    "zt",
    "yjsBsznbltjId",
    "yjsbsznId",
    "bzclId",
    "yjsMlSsqdClId",
    "yjsSsqdId",
    "yjsMlSsqdQuestionId",
    "CHARGE_ID",
    "UNIFY_ID",
    "ywdxId",
    "fjId",
}
ATTACH_EXCLUDE = {
    "cjsj",
    "fjId",
    "fjdx",
    "fjlj",
    "s3tmc",
    "sfyx",
    "ywdxId",
    "fjbm",
}
BASIC_LOCAL_FIELDS = {
    "areaName",
    "cooperateName",
    "jdtsdh",
    "lbjgId",
    "qtbmId",
    "qtbmmc",
    "yjsssqdbsznid",
    "zxdh",
}
GUIDE_LOCAL_FIELDS = {
    "cjrId",
    "cjsj",
    "gxrId",
    "gxsj",
    "lbjgId",
    "yjsMlBzhId",
    "yjsbsznId",
    "jdtsdh",
    "jdtsdz",
    "zxdh",
    "zxdz",
    "dndtzdz",
    "yddtzdz",
    "zzzdtzdz",
    "xxcktzdz",
    "zcblqdWbdz",
}


def read_json(path: Path) -> Any:
    with path.open("r", encoding="utf-8") as handle:
        return json.load(handle)


def atomic_write_text(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(text, encoding="utf-8", newline="\n")
    os.replace(temporary, path)


def atomic_write_json(path: Path, value: Any) -> None:
    atomic_write_text(path, json.dumps(value, ensure_ascii=False, indent=2) + "\n")


def clean_text(value: Any) -> str:
    if value is None:
        return ""
    text = html.unescape(str(value)).replace("\r\n", "\n").replace("\r", "\n")
    text = re.sub(r"(?i)<\s*br\s*/?\s*>", "\n", text)
    text = re.sub(r"(?i)</?\s*(p|div|li|tr|h[1-6])\b[^>]*>", "\n", text)
    text = re.sub(r"<[^>]+>", "", text)
    text = text.replace("\u00a0", " ").replace("\u3000", " ")
    lines = [re.sub(r"[ \t]+", " ", line).strip() for line in text.split("\n")]
    return "\n".join(line for line in lines if line).strip()


def one_line(value: Any) -> str:
    return re.sub(r"\s+", " ", clean_text(value)).strip()


def md_cell(value: Any) -> str:
    return clean_text(value).replace("|", "\\|").replace("\n", "<br>") or "—"


def yaml_value(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False)


def frontmatter(fields: list[tuple[str, Any]]) -> str:
    lines = ["---"]
    lines.extend(f"{key}: {yaml_value(value)}" for key, value in fields)
    lines.extend(["---", ""])
    return "\n".join(lines)


def short_hash(value: str, length: int = 10) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()[:length]


def slug_text(value: Any, fallback: str = "page", max_length: int = 48) -> str:
    text = unicodedata.normalize("NFKC", one_line(value)).lower()
    text = re.sub(r"[\\/:*?\"<>|#\[\]{}()（）【】《》“”‘’，。；：、·]+", "-", text)
    text = re.sub(r"\s+", "-", text)
    text = re.sub(r"-+", "-", text).strip("-. ")
    return text[:max_length].rstrip("-.") or fallback


def wikilink(path: str, alias: str | None = None) -> str:
    target = path[:-3] if path.endswith(".md") else path
    return f"[[{target}|{alias}]]" if alias else f"[[{target}]]"


def existing_created(path: Path, default: str) -> str:
    if not path.exists():
        return default
    try:
        head = path.read_text(encoding="utf-8")[:3000]
    except OSError:
        return default
    match = re.search(r'^created:\s*["\']?([^"\'\n]+)', head, flags=re.MULTILINE)
    return match.group(1).strip() if match else default


def scalar_fields(record: dict[str, Any], excluded: set[str]) -> dict[str, str]:
    result: dict[str, str] = {}
    for key, value in sorted(record.items()):
        if key in excluded or isinstance(value, (dict, list)):
            continue
        normalized = one_line(value)
        if normalized:
            result[key] = normalized
    return result


def canonical_business_content(detail: dict[str, Any]) -> dict[str, Any]:
    materials: list[dict[str, Any]] = []
    for material in detail.get("materialList") or []:
        if not isinstance(material, dict):
            continue
        normalized = scalar_fields(material, ROW_EXCLUDE | {"attachList"})
        normalized["attachments"] = [
            scalar_fields(attachment, ATTACH_EXCLUDE)
            for attachment in material.get("attachList") or []
            if isinstance(attachment, dict)
        ]
        materials.append(normalized)
    return {
        "basic": scalar_fields(detail.get("basicInfo") or {}, BASIC_LOCAL_FIELDS),
        "guide": scalar_fields(
            detail.get("onethingCatalogStandServiceGuide") or {},
            GUIDE_LOCAL_FIELDS,
        ),
        "conditions": [
            scalar_fields(item, ROW_EXCLUDE)
            for item in detail.get("guideCondition") or []
            if isinstance(item, dict)
        ],
        "materials": materials,
        "approvals": [
            scalar_fields(item, ROW_EXCLUDE)
            for item in detail.get("approveList") or []
            if isinstance(item, dict)
        ],
        "charges": [
            scalar_fields(item, ROW_EXCLUDE)
            for item in detail.get("chargelist") or []
            if isinstance(item, dict)
        ],
        "questions": [
            scalar_fields(item, ROW_EXCLUDE)
            for item in detail.get("questionInfo") or []
            if isinstance(item, dict)
        ],
        "attachments": [
            scalar_fields(item, ATTACH_EXCLUDE)
            for item in detail.get("attachInfo") or []
            if isinstance(item, dict)
        ],
        "intermediary": one_line(detail.get("intermediarylist")),
    }


def business_fingerprint(detail: dict[str, Any]) -> tuple[str, dict[str, Any]]:
    canonical = canonical_business_content(detail)
    raw = json.dumps(
        canonical,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(raw.encode("utf-8")).hexdigest(), canonical


def official_guide_url(theme_code: str, checklist_id: str) -> str:
    return (
        f"{BASE_URL}/hnywtb/onething/html/eventGuideline.html?"
        + urlencode(
            {
                "onethingCode": theme_code,
                "onethingChecklistId": checklist_id,
            }
        )
    )


def theme_default_path(title: str, theme_code: str) -> str:
    return (
        f"entities/theme-services/{slug_text(title)}-"
        f"{short_hash(theme_code)}.md"
    )


def variant_path(theme_code: str, fingerprint: str) -> str:
    return (
        f"entities/theme-services/details/{theme_code}/variants/"
        f"{fingerprint[:16]}.md"
    )


def region_path(theme_code: str, area_code: str, checklist_id: str) -> str:
    return (
        f"entities/theme-services/details/{theme_code}/regions/"
        f"{area_code}-{short_hash(checklist_id, 8)}.md"
    )


def theme_meta_index(theme_code: str) -> str:
    return f"_meta/onething/{theme_code}/index.md"


def first_text(record: dict[str, Any], *keys: str) -> str:
    for key in keys:
        value = clean_text(record.get(key))
        if value:
            return value
    return ""


def append_field(lines: list[str], label: str, value: Any) -> None:
    text = clean_text(value)
    if text:
        lines.append(f"- {label}：{text}")


def render_conditions(detail: dict[str, Any]) -> list[str]:
    conditions = detail.get("guideCondition") or []
    lines = ["## 办理条件", ""]
    if not conditions:
        return lines + ["官网结构化数据未提供办理条件。", ""]
    for item in conditions:
        if not isinstance(item, dict):
            continue
        text = first_text(item, "processingConditions", "bltj")
        title = one_line(item.get("smmc"))
        if text:
            lines.append(f"- {title + '：' if title and title != '/' else ''}{text}")
    lines.append("")
    return lines


def render_materials(detail: dict[str, Any]) -> list[str]:
    materials = detail.get("materialList") or []
    lines = ["## 申请材料", ""]
    if not materials:
        return lines + ["官网结构化数据未提供申请材料。", ""]
    for index, material in enumerate(materials, 1):
        if not isinstance(material, dict):
            continue
        title = first_text(material, "materialTitle", "clmc") or f"材料{index}"
        lines.extend([f"### {index}. {title}", ""])
        append_field(lines, "材料说明", material.get("clbz"))
        append_field(lines, "材料来源说明", material.get("cllyqdsm"))
        append_field(lines, "提交方式说明", material.get("mtjfssm"))
        append_field(lines, "纸质材料规格", material.get("zzclgg"))
        append_field(lines, "要求提供依据", material.get("yqtgyj"))
        codes = []
        for label, key in (
            ("必要性代码", "clbyx"),
            ("来源代码", "cllyqd"),
            ("形式代码", "clxs"),
            ("提交方式代码", "tgfs"),
        ):
            value = one_line(material.get(key))
            if value:
                codes.append(f"{label} `{value}`")
        if codes:
            lines.append(f"- 官网枚举：{'；'.join(codes)}")
        attachment_names = [
            one_line(item.get("fjmc"))
            for item in material.get("attachList") or []
            if isinstance(item, dict) and one_line(item.get("fjmc"))
        ]
        if attachment_names:
            lines.append(f"- 相关表格或样例：{'；'.join(attachment_names)}")
        lines.append("")
    return lines


def render_process(detail: dict[str, Any]) -> list[str]:
    guide = detail.get("onethingCatalogStandServiceGuide") or {}
    basic = detail.get("basicInfo") or {}
    lines = ["## 办理流程与方式", ""]
    append_field(lines, "办理流程", guide.get("bllctsm"))
    append_field(lines, "网上办理情况", basic.get("reasonLink"))
    append_field(lines, "线下办理说明", guide.get("xxbldsm"))
    append_field(lines, "结果送达说明", guide.get("jgsdsm"))
    completed = one_line(basic.get("completedLimit") or guide.get("cnbjsx"))
    unit_code = one_line(guide.get("hjsxz"))
    if completed:
        suffix = f"；单位代码 `{unit_code}`" if unit_code else ""
        lines.append(f"- 承诺时限（官网原始值）：{completed}{suffix}")
    for label, key in (
        ("办理形式代码", "blxs"),
        ("正常办理渠道代码", "zcblqd"),
        ("线上办理渠道代码", "xsblqd"),
        ("是否支持统一办结代码", "sfzdtybj"),
    ):
        value = one_line(guide.get(key))
        if value:
            lines.append(f"- {label}：`{value}`")
    lines.append("")
    return lines


def render_approvals(detail: dict[str, Any]) -> list[str]:
    approvals = detail.get("approveList") or []
    lines = ["## 联办事项与结果", ""]
    if not approvals:
        return lines + ["官网结构化数据未列出联办事项。", ""]
    lines.extend(["| 联办事项 | 承诺时限原始值 | 办理结果 |", "|---|---:|---|"])
    for item in approvals:
        if not isinstance(item, dict):
            continue
        lines.append(
            f"| {md_cell(item.get('lbsxfwmc') or item.get('tyywblxName'))} "
            f"| {md_cell(item.get('cnsxsx'))} "
            f"| {md_cell(item.get('resultSampleName'))} |"
        )
    lines.append("")
    return lines


def render_charges(detail: dict[str, Any]) -> list[str]:
    basic = detail.get("basicInfo") or {}
    charges = detail.get("chargelist") or []
    lines = ["## 收费与中介", ""]
    append_field(lines, "是否收费代码", basic.get("isCharge"))
    append_field(lines, "是否涉及中介代码", basic.get("isIntermediary"))
    if charges:
        lines.extend(["", "| 收费项目 | 收费标准 | 说明 |", "|---|---|---|"])
        for item in charges:
            if isinstance(item, dict):
                lines.append(
                    f"| {md_cell(item.get('FEENAME'))} "
                    f"| {md_cell(item.get('FEESTAND'))} "
                    f"| {md_cell(item.get('DESCEXPLAIN'))} |"
                )
    intermediary = clean_text(detail.get("intermediarylist"))
    if intermediary:
        lines.extend(["", intermediary])
    lines.append("")
    return lines


def render_faq(detail: dict[str, Any]) -> list[str]:
    questions = detail.get("questionInfo") or []
    lines = ["## 常见问题", ""]
    if not questions:
        return lines + ["官网结构化数据未提供常见问题。", ""]
    for item in questions:
        if not isinstance(item, dict):
            continue
        question = first_text(item, "problem", "wt")
        answer = first_text(item, "answer", "da")
        if question:
            lines.extend([f"### {question}", "", answer or "官网未提供回答。", ""])
    return lines


def render_attachments(detail: dict[str, Any]) -> list[str]:
    attachments = detail.get("attachInfo") or []
    names = [
        one_line(item.get("fjmc"))
        for item in attachments
        if isinstance(item, dict) and one_line(item.get("fjmc"))
    ]
    if not names:
        return []
    return ["## 指南附件", "", *[f"- {name}" for name in names], ""]


def guide_counts(detail: dict[str, Any]) -> dict[str, int]:
    return {
        "materials": len(detail.get("materialList") or []),
        "conditions": len(detail.get("guideCondition") or []),
        "approvals": len(detail.get("approveList") or []),
        "faqs": len(detail.get("questionInfo") or []),
    }


def render_variant(
    wiki: Path,
    theme_code: str,
    title: str,
    fingerprint: str,
    implementations: list[dict[str, Any]],
    representative: dict[str, Any],
    representative_raw: str,
    region_paths: dict[tuple[str, str], str],
    today: str,
    enrich_min_areas: int,
) -> tuple[str, dict[str, Any]]:
    rel = variant_path(theme_code, fingerprint)
    path = wiki / rel
    variant_id = fingerprint[:16]
    count = len(implementations)
    official_url = implementations[0].get("official_guide_url") or official_guide_url(
        theme_code, implementations[0]["checklist_id"]
    )
    counts = guide_counts(representative)
    fields = [
        ("title", f"{title}业务版本"),
        ("created", existing_created(path, today)),
        ("updated", today),
        ("type", "entity"),
        ("tags", ["government-service", "theme-service", "theme-variant", "detail-complete"]),
        ("sources", [RAW_MANIFEST]),
        ("confidence", "high"),
        ("theme_id", theme_code),
        ("variant_id", variant_id),
        ("applicable_area_count", count),
        ("detail_status", "complete"),
        ("answerable", True),
        ("answer_scope", "business_rules"),
        ("source_json", representative_raw),
        ("official_url", official_url),
    ]
    lines = [
        frontmatter(fields),
        f"# {title}业务版本",
        "",
        f"> 本页是官网地区指南经程序化比对后得到的业务内容版本，适用于{count}个地区。",
        "> 材料、条件和流程来自官网原始JSON；地区部门、地点和电话请查看对应地区页。",
        "",
        "## 版本概况",
        "",
        f"- 适用地区数：{count}",
        f"- 申请材料数：{counts['materials']}",
        f"- 办理条件数：{counts['conditions']}",
        f"- 联办事项数：{counts['approvals']}",
        f"- 常见问题数：{counts['faqs']}",
        f"- 内容指纹：`{fingerprint}`",
        "",
    ]
    lines.extend(render_conditions(representative))
    lines.extend(render_materials(representative))
    lines.extend(render_process(representative))
    lines.extend(render_approvals(representative))
    lines.extend(render_charges(representative))
    lines.extend(render_faq(representative))
    lines.extend(render_attachments(representative))
    lines.extend(["## 部分适用地区", ""])
    for item in implementations[:20]:
        key = (item["area_code"], item["checklist_id"])
        area = " / ".join(item.get("area_path") or [item.get("area_name") or item["area_code"]])
        lines.append(f"- {wikilink(region_paths[key], area)}")
    if count > 20:
        lines.append(f"- 其余{count - 20}个地区见主题地区索引。")
    lines.extend(
        [
            "",
            "## 相关页面",
            "",
            f"- {wikilink(theme_meta_index(theme_code), '主题地区与版本索引')}",
            "",
        ]
    )
    summary = (
        f"{title}的业务版本，覆盖{count}个地区，包含"
        f"{counts['materials']}项材料、{counts['conditions']}条条件和"
        f"{counts['approvals']}个联办事项；具体地点和部门按地区查询。"
    )
    rag = {
        "doc_id": f"onething-variant:{theme_code}:{variant_id}",
        "title": f"{title}业务版本",
        "path": rel,
        "page_type": "theme_variant",
        "answerable": True,
        "answer_scope": "business_rules",
        "service_id": theme_code,
        "theme_id": theme_code,
        "variant_id": variant_id,
        "service_objects": [],
        "matter_type": "高效办成一件事",
        "categories": ["高效办成一件事"],
        "organization": "",
        "keywords": [title, theme_code, "一件事", "办理条件", "申请材料", "办理流程"],
        "summary": summary,
        "official_url": official_url,
        "enrich_with_llm": count >= enrich_min_areas,
        "applicable_area_count": count,
    }
    atomic_write_text(path, "\n".join(lines).rstrip() + "\n")
    return rel, rag


def render_region(
    wiki: Path,
    theme_code: str,
    title: str,
    implementation: dict[str, Any],
    detail: dict[str, Any],
    variant_rel: str,
    variant_id: str,
    today: str,
) -> tuple[str, dict[str, Any]]:
    area_code = implementation["area_code"]
    checklist_id = implementation["checklist_id"]
    rel = region_path(theme_code, area_code, checklist_id)
    path = wiki / rel
    area_path = implementation.get("area_path") or [implementation.get("area_name") or area_code]
    area_label = " / ".join(one_line(item) for item in area_path if one_line(item))
    basic = detail.get("basicInfo") or {}
    points = detail.get("guidePoint") or []
    guide = detail.get("onethingCatalogStandServiceGuide") or {}
    official_url = implementation.get("official_guide_url") or official_guide_url(
        theme_code, checklist_id
    )
    fields = [
        ("title", f"{title}（{area_label}）"),
        ("created", existing_created(path, today)),
        ("updated", today),
        ("type", "entity"),
        ("tags", ["government-service", "theme-service", "theme-region", "detail-complete"]),
        ("sources", [RAW_MANIFEST]),
        ("confidence", "high"),
        ("theme_id", theme_code),
        ("variant_id", variant_id),
        ("area_code", area_code),
        ("area_path", area_path),
        ("checklist_id", checklist_id),
        ("detail_status", "complete"),
        ("answerable", True),
        ("answer_scope", "regional_details"),
        ("source_json", implementation["guide_raw_file"]),
        ("official_url", official_url),
    ]
    lines = [
        frontmatter(fields),
        f"# {title}（{area_label}）",
        "",
        "> 本页保存该地区特有的实施部门、联系方式、办理地点和对应业务版本。",
        f"> 材料、条件、流程和常见问题见：{wikilink(variant_rel, '对应业务版本')}。",
        "",
        "## 地区实施信息",
        "",
        f"- 地区路径：{area_label}",
        f"- 地区编码：`{area_code}`",
        f"- 一件事编码：`{theme_code}`",
        f"- 实施清单ID：`{checklist_id}`",
        f"- 实施编码：`{one_line(detail.get('yjsSsqdbm'))}`",
        f"- 对应业务版本：`{variant_id}`",
    ]
    append_field(lines, "牵头部门", basic.get("qtbmmc"))
    append_field(lines, "协同部门", basic.get("cooperateName"))
    append_field(lines, "咨询电话", basic.get("zxdh"))
    append_field(lines, "监督投诉电话", basic.get("jdtsdh"))
    append_field(lines, "网上办理情况", basic.get("reasonLink"))
    append_field(lines, "承诺时限（官网原始值）", basic.get("completedLimit"))
    append_field(lines, "线下办理说明", guide.get("xxbldsm"))
    lines.append("")
    lines.extend(["## 办理地点", ""])
    if points:
        for index, point in enumerate(points, 1):
            if not isinstance(point, dict):
                continue
            name = first_text(point, "pointName", "bldmc") or f"办理点{index}"
            lines.extend([f"### {name}", ""])
            append_field(lines, "详细地址", point.get("xxdz") or point.get("infoAddr"))
            append_field(lines, "办理时间", point.get("processingTime") or point.get("blsj"))
            lines.append("")
    else:
        lines.extend(["官网结构化数据未提供办理地点。", ""])
    lines.extend(
        [
            "## 业务内容",
            "",
            f"- {wikilink(variant_rel, '查看本地区对应的材料、条件、流程和常见问题')}",
            "",
            "## 相关页面",
            "",
            f"- {wikilink(theme_meta_index(theme_code), '主题地区与版本索引')}",
            "",
        ]
    )
    organization = one_line(basic.get("qtbmmc"))
    summary = (
        f"{area_label}的{title}地区实施页，由{organization or '当地实施部门'}办理，"
        "具体材料、条件和流程见对应业务版本。"
    )
    keywords = [title, theme_code, area_code, checklist_id, area_label]
    keywords.extend(one_line(item) for item in area_path)
    if organization:
        keywords.append(organization)
    rag = {
        "doc_id": f"onething-region:{theme_code}:{area_code}:{checklist_id}",
        "title": f"{title}（{area_label}）",
        "path": rel,
        "page_type": "theme_region",
        "answerable": True,
        "answer_scope": "regional_details",
        "service_id": theme_code,
        "theme_id": theme_code,
        "variant_id": variant_id,
        "related_doc_ids": [f"onething-variant:{theme_code}:{variant_id}"],
        "service_objects": [],
        "matter_type": "高效办成一件事",
        "categories": ["高效办成一件事", *[one_line(item) for item in area_path]],
        "organization": organization,
        "keywords": keywords,
        "summary": summary,
        "official_url": official_url,
        "enrich_with_llm": False,
    }
    atomic_write_text(path, "\n".join(lines).rstrip() + "\n")
    return rel, rag


def write_theme_indexes(
    wiki: Path,
    theme_code: str,
    title: str,
    variants: list[dict[str, Any]],
    region_rows: list[dict[str, Any]],
    variant_paths: dict[str, str],
    region_paths: dict[tuple[str, str], str],
    chunk_size: int = 100,
) -> list[str]:
    root_rel = theme_meta_index(theme_code)
    index_paths = [root_rel]
    parts: list[str] = []
    sorted_regions = sorted(
        region_rows,
        key=lambda item: (
            tuple(item.get("area_path") or []),
            item["area_code"],
            item["checklist_id"],
        ),
    )
    for start in range(0, len(sorted_regions), chunk_size):
        number = start // chunk_size + 1
        rel = f"_meta/onething/{theme_code}/regions-part-{number:02d}.md"
        parts.append(rel)
        index_paths.append(rel)
        lines = [f"# {title}地区索引（第{number}部分）", ""]
        for item in sorted_regions[start : start + chunk_size]:
            key = (item["area_code"], item["checklist_id"])
            area = " / ".join(item.get("area_path") or [item.get("area_name") or item["area_code"]])
            lines.append(
                f"- {wikilink(region_paths[key], area)} — "
                f"{wikilink(variant_paths[item['fingerprint']], '业务版本')}"
            )
        lines.extend(["", f"- {wikilink(root_rel, '返回主题索引')}", ""])
        atomic_write_text(wiki / rel, "\n".join(lines))
    lines = [
        f"# {title}地区与版本索引",
        "",
        f"> 共{len(region_rows)}份地区实施指南，去重后为{len(variants)}个业务版本。",
        "",
        "## 业务版本",
        "",
    ]
    for group in sorted(variants, key=lambda item: (-len(item["implementations"]), item["fingerprint"])):
        count = len(group["implementations"])
        lines.append(
            f"- {wikilink(variant_paths[group['fingerprint']], '业务版本 ' + group['fingerprint'][:8])}"
            f" — 适用{count}个地区"
        )
    lines.extend(["", "## 地区索引", ""])
    for index, rel in enumerate(parts, 1):
        lines.append(f"- {wikilink(rel, f'第{index}部分')}")
    lines.append("")
    atomic_write_text(wiki / root_rel, "\n".join(lines))
    return index_paths


def render_theme_overview(
    wiki: Path,
    theme: dict[str, Any],
    catalog_theme: dict[str, Any] | None,
    theme_rel: str,
    groups: list[dict[str, Any]],
    variant_paths: dict[str, str],
    today: str,
) -> tuple[dict[str, Any], tuple[str, str, str]]:
    theme_code = str(theme["id"])
    title = one_line(
        (catalog_theme or {}).get("theme_title")
        or theme.get("name")
        or theme.get("_title")
        or theme_code
    )
    path = wiki / theme_rel
    implementations = (catalog_theme or {}).get("implementations") or []
    available = bool(implementations)
    official_url = (
        implementations[0].get("official_guide_url")
        if implementations
        else f"{BASE_URL}/ywtb/gov-search/index.html#/things"
    )
    status = "complete" if available else "unavailable"
    fields = [
        ("title", title),
        ("created", existing_created(path, today)),
        ("updated", today),
        ("type", "entity"),
        (
            "tags",
            [
                "government-service",
                "theme-service",
                "detail-complete" if available else "detail-unavailable",
            ],
        ),
        ("sources", [RAW_MANIFEST]),
        ("confidence", "high" if available else "low"),
        ("theme_id", theme_code),
        ("matter_type", one_line(theme.get("type_name")) or "高效办成一件事"),
        ("detail_status", status),
        ("answerable", available),
        ("answer_scope", "overview_and_region_navigation" if available else "navigation_only"),
        ("official_url", official_url),
    ]
    if available:
        largest = max((len(group["implementations"]) for group in groups), default=0)
        lines = [
            frontmatter(fields),
            f"# {title}",
            "",
            "> 本页提供主题概况和地区导航。不同地区的部门、地点、材料或流程可能不同。",
            "> 查询具体办理要求时，应同时使用对应地区页和业务版本页。",
            "",
            "## 主题概况",
            "",
            f"- 地区实施指南：{len(implementations)}份",
            f"- 去重后业务版本：{len(groups)}个",
            f"- 覆盖地区最多的版本：{largest}个地区",
            f"- 主题编码：`{theme_code}`",
            "",
            "## 主要业务版本",
            "",
        ]
        for group in sorted(groups, key=lambda item: (-len(item["implementations"]), item["fingerprint"]))[:10]:
            count = len(group["implementations"])
            lines.append(
                f"- {wikilink(variant_paths[group['fingerprint']], '业务版本 ' + group['fingerprint'][:8])}"
                f" — 覆盖{count}个地区"
            )
        if len(groups) > 10:
            lines.append(f"- 其余{len(groups) - 10}个版本见完整索引。")
        lines.extend(
            [
                "",
                "## 查询提示",
                "",
                "如果问题涉及材料、条件、办理地点、部门、电话或时限，请提供所在市、县区或街道。",
                "",
                "## 地区与版本索引",
                "",
                f"- {wikilink(theme_meta_index(theme_code), '查看全部地区和业务版本')}",
                "",
            ]
        )
        summary = (
            f"{title}在湖南省有{len(implementations)}份地区实施指南，"
            f"去重后为{len(groups)}个业务版本；具体要求应按办理地区查询。"
        )
    else:
        lines = [
            frontmatter(fields),
            f"# {title}",
            "",
            "> 这是湖南政务服务智能搜索中的“高效办成一件事”主题。",
            "",
            "## 数据状态",
            "",
            "本次地区实施清单遍历未发现可用办事指南，本页不能作为材料、条件或流程的回答依据。",
            "",
        ]
        summary = f"{title}是“高效办成一件事”主题；当前未发现可用地区实施指南。"
    atomic_write_text(path, "\n".join(lines).rstrip() + "\n")
    rag = {
        "doc_id": f"theme:{theme_code}",
        "title": title,
        "path": theme_rel,
        "page_type": "theme_service",
        "answerable": available,
        "answer_scope": "overview_and_region_navigation" if available else "navigation_only",
        "service_id": theme_code,
        "theme_id": theme_code,
        "service_objects": [],
        "matter_type": one_line(theme.get("type_name")) or "高效办成一件事",
        "categories": ["高效办成一件事"],
        "organization": "",
        "keywords": [title, "高效办成一件事", "一件事", theme_code],
        "summary": summary,
        "official_url": official_url,
        "enrich_with_llm": available,
    }
    return rag, (theme_rel, title, summary)


def load_candidates(input_root: Path) -> list[dict[str, Any]]:
    payload = read_json(input_root / "audit" / "search_only_candidates.json")
    items = payload.get("items") if isinstance(payload, dict) else None
    if not isinstance(items, list):
        raise ValueError("search_only_candidates.json 的 items 不是数组")
    return sorted(
        [
            item
            for item in items
            if isinstance(item, dict)
            and str(item.get("type") or "").lower() == "theme"
            and item.get("id")
        ],
        key=lambda item: str(item["id"]),
    )


def load_previous_theme_paths(wiki: Path) -> dict[str, str]:
    path = wiki / "_meta" / "generated_manifest.json"
    if not path.exists():
        return {}
    try:
        return dict(read_json(path).get("theme_paths") or {})
    except (OSError, ValueError, TypeError):
        return {}


def convert_onething(
    input_root: Path,
    wiki: Path,
    themes: list[dict[str, Any]],
    theme_paths: dict[str, str] | None = None,
    today: str | None = None,
    enrich_min_areas: int = 5,
) -> dict[str, Any]:
    catalog_path = input_root / "catalog" / "onething_guides.json"
    previous_manifest_path = wiki / "_meta" / "onething_generated_manifest.json"
    previous_generated_pages: set[str] = set()
    if previous_manifest_path.exists():
        try:
            previous_generated_pages = set(
                read_json(previous_manifest_path).get("generated_pages") or []
            )
        except (OSError, ValueError, TypeError):
            previous_generated_pages = set()
    payload = read_json(catalog_path)
    catalog_themes = payload.get("themes") if isinstance(payload, dict) else None
    if not isinstance(catalog_themes, list):
        raise ValueError("onething_guides.json 的 themes 不是数组")
    by_code = {
        str(item["theme_code"]): item
        for item in catalog_themes
        if isinstance(item, dict) and item.get("theme_code")
    }
    theme_paths = dict(theme_paths or {})
    today = today or date.today().isoformat()
    rag_records: list[dict[str, Any]] = []
    theme_entries: list[tuple[str, str, str]] = []
    generated_pages: set[str] = set()
    variant_pages: set[str] = set()
    region_pages: set[str] = set()
    index_pages: set[str] = set()
    manifest_themes: list[dict[str, Any]] = []
    skipped: list[dict[str, str]] = []
    llm_priority_count = 0

    for theme_index, theme in enumerate(themes, 1):
        theme_code = str(theme["id"])
        catalog_theme = by_code.get(theme_code)
        title = one_line(
            (catalog_theme or {}).get("theme_title")
            or theme.get("name")
            or theme.get("_title")
            or theme_code
        )
        theme_rel = theme_paths.get(theme_code) or theme_default_path(title, theme_code)
        theme_paths[theme_code] = theme_rel
        implementations = (catalog_theme or {}).get("implementations") or []
        groups_by_hash: dict[str, dict[str, Any]] = {}
        implementation_details: dict[tuple[str, str], dict[str, Any]] = {}

        for implementation in implementations:
            if not isinstance(implementation, dict):
                continue
            raw_rel = str(implementation.get("guide_raw_file") or "")
            raw_path = input_root / raw_rel
            try:
                guide_payload = read_json(raw_path)
                detail = guide_payload.get("data") if isinstance(guide_payload, dict) else None
                if not isinstance(detail, dict) or not detail:
                    raise ValueError("data不是非空对象")
                fingerprint, _canonical = business_fingerprint(detail)
            except (OSError, ValueError, TypeError) as exc:
                skipped.append(
                    {
                        "theme_code": theme_code,
                        "area_code": str(implementation.get("area_code") or ""),
                        "checklist_id": str(implementation.get("checklist_id") or ""),
                        "reason": str(exc),
                    }
                )
                continue
            normalized = dict(implementation)
            normalized["fingerprint"] = fingerprint
            key = (normalized["area_code"], normalized["checklist_id"])
            implementation_details[key] = detail
            group = groups_by_hash.setdefault(
                fingerprint,
                {
                    "fingerprint": fingerprint,
                    "representative": detail,
                    "representative_raw": raw_rel,
                    "implementations": [],
                },
            )
            group["implementations"].append(normalized)

        groups = list(groups_by_hash.values())
        local_region_paths: dict[tuple[str, str], str] = {}
        for group in groups:
            for implementation in group["implementations"]:
                key = (implementation["area_code"], implementation["checklist_id"])
                local_region_paths[key] = region_path(
                    theme_code,
                    implementation["area_code"],
                    implementation["checklist_id"],
                )
        local_variant_paths = {
            fingerprint: variant_path(theme_code, fingerprint)
            for fingerprint in groups_by_hash
        }

        region_rows: list[dict[str, Any]] = []
        for group in groups:
            fingerprint = group["fingerprint"]
            variant_id = fingerprint[:16]
            for implementation in group["implementations"]:
                key = (implementation["area_code"], implementation["checklist_id"])
                rel, rag = render_region(
                    wiki,
                    theme_code,
                    title,
                    implementation,
                    implementation_details[key],
                    local_variant_paths[fingerprint],
                    variant_id,
                    today,
                )
                region_rows.append(implementation)
                rag_records.append(rag)
                generated_pages.add(rel)
                region_pages.add(rel)

        variant_manifest: list[dict[str, Any]] = []
        for group in sorted(groups, key=lambda item: (-len(item["implementations"]), item["fingerprint"])):
            fingerprint = group["fingerprint"]
            rel, rag = render_variant(
                wiki,
                theme_code,
                title,
                fingerprint,
                group["implementations"],
                group["representative"],
                group["representative_raw"],
                local_region_paths,
                today,
                enrich_min_areas,
            )
            rag_records.append(rag)
            generated_pages.add(rel)
            variant_pages.add(rel)
            if rag["enrich_with_llm"]:
                llm_priority_count += 1
            variant_manifest.append(
                {
                    "variant_id": fingerprint[:16],
                    "fingerprint": fingerprint,
                    "path": rel,
                    "applicable_area_count": len(group["implementations"]),
                    "representative_source_json": group["representative_raw"],
                    "area_mappings": [
                        {
                            "area_code": item["area_code"],
                            "area_path": item.get("area_path") or [],
                            "checklist_id": item["checklist_id"],
                            "region_path": local_region_paths[
                                (item["area_code"], item["checklist_id"])
                            ],
                        }
                        for item in group["implementations"]
                    ],
                }
            )

        if groups:
            paths = write_theme_indexes(
                wiki,
                theme_code,
                title,
                groups,
                region_rows,
                local_variant_paths,
                local_region_paths,
            )
            index_pages.update(paths)
        overview_rag, overview_entry = render_theme_overview(
            wiki,
            theme,
            catalog_theme,
            theme_rel,
            groups,
            local_variant_paths,
            today,
        )
        rag_records.append(overview_rag)
        if overview_rag["enrich_with_llm"]:
            llm_priority_count += 1
        theme_entries.append(overview_entry)
        generated_pages.add(theme_rel)
        manifest_themes.append(
            {
                "theme_code": theme_code,
                "theme_title": title,
                "overview_path": theme_rel,
                "implementation_count": len(region_rows),
                "variant_count": len(groups),
                "variants": variant_manifest,
            }
        )
        if theme_index == 1 or theme_index % 10 == 0 or theme_index == len(themes):
            print(
                f"  一件事转换：{theme_index}/{len(themes)} "
                f"（累计版本{len(variant_pages)}，地区页{len(region_pages)}）"
            )

    stale_pages = sorted(
        path
        for path in previous_generated_pages - generated_pages
        if (wiki / path).exists()
    )
    manifest = {
        "schema": "hunan-gov-onething-wiki-manifest/1.0",
        "converter_version": VERSION,
        "generated_at": date.today().isoformat(),
        "input_catalog": "catalog/onething_guides.json",
        "theme_count": len(themes),
        "variant_count": len(variant_pages),
        "region_page_count": len(region_pages),
        "rag_record_count": len(rag_records),
        "llm_enrichment_priority_count": llm_priority_count,
        "enrich_min_areas": enrich_min_areas,
        "skipped_count": len(skipped),
        "skipped": skipped,
        "stale_generated_page_count": len(stale_pages),
        "stale_generated_pages": stale_pages,
        "themes": manifest_themes,
        "generated_pages": sorted(generated_pages),
        "index_pages": sorted(index_pages),
    }
    atomic_write_json(wiki / "_meta" / "onething_generated_manifest.json", manifest)
    return {
        "rag_records": rag_records,
        "theme_entries": theme_entries,
        "theme_paths": theme_paths,
        "generated_pages": generated_pages,
        "variant_pages": variant_pages,
        "region_pages": region_pages,
        "index_pages": index_pages,
        "manifest": manifest,
    }


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="将“一件事”地区JSON转换为去重的Hermes LLM-WIKI"
    )
    parser.add_argument("--input", type=Path, default=Path.cwd() / "gov-wiki")
    parser.add_argument("--output", type=Path)
    parser.add_argument("--theme-code", action="append", default=[])
    parser.add_argument(
        "--enrich-min-areas",
        type=int,
        default=5,
        help="业务版本覆盖至少多少地区时交给DeepSeek增强，默认5",
    )
    args = parser.parse_args()
    args.input = args.input.resolve()
    args.output = (args.output or args.input / "wiki-onething").resolve()
    if args.enrich_min_areas <= 0:
        parser.error("--enrich-min-areas 必须大于0")
    return args


def main() -> int:
    args = parse_args()
    themes = load_candidates(args.input)
    if args.theme_code:
        wanted = set(args.theme_code)
        themes = [item for item in themes if str(item["id"]) in wanted]
        missing = wanted - {str(item["id"]) for item in themes}
        if missing:
            raise SystemExit(f"找不到主题：{', '.join(sorted(missing))}")
    result = convert_onething(
        args.input,
        args.output,
        themes,
        load_previous_theme_paths(args.output),
        enrich_min_areas=args.enrich_min_areas,
    )
    rag_path = args.output / "_meta" / "onething_rag_catalog.jsonl"
    atomic_write_text(
        rag_path,
        "".join(
            json.dumps(row, ensure_ascii=False) + "\n"
            for row in result["rag_records"]
        ),
    )
    manifest = result["manifest"]
    print("\n一件事转换完成")
    print(f"主题页：{manifest['theme_count']}")
    print(f"去重业务版本页：{manifest['variant_count']}")
    print(f"地区页：{manifest['region_page_count']}")
    print(f"建议DeepSeek增强：{manifest['llm_enrichment_priority_count']}")
    print(f"跳过异常：{manifest['skipped_count']}")
    print(f"RAG清单：{rag_path}")
    return 0 if not manifest["skipped_count"] else 2


if __name__ == "__main__":
    raise SystemExit(main())
