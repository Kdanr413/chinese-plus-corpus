#!/usr/bin/env python3
"""Cluster extracted term candidates by their corpus contexts and create the term review workbook."""

from __future__ import annotations

import argparse
import csv
import json
from collections import Counter, defaultdict
from datetime import datetime
from pathlib import Path
from typing import Any


def read_json(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8-sig"))


def read_csv(path: Path) -> list[dict[str, str]]:
    with path.open("r", encoding="utf-8-sig", newline="") as handle:
        return [dict(row) for row in csv.DictReader(handle)]


def write_csv(path: Path, fields: list[str], rows: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)


def write_jsonl(path: Path, rows: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join(json.dumps(row, ensure_ascii=False) + "\n" for row in rows), encoding="utf-8")


def load_sentences(path: Path) -> dict[str, dict[str, Any]]:
    return {str(row.get("sentence_id")): row for row in (json.loads(line) for line in path.read_text(encoding="utf-8-sig").splitlines() if line.strip())}


def main() -> int:
    parser = argparse.ArgumentParser(description="术语候选上下文 TF-IDF、K-means 聚类与人工审核表生成")
    parser.add_argument("--candidates", type=Path, required=True)
    parser.add_argument("--sentences", type=Path, required=True)
    parser.add_argument("--sections", type=Path, required=True)
    parser.add_argument("--taxonomy", type=Path, required=True)
    parser.add_argument("--config", type=Path, required=True)
    parser.add_argument("--output-root", type=Path, required=True)
    parser.add_argument("--review-root", type=Path, required=True)
    args = parser.parse_args()
    if not args.taxonomy.exists():
        raise RuntimeError("术语识别被阻断：必须先发布 data/curated/topics/taxonomy.json")
    taxonomy = read_json(args.taxonomy)
    if taxonomy.get("status") != "approved":
        raise RuntimeError("术语识别被阻断：话题体系尚未标记为 approved")
    config = read_json(args.config)
    candidates = read_csv(args.candidates)
    sentences = load_sentences(args.sentences)
    sections = {str(row.get("section_id")): row for row in (json.loads(line) for line in args.sections.read_text(encoding="utf-8-sig").splitlines() if line.strip())}
    evidence_lookup = {**sentences, **sections}
    contexts: list[dict[str, Any]] = []
    context_documents: list[str] = []
    for row in candidates:
        evidence_ids = [value for value in row.get("evidence_ids", "").split("|") if value]
        evidence = [evidence_lookup[value] for value in evidence_ids if value in evidence_lookup]
        snippets = [str(item.get("text", "")) for item in evidence]
        context_document = " ".join([row.get("term", ""), *snippets])
        contexts.append({"candidate_id": row.get("candidate_id", ""), "candidate_term": row.get("term", ""), "evidence_ids": evidence_ids, "contexts": [{"sentence_id": item.get("sentence_id", ""), "doc_id": item.get("doc_id", ""), "section_id": item.get("section_id", ""), "text": item.get("text", "")} for item in evidence]})
        context_documents.append(context_document)
    if not candidates:
        raise RuntimeError("没有可供术语聚类的候选")
    try:
        from sklearn.cluster import KMeans
        from sklearn.feature_extraction.text import TfidfVectorizer
        from sklearn.metrics import silhouette_score
    except ImportError as exc:
        raise RuntimeError("缺少术语 TF-IDF/K-means 依赖。请先执行：python -m pip install -r requirements.txt") from exc
    vectorizer = TfidfVectorizer(analyzer="char", ngram_range=tuple(config.get("ngram_range", [1, 3])), max_features=int(config.get("max_features", 12000)))
    matrix = vectorizer.fit_transform(context_documents)
    candidate_k = sorted({int(value) for value in config.get("candidate_k", [8, 12, 16]) if 1 < int(value) < len(candidates)})
    if not candidate_k:
        candidate_k = [max(1, min(len(candidates), round(len(candidates) ** 0.5)))]
    trials: list[dict[str, Any]] = []
    best_model = None
    best_score = float("-inf")
    for k in candidate_k:
        model = KMeans(n_clusters=k, random_state=int(config.get("random_seed", 20260817)), n_init=int(config.get("n_init", 20)))
        labels = model.fit_predict(matrix)
        score = float(silhouette_score(matrix, labels, metric="cosine")) if 1 < k < len(candidates) else -1.0
        trials.append({"k": k, "silhouette": round(score, 6), "inertia": round(float(model.inertia_), 6)})
        if score > best_score:
            best_score, best_model = score, model
    assert best_model is not None
    labels = best_model.labels_.tolist()
    distances = best_model.transform(matrix)
    taxonomy_nodes = {str(node.get("id")): str(node.get("name")) for node in taxonomy.get("nodes", [])}
    assignments: list[dict[str, Any]] = []
    members: dict[int, list[int]] = defaultdict(list)
    for index, label in enumerate(labels):
        members[int(label)].append(index)
        assignments.append({"candidate_id": candidates[index].get("candidate_id", ""), "candidate_term": candidates[index].get("term", ""), "cluster_id": f"TERM_CLUSTER_{int(label):03d}", "distance_to_center": round(float(distances[index][int(label)]), 6), "stability": "single_run"})
    summary_rows: list[dict[str, Any]] = []
    for label, indexes in sorted(members.items()):
        ordered = sorted(indexes, key=lambda index: float(distances[index][label]))
        terms = [candidates[index].get("term", "") for index in ordered]
        summary_rows.append({"cluster_id": f"TERM_CLUSTER_{label:03d}", "candidate_count": len(indexes), "representative_candidates": "|".join(terms[:10]), "representative_contexts": " || ".join(context_documents[index][:300] for index in ordered[: int(config.get("representative_contexts", 5))]), "topic_distribution": "", "risk_flags": "small_cluster" if len(indexes) < 3 else ""})
    write_jsonl(args.output_root / "term_contexts.jsonl", contexts)
    write_csv(args.output_root / "term_cluster_assignments.csv", ["candidate_id", "candidate_term", "cluster_id", "distance_to_center", "stability"], assignments)
    write_csv(args.output_root / "term_cluster_summary.csv", ["cluster_id", "candidate_count", "representative_candidates", "representative_contexts", "topic_distribution", "risk_flags"], summary_rows)
    (args.output_root / "term_tfidf_features.json").write_text(json.dumps({"analyzer": "char", "ngram_range": config.get("ngram_range"), "features": vectorizer.get_feature_names_out().tolist(), "k_trials": trials, "selected_k": int(best_model.n_clusters)}, ensure_ascii=False, indent=2), encoding="utf-8")
    assignment_by_id = {row["candidate_id"]: row for row in assignments}
    cluster_neighbors = {row["cluster_id"]: row["representative_candidates"] for row in summary_rows}
    review_rows: list[dict[str, Any]] = []
    for row, context in zip(candidates, contexts):
        assignment = assignment_by_id[row.get("candidate_id", "")]
        review_rows.append({"candidate_id": row.get("candidate_id", ""), "candidate_term": row.get("term", ""), "cluster_id": assignment["cluster_id"], "occurrence_count": row.get("occurrence_count", ""), "document_count": row.get("document_count", ""), "heading_count": row.get("heading_count", ""), "statistical_scores": json.dumps({key: row.get(key, "") for key in ("score", "c_value", "context_entropy", "domain_specificity", "nested_dominance")}, ensure_ascii=False), "representative_contexts": " || ".join(item["text"] for item in context["contexts"]), "existing_term_matches": row.get("term_id", ""), "topic_distribution": "", "cluster_neighbors": cluster_neighbors[assignment["cluster_id"]], "risk_flags": "single_document" if str(row.get("document_count", "")) == "1" else "", "llm_evidence": ""})
    machine = ["candidate_id", "candidate_term", "cluster_id", "occurrence_count", "document_count", "heading_count", "statistical_scores", "representative_contexts", "existing_term_matches", "topic_distribution", "cluster_neighbors", "risk_flags", "llm_evidence"]
    human = ["human_decision", "approved_term_id", "preferred_term", "term_definition", "term_type", "aliases", "abbreviations", "english_term", "merge_target_id", "primary_topic_id", "related_topic_ids", "boundary_note", "evidence_judgement", "review_notes", "reviewer", "reviewed_at"]
    write_csv(args.review_root / "term_review_workbench.csv", machine + human, review_rows)
    write_csv(args.output_root / "term_candidates.csv", list(candidates[0]), candidates)
    write_csv(args.output_root / "term_topic_evidence.csv", ["candidate_id", "candidate_term", "primary_topic_id", "related_topic_ids", "topic_evidence_ids"], [])
    write_csv(args.output_root / "term_issue_candidates.csv", ["candidate_id", "issue_type", "details"], [{"candidate_id": row["candidate_id"], "issue_type": row["risk_flags"], "details": ""} for row in review_rows if row["risk_flags"]])
    write_jsonl(args.output_root / "term_llm_tasks.jsonl", [{"candidate_id": row["candidate_id"], "candidate_term": row["candidate_term"], "contexts": row["representative_contexts"], "cluster_neighbors": row["cluster_neighbors"], "allowed_actions": ["accept", "reject", "normalize", "merge", "split_sense", "needs_context"]} for row in review_rows])
    write_jsonl(args.output_root / "term_llm_responses.jsonl", [])
    write_csv(args.output_root / "term_llm_suggestions.csv", ["candidate_id", "suggestion", "preferred_term", "definition", "topic_ids", "confidence"], [])
    report = {"run_id": datetime.now().strftime("terms_%Y%m%d_%H%M%S"), "taxonomy_version": taxonomy.get("taxonomy_version"), "candidate_count": len(candidates), "selected_k": int(best_model.n_clusters), "k_trials": trials, "review_rows": len(review_rows), "status": "awaiting_human_review"}
    (args.output_root / "term_extraction_run_report.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(report, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
