#!/usr/bin/env python3
"""Validate reviewed term candidates and publish curated terminology plus segmentation dictionary."""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import shutil
from datetime import datetime
from pathlib import Path


ALLOWED = {"approve", "normalize", "merge", "split_sense", "adjust_boundary", "reject", "defer"}


def stable_term_id(term: str) -> str:
    return "TERM_" + hashlib.sha256(term.strip().encode("utf-8")).hexdigest()[:12].upper()


def read_csv(path: Path) -> list[dict[str, str]]:
    with path.open("r", encoding="utf-8-sig", newline="") as handle:
        return [dict(row) for row in csv.DictReader(handle)]


def write_csv(path: Path, fields: list[str], rows: list[dict[str, str]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields, extrasaction="ignore")
        writer.writeheader(); writer.writerows(rows)


def main() -> int:
    parser = argparse.ArgumentParser(description="发布人工审核后的正式领域术语")
    parser.add_argument("--workbench", type=Path, required=True)
    parser.add_argument("--taxonomy", type=Path, required=True)
    parser.add_argument("--output-root", type=Path, required=True)
    parser.add_argument("--validate-only", action="store_true")
    args = parser.parse_args()
    taxonomy = json.loads(args.taxonomy.read_text(encoding="utf-8-sig"))
    topic_ids = {str(node.get("id")) for node in taxonomy.get("nodes", [])}
    rows = read_csv(args.workbench)
    errors: list[str] = []
    formal_ids: list[str] = []
    for row in rows:
        cid = row.get("candidate_id", "")
        decision = row.get("human_decision", "").strip()
        if decision not in ALLOWED:
            errors.append(f"{cid} 决定为空或无效")
            continue
        required = {"reviewer", "reviewed_at"}
        if decision in {"normalize", "adjust_boundary"}:
            required |= {"approved_term_id", "preferred_term", "term_definition", "term_type", "primary_topic_id"}
        if decision == "merge": required |= {"merge_target_id", "evidence_judgement"}
        if decision in {"split_sense", "defer"}: errors.append(f"{cid} 的 {decision} 必须先落实，当前阻断发布")
        missing = sorted(field for field in required if not row.get(field, "").strip())
        if missing: errors.append(f"{cid} 缺少：{','.join(missing)}")
        if decision == "approve":
            candidate_term = row.get("candidate_term", "").strip()
            if not candidate_term:
                errors.append(f"{cid} 的机器候选术语为空")
            else:
                formal_ids.append(stable_term_id(candidate_term))
        elif decision in {"normalize", "adjust_boundary"} and row.get("approved_term_id", "").strip():
            formal_ids.append(row["approved_term_id"].strip())
        for topic_id in [row.get("primary_topic_id", ""), *row.get("related_topic_ids", "").split("|")]:
            if topic_id.strip() and topic_id.strip() not in topic_ids: errors.append(f"{cid} 引用了不存在的话题 ID：{topic_id}")
    if len(formal_ids) != len(set(formal_ids)):
        errors.append("正式术语 ID 重复；请检查规范化结果或人工填写的 approved_term_id")
    report = {"checked_at": datetime.now().astimezone().isoformat(timespec="seconds"), "rows": len(rows), "error_count": len(errors), "publishable": bool(rows) and not errors, "errors": errors}
    args.output_root.mkdir(parents=True, exist_ok=True)
    (args.output_root / "term_validation_report.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    if errors:
        print(json.dumps(report, ensure_ascii=False, indent=2)); return 2
    if args.validate_only:
        print(json.dumps(report, ensure_ascii=False, indent=2)); return 0
    terminology: list[dict[str, str]] = []
    variants: list[dict[str, str]] = []
    relations: list[dict[str, str]] = []
    changes: list[dict[str, str]] = []
    for row in rows:
        decision = row["human_decision"].strip()
        if decision in {"reject", "merge"}: 
            changes.append({"candidate_id": row["candidate_id"], "decision": decision, "target_id": row.get("merge_target_id", ""), "reviewer": row["reviewer"], "reviewed_at": row["reviewed_at"]}); continue
        if decision == "approve":
            preferred = row.get("candidate_term", "").strip()
            term_id = stable_term_id(preferred)
            definition = ""
            term_type = "领域术语"
        else:
            term_id = row["approved_term_id"].strip()
            preferred = row["preferred_term"].strip()
            definition = row["term_definition"].strip()
            term_type = row["term_type"].strip()
        terminology.append({"term_id": term_id, "preferred_term": preferred, "definition": definition, "term_type": term_type, "status": "approved"})
        for variant_type, values in (("alias", row.get("aliases", "")), ("abbreviation", row.get("abbreviations", "")), ("english", row.get("english_term", ""))):
            for value in [item.strip() for item in values.split("|") if item.strip()]: variants.append({"term_id": term_id, "variant": value, "variant_type": variant_type})
        for relation, values in (("primary", row.get("primary_topic_id", "")), ("related", row.get("related_topic_ids", ""))):
            for topic_id in [item.strip() for item in values.split("|") if item.strip()]: relations.append({"term_id": term_id, "topic_id": topic_id, "relation": relation, "evidence": row.get("candidate_id", "")})
        changes.append({"candidate_id": row["candidate_id"], "decision": decision, "target_id": term_id, "reviewer": row["reviewer"], "reviewed_at": row["reviewed_at"]})
    history = args.output_root / "history" / datetime.now().strftime("%Y%m%d_%H%M%S"); history.mkdir(parents=True, exist_ok=True); shutil.copy2(args.workbench, history / args.workbench.name)
    write_csv(args.output_root / "terminology.csv", ["term_id", "preferred_term", "definition", "term_type", "status"], terminology)
    write_csv(args.output_root / "term_variants.csv", ["term_id", "variant", "variant_type"], variants)
    write_csv(args.output_root / "term_topic_relations.csv", ["term_id", "topic_id", "relation", "evidence"], relations)
    write_csv(args.output_root / "term_change_log.csv", ["candidate_id", "decision", "target_id", "reviewer", "reviewed_at"], changes)
    surfaces = sorted({row["preferred_term"] for row in terminology} | {row["variant"] for row in variants}, key=lambda value: (-len(value), value))
    (args.output_root / "segmentation_dictionary.txt").write_text("".join(f"{value} 10000000 nz\n" for value in surfaces), encoding="utf-8")
    (args.output_root / "term_release_report.md").write_text(f"# 正式术语发布说明\n\n- 正式术语：{len(terminology)}\n- 变体：{len(variants)}\n- 话题关系：{len(relations)}\n- 分词保护形式：{len(surfaces)}\n", encoding="utf-8")
    print(json.dumps({"status": "published", "terms": len(terminology)}, ensure_ascii=False)); return 0


if __name__ == "__main__": raise SystemExit(main())
