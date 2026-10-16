from __future__ import annotations

import csv
import hashlib
import json
import math
import os
import re
import shutil
import sys
import time
import urllib.request
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any, Callable, Iterable, Iterator, Mapping

VENDOR_ROOT = Path(__file__).resolve().parents[2] / "vendor"
if str(VENDOR_ROOT) not in sys.path:
    sys.path.insert(0, str(VENDOR_ROOT))
import jieba  # type: ignore  # vendored, pinned in requirements.txt
import jieba.posseg as pseg  # type: ignore

from .structure_reuse import (
    SCHEMA_VERSION as STRUCTURE_REUSE_SCHEMA_VERSION,
    load_manifest as load_structure_reuse_manifest,
    resource_snapshot as structure_resource_snapshot,
    source_sha256,
)

from .core import (
    Feedback,
    StageResult,
    WorkflowContext,
    now_iso,
    read_csv,
    read_json,
    read_jsonl,
    run_python,
    sha256_file,
    stable_id,
    write_csv,
    write_json,
    write_jsonl,
)


def _count_files(root: Path) -> tuple[int, int]:
    files = [path for path in root.rglob("*") if path.is_file()] if root.exists() else []
    return len(files), sum(path.stat().st_size for path in files)


def _line_count(path: Path) -> int:
    if not path.exists():
        return 0
    with path.open("r", encoding="utf-8-sig") as handle:
        return sum(1 for line in handle if line.strip())


def _emit_progress(percent: int, detail: str) -> None:
    """Emit a machine-readable child-process progress event for the workbench."""
    print(f"CORPUS_PROGRESS\t{max(0, min(100, int(percent)))}\t{detail}", flush=True)


def _semantic_topic_levels(path: str) -> list[str]:
    """Remove workflow-only BERTopic wrappers from structure metadata."""
    levels = [item.strip() for item in str(path).split(">") if item.strip()]
    if (
        len(levels) >= 3
        and levels[0] in {"BERTopic测试体系", "BERTopic 聚类测试体系"}
        and levels[1] == "聚类结果"
    ):
        levels = levels[2:]
    return [
        re.sub(r"^BT_TOPIC_\d+\s*[｜|]\s*", "", item).strip()
        for item in levels[:3]
        if item.strip()
    ]


def _documents_path(ctx: WorkflowContext) -> Path:
    return ctx.configured_path("corpus_output") / "jsonl" / "documents.jsonl"


def _sections_path(ctx: WorkflowContext) -> Path:
    return ctx.configured_path("corpus_output") / "jsonl" / "sections.jsonl"


def _paragraphs_path(ctx: WorkflowContext) -> Path:
    return ctx.configured_path("corpus_output") / "jsonl" / "paragraphs.jsonl"


def _sentences_path(ctx: WorkflowContext) -> Path:
    return ctx.configured_path("corpus_output") / "jsonl" / "sentences.jsonl"


def planning_stage(ctx: WorkflowContext, **_: Any) -> StageResult:
    manual = ctx.project_root / "docs" / "新能源中文教学专门语料库构建流程手册（试行版）.md"
    workflow = ctx.config.get("workflow", {})
    metrics = {
        "project_name": ctx.config.get("project", {}).get("name", ""),
        "domain": ctx.config.get("project", {}).get("domain", ""),
        "corpus_version": ctx.version,
        "manual_version": workflow.get("manual_version", ""),
        "release_level": workflow.get("release_level", "internal_draft"),
        "stage_count": 10,
    }
    feedback = []
    if "draft" in ctx.version.lower():
        feedback.append(
            Feedback(
                "info",
                "version",
                "当前版本为草稿版本，只能按配置的内部范围使用。",
                suggested_action="完成各阶段人工签署后再申请正式版本号。",
                assignee_role="项目负责人",
            )
        )
    return StageResult(
        "01_planning",
        "立项与需求",
        "needs_human_review" if feedback else "complete",
        "已固化当前项目、版本、手册和发布边界。",
        metrics=metrics,
        inputs=[ctx.config_path, manual],
        validations=[
            {"name": "流程手册存在", "passed": manual.exists(), "details": str(manual)},
            {
                "name": "版本配置完整",
                "passed": bool(ctx.version and workflow.get("manual_version")),
                "details": f"corpus={ctx.version}; manual={workflow.get('manual_version', '')}",
            },
        ],
        feedback=feedback,
        decisions=[
            "当前语料定位为流程验证用不均衡样本，扩充由覆盖缺口驱动。",
            "当前不处理中文语言难度和教学适用性标签。",
            "每阶段强制输出报告、指标、反馈队列和责任签署。",
        ],
    )


def design_stage(ctx: WorkflowContext, **_: Any) -> StageResult:
    target_path = ctx.resolve(ctx.config["governance"]["coverage_targets"])
    output_root = ctx.configured_path("governance_output")
    output_root.mkdir(parents=True, exist_ok=True)
    documents_path = _documents_path(ctx)
    documents = list(read_jsonl(documents_path)) if documents_path.exists() else []
    domain_counts = Counter(str(row.get("domain", "未标注")) for row in documents)
    source_counts = Counter(str(row.get("source_type", "未标注")) for row in documents)
    targets = read_csv(target_path)
    rows = []
    feedback = []
    for target in targets:
        dimension = target.get("dimension", "")
        value = target.get("value", "")
        actual = domain_counts.get(value, 0) if dimension == "domain" else source_counts.get(value, 0)
        try:
            minimum = int(target.get("target_min_documents") or 0)
        except ValueError:
            minimum = 0
        status = "gap" if actual < minimum else "baseline"
        row = dict(target)
        row.update({"actual_documents": actual, "gap_documents": max(minimum - actual, 0), "status": status})
        rows.append(row)
        if status == "gap":
            feedback.append(
                Feedback(
                    "warning",
                    "coverage_gap",
                    f"{dimension}={value} 当前 {actual} 篇，低于目标 {minimum} 篇。",
                    object_id=f"{dimension}:{value}",
                    suggested_action="加入下一轮采集任务。",
                    assignee_role="语料架构负责人",
                )
            )
    if targets and all(not (row.get("target_min_documents") or "").strip("0") for row in targets):
        feedback.append(
            Feedback(
                "warning",
                "coverage_targets",
                "覆盖矩阵尚未设置非零目标配额。",
                suggested_action="由项目负责人和领域专家填写目标文档量与来源上限。",
                assignee_role="语料架构负责人",
            )
        )
    coverage_path = output_root / "coverage_matrix_actual.csv"
    fields = list(rows[0].keys()) if rows else ["dimension", "value", "actual_documents", "status"]
    write_csv(coverage_path, rows, fields)
    return StageResult(
        "02_design",
        "语料设计与抽样",
        "needs_human_review" if feedback else "complete",
        "已按当前文档分布生成覆盖基线和缺口接口。",
        metrics={
            "documents_observed": len(documents),
            "domain_categories": len(domain_counts),
            "source_type_categories": len(source_counts),
            "coverage_rows": len(rows),
            "coverage_gaps": sum(1 for row in rows if row["status"] == "gap"),
        },
        inputs=[target_path, documents_path],
        outputs=[coverage_path],
        validations=[
            {"name": "覆盖模板可读", "passed": target_path.exists(), "details": str(target_path)},
            {"name": "覆盖统计已输出", "passed": coverage_path.exists(), "details": str(coverage_path)},
        ],
        feedback=feedback,
        decisions=["以当前实际分布作为 baseline，不把现有样本比例当作目标比例。"],
    )


def rights_stage(ctx: WorkflowContext, **_: Any) -> StageResult:
    configured = ctx.resolve(ctx.config["governance"]["source_rights"])
    output_root = ctx.configured_path("governance_output")
    output_root.mkdir(parents=True, exist_ok=True)
    configured_rows = read_csv(configured)
    by_id = {row.get("source_id", ""): row for row in configured_rows if row.get("source_id")}
    documents_path = _documents_path(ctx)
    observed = Counter()
    if documents_path.exists():
        for row in read_jsonl(documents_path):
            observed[str(row.get("source", "unknown"))] += 1
    for source_id in observed:
        by_id.setdefault(
            source_id,
            {
                "source_id": source_id,
                "source_name": source_id,
                "rights_holder": "",
                "license_type": "",
                "permitted_uses": "",
                "release_level": "internal_only",
                "personal_info_risk": "unknown",
                "status": "needs_review",
                "reviewer": "",
                "reviewed_at": "",
                "notes": "自动发现来源，待人工补全",
            },
        )
    rows = []
    feedback = []
    for source_id, row in sorted(by_id.items()):
        item = dict(row)
        item["observed_documents"] = observed.get(source_id, 0)
        rows.append(item)
        if item.get("status") != "approved":
            feedback.append(
                Feedback(
                    "warning",
                    "rights_review",
                    f"来源 {source_id} 的权利状态尚未批准。",
                    object_id=source_id,
                    suggested_action="补充权利人、许可类型、允许用途并由合规管理员审核。",
                    assignee_role="来源与合规管理员",
                )
            )
    output_path = output_root / "source_rights.csv"
    fields = list(rows[0].keys()) if rows else []
    write_csv(output_path, rows, fields)
    approved = sum(1 for row in rows if row.get("status") == "approved")
    return StageResult(
        "03_rights",
        "权利与数据安全",
        "needs_human_review" if approved < len(rows) else "complete",
        "已生成当前来源的权利与发布级别审核接口，未自动推定许可。",
        metrics={
            "sources_total": len(rows),
            "sources_approved": approved,
            "sources_needing_review": len(rows) - approved,
            "documents_covered": sum(observed.values()),
        },
        inputs=[configured, documents_path],
        outputs=[output_path],
        validations=[
            {"name": "来源均有状态", "passed": all(row.get("status") for row in rows), "details": f"rows={len(rows)}"},
            {"name": "公开发布许可", "passed": approved == len(rows), "details": f"approved={approved}/{len(rows)}"},
        ],
        feedback=feedback,
        decisions=["未确认许可的材料仅按 internal_only 管理，不自动批准公开发布。"],
    )


def collection_stage(ctx: WorkflowContext, run_collection: bool = False, **_: Any) -> StageResult:
    manual_root = ctx.configured_path("manual_sources")
    web_root = ctx.configured_path("web_sources")
    configured_run = bool(ctx.config.get("workflow", {}).get("run_collection_in_complete", False))
    should_run = run_collection or configured_run
    if should_run:
        run_python(
            ctx.project_root / "scripts" / "collect" / "web.py",
            ["--config", str(ctx.resolve(ctx.config["collection"]["config"]))],
            ctx.project_root,
        )
    inventory_rows = []
    for source_kind, root in (("manual", manual_root), ("web", web_root)):
        if not root.exists():
            continue
        for path in root.rglob("*"):
            if not path.is_file():
                continue
            inventory_rows.append(
                {
                    "source_kind": source_kind,
                    "relative_path": str(path.relative_to(root)),
                    "suffix": path.suffix.lower(),
                    "bytes": path.stat().st_size,
                    "modified_at": path.stat().st_mtime,
                }
            )
    output_root = ctx.configured_path("governance_output")
    output_path = output_root / "collection_inventory.csv"
    write_csv(
        output_path,
        inventory_rows,
        ["source_kind", "relative_path", "suffix", "bytes", "modified_at"],
    )
    manual_count = sum(1 for row in inventory_rows if row["source_kind"] == "manual")
    web_count = len(inventory_rows) - manual_count
    return StageResult(
        "04_collection",
        "采集与原始归档",
        "complete",
        "已盘点原始采集资产；是否实时抓取由显式配置控制。",
        metrics={
            "live_collection_executed": should_run,
            "raw_files_total": len(inventory_rows),
            "manual_files": manual_count,
            "web_files": web_count,
            "raw_bytes": sum(int(row["bytes"]) for row in inventory_rows),
        },
        inputs=[manual_root, web_root, ctx.resolve(ctx.config["collection"]["config"])],
        outputs=[output_path],
        validations=[
            {"name": "原始目录存在", "passed": manual_root.exists() and web_root.exists(), "details": f"manual={manual_root.exists()}; web={web_root.exists()}"},
            {"name": "资产已盘点", "passed": bool(inventory_rows), "details": f"files={len(inventory_rows)}"},
        ],
        decisions=[
            "完整工作流默认不主动访问外部网站；当前样本扩充须先由覆盖缺口批准。"
            if not should_run
            else "本次已按显式参数执行网站采集。"
        ],
    )


def _simhash(text: str) -> int:
    compact = re.sub(r"\s+", "", text)[:200000]
    vector = [0] * 64
    if len(compact) < 5:
        compact = compact or "_"
    for index in range(0, max(len(compact) - 4, 1), 3):
        shingle = compact[index : index + 5]
        digest = int.from_bytes(hashlib.blake2b(shingle.encode("utf-8"), digest_size=8).digest(), "big")
        for bit in range(64):
            vector[bit] += 1 if digest & (1 << bit) else -1
    value = 0
    for bit, score in enumerate(vector):
        if score >= 0:
            value |= 1 << bit
    return value


def _cleaning_audits(ctx: WorkflowContext) -> tuple[dict[str, int], list[Path], list[Feedback]]:
    output_root = ctx.configured_path("cleaning_audit_output")
    output_root.mkdir(parents=True, exist_ok=True)
    documents = list(read_jsonl(_documents_path(ctx)))
    fingerprints = {row["doc_id"]: _simhash(str(row.get("text", ""))) for row in documents}
    near_rows = []
    for index, left in enumerate(documents):
        for right in documents[index + 1 :]:
            distance = (fingerprints[left["doc_id"]] ^ fingerprints[right["doc_id"]]).bit_count()
            similarity = 1.0 - distance / 64.0
            if similarity >= 0.90:
                near_rows.append(
                    {
                        "left_doc_id": left["doc_id"],
                        "right_doc_id": right["doc_id"],
                        "similarity": round(similarity, 4),
                        "left_title": left.get("title", ""),
                        "right_title": right.get("title", ""),
                        "decision": "pending",
                        "reviewer": "",
                        "notes": "",
                    }
                )
    near_path = output_root / "near_duplicate_review_queue.csv"
    write_csv(near_path, near_rows, ["left_doc_id", "right_doc_id", "similarity", "left_title", "right_title", "decision", "reviewer", "notes"])

    object_patterns = {
        "standard_number": re.compile(r"\b(?:GB|GB/T|ISO|IEC|QC/T|NB/T)\s*[A-Za-z0-9.\-/]+", re.I),
        "number_unit": re.compile(r"\d+(?:\.\d+)?\s*(?:kW|MW|GW|kWh|MWh|V|A|Ah|Wh|%|％|千瓦|兆瓦|吉瓦|伏|安时|摄氏度)", re.I),
        "chemical_or_formula": re.compile(r"(?:LiFePO4|LiPF6|CO2|H2|O2|Ni|Co|Mn|Fe|C)\b", re.I),
    }
    complex_rows = []
    for row in read_jsonl(_paragraphs_path(ctx)):
        text = str(row.get("text", ""))
        for kind, pattern in object_patterns.items():
            for match in pattern.finditer(text):
                complex_rows.append(
                    {
                        "doc_id": row.get("doc_id", ""),
                        "paragraph_id": row.get("paragraph_id", ""),
                        "object_type": kind,
                        "raw_object": match.group(0),
                        "normalized_object": match.group(0),
                        "needs_review": "false",
                    }
                )
    complex_path = output_root / "complex_objects.csv"
    write_csv(complex_path, complex_rows, ["doc_id", "paragraph_id", "object_type", "raw_object", "normalized_object", "needs_review"])

    validity_rows = []
    for row in documents:
        blob = f"{row.get('title', '')}\n{str(row.get('text', ''))[:3000]}"
        if any(key in str(row.get("source_type", "")) for key in ("政策", "报告")):
            statuses = [word for word in ("废止", "失效", "修订", "现行") if word in blob]
            validity_rows.append(
                {
                    "doc_id": row["doc_id"],
                    "title": row.get("title", ""),
                    "source_date": row.get("source_date", ""),
                    "status_evidence": "|".join(statuses),
                    "validity_status": "needs_review" if not statuses else "evidence_found",
                    "reviewer": "",
                    "notes": "",
                }
            )
    validity_path = output_root / "validity_review_queue.csv"
    write_csv(validity_path, validity_rows, ["doc_id", "title", "source_date", "status_evidence", "validity_status", "reviewer", "notes"])

    title_groups: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in documents:
        normalized_title = re.sub(r"\W+", "", str(row.get("title", ""))).lower()
        if normalized_title:
            title_groups[normalized_title].append(row)
    conflict_rows = []
    for group in title_groups.values():
        hashes = {row.get("text_hash") for row in group}
        if len(group) > 1 and len(hashes) > 1:
            conflict_rows.append(
                {
                    "title": group[0].get("title", ""),
                    "doc_ids": "|".join(row["doc_id"] for row in group),
                    "source_count": len({row.get("source") for row in group}),
                    "decision": "pending",
                    "reviewer": "",
                    "notes": "",
                }
            )
    conflict_path = output_root / "content_conflict_review_queue.csv"
    write_csv(conflict_path, conflict_rows, ["title", "doc_ids", "source_count", "decision", "reviewer", "notes"])

    feedback = []
    if near_rows:
        feedback.append(Feedback("warning", "near_duplicate", f"发现 {len(near_rows)} 对近重复候选。", suggested_action="逐对确认主版本与版本关系。", assignee_role="清洗与结构化工程师"))
    pending_validity = sum(1 for row in validity_rows if row["validity_status"] == "needs_review")
    if pending_validity:
        feedback.append(Feedback("warning", "validity", f"有 {pending_validity} 篇政策/报告需要核验时效性。", suggested_action="填写时效性审核队列。", assignee_role="来源与合规管理员"))
    metrics = {
        "near_duplicate_candidates": len(near_rows),
        "complex_objects": len(complex_rows),
        "validity_rows": len(validity_rows),
        "validity_pending": pending_validity,
        "content_conflicts": len(conflict_rows),
    }
    return metrics, [near_path, complex_path, validity_path, conflict_path], feedback


def cleaning_stage(ctx: WorkflowContext, rebuild: bool = True, **_: Any) -> StageResult:
    inventory_root = ctx.configured_path("inventory")
    corpus_root = ctx.configured_path("corpus_output")
    manual_root = ctx.configured_path("manual_sources")
    web_root = ctx.configured_path("web_sources")
    inventory_file = ctx.resolve(ctx.config["cleaning"]["inventory_file"])
    if rebuild:
        run_python(
            ctx.project_root / "scripts" / "prepare" / "inventory.py",
            ["--manual-root", str(manual_root), "--web-root", str(web_root), "--output-dir", str(inventory_root), "--exclude-manifest", str(ctx.resolve(ctx.config["cleaning"].get("exclusion_manifest", "data/raw/corpus_exclusions.csv")))],
            ctx.project_root,
        )
        run_python(
            ctx.project_root / "scripts" / "prepare" / "clean.py",
            [
                "--inventory", str(inventory_file),
                "--output-root", str(corpus_root),
                "--manual-root", str(manual_root),
                "--web-root", str(web_root),
                "--config", str(ctx.resolve(ctx.config["cleaning"]["config"])),
            ],
            ctx.project_root,
        )
    report_path = corpus_root / "quality" / "cleaning_report.json"
    report = read_json(report_path)
    audit_metrics, audit_outputs, feedback = _cleaning_audits(ctx)
    metrics = {
        "inventory_rows": report.get("selected_inventory_rows", report.get("usable_inventory_rows", 0)),
        "documents": report.get("documents_written", 0),
        "sections": report.get("sections_written", 0),
        "paragraphs": report.get("paragraphs_written", 0),
        "sentences": report.get("sentences_written", 0),
        "skipped_after_cleaning": len(report.get("skipped", [])),
        "quality_warning_files": len(report.get("quality_warnings", [])),
        **audit_metrics,
    }
    outputs = [
        _documents_path(ctx),
        _sections_path(ctx),
        _paragraphs_path(ctx),
        _sentences_path(ctx),
        report_path,
        *audit_outputs,
    ]
    validations = [
        {"name": "文档产物存在", "passed": _documents_path(ctx).exists(), "details": str(_documents_path(ctx))},
        {"name": "层级数量非零", "passed": all(metrics[key] > 0 for key in ("documents", "sections", "paragraphs", "sentences")), "details": f"docs={metrics['documents']}; sections={metrics['sections']}; paragraphs={metrics['paragraphs']}; sentences={metrics['sentences']}"},
        {"name": "报告与文件行数一致", "passed": metrics["documents"] == _line_count(_documents_path(ctx)) and metrics["sentences"] == _line_count(_sentences_path(ctx)), "details": "documents/sentences"},
        {
            "name": "已选文件数量守恒",
            "passed": metrics["inventory_rows"] == metrics["documents"] + metrics["skipped_after_cleaning"],
            "details": (
                f"selected={metrics['inventory_rows']}; documents={metrics['documents']}; "
                f"hard_skipped={metrics['skipped_after_cleaning']}"
            ),
        },
    ]
    return StageResult(
        "05_cleaning",
        "盘点、清洗与结构化",
        "needs_human_review" if feedback else "complete",
        "已执行现有清洗流水线并补充近重复、复杂对象、时效性和冲突审核。",
        metrics=metrics,
        inputs=[manual_root, web_root, inventory_file, ctx.resolve(ctx.config["cleaning"]["config"])],
        outputs=outputs,
        validations=validations,
        feedback=feedback,
        decisions=[
            "近重复采用 64 位字符五元组 SimHash，当前阈值为 0.90，只产生审核候选，不自动删除。",
            "复杂技术对象保留原形并输出审计表，不在增强阶段改写数值或单位。",
            "政策与报告时效性无法可靠自动判定时进入人工队列。",
        ],
    )


LEXICON_FIELDS = ["term_id", "term", "preferred_term", "category", "priority", "status", "notes"]


def _active_terms(ctx: WorkflowContext) -> tuple[list[dict[str, str]], list[dict[str, str]]]:
    """Load the three human-maintained lexical interfaces and detect conflicts."""
    paths = []
    if os.environ.get("CORPUS_USE_DOMAIN_LEXICON", "1") != "0":
        paths.append(("domain", ctx.resolve(ctx.config["linguistic"]["domain_lexicon"])))
    if os.environ.get("CORPUS_USE_TERMINOLOGY", "1") != "0":
        paths.append(("terminology", ctx.resolve(ctx.config["linguistic"]["terminology"])))
    discovered_path = ctx.resolve(ctx.config["linguistic"].get("discovered_terminology", "data/processed/topics/segmentation_term_dictionary.csv"))
    if discovered_path.exists():
        paths.append(("discovered_terminology", discovered_path))
    rows: list[dict[str, str]] = []
    for resource_type, path in paths:
        for row in read_csv(path):
            if (row.get("status") or "active").lower() in {"active", "approved", "启用", "通过"}:
                preferred = (row.get("preferred_term") or row.get("term") or "").strip()
                expressions = [preferred]
                if resource_type in {"terminology", "discovered_terminology"}:
                    expressions.extend(value.strip() for value in re.split(r"[|；;]", row.get("aliases", "")) if value.strip())
                    abbreviation = (row.get("abbreviation") or "").strip()
                    if abbreviation:
                        expressions.append(abbreviation)
                for expression in dict.fromkeys(value for value in expressions if value):
                    normalized = dict(row)
                    normalized["term"] = expression
                    normalized["preferred_term"] = preferred or expression
                    normalized.setdefault("category", resource_type)
                    normalized.setdefault("term_id", row.get("term_id") or stable_id("lex", expression))
                    rows.append(normalized)
    variant_path = ctx.resolve(ctx.config["linguistic"].get("curated_term_variants", "data/curated/terms/term_variants.csv"))
    if variant_path.exists():
        preferred_by_id = {row.get("term_id", ""): row.get("preferred_term", "") for row in rows if row.get("term_id")}
        for variant in read_csv(variant_path):
            value = str(variant.get("variant", "")).strip()
            term_id = str(variant.get("term_id", "")).strip()
            if value and term_id in preferred_by_id:
                rows.append({"term_id": term_id, "term": value, "preferred_term": preferred_by_id[term_id], "category": "curated_term_variant", "priority": "1000", "status": "approved", "notes": str(variant.get("variant_type", ""))})
    notes = read_csv(ctx.resolve(ctx.config["linguistic"]["segmentation_notes"])) if os.environ.get("CORPUS_USE_SEGMENTATION_NOTES", "1") != "0" else []
    conflicts: list[dict[str, str]] = []
    seen: dict[str, dict[str, str]] = {}
    for row in rows:
        term = row["term"].strip()
        previous = seen.get(term)
        if previous and previous.get("preferred_term") != row.get("preferred_term"):
            previous_category = previous.get("category")
            current_category = row.get("category")
            if previous_category == "terminology" and current_category == "discovered_terminology":
                # A newly observed abbreviation/surface form must not replace an
                # existing curated preferred-term mapping (for example BMS).
                continue
            if previous_category == "discovered_terminology" and current_category == "terminology":
                seen[term] = row
                continue
            conflicts.append({
                "term": term,
                "conflict_type": "duplicate_definition",
                "value_a": json.dumps(previous, ensure_ascii=False),
                "value_b": json.dumps(row, ensure_ascii=False),
                "human_decision": "",
                "reviewer": "",
                "review_notes": "",
            })
        elif not previous or row.get("category") in {"terminology", "discovered_terminology"}:
            # The formal terminology table is authoritative when the same
            # protected expression also appears in the broader domain lexicon.
            seen[term] = row
    for note in notes:
        if (note.get("status") or "active").lower() not in {"active", "approved", "启用", "通过"}:
            continue
        action = (note.get("action") or "").strip().lower()
        expression = (note.get("result") or note.get("target") or note.get("expression") or note.get("term") or "").strip()
        if action in {"merge", "force_merge", "合并"} and expression:
            seen.setdefault(expression, {
                "term_id": note.get("note_id") or note.get("rule_id", ""), "term": expression,
                "preferred_term": expression, "category": "segmentation_note",
                "priority": note.get("priority", "100"), "status": "active",
                "notes": note.get("notes", ""),
            })
    def priority(row: Mapping[str, str]) -> tuple[int, int, str]:
        try:
            value = int(row.get("priority") or 0)
        except ValueError:
            value = 0
        return (-len(row.get("term", "")), -value, row.get("term", ""))
    return sorted(seen.values(), key=priority), conflicts


def _create_jieba_tokenizer(terms: list[dict[str, str]], user_dict: Path | None = None) -> Any:
    tokenizer = jieba.Tokenizer()
    if user_dict and user_dict.exists() and os.environ.get("CORPUS_USE_JIEBA_USER_DICT", "1") != "0":
        tokenizer.load_userdict(str(user_dict))
    for row in terms:
        try:
            priority = max(int(row.get("priority") or 100), 1)
        except ValueError:
            priority = 100
        tokenizer.add_word(row["term"], freq=10_000_000 + priority)
    return tokenizer


def _segment_text(text: str, terms: list[dict[str, str]], tokenizer: Any | None = None) -> list[dict[str, Any]]:
    """Jieba default-mode segmentation with controlled terms injected at high frequency."""
    tokenizer = tokenizer or _create_jieba_tokenizer(terms)
    controlled = {row["term"]: row for row in terms}
    tokens: list[dict[str, Any]] = []
    for value, start, end in tokenizer.tokenize(text, mode="default", HMM=True):
        if not value or value.isspace():
            continue
        matched = controlled.get(value)
        if matched:
            source, term_id = "controlled_lexicon", matched.get("term_id", "")
        elif re.fullmatch(r"[A-Za-z0-9]+(?:[._+\-/][A-Za-z0-9]+)*", value):
            source, term_id = "jieba_ascii", ""
        elif re.search(r"[\u3400-\u9fff]", value):
            source, term_id = "jieba", ""
        else:
            source, term_id = "symbol", ""
        tokens.append({"text": value, "start": start, "end": end, "source": source, "term_id": term_id})
    return tokens


def linguistic_stage(ctx: WorkflowContext, **_: Any) -> StageResult:
    output = ctx.configured_path("linguistic_output")
    output.mkdir(parents=True, exist_ok=True)
    terms, conflicts = _active_terms(ctx)
    conflict_path = output / "lexicon_conflicts.csv"
    write_csv(conflict_path, conflicts, ["term", "conflict_type", "value_a", "value_b", "human_decision", "reviewer", "review_notes"])
    token_path = output / "sentences_tokens.jsonl"
    user_dict = ctx.resolve(ctx.config["linguistic"]["jieba_user_dict"])
    tokenizer = _create_jieba_tokenizer(terms, user_dict)
    frequency: Counter[str] = Counter()
    source_frequency: Counter[str] = Counter()
    protected_occurrences = 0
    sentence_count = 0
    total_sentences = _line_count(_sentences_path(ctx))
    _emit_progress(2, f"准备逐句分词：0/{total_sentences}")
    with token_path.open("w", encoding="utf-8", newline="\n") as handle:
        for sentence in read_jsonl(_sentences_path(ctx)):
            tokens = _segment_text(str(sentence.get("text", "")), terms, tokenizer)
            for token in tokens:
                frequency[token["text"]] += 1
                source_frequency[token["source"]] += 1
                protected_occurrences += int(token["source"] == "controlled_lexicon")
            handle.write(json.dumps({
                "doc_id": sentence.get("doc_id"), "section_id": sentence.get("section_id"),
                "paragraph_id": sentence.get("paragraph_id"), "sentence_id": sentence.get("sentence_id"),
                "text": sentence.get("text", ""), "tokens": tokens,
            }, ensure_ascii=False) + "\n")
            sentence_count += 1
            if sentence_count == total_sentences or sentence_count % 1000 == 0:
                _emit_progress(
                    5 + round(sentence_count / max(total_sentences, 1) * 85),
                    f"正在逐句分词：{sentence_count}/{total_sentences}",
                )
    controlled_terms = {row["term"] for row in terms}
    freq_path = output / "word_frequency.csv"
    write_csv(freq_path, [
        {"token": token, "frequency": count, "is_protected": "yes" if token in controlled_terms else "no"}
        for token, count in frequency.most_common()
    ], ["token", "frequency", "is_protected"])
    review_rows = [
        {"candidate": token, "frequency": count, "reason": "high_frequency_unprotected_ascii", "human_decision": "", "preferred_term": "", "reviewer": "", "review_notes": ""}
        for token, count in frequency.most_common()
        if len(token) >= 2 and re.fullmatch(r"[A-Za-z][A-Za-z0-9._+\-/]+", token)
        and token not in controlled_terms
    ][: int(ctx.config["linguistic"].get("max_review_candidates", 500))]
    review_path = output / "segmentation_review_queue.csv"
    write_csv(review_path, review_rows, ["candidate", "frequency", "reason", "human_decision", "preferred_term", "reviewer", "review_notes"])
    feedback = []
    if conflicts:
        feedback.append(Feedback("error", "lexicon_conflict", f"发现 {len(conflicts)} 项词表冲突。", suggested_action="在 lexicon_conflicts.csv 中裁决后修订接口词表。", assignee_role="语言处理工程师"))
    if review_rows:
        feedback.append(Feedback("warning", "term_candidate", f"有 {len(review_rows)} 个高频未保护候选需审阅。", suggested_action="确认后加入术语表或注明排除。", assignee_role="领域专家"))
    validations = [
        {"name": "句子与分词记录一一对应", "passed": sentence_count == _line_count(_sentences_path(ctx)), "details": str(sentence_count)},
        {"name": "词典冲突清零", "passed": not conflicts, "details": str(len(conflicts))},
        {"name": "Jieba 分词器已加载", "passed": tokenizer is not None, "details": "jieba-0.42.1"},
    ]
    stage_status = "failed" if conflicts else ("needs_human_review" if feedback else "complete")
    return StageResult("06_linguistic", "基础语言处理", stage_status,
        "按上传的领域词表、术语表和分词备注表执行严格优先切分，并生成逐句可追溯结果。",
        metrics={"segmenter": "jieba-0.42.1", "sentences": sentence_count, "tokens": sum(frequency.values()), "unique_tokens": len(frequency), "average_token_characters": round(sum(len(token) * count for token, count in frequency.items()) / max(sum(frequency.values()), 1), 4), "single_han_token_rate": round(source_frequency.get("jieba", 0) and sum(count for token, count in frequency.items() if len(token) == 1 and re.fullmatch(r"[\u3400-\u9fff]", token)) / max(sum(frequency.values()), 1), 6), "active_lexicon_terms": len(terms), "protected_occurrences": protected_occurrences, "review_candidates": len(review_rows), "lexicon_conflicts": len(conflicts), "token_sources": dict(source_frequency)},
        inputs=[_sentences_path(ctx), ctx.resolve(ctx.config["linguistic"]["domain_lexicon"]), ctx.resolve(ctx.config["linguistic"]["terminology"]), ctx.resolve(ctx.config["linguistic"]["segmentation_notes"]), user_dict],
        outputs=[token_path, freq_path, review_path, conflict_path], validations=validations, feedback=feedback,
        decisions=["采用项目内封装的 Jieba 0.42.1 默认精确模式，不再将未命中汉字逐字回退。", "正式术语、领域词、分词备注和 Jieba 用户词典在切分前注入；受控词使用高词频保证整体保留。", "同一表达同时存在于领域词表和正式术语表时，以正式术语表的规范词和标识为准；只有规范词不一致才判为冲突。", "任何真实词表冲突均阻止其成为无条件正式结果，并进入人工反馈队列。"])


def quality_stage(ctx: WorkflowContext, **_: Any) -> StageResult:
    documents = list(read_jsonl(_documents_path(ctx)))
    doc_ids = {str(row.get("doc_id")) for row in documents}
    section_ids: set[str] = set()
    paragraph_ids: set[str] = set()
    defects: list[dict[str, Any]] = []
    hash_counts: Counter[str] = Counter(str(row.get("text_hash", "")) for row in documents)
    for row in read_jsonl(_sections_path(ctx)):
        object_id = str(row.get("section_id", ""))
        section_ids.add(object_id)
        if str(row.get("doc_id")) not in doc_ids:
            defects.append({"severity": "error", "defect_type": "orphan_section", "object_id": object_id, "details": row.get("doc_id", ""), "status": "open", "reviewer": "", "review_notes": ""})
    for row in read_jsonl(_paragraphs_path(ctx)):
        object_id = str(row.get("paragraph_id", ""))
        paragraph_ids.add(object_id)
        if str(row.get("doc_id")) not in doc_ids or str(row.get("section_id")) not in section_ids:
            defects.append({"severity": "error", "defect_type": "orphan_paragraph", "object_id": object_id, "details": f"doc={row.get('doc_id')};section={row.get('section_id')}", "status": "open", "reviewer": "", "review_notes": ""})
    sentence_count = 0
    for row in read_jsonl(_sentences_path(ctx)):
        sentence_count += 1
        if str(row.get("doc_id")) not in doc_ids or str(row.get("section_id")) not in section_ids or str(row.get("paragraph_id")) not in paragraph_ids:
            defects.append({"severity": "error", "defect_type": "orphan_sentence", "object_id": row.get("sentence_id", ""), "details": "parent reference missing", "status": "open", "reviewer": "", "review_notes": ""})
    duplicate_groups = sum(1 for value, count in hash_counts.items() if value and count > 1)
    duplicate_documents = sum(count - 1 for value, count in hash_counts.items() if value and count > 1)
    duplicate_rate = duplicate_documents / max(len(documents), 1)
    if duplicate_documents:
        defects.append({"severity": "warning", "defect_type": "exact_duplicate_documents", "object_id": "corpus", "details": f"documents={duplicate_documents};groups={duplicate_groups}", "status": "open", "reviewer": "", "review_notes": ""})
    token_count = _line_count(ctx.configured_path("linguistic_output") / "sentences_tokens.jsonl")
    if token_count != sentence_count:
        defects.append({"severity": "error", "defect_type": "token_alignment", "object_id": "corpus", "details": f"sentences={sentence_count};token_rows={token_count}", "status": "open", "reviewer": "", "review_notes": ""})
    defect_path = ctx.configured_path("governance_output") / "defect_register.csv"
    write_csv(defect_path, defects, ["severity", "defect_type", "object_id", "details", "status", "reviewer", "review_notes"])
    error_count = sum(row["severity"] == "error" for row in defects)
    threshold = float(ctx.config["quality"].get("max_exact_duplicate_rate", 0.01))
    feedback = [Feedback(row["severity"], row["defect_type"], str(row["details"]), object_id=str(row["object_id"]), suggested_action="修正后重新运行本阶段。", assignee_role="质量审核员") for row in defects]
    validations = [
        {"name": "父子关系完整", "passed": not any(row["defect_type"].startswith("orphan_") for row in defects), "details": f"defects={error_count}"},
        {"name": "句子分词对齐", "passed": token_count == sentence_count, "details": f"sentences={sentence_count};tokens={token_count}"},
        {"name": "精确重复率不超过阈值", "passed": duplicate_rate <= threshold, "details": f"rate={duplicate_rate:.6f};threshold={threshold:.6f}"},
    ]
    status = "failed" if any(not row["passed"] for row in validations) else ("needs_human_review" if defects else "complete")
    return StageResult("08_quality", "质量控制与验收", status, "对语料层级关系、重复、分词对齐和发布前技术指标执行自动验收，并输出缺陷台账。",
        metrics={"documents": len(documents), "sections": len(section_ids), "paragraphs": len(paragraph_ids), "sentences": sentence_count, "token_rows": token_count, "exact_duplicate_documents": duplicate_documents, "exact_duplicate_rate": round(duplicate_rate, 8), "defects": len(defects), "error_defects": error_count},
        inputs=[_documents_path(ctx), _sections_path(ctx), _paragraphs_path(ctx), _sentences_path(ctx), ctx.configured_path("linguistic_output") / "sentences_tokens.jsonl"], outputs=[defect_path], validations=validations, feedback=feedback,
        decisions=[f"精确重复文档率阈值为 {threshold:.2%}，来自 pipeline.json。", "自动验收失败时不得进入正式发布；警告项可由质量负责人书面裁决。", "语言难度与教学等级不在当前语料库加工范围内。"])


def _metadata_value(value: Any) -> str:
    return re.sub(r"\s+", " ", str(value or "")).strip()


def _valid_metadata(value: str) -> bool:
    return not any(char in value for char in ";=\r\n")


def _parse_index_counts(
    path: Path, encoding: str = "utf-8"
) -> tuple[int, int, list[str]]:
    docs = items = 0
    errors: list[str] = []
    in_doc = False
    has_global = has_group = False
    for line_no, raw in enumerate(path.read_text(encoding=encoding).splitlines(), 1):
        if raw.startswith("Doc "):
            docs += 1
            in_doc = True
            has_global = has_group = False
        elif raw == "#Global" or raw.startswith("#Global "):
            if not in_doc:
                errors.append(f"line {line_no}: metadata before Doc")
            has_global = True
            has_group = False
        elif raw == "#Group" or raw.startswith("#Group "):
            if not in_doc:
                errors.append(f"line {line_no}: metadata before Doc")
            elif not has_global:
                errors.append(f"line {line_no}: Group before Global")
            has_group = True
        elif raw.startswith("Item: "):
            items += 1
            if not in_doc:
                errors.append(f"line {line_no}: Item before Doc")
            elif not has_global or not has_group:
                errors.append(f"line {line_no}: Item missing Global or Group")
            has_global = has_group = False
        elif raw and not raw.startswith(("Doc ", "#Global ", "#Group ", "Item: ")):
            errors.append(f"line {line_no}: unknown record")
    return docs, items, errors


def _validate_index_annotation_keys(
    path: Path, encoding: str = "utf-8"
) -> list[str]:
    errors: list[str] = []
    allowed_global = {
        "语种", "语料领域", "语料来源", "文体", "话题", "知识维度", "术语", "任务",
        "句子序号", "来源", "一级话题", "二级话题", "三级话题",
    }
    allowed_group = {"话题", "知识维度", "术语", "任务"}
    for line_number, line in enumerate(path.read_text(encoding=encoding).splitlines(), 1):
        if line not in {"#Global", "#Group"} and not line.startswith(("#Global ", "#Group ")):
            continue
        if line in {"#Global", "#Group"}:
            continue
        values = line.split(" ", 1)[1]
        allowed = allowed_global if line.startswith("#Global ") else allowed_group
        for part in filter(None, values.split(";")):
            if "=" not in part:
                errors.append(f"line {line_number}: malformed annotation {part}")
                continue
            key, value = part.split("=", 1)
            if key not in allowed:
                errors.append(f"line {line_number}: unsupported annotation key {key}")
            if not value.strip():
                errors.append(f"line {line_number}: empty annotation value for {key}")
    return errors


def _split_metadata_values(value: Any, split_slash: bool = False) -> list[str]:
    if isinstance(value, str):
        values = re.split(r"[|；;/]" if split_slash else r"[|；;]", value)
    else:
        values = list(value or [])
    cleaned = sorted({_metadata_value(item) for item in values if _metadata_value(item)})
    return cleaned


def _append_metadata_pairs(
    pairs: list[str], rejected: list[dict[str, str]], object_id: str,
    label: str, values: Any, split_slash: bool = False,
) -> None:
    for value in _split_metadata_values(values, split_slash=split_slash):
        if _valid_metadata(value):
            pairs.append(f"{label}={value}")
        else:
            rejected.append({"object_id": object_id, "field": label, "value": value, "reason": "reserved_character"})


def _term_surface_map(ctx: WorkflowContext) -> dict[str, str]:
    """Return approved surface-form -> preferred-term mappings for Item-local matching."""
    canonical_path = ctx.project_root / "data" / "curated" / "terms" / "terminology.csv"
    canonical_rows = read_csv(canonical_path) if canonical_path.exists() else []
    preferred_by_id: dict[str, str] = {}
    surface_map: dict[str, str] = {}
    score_threshold = max(
        0.0, min(1.0, float(os.environ.get("CORPUS_TERM_SCORE_THRESHOLD", "0") or 0))
    )
    for row in canonical_rows:
        if str(row.get("status", "")).lower() not in {"approved", "active", "通过"}:
            continue
        try:
            score = float(row.get("score") or 1.0)
        except (TypeError, ValueError):
            score = 1.0
        if score < score_threshold:
            continue
        preferred = _metadata_value(row.get("preferred_term", ""))
        if len(preferred) < 2:
            continue
        preferred_by_id[str(row.get("term_id", ""))] = preferred
        surface_map[preferred] = preferred
        for alias in _split_metadata_values(row.get("aliases", "")):
            if len(alias) >= 2:
                surface_map[alias] = preferred
    variants_path = ctx.project_root / "data" / "curated" / "terms" / "term_variants.csv"
    for row in read_csv(variants_path) if variants_path.exists() else []:
        variant = _metadata_value(row.get("variant", ""))
        preferred = preferred_by_id.get(str(row.get("term_id", "")), "")
        if len(variant) >= 2 and preferred:
            surface_map[variant] = preferred
    return surface_map


def _match_item_terms(text: str, surface_map: Mapping[str, str]) -> list[str]:
    """Match only terms occurring in this exact Item; never inherit document terms."""
    if not text or not surface_map:
        return []
    pattern = re.compile("|".join(re.escape(value) for value in sorted(surface_map, key=len, reverse=True)))
    return sorted({surface_map[match.group(0)] for match in pattern.finditer(text)})


def _dialogue_heading_units(sections: Iterable[Mapping[str, Any]]) -> list[dict[str, Any]]:
    """Use non-overlapping leaf headings as dialogue article units."""
    rows = [dict(row) for row in sections]
    parent_ids = {str(row.get("parent_section_id", "")) for row in rows if row.get("parent_section_id")}
    units = [
        row for row in rows
        if str(row.get("section_type", "")) == "heading"
        and str(row.get("section_id", "")) not in parent_ids
        and _metadata_value(row.get("text", ""))
    ]
    return sorted(units, key=lambda row: (int(row.get("start_paragraph_index", 0)), str(row.get("section_id", ""))))


def _build_index(ctx: WorkflowContext) -> tuple[Path, list[dict[str, str]], dict[str, int]]:
    output = ctx.configured_path("index_output")
    output.mkdir(parents=True, exist_ok=True)
    index_path = output / ctx.config["index"].get("file_name", "corpus_index.txt")
    index_encoding = str(ctx.config["index"].get("encoding", "gb18030")).lower()
    if index_encoding != "gb18030":
        raise RuntimeError("最终结构化语料编码必须配置为 gb18030，供 LangSC BCC 建索引")
    documents = {str(row["doc_id"]): row for row in read_jsonl(_documents_path(ctx))}
    term_score_threshold = max(
        0.0, min(1.0, float(os.environ.get("CORPUS_TERM_SCORE_THRESHOLD", "0") or 0))
    )
    _emit_progress(2, f"读取结构化输入：{len(documents)} 篇文档；术语最低分 {term_score_threshold:.2f}")
    reuse_manifest = load_structure_reuse_manifest(ctx.project_root)
    resource_state = structure_resource_snapshot(ctx.project_root, ctx.config)
    previous_item_path = output / "index_items.jsonl"
    previous_items = list(read_jsonl(previous_item_path)) if previous_item_path.is_file() else []
    previous_items_by_doc: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for previous_item in previous_items:
        previous_items_by_doc[str(previous_item.get("doc_id", ""))].append(previous_item)
    previous_sources = (
        reuse_manifest.get("sources", {})
        if isinstance(reuse_manifest.get("sources"), dict) else {}
    )
    reuse_generation_compatible = (
        reuse_manifest.get("schema_version") == STRUCTURE_REUSE_SCHEMA_VERSION
        and reuse_manifest.get("resource_fingerprint") == resource_state["fingerprint"]
    )
    section_annotations: dict[str, set[str]] = defaultdict(set)
    document_annotations: dict[str, set[str]] = defaultdict(set)
    curated_path = ctx.project_root / "data" / "curated" / "topics" / "taxonomy.json"
    curated = read_json(curated_path) if curated_path.exists() else {}
    if os.environ.get("CORPUS_REQUIRE_TOPIC_REVIEW", "0") == "1" and curated.get("status") != "approved":
        raise RuntimeError("结构化已阻止：本轮话题体系尚未人工审核并发布为 approved")
    topic_names = {str(node.get("id", "")): str(node.get("name", "")) for node in curated.get("nodes", [])}
    cluster_to_nodes: dict[str, set[str]] = defaultdict(set)
    evidence_path = ctx.project_root / "data" / "curated" / "topics" / "taxonomy_cluster_evidence.csv"
    for row in read_csv(evidence_path) if evidence_path.exists() else []:
        if row.get("cluster_id") and row.get("node_id") in topic_names:
            cluster_to_nodes[row["cluster_id"]].add(row["node_id"])
    assignment_path = ctx.configured_path("topic_output") / "bertopic_unit_assignments.jsonl"
    if assignment_path.exists():
        for row in read_jsonl(assignment_path):
            section_id = str(row.get("section_id", ""))
            doc_id = str(row.get("doc_id", ""))
            for topic_id in cluster_to_nodes.get(str(row.get("cluster_id", "")), set()):
                label = topic_names.get(topic_id, "")
                if label:
                    section_annotations[section_id].add(label)
                    document_annotations[doc_id].add(label)

    recognition_annotations_path = (
        ctx.project_root / "data" / "curated" / "annotations" / "sentence_annotations.jsonl"
    )
    recognition_by_source_sentence: dict[str, dict[str, Any]] = {}
    recognition_by_unit: dict[str, dict[str, Any]] = {}
    recognition_by_document: dict[str, list[dict[str, Any]]] = defaultdict(list)
    if recognition_annotations_path.is_file():
        for row in read_jsonl(recognition_annotations_path):
            unit_id = str(row.get("document_id", ""))
            sentence_id = str(row.get("source_sentence_id", ""))
            source_doc_id = str(row.get("source_document_id", ""))
            if unit_id:
                recognition_by_unit[unit_id] = row
            if sentence_id:
                recognition_by_source_sentence[sentence_id] = row
            if source_doc_id:
                recognition_by_document[source_doc_id].append(row)

    sentences_by_doc: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for sentence in read_jsonl(_sentences_path(ctx)):
        sentences_by_doc[str(sentence.get("doc_id", ""))].append(sentence)
    sections_by_doc: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for section in read_jsonl(_sections_path(ctx)):
        sections_by_doc[str(section.get("doc_id", ""))].append(section)
    surface_map = _term_surface_map(ctx)
    rejected: list[dict[str, str]] = []
    item_rows: list[dict[str, Any]] = []
    dialogue_items = sentence_items = 0
    reused_documents = rebuilt_documents = 0

    for document_index, (doc_id, document) in enumerate(documents.items(), start=1):
        source_value = str(document.get("original_path") or document.get("relative_path") or "")
        source_path = Path(source_value)
        if not source_path.is_absolute():
            source_path = ctx.project_root / "data" / "raw" / "manual" / source_path
        try:
            source_relative = source_path.resolve().relative_to(ctx.project_root.resolve()).as_posix()
        except ValueError:
            source_relative = ""
        source_entry = previous_sources.get(source_relative.casefold()) if source_relative else None
        cached_rows = (
            previous_items_by_doc.get(str(source_entry.get("doc_id", "")), [])
            if isinstance(source_entry, dict) else []
        )
        can_reuse = bool(
            reuse_generation_compatible
            and source_path.is_file()
            and isinstance(source_entry, dict)
            and source_entry.get("source_sha256") == source_sha256(source_path, cached=False)
            and int(source_entry.get("item_count", 0)) == len(cached_rows)
            and cached_rows
        )
        if can_reuse:
            item_rows.extend(dict(row) for row in cached_rows)
            reused_documents += 1
            continue
        rebuilt_documents += 1
        title = _metadata_value(document.get("title") or doc_id)
        doc_name = re.sub(r"\s+", "_", title) or doc_id
        if not _valid_metadata(doc_name):
            rejected.append({"object_id": doc_id, "field": "DocName", "value": doc_name, "reason": "reserved_character"})
            doc_name = doc_id
        genre = _metadata_value(document.get("text_genre")) or "记叙文"
        if genre == "对话":
            annotated_turns = sorted(
                (
                    row for row in recognition_by_document.get(doc_id, [])
                    if row.get("analysis_unit_type") == "dialogue_turn"
                ),
                key=lambda row: int(row.get("sentence_sequence") or 0),
            )
            candidates = [{
                "item_id": str(row.get("document_id", "")),
                "doc_id": doc_id, "section_id": "", "heading": title,
                "heading_path": title, "text": _metadata_value(row.get("clean_text", "")),
                "recognition_annotation": row,
            } for row in annotated_turns]
            if not candidates:
                dialogue_units = _dialogue_heading_units(sections_by_doc.get(doc_id, []))
                candidates = [{
                    "item_id": f"{str(unit.get('section_id', ''))}__dialogue_article",
                    "doc_id": doc_id,
                    "section_id": str(unit.get("section_id", "")),
                    "heading": _metadata_value(unit.get("heading", "")),
                    "heading_path": _metadata_value(unit.get("heading_path", "")),
                    "text": _metadata_value(unit.get("text", "")),
                } for unit in dialogue_units]
            if not candidates:
                candidates = [{"item_id": f"{doc_id}__dialogue_article", "doc_id": doc_id, "section_id": "", "heading": title, "heading_path": title, "text": _metadata_value(document.get("text", ""))}]
            dialogue_items += len(candidates)
        else:
            candidates = [dict(row) for row in sentences_by_doc.get(doc_id, []) if _metadata_value(row.get("text", ""))]
            sentence_items += len(candidates)
        for candidate in candidates:
            item_id = str(candidate.get("item_id") or candidate.get("sentence_id") or stable_id(doc_id, candidate.get("text", "")))
            section_id = str(candidate.get("section_id", ""))
            text = _metadata_value(candidate.get("text", ""))
            annotation = candidate.get("recognition_annotation") or recognition_by_source_sentence.get(
                str(candidate.get("sentence_id", "")), {}
            )
            if not annotation:
                annotation = recognition_by_unit.get(item_id, {})
            recognized_paths = [str(value).strip() for value in annotation.get("topics", []) if str(value).strip()]
            levels = _semantic_topic_levels(recognized_paths[0] if recognized_paths else "")
            normalized_path = " > ".join(levels)
            topics = (
                {normalized_path} if normalized_path else
                section_annotations.get(section_id) or document_annotations.get(doc_id, set())
            )
            global_pairs = ["语种=汉语" if str(document.get("language", "zh")).lower().startswith("zh") else "语种=其他"]
            _append_metadata_pairs(global_pairs, rejected, item_id, "句子序号", str(annotation.get("sentence_sequence") or candidate.get("sentence_index", "")))
            _append_metadata_pairs(global_pairs, rejected, item_id, "来源", annotation.get("source") or document.get("source_type", ""), split_slash=True)
            for level_index, field in enumerate(("一级话题", "二级话题", "三级话题")):
                _append_metadata_pairs(global_pairs, rejected, item_id, field, levels[level_index] if level_index < len(levels) else "")
            _append_metadata_pairs(global_pairs, rejected, item_id, "语料领域", document.get("domain", ""))
            _append_metadata_pairs(global_pairs, rejected, item_id, "语料来源", document.get("source_type", ""), split_slash=True)
            _append_metadata_pairs(global_pairs, rejected, item_id, "文体", genre)
            _append_metadata_pairs(global_pairs, rejected, item_id, "话题", topics)
            group_pairs: list[str] = []
            scored_terms = annotation.get("term_candidates")
            if isinstance(scored_terms, list) and scored_terms:
                annotated_terms = []
                for value in scored_terms:
                    if not isinstance(value, Mapping):
                        continue
                    try:
                        score = float(value.get("score") if value.get("score") is not None else 1.0)
                    except (TypeError, ValueError):
                        score = 1.0
                    term = str(value.get("term", "")).strip()
                    if term and score >= term_score_threshold:
                        annotated_terms.append(term)
            else:
                annotated_terms = [str(value).strip() for value in annotation.get("terms", []) if str(value).strip()]
            terms = sorted(set(annotated_terms)) if annotation else _match_item_terms(text, surface_map)
            _append_metadata_pairs(group_pairs, rejected, item_id, "术语", terms)
            item_rows.append({
                "item_id": item_id, "doc_id": doc_id, "doc_name": doc_name,
                "section_id": section_id, "text_genre": genre, "text": text,
                "heading": _metadata_value(candidate.get("heading", "")),
                "heading_path": _metadata_value(candidate.get("heading_path", "")),
                "topics": sorted(topics), "terms": terms,
                "global_pairs": global_pairs, "group_pairs": group_pairs,
            })
        if document_index == len(documents) or document_index % 10 == 0:
            _emit_progress(
                8 + round(document_index / max(len(documents), 1) * 62),
                f"正在生成结构化 Item：{document_index}/{len(documents)} 篇，累计 {len(item_rows)} 条",
            )

    with index_path.open("w", encoding=index_encoding, newline="\n") as handle:
        current_doc = ""
        for item_index, item in enumerate(item_rows, start=1):
            if item["doc_id"] != current_doc:
                handle.write(f"Doc {item['doc_name']}\n")
                current_doc = str(item["doc_id"])
            handle.write("#Global " + ";".join(item["global_pairs"]) + "\n")
            handle.write("#Group" + (" " + ";".join(item["group_pairs"]) if item["group_pairs"] else "") + "\n")
            handle.write(f"Item: {item['text']}\n")
            if item_index == len(item_rows) or item_index % 5000 == 0:
                _emit_progress(
                    72 + round(item_index / max(len(item_rows), 1) * 18),
                    f"正在写入 GB18030 结构化语料：{item_index}/{len(item_rows)} 条",
                )
    manifest_path = output / "index_items.jsonl"
    write_jsonl(manifest_path, item_rows)
    items_by_doc: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for item in item_rows:
        items_by_doc[str(item.get("doc_id", ""))].append(item)
    source_records: dict[str, dict[str, Any]] = {}
    for doc_id, document in documents.items():
        source_value = str(document.get("original_path") or document.get("relative_path") or "")
        source_path = Path(source_value)
        if not source_path.is_absolute():
            source_path = ctx.project_root / "data" / "raw" / "manual" / source_path
        if not source_path.is_file():
            continue
        try:
            relative = source_path.resolve().relative_to(ctx.project_root.resolve()).as_posix()
        except ValueError:
            continue
        source_records[relative.casefold()] = {
            "path": relative,
            "source_sha256": source_sha256(source_path, cached=False),
            "source_bytes": source_path.stat().st_size,
            "doc_id": doc_id,
            "document_text_hash": str(document.get("text_hash", "")),
            "item_count": len(items_by_doc.get(doc_id, [])),
        }
    write_json(output / "structure_reuse_manifest.json", {
        "schema_version": STRUCTURE_REUSE_SCHEMA_VERSION,
        "created_at": now_iso(),
        "resource_fingerprint": resource_state["fingerprint"],
        "resources": resource_state,
        "index_file": index_path.name,
        "index_sha256": sha256_file(index_path),
        "sources": source_records,
    })
    reject_path = output / "index_rejected_metadata.csv"
    write_csv(reject_path, rejected, ["object_id", "field", "value", "reason"])
    parsed_docs, parsed_items, errors = _parse_index_counts(
        index_path, encoding=index_encoding
    )
    errors.extend(
        _validate_index_annotation_keys(index_path, encoding=index_encoding)
    )
    error_path = output / "index_validation_errors.csv"
    write_csv(error_path, [{"error": error} for error in errors], ["error"])
    return index_path, rejected, {
        "parsed_documents": parsed_docs, "parsed_items": parsed_items,
        "expected_documents": len(documents), "expected_items": len(item_rows),
        "dialogue_items": dialogue_items, "sentence_items": sentence_items,
        "reused_documents": reused_documents,
        "rebuilt_documents": rebuilt_documents,
        "validation_errors": len(errors),
    }


def _segment_text_with_pos(text: str, tokenizer: Any) -> tuple[list[str], list[str]]:
    tagger = pseg.POSTokenizer(tokenizer)
    pairs = [(pair.word, pair.flag or "x") for pair in tagger.cut(text, HMM=True) if pair.word and not pair.word.isspace()]
    return [word for word, _ in pairs], [flag for _, flag in pairs]


def _syntax_tree_units(payload: Any) -> str:
    if isinstance(payload, dict):
        value = payload.get("Units") or payload.get("units") or payload.get("tree")
        if isinstance(value, str) and value.strip():
            return value.strip()
    raise ValueError("句法服务回复中缺少非空 Units/tree 字段")


def _bcc_safe_tree(tree: str) -> str:
    """Disambiguate literal parentheses from BCC's tree delimiters."""
    return tree.replace("(x ()", "(x （)").replace("(x ))", "(x ）)")


def _syntax_chunks(words: list[str], pos: list[str], max_tokens: int) -> list[tuple[list[str], list[str]]]:
    """Split an oversized Item only for transport; the exported Item remains one tree."""
    if len(words) != len(pos):
        raise ValueError("句法输入的 words 与 pos 长度不一致")
    limit = max(int(max_tokens), 1)
    chunks: list[tuple[list[str], list[str]]] = []
    start = 0
    while start < len(words):
        hard_end = min(start + limit, len(words))
        end = hard_end
        if hard_end < len(words):
            # Prefer the last sentence/turn boundary in the latter half of the window.
            for index in range(hard_end - 1, start + limit // 2 - 1, -1):
                if words[index] in {"。", "！", "？", "!", "?", "；", ";"}:
                    end = index + 1
                    break
        chunks.append((words[start:end], pos[start:end]))
        start = end
    return chunks


def _request_syntax_tree(url: str, words: list[str], pos: list[str], timeout: float, attempts: int, on_attempt: Callable[[int], None] | None = None) -> tuple[str, Any]:
    body = json.dumps({"words": words, "pos": pos}, ensure_ascii=False).encode("utf-8")
    request = urllib.request.Request(url, data=body, headers={"Content-Type": "application/json"}, method="POST")
    last_error: Exception | None = None
    for attempt in range(max(attempts, 1)):
        if on_attempt:
            on_attempt(attempt + 1)
        try:
            with urllib.request.urlopen(request, timeout=timeout) as response:
                payload = json.loads(response.read().decode("utf-8"))
            return _syntax_tree_units(payload), payload
        except Exception as exc:  # network/protocol errors are audited per Item
            last_error = exc
            if callable(getattr(exc, "close", None)):
                exc.close()
            if attempt + 1 < max(attempts, 1):
                time.sleep(min(1.0 * (attempt + 1), 3.0))
    raise RuntimeError(str(last_error) if last_error else "句法服务调用失败")


def _build_syntax_tree_index(ctx: WorkflowContext) -> tuple[Path, dict[str, int], list[dict[str, str]]]:
    # Streaming checkpointing is shared by CLI and web runs.
    from .syntax_checkpoint import build_syntax_tree_index
    return build_syntax_tree_index(ctx)


def _version_profile(ctx: WorkflowContext, index_path: Path, index_metrics: Mapping[str, int]) -> tuple[Path, ...]:
    release = ctx.release_root
    release.mkdir(parents=True, exist_ok=True)
    documents = list(read_jsonl(_documents_path(ctx)))
    taxonomy = read_json(ctx.project_root / "data" / "curated" / "topics" / "taxonomy.json")
    canonical_terms = ctx.project_root / "data" / "curated" / "terms" / "terminology.csv"
    fallback_terms = ctx.configured_path("term_output") / "term_candidates.csv"
    terms = read_csv(canonical_terms if canonical_terms.exists() else fallback_terms)
    controlled_terminology = read_csv(ctx.resolve(ctx.config["linguistic"]["terminology"]))
    active_controlled_terms = [row for row in controlled_terminology if (row.get("status") or "active").lower() in {"active", "approved", "通过"}]
    term_run_metrics_path = ctx.configured_path("term_output") / "term_extraction_run_report.json"
    term_run_metrics = read_json(term_run_metrics_path) if term_run_metrics_path.exists() else {}
    # `bertopic_run_report.json` belongs to the retired BERTopic-only route.
    # The current corpus_recognition route activates an approved taxonomy
    # directly, so absence of this optional historical metric must not block a
    # Release.  A non-BERTopic run has zero BERTopic clusters by definition.
    topic_run_metrics_path = ctx.configured_path("topic_output") / "bertopic_run_report.json"
    topic_run_metrics = read_json(topic_run_metrics_path) if topic_run_metrics_path.exists() else {}
    source_counts = Counter(str(row.get("source_type", "unknown")) for row in documents)
    profile = {
        "schema_version": "1.0", "corpus_version": ctx.version, "generated_at": now_iso(),
        "release_level": ctx.config["workflow"].get("release_level", "internal_draft"),
        "scale": {"documents": len(documents), "sections": _line_count(_sections_path(ctx)), "paragraphs": _line_count(_paragraphs_path(ctx)), "sentences": _line_count(_sentences_path(ctx)), "characters": sum(int(row.get("clean_chars", 0)) for row in documents)},
        "sources": dict(source_counts),
        "topics": {
            "controlled_nodes": len(taxonomy.get("nodes", [])),
            "candidate_clusters": int(topic_run_metrics.get("topics", 0)),
            "candidate_cluster_source": "bertopic" if topic_run_metrics else "not_applicable",
        },
        "terms": {
            "canonical_candidates": len(terms),
            "controlled_entries": len(active_controlled_terms),
            "protected_surface_forms": _line_count(ctx.project_root / "data" / "curated" / "terms" / "segmentation_dictionary.txt"),
            "candidate_approvals": sum((row.get("status") or "").lower() in {"approved", "通过"} for row in terms),
        },
        "index": dict(index_metrics),
        "limitations": ["当前采集内容为流程样本，来源和话题分布尚不均衡。", "来源权利、话题体系和术语表仍包含人工待审核项。", "本版本不包含中文教学等级或语言难度标注。"],
    }
    json_path = release / "corpus_profile.json"
    write_json(json_path, profile)
    md_path = release / "corpus_profile.md"
    md_path.write_text("\n".join([
        f"# 新能源中文教学专门语料库 {ctx.version} 数据说明", "",
        f"- 发布级别：`{profile['release_level']}`", f"- 文档：{profile['scale']['documents']}", f"- 章节：{profile['scale']['sections']}",
        f"- 段落：{profile['scale']['paragraphs']}", f"- 句子：{profile['scale']['sentences']}", f"- 清洗后字符：{profile['scale']['characters']}",
        f"- 受控话题节点：{profile['topics']['controlled_nodes']}", f"- 候选聚类：{profile['topics']['candidate_clusters']}", f"- 去重后术语候选：{profile['terms']['canonical_candidates']}", f"- 已启用受控术语词条：{profile['terms']['controlled_entries']}", f"- 受保护表层形式：{profile['terms']['protected_surface_forms']}", f"- 本轮候选人工批准：{profile['terms']['candidate_approvals']}", "",
        "## 来源分布", "", *[f"- {key}：{value}" for key, value in source_counts.items()], "", "## 已知限制", "", *[f"- {value}" for value in profile["limitations"]], "",
        "详细机器可读数据见 `corpus_profile.json`。",
    ]) + "\n", encoding="utf-8")
    comparison_path = release / "version_comparison.csv"
    previous = str(ctx.config["workflow"].get("previous_version", "")).strip()
    comparison_rows: list[dict[str, Any]] = []
    previous_path = ctx.configured_path("releases_root") / previous / "corpus_profile.json" if previous else None
    previous_profile = read_json(previous_path) if previous_path and previous_path.exists() else {}
    for metric, current in profile["scale"].items():
        prior = previous_profile.get("scale", {}).get(metric, "")
        comparison_rows.append({"metric": metric, "previous_version": previous, "previous_value": prior, "current_version": ctx.version, "current_value": current, "change": current - prior if isinstance(prior, (int, float)) else ""})
    write_csv(comparison_path, comparison_rows, ["metric", "previous_version", "previous_value", "current_version", "current_value", "change"])
    changelog_path = release / "CHANGELOG.md"
    if not changelog_path.exists():
        changelog_path.write_text(f"# {ctx.version} 变更记录\n\n- 建立十阶段可复用加工、验证、反馈和报告闭环。\n- 发布级别：{profile['release_level']}。\n", encoding="utf-8")
    shutil.copy2(index_path, release / index_path.name)
    shutil.copy2(ctx.config_path, release / "pipeline.snapshot.json")
    definitions_path = release / "metric_definitions.md"
    definitions_path.write_text("""# 指标口径

- `documents`：清洗后 `documents.jsonl` 的非空记录数。
- `sections`：清洗后 `sections.jsonl` 的非空记录数，含文档根章节。
- `paragraphs`：清洗后 `paragraphs.jsonl` 的非空记录数，不再等同于索引 `Item:` 数。
- `sentences`：清洗后 `sentences.jsonl` 的非空记录数。
- `index.expected_items`：记叙文按句、对话按非重叠叶级标题篇章生成后的 Item 总数。
- `characters`：文档记录中 `clean_chars` 之和，不等同于 Unicode 字素或 token 数。
- `controlled_nodes`：候选分类体系中的人工预设受控节点数。
- `candidate_clusters`：本版本机器发现聚类数，未经审核不等于正式话题数。
- `canonical_candidates`：归一化去重后的术语候选数，未经人工审核不等于正式术语数。
- `controlled_entries`：人工上传术语表中状态为 `active/approved/通过` 的规范词条数。
- `protected_surface_forms`：受控规范词及其批准别名在本轮抽取中形成的受保护表层形式数。
- `candidate_approvals`：本轮候选审核主表中人工决定为 `approved/通过` 的新增正式术语数。

口径变化时必须在变更日志中标记统计不连续。
""", encoding="utf-8")
    return md_path, json_path, comparison_path, changelog_path, definitions_path


def release_stage(ctx: WorkflowContext, **_: Any) -> StageResult:
    _emit_progress(1, "开始生成结构化语料和版本说明")
    index_path, rejected, index_metrics = _build_index(ctx)
    _emit_progress(92, "结构化语料写入完成，正在生成清单与统计")
    profile_paths = _version_profile(ctx, index_path, index_metrics)
    export_manifest_path = ctx.release_root / "export_manifest.json"
    write_json(export_manifest_path, {
        "schema_version": "1.0", "corpus_version": ctx.version, "generated_at": now_iso(),
        "item_level": ctx.config["index"].get("item_level", "paragraph"),
        "index_encoding": ctx.config["index"].get("encoding", "gb18030"),
        "index_file": index_path.name, "index_sha256": sha256_file(index_path),
        "counts": index_metrics, "rejected_metadata": len(rejected),
    })
    manifest_path = ctx.release_root / "manifest.csv"
    release_files = [path for path in ctx.release_root.rglob("*") if path.is_file() and path != manifest_path]
    write_csv(manifest_path, [{"path": str(path.relative_to(ctx.release_root)).replace("\\", "/"), "bytes": path.stat().st_size, "sha256": sha256_file(path)} for path in sorted(release_files)], ["path", "bytes", "sha256"])
    feedback = []
    if rejected:
        feedback.append(Feedback("warning", "index_metadata", f"索引导出时拒绝了 {len(rejected)} 个含保留字符的元数据值。", suggested_action="修正源元数据后重新导出；当前未自行发明转义规则。", assignee_role="发布管理员"))
    if ctx.config["workflow"].get("release_level") != "public":
        feedback.append(Feedback("warning", "release_level", "当前配置为内部草案，不得作为公开正式版发布。", suggested_action="完成权利、领域和质量签署后再调整发布级别。", assignee_role="项目负责人"))
    validations = [
        {"name": "索引文档数一致", "passed": index_metrics["parsed_documents"] == index_metrics["expected_documents"], "details": str(index_metrics)},
        {"name": "索引条目数一致", "passed": index_metrics["parsed_items"] == index_metrics["expected_items"], "details": str(index_metrics)},
        {"name": "索引语法零错误", "passed": index_metrics["validation_errors"] == 0, "details": str(index_metrics["validation_errors"])},
    ]
    return StageResult("09_release", "索引构建、版本发布与归档", "needs_human_review" if feedback else "complete", "按指定扩展格式生成索引，验证记录计数，并输出每版本规模、话题、术语、来源和限制说明。",
        metrics={**index_metrics, "rejected_metadata": len(rejected), "release_files": len(release_files) + 1, "release_level": ctx.config["workflow"].get("release_level", "internal_draft")},
        inputs=[_documents_path(ctx), _paragraphs_path(ctx), ctx.configured_path("topic_output") / "section_topic_candidates.jsonl", ctx.config_path],
        outputs=[index_path, ctx.configured_path("index_output") / "index_items.jsonl", ctx.configured_path("index_output") / "structure_reuse_manifest.json", ctx.configured_path("index_output") / "index_rejected_metadata.csv", ctx.configured_path("index_output") / "index_validation_errors.csv", *profile_paths, manifest_path, export_manifest_path], validations=validations, feedback=feedback,
        decisions=["索引严格使用 Doc、#Global、#Group、Item: 四类记录。", "对话文档按非重叠的叶级标题篇章生成 Item；其他文本按句生成 Item。", "每个 Item 只标记自身文本中实际出现的术语。", "元数据含分号、等号或换行时拒绝该值并报告，不自行定义索引工具未规定的转义语法。", "句法结构分析是结构化之后的独立可选流程。", "每个版本固定输出数据说明、机器指标、版本比较、变更记录和校验清单。"])


def syntax_stage(ctx: WorkflowContext, **_: Any) -> StageResult:
    item_manifest = ctx.configured_path("index_output") / "index_items.jsonl"
    if not item_manifest.exists():
        raise RuntimeError("句法结构分析前必须先运行语料结构化，生成 index_items.jsonl")
    syntax_path, metrics, errors = _build_syntax_tree_index(ctx)
    _emit_progress(90, "句法树处理完成，正在更新版本清单")
    ctx.release_root.mkdir(parents=True, exist_ok=True)
    shutil.copy2(syntax_path, ctx.release_root / syntax_path.name)
    export_manifest_path = ctx.release_root / "export_manifest.json"
    export_manifest = read_json(export_manifest_path) if export_manifest_path.exists() else {
        "schema_version": "1.0", "corpus_version": ctx.version, "generated_at": now_iso(), "counts": {}
    }
    export_manifest.update({
        "syntax_tree_index_file": syntax_path.name,
        "syntax_tree_index_sha256": sha256_file(syntax_path),
        "syntax_tree_generated_at": now_iso(),
    })
    export_manifest.setdefault("counts", {}).update(metrics)
    write_json(export_manifest_path, export_manifest)
    manifest_path = ctx.release_root / "manifest.csv"
    release_files = [path for path in ctx.release_root.rglob("*") if path.is_file() and path != manifest_path]
    write_csv(manifest_path, [{"path": str(path.relative_to(ctx.release_root)).replace("\\", "/"), "bytes": path.stat().st_size, "sha256": sha256_file(path)} for path in sorted(release_files)], ["path", "bytes", "sha256"])
    feedback = [Feedback("warning", "syntax_tree_api", f"{len(errors)} 个 Item 未取得完整句法树，相应 Item 已保留原文。", suggested_action="查看 syntax_tree_errors.csv 后重跑本步骤；成功分块会使用缓存。", assignee_role="语言加工员")] if errors else []
    outputs = [syntax_path, ctx.configured_path("index_output") / "index_item_tokens.jsonl", ctx.configured_path("index_output") / "syntax_tree_responses.jsonl", ctx.configured_path("index_output") / "syntax_tree_errors.csv", export_manifest_path, manifest_path]
    return StageResult(
        "09_syntax", "Item 分词、词性与句法结构树", "needs_human_review" if errors else "complete",
        "读取结构化 Item，沿用当前用户词典进行 Jieba 分词和词性标注，再调用 struct_by_tokens 生成句法结构树版索引。",
        metrics=metrics, inputs=[item_manifest, ctx.resolve(ctx.config["linguistic"]["jieba_user_dict"]), ctx.config_path], outputs=outputs,
        validations=[{"name": "句法树 Item 全部成功", "passed": not errors, "details": f"success={metrics['syntax_successes']};failed={metrics['syntax_failures']}"}], feedback=feedback,
        decisions=["本步骤在页面中独立选择和运行。", "超长 Item 仅为接口调用按标点和 token 上限分块，最终仍合并为一个 Item。", "失败项不伪造句法树，保留原文并进入错误台账；成功分块缓存复用。"],
    )


def maintenance_stage(ctx: WorkflowContext, **_: Any) -> StageResult:
    rows: list[dict[str, str]] = []
    pending_signoffs = 0
    source_stage_dirs = sorted(
        path for path in ctx.reports_root.glob("*")
        if path.is_dir() and path.name != "10_maintenance"
    )
    for stage_dir in source_stage_dirs:
        for feedback in read_csv(stage_dir / "feedback_queue.csv"):
            if (feedback.get("status") or "open").lower() not in {"resolved", "closed", "已解决", "关闭"}:
                rows.append({"request_id": feedback.get("feedback_id", ""), "source_stage": stage_dir.name, "category": feedback.get("category", ""), "severity": feedback.get("severity", ""), "description": feedback.get("message", ""), "owner_role": feedback.get("assignee_role", ""), "status": feedback.get("status", "open"), "target_version": "", "resolution": ""})
        pending_signoffs += sum((row.get("decision") or "pending").lower() not in {"approved", "通过"} for row in read_csv(stage_dir / "signoff.csv"))
    path = ctx.report_dir("10_maintenance") / "change_requests.csv"
    write_csv(path, rows, ["request_id", "source_stage", "category", "severity", "description", "owner_role", "status", "target_version", "resolution"])
    feedback = []
    if rows:
        feedback.append(Feedback("warning", "open_change_requests", f"汇总到 {len(rows)} 项未闭环反馈。", suggested_action="分配目标版本并记录处理结论。", assignee_role="项目负责人"))
    if pending_signoffs:
        feedback.append(Feedback("warning", "pending_signoff", f"尚有 {pending_signoffs} 个角色签署未完成。", suggested_action="由相应执行、审核和负责角色签署。", assignee_role="项目负责人"))
    return StageResult("10_maintenance", "迭代维护与持续改进", "needs_human_review" if feedback else "complete", "汇总各阶段反馈和签署状态，形成下一版本变更请求入口。",
        metrics={"open_change_requests": len(rows), "pending_signoffs": pending_signoffs, "stages_scanned": len(source_stage_dirs)}, inputs=[ctx.reports_root], outputs=[path],
        validations=[{"name": "变更请求均有来源阶段", "passed": all(row["source_stage"] for row in rows), "details": str(len(rows))}], feedback=feedback,
        decisions=["未闭环反馈自动汇总但不自动标记解决。", "流程选择或阈值变化必须先更新配置和构建手册，再进入目标版本。"])


STAGES: dict[str, Callable[..., StageResult]] = {
    "01": planning_stage, "02": design_stage, "03": rights_stage, "04": collection_stage,
    "05": cleaning_stage, "06": linguistic_stage, "08": quality_stage,
    "09": release_stage, "09_syntax": syntax_stage, "10": maintenance_stage,
}
