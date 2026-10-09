#!/usr/bin/env python3
"""Build the optional post-publication LLM validation task for a curated taxonomy."""

from __future__ import annotations

import argparse
import json
from pathlib import Path


def main() -> int:
    parser = argparse.ArgumentParser(description="生成正式话题体系结构的大模型核验任务")
    parser.add_argument("--taxonomy", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    taxonomy = json.loads(args.taxonomy.read_text(encoding="utf-8-sig"))
    task = {
        "task_type": "final_taxonomy_structure_validation",
        "instruction": "核验名称一致性、父子层级、重复或交叉节点、定义边界和覆盖缺口；只报告问题和建议，不修改正式体系。",
        "taxonomy": taxonomy,
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(task, ensure_ascii=False) + "\n", encoding="utf-8")
    print(json.dumps({"tasks": 1, "output": str(args.output)}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
