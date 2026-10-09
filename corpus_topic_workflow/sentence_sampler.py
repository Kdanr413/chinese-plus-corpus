from __future__ import annotations

import argparse
import json
import math
import random
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any


def allocate_balanced_quotas(
    group_sizes: dict[str, int], target_size: int, seed: int = 20260817
) -> dict[str, int]:
    """Allocate an almost-equal quota per doc_id and redistribute unused capacity."""
    available = sum(max(0, int(size)) for size in group_sizes.values())
    if target_size < 1:
        raise ValueError("sample_size 必须至少为1")
    if available < target_size:
        raise ValueError(f"有效句子只有 {available} 条，无法抽取 {target_size} 条")
    doc_ids = sorted(doc_id for doc_id, size in group_sizes.items() if size > 0)
    if not doc_ids:
        raise ValueError("没有包含有效句子的 doc_id")
    random.Random(seed).shuffle(doc_ids)
    quotas = {doc_id: 0 for doc_id in doc_ids}
    remaining = target_size
    while remaining:
        progressed = False
        for doc_id in doc_ids:
            if quotas[doc_id] >= group_sizes[doc_id]:
                continue
            quotas[doc_id] += 1
            remaining -= 1
            progressed = True
            if remaining == 0:
                break
        if not progressed:
            raise RuntimeError("抽样配额分配未能达到目标数量")
    return dict(sorted(quotas.items()))


def systematic_positions(population_size: int, sample_size: int) -> list[int]:
    if not 0 <= sample_size <= population_size:
        raise ValueError("系统抽样数量必须在0和总体数量之间")
    if sample_size == 0:
        return []
    return [min(population_size - 1, math.floor((index + 0.5) * population_size / sample_size)) for index in range(sample_size)]


def sample_units_by_doc(
    units: list[dict[str, Any]], target_size: int,
    *, strategy: str = "balanced_systematic", seed: int = 20260817,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    groups: dict[str, list[tuple[int, dict[str, Any]]]] = defaultdict(list)
    for source_index, unit in enumerate(units):
        doc_id = str(unit.get("doc_id", "")).strip()
        if not doc_id:
            continue
        groups[doc_id].append((source_index, unit))
    quotas = allocate_balanced_quotas(
        {doc_id: len(rows) for doc_id, rows in groups.items()}, target_size, seed
    )
    rng = random.Random(seed)
    sampled: list[tuple[int, dict[str, Any]]] = []
    per_doc: list[dict[str, Any]] = []
    for doc_id in sorted(groups):
        rows = groups[doc_id]
        quota = quotas.get(doc_id, 0)
        if strategy == "balanced_systematic":
            selected_indexes = systematic_positions(len(rows), quota)
        elif strategy == "balanced_random":
            selected_indexes = sorted(rng.sample(range(len(rows)), quota))
        else:
            raise ValueError("sampling.strategy 只支持 balanced_systematic 或 balanced_random")
        selected = [rows[index] for index in selected_indexes]
        sampled.extend(selected)
        per_doc.append({
            "doc_id": doc_id,
            "eligible_count": len(rows),
            "sampled_count": len(selected),
        })
    sampled.sort(key=lambda item: item[0])
    sampled_units = [dict(unit) for _, unit in sampled]
    sampled_counts = [row["sampled_count"] for row in per_doc]
    audit = {
        "status": "sampled",
        "strategy": strategy,
        "seed": seed,
        "requested_sample_size": target_size,
        "sampled_size": len(sampled_units),
        "eligible_size": len(units),
        "document_count": len(per_doc),
        "documents_represented": sum(row["sampled_count"] > 0 for row in per_doc),
        "minimum_per_document": min(sampled_counts, default=0),
        "maximum_per_document": max(sampled_counts, default=0),
        "per_document": per_doc,
    }
    if len(sampled_units) != target_size:
        raise RuntimeError(f"实际抽取 {len(sampled_units)} 条，与目标 {target_size} 条不一致")
    return sampled_units, audit


def main() -> int:
    root = Path(__file__).resolve().parent
    parser = argparse.ArgumentParser(description="按 doc_id 均衡抽取话题聚类句子")
    parser.add_argument("--input", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--sample-size", type=int, default=800)
    parser.add_argument("--strategy", choices=("balanced_systematic", "balanced_random"), default="balanced_systematic")
    parser.add_argument("--seed", type=int, default=20260817)
    parser.add_argument("--config", type=Path, default=root / "configs" / "bertopic.json")
    parser.add_argument("--report", type=Path, default=None)
    args = parser.parse_args()

    # Reuse exactly the same validity rules as the clustering pipeline.
    from run_topic_demo import build_units, load_bertopic_config

    config = load_bertopic_config(args.config)
    input_config = config["input"]
    units, excluded, policy = build_units(
        args.input,
        int(input_config["min_analysis_characters"]),
        int(input_config["max_analysis_characters"]),
        input_config["structural_noise_exact"] if input_config["exclude_structural_noise"] else [],
        input_config["structural_noise_regex"] if input_config["exclude_structural_noise"] else [],
    )
    if policy != "sentence_level":
        raise ValueError("输入文件必须包含 sentence_id")
    sampled, audit = sample_units_by_doc(
        units, args.sample_size, strategy=args.strategy, seed=args.seed
    )
    args.output.parent.mkdir(parents=True, exist_ok=True)
    with args.output.open("w", encoding="utf-8", newline="") as handle:
        for rank, row in enumerate(sampled, start=1):
            output = {
                "doc_id": row["doc_id"],
                "sentence_id": row["unit_id"],
                "sentence_index": row.get("sentence_index", ""),
                "paragraph_id": row.get("paragraph_id", ""),
                "section_id": row.get("section_id", ""),
                "section_path": row.get("heading", ""),
                "is_heading": False,
                "text": row["analysis_text"],
                "topic_sampling": {
                    "rank": rank,
                    "strategy": args.strategy,
                    "seed": args.seed,
                    "target_size": args.sample_size,
                },
            }
            handle.write(json.dumps(output, ensure_ascii=False) + "\n")
    report_path = args.report or args.output.with_suffix(".report.json")
    report = {
        **audit,
        "input_path": str(args.input),
        "output_path": str(args.output),
        "excluded_before_sampling": len(excluded),
        "exclusion_reasons": dict(Counter(row["reason"] for row in excluded)),
    }
    report_path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(report, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
