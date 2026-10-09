#!/usr/bin/env python3
"""Merge mandatory topic mapping replies into the read-only evidence columns."""

from __future__ import annotations

import argparse
import csv
import json
from pathlib import Path


def read_jsonl(path: Path) -> list[dict]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8-sig").splitlines() if line.strip()]


def main() -> int:
    parser = argparse.ArgumentParser(description="将必选大模型话题匹配结果写入人工审核表证据列")
    parser.add_argument("--tasks", type=Path, required=True)
    parser.add_argument("--responses", type=Path, required=True)
    parser.add_argument("--workbench", type=Path, required=True)
    args = parser.parse_args()
    tasks = read_jsonl(args.tasks)
    responses = read_jsonl(args.responses)
    successful = {int(row["task_index"]): row for row in responses if row.get("status") == "success"}
    if len(successful) != len(tasks):
        raise RuntimeError(f"必选话题匹配不完整：任务 {len(tasks)}，成功 {len(successful)}")
    evidence: dict[str, str] = {}
    for index, task in enumerate(tasks):
        parsed = successful[index].get("parsed")
        if task.get("task_type") == "cluster_to_taxonomy_mapping":
            item_id = "CLUSTER::" + str(task.get("cluster", {}).get("cluster_id", ""))
        else:
            raise RuntimeError(f"任务 {index} 使用了不支持的类型：{task.get('task_type')}")
        evidence[item_id] = json.dumps(parsed, ensure_ascii=False, separators=(",", ":"))
    with args.workbench.open("r", encoding="utf-8-sig", newline="") as handle:
        reader = csv.DictReader(handle)
        fields = list(reader.fieldnames or [])
        rows = [dict(row) for row in reader]
    if "llm_mapping" not in fields:
        raise RuntimeError("审核表缺少 llm_mapping 字段")
    for row in rows:
        row["llm_mapping"] = evidence.get(row.get("review_item_id", ""), "")
    with args.workbench.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields)
        writer.writeheader()
        writer.writerows(rows)
    print(json.dumps({"tasks": len(tasks), "evidence_written": len(evidence)}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
