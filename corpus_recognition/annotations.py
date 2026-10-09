"""Portable CSV annotation tasks and multi-annotator consensus summaries."""

from __future__ import annotations

import csv
import io
import json
import uuid
from collections import Counter
from typing import Any, Mapping, Sequence


ANNOTATION_FIELDS = (
    "run_id", "scope", "decision_id", "document_id", "analysis_unit_type",
    "text", "machine_candidate", "candidate_method", "candidate_evidence",
    "model_evidence", "annotator",
    "decision", "final_value", "reason",
)
SCOPES = {"topic", "term"}
DECISIONS = {"confirm", "modify", "reject"}


def _machine_values(bundle: Mapping[str, Any], scope: str) -> dict[str, str]:
    values: dict[str, list[str]] = {}
    rows = bundle["topic_candidates"] if scope == "topic" else bundle["term_candidates"]
    for row in rows:
        document_id = str(row.get("recognition_document_id") or "")
        if not document_id:
            continue
        value = (
            str((row.get("evidence") or {}).get("path") or row.get("topic_name") or "")
            if scope == "topic"
            else str(row.get("normalized_form") or "")
        ).strip()
        if value and value not in values.setdefault(document_id, []):
            values[document_id].append(value)
    return {key: "，".join(items) for key, items in values.items()}


def _machine_details(bundle: Mapping[str, Any], scope: str) -> tuple[dict[str, str], dict[str, str]]:
    methods: dict[str, list[str]] = {}
    evidence: dict[str, list[str]] = {}
    rows = bundle["topic_candidates"] if scope == "topic" else bundle["term_candidates"]
    for row in rows:
        document_id = str(row.get("recognition_document_id") or "")
        if not document_id:
            continue
        method = str((row.get("source_method") if scope == "topic" else row.get("extractor")) or "").strip()
        if method and method not in methods.setdefault(document_id, []):
            methods[document_id].append(method)
        item_evidence = row.get("evidence") or {}
        readable = json.dumps(item_evidence, ensure_ascii=False, sort_keys=True)
        if readable not in evidence.setdefault(document_id, []):
            evidence[document_id].append(readable)
    return (
        {key: "；".join(items) for key, items in methods.items()},
        {key: "\n".join(items) for key, items in evidence.items()},
    )


def build_annotation_csv(bundle: Mapping[str, Any], scope: str) -> bytes:
    if scope not in SCOPES:
        raise ValueError("annotation scope must be topic or term")
    run_id = str(bundle["run"]["id"])
    documents = {str(row["id"]): row for row in bundle["documents"]}
    machine = _machine_values(bundle, scope)
    candidate_methods, candidate_evidence = _machine_details(bundle, scope)
    evidence_by_document: dict[str, list[str]] = {}
    candidate_field = "topic_candidate_id" if scope == "topic" else "term_candidate_id"
    candidate_rows = bundle["topic_candidates"] if scope == "topic" else bundle["term_candidates"]
    candidate_documents = {str(row["id"]): str(row.get("recognition_document_id") or "") for row in candidate_rows}
    for row in bundle.get("validator_results", []):
        candidate_id = str(row.get(candidate_field) or "")
        document_id = candidate_documents.get(candidate_id, "")
        if document_id:
            evidence_by_document.setdefault(document_id, []).append(
                f"{row.get('model') or row.get('validator')}：{row.get('decision')}｜{row.get('reason')}"
            )
    output = io.StringIO(newline="")
    writer = csv.DictWriter(output, fieldnames=ANNOTATION_FIELDS)
    writer.writeheader()
    for decision in bundle["resource_decisions"]:
        document_key = str(decision["recognition_document_id"])
        document = documents[document_key]
        writer.writerow({
            "run_id": run_id,
            "scope": scope,
            "decision_id": str(decision["id"]),
            "document_id": document["document_id"],
            "analysis_unit_type": (document.get("metadata") or {}).get("analysis_unit_type", "text_unit"),
            "text": document.get("clean_text", ""),
            "machine_candidate": machine.get(document_key, ""),
            "candidate_method": candidate_methods.get(document_key, ""),
            "candidate_evidence": candidate_evidence.get(document_key, ""),
            "model_evidence": "\n".join(evidence_by_document.get(document_key, [])),
            "annotator": "",
            "decision": "",
            "final_value": "",
            "reason": "",
        })
    return ("\ufeff" + output.getvalue()).encode("utf-8")


def parse_annotation_csv(data: bytes, *, run_id: uuid.UUID, scope: str) -> list[dict[str, str]]:
    if scope not in SCOPES:
        raise ValueError("annotation scope must be topic or term")
    try:
        text = data.decode("utf-8-sig")
    except UnicodeDecodeError as exc:
        raise ValueError("标注文件必须保存为 UTF-8 CSV") from exc
    reader = csv.DictReader(io.StringIO(text))
    if not set(ANNOTATION_FIELDS).issubset(reader.fieldnames or []):
        raise ValueError("标注文件列名不完整，请重新下载模板后填写")
    rows: list[dict[str, str]] = []
    seen: set[tuple[str, str]] = set()
    for line_number, original in enumerate(reader, start=2):
        row = {key: str(value or "").strip() for key, value in original.items()}
        if row["run_id"] != str(run_id) or row["scope"] != scope:
            raise ValueError(f"第 {line_number} 行不属于当前任务或标注类型")
        annotator, decision = row["annotator"], row["decision"].lower()
        if not annotator:
            raise ValueError(f"第 {line_number} 行缺少 annotator")
        if decision not in DECISIONS:
            raise ValueError(f"第 {line_number} 行 decision 必须是 confirm、modify 或 reject")
        if decision == "modify" and not row["final_value"]:
            raise ValueError(f"第 {line_number} 行选择 modify 时必须填写 final_value")
        if not row["reason"]:
            raise ValueError(f"第 {line_number} 行缺少 reason")
        key = (row["decision_id"], annotator)
        if key in seen:
            raise ValueError(f"第 {line_number} 行重复了同一标注员和任务")
        seen.add(key)
        rows.append(row)
    if not rows:
        raise ValueError("标注文件没有数据行")
    return rows


def annotation_status(bundle: Mapping[str, Any]) -> dict[str, Any]:
    human_enabled = bool(bundle["run"].get("config_snapshot", {}).get("human_validation"))
    required = max(1, int(bundle["run"].get("config_snapshot", {}).get("required_annotators") or 1))
    decisions = [str(row["id"]) for row in bundle.get("resource_decisions", [])]
    latest: dict[tuple[str, str, str], Mapping[str, Any]] = {}
    for review in bundle.get("human_reviews", []):
        state = review.get("after_state") or {}
        scope = str(state.get("review_scope") or "")
        if scope not in SCOPES or state.get("annotation_aggregate"):
            continue
        reviewer = str(review.get("reviewer") or "legacy-reviewer").strip()
        key = (str(review["resource_decision_id"]), scope, reviewer)
        latest[key] = review
    consensus: dict[str, dict[str, dict[str, Any]]] = {"topic": {}, "term": {}}
    summaries: dict[str, dict[str, Any]] = {}
    for scope in ("topic", "term"):
        completed = conflicts = 0
        annotators: set[str] = set()
        for decision_id in decisions:
            reviews = [row for (did, item_scope, _), row in latest.items() if did == decision_id and item_scope == scope]
            annotators.update(str(row.get("reviewer") or "legacy-reviewer") for row in reviews)
            votes: list[str] = []
            for review in reviews:
                state = review.get("after_state") or {}
                value = "__REJECT__" if review.get("action") == "reject" else str(
                    state.get("topics" if scope == "topic" else "terms") or ""
                ).strip()
                if value:
                    votes.append(value)
            winner = Counter(votes).most_common(1)[0] if votes else ("", 0)
            has_majority = bool(votes) and winner[1] > len(votes) / 2
            enough = len(reviews) >= required
            conflict = enough and not has_majority
            if enough and has_majority:
                completed += 1
            elif conflict:
                conflicts += 1
            consensus[scope][decision_id] = {
                "complete": enough and has_majority,
                "conflict": conflict,
                "reviewer_count": len(reviews),
                "value": winner[0] if has_majority else "",
                "reviewers": sorted(str(row.get("reviewer") or "legacy-reviewer") for row in reviews),
            }
        summaries[scope] = {
            "completed": completed,
            "total": len(decisions),
            "pending": len(decisions) - completed,
            "conflicts": conflicts,
            "annotators": sorted(annotators),
        }
    ready = (not human_enabled) or (
        bool(decisions)
        and all(summaries[scope]["completed"] == len(decisions) for scope in SCOPES)
    )
    return {
        "human_validation": human_enabled,
        "required_annotators": required,
        "ready": ready,
        "scopes": summaries,
        "consensus": consensus,
    }
