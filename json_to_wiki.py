#!/usr/bin/env python3
"""将湖南政务服务结构化 JSON 程序化转换为 Hermes LLM-WIKI Markdown。

转换原则：
- 普通事项以 enriched_catalog.json + raw/details/*.json 生成完整实体页；
- 智能搜索中的 ``type=theme`` 生成独立主题索引页，明确标记详情不可用；
- 不调用大模型，不补写原始数据中不存在的条件、材料、地址或时限单位；
- 生成 SCHEMA、分层索引、日志、概念页、RAG 检索清单和转换报告；
- 输出可重复生成，已有 service_id 会复用上一版文件路径。
"""

from __future__ import annotations

import argparse
import hashlib
import html
import json
import os
import re
import sys
import unicodedata
from collections import defaultdict
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any, Iterable
from urllib.parse import urlencode


VERSION = "1.0.0"
BASE_URL = "https://zwfw-new.hunan.gov.cn"
RAW_MANIFEST = "raw/manifests/hunan-government-service-dataset.md"

MATTER_TAGS = {
    "行政许可": "administrative-license",
    "其他行政权力": "other-administrative-power",
    "公共服务": "public-service",
    "行政确认": "administrative-confirmation",
    "行政奖励": "administrative-reward",
    "行政给付": "administrative-benefit",
    "行政裁决": "administrative-adjudication",
}

TAG_TAXONOMY = [
    "government-service",
    "personal-service",
    "legal-entity-service",
    "theme-service",
    "detail-complete",
    "detail-unavailable",
    *MATTER_TAGS.values(),
    "other-matter-type",
]

PLACEHOLDERS = {"", "暂无", "暂无信息", "无", "无。", "/", "null", "none"}


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def read_json(path: Path) -> Any:
    with path.open("r", encoding="utf-8") as file:
        return json.load(file)


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
    compact: list[str] = []
    for line in lines:
        if line or (compact and compact[-1]):
            compact.append(line)
    return "\n".join(compact).strip()


def meaningful(value: Any) -> bool:
    return clean_text(value).lower() not in PLACEHOLDERS


def one_line(value: Any) -> str:
    return re.sub(r"\s+", " ", clean_text(value)).strip()


def md_cell(value: Any) -> str:
    return clean_text(value).replace("|", "\\|").replace("\n", "<br>") or "—"


def md_link_label(value: Any) -> str:
    return one_line(value).replace("[", "［").replace("]", "］") or "附件"


def yaml_value(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False)


def frontmatter(fields: list[tuple[str, Any]]) -> str:
    lines = ["---"]
    for key, value in fields:
        lines.append(f"{key}: {yaml_value(value)}")
    lines.extend(["---", ""])
    return "\n".join(lines)


def short_hash(value: str, length: int = 10) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()[:length]


def slug_text(value: Any, fallback: str = "page", max_length: int = 48) -> str:
    text = unicodedata.normalize("NFKC", one_line(value)).lower()
    text = re.sub(r"[\\/:*?\"<>|#\[\]{}()（）【】《》“”‘’，。；：、·]+", "-", text)
    text = re.sub(r"\s+", "-", text)
    text = re.sub(r"-+", "-", text).strip("-. ")
    return (text[:max_length].rstrip("-.") or fallback)


def page_path(kind: str, title: str, stable_id: str) -> str:
    slug = slug_text(title)
    return f"entities/{kind}/{slug}-{short_hash(stable_id)}.md"


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


def raw_detail_path(service_id: str) -> str:
    return f"raw/details/{service_id[:2]}/{service_id}.json"


def asset_url(path: Any, bucket: Any) -> str | None:
    path_text = str(path or "").strip()
    bucket_text = str(bucket or "").strip()
    if not path_text or not bucket_text:
        return None
    return f"{BASE_URL}/picPathMapping?{urlencode({'picPath': path_text, 'bucketName': bucket_text})}"


def add_unique(target: list[str], value: Any, max_length: int = 80) -> None:
    text = one_line(value)
    if text and len(text) <= max_length and text not in target:
        target.append(text)


def service_objects(record: dict[str, Any], detail: dict[str, Any]) -> list[str]:
    result: list[str] = []
    types = record.get("service_types") or []
    if "personal" in types:
        result.append("个人")
    if "legal_entity" in types:
        result.append("法人")
    if not result:
        text = one_line(detail.get("serveObject"))
        if text:
            result.append(text)
    return result


def matter_tag(matter_type: str) -> str:
    return MATTER_TAGS.get(matter_type, "other-matter-type")


def service_tags(record: dict[str, Any], detail: dict[str, Any]) -> list[str]:
    tags = ["government-service", "detail-complete"]
    if "personal" in (record.get("service_types") or []):
        tags.append("personal-service")
    if "legal_entity" in (record.get("service_types") or []):
        tags.append("legal-entity-service")
    tags.append(matter_tag(one_line(detail.get("typeName"))))
    return tags


def guide_url(record: dict[str, Any]) -> str:
    detail = record.get("detail") or {}
    if detail.get("official_guide_url"):
        return str(detail["official_guide_url"])
    query: dict[str, str] = {"id": str(record["service_id"])}
    types = record.get("service_types") or []
    if types:
        query["type"] = "gr" if types[0] == "personal" else "fr"
    if record.get("rights_codes"):
        query["rightsCode"] = str(record["rights_codes"][0])
    if record.get("business_codes"):
        query["ywCode"] = str(record["business_codes"][0])
    return f"{BASE_URL}/hnywtb/service/guide.html?{urlencode(query)}"


def render_text_section(title: str, value: Any) -> list[str]:
    if not meaningful(value):
        return []
    return [f"## {title}", "", clean_text(value), ""]


def content_line_count(value: Any) -> int:
    text = clean_text(value)
    return text.count("\n") + 1 if text else 0


def compact_long_content(value: Any, group_size: int = 8) -> str:
    """保留视觉换行，同时避免超长法规清单形成数百个Markdown物理行。"""
    lines = clean_text(value).splitlines()
    if len(lines) <= 120:
        return "\n".join(lines)
    groups = [lines[index : index + group_size] for index in range(0, len(lines), group_size)]
    return "\n\n".join("<br>".join(group) for group in groups)


def render_materials(detail: dict[str, Any]) -> list[str]:
    materials = [x for x in (detail.get("materialList") or []) if isinstance(x, dict)]
    if not materials:
        return []
    lines = ["## 申请材料", ""]
    for index, material in enumerate(materials, start=1):
        title = one_line(material.get("materialTitle") or material.get("standardMaterialsName"))
        lines.append(f"### {index}. {title or '未命名材料'}")
        facts = [
            ("必要性", material.get("isMust")),
            ("材料类型", material.get("materialType")),
            ("材料形式", material.get("materialStandard")),
            ("份数", material.get("copiesNum")),
            ("来源渠道", material.get("sourceChannel")),
            ("提交方式", material.get("commitWay")),
            ("纸张规格", material.get("pageformat")),
        ]
        fact_text = [
            f"{label}：{one_line(value)}"
            for label, value in facts
            if value not in (None, "", [], {})
        ]
        if fact_text:
            lines.append("- 提交要求：" + "；".join(fact_text))
        descriptions = []
        for label, key in (
            ("材料说明", "materialExplain"),
            ("来源说明", "sourceexplain"),
            ("受理标准", "acceptStandard"),
        ):
            if meaningful(material.get(key)):
                descriptions.append(f"{label}：{one_line(material.get(key))}")
        if descriptions:
            lines.append("- 说明：" + "；".join(descriptions))
        if meaningful(material.get("bylaw")):
            lines.append(f"- 材料依据：{one_line(material.get('bylaw'))}")
        attachments = [x for x in (material.get("attachList") or []) if isinstance(x, dict)]
        attachment_links: list[str] = []
        for attachment in attachments:
            url = asset_url(
                attachment.get("attachPath") or attachment.get("filePath"),
                attachment.get("bucketName"),
            )
            if url:
                name = md_link_label(
                    attachment.get("attachName") or attachment.get("fileName")
                )
                attachment_links.append(f"[{name}]({url})")
        if attachment_links:
            lines.append("- 附件：" + "；".join(attachment_links))
    lines.append("")
    return lines


def render_process(detail: dict[str, Any]) -> list[str]:
    description = detail.get("processDescript")
    processes = [x for x in (detail.get("lctList") or []) if isinstance(x, dict)]
    if not meaningful(description) and not processes:
        return []
    lines = ["## 办理流程", ""]
    if meaningful(description):
        lines.extend([clean_text(description), ""])
    for process in processes:
        url = asset_url(
            process.get("flowc") or process.get("flowPath"),
            process.get("bucketName") or process.get("flowBucketName"),
        )
        if url:
            lines.append(f"- [查看办理流程图]({url})")
    lines.append("")
    return lines


def render_limits(detail: dict[str, Any]) -> list[str]:
    facts = [
        ("法定办结时限", detail.get("approveLimit")),
        ("法定时限说明", detail.get("approveLimitExplain")),
        ("承诺办结时限", detail.get("commitmentLimit")),
        ("承诺时限说明", detail.get("commitmentLimitExplain")),
        ("是否收费", detail.get("isCharge")),
        ("是否需要特别程序", detail.get("isSpecialProcedure")),
        ("特别程序类型", detail.get("specialProcedureType")),
        ("特别程序时限", detail.get("specialProcedureTimelimit")),
        ("是否涉及中介服务", detail.get("isIntermediaryServices")),
    ]
    present = [(label, value) for label, value in facts if value not in (None, "", [], {})]
    if not present:
        return []
    lines = ["## 办理时限与费用", "", "| 项目 | 官网数据 |", "|---|---|"]
    lines.extend(f"| {label} | {md_cell(value)} |" for label, value in present)
    lines.append("")
    lines.append("> 时限单位仅按官网原始字段展示；原始数据未明确单位时，本页不作推测。")
    lines.append("")
    return lines


def render_results(detail: dict[str, Any]) -> list[str]:
    results = [x for x in (detail.get("resultList") or []) if isinstance(x, dict)]
    fallback = detail.get("resultSampleName")
    if not results and not meaningful(fallback):
        return []
    lines = ["## 办理结果", ""]
    if meaningful(fallback):
        lines.append(f"- 结果名称：{one_line(fallback)}")
    seen: set[tuple[str, str]] = set()
    for result in results:
        name = one_line(result.get("resultSampleName") or result.get("fileName")) or "办理结果样本"
        url = asset_url(
            result.get("resultChart") or result.get("attachPath"),
            result.get("bucketName"),
        )
        key = (name, url or "")
        if key in seen:
            continue
        seen.add(key)
        lines.append(f"- [{md_link_label(name)}]({url})" if url else f"- {name}")
    lines.append("")
    return lines


def render_section_subpage(
    output: Path,
    page_rel: str,
    record: dict[str, Any],
    detail: dict[str, Any],
    main_title: str,
    section_title: str,
    section_value: Any,
    role: str,
    related: list[tuple[str, str]],
    official_url: str,
    raw_file: str,
    today: str,
) -> tuple[str, str, dict[str, Any]]:
    rel = page_rel[:-3] + f"-{role}.md"
    title = f"{main_title}—{section_title}"
    path = output / rel
    fields = [
        ("title", title),
        ("created", existing_created(path, today)),
        ("updated", today),
        ("type", "entity"),
        ("tags", service_tags(record, detail)),
        ("sources", [RAW_MANIFEST]),
        ("confidence", "medium"),
        ("service_id", record["service_id"]),
        ("page_role", role),
        ("detail_status", "complete"),
        ("official_url", official_url),
        ("source_json", raw_file),
    ]
    lines = [
        frontmatter(fields),
        f"# {title}",
        "",
        f"> {main_title}的{section_title}完整内容。",
        "",
        f"## {section_title}",
        "",
        compact_long_content(section_value),
        "",
        "## 相关页面",
        "",
        f"- {wikilink(page_rel, main_title)}",
        f"- {wikilink(related[0][0], related[0][1])}",
        "",
        "## 数据说明",
        "",
        f"- 原始JSON：`{raw_file}`",
        f"- 官方页面：[{main_title}]({official_url})",
        "- 本页由程序按官网结构化数据生成；实际办理要求以官网最新页面为准。",
        "",
    ]
    objects = service_objects(record, detail)
    categories = [
        one_line(item.get("name"))
        for item in (record.get("categories") or [])
        if isinstance(item, dict) and one_line(item.get("name"))
    ]
    keywords = build_keywords(record, detail)
    add_unique(keywords, section_title)
    rag = {
        "doc_id": f"service:{record['service_id']}:{role}",
        "title": title,
        "path": rel,
        "page_type": f"government_service_{role.replace('-', '_')}",
        "answerable": True,
        "service_id": record["service_id"],
        "service_objects": objects,
        "matter_type": one_line(detail.get("typeName")) or "其他事项类型",
        "categories": categories,
        "organization": one_line(detail.get("orgName")),
        "keywords": keywords,
        "summary": f"{main_title}的{section_title}完整内容。",
        "official_url": official_url,
    }
    return rel, "\n".join(lines).rstrip() + "\n", rag


def service_summary(record: dict[str, Any], detail: dict[str, Any]) -> str:
    title = one_line(detail.get("unifyName") or record.get("title"))
    objects = "、".join(service_objects(record, detail)) or "未注明"
    matter = one_line(detail.get("typeName")) or "未注明事项类型"
    org = one_line(detail.get("orgName"))
    suffix = f"，由{org}办理" if org else ""
    return f"{title}，服务对象为{objects}，事项类型为{matter}{suffix}。"


def build_keywords(record: dict[str, Any], detail: dict[str, Any]) -> list[str]:
    keywords: list[str] = []
    for value in (
        record.get("title"),
        detail.get("unifyName"),
        detail.get("serveObject"),
        detail.get("typeName"),
        detail.get("orgName"),
        detail.get("deptName"),
        detail.get("blbm"),
        detail.get("schemeLevels"),
    ):
        add_unique(keywords, value)
    for category in record.get("categories") or []:
        if isinstance(category, dict):
            add_unique(keywords, category.get("name"))
    for key in ("rights_codes", "business_codes"):
        for value in record.get(key) or []:
            add_unique(keywords, value)
    for material in detail.get("materialList") or []:
        if isinstance(material, dict):
            add_unique(keywords, material.get("materialTitle"), max_length=60)
        if len(keywords) >= 40:
            break
    return keywords[:40]


def concept_paths(
    record: dict[str, Any],
    detail: dict[str, Any],
    matter_paths: dict[str, str],
    category_paths: dict[tuple[str, str], str],
) -> list[tuple[str, str]]:
    links: list[tuple[str, str]] = []
    types = record.get("service_types") or []
    if "personal" in types:
        links.append(("concepts/service-objects/personal-service.md", "个人服务"))
    if "legal_entity" in types:
        links.append(("concepts/service-objects/legal-entity-service.md", "法人服务"))
    matter = one_line(detail.get("typeName")) or "其他事项类型"
    links.append((matter_paths[matter], matter))
    for category in record.get("categories") or []:
        if not isinstance(category, dict):
            continue
        key = (str(category.get("service_type") or ""), str(category.get("code") or ""))
        if key in category_paths:
            links.append((category_paths[key], str(category.get("name") or "主题分类")))
    seen: set[str] = set()
    return [(path, alias) for path, alias in links if not (path in seen or seen.add(path))]


def render_service_page(
    output: Path,
    page_rel: str,
    record: dict[str, Any],
    detail: dict[str, Any],
    matter_paths: dict[str, str],
    category_paths: dict[tuple[str, str], str],
    today: str,
) -> tuple[str, dict[str, Any], list[tuple[str, str, dict[str, Any]]]]:
    path = output / page_rel
    title = one_line(detail.get("unifyName") or record.get("title"))
    objects = service_objects(record, detail)
    matter = one_line(detail.get("typeName")) or "其他事项类型"
    categories = [
        one_line(item.get("name"))
        for item in (record.get("categories") or [])
        if isinstance(item, dict) and one_line(item.get("name"))
    ]
    official_url = guide_url(record)
    raw_file = (record.get("detail") or {}).get("raw_file") or raw_detail_path(record["service_id"])
    related = concept_paths(record, detail, matter_paths, category_paths)
    fields = [
        ("title", title),
        ("created", existing_created(path, today)),
        ("updated", today),
        ("type", "entity"),
        ("tags", service_tags(record, detail)),
        ("sources", [RAW_MANIFEST]),
        ("confidence", "medium"),
        ("service_id", record["service_id"]),
        ("service_objects", objects),
        ("matter_type", matter),
        ("organization", one_line(detail.get("orgName"))),
        ("department", one_line(detail.get("deptName") or detail.get("blbm"))),
        ("implementation_level", one_line(detail.get("schemeLevels"))),
        ("categories", categories),
        ("rights_codes", record.get("rights_codes") or []),
        ("business_codes", record.get("business_codes") or []),
        ("detail_status", "complete"),
        ("official_url", official_url),
        ("source_json", raw_file),
    ]
    lines = [frontmatter(fields), f"# {title}", "", f"> {service_summary(record, detail)}", ""]
    facts = [
        ("服务对象", "、".join(objects)),
        ("事项类型", matter),
        ("实施机构", detail.get("orgName")),
        ("主管部门", detail.get("deptName") or detail.get("blbm")),
        ("实施层级", detail.get("schemeLevels")),
        ("办理范围", detail.get("doScope")),
        ("网上办理", detail.get("isonline")),
        ("最少现场办理次数", detail.get("minSeq")),
    ]
    lines.extend(["## 基本信息", "", "| 项目 | 内容 |", "|---|---|"])
    lines.extend(
        f"| {label} | {md_cell(value)} |"
        for label, value in facts
        if value not in (None, "", [], {})
    )
    lines.extend(["", f"- 官网办理指南：[{title}]({official_url})", ""])
    extras: list[tuple[str, str, dict[str, Any]]] = []
    acceptance_lines = render_text_section("受理条件", detail.get("acceptanceCondition"))
    if content_line_count(detail.get("acceptanceCondition")) > 40:
        extra = render_section_subpage(
            output,
            page_rel,
            record,
            detail,
            title,
            "受理条件",
            detail.get("acceptanceCondition"),
            "conditions",
            related,
            official_url,
            raw_file,
            today,
        )
        extras.append(extra)
        lines.extend(
            [
                "## 受理条件",
                "",
                f"受理条件内容较长，详见：{wikilink(extra[0], extra[2]['title'])}",
                "",
            ]
        )
    else:
        lines.extend(acceptance_lines)
    material_lines = render_materials(detail)
    if len(material_lines) > 50:
        material_rel = page_rel[:-3] + "-materials.md"
        material_title = f"{title}—申请材料"
        material_path = output / material_rel
        material_fields = [
            ("title", material_title),
            ("created", existing_created(material_path, today)),
            ("updated", today),
            ("type", "entity"),
            ("tags", service_tags(record, detail)),
            ("sources", [RAW_MANIFEST]),
            ("confidence", "medium"),
            ("service_id", record["service_id"]),
            ("page_role", "material-list"),
            ("detail_status", "complete"),
            ("official_url", official_url),
            ("source_json", raw_file),
        ]
        material_page_lines = [
            frontmatter(material_fields),
            f"# {material_title}",
            "",
            f"> {title}的完整申请材料清单，共{len(detail.get('materialList') or [])}项。",
            "",
            *material_lines,
            "## 相关页面",
            "",
            f"- {wikilink(page_rel, title)}",
            f"- {wikilink(related[0][0], related[0][1])}",
            "",
            "## 数据说明",
            "",
            f"- 原始JSON：`{raw_file}`",
            f"- 官方页面：[{title}]({official_url})",
            "- 本页由程序按官网结构化数据生成；实际办理要求以官网最新页面为准。",
            "",
        ]
        material_text = "\n".join(material_page_lines).rstrip() + "\n"
        material_keywords = build_keywords(record, detail)
        add_unique(material_keywords, "申请材料")
        material_rag = {
            "doc_id": f"service:{record['service_id']}:materials",
            "title": material_title,
            "path": material_rel,
            "page_type": "government_service_materials",
            "answerable": True,
            "service_id": record["service_id"],
            "service_objects": objects,
            "matter_type": matter,
            "categories": categories,
            "organization": one_line(detail.get("orgName")),
            "keywords": material_keywords,
            "summary": f"{title}的完整申请材料、提交要求、受理标准和附件。",
            "official_url": official_url,
        }
        extras.append((material_rel, material_text, material_rag))
        lines.extend(
            [
                "## 申请材料",
                "",
                f"本事项共有{len(detail.get('materialList') or [])}项申请材料，详见：",
                "",
                f"- {wikilink(material_rel, material_title)}",
                "",
            ]
        )
    else:
        lines.extend(material_lines)
    lines.extend(render_process(detail))
    lines.extend(render_limits(detail))
    lines.extend(render_results(detail))
    legal_lines = render_text_section("法律依据", detail.get("according"))
    if content_line_count(detail.get("according")) > 40:
        extra = render_section_subpage(
            output,
            page_rel,
            record,
            detail,
            title,
            "法律依据",
            detail.get("according"),
            "legal-basis",
            related,
            official_url,
            raw_file,
            today,
        )
        extras.append(extra)
        lines.extend(
            [
                "## 法律依据",
                "",
                f"法律依据内容较长，详见：{wikilink(extra[0], extra[2]['title'])}",
                "",
            ]
        )
    else:
        lines.extend(legal_lines)
    if meaningful(detail.get("applicantPower")) or meaningful(detail.get("applicantDuty")):
        lines.extend(["## 申请人权利与义务", ""])
        if meaningful(detail.get("applicantPower")):
            lines.extend(["### 权利", "", clean_text(detail.get("applicantPower")), ""])
        if meaningful(detail.get("applicantDuty")):
            lines.extend(["### 义务", "", clean_text(detail.get("applicantDuty")), ""])
    lines.extend(["## 相关页面", ""])
    lines.extend(f"- {wikilink(link, alias)}" for link, alias in related)
    lines.extend(
        [
            "",
            "## 数据说明",
            "",
            f"- 原始JSON：`{raw_file}`",
            f"- 官方页面：[{title}]({official_url})",
            "- 本页由程序按官网结构化数据生成；缺失字段不推测，实际办理要求以官网最新页面为准。",
            "",
        ]
    )
    text = "\n".join(lines).rstrip() + "\n"
    rag = {
        "doc_id": f"service:{record['service_id']}",
        "title": title,
        "path": page_rel,
        "page_type": "government_service",
        "answerable": True,
        "service_id": record["service_id"],
        "service_objects": objects,
        "matter_type": matter,
        "categories": categories,
        "organization": one_line(detail.get("orgName")),
        "keywords": build_keywords(record, detail),
        "summary": service_summary(record, detail),
        "official_url": official_url,
    }
    return text, rag, extras


def render_theme_page(
    output: Path, page_rel: str, item: dict[str, Any], today: str
) -> tuple[str, dict[str, Any]]:
    path = output / page_rel
    theme_id = str(item.get("id") or "")
    title = one_line(item.get("name") or item.get("_title")) or theme_id
    official_url = f"{BASE_URL}/ywtb/gov-search/index.html#/things"
    fields = [
        ("title", title),
        ("created", existing_created(path, today)),
        ("updated", today),
        ("type", "entity"),
        ("tags", ["government-service", "theme-service", "detail-unavailable"]),
        ("sources", [RAW_MANIFEST]),
        ("confidence", "low"),
        ("theme_id", theme_id),
        ("matter_type", one_line(item.get("type_name")) or "高效办成一件事"),
        ("detail_status", "unavailable"),
        ("answerable", False),
        ("official_url", official_url),
    ]
    concept = "concepts/theme-services.md"
    theme_index = "_meta/indexes/theme-services.md"
    lines = [
        frontmatter(fields),
        f"# {title}",
        "",
        "> 这是湖南政务服务智能搜索中的“高效办成一件事”主题服务索引项。",
        "",
        "## 当前可用信息",
        "",
        f"- 主题名称：{title}",
        f"- 主题编号：`{theme_id}`",
        f"- 搜索分类：{one_line(item.get('type_name')) or '高效办成一件事'}",
        f"- 官方搜索入口：[湖南政务服务事项搜索]({official_url})",
        "",
        "## 数据状态",
        "",
        "当前抓取接口未提供与普通事项相同结构的受理条件、申请材料和办理流程。",
        "本页只参与主题名称和关键词检索，不应作为具体办理要求的回答依据。",
        "",
        "## 相关页面",
        "",
        f"- {wikilink(concept, '高效办成一件事')}",
        f"- {wikilink(theme_index, '主题服务索引')}",
        "",
    ]
    summary = f"{title}是“高效办成一件事”主题服务；当前仅有搜索索引信息，无结构化办理指南。"
    rag = {
        "doc_id": f"theme:{theme_id}",
        "title": title,
        "path": page_rel,
        "page_type": "theme_service",
        "answerable": False,
        "theme_id": theme_id,
        "service_objects": [],
        "matter_type": one_line(item.get("type_name")) or "高效办成一件事",
        "categories": ["高效办成一件事"],
        "organization": "",
        "keywords": [title, "高效办成一件事", "一件事", theme_id],
        "summary": summary,
        "official_url": official_url,
    }
    return "\n".join(lines).rstrip() + "\n", rag


def concept_frontmatter(title: str, tags: list[str], today: str) -> str:
    return frontmatter(
        [
            ("title", title),
            ("created", today),
            ("updated", today),
            ("type", "concept"),
            ("tags", tags),
            ("sources", [RAW_MANIFEST]),
            ("confidence", "high"),
        ]
    )


def build_concepts(
    output: Path,
    services: list[dict[str, Any]],
    details: dict[str, dict[str, Any]],
    include_themes: bool,
    today: str,
) -> tuple[dict[str, str], dict[tuple[str, str], str], list[tuple[str, str]]]:
    matter_names = sorted(
        {
            one_line(details[item["service_id"]].get("typeName")) or "其他事项类型"
            for item in services
        }
    )
    matter_paths = {
        name: f"concepts/matter-types/{slug_text(name)}-{short_hash(name)}.md"
        for name in matter_names
    }
    category_info: dict[tuple[str, str], str] = {}
    for item in services:
        for category in item.get("categories") or []:
            if isinstance(category, dict):
                key = (
                    str(category.get("service_type") or ""),
                    str(category.get("code") or ""),
                )
                category_info.setdefault(key, one_line(category.get("name")) or "未命名分类")
    category_paths = {
        key: (
            f"concepts/categories/{slug_text(key[0])}-{slug_text(name)}-"
            f"{short_hash(key[0] + ':' + key[1])}.md"
        )
        for key, name in category_info.items()
    }
    concepts: list[tuple[str, str]] = []

    object_pages = [
        (
            "concepts/service-objects/personal-service.md",
            "个人服务",
            ["government-service", "personal-service"],
            "面向自然人的湖南省政务服务事项集合。",
            "_meta/indexes/personal-only.md",
        ),
        (
            "concepts/service-objects/legal-entity-service.md",
            "法人服务",
            ["government-service", "legal-entity-service"],
            "面向法人或其他组织的湖南省政务服务事项集合。",
            "_meta/indexes/legal-entity-only.md",
        ),
    ]
    for path, title, tags, description, index_path in object_pages:
        text = concept_frontmatter(title, tags, today)
        text += f"# {title}\n\n{description}\n\n## 导航\n\n"
        text += f"- {wikilink(index_path, title + '索引')}\n"
        text += f"- {wikilink('_meta/topic-map.md', '知识库主题地图')}\n"
        atomic_write_text(output / path, text)
        concepts.append((path, title))

    for name, path in matter_paths.items():
        tag = matter_tag(name)
        text = concept_frontmatter(name, ["government-service", tag], today)
        text += f"# {name}\n\n{name}类政务服务事项的导航概念页。\n\n## 相关页面\n\n"
        text += f"- {wikilink('concepts/service-objects/personal-service.md', '个人服务')}\n"
        text += f"- {wikilink('concepts/service-objects/legal-entity-service.md', '法人服务')}\n"
        atomic_write_text(output / path, text)
        concepts.append((path, name))

    for key, name in sorted(category_info.items()):
        path = category_paths[key]
        is_personal = key[0] == "personal"
        object_path = (
            "concepts/service-objects/personal-service.md"
            if is_personal
            else "concepts/service-objects/legal-entity-service.md"
        )
        object_title = "个人服务" if is_personal else "法人服务"
        tag = "personal-service" if is_personal else "legal-entity-service"
        text = concept_frontmatter(name, ["government-service", tag], today)
        text += f"# {name}\n\n{name}是湖南政务服务网站中的{object_title}主题分类。\n\n"
        text += "## 相关页面\n\n"
        text += f"- {wikilink(object_path, object_title)}\n"
        text += f"- {wikilink('_meta/topic-map.md', '知识库主题地图')}\n"
        atomic_write_text(output / path, text)
        concepts.append((path, f"{object_title}／{name}"))

    if include_themes:
        path = "concepts/theme-services.md"
        title = "高效办成一件事"
        text = concept_frontmatter(
            title, ["government-service", "theme-service", "detail-unavailable"], today
        )
        text += f"# {title}\n\n将多个关联事项组合为办事场景的主题服务。\n\n"
        text += "当前数据仅用于主题检索，不用于推断具体材料或条件。\n\n"
        text += "## 导航\n\n"
        text += f"- {wikilink('_meta/indexes/theme-services.md', '主题服务索引')}\n"
        text += f"- {wikilink('_meta/topic-map.md', '知识库主题地图')}\n"
        atomic_write_text(output / path, text)
        concepts.append((path, title))
    return matter_paths, category_paths, concepts


def write_partitioned_index(
    output: Path,
    parent_path: str,
    title: str,
    entries: list[tuple[str, str, str]],
    chunk_size: int = 100,
) -> list[str]:
    entries = sorted(entries, key=lambda item: (item[1], item[0]))
    parent = Path(parent_path)
    part_paths: list[str] = []
    if not entries:
        atomic_write_text(output / parent_path, f"# {title}\n\n当前没有条目。\n")
        return part_paths
    for start in range(0, len(entries), chunk_size):
        part_number = start // chunk_size + 1
        part_path = str(parent.with_name(f"{parent.stem}-part-{part_number:02d}.md")).replace("\\", "/")
        part_paths.append(part_path)
        lines = [f"# {title}（第{part_number}部分）", ""]
        for path, entry_title, summary in entries[start : start + chunk_size]:
            lines.append(f"- {wikilink(path, entry_title)} — {summary}")
        lines.extend(["", f"返回：{wikilink(parent_path, title)}", ""])
        atomic_write_text(output / part_path, "\n".join(lines))
    parent_lines = [f"# {title}", "", f"> 共{len(entries)}项。", ""]
    for index, path in enumerate(part_paths, start=1):
        parent_lines.append(f"- {wikilink(path, f'第{index}部分')}")
    parent_lines.extend(["", f"- {wikilink('_meta/topic-map.md', '知识库主题地图')}", ""])
    atomic_write_text(output / parent_path, "\n".join(parent_lines))
    return part_paths


def select_sample(items: list[dict[str, Any]], count: int) -> list[dict[str, Any]]:
    if not count or count >= len(items):
        return items
    groups: dict[tuple[str, ...], list[dict[str, Any]]] = defaultdict(list)
    for item in items:
        groups[tuple(item.get("service_types") or [])].append(item)
    selected: list[dict[str, Any]] = []
    used: set[str] = set()
    for group in groups.values():
        if group and len(selected) < count:
            selected.append(group[0])
            used.add(group[0]["service_id"])
    for item in items:
        if len(selected) >= count:
            break
        if item["service_id"] not in used:
            selected.append(item)
            used.add(item["service_id"])
    return selected


def load_previous_paths(output: Path) -> tuple[dict[str, str], dict[str, str], set[str]]:
    path = output / "_meta" / "generated_manifest.json"
    if not path.exists():
        return {}, {}, set()
    try:
        payload = read_json(path)
        return (
            dict(payload.get("service_paths") or {}),
            dict(payload.get("theme_paths") or {}),
            set(payload.get("generated_pages") or []),
        )
    except (OSError, ValueError, TypeError):
        return {}, {}, set()


def markdown_files(output: Path) -> list[Path]:
    return sorted(path for path in output.rglob("*.md") if path.is_file())


def lint_output(
    output: Path,
    entity_paths: set[str],
    indexed_paths: set[str],
    stale_paths: set[str],
) -> dict[str, Any]:
    files = markdown_files(output)
    existing = {str(path.relative_to(output).with_suffix("")).replace("\\", "/") for path in files}
    broken: list[dict[str, str]] = []
    frontmatter_errors: list[str] = []
    oversized: list[dict[str, Any]] = []
    link_pattern = re.compile(r"\[\[([^\]|#]+)(?:#[^\]|]+)?(?:\|[^\]]+)?\]\]")
    for path in files:
        rel = str(path.relative_to(output)).replace("\\", "/")
        text = path.read_text(encoding="utf-8")
        if rel.startswith(("entities/", "concepts/")):
            required = ("title:", "created:", "updated:", "type:", "tags:", "sources:")
            if not text.startswith("---\n") or any(field not in text[:4000] for field in required):
                frontmatter_errors.append(rel)
            line_count = text.count("\n") + 1
            if line_count > 200:
                oversized.append({"path": rel, "line_count": line_count})
        for target in link_pattern.findall(text):
            normalized = target.strip().lstrip("/")
            if normalized.endswith(".md"):
                normalized = normalized[:-3]
            if normalized not in existing:
                broken.append({"source": rel, "target": target})
    missing_index = sorted(entity_paths - indexed_paths)
    return {
        "markdown_file_count": len(files),
        "broken_wikilink_count": len(broken),
        "broken_wikilinks": broken[:200],
        "frontmatter_error_count": len(frontmatter_errors),
        "frontmatter_errors": frontmatter_errors,
        "pages_over_200_lines_count": len(oversized),
        "pages_over_200_lines": oversized,
        "entity_pages_missing_from_subindexes_count": len(missing_index),
        "entity_pages_missing_from_subindexes": missing_index,
        "stale_generated_page_count": len(stale_paths),
        "stale_generated_pages": sorted(stale_paths),
    }


def schema_text(today: str) -> str:
    tags = "\n".join(f"- `{tag}`" for tag in TAG_TAXONOMY)
    return f"""# Wiki Schema

## Domain

湖南省政务服务事项知识库，覆盖个人服务、法人服务和“高效办成一件事”主题索引。

## Conventions

- 普通事项一项一页，位于 `entities/services/`。
- “一件事”主题位于 `entities/theme-services/`，详情缺失时必须标记 `answerable: false`。
- 所有实体页和概念页使用YAML frontmatter及双向Wiki链接。
- 由于页面规模超过200页，根 `index.md` 只列分区索引；每个实体页必须进入一个 `_meta/indexes/` 子索引。
- `raw/` 是不可手工修改的来源层；重新生成时由转换程序维护。
- 不推测官网JSON中不存在的材料、条件、时限单位、地址或联系方式。
- 普通事项正文可作为回答依据；`detail_status: unavailable` 的主题页只用于检索导航。
- 原始结构化JSON位于本知识库上级数据目录，各事项通过 `source_json` 字段追溯。

## Frontmatter

必填字段：`title`、`created`、`updated`、`type`、`tags`、`sources`。

普通事项还必须包含：`service_id`、`service_objects`、`matter_type`、`detail_status`、`official_url`、`source_json`。

## Tag Taxonomy

{tags}

## Page Thresholds

- 每个正式普通事项均为中央实体，允许独立建页。
- 每个“一件事”搜索主题均建导航页，但不能充当具体办理指南。
- 超过200行的页面进入转换报告，后续按材料或法律依据拆页。

## Retrieval Policy

- `_meta/rag_catalog.jsonl` 是后续RAG建索引的机器可读清单。
- 优先检索 `answerable: true` 的普通事项；主题页命中后应提示用户进入官方页面核实。
- 回答具体材料、条件、时限时，必须引用普通事项页面，不得从主题标题推断。

## Update Policy

- 重新爬取后再次运行转换器，按 `service_id` 更新同一页面。
- 页面标题变化不会改变已记录的文件路径。
- 旧版生成页不会自动删除，而是在转换报告中列为 stale，避免误删人工内容。

最后更新：{today}
"""


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="将湖南政务服务JSON转换为Hermes LLM-WIKI Markdown"
    )
    parser.add_argument(
        "--input",
        type=Path,
        default=Path.cwd() / "gov-wiki",
        help="爬虫数据目录，默认 .\\gov-wiki",
    )
    parser.add_argument(
        "--output",
        type=Path,
        default=None,
        help="WIKI输出目录；全量默认 INPUT\\wiki，样本默认 INPUT\\wiki-sample",
    )
    parser.add_argument(
        "--sample", type=int, default=0, help="只转换指定数量的普通事项，0表示全量"
    )
    parser.add_argument(
        "--no-themes", action="store_true", help="不生成58个“一件事”主题索引页"
    )
    args = parser.parse_args()
    if args.sample < 0:
        parser.error("--sample 不能为负数")
    args.input = args.input.resolve()
    if args.output is None:
        args.output = args.input / ("wiki-sample" if args.sample else "wiki")
    args.output = args.output.resolve()
    return args


def main() -> int:
    args = parse_args()
    input_root: Path = args.input
    output: Path = args.output
    today = date.today().isoformat()
    include_themes = not args.no_themes

    enriched_path = input_root / "catalog" / "enriched_catalog.json"
    candidates_path = input_root / "audit" / "search_only_candidates.json"
    try:
        enriched_payload = read_json(enriched_path)
        all_records = enriched_payload.get("items")
        if not isinstance(all_records, list):
            raise ValueError("enriched_catalog.json 的 items 不是数组")
        candidates_payload = read_json(candidates_path)
        candidates = candidates_payload.get("items")
        if not isinstance(candidates, list):
            raise ValueError("search_only_candidates.json 的 items 不是数组")
    except (OSError, ValueError, TypeError) as exc:
        print(f"[错误] 无法加载输入：{exc}", file=sys.stderr)
        return 2

    records_by_id: dict[str, dict[str, Any]] = {}
    details: dict[str, dict[str, Any]] = {}
    skipped_details: list[dict[str, str]] = []
    resolved_raw_paths: dict[str, str] = {}
    raw_lookup: dict[str, Path] = {}
    for path in (input_root / "raw" / "details").glob("*/*.json"):
        if path.stem in raw_lookup:
            print(f"[错误] 详情文件名重复：{path.stem}", file=sys.stderr)
            return 2
        raw_lookup[path.stem] = path
    for record in all_records:
        if not isinstance(record, dict) or not record.get("service_id"):
            continue
        service_id = str(record["service_id"])
        detail_meta = record.get("detail") or {}
        if detail_meta.get("status") != "ok":
            skipped_details.append({"service_id": service_id, "reason": "detail_status_not_ok"})
            continue
        raw_file = input_root / str(detail_meta.get("raw_file") or raw_detail_path(service_id))
        if not raw_file.exists():
            raw_file = raw_lookup.get(service_id, raw_file)
        try:
            payload = read_json(raw_file)
            detail = payload.get("data") if isinstance(payload, dict) else None
            if not isinstance(detail, dict) or not detail:
                raise ValueError("data不是非空对象")
        except (OSError, ValueError, TypeError) as exc:
            skipped_details.append({"service_id": service_id, "reason": str(exc)})
            continue
        resolved = str(raw_file.relative_to(input_root)).replace("\\", "/")
        normalized_record = dict(record)
        normalized_detail_meta = dict(detail_meta)
        normalized_detail_meta["raw_file"] = resolved
        normalized_record["detail"] = normalized_detail_meta
        records_by_id[service_id] = normalized_record
        details[service_id] = detail
        resolved_raw_paths[service_id] = resolved

    services = select_sample(
        sorted(records_by_id.values(), key=lambda item: str(item["service_id"])),
        args.sample,
    )
    themes = sorted(
        [
            item
            for item in candidates
            if isinstance(item, dict)
            and str(item.get("type") or "").lower() == "theme"
            and item.get("id")
        ],
        key=lambda item: str(item["id"]),
    )
    if not include_themes:
        themes = []

    output.mkdir(parents=True, exist_ok=True)
    previous_services, previous_themes, previous_pages = load_previous_paths(output)
    matter_paths, category_paths, concepts = build_concepts(
        output, services, details, include_themes, today
    )

    service_paths: dict[str, str] = {}
    theme_paths: dict[str, str] = {}
    rag_records: list[dict[str, Any]] = []
    bucket_entries: dict[str, list[tuple[str, str, str]]] = defaultdict(list)
    indexed_paths: set[str] = set()
    supplemental_paths: set[str] = set()
    supplemental_counts: dict[str, int] = defaultdict(int)

    print(f"输入目录：{input_root}")
    print(f"输出目录：{output}")
    print(f"普通事项：{len(services)}")
    print(f"一件事主题：{len(themes)}")

    for index, record in enumerate(services, start=1):
        service_id = str(record["service_id"])
        title = one_line(record.get("title")) or service_id
        rel = previous_services.get(service_id) or page_path("services", title, service_id)
        service_paths[service_id] = rel
        text, rag, extras = render_service_page(
            output, rel, record, details[service_id], matter_paths, category_paths, today
        )
        atomic_write_text(output / rel, text)
        rag_records.append(rag)
        types = set(record.get("service_types") or [])
        if types == {"personal"}:
            bucket = "personal-only"
        elif types == {"legal_entity"}:
            bucket = "legal-entity-only"
        else:
            bucket = "personal-and-legal"
        bucket_entries[bucket].append((rel, rag["title"], rag["summary"]))
        indexed_paths.add(rel)
        for extra_rel, extra_text, extra_rag in extras:
            atomic_write_text(output / extra_rel, extra_text)
            rag_records.append(extra_rag)
            bucket_entries[bucket].append(
                (extra_rel, extra_rag["title"], extra_rag["summary"])
            )
            indexed_paths.add(extra_rel)
            supplemental_paths.add(extra_rel)
            supplemental_counts[str(extra_rag.get("page_type") or "other")] += 1
        if index == 1 or index % 500 == 0 or index == len(services):
            print(f"  普通事项进度：{index}/{len(services)}")

    for item in themes:
        theme_id = str(item["id"])
        title = one_line(item.get("name") or item.get("_title")) or theme_id
        rel = previous_themes.get(theme_id) or page_path("theme-services", title, theme_id)
        theme_paths[theme_id] = rel
        text, rag = render_theme_page(output, rel, item, today)
        atomic_write_text(output / rel, text)
        rag_records.append(rag)
        bucket_entries["theme-services"].append((rel, rag["title"], rag["summary"]))
        indexed_paths.add(rel)

    index_titles = {
        "personal-only": "仅个人服务事项索引",
        "legal-entity-only": "仅法人服务事项索引",
        "personal-and-legal": "个人和法人通用事项索引",
        "theme-services": "高效办成一件事主题索引",
    }
    parent_paths: dict[str, str] = {}
    index_parts: list[str] = []
    for bucket, title in index_titles.items():
        if bucket == "theme-services" and not include_themes:
            continue
        parent = f"_meta/indexes/{bucket}.md"
        parent_paths[bucket] = parent
        index_parts.extend(
            write_partitioned_index(output, parent, title, bucket_entries.get(bucket, []))
        )

    concept_lines = ["# Wiki Index", "", "> 湖南省政务服务知识库导航。", f"> Last updated: {today}", ""]
    concept_lines.extend(["## 政务事项", ""])
    for bucket, parent in parent_paths.items():
        concept_lines.append(
            f"- {wikilink(parent, index_titles[bucket])} — {len(bucket_entries.get(bucket, []))}页"
        )
    concept_lines.extend(["", "## 概念与分类", ""])
    for path, title in sorted(concepts, key=lambda item: item[1]):
        concept_lines.append(f"- {wikilink(path, title)}")
    concept_lines.extend(["", "## 元数据", "", f"- {wikilink('_meta/topic-map.md', '知识库主题地图')}", ""])
    atomic_write_text(output / "index.md", "\n".join(concept_lines))

    topic_lines = ["# 知识库主题地图", "", "## 按服务对象", ""]
    for bucket in ("personal-only", "legal-entity-only", "personal-and-legal"):
        topic_lines.append(f"- {wikilink(parent_paths[bucket], index_titles[bucket])}")
    if include_themes:
        topic_lines.extend(["", "## 主题服务", "", f"- {wikilink(parent_paths['theme-services'], index_titles['theme-services'])}"])
    topic_lines.extend(["", "## 事项类型与网站主题分类", ""])
    for path, title in sorted(concepts, key=lambda item: item[1]):
        topic_lines.append(f"- {wikilink(path, title)}")
    topic_lines.append("")
    atomic_write_text(output / "_meta" / "topic-map.md", "\n".join(topic_lines))

    atomic_write_text(output / "SCHEMA.md", schema_text(today))
    raw_body = f"""# 湖南省政务服务数据集

本知识库由湖南政务服务网公开结构化数据程序化生成。

- 个人服务：{BASE_URL}/hnywtb/service/index.html?type=gr
- 法人服务：{BASE_URL}/hnywtb/service/index.html?type=fr
- 智能搜索：{BASE_URL}/ywtb/gov-search/index.html#/things
- 输入目录：`{input_root}`
- 转换器版本：`{VERSION}`

普通事项的具体来源JSON记录在各页面 `source_json` frontmatter中。
"""
    raw_hash = hashlib.sha256(raw_body.encode("utf-8")).hexdigest()
    raw_header = frontmatter(
        [
            ("source_url", f"{BASE_URL}/hnywtb/service/index.html?type=gr"),
            ("ingested", today),
            ("sha256", raw_hash),
        ]
    )
    atomic_write_text(output / RAW_MANIFEST, raw_header + raw_body)

    log_path = output / "log.md"
    if log_path.exists():
        log_text = log_path.read_text(encoding="utf-8").rstrip() + "\n\n"
    else:
        log_text = "# Wiki Log\n\n> Chronological record of wiki actions.\n\n"
    log_text += f"## [{today}] ingest | 湖南政务服务JSON程序化转换\n"
    log_text += f"- 普通事项页：{len(service_paths)}\n"
    log_text += f"- 一件事主题页：{len(theme_paths)}\n"
    log_text += f"- 转换器版本：{VERSION}\n"
    atomic_write_text(log_path, log_text)

    rag_text = "".join(json.dumps(item, ensure_ascii=False) + "\n" for item in rag_records)
    atomic_write_text(output / "_meta" / "rag_catalog.jsonl", rag_text)

    generated_pages = (
        set(service_paths.values()) | set(theme_paths.values()) | supplemental_paths
    )
    stale_paths = {path for path in previous_pages - generated_pages if (output / path).exists()}
    generated_manifest = {
        "schema": "hunan-gov-wiki-generated-manifest/1.0",
        "converter_version": VERSION,
        "generated_at": utc_now(),
        "input_root": str(input_root),
        "service_paths": service_paths,
        "theme_paths": theme_paths,
        "supplemental_pages": sorted(supplemental_paths),
        "generated_pages": sorted(generated_pages),
        "concept_pages": sorted(path for path, _ in concepts),
        "index_pages": sorted(set(parent_paths.values()) | set(index_parts)),
    }
    atomic_write_json(output / "_meta" / "generated_manifest.json", generated_manifest)

    lint = lint_output(output, generated_pages, indexed_paths, stale_paths)
    ordinary_candidates = [
        item for item in candidates if isinstance(item, dict) and item.get("type") == "approve"
    ]
    ordinary_ids = {str(item.get("id")) for item in ordinary_candidates}
    catalog_ids = set(records_by_id)
    report = {
        "schema": "hunan-gov-wiki-conversion-report/1.0",
        "converter_version": VERSION,
        "generated_at": utc_now(),
        "mode": "sample" if args.sample else "full",
        "input_enriched_items": len(all_records),
        "valid_detail_items": len(records_by_id),
        "converted_service_pages": len(service_paths),
        "supplemental_pages": len(supplemental_paths),
        "supplemental_page_counts": dict(sorted(supplemental_counts.items())),
        "converted_theme_pages": len(theme_paths),
        "ordinary_search_candidates_not_converted": len(ordinary_ids - catalog_ids),
        "skipped_detail_count": len(skipped_details),
        "skipped_details": skipped_details,
        "rag_record_count": len(rag_records),
        "lint": lint,
    }
    atomic_write_json(output / "_meta" / "conversion_report.json", report)

    print("\n转换完成")
    print(f"普通事项页：{len(service_paths)}")
    print(f"独立长内容页：{len(supplemental_paths)}")
    print(f"一件事主题页：{len(theme_paths)}")
    print(f"RAG清单记录：{len(rag_records)}")
    print(f"断链：{lint['broken_wikilink_count']}")
    print(f"Frontmatter错误：{lint['frontmatter_error_count']}")
    print(f"未进入子索引的实体页：{lint['entity_pages_missing_from_subindexes_count']}")
    print(f"报告：{output / '_meta' / 'conversion_report.json'}")
    return 0 if not (
        lint["broken_wikilink_count"]
        or lint["frontmatter_error_count"]
        or lint["entity_pages_missing_from_subindexes_count"]
    ) else 2


if __name__ == "__main__":
    raise SystemExit(main())
