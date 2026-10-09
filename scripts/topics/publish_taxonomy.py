#!/usr/bin/env python3
"""Validate a taxonomy review workbook and publish the curated taxonomy."""

from __future__ import annotations

import argparse
import csv
import json
import shutil
import sys
from datetime import datetime
from pathlib import Path
from typing import Any

PROJECT_ROOT = Path(__file__).resolve().parents[2]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from scripts.topics.taxonomy_workflow import flatten_reference, read_json, write_csv, write_json


ALLOWED_BY_TYPE = {
    "existing_node": {"approve", "rename", "redefine", "reparent", "merge", "move_to_boundary", "move_to_dimension", "retire", "defer"},
    "boundary_candidate": {"approve", "rename", "redefine", "move_to_boundary", "move_to_dimension", "retire", "defer"},
    "dimension_candidate": {"approve", "rename", "redefine", "move_to_boundary", "move_to_dimension", "retire", "defer"},
    "excluded_candidate": {"approve", "rename", "redefine", "retire", "defer"},
    "cluster_mapping_candidate": {"map_existing", "add", "move_to_boundary", "move_to_dimension", "reject", "split", "defer"},
}
SAMPLE_JUDGEMENTS = {"支持", "部分支持", "不支持", "样本不足"}
EVIDENCE_JUDGEMENTS = {"充分", "有限", "不足"}


def read_rows(path: Path) -> list[dict[str, str]]:
    with path.open("r", encoding="utf-8-sig", newline="") as handle:
        return [dict(row) for row in csv.DictReader(handle)]


def require(row: dict[str, str], fields: set[str], errors: list[str], action: str = "") -> None:
    missing = sorted(field for field in fields if not row.get(field, "").strip())
    if missing:
        suffix = f"（动作 {action}）" if action else ""
        errors.append(f"{row.get('review_item_id')} 缺少条件必填字段：{','.join(missing)}{suffix}；其他修改字段可留空")


def main() -> int:
    parser = argparse.ArgumentParser(description="校验并发布人工审核后的正式话题体系")
    parser.add_argument("--reference", type=Path, required=True)
    parser.add_argument("--workbench", type=Path, required=True)
    parser.add_argument("--topic-summary", type=Path, required=True)
    parser.add_argument("--output-root", type=Path, required=True)
    parser.add_argument("--validate-only", action="store_true")
    args = parser.parse_args()
    reference = read_json(args.reference)
    reference_nodes, reference_errors = flatten_reference(reference)
    rows = read_rows(args.workbench)
    errors = list(reference_errors)
    issues: list[dict[str, str]] = []
    seen_items: set[str] = set()
    main_ids = {str(node.get("id", "")).strip() for node in reference_nodes if node.get("id") and node.get("node_type") in {"root", "main_tree"}}
    reference_ids = set(main_ids)
    for collection in ("boundary_candidates", "cross_cutting_dimensions", "excluded_label_types"):
        reference_ids.update(str(entry.get("id", "")).strip() for entry in reference.get(collection, []) if isinstance(entry, dict) and entry.get("id"))
    proposed_ids = [row.get("approved_node_id", "").strip() for row in rows if row.get("item_type", "").strip() == "cluster_mapping_candidate" and row.get("human_decision", "").strip() in {"add", "move_to_boundary", "move_to_dimension"} and row.get("approved_node_id", "").strip()]
    for row in rows:
        item = row.get("review_item_id", "").strip()
        if not item or item in seen_items:
            errors.append(f"审核项 ID 为空或重复：{item}")
        seen_items.add(item)
        item_type = row.get("item_type", "").strip()
        decision = row.get("human_decision", "").strip()
        allowed = ALLOWED_BY_TYPE.get(item_type)
        if not allowed:
            errors.append(f"{item} 的 item_type 无效：{item_type or '空'}")
            continue
        if decision not in allowed:
            errors.append(f"{item} 的 human_decision 无效：{decision or '空'}；{item_type} 只允许：{'、'.join(sorted(allowed))}")
            continue
        # 所有动作只统一要求决定、审核者身份和日期；reviewer 不是“通过/不通过”。
        require(row, {"reviewer", "reviewed_at"}, errors)
        if decision == "defer":
            errors.append(f"{item} 尚为 defer，阻断发布")
        if item_type == "cluster_mapping_candidate" and decision not in {"defer", "split"}:
            require(row, {"sample_judgement", "evidence_judgement"}, errors, decision)
        sample_value = row.get("sample_judgement", "").strip()
        evidence_value = row.get("evidence_judgement", "").strip()
        if item_type == "cluster_mapping_candidate" and sample_value and sample_value not in SAMPLE_JUDGEMENTS:
            errors.append(f"{item} 的 sample_judgement 无效：{sample_value}；只允许：{'、'.join(sorted(SAMPLE_JUDGEMENTS))}")
        if item_type == "cluster_mapping_candidate" and evidence_value and evidence_value not in EVIDENCE_JUDGEMENTS:
            errors.append(f"{item} 的 evidence_judgement 无效：{evidence_value}；只允许：{'、'.join(sorted(EVIDENCE_JUDGEMENTS))}")
        if decision == "rename": require(row, {"approved_name"}, errors, decision)
        if decision == "redefine": require(row, {"approved_definition"}, errors, decision)
        if decision == "reparent": require(row, {"approved_parent_id"}, errors, decision)
        if decision == "add":
            require(row, {"approved_node_id", "approved_name", "approved_parent_id", "approved_definition", "approved_keywords", "include_scope", "exclude_scope"}, errors, decision)
        if decision in {"merge", "map_existing"}: require(row, {"target_node_id"}, errors, decision)
        if decision in {"move_to_boundary", "move_to_dimension"} and item_type == "cluster_mapping_candidate":
            require(row, {"approved_node_id", "approved_name", "approved_definition", "approved_keywords"}, errors, decision)
        if decision in {"retire", "reject"}: require(row, {"review_notes"}, errors, decision)
        approved_id = row.get("approved_node_id", "").strip()
        if item_type == "cluster_mapping_candidate" and decision in {"add", "move_to_boundary", "move_to_dimension"} and approved_id:
            if approved_id in reference_ids:
                errors.append(f"{item} 的 approved_node_id 与既有体系 ID 冲突：{approved_id}；请填写新的唯一正式 ID")
            if approved_id.startswith("BT_TOPIC"):
                errors.append(f"{item} 的 approved_node_id 不能沿用临时聚类 ID：{approved_id}")
            if proposed_ids.count(approved_id) > 1:
                errors.append(f"{item} 的 approved_node_id 在本次新增候选中重复：{approved_id}")
        target_id = row.get("target_node_id", "").strip()
        if decision in {"merge", "map_existing"} and target_id and target_id not in reference_ids and target_id not in proposed_ids:
            errors.append(f"{item} 的 target_node_id 不存在：{target_id}；请填写既有或本次新增的正式节点 ID")
        parent_id = row.get("approved_parent_id", "").strip()
        if decision == "add" and parent_id and parent_id not in main_ids and parent_id not in proposed_ids:
            errors.append(f"{item} 的 approved_parent_id 不是主树正式节点：{parent_id}；边界对象应使用 move_to_boundary")
        if decision == "split":
            require(row, {"split_plan"}, errors, decision)
            errors.append(f"{item} 的 split_plan 必须先落实为 add 行，当前阻断发布")
        if item_type == "cluster_mapping_candidate" and row.get("sample_judgement") in {"不支持", "样本不足"} and decision not in {"reject", "defer"}:
            errors.append(f"{item} 的样本判断为 {row.get('sample_judgement')}，不能按 {decision} 发布")
    for message in errors:
        item_id = message.split(" ", 1)[0].rstrip("：")
        if "缺少条件必填字段" in message:
            suggestion = "补填消息中列出的字段；未列出的 approved_* 和范围字段继续留空即可。"
            code = "MISSING_ACTION_FIELDS"
        elif "cluster_mapping_candidate 只允许" in message:
            suggestion = "聚类候选不能直接 rename：若它属于已有节点，改为 map_existing 并填 target_node_id；若要形成新节点，改为 add 并完整定义新节点。"
            code = "ACTION_NOT_ALLOWED"
        elif "human_decision 无效" in message:
            suggestion = "从消息列出的该对象允许动作中重新选择；不要自由填写动作名。"
            code = "ACTION_NOT_ALLOWED"
        elif "ID 冲突" in message or "临时聚类 ID" in message or "本次新增候选中重复" in message:
            suggestion = "为该新节点填写未被参考体系和其他新增行使用的稳定 ID；不要复用 DIM_*、BOUNDARY_* 既有 ID 或 BT_TOPIC 临时 ID。"
            code = "NODE_ID_CONFLICT"
        elif "sample_judgement 无效" in message or "evidence_judgement 无效" in message:
            suggestion = "sample_judgement 只能填支持/部分支持/不支持/样本不足；evidence_judgement 只能填充分/有限/不足。具体意见写入 review_notes。"
            code = "INVALID_JUDGEMENT_VALUE"
        elif "target_node_id 不存在" in message or "approved_parent_id 不是" in message:
            suggestion = "从正式参考体系复制有效节点 ID；若对象属于边界而非主树，请改用 move_to_boundary。"
            code = "UNKNOWN_REFERENCE_ID"
        elif "defer" in message or "split_plan" in message:
            suggestion = "完成判断或将拆分结果落实为独立 add 行后，再重新上传完整审核表。"
            code = "UNRESOLVED_DECISION"
        else:
            suggestion = "按页面动作—字段对照表修改该行后重新上传完整审核表。"
            code = "REVIEW_VALIDATION"
        issues.append({"review_item_id": item_id, "severity": "error", "code": code, "message": message, "suggestion": suggestion})
    report = {"checked_at": datetime.now().astimezone().isoformat(timespec="seconds"), "rows": len(rows), "error_count": len(errors), "warning_count": 0, "publishable": bool(rows) and not errors, "errors": errors, "issues": issues}
    args.output_root.mkdir(parents=True, exist_ok=True)
    write_json(args.output_root / "taxonomy_validation_report.json", report)
    if errors:
        print(json.dumps(report, ensure_ascii=False, indent=2))
        return 2
    if args.validate_only:
        print(json.dumps(report, ensure_ascii=False, indent=2))
        return 0
    by_id = {node["id"]: dict(node) for node in reference_nodes if node["node_type"] in {"root", "main_tree"}}
    boundary = {str(item["id"]): dict(item) for item in reference.get("boundary_candidates", [])}
    dimensions = {str(item["id"]): dict(item) for item in reference.get("cross_cutting_dimensions", [])}
    excluded = {str(item["id"]): dict(item) for item in reference.get("excluded_label_types", [])}
    changes: list[dict[str, Any]] = []
    cluster_evidence: list[dict[str, Any]] = []
    for row in rows:
        decision = row["human_decision"].strip()
        node_id = row.get("node_id", "").strip()
        if row.get("item_type") == "cluster_mapping_candidate":
            if decision == "add":
                formal_id = row["approved_node_id"].strip()
                by_id[formal_id] = {"id": formal_id, "name": row["approved_name"].strip(), "parent_id": row["approved_parent_id"].strip(), "definition": row["approved_definition"].strip(), "representative_keywords": [value.strip() for value in row["approved_keywords"].split("|") if value.strip()], "include_scope": row["include_scope"].strip(), "exclude_scope": row["exclude_scope"].strip()}
                changes.append({"change_type": "add", "source_id": node_id, "target_id": formal_id, "reviewer": row["reviewer"], "reviewed_at": row["reviewed_at"], "reason": row.get("review_notes", "")})
                cluster_evidence.append({"node_id": formal_id, "cluster_id": node_id, "sample_judgement": row["sample_judgement"], "evidence_judgement": row["evidence_judgement"]})
            elif decision == "map_existing" and row.get("target_node_id", "").strip():
                cluster_evidence.append({"node_id": row["target_node_id"].strip(), "cluster_id": node_id, "sample_judgement": row["sample_judgement"], "evidence_judgement": row["evidence_judgement"]})
            elif decision in {"move_to_boundary", "move_to_dimension"}:
                formal_id = row["approved_node_id"].strip()
                target = boundary if decision == "move_to_boundary" else dimensions
                target[formal_id] = {"id": formal_id, "name": row["approved_name"].strip(), "definition": row["approved_definition"].strip(), "representative_keywords": [value.strip() for value in row["approved_keywords"].split("|") if value.strip()]}
                cluster_evidence.append({"node_id": formal_id, "cluster_id": node_id, "sample_judgement": row["sample_judgement"], "evidence_judgement": row["evidence_judgement"]})
            continue
        item_type = row.get("item_type", "")
        source_pool = {"existing_node": by_id, "boundary_candidate": boundary, "dimension_candidate": dimensions, "excluded_candidate": excluded}.get(item_type, by_id)
        node = source_pool.get(node_id)
        if not node:
            continue
        if decision == "retire":
            source_pool.pop(node_id, None)
        elif decision == "merge":
            source_pool.pop(node_id, None)
        elif decision == "move_to_boundary":
            source_pool.pop(node_id, None)
            boundary[node_id] = {"id": node_id, "name": row.get("approved_name") or node["name"], "definition": row.get("approved_definition") or node.get("definition", ""), "representative_keywords": [value.strip() for value in (row.get("approved_keywords") or "|".join(node.get("keywords", []))).split("|") if value.strip()]}
        elif decision == "move_to_dimension":
            source_pool.pop(node_id, None)
            dimensions[node_id] = {"id": node_id, "name": row.get("approved_name") or node["name"], "definition": row.get("approved_definition") or node.get("definition", ""), "representative_keywords": [value.strip() for value in (row.get("approved_keywords") or "|".join(node.get("keywords", []))).split("|") if value.strip()]}
        else:
            node["name"] = row.get("approved_name", "").strip() or node["name"]
            if "parent_id" in node:
                node["parent_id"] = row.get("approved_parent_id", "").strip() or node.get("parent_id", "")
            node["definition"] = row.get("approved_definition", "").strip() or node.get("definition", "")
            keywords = row.get("approved_keywords", "").strip()
            node["representative_keywords"] = [value.strip() for value in keywords.split("|") if value.strip()] if keywords else node.get("keywords", [])
            if row.get("include_scope", "").strip():
                node["include_scope"] = row["include_scope"].strip()
            if row.get("exclude_scope", "").strip():
                node["exclude_scope"] = row["exclude_scope"].strip()
        changes.append({"change_type": decision, "source_id": node_id, "target_id": row.get("target_node_id", ""), "reviewer": row["reviewer"], "reviewed_at": row["reviewed_at"], "reason": row.get("review_notes", "")})
    ids = set(by_id)
    structural = [f"{node_id} 父节点不存在：{node.get('parent_id')}" for node_id, node in by_id.items() if node.get("parent_id") and node.get("parent_id") not in ids]
    if structural:
        report["errors"] = structural
        report["error_count"] = len(structural)
        report["publishable"] = False
        write_json(args.output_root / "taxonomy_validation_report.json", report)
        return 2
    history = args.output_root / "history" / datetime.now().strftime("%Y%m%d_%H%M%S")
    history.mkdir(parents=True, exist_ok=True)
    shutil.copy2(args.reference, history / args.reference.name)
    shutil.copy2(args.workbench, history / args.workbench.name)
    curated = {"taxonomy_name": reference.get("taxonomy_name"), "taxonomy_version": datetime.now().strftime("%Y.%m.%d-review1"), "status": "approved", "classification_object": reference.get("classification_object"), "classification_principle": reference.get("classification_principle"), "nodes": list(by_id.values()), "boundary_candidates": list(boundary.values()), "cross_cutting_dimensions": list(dimensions.values()), "excluded_label_types": list(excluded.values()), "published_at": datetime.now().astimezone().isoformat(timespec="seconds")}
    write_json(args.output_root / "taxonomy.json", curated)
    write_csv(args.output_root / "taxonomy_change_log.csv", ["change_type", "source_id", "target_id", "reviewer", "reviewed_at", "reason"], changes)
    write_csv(args.output_root / "taxonomy_cluster_evidence.csv", ["node_id", "cluster_id", "sample_judgement", "evidence_judgement"], cluster_evidence)
    (args.output_root / "taxonomy_release_report.md").write_text(f"# 正式话题体系发布说明\n\n- 发布时间：{curated['published_at']}\n- 正式节点：{len(by_id)}\n- 边界候选：{len(boundary)}\n- 横向维度：{len(dimensions)}\n- 变更记录：{len(changes)}\n- 聚类证据关系：{len(cluster_evidence)}\n\n> ‘不作为新能源话题节点’只表示该标签不进入主话题树，不会删除任何原始语料或清洗后语料。\n", encoding="utf-8")
    print(json.dumps({"status": "published", "nodes": len(by_id)}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
