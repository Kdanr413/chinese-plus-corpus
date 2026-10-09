#!/usr/bin/env python3
"""Build a reviewable topic taxonomy with BERTopic; never overwrite the reference taxonomy."""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import random
import sys
from collections import Counter, defaultdict
from datetime import datetime
from pathlib import Path
from typing import Any, Iterable


def read_json(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8-sig"))


def read_jsonl(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text(encoding="utf-8-sig").splitlines() if line.strip()]


def write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def write_jsonl(path: Path, rows: Iterable[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join(json.dumps(row, ensure_ascii=False) + "\n" for row in rows), encoding="utf-8")


def preserve_matching_llm_responses(
    previous_tasks: list[dict[str, Any]],
    new_tasks: list[dict[str, Any]],
    previous_responses: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Keep audit records only when their indexed task is byte-for-byte equivalent.

    Successful records let the LLM runner skip already completed calls. Error records
    remain as an audit trail and do not prevent that task from being retried.
    """

    def canonical(value: dict[str, Any]) -> str:
        return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))

    matching_indexes = {
        index
        for index, task in enumerate(new_tasks)
        if index < len(previous_tasks) and canonical(previous_tasks[index]) == canonical(task)
    }
    preserved: list[dict[str, Any]] = []
    for response in previous_responses:
        try:
            task_index = int(response.get("task_index", -1))
        except (TypeError, ValueError):
            continue
        if task_index in matching_indexes:
            preserved.append(response)
    return preserved


def write_csv(path: Path, fields: list[str], rows: Iterable[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)


def sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def flatten_reference(reference: dict[str, Any]) -> tuple[list[dict[str, Any]], list[str]]:
    errors: list[str] = []
    nodes: list[dict[str, Any]] = []
    seen: set[str] = set()

    def visit(node: dict[str, Any], parent_id: str, path: list[str]) -> None:
        node_id = str(node.get("id", "")).strip()
        name = str(node.get("name", "")).strip()
        if not node_id or not name:
            errors.append(f"主树节点缺少 id/name：{path}")
            return
        if node_id in seen:
            errors.append(f"重复节点 ID：{node_id}")
            return
        seen.add(node_id)
        current_path = [*path, name]
        nodes.append({
            "id": node_id,
            "name": name,
            "parent_id": parent_id,
            "path": " > ".join(current_path),
            "definition": str(node.get("definition", "")).strip(),
            "keywords": [str(value).strip() for value in node.get("representative_keywords", []) if str(value).strip()],
            "node_type": "root" if not parent_id else "main_tree",
        })
        for child in node.get("children", []):
            visit(child, node_id, current_path)

    root = reference.get("root")
    if not isinstance(root, dict):
        return [], ["参考体系缺少 root 对象"]
    visit(root, "", [])
    for key, node_type in (("boundary_candidates", "boundary"), ("cross_cutting_dimensions", "dimension"), ("excluded_label_types", "excluded")):
        for item in reference.get(key, []):
            node_id = str(item.get("id", "")).strip()
            name = str(item.get("name", "")).strip()
            if not node_id or not name or node_id in seen:
                errors.append(f"{key} 包含空值或重复 ID：{node_id or name}")
                continue
            seen.add(node_id)
            nodes.append({"id": node_id, "name": name, "parent_id": "", "path": name, "definition": str(item.get("definition", "")).strip(), "keywords": item.get("representative_keywords", []), "node_type": node_type})
    return nodes, errors


def build_units(input_root: Path, minimum: int, maximum: int) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    units: list[dict[str, Any]] = []
    excluded: list[dict[str, Any]] = []
    for section in read_jsonl(input_root / "sections.jsonl"):
        text = " ".join(str(section.get(key, "")).strip() for key in ("heading_path", "heading", "text") if section.get(key)).strip()
        reason = ""
        if section.get("section_type") == "document_root":
            reason = "document_root"
        elif len(text) < minimum:
            reason = "too_short"
        elif not str(section.get("section_id", "")).strip():
            reason = "missing_section_id"
        if reason:
            excluded.append({"unit_id": section.get("section_id", ""), "doc_id": section.get("doc_id", ""), "heading": section.get("heading", ""), "reason": reason})
            continue
        units.append({"unit_id": str(section["section_id"]), "section_id": str(section["section_id"]), "doc_id": str(section.get("doc_id", "")), "heading": str(section.get("heading", "")), "heading_path": str(section.get("heading_path", "")), "analysis_text": text[:maximum]})
    return units, excluded


def keyword_match(summary_text: str, nodes: list[dict[str, Any]]) -> tuple[str, str, float]:
    scores: list[tuple[int, int, str, str]] = []
    lowered = summary_text.casefold()
    for node in nodes:
        if node["node_type"] not in {"root", "main_tree"}:
            continue
        hits = sum(lowered.count(str(word).casefold()) for word in node["keywords"] if str(word).strip())
        if hits:
            scores.append((hits, len(node["path"]), node["id"], node["name"]))
    if not scores:
        return "", "", 0.0
    score, _, node_id, name = max(scores)
    return node_id, name, float(score)


def require_bertopic() -> tuple[Any, Any, Any, Any]:
    try:
        from bertopic import BERTopic
        from hdbscan import HDBSCAN
        from sklearn.feature_extraction.text import CountVectorizer
        from umap import UMAP
    except ImportError as exc:
        raise RuntimeError("缺少 BERTopic 运行依赖。请先执行：python -m pip install -r requirements.txt") from exc
    return BERTopic, HDBSCAN, CountVectorizer, UMAP


def run_model(texts: list[str], config: dict[str, Any], seed: int) -> tuple[Any, list[int], Any]:
    BERTopic, HDBSCAN, CountVectorizer, UMAP = require_bertopic()
    count = len(texts)
    minimum = max(int(config["hdbscan"].get("min_topic_size_floor", 10)), round(count * float(config["hdbscan"].get("min_topic_size_ratio", 0.005))))
    min_samples = max(int(config["hdbscan"].get("min_samples_floor", 5)), round(minimum / 2))
    umap = UMAP(random_state=seed, **{key: value for key, value in config["umap"].items() if key != "random_state"})
    hdbscan = HDBSCAN(min_cluster_size=minimum, min_samples=min_samples, metric="euclidean", prediction_data=True)
    ngram = tuple(config["representation"].get("ngram_range", [1, 3]))
    vectorizer = CountVectorizer(analyzer="char", ngram_range=ngram)
    model = BERTopic(embedding_model=config["embedding_model"], umap_model=umap, hdbscan_model=hdbscan, vectorizer_model=vectorizer, calculate_probabilities=True, verbose=True)
    topics, probabilities = model.fit_transform(texts)
    return model, [int(topic) for topic in topics], probabilities


def main() -> int:
    parser = argparse.ArgumentParser(description="BERTopic 聚类结果与固定参考话题体系匹配、人工审核表生成")
    parser.add_argument("--input-root", type=Path, required=True)
    parser.add_argument("--reference-taxonomy", type=Path, required=True)
    parser.add_argument("--config", type=Path, required=True)
    parser.add_argument("--output-root", type=Path, required=True)
    parser.add_argument("--review-root", type=Path, required=True)
    parser.add_argument("--validate-only", action="store_true")
    args = parser.parse_args()
    reference = read_json(args.reference_taxonomy)
    config = read_json(args.config)
    nodes, errors = flatten_reference(reference)
    if errors:
        raise ValueError("；".join(errors))
    snapshot = {"source_path": str(args.reference_taxonomy), "sha256": sha256(args.reference_taxonomy), "taxonomy_name": reference.get("taxonomy_name"), "taxonomy_version": reference.get("taxonomy_version"), "captured_at": datetime.now().astimezone().isoformat(timespec="seconds"), "node_count": len(nodes), "reference": reference}
    write_json(args.output_root / "reference_taxonomy_snapshot.json", snapshot)
    if args.validate_only:
        print(json.dumps({"status": "valid", "nodes": len(nodes), "sha256": snapshot["sha256"]}, ensure_ascii=False))
        return 0
    units, excluded = build_units(args.input_root, int(config.get("min_analysis_characters", 40)), int(config.get("max_analysis_characters", 4000)))
    if len(units) < 2:
        raise RuntimeError("可用于 BERTopic 的有效章节不足 2 条")
    model, topics, probabilities = run_model([row["analysis_text"] for row in units], config, int(config.get("random_seed", 20260817)))
    by_topic: dict[int, list[int]] = defaultdict(list)
    for index, topic in enumerate(topics):
        by_topic[topic].append(index)
    rng = random.Random(int(config.get("random_seed", 20260817)))
    assignments: list[dict[str, Any]] = []
    summaries: list[dict[str, Any]] = []
    sample_rows: list[dict[str, Any]] = []
    for topic, indexes in sorted(by_topic.items()):
        cluster_id = "BT_OUTLIER" if topic == -1 else f"BT_TOPIC_{topic:03d}"
        for index in indexes:
            assignments.append({**{key: units[index][key] for key in ("unit_id", "section_id", "doc_id", "heading")}, "cluster_id": cluster_id, "bertopic_id": topic, "is_outlier": topic == -1})
        if topic == -1:
            continue
        words = [{"term": term, "score": round(float(score), 6)} for term, score in (model.get_topic(topic) or [])[: int(config["representation"].get("top_words", 20))]]
        ordered = list(indexes)
        center_n = int(config["representation"].get("center_samples", 5))
        boundary_n = int(config["representation"].get("boundary_samples", 3))
        random_n = int(config["representation"].get("random_samples", 5))
        center = ordered[:center_n]
        boundary = list(reversed(ordered))[:boundary_n]
        remaining = [index for index in ordered if index not in {*center, *boundary}]
        random_indexes = rng.sample(remaining, min(random_n, len(remaining)))
        for sample_type, selected in (("center", center), ("random", random_indexes), ("boundary", boundary)):
            for index in selected:
                sample_rows.append({"cluster_id": cluster_id, "sample_type": sample_type, "unit_id": units[index]["unit_id"], "doc_id": units[index]["doc_id"], "heading": units[index]["heading"], "excerpt": units[index]["analysis_text"][:500]})
        summary_text = " ".join([value["term"] for value in words] + [units[index]["heading"] for index in center])
        node_id, node_name, match_score = keyword_match(summary_text, nodes)
        documents = sorted({units[index]["doc_id"] for index in indexes})
        summaries.append({"cluster_id": cluster_id, "bertopic_id": topic, "unit_count": len(indexes), "document_count": len(documents), "representative_words": words, "reference_match_node_id": node_id, "reference_match_name": node_name, "reference_match_score": match_score, "risk_flags": [flag for flag, state in (("single_document", len(documents) == 1), ("small_cluster", len(indexes) < 5)) if state]})
    write_jsonl(args.output_root / "bertopic_unit_assignments.jsonl", assignments)
    write_json(args.output_root / "bertopic_topic_summary.json", {"topics": summaries})
    write_csv(args.output_root / "bertopic_topic_summary.csv", ["cluster_id", "bertopic_id", "unit_count", "document_count", "representative_words", "reference_match_node_id", "reference_match_name", "reference_match_score", "risk_flags"], [{**row, "representative_words": "|".join(item["term"] for item in row["representative_words"]), "risk_flags": "|".join(row["risk_flags"])} for row in summaries])
    write_jsonl(args.output_root / "bertopic_topic_samples.jsonl", sample_rows)
    write_csv(args.output_root / "bertopic_exclusion_audit.csv", ["unit_id", "doc_id", "heading", "reason"], excluded)
    write_json(args.output_root / "bertopic_stability_report.json", {"status": "pending_multi_run", "configured_random_seeds": config.get("stability_random_seeds", []), "note": "正式发布前须完成多随机种子对齐；本次工作表将其作为风险项。"})
    evidence_summaries = [{**row, "samples": [sample for sample in sample_rows if sample["cluster_id"] == row["cluster_id"]]} for row in summaries]
    tasks = [{"task_type": "cluster_to_taxonomy_mapping", "cluster": row, "reference_nodes": nodes} for row in evidence_summaries]
    tasks_path = args.output_root / "topic_mapping_llm_tasks.jsonl"
    responses_path = args.output_root / "topic_mapping_llm_responses.jsonl"
    previous_tasks = read_jsonl(tasks_path)
    previous_responses = read_jsonl(responses_path)
    preserved_responses = preserve_matching_llm_responses(previous_tasks, tasks, previous_responses)
    write_jsonl(tasks_path, tasks)
    write_jsonl(responses_path, preserved_responses)
    samples_by_cluster: dict[str, dict[str, list[str]]] = defaultdict(lambda: defaultdict(list))
    for row in sample_rows:
        samples_by_cluster[row["cluster_id"]][row["sample_type"]].append(f"{row['unit_id']}:{row['heading']}｜{row['excerpt']}")
    review_rows: list[dict[str, Any]] = []
    for node in nodes:
        review_rows.append({"review_item_id": f"NODE::{node['id']}", "item_type": "existing_node" if node["node_type"] in {"root", "main_tree"} else f"{node['node_type']}_candidate", "node_id": node["id"], "current_path": node["path"], "current_name": node["name"], "current_definition": node["definition"], "current_keywords": "|".join(node["keywords"]), "related_cluster_ids": "|".join(row["cluster_id"] for row in summaries if row["reference_match_node_id"] == node["id"]), "cluster_metrics": "", "center_samples": "", "random_samples": "", "boundary_samples": "", "llm_mapping": "", "llm_adjustment": "", "issue_types": "coverage_gap" if not any(row["reference_match_node_id"] == node["id"] for row in summaries) else "", "risk_flags": ""})
    for summary in summaries:
        cluster_id = summary["cluster_id"]
        review_rows.append({"review_item_id": f"CLUSTER::{cluster_id}", "item_type": "cluster_mapping_candidate", "node_id": cluster_id, "current_path": "", "current_name": "待固定匹配", "current_definition": "", "current_keywords": "|".join(item["term"] for item in summary["representative_words"]), "related_cluster_ids": cluster_id, "cluster_metrics": json.dumps({key: summary[key] for key in ("unit_count", "document_count", "reference_match_score")}, ensure_ascii=False), "center_samples": " || ".join(samples_by_cluster[cluster_id]["center"]), "random_samples": " || ".join(samples_by_cluster[cluster_id]["random"]), "boundary_samples": " || ".join(samples_by_cluster[cluster_id]["boundary"]), "llm_mapping": "", "llm_adjustment": "", "issue_types": "cluster_mapping", "risk_flags": "|".join(summary["risk_flags"])})
    machine_fields = ["review_item_id", "item_type", "node_id", "current_path", "current_name", "current_definition", "current_keywords", "related_cluster_ids", "cluster_metrics", "center_samples", "random_samples", "boundary_samples", "llm_mapping", "llm_adjustment", "issue_types", "risk_flags"]
    human_fields = ["human_decision", "approved_node_id", "approved_name", "approved_parent_id", "approved_definition", "approved_keywords", "include_scope", "exclude_scope", "target_node_id", "split_plan", "sample_judgement", "evidence_judgement", "review_notes", "reviewer", "reviewed_at"]
    write_csv(args.review_root / "taxonomy_review_workbench.csv", machine_fields + human_fields, review_rows)
    write_json(args.output_root / "bertopic_run_report.json", {"run_id": datetime.now().strftime("topic_%Y%m%d_%H%M%S"), "reference_sha256": snapshot["sha256"], "reference_version": reference.get("taxonomy_version"), "embedding_model": config.get("embedding_model"), "valid_units": len(units), "excluded_units": len(excluded), "topics": len(summaries), "outliers": len(by_topic.get(-1, [])), "review_rows": len(review_rows), "preserved_llm_response_records": len(preserved_responses), "status": "awaiting_human_review"})
    print(json.dumps({"status": "awaiting_human_review", "topics": len(summaries), "review_rows": len(review_rows), "preserved_llm_response_records": len(preserved_responses)}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
