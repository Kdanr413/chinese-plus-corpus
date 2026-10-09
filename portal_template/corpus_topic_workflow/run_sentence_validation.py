from __future__ import annotations

import argparse
import json
from collections import Counter
from datetime import datetime
from pathlib import Path
from typing import Any

try:
    from .run_topic_demo import (
        build_units, classify_sentence_candidates, flatten_dynamic_taxonomy, flatten_taxonomy,
        load_bertopic_config, node_prototype, read_json, read_jsonl, read_term_table,
        score_node_candidates, write_csv, write_json, write_jsonl,
    )
    from .topic_taxonomy_store import read_taxonomy
except ImportError:  # Direct script execution by WorkflowManager.
    from run_topic_demo import (
        build_units, classify_sentence_candidates, flatten_dynamic_taxonomy, flatten_taxonomy,
        load_bertopic_config, node_prototype, read_json, read_jsonl, read_term_table,
        score_node_candidates, write_csv, write_json, write_jsonl,
    )
    from topic_taxonomy_store import read_taxonomy


def emit_progress(completed: int, total: int, message: str) -> None:
    print(
        "TOPIC_PROGRESS " + json.dumps(
            {"completed": completed, "total": total, "message": message}, ensure_ascii=True,
        ),
        flush=True,
    )


def published_cluster_priors(
    published_path: Path, node_by_path: dict[str, dict[str, Any]],
) -> dict[str, list[dict[str, Any]]]:
    priors: dict[str, list[dict[str, Any]]] = {}
    payload = read_json(published_path) if published_path.exists() else {"applied": []}
    for row in payload.get("applied", []):
        cluster_id = str(row.get("cluster_id", ""))
        result = row.get("result") or {}
        values = []
        for path in result.get("final_topic_paths", []):
            node = node_by_path.get(str(path).strip())
            if not node:
                continue
            values.append({
                "node_id": node["id"], "name": node["name"], "path": node["path"],
                "node_type": node["node_type"], "depth": node["depth"],
                "scope_status": str(node.get("scope_status") or "IN_SCOPE"),
                "score": 1.0, "vector_score": 1.0, "term_score": 0.0,
                "keyword_score": 0.0, "hierarchy_score": 0.0,
                "keyword_hits": [], "term_hits": [], "prior_source": "published_human_result",
            })
        priors[cluster_id] = values
    return priors


def main() -> int:
    root = Path(__file__).resolve().parent
    parser = argparse.ArgumentParser(description="使用已发布体系进行正式单句分类，不重新拟合 BERTopic")
    parser.add_argument("--sections", type=Path, required=True)
    parser.add_argument("--taxonomy", type=Path, required=True)
    parser.add_argument("--dynamic-taxonomy", type=Path, required=True)
    parser.add_argument("--term-table", type=Path, required=True)
    parser.add_argument("--config", type=Path, required=True)
    parser.add_argument("--cluster-assignments", type=Path, required=True)
    parser.add_argument("--published-changes", type=Path, required=True)
    parser.add_argument("--output-root", type=Path, required=True)
    args = parser.parse_args()

    config = load_bertopic_config(args.config)
    input_config = config["input"]
    matching = config["matching"]
    prior_config = config["workflow"]["cluster_prior"]
    units, excluded_units, unit_policy = build_units(
        args.sections,
        int(input_config["min_analysis_characters"]),
        int(input_config["max_analysis_characters"]),
        input_config["structural_noise_exact"] if input_config["exclude_structural_noise"] else [],
        input_config["structural_noise_regex"] if input_config["exclude_structural_noise"] else [],
    )
    if not units:
        raise RuntimeError("没有可用于正式分类的有效句子")
    emit_progress(0, len(units), "正在载入正式体系与句子向量模型")

    reference = read_json(args.taxonomy) if args.taxonomy.exists() else {}
    reference_nodes = flatten_taxonomy(reference)
    taxonomy = read_taxonomy(args.dynamic_taxonomy)
    nodes = flatten_dynamic_taxonomy(taxonomy, reference_nodes)
    nodes = [node for node in nodes if node.get("assignable")]
    if not nodes:
        raise RuntimeError("正式单句分类需要至少一个活动的三级话题")
    node_by_id = {node["id"]: node for node in nodes}
    node_by_path = {node["path"]: node for node in nodes}
    term_rows, term_audit = read_term_table(args.term_table)

    try:
        import numpy as np
        from sentence_transformers import SentenceTransformer
    except ModuleNotFoundError as error:
        raise RuntimeError(
            f"正式分类运行环境缺少依赖 {error.name}；请使用配置的 BERTopic Conda 环境"
        ) from error

    embedder = SentenceTransformer(config["embedding_model"])
    embeddings = embedder.encode(
        [unit["analysis_text"] for unit in units], show_progress_bar=True, normalize_embeddings=True,
    )
    prototype_vectors = embedder.encode(
        [node_prototype(node, term_rows) for node in nodes],
        show_progress_bar=False, normalize_embeddings=True,
    )

    previous_by_unit = {
        str(row.get("unit_id", "")): row for row in read_jsonl(args.cluster_assignments)
        if row.get("unit_id")
    }
    priors_by_cluster = published_cluster_priors(args.published_changes, node_by_path)
    assignments: list[dict[str, Any]] = []
    total = len(units)
    for index, unit in enumerate(units):
        previous = previous_by_unit.get(unit["unit_id"], {})
        cluster_id = str(previous.get("cluster_id") or "BT_UNASSIGNED")
        candidates = score_node_candidates(
            text=unit["analysis_text"], vector=embeddings[index], nodes=nodes,
            prototype_vectors=prototype_vectors, term_rows=term_rows, np=np,
            allowed_types={"main_tree", "boundary"},
            limit=max(int(matching["unit_candidate_limit"]), len(nodes)),
            weights=matching["weights"],
        )
        classified = classify_sentence_candidates(
            candidates, priors_by_cluster.get(cluster_id, []), matching, prior_config, node_by_id,
        )
        classified["topic_candidates"] = classified["topic_candidates"][:int(matching["stored_candidate_limit"])]
        assignments.append({
            "unit_id": unit["unit_id"], "doc_id": unit["doc_id"], "heading": unit["heading"],
            "section_id": unit.get("section_id", ""), "paragraph_id": unit.get("paragraph_id", ""),
            "sentence_index": unit.get("sentence_index", ""), "cluster_id": cluster_id,
            "bertopic_id": previous.get("bertopic_id", ""),
            "membership_confidence": previous.get("membership_confidence", ""),
            "centroid_similarity": previous.get("centroid_similarity", ""),
            "cluster_margin": previous.get("cluster_margin", ""),
            "classification_stage": "formal", "cross_cutting_dimensions": [],
            "representation_text": "", "excerpt": unit["analysis_text"][:500],
            **classified,
        })
        completed = index + 1
        if completed == total or completed % 10 == 0:
            emit_progress(completed, total, f"正在正式分类：{completed}/{total}")

    fields = [
        "unit_id", "doc_id", "heading", "classification_stage", "cluster_id", "classification_status",
        "cluster_primary_topic_path", "cluster_primary_topic_score", "sentence_deviates_from_cluster",
        "best_candidate_topic_id", "best_candidate_topic_path", "best_candidate_score",
        "sentence_score_before_prior", "cluster_prior_contribution", "accepted_topic_id",
        "accepted_topic_path", "accepted_topic_scope", "accepted_topic_score", "secondary_topic_ids", "dimension_ids",
        "matched_terms", "matched_keywords", "topic_match_margin",
    ]
    classification_rows = [{
        "unit_id": row["unit_id"], "doc_id": row["doc_id"], "heading": row["heading"],
        "classification_stage": "formal", "cluster_id": row["cluster_id"],
        "classification_status": row["classification_status"],
        "cluster_primary_topic_path": row.get("cluster_primary_topic", {}).get("path", ""),
        "cluster_primary_topic_score": row.get("cluster_primary_topic", {}).get("score", ""),
        "sentence_deviates_from_cluster": row.get("sentence_deviates_from_cluster", False),
        "best_candidate_topic_id": row["primary_topic"].get("node_id", ""),
        "best_candidate_topic_path": row["primary_topic"].get("path", ""),
        "best_candidate_score": row["primary_topic"].get("score", ""),
        "sentence_score_before_prior": row["primary_topic"].get("sentence_score", ""),
        "cluster_prior_contribution": row["primary_topic"].get("cluster_prior_contribution", ""),
        "accepted_topic_id": row["primary_topic"].get("node_id", "") if row["classification_status"] == "classified" else "",
        "accepted_topic_path": row["primary_topic"].get("path", "") if row["classification_status"] == "classified" else "",
        "accepted_topic_scope": row["primary_topic"].get("scope_status", "") if row["classification_status"] == "classified" else "",
        "accepted_topic_score": row["primary_topic"].get("score", "") if row["classification_status"] == "classified" else "",
        "secondary_topic_ids": "|".join(item["node_id"] for item in row["secondary_topics"]),
        "dimension_ids": "", "matched_terms": "|".join(row["primary_topic"].get("term_hits", [])),
        "matched_keywords": "|".join(row["primary_topic"].get("keyword_hits", [])),
        "topic_match_margin": row["topic_match_margin"],
    } for row in assignments]

    output = args.output_root
    output.mkdir(parents=True, exist_ok=True)
    write_jsonl(output / "demo_unit_assignments.jsonl", assignments)
    write_jsonl(output / "demo_sampled_units.jsonl", units)
    write_csv(output / "demo_sentence_classification.csv", fields, classification_rows)
    write_csv(output / "demo_exclusion_audit.csv", ["unit_id", "doc_id", "heading", "reason"], excluded_units)
    write_json(output / "demo_sampling_report.json", {
        "status": "reused_round_sample", "sampled_size": len(units),
        "document_count": len({unit["doc_id"] for unit in units}), "excluded_size": len(excluded_units),
    })
    term_evidence = []
    for term in term_rows:
        matched = [i for i, unit in enumerate(units) if any(form.casefold() in unit["analysis_text"].casefold() for form in term["forms"])]
        term_evidence.append({
            "term": term["term"], "aliases": "|".join(term["aliases"]), "status": term["status"],
            "term_role": term["role"], "term_weight": term["weight"],
            "configured_topic_node_ids": "|".join(term["topic_node_ids"]),
            "matched_unit_count": len(matched),
            "matched_cluster_ids": "|".join(sorted({assignments[i]["cluster_id"] for i in matched})),
            "observed_primary_topic_ids": "|".join(sorted({assignments[i]["primary_topic"].get("node_id", "") for i in matched if assignments[i]["primary_topic"].get("node_id")})),
        })
    write_csv(output / "demo_term_topic_evidence.csv", [
        "term", "aliases", "status", "term_role", "term_weight", "configured_topic_node_ids",
        "matched_unit_count", "matched_cluster_ids", "observed_primary_topic_ids",
    ], term_evidence)
    counts = Counter(row["classification_status"] for row in assignments)
    write_json(output / "demo_run_report.json", {
        "status": "formal_sentence_classification_completed",
        "created_at": datetime.now().astimezone().isoformat(timespec="seconds"),
        "mode": "direct_taxonomy_classification_with_published_cluster_prior",
        "workflow_stage": "sentence_classification",
        "reclustered": False,
        "valid_units": len(units), "excluded_units": len(excluded_units),
        "taxonomy_version": taxonomy.get("taxonomy_version", 1),
        "active_leaf_topic_count": len(nodes), "classification_counts": dict(counts),
        "cluster_assignment_source": str(args.cluster_assignments),
        "cluster_prior_source": str(args.published_changes), "term_table": term_audit,
        "unit_policy": unit_policy,
    })
    emit_progress(total, total, "正式单句分类完成，正在计算完善度指标")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
