from __future__ import annotations

import copy
import csv
import hashlib
import json
import os
import random
import shutil
import subprocess
import sys
import threading
import uuid
from collections import Counter
from datetime import datetime
from pathlib import Path
from typing import Any

from .llm_annotation_api import call_model, render_prompts, validate_config
from .run_topic_demo import build_units, load_bertopic_config, normalize_representative_term, write_csv
from .sentence_sampler import sample_units_by_doc
from .topic_taxonomy_store import (
    BOUNDARY_PATH_MIGRATIONS, add_topic_path, assignable_paths, empty_taxonomy, normalize_topic_path,
    normalize_three_level_taxonomy, read_taxonomy, split_topic_path, topic_record, write_taxonomy,
)


WORKFLOW_SCHEMA_VERSION = "3.0"
MODEL_RESULT_SCHEMA = {
    "type": "object",
    "properties": {
        "decision": {
            "type": "string",
            "enum": ["KEEP", "ASSIGN_EXISTING", "ADD_TOPIC", "MODIFY_TOPIC", "NO_STABLE_TOPIC"],
        },
        "topic_assignments": {
            "type": "array",
            "maxItems": 4,
            "items": {
                "type": "object",
                "properties": {
                    "level_1_topic": {"type": "string"},
                    "level_2_topic": {"type": "string"},
                    "level_3_topic": {"type": "string"},
                    "full_path": {"type": "string"},
                },
                "required": ["level_1_topic", "level_2_topic", "level_3_topic", "full_path"],
                "additionalProperties": False,
            },
        },
        "final_topic_paths": {"type": "array", "items": {"type": "string"}, "maxItems": 4},
        "scope_status": {
            "type": "string", "enum": ["IN_SCOPE", "OUT_OF_SCOPE", "UNCERTAIN"],
        },
        "cluster_quality": {
            "type": "string", "enum": ["COHERENT", "MIXED", "NOISE"],
        },
        "suggested_external_domain": {"type": "string"},
        "follow_up_action": {
            "type": "string",
            "enum": [
                "NONE", "ADD_TOPIC_CANDIDATE", "ROUTE_EXTERNAL_DOMAIN",
                "RECLUSTER", "MANUAL_REVIEW", "EXCLUDE_NOISE",
            ],
        },
        "changes": {
            "type": "array",
            "maxItems": 8,
            "items": {
                "type": "object",
                "properties": {
                    "operation": {
                        "type": "string",
                        "enum": [
                            "ADD_TOPIC", "RENAME_TOPIC", "MOVE_TOPIC", "EDIT_DEFINITION",
                            "EDIT_KEYWORDS", "EDIT_SCOPE", "MERGE_TOPICS", "SPLIT_TOPIC", "NONE",
                        ],
                    },
                    "topic_id": {"type": "string"},
                    "topic_path": {"type": "string"},
                    "target_topic_id": {"type": "string"},
                    "target_path": {"type": "string"},
                    "new_path": {"type": "string"},
                    "definition": {"type": "string"},
                    "keywords": {"type": "array", "items": {"type": "string"}, "maxItems": 12},
                    "scope_status": {
                        "type": "string", "enum": ["IN_SCOPE", "OUT_OF_SCOPE"],
                    },
                    "reason": {"type": "string"},
                },
                "required": [
                    "operation", "topic_id", "topic_path", "target_topic_id", "target_path",
                    "new_path", "definition", "keywords", "reason",
                ],
                "additionalProperties": False,
            },
        },
        "rationale": {"type": "string"},
        "confidence": {"type": "number", "minimum": 0, "maximum": 1},
        "needs_human_review": {"type": "boolean"},
    },
    "required": [
        "decision", "topic_assignments", "final_topic_paths", "scope_status", "cluster_quality",
        "suggested_external_domain", "follow_up_action", "changes", "rationale", "confidence",
        "needs_human_review",
    ],
    "additionalProperties": False,
}

MODEL_SYSTEM_PROMPT = """你是中文领域话题体系的独立审核模型。你将按固定顺序审核一批聚类。
你只能读取本模型自己的工作体系，不能假设其他模型的结论。每次判断必须同时参考话题名称、层级、说明、典型词、聚类代表词和代表文本。
必须先读取工作体系中的 scope_policy 和每个话题的 scope_status。若 restrict_new_level_1=true，模型只能在 allowed_level_1_topics 内新增话题；人工已经确认并写入体系、且 scope_status=OUT_OF_SCOPE 的三级话题可以作为受控的体系外标签使用，但模型不得自行新增体系外一级话题。
体系固定为三级：一级领域 > 二级类别 > 三级具体话题。必须依次判断一级、二级、三级，不能跳过上级直接猜三级；每个 topic_assignments 元素必须分别填写 level_1_topic、level_2_topic、level_3_topic 和一致的 full_path。
任务中的 hierarchy_candidates 是一级、二级判断证据，只能帮助确定上级范围，不能直接当作三级最终话题；reference_candidates 是已有三级候选，但不是新增话题的限制条件。reference_candidates 为空、得分低或均不合适时，仍须继续判断是否应新增话题，不能因此直接输出 NO_STABLE_TOPIC。
严格按以下顺序判断：
1. 先判断是否属于 scope_policy 规定的领域；体系外内容优先匹配人工已确认的 OUT_OF_SCOPE 三级话题，匹配成功时使用 ASSIGN_EXISTING 并保留 scope_status=OUT_OF_SCOPE；没有已有外部话题时使用 NO_STABLE_TOPIC 与 ROUTE_EXTERNAL_DOMAIN，不得由模型自行新增体系外话题。
2. 再判断聚类是否语义集中；MIXED 应重聚类，NOISE 应排除，二者都不得据此新增话题。
3. 对 IN_SCOPE + COHERENT 聚类，逐级比较已有一级、二级、三级的名称、说明和典型词；已有三级能够覆盖时使用 ASSIGN_EXISTING 或 KEEP。
4. 若聚类属于当前领域且语义集中，但所有已有三级都不能准确覆盖，必须使用 ADD_TOPIC，而不是 NO_STABLE_TOPIC。
新增时优先复用已有一级、二级；若已有一级合适但缺少二级，可在允许的一级下同时建立新的二级和三级；仅在 restrict_new_level_1=false 且现有一级确实都不能容纳时，才允许提出新的一级。系统会根据三级完整路径自动建立缺失的上级节点，不要为一级、二级分别提交 ADD_TOPIC。
允许一个聚类归入多个互不从属的话题。每个新增三级话题都必须在 topic_assignments 和 final_topic_paths 中给出相同的完整三级路径，并在 changes 中提供一条对应的 ADD_TOPIC：new_path 为完整路径，definition 为边界清晰的三级话题说明，keywords 至少3个且来自代表词或代表文本。一级、二级不设置聚类代表词。
新增名称应与现有体系命名风格一致，避免同义重复、过宽上位概念、文体名称、机构名称、单篇事件或仅由通用词组成的话题。不要因单篇文档、章节标题或通用表达创建话题。
新增话题不设置“至少来自若干文档”或“至少包含若干句子”的硬门槛；应依据语义稳定性、跨轮复现、多模型一致性和人工确认决定是否正式发布。
不要直接删除话题；重复话题在 changes 中使用 MERGE_TOPICS。无法形成稳定话题时 decision 使用 NO_STABLE_TOPIC，并继续判断：scope_status（IN_SCOPE/OUT_OF_SCOPE/UNCERTAIN）、cluster_quality（COHERENT/MIXED/NOISE）、suggested_external_domain 和 follow_up_action。
分流规则：体系外且语义集中使用 ROUTE_EXTERNAL_DOMAIN；混杂聚类使用 RECLUSTER；噪声使用 EXCLUDE_NOISE；证据不足使用 MANUAL_REVIEW。体系内、语义集中且现有体系缺失时，应使用 ADD_TOPIC，而不是 NO_STABLE_TOPIC。严格返回指定 JSON。"""

MODEL_USER_PROMPT = """【本模型当前工作体系】
{{taxonomy_json}}

【当前聚类审核任务】
{{task_json}}

请判断聚类应保留、归入、新增或修改哪些话题，并返回符合以下 Schema 的 JSON：
{{output_schema_json}}

先明确回答：①是否属于当前领域；②聚类是集中、混杂还是噪声；③已有三级话题能否覆盖；④若不能覆盖，是否满足新增条件。reference_candidates 为空或不合适不等于没有话题：对于 IN_SCOPE + COHERENT 的稳定内容，应按体系层级提出新增。
topic_assignments 与 final_topic_paths 必须逐项一致。changes 中不需要修改时返回空数组。ADD_TOPIC 的 new_path 必须是恰好三级的完整路径（一级 > 二级 > 三级），并与 topic_assignments 中的 full_path 对应；definition 是三级话题说明，keywords 是三级话题的代表词且至少3个。优先复用现有一级、二级；缺少二级时可由完整路径自动建立，不要为父级单独提交 ADD_TOPIC 或关键词。
每次均须填写 scope_status、cluster_quality、suggested_external_domain、follow_up_action。正常归类时 follow_up_action 为 NONE；ADD_TOPIC 时为 ADD_TOPIC_CANDIDATE；NO_STABLE_TOPIC 必须按体系外、混杂、噪声或不确定情形选择对应后续动作。
编辑现有话题时优先填写 topic_id；RENAME_TOPIC/MOVE_TOPIC 使用 new_path；MERGE_TOPICS 使用 target_topic_id 或 target_path。"""

DEFAULT_GATES = {
    "max_outlier_rate": 0.30,
    "min_cluster_coverage": 0.90,
    "min_sentence_coverage": 0.80,
    "max_unknown_rate": 0.15,
    "max_out_of_scope_false_acceptance_rate": 0.05,
    "max_noise_leakage_rate": 0.02,
    "max_uncertain_sentence_rate": 0.10,
    "max_unresolved_conflict_rate": 0.05,
    "max_human_override_rate": 0.10,
    "max_new_topic_rate": 0.03,
    "min_level1_agreement": 0.95,
    "min_full_path_agreement": 0.80,
    "min_anchor_stability": 0.80,
    "required_consecutive_rounds": 2,
}

VALIDATION_IDENTITY_LABELS = {
    "IN_SCOPE_TYPICAL": "新能源领域典型句",
    "IN_SCOPE_ATYPICAL": "新能源相关但非典型或话题证据不足",
    "OUT_OF_SCOPE": "非新能源领域句",
    "NOISE": "结构性噪声",
    "UNCERTAIN": "待逐句确认",
}


def now_iso() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


def read_json(path: Path, default: Any = None) -> Any:
    if not path.exists() or path.stat().st_size == 0:
        return copy.deepcopy(default)
    return json.loads(path.read_text(encoding="utf-8-sig"))


def write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8")
    temporary.replace(path)


def read_jsonl(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text(encoding="utf-8-sig").splitlines() if line.strip()]


def write_jsonl(path: Path, rows: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text("".join(json.dumps(row, ensure_ascii=False) + "\n" for row in rows), encoding="utf-8")
    temporary.replace(path)


def stable_topic_id(path: str) -> str:
    return "TOPIC_" + hashlib.sha1(normalize_topic_path(path).encode("utf-8")).hexdigest()[:12].upper()


def ensure_topic_ids(state: dict[str, Any]) -> dict[str, Any]:
    state = copy.deepcopy(state)
    by_path: dict[str, str] = {}
    for row in state.get("topics", []):
        path = normalize_topic_path(row.get("path", ""))
        if not path:
            continue
        row["path"] = path
        row["topic_id"] = str(row.get("topic_id") or stable_topic_id(path))
        by_path[path] = row["topic_id"]
    for row in state.get("topics", []):
        path = normalize_topic_path(row.get("path", ""))
        parts = [part.strip() for part in path.split(">") if part.strip()]
        parent_path = " > ".join(parts[:-1])
        row["parent_id"] = by_path.get(parent_path, "")
        row["parent_path"] = parent_path
        row["level"] = len(parts)
        row["name"] = parts[-1] if parts else row.get("name", "")
    state["schema_version"] = WORKFLOW_SCHEMA_VERSION
    return state


def public_taxonomy(state: dict[str, Any]) -> dict[str, Any]:
    allowed = set(assignable_paths(state))
    return {
        "taxonomy_name": state.get("taxonomy_name", "动态话题体系"),
        "taxonomy_version": state.get("taxonomy_version", 1),
        "topics": [
            {
                key: row.get(key, [] if key == "keywords" else "")
                for key in (
                    "topic_id", "parent_id", "path", "name", "level", "definition",
                    "keywords", "status", "scope_status",
                )
            }
            | {
                "scope_status": str(row.get("scope_status") or "IN_SCOPE"),
                "assignable": normalize_topic_path(row.get("path", "")) in allowed,
                "node_role": "leaf_topic" if normalize_topic_path(row.get("path", "")) in allowed else "category",
            }
            for row in state.get("topics", []) if row.get("status", "active") == "active"
        ],
        "assignable_topic_paths": sorted(allowed),
        "scope_policy": copy.deepcopy(state.get("scope_policy", {})),
        "hierarchy_rule": "逐级判断一级、二级、三级；每个分类结果同时提供三级标签，最终完整路径必须恰好三级。",
        "publish_mode": str(state.get("publish_mode") or "reviewed"),
        "test_only": bool(state.get("test_only", False)),
        "source_method": str(state.get("source_method") or "model_and_human_review"),
    }


def with_routing_defaults(result: dict[str, Any]) -> dict[str, Any]:
    """Add routing metadata to legacy saved results without rewriting their source files."""
    normalized = dict(result)
    decision = str(normalized.get("decision", ""))
    if decision == "NO_STABLE_TOPIC":
        normalized.setdefault("scope_status", "UNCERTAIN")
        normalized.setdefault("cluster_quality", "MIXED")
        normalized.setdefault("suggested_external_domain", "")
        normalized.setdefault("follow_up_action", "RECLUSTER")
        normalized["needs_human_review"] = True
    elif decision == "ADD_TOPIC":
        normalized.setdefault("scope_status", "IN_SCOPE")
        normalized.setdefault("cluster_quality", "COHERENT")
        normalized.setdefault("suggested_external_domain", "")
        normalized.setdefault("follow_up_action", "ADD_TOPIC_CANDIDATE")
        normalized["needs_human_review"] = True
    else:
        normalized.setdefault("scope_status", "IN_SCOPE")
        normalized.setdefault("cluster_quality", "COHERENT")
        normalized.setdefault("suggested_external_domain", "")
        normalized.setdefault("follow_up_action", "NONE")
    return normalized


def cluster_validation_identity(result: dict[str, Any] | None) -> tuple[str, str]:
    """Convert an audited cluster result into a sentence-validation stratum and route."""
    if not result:
        return "UNCERTAIN", "MANUAL_REVIEW"
    result = with_routing_defaults(result)
    scope = result.get("scope_status")
    quality = result.get("cluster_quality")
    if quality == "NOISE":
        return "NOISE", "EXCLUDE_NOISE"
    if quality == "MIXED":
        return "UNCERTAIN", "RECLUSTER"
    if scope == "OUT_OF_SCOPE":
        return "OUT_OF_SCOPE", "ROUTE_EXTERNAL_DOMAIN"
    if scope == "UNCERTAIN":
        return "UNCERTAIN", "MANUAL_REVIEW"
    if scope == "IN_SCOPE" and result.get("final_topic_paths"):
        return "IN_SCOPE_TYPICAL", "CLASSIFY"
    if scope == "IN_SCOPE":
        return "IN_SCOPE_ATYPICAL", "RETAIN_AS_ATYPICAL"
    return "UNCERTAIN", "MANUAL_REVIEW"


def sentence_validation_rows(
    classification_rows: list[dict[str, Any]], published_results: dict[str, dict[str, Any]],
) -> list[dict[str, Any]]:
    """Attach auditable validation eligibility without treating a mixed cluster as uniformly external."""
    rows: list[dict[str, Any]] = []
    for source in classification_rows:
        cluster_id = str(source.get("cluster_id", ""))
        result = published_results.get(cluster_id)
        identity, route = cluster_validation_identity(result)
        rows.append({
            **source,
            "validation_identity": identity,
            "validation_identity_label": VALIDATION_IDENTITY_LABELS[identity],
            "validation_route": route,
            "primary_metric_eligible": identity == "IN_SCOPE_TYPICAL",
            "identity_source": "published_cluster_result" if result else "missing_cluster_audit",
            "cluster_scope_status": (result or {}).get("scope_status", "UNCERTAIN"),
            "cluster_quality": (result or {}).get("cluster_quality", "MIXED"),
        })
    return rows


def sentence_scope_metrics(
    classification_rows: list[dict[str, Any]], published_results: dict[str, dict[str, Any]],
) -> dict[str, float | int]:
    """Measure primary coverage, robustness and rejection ability on separate sentence strata."""
    labeled = sentence_validation_rows(classification_rows, published_results)
    groups = {
        identity: [row for row in labeled if row["validation_identity"] == identity]
        for identity in VALIDATION_IDENTITY_LABELS
    }
    typical = groups["IN_SCOPE_TYPICAL"]
    atypical = groups["IN_SCOPE_ATYPICAL"]
    external = groups["OUT_OF_SCOPE"]
    noise = groups["NOISE"]
    uncertain = groups["UNCERTAIN"]

    def accepted(row: dict[str, Any]) -> bool:
        return bool(str(row.get("accepted_topic_path", "")).strip())

    def accepted_in_scope(row: dict[str, Any]) -> bool:
        return accepted(row) and row.get("accepted_topic_scope", "IN_SCOPE") != "OUT_OF_SCOPE"

    def accepted_external(row: dict[str, Any]) -> bool:
        return accepted(row) and row.get("accepted_topic_scope") == "OUT_OF_SCOPE"

    classified = sum(accepted_in_scope(row) for row in typical)
    unknown = sum(not accepted_in_scope(row) for row in typical)
    atypical_classified = sum(accepted_in_scope(row) for row in atypical)
    overall_classified = sum(accepted(row) for row in labeled)
    overall_unknown = len(labeled) - overall_classified
    in_scope_count = len(typical) + len(atypical)
    return {
        "sentence_coverage": classified / len(typical) if typical else 1.0,
        "unknown_rate": unknown / len(typical) if typical else 0.0,
        "atypical_sentence_coverage": atypical_classified / len(atypical) if atypical else 0.0,
        "out_of_scope_false_acceptance_rate": (
            sum(accepted_in_scope(row) for row in external) / len(external) if external else 0.0
        ),
        "external_topic_coverage": (
            sum(accepted_external(row) for row in external) / len(external) if external else 0.0
        ),
        "noise_leakage_rate": sum(accepted(row) for row in noise) / len(noise) if noise else 0.0,
        "uncertain_sentence_rate": len(uncertain) / len(labeled) if labeled else 0.0,
        "overall_sentence_coverage": overall_classified / len(labeled) if labeled else 0.0,
        "overall_unknown_rate": overall_unknown / len(labeled) if labeled else 1.0,
        "primary_sentence_count": len(typical),
        "in_scope_sentence_count": in_scope_count,
        "atypical_sentence_count": len(atypical),
        "out_of_scope_sentence_count": len(external),
        "noise_sentence_count": len(noise),
        "uncertain_sentence_count": len(uncertain),
        "excluded_sentence_count": len(external) + len(noise) + len(uncertain),
    }


def result_signature(result: dict[str, Any]) -> str:
    result = with_routing_defaults(result)
    normalized_changes = [
        {
            key: change.get(key, [] if key == "keywords" else "")
            for key in (
                "operation", "topic_id", "topic_path", "target_topic_id", "target_path",
                "new_path", "definition", "keywords", "scope_status",
            )
        }
        for change in result.get("changes", []) if change.get("operation") != "NONE"
    ]
    value = {
        "decision": result.get("decision", ""),
        "paths": sorted(normalize_topic_path(path) for path in result.get("final_topic_paths", []) if path),
        "scope_status": result.get("scope_status", ""),
        "cluster_quality": result.get("cluster_quality", ""),
        "suggested_external_domain": str(result.get("suggested_external_domain", "")).strip(),
        "follow_up_action": result.get("follow_up_action", ""),
        "changes": normalized_changes,
    }
    return json.dumps(value, ensure_ascii=False, sort_keys=True)


def validate_model_result(
    value: dict[str, Any], taxonomy: dict[str, Any] | None = None, *,
    allow_external_additions: bool = False,
) -> dict[str, Any]:
    if value.get("decision") not in {"KEEP", "ASSIGN_EXISTING", "ADD_TOPIC", "MODIFY_TOPIC", "NO_STABLE_TOPIC"}:
        raise ValueError("模型 decision 无效")
    value["final_topic_paths"] = [
        normalize_topic_path(path) for path in value.get("final_topic_paths", []) if normalize_topic_path(path)
    ]
    assignments: list[dict[str, str]] = []
    for assignment in value.get("topic_assignments", []):
        level_1 = str(assignment.get("level_1_topic", "")).strip()
        level_2 = str(assignment.get("level_2_topic", "")).strip()
        level_3 = str(assignment.get("level_3_topic", "")).strip()
        full_path = normalize_topic_path(str(assignment.get("full_path", "")))
        expected = normalize_topic_path(" > ".join((level_1, level_2, level_3)))
        if not all((level_1, level_2, level_3)) or full_path != expected:
            raise ValueError("topic_assignments 必须分别提供一致的一级、二级、三级话题和完整路径")
        assignments.append({
            "level_1_topic": level_1, "level_2_topic": level_2,
            "level_3_topic": level_3, "full_path": full_path,
        })
    if assignments:
        assignment_paths = [row["full_path"] for row in assignments]
        if value["final_topic_paths"] and value["final_topic_paths"] != assignment_paths:
            raise ValueError("topic_assignments 与 final_topic_paths 不一致")
        value["final_topic_paths"] = assignment_paths
    elif value["final_topic_paths"]:
        # Retain compatibility with existing saved model results while normalizing to the new schema.
        assignments = [{
            "level_1_topic": split_topic_path(path)[0],
            "level_2_topic": split_topic_path(path)[1],
            "level_3_topic": split_topic_path(path)[2],
            "full_path": path,
        } for path in value["final_topic_paths"] if len(split_topic_path(path)) == 3]
    value["topic_assignments"] = assignments
    invalid_paths = [path for path in value["final_topic_paths"] if len(split_topic_path(path)) != 3]
    if invalid_paths:
        raise ValueError(f"模型最终话题必须是三级叶子路径：{invalid_paths}")
    value["confidence"] = float(value.get("confidence", 0.0))
    if not 0 <= value["confidence"] <= 1:
        raise ValueError("模型 confidence 必须在0到1之间")
    scope_values = {"IN_SCOPE", "OUT_OF_SCOPE", "UNCERTAIN"}
    quality_values = {"COHERENT", "MIXED", "NOISE"}
    action_values = {
        "NONE", "ADD_TOPIC_CANDIDATE", "ROUTE_EXTERNAL_DOMAIN",
        "RECLUSTER", "MANUAL_REVIEW", "EXCLUDE_NOISE",
    }
    scope_status = str(value.get("scope_status", "")).strip().upper()
    cluster_quality = str(value.get("cluster_quality", "")).strip().upper()
    follow_up_action = str(value.get("follow_up_action", "")).strip().upper()
    if scope_status not in scope_values:
        scope_status = "UNCERTAIN" if value["decision"] == "NO_STABLE_TOPIC" else "IN_SCOPE"
    if cluster_quality not in quality_values:
        cluster_quality = "MIXED" if value["decision"] == "NO_STABLE_TOPIC" else "COHERENT"
    if follow_up_action not in action_values:
        follow_up_action = "MANUAL_REVIEW" if value["decision"] == "NO_STABLE_TOPIC" else "NONE"
    suggested_external_domain = str(value.get("suggested_external_domain", "")).strip()
    if value["decision"] == "NO_STABLE_TOPIC":
        value["final_topic_paths"] = []
        value["topic_assignments"] = []
        if cluster_quality == "NOISE":
            follow_up_action = "EXCLUDE_NOISE"
        elif cluster_quality == "MIXED":
            follow_up_action = "RECLUSTER"
        elif scope_status == "OUT_OF_SCOPE":
            follow_up_action = "ROUTE_EXTERNAL_DOMAIN"
        else:
            follow_up_action = "MANUAL_REVIEW"
        value["needs_human_review"] = True
    elif value["decision"] == "ADD_TOPIC":
        cluster_quality = "COHERENT"
        follow_up_action = "ADD_TOPIC_CANDIDATE"
        value["needs_human_review"] = True
    else:
        follow_up_action = "NONE"
    value["scope_status"] = scope_status
    value["cluster_quality"] = cluster_quality
    value["suggested_external_domain"] = suggested_external_domain
    value["follow_up_action"] = follow_up_action
    added_paths: set[str] = set()
    for change in value.get("changes", []):
        if change.get("operation") in {"ADD_TOPIC", "SPLIT_TOPIC"}:
            new_path = normalize_topic_path(change.get("new_path", ""))
            if not new_path:
                raise ValueError("ADD_TOPIC 缺少 new_path")
            if len(split_topic_path(new_path)) != 3:
                raise ValueError("新增话题的 new_path 必须恰好包含三级")
            if not str(change.get("definition", "")).strip() or len(change.get("keywords", [])) < 3:
                raise ValueError("ADD_TOPIC 必须提供说明和至少3个典型词")
            if change.get("operation") == "ADD_TOPIC":
                added_paths.add(new_path)
    if value["decision"] == "ADD_TOPIC":
        if not added_paths:
            raise ValueError("decision 为 ADD_TOPIC 时，changes 必须包含对应的 ADD_TOPIC 操作")
        missing_assignments = sorted(added_paths - set(value["final_topic_paths"]))
        if missing_assignments:
            raise ValueError(f"新增话题必须同时出现在 topic_assignments 和 final_topic_paths：{missing_assignments}")
        for change in value.get("changes", []):
            if change.get("operation") == "ADD_TOPIC":
                change["scope_status"] = scope_status
    if value["decision"] == "MODIFY_TOPIC":
        editable = [
            change for change in value.get("changes", [])
            if change.get("operation") in {"EDIT_DEFINITION", "EDIT_KEYWORDS", "EDIT_SCOPE"}
        ]
        if not editable:
            raise ValueError("MODIFY_TOPIC 必须包含 EDIT_DEFINITION 或 EDIT_KEYWORDS")
        for change in editable:
            topic_path = normalize_topic_path(change.get("topic_path", ""))
            if not topic_path:
                raise ValueError("修改话题说明或核心词时必须提供 topic_path")
            if change.get("operation") == "EDIT_DEFINITION" and not str(change.get("definition", "")).strip():
                raise ValueError("修改话题说明时 definition 不能为空")
            if change.get("operation") == "EDIT_KEYWORDS" and len(change.get("keywords", [])) < 3:
                raise ValueError("修改话题核心词时至少提供3个关键词")
            if change.get("operation") == "EDIT_SCOPE" and change.get("scope_status") not in {"IN_SCOPE", "OUT_OF_SCOPE"}:
                raise ValueError("修改话题范围时 scope_status 必须为 IN_SCOPE 或 OUT_OF_SCOPE")
    if taxonomy is not None:
        scope_policy = dict(taxonomy.get("scope_policy") or {})
        if scope_policy.get("restrict_new_level_1"):
            allowed_roots = {
                str(name).strip() for name in scope_policy.get("allowed_level_1_topics", [])
                if str(name).strip()
            }
            scoped_paths = [
                normalize_topic_path(change.get("new_path", ""))
                for change in value.get("changes", [])
                if change.get("operation") in {"ADD_TOPIC", "SPLIT_TOPIC"}
            ]
            outside = sorted({
                split_topic_path(path)[0] for path in scoped_paths
                if split_topic_path(path) and split_topic_path(path)[0] not in allowed_roots
            })
            if outside and not (allow_external_additions and scope_status == "OUT_OF_SCOPE"):
                raise ValueError(
                    f"当前体系限制新增一级话题，只允许 {sorted(allowed_roots)}；"
                    f"体系外领域应使用 NO_STABLE_TOPIC 与 ROUTE_EXTERNAL_DOMAIN：{outside}"
                )
        allowed = set(assignable_paths(taxonomy))
        allowed.update(
            normalize_topic_path(change.get("new_path", ""))
            for change in value.get("changes", [])
            if change.get("operation") in {"ADD_TOPIC", "SPLIT_TOPIC"}
            and normalize_topic_path(change.get("new_path", ""))
        )
        unknown = [path for path in value["final_topic_paths"] if path not in allowed]
        if unknown:
            raise ValueError(f"模型最终话题不是体系中的可标注叶子，也没有对应新增操作：{unknown}")
        if value["decision"] != "ADD_TOPIC" and value["final_topic_paths"]:
            scope_edits = [
                change.get("scope_status") for change in value.get("changes", [])
                if change.get("operation") == "EDIT_SCOPE"
                and change.get("scope_status") in {"IN_SCOPE", "OUT_OF_SCOPE"}
            ]
            scope_by_path = {
                normalize_topic_path(row.get("path", "")): str(row.get("scope_status") or "IN_SCOPE")
                for row in taxonomy.get("topics", [])
            }
            selected_scopes = {
                scope_by_path[path] for path in value["final_topic_paths"] if path in scope_by_path
            }
            if scope_edits:
                value["scope_status"] = scope_edits[-1]
            elif selected_scopes == {"OUT_OF_SCOPE"}:
                value["scope_status"] = "OUT_OF_SCOPE"
            elif selected_scopes == {"IN_SCOPE"}:
                value["scope_status"] = "IN_SCOPE"
            elif len(selected_scopes) > 1:
                value["scope_status"] = "UNCERTAIN"
    return value


def apply_changes(state: dict[str, Any], changes: list[dict[str, Any]], actor: str) -> dict[str, Any]:
    state = ensure_topic_ids(state)

    def find(change: dict[str, Any]) -> dict[str, Any] | None:
        topic_id = str(change.get("topic_id", ""))
        path = normalize_topic_path(change.get("topic_path", ""))
        return next((row for row in state.get("topics", []) if row.get("topic_id") == topic_id or (path and row.get("path") == path)), None)

    changed = False
    for change in changes:
        operation = change.get("operation")
        if operation in {"NONE", ""}:
            continue
        if operation in {"ADD_TOPIC", "SPLIT_TOPIC"}:
            path = normalize_topic_path(change.get("new_path", ""))
            if not path:
                continue
            added = add_topic_path(
                state, path, source="model_working_branch", created_by=actor,
                definition=str(change.get("definition", "")), keywords=change.get("keywords", []),
                change_reason=str(change.get("reason") or operation),
                scope_status=str(change.get("scope_status") or "IN_SCOPE"),
            )
            state = ensure_topic_ids(state)
            # Several clusters may support the same canonical topic.  The topic
            # store deliberately leaves an existing path untouched, so enrich it
            # here instead of silently dropping later review metadata.
            if not added:
                row = next(
                    (item for item in state.get("topics", []) if item.get("path") == path),
                    None,
                )
                if row is not None:
                    incoming_scope = str(change.get("scope_status") or "IN_SCOPE")
                    incoming_definition = str(change.get("definition", "")).strip()
                    incoming_keywords = {
                        str(word).strip()
                        for word in change.get("keywords", [])
                        if str(word).strip()
                    }
                    merged_keywords = sorted({
                        str(word).strip()
                        for word in row.get("keywords", [])
                        if str(word).strip()
                    } | incoming_keywords)
                    if incoming_definition and row.get("definition", "") != incoming_definition:
                        row["definition"] = incoming_definition
                        changed = True
                    if row.get("keywords", []) != merged_keywords:
                        row["keywords"] = merged_keywords
                        changed = True
                    if row.get("scope_status", "IN_SCOPE") != incoming_scope:
                        row["scope_status"] = incoming_scope
                        changed = True
            else:
                changed = True
            continue
        row = find(change)
        if row is None:
            continue
        if operation == "EDIT_DEFINITION":
            row["definition"] = str(change.get("definition", "")).strip()
        elif operation == "EDIT_KEYWORDS":
            row["keywords"] = sorted({str(word).strip() for word in change.get("keywords", []) if str(word).strip()})
        elif operation == "EDIT_SCOPE":
            row["scope_status"] = str(change.get("scope_status") or "IN_SCOPE")
        elif operation in {"RENAME_TOPIC", "MOVE_TOPIC"}:
            old_path = row["path"]
            new_path = normalize_topic_path(change.get("new_path", ""))
            if not new_path:
                continue
            for candidate in state.get("topics", []):
                if candidate.get("path") == old_path or str(candidate.get("path", "")).startswith(old_path + " > "):
                    candidate["path"] = new_path + str(candidate["path"])[len(old_path):]
            state = ensure_topic_ids(state)
        elif operation == "MERGE_TOPICS":
            target_id = str(change.get("target_topic_id", ""))
            target_path = normalize_topic_path(change.get("target_path", ""))
            target = next((item for item in state.get("topics", []) if item.get("topic_id") == target_id or (target_path and item.get("path") == target_path)), None)
            if target:
                row["status"] = "merged"
                row["merged_into"] = target["topic_id"]
        changed = True
    if changed:
        state["taxonomy_version"] = int(state.get("taxonomy_version", 1)) + 1
        state["updated_at"] = now_iso()
    return ensure_topic_ids(state)


class WorkflowManager:
    def __init__(self, root: Path, results: Path, canonical_taxonomy: Path) -> None:
        self.root = root.resolve()
        self.results = results.resolve()
        self.workflow_root = self.results / "workflow"
        self.rounds_root = self.workflow_root / "rounds"
        self.upload_root = self.workflow_root / "uploads"
        self.snapshots_root = self.workflow_root / "taxonomy_snapshots"
        self.registry_path = self.workflow_root / "sample_registry.json"
        self.jobs_path = self.workflow_root / "jobs.json"
        self.settings_path = self.root / "configs" / "workflow.json"
        self.canonical_taxonomy = canonical_taxonomy.resolve()
        self.lock = threading.RLock()
        loaded_jobs = read_json(self.jobs_path, []) or []
        self.jobs: dict[str, dict[str, Any]] = {}
        for row in loaded_jobs:
            if row.get("status") in {"queued", "running", "cancelling"}:
                row["status"] = "interrupted"
                row["message"] = "服务重启，任务已中断；已保存断点，可继续运行"
                row["finished_at"] = now_iso()
            if row.get("job_id"):
                self.jobs[row["job_id"]] = row
        for path in (self.rounds_root, self.upload_root, self.snapshots_root):
            path.mkdir(parents=True, exist_ok=True)
        if not self.canonical_taxonomy.exists():
            write_taxonomy(self.canonical_taxonomy, empty_taxonomy())
        state, _ = normalize_three_level_taxonomy(read_taxonomy(self.canonical_taxonomy))
        state = ensure_topic_ids(state)
        write_taxonomy(self.canonical_taxonomy, state)
        if not self.settings_path.exists():
            write_json(self.settings_path, {
                "schema_version": WORKFLOW_SCHEMA_VERSION,
                "sampling": {"sample_size": 800, "anchor_ratio": 0.30, "seed": 20260817},
                "review": {"agreement_audit_ratio": 0.10, "low_confidence_threshold": 0.75},
                "gates": DEFAULT_GATES,
            })
        self._migrate_legacy_results()
        self._normalize_managed_taxonomies()
        self._repair_anchor_registry()
        self._reconcile_interrupted_jobs()
        self._reconcile_published_decision_changes()
        self._persist_jobs()

    def _reconcile_published_decision_changes(self) -> None:
        """Mark legacy published rounds dirty when decisions were edited after publication."""
        for round_dir in self.rounds_root.iterdir():
            if not round_dir.is_dir():
                continue
            state = read_json(round_dir / "round.json", {}) or {}
            decisions = round_dir / "human_decisions.jsonl"
            published = round_dir / "published_changes.json"
            if not state.get("published") or not decisions.exists() or not published.exists():
                continue
            if decisions.stat().st_mtime <= published.stat().st_mtime:
                continue
            state.update(
                status="human_review", stage=3, published=False, revision_pending=True,
                last_published_taxonomy_version=state.get("published_taxonomy_version"),
            )
            state.pop("metrics", None)
            self.write_round(state)

    def _reconcile_interrupted_jobs(self) -> None:
        for job in self.jobs.values():
            if job.get("status") != "interrupted" or not job.get("round_id"):
                continue
            try:
                round_state = self.read_round(job["round_id"])
            except ValueError:
                continue
            if job.get("kind") == "model_review":
                for model in round_state.get("models", []):
                    if model.get("model_id") == job.get("model_id") and model.get("status") in {"running", "cancelling"}:
                        model["status"] = "interrupted"
                        model["resumable"] = True
                round_state["status"] = "model_review"
            elif job.get("kind") == "clustering" and round_state.get("status") == "clustering":
                round_state["status"] = "sample_ready"
            elif job.get("kind") == "validation" and round_state.get("status") == "validating":
                round_state["status"] = "published"
            self.write_round(round_state)

    def _normalize_managed_taxonomies(self) -> None:
        """Keep existing round baselines and resumable model branches on the canonical hierarchy policy."""
        for round_path in self.rounds_root.iterdir():
            if not round_path.is_dir():
                continue
            candidates = [round_path / "baseline_taxonomy.json"]
            model_root = round_path / "models"
            if model_root.exists():
                candidates.extend(model_root.glob("*/working_taxonomy.json"))
            for path in candidates:
                if not path.exists():
                    continue
                source_state = read_taxonomy(path)
                had_embedded_log = "change_log" in source_state
                state, changed = normalize_three_level_taxonomy(source_state)
                if changed or had_embedded_log:
                    write_taxonomy(path, ensure_topic_ids(state))
            self._normalize_cluster_task_hierarchy(round_path / "cluster_tasks.jsonl")

    @staticmethod
    def _normalize_cluster_task_hierarchy(path: Path) -> None:
        if not path.exists():
            return
        tasks = read_jsonl(path)
        changed = False
        for task in tasks:
            normalized_words: list[dict[str, Any]] = []
            seen_words: set[str] = set()
            for source_word in task.get("representative_words", []):
                raw_term = source_word.get("term", "") if isinstance(source_word, dict) else str(source_word)
                term = normalize_representative_term(raw_term)
                if not term or term in seen_words:
                    changed = True
                    continue
                seen_words.add(term)
                normalized_words.append({
                    "term": term,
                    "score": source_word.get("score", 0) if isinstance(source_word, dict) else 0,
                })
                if term != raw_term or not isinstance(source_word, dict) or "raw_term" in source_word:
                    changed = True
            task["representative_words"] = normalized_words
            hierarchy_by_path: dict[str, dict[str, Any]] = {
                str(row.get("path")): row for row in task.get("hierarchy_candidates", []) if row.get("path")
            }
            for field in ("reference_candidates", "machine_matches"):
                retained = []
                for source in task.get(field, []):
                    candidate = copy.deepcopy(source)
                    old_path = normalize_topic_path(candidate.get("path", ""))
                    mapped_path = BOUNDARY_PATH_MIGRATIONS.get(old_path, old_path)
                    if mapped_path != old_path:
                        candidate["path"] = mapped_path
                        candidate["depth"] = 2
                        candidate["hierarchy_migrated_from"] = old_path
                        retained.append(candidate)
                        changed = True
                    elif len(split_topic_path(old_path)) == 3:
                        retained.append(candidate)
                    else:
                        evidence = hierarchy_by_path.setdefault(old_path, {
                            **candidate, "path": old_path,
                            "level": len(split_topic_path(old_path)),
                            "candidate_role": "hierarchy_evidence",
                            "source_fields": [],
                        })
                        if field not in evidence["source_fields"]:
                            evidence["source_fields"].append(field)
                        changed = True
                task[field] = retained
            hierarchy = sorted(
                hierarchy_by_path.values(),
                key=lambda row: (-float(row.get("score", 0)), int(row.get("level", 0)), row.get("path", "")),
            )
            task["hierarchy_candidates"] = hierarchy
            matches = task.get("machine_matches", [])
            task["mapping_status"] = (
                "matched_multiple" if len(matches) > 1 else "matched_single" if matches else "unmatched"
            )
        if changed:
            write_jsonl(path, tasks)

    def _persist_jobs(self) -> None:
        with self.lock:
            rows = sorted(self.jobs.values(), key=lambda row: row.get("created_at", ""), reverse=True)[:200]
            write_json(self.jobs_path, rows)

    def _update_job(self, job_id: str, **values: Any) -> dict[str, Any]:
        with self.lock:
            self.jobs[job_id].update(values)
            self._persist_jobs()
            return copy.deepcopy(self.jobs[job_id])

    def _migrate_legacy_results(self) -> None:
        """Promote one completed legacy clustering run into the first managed round."""
        if any(path.is_dir() for path in self.rounds_root.iterdir()):
            return
        legacy_summary = self.results / "demo_topic_summary.json"
        legacy_sample = self.results / "sentences_topic_sample_800.jsonl"
        if not legacy_summary.exists() or not legacy_sample.exists():
            return
        round_id = "R0001"
        round_dir = self.round_dir(round_id)
        clustering_dir = round_dir / "clustering"
        clustering_dir.mkdir(parents=True)
        shutil.copy2(legacy_sample, round_dir / "sample.jsonl")
        for path in self.results.glob("demo_*"):
            if path.is_file() and path.name not in {
                "demo_human_annotations.csv", "demo_llm_annotations.jsonl", "demo_llm_batch_jobs.json",
            }:
                shutil.copy2(path, clustering_dir / path.name)
        baseline = ensure_topic_ids(read_taxonomy(self.canonical_taxonomy))
        write_taxonomy(round_dir / "baseline_taxonomy.json", baseline)
        tasks = self._consolidate_cluster_tasks(clustering_dir)
        write_jsonl(round_dir / "cluster_tasks.jsonl", tasks)
        self._prune_clustering_outputs(clustering_dir)
        rows = read_jsonl(round_dir / "sample.jsonl")
        anchor_target = int(round(len(rows) * 0.30))
        anchor_units = [
            {"unit_id": str(row.get("sentence_id", "")), "doc_id": str(row.get("doc_id", ""))}
            for row in rows if row.get("sentence_id") and row.get("doc_id")
        ]
        selected_anchor, _ = sample_units_by_doc(
            anchor_units, anchor_target, strategy="balanced_systematic", seed=20260817
        )
        anchor_ids = [row["unit_id"] for row in selected_anchor]
        registry = {
            "schema_version": 2,
            "anchor_ids": anchor_ids,
            "usage": {str(row.get("sentence_id", "")): 1 for row in rows if row.get("sentence_id")},
            "rounds": {round_id: [str(row.get("sentence_id", "")) for row in rows]},
        }
        write_json(self.registry_path, registry)
        state = {
            "schema_version": WORKFLOW_SCHEMA_VERSION, "round_id": round_id,
            "status": "clustered", "stage": 2, "created_at": now_iso(), "updated_at": now_iso(),
            "source_path": str(legacy_sample), "sample_path": str(round_dir / "sample.jsonl"),
            "sample_size": len(rows), "anchor_count": anchor_target,
            "exploration_count": len(rows) - anchor_target, "excluded_count": 0,
            "documents_represented": len({row.get("doc_id") for row in rows}), "seed": 20260817,
            "use_initial_taxonomy": True, "baseline_taxonomy_version": baseline.get("taxonomy_version", 1),
            "cluster_count": len(tasks), "models": [], "published": False,
            "migration_note": "由 results 中已完成的旧版聚类结果自动迁移",
        }
        self.write_round(state)

    def _repair_anchor_registry(self) -> None:
        registry = read_json(self.registry_path, {}) or {}
        if int(registry.get("schema_version", 0)) >= 2:
            return
        rounds = sorted(self.list_rounds(), key=lambda row: row["round_id"])
        if not rounds:
            return
        first = rounds[0]
        sample_path = self.round_dir(first["round_id"]) / "sample.jsonl"
        rows = read_jsonl(sample_path)
        anchor_target = int(first.get("anchor_count") or round(len(rows) * 0.30))
        units = [
            {"unit_id": str(row.get("sentence_id", "")), "doc_id": str(row.get("doc_id", ""))}
            for row in rows if row.get("sentence_id") and row.get("doc_id")
        ]
        anchors, _ = sample_units_by_doc(
            units, anchor_target, strategy="balanced_systematic", seed=int(first.get("seed", 20260817))
        )
        anchor_ids = {row["unit_id"] for row in anchors}
        for row in rows:
            sampling = dict(row.get("topic_sampling") or {})
            sampling.update({
                "round_id": first["round_id"],
                "sample_role": "anchor" if row.get("sentence_id") in anchor_ids else "exploration",
            })
            row["topic_sampling"] = sampling
        write_jsonl(sample_path, rows)
        registry["schema_version"] = 2
        registry["anchor_ids"] = sorted(anchor_ids)
        registry.setdefault("usage", {}).update({
            str(row.get("sentence_id")): max(1, int(registry.get("usage", {}).get(str(row.get("sentence_id")), 0)))
            for row in rows if row.get("sentence_id")
        })
        registry.setdefault("rounds", {})[first["round_id"]] = [
            str(row.get("sentence_id")) for row in rows if row.get("sentence_id")
        ]
        write_json(self.registry_path, registry)

    def round_dir(self, round_id: str) -> Path:
        if not re_fullmatch_round(round_id):
            raise ValueError("round_id 格式无效")
        return self.rounds_root / round_id

    def read_round(self, round_id: str) -> dict[str, Any]:
        value = read_json(self.round_dir(round_id) / "round.json")
        if not value:
            raise ValueError(f"轮次不存在：{round_id}")
        return value

    def write_round(self, value: dict[str, Any]) -> None:
        value["updated_at"] = now_iso()
        write_json(self.round_dir(value["round_id"]) / "round.json", value)

    def list_rounds(self) -> list[dict[str, Any]]:
        rows = [read_json(path / "round.json") for path in self.rounds_root.iterdir() if path.is_dir()]
        return sorted((row for row in rows if row), key=lambda row: row["round_id"], reverse=True)

    def bootstrap(self) -> dict[str, Any]:
        settings = read_json(self.settings_path, {"gates": DEFAULT_GATES})
        gates = {**DEFAULT_GATES, **settings.get("gates", {})}
        return {
            "schema_version": WORKFLOW_SCHEMA_VERSION,
            "rounds": self.list_rounds(),
            "canonical_taxonomy": public_taxonomy(ensure_topic_ids(read_taxonomy(self.canonical_taxonomy))),
            "gates": gates,
            "execution": settings.get("execution", {}),
            "jobs": list(self.jobs.values()),
            "model_prompt_defaults": {
                "system_prompt": MODEL_SYSTEM_PROMPT,
                "user_prompt_template": MODEL_USER_PROMPT,
            },
        }

    def _python_executable(self, requested: str = "", round_state: dict[str, Any] | None = None) -> str:
        settings = read_json(self.settings_path, {}) or {}
        configured = str((settings.get("execution") or {}).get("python_executable", "")).strip()
        remembered = str((round_state or {}).get("clustering_python_executable", "")).strip()
        executable = requested.strip() or remembered or configured or sys.executable
        path = Path(executable)
        if path.is_absolute() and not path.exists():
            raise ValueError(f"配置的 Python 不存在：{path}")
        return executable

    def save_upload(self, filename: str, content: bytes) -> Path:
        safe_name = Path(filename).name
        if Path(safe_name).suffix.lower() != ".jsonl":
            raise ValueError("语料文件必须为 JSONL")
        path = self.upload_root / f"{datetime.now():%Y%m%d_%H%M%S}_{safe_name}"
        path.write_bytes(content)
        return path

    def _next_round_id(self) -> str:
        numbers = [int(row["round_id"][1:]) for row in self.list_rounds() if re_fullmatch_round(row["round_id"])]
        return f"R{max(numbers, default=0) + 1:04d}"

    def create_round(
        self, source_path: Path, *, sample_size: int = 800, anchor_ratio: float = 0.30,
        seed: int = 20260817, use_initial_taxonomy: bool = True,
    ) -> dict[str, Any]:
        source_path = source_path.resolve()
        if not source_path.exists():
            raise ValueError(f"语料文件不存在：{source_path}")
        if not 0 <= anchor_ratio < 1:
            raise ValueError("anchor_ratio 必须在0到1之间")
        config = load_bertopic_config(self.root / "configs" / "bertopic.json")
        input_config = config["input"]
        units, excluded, policy = build_units(
            source_path, int(input_config["min_analysis_characters"]),
            int(input_config["max_analysis_characters"]),
            input_config["structural_noise_exact"] if input_config["exclude_structural_noise"] else [],
            input_config["structural_noise_regex"] if input_config["exclude_structural_noise"] else [],
        )
        if policy != "sentence_level":
            raise ValueError("轮次语料必须包含 sentence_id")
        if len(units) < sample_size:
            raise ValueError(f"有效句子只有 {len(units)} 条，无法抽取 {sample_size} 条")
        registry = read_json(self.registry_path, {"anchor_ids": [], "usage": {}, "rounds": {}})
        by_id = {row["unit_id"]: row for row in units}
        anchor_target = int(round(sample_size * anchor_ratio))
        anchor_ids = [unit_id for unit_id in registry.get("anchor_ids", []) if unit_id in by_id]
        if len(anchor_ids) < anchor_target:
            needed = anchor_target - len(anchor_ids)
            pool = [row for row in units if row["unit_id"] not in set(anchor_ids)]
            selected, _ = sample_units_by_doc(pool, needed, strategy="balanced_systematic", seed=seed)
            anchor_ids.extend(row["unit_id"] for row in selected)
        anchor_ids = anchor_ids[:anchor_target]
        anchor_set = set(anchor_ids)
        usage = {str(key): int(value) for key, value in registry.get("usage", {}).items()}
        exploration_target = sample_size - len(anchor_ids)
        unused = [row for row in units if row["unit_id"] not in anchor_set and usage.get(row["unit_id"], 0) == 0]
        pool = unused
        if len(pool) < exploration_target:
            pool = sorted(
                (row for row in units if row["unit_id"] not in anchor_set),
                key=lambda row: (usage.get(row["unit_id"], 0), row["unit_id"]),
            )
        exploration, audit = sample_units_by_doc(
            pool, exploration_target, strategy="balanced_systematic", seed=seed + len(self.list_rounds())
        )
        selected_ids = set(anchor_ids) | {row["unit_id"] for row in exploration}
        sampled = [row for row in units if row["unit_id"] in selected_ids]
        if len(sampled) != sample_size:
            raise RuntimeError("锚点与探索样本合并后数量不正确")
        round_id = self._next_round_id()
        round_dir = self.round_dir(round_id)
        round_dir.mkdir(parents=True)
        sample_path = round_dir / "sample.jsonl"
        sample_rows = []
        for rank, row in enumerate(sampled, 1):
            sample_rows.append({
                "doc_id": row["doc_id"], "sentence_id": row["unit_id"],
                "sentence_index": row.get("sentence_index", ""),
                "paragraph_id": row.get("paragraph_id", ""), "section_id": row.get("section_id", ""),
                "section_path": row.get("heading", ""), "is_heading": False, "text": row["analysis_text"],
                "topic_sampling": {
                    "round_id": round_id, "rank": rank,
                    "sample_role": "anchor" if row["unit_id"] in anchor_set else "exploration",
                    "seed": seed,
                },
            })
            usage[row["unit_id"]] = usage.get(row["unit_id"], 0) + 1
        write_jsonl(sample_path, sample_rows)
        registry["anchor_ids"] = anchor_ids
        registry["usage"] = usage
        registry.setdefault("rounds", {})[round_id] = list(selected_ids)
        write_json(self.registry_path, registry)
        baseline = ensure_topic_ids(
            read_taxonomy(self.canonical_taxonomy)
            if use_initial_taxonomy else empty_taxonomy("动态话题体系")
        )
        baseline_path = round_dir / "baseline_taxonomy.json"
        write_taxonomy(baseline_path, baseline)
        round_state = {
            "schema_version": WORKFLOW_SCHEMA_VERSION,
            "round_id": round_id, "status": "sample_ready", "stage": 1,
            "created_at": now_iso(), "source_path": str(source_path), "sample_path": str(sample_path),
            "source_sha256": hashlib.sha256(source_path.read_bytes()).hexdigest(),
            "source_unit_count": len(units), "sampling_fraction": sample_size / len(units),
            "sample_size": sample_size, "anchor_count": len(anchor_ids),
            "exploration_count": exploration_target, "excluded_count": len(excluded),
            "documents_represented": audit["documents_represented"], "seed": seed,
            "use_initial_taxonomy": bool(use_initial_taxonomy),
            "baseline_taxonomy_version": baseline.get("taxonomy_version", 1),
            "models": [], "published": False,
        }
        self.write_round(round_state)
        return round_state

    def _new_job(self, kind: str, round_id: str, extra: dict[str, Any] | None = None) -> dict[str, Any]:
        job_id = uuid.uuid4().hex
        job = {
            "job_id": job_id, "kind": kind, "round_id": round_id, "status": "queued",
            "created_at": now_iso(), "completed": 0, "total": 0, "message": "",
            **(extra or {}),
        }
        self.jobs[job_id] = job
        self._persist_jobs()
        return job

    def start_clustering(self, round_id: str, python_executable: str = "") -> dict[str, Any]:
        # Reserve the job and round before starting the thread. All tabs and
        # concurrent requests in this project's worker share this lock.
        with self.lock:
            round_state = self.read_round(round_id)
            active = [job for job in self.jobs.values() if job.get("kind") == "clustering" and job.get("status") in {"queued", "running", "paused"}]
            for job in active:
                if job.get("round_id") == round_id:
                    return copy.deepcopy(job)
            if active:
                raise ValueError("已有聚类任务正在运行，请等待完成，不能重复启动其他轮次")
            completed = [job for job in self.jobs.values() if job.get("kind") == "clustering" and job.get("round_id") == round_id and job.get("status") == "completed"]
            if completed and round_state.get("cluster_count", 0):
                return copy.deepcopy(completed[-1])
            if round_state.get("published") or round_state.get("cluster_count", 0):
                raise ValueError("本轮聚类已完成，请使用已有结果；重新聚类请创建新轮次")
            executable = self._python_executable(python_executable, round_state)
            job = self._new_job("clustering", round_id, {"python_executable": executable})
            round_state.update(status="clustering", stage=1, clustering_python_executable=executable)
            self.write_round(round_state)
            try:
                threading.Thread(target=self._run_clustering, args=(job["job_id"], executable), daemon=True).start()
            except Exception:
                self._update_job(job["job_id"], status="failed", finished_at=now_iso(), message="聚类线程启动失败")
                round_state["status"] = "clustering_failed"
                self.write_round(round_state)
                raise
            return copy.deepcopy(job)

    def _run_clustering(self, job_id: str, executable: str) -> None:
        job = self.jobs[job_id]
        round_id = job["round_id"]
        round_dir = self.round_dir(round_id)
        output = round_dir / "clustering"
        output.mkdir(parents=True, exist_ok=True)
        round_state = self.read_round(round_id)
        taxonomy_path = self.root / "configs" / "new_energy_candidate_taxonomy.json"
        if not round_state.get("use_initial_taxonomy", True):
            taxonomy_path = round_dir / "empty_reference_taxonomy.json"
            write_json(taxonomy_path, {})
        command = [
            executable, str(self.root / "run_topic_demo.py"), "--stage", "taxonomy_build",
            "--sections", round_state["sample_path"], "--sample-size", "0", "--taxonomy", str(taxonomy_path),
            "--dynamic-taxonomy", str(round_dir / "baseline_taxonomy.json"),
            "--term-table", str(self.root / "configs" / "topic_seed_terms.csv"),
            "--config", str(self.root / "configs" / "bertopic.json"), "--output-root", str(output),
        ]
        self._update_job(job_id, status="running", started_at=now_iso(), message="正在运行 BERTopic")
        log_path = round_dir / "clustering.log"
        try:
            flags = subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0
            with log_path.open("w", encoding="utf-8") as log:
                result = subprocess.run(command, cwd=self.root, stdout=log, stderr=subprocess.STDOUT, creationflags=flags)
            if result.returncode:
                raise RuntimeError(f"聚类进程退出码 {result.returncode}，请查看 {log_path.name}")
            tasks = self._consolidate_cluster_tasks(output)
            write_jsonl(round_dir / "cluster_tasks.jsonl", tasks)
            self._prune_clustering_outputs(output)
            round_state = self.read_round(round_id)
            round_state.update(status="clustered", stage=2, cluster_count=len(tasks))
            self.write_round(round_state)
            self._update_job(job_id, status="completed", completed=len(tasks), total=len(tasks), finished_at=now_iso(), message="聚类完成")
        except Exception as error:  # noqa: BLE001
            round_state = self.read_round(round_id)
            round_state["status"] = "clustering_failed"
            self.write_round(round_state)
            self._update_job(job_id, status="failed", finished_at=now_iso(), message=str(error))

    def _consolidate_cluster_tasks(self, output: Path) -> list[dict[str, Any]]:
        summaries = read_json(output / "demo_topic_summary.json", {"topics": []}).get("topics", [])
        tasks = []
        for summary in summaries:
            candidates = summary.get("mapping_candidates", [])[:8]
            representative_words = [
                {"term": word.get("term", ""), "score": word.get("score", 0)}
                if isinstance(word, dict) else {"term": str(word), "score": 0}
                for word in summary.get("representative_words", [])
            ]
            tasks.append({
                "round_id": output.parent.name,
                "cluster_id": summary.get("cluster_id", ""),
                "unit_count": summary.get("unit_count", 0),
                "document_count": summary.get("document_count", 0),
                # raw_term is intentionally omitted so web/LLM inputs only see normalized terms.
                "representative_words": representative_words,
                "representative_texts": summary.get("representative_texts", []),
                "center_samples": summary.get("center_samples", []),
                "boundary_samples": summary.get("boundary_samples", []),
                "reference_candidates": candidates,
                "machine_matches": summary.get("matched_topics", []),
                "mapping_status": summary.get("mapping_status", ""),
                "keyword_coverage_rate": summary.get("keyword_coverage_rate", 0),
            })
        return sorted(
            tasks,
            key=lambda row: (
                row["cluster_id"] == "BT_OUTLIER",
                -float(row.get("keyword_coverage_rate", 0)), -int(row.get("unit_count", 0)),
            ),
        )

    def _prune_clustering_outputs(self, output: Path) -> None:
        keep = {
            "demo_run_report.json", "demo_sampling_report.json", "demo_topic_quality.json",
            "demo_topic_summary.json", "demo_unit_assignments.jsonl", "demo_sampled_units.jsonl",
            "demo_term_topic_evidence.csv", "demo_exclusion_audit.csv",
        }
        for path in output.iterdir():
            if path.is_file() and path.name not in keep:
                path.unlink()

    def cluster_tasks(self, round_id: str) -> list[dict[str, Any]]:
        return read_jsonl(self.round_dir(round_id) / "cluster_tasks.jsonl")

    def round_sample(self, round_id: str) -> list[dict[str, Any]]:
        return read_jsonl(self.round_dir(round_id) / "sample.jsonl")

    def _model_paths(self, round_id: str, model_id: str) -> dict[str, Path]:
        model_dir = self.round_dir(round_id) / "models" / model_id
        return {
            "dir": model_dir,
            "branch": model_dir / "working_taxonomy.json",
            "annotations": model_dir / "annotations.jsonl",
            "attempts": model_dir / "attempts.jsonl",
            "checkpoint": model_dir / "checkpoint.json",
            "settings": model_dir / "settings.json",
        }

    def _successful_model_results(self, round_id: str, model_id: str) -> dict[str, dict[str, Any]]:
        paths = self._model_paths(round_id, model_id)
        return {
            str(row.get("cluster_id")): row for row in read_jsonl(paths["annotations"])
            if row.get("cluster_id") and not row.get("error")
        }

    def _rebuild_model_branch(self, round_id: str, model_id: str) -> dict[str, Any]:
        """Rebuild a branch from committed successes so a mid-write crash is harmless."""
        paths = self._model_paths(round_id, model_id)
        taxonomy = ensure_topic_ids(read_taxonomy(self.round_dir(round_id) / "baseline_taxonomy.json"))
        successes = self._successful_model_results(round_id, model_id)
        for task in self.cluster_tasks(round_id):
            result = successes.get(str(task.get("cluster_id")))
            if result:
                taxonomy = apply_changes(taxonomy, result.get("changes", []), f"model:{model_id}")
        write_taxonomy(paths["branch"], taxonomy)
        return taxonomy

    def _write_model_checkpoint(
        self, round_id: str, model_id: str, tasks: list[dict[str, Any]],
        results: dict[str, dict[str, Any]], status: str, job_id: str,
    ) -> dict[str, Any]:
        task_ids = [str(task.get("cluster_id")) for task in tasks]
        successful = [cluster_id for cluster_id in task_ids if cluster_id in results and not results[cluster_id].get("error")]
        failed = [cluster_id for cluster_id in task_ids if cluster_id in results and results[cluster_id].get("error")]
        pending = [cluster_id for cluster_id in task_ids if cluster_id not in successful]
        checkpoint = {
            "schema_version": WORKFLOW_SCHEMA_VERSION, "round_id": round_id, "model_id": model_id,
            "job_id": job_id, "status": status, "total": len(task_ids),
            "successful_count": len(successful), "failed_count": len(failed),
            "successful_cluster_ids": successful, "failed_cluster_ids": failed,
            "pending_cluster_ids": pending, "next_cluster_id": pending[0] if pending else "",
            "heartbeat_at": now_iso(),
        }
        write_json(self._model_paths(round_id, model_id)["checkpoint"], checkpoint)
        return checkpoint

    def _active_model_job(self, round_id: str, model_id: str) -> dict[str, Any] | None:
        active = [
            row for row in self.jobs.values()
            if row.get("kind") == "model_review" and row.get("round_id") == round_id
            and row.get("model_id") == model_id and row.get("status") in {"queued", "running", "cancelling"}
        ]
        return max(active, key=lambda row: row.get("created_at", ""), default=None)

    def start_model(
        self, round_id: str, model_id: str, settings: dict[str, Any], api_key: str,
        *, resume: bool = False,
    ) -> dict[str, Any]:
        model_id = sanitize_identifier(model_id)
        if not model_id:
            raise ValueError("model_id 不能为空")
        round_state = self.read_round(round_id)
        if round_state.get("status") not in {"clustered", "model_review", "models_completed", "human_review"}:
            raise ValueError("必须先完成本轮聚类")
        if self._active_model_job(round_id, model_id):
            raise ValueError("该模型分支正在运行，不能重复启动")
        paths = self._model_paths(round_id, model_id)
        existing = read_jsonl(paths["annotations"])
        if resume and not existing and not paths["checkpoint"].exists():
            raise ValueError("该模型分支没有可恢复的断点")
        if resume:
            saved_settings = read_json(paths["settings"], {})
            if saved_settings:
                requested_settings = settings
                settings = saved_settings
                # Allow safe runtime tuning after timeouts/empty outputs without changing the branch model or prompts.
                for key in ("structured_output", "timeout_seconds", "max_output_tokens"):
                    if key in requested_settings and requested_settings[key] not in {None, ""}:
                        settings[key] = requested_settings[key]
        if not resume and existing:
            raise ValueError("该模型分支已有结果；请使用“继续运行”，或更换模型分支ID")
        validate_config(settings)
        if not api_key:
            raise ValueError("必须为该模型提供 API Key")
        job = self._new_job("model_review", round_id, {"model_id": model_id, "resume": resume})
        models = [row for row in round_state.get("models", []) if row.get("model_id") != model_id]
        successes = self._successful_model_results(round_id, model_id) if resume else {}
        models.append({
            "model_id": model_id, "model": settings.get("model", ""), "status": "running",
            "completed": len(successes), "failed": 0, "resumable": False,
        })
        round_state.update(status="model_review", stage=2, models=models)
        self.write_round(round_state)
        threading.Thread(
            target=self._run_model_guarded,
            args=(job["job_id"], model_id, copy.deepcopy(settings), api_key, resume), daemon=True,
        ).start()
        return job

    def test_model(
        self, round_id: str, settings: dict[str, Any], api_key: str,
    ) -> dict[str, Any]:
        validate_config(settings)
        if not api_key:
            raise ValueError("必须提供 API Key")
        tasks = self.cluster_tasks(round_id)
        if not tasks:
            raise ValueError("本轮没有聚类审核任务")
        taxonomy = ensure_topic_ids(read_taxonomy(self.round_dir(round_id) / "baseline_taxonomy.json"))
        system_prompt, user_prompt = render_prompts(
            tasks[0], public_taxonomy(taxonomy), settings, MODEL_RESULT_SCHEMA
        )
        response = call_model(settings, api_key, system_prompt, user_prompt, MODEL_RESULT_SCHEMA)
        parsed = validate_model_result(response["parsed"], taxonomy)
        return {
            "notice": "提示词测试结果未保存，也没有修改任何工作体系",
            "cluster_id": tasks[0]["cluster_id"], "parsed": parsed,
            "usage": response.get("usage", {}), "response_id": response.get("response_id", ""),
            "structured_output_mode": response.get("structured_output_mode", settings.get("structured_output", "auto")),
            "fallback_used": bool(response.get("fallback_used", False)),
            "effective_api_style": response.get("effective_api_style", settings.get("api_style", "")),
            "endpoint_url": response.get("endpoint_url", ""),
        }

    def _run_model(
        self, job_id: str, model_id: str, settings: dict[str, Any], api_key: str, resume: bool = False,
    ) -> None:
        job = self.jobs[job_id]
        round_id = job["round_id"]
        round_dir = self.round_dir(round_id)
        paths = self._model_paths(round_id, model_id)
        model_dir = paths["dir"]
        model_dir.mkdir(parents=True, exist_ok=True)
        branch_path = paths["branch"]
        tasks = self.cluster_tasks(round_id)
        if resume:
            self._rebuild_model_branch(round_id, model_id)
            results = {str(row.get("cluster_id")): row for row in read_jsonl(paths["annotations"]) if row.get("cluster_id")}
        else:
            shutil.copy2(round_dir / "baseline_taxonomy.json", branch_path)
            results = {}
            write_jsonl(paths["annotations"], [])
            write_jsonl(paths["attempts"], [])
        write_json(paths["settings"], {key: value for key, value in settings.items() if key != "api_key"})
        successful_ids = {cluster_id for cluster_id, row in results.items() if not row.get("error")}
        failures = len([row for row in results.values() if row.get("error")])
        self._update_job(
            job_id, status="running", started_at=now_iso(), heartbeat_at=now_iso(), total=len(tasks),
            completed=len(results), successful=len(successful_ids), failed=failures,
            message=f"从断点继续；已跳过 {len(successful_ids)} 个成功聚类" if resume else "模型独立审核中",
        )
        self._write_model_checkpoint(round_id, model_id, tasks, results, "running", job_id)
        for task in tasks:
            cluster_id = str(task.get("cluster_id"))
            if cluster_id in successful_ids:
                continue
            if self.jobs[job_id].get("cancel_requested"):
                self._finish_model_interrupted(job_id, model_id, tasks, results, "用户取消，断点已保存")
                return
            attempts = read_jsonl(paths["attempts"])
            attempt_number = 1 + sum(1 for row in attempts if row.get("cluster_id") == cluster_id)
            self._update_job(
                job_id,
                current_cluster_id=cluster_id,
                heartbeat_at=now_iso(),
                message=(
                    f"正在处理 {cluster_id}；已完成 {len(results)}/{len(tasks)}；"
                    f"成功 {len(successful_ids)}；失败 {failures}"
                ),
            )
            try:
                taxonomy = ensure_topic_ids(read_taxonomy(branch_path))
                system_prompt, user_prompt = render_prompts(task, public_taxonomy(taxonomy), settings, MODEL_RESULT_SCHEMA)
                response = call_model(settings, api_key, system_prompt, user_prompt, MODEL_RESULT_SCHEMA)
                parsed = validate_model_result(response["parsed"], taxonomy)
                record = {
                    "schema_version": WORKFLOW_SCHEMA_VERSION, "round_id": round_id,
                    "model_id": model_id, "cluster_id": cluster_id,
                    "taxonomy_version_before": taxonomy.get("taxonomy_version", 1),
                    **parsed, "response_id": response.get("response_id", ""),
                    "usage": response.get("usage", {}), "created_at": now_iso(),
                    "structured_output_mode": response.get(
                        "structured_output_mode", settings.get("structured_output", "auto")
                    ),
                    "fallback_used": bool(response.get("fallback_used", False)),
                    "effective_api_style": response.get("effective_api_style", settings.get("api_style", "")),
                    "endpoint_url": response.get("endpoint_url", ""),
                }
                taxonomy = apply_changes(taxonomy, parsed.get("changes", []), f"model:{model_id}")
                write_taxonomy(branch_path, taxonomy)
                record["taxonomy_version_after"] = taxonomy.get("taxonomy_version", 1)
                results[cluster_id] = record
                successful_ids.add(cluster_id)
            except Exception as error:  # noqa: BLE001
                results[cluster_id] = {
                    "schema_version": WORKFLOW_SCHEMA_VERSION, "round_id": round_id,
                    "model_id": model_id, "cluster_id": cluster_id,
                    "error": str(error), "created_at": now_iso(),
                }
            attempts.append({
                "schema_version": WORKFLOW_SCHEMA_VERSION, "round_id": round_id, "model_id": model_id,
                "cluster_id": cluster_id, "attempt": attempt_number,
                "status": "failed" if results[cluster_id].get("error") else "completed",
                "error": results[cluster_id].get("error", ""), "created_at": now_iso(),
            })
            write_jsonl(paths["attempts"], attempts)
            ordered_results = [results[str(task_row.get("cluster_id"))] for task_row in tasks if str(task_row.get("cluster_id")) in results]
            write_jsonl(paths["annotations"], ordered_results)
            failures = len([row for row in results.values() if row.get("error")])
            checkpoint = self._write_model_checkpoint(round_id, model_id, tasks, results, "running", job_id)
            self._update_job(
                job_id, completed=len(results), successful=checkpoint["successful_count"], failed=failures,
                heartbeat_at=checkpoint["heartbeat_at"],
                message=f"已处理 {len(results)}/{len(tasks)}；成功 {checkpoint['successful_count']}；失败 {failures}",
            )
            if self.jobs[job_id].get("cancel_requested"):
                self._finish_model_interrupted(job_id, model_id, tasks, results, "用户取消，断点已保存")
                return
        status = "completed" if failures == 0 else "completed_with_errors"
        self._write_model_checkpoint(round_id, model_id, tasks, results, status, job_id)
        self._update_job(
            job_id, status=status, finished_at=now_iso(), heartbeat_at=now_iso(),
            completed=len(results), successful=len(tasks) - failures, failed=failures,
            current_cluster_id="",
            message=f"完成；失败 {failures} 条" + ("，可继续运行以重试失败项" if failures else ""),
        )
        round_state = self.read_round(round_id)
        for row in round_state.get("models", []):
            if row.get("model_id") == model_id:
                row.update(
                    status=status, completed=len(tasks) - failures, failed=failures,
                    resumable=failures > 0, checkpoint_path=str(paths["checkpoint"]),
                )
        if len([row for row in round_state.get("models", []) if row.get("status") in {"completed", "completed_with_errors"}]) >= 2:
            round_state.update(status="models_completed", stage=3)
        self.write_round(round_state)
        self.build_review_queue(round_id)

    def _run_model_guarded(
        self, job_id: str, model_id: str, settings: dict[str, Any], api_key: str, resume: bool,
    ) -> None:
        try:
            self._run_model(job_id, model_id, settings, api_key, resume)
        except Exception as error:  # noqa: BLE001
            self._update_job(
                job_id, status="interrupted", finished_at=now_iso(), heartbeat_at=now_iso(),
                message=f"运行异常，断点已保留：{error}",
            )
            try:
                round_state = self.read_round(self.jobs[job_id]["round_id"])
                for row in round_state.get("models", []):
                    if row.get("model_id") == model_id:
                        row.update(status="interrupted", resumable=True)
                round_state.update(status="model_review", stage=2)
                self.write_round(round_state)
            except Exception:  # noqa: BLE001
                pass

    def _finish_model_interrupted(
        self, job_id: str, model_id: str, tasks: list[dict[str, Any]],
        results: dict[str, dict[str, Any]], message: str,
    ) -> None:
        round_id = self.jobs[job_id]["round_id"]
        checkpoint = self._write_model_checkpoint(round_id, model_id, tasks, results, "interrupted", job_id)
        self._update_job(
            job_id, status="interrupted", finished_at=now_iso(), heartbeat_at=now_iso(),
            completed=len(results), successful=checkpoint["successful_count"],
            failed=checkpoint["failed_count"], message=message,
        )
        round_state = self.read_round(round_id)
        for row in round_state.get("models", []):
            if row.get("model_id") == model_id:
                row.update(
                    status="interrupted", completed=checkpoint["successful_count"],
                    failed=checkpoint["failed_count"], resumable=True,
                    checkpoint_path=str(self._model_paths(round_id, model_id)["checkpoint"]),
                )
        round_state.update(status="model_review", stage=2)
        self.write_round(round_state)

    def cancel_job(self, job_id: str) -> dict[str, Any]:
        if job_id not in self.jobs:
            raise ValueError("任务不存在")
        job = self.jobs[job_id]
        if job.get("kind") != "model_review":
            raise ValueError("当前只支持取消模型审核任务")
        if job.get("status") not in {"queued", "running", "cancelling"}:
            raise ValueError("任务已经结束，无需取消")
        return self._update_job(
            job_id, cancel_requested=True, status="cancelling",
            message="已请求取消；当前 API 请求结束后保存断点",
        )

    def model_results(self, round_id: str) -> dict[str, list[dict[str, Any]]]:
        model_root = self.round_dir(round_id) / "models"
        if not model_root.exists():
            return {}
        return {
            path.name: [with_routing_defaults(row) for row in read_jsonl(path / "annotations.jsonl")]
            for path in model_root.iterdir() if path.is_dir()
        }

    def model_result_export(self, round_id: str, model_id: str) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
        model_id = sanitize_identifier(model_id)
        by_model = self.model_results(round_id)
        if model_id not in by_model:
            raise ValueError("模型分支不存在或尚未生成结果")
        original = by_model[model_id]
        tasks = {str(row.get("cluster_id")): row for row in self.cluster_tasks(round_id)}
        flattened: list[dict[str, Any]] = []
        for result in original:
            cluster_id = str(result.get("cluster_id", ""))
            task = tasks.get(cluster_id, {})
            assignments = result.get("topic_assignments") or []
            if not assignments:
                assignments = [{
                    "level_1_topic": "", "level_2_topic": "", "level_3_topic": "", "full_path": "",
                }]
            for assignment_index, assignment in enumerate(assignments, 1):
                flattened.append({
                    "round_id": round_id,
                    "model_id": model_id,
                    "cluster_id": cluster_id,
                    "cluster_sentence_count": task.get("unit_count", ""),
                    "cluster_document_count": task.get("document_count", ""),
                    "assignment_index": assignment_index if result.get("topic_assignments") else "",
                    "decision": result.get("decision", ""),
                    "scope_status": result.get("scope_status", ""),
                    "cluster_quality": result.get("cluster_quality", ""),
                    "suggested_external_domain": result.get("suggested_external_domain", ""),
                    "follow_up_action": result.get("follow_up_action", ""),
                    "level_1_topic": assignment.get("level_1_topic", ""),
                    "level_2_topic": assignment.get("level_2_topic", ""),
                    "level_3_topic": assignment.get("level_3_topic", ""),
                    "full_path": assignment.get("full_path", ""),
                    "confidence": result.get("confidence", ""),
                    "needs_human_review": result.get("needs_human_review", ""),
                    "representative_words": " | ".join(
                        str(word.get("term", "")) if isinstance(word, dict) else str(word)
                        for word in task.get("representative_words", [])
                    ),
                    "rationale": result.get("rationale", ""),
                    "changes_json": json.dumps(result.get("changes", []), ensure_ascii=False),
                    "error": result.get("error", ""),
                    "fallback_used": result.get("fallback_used", ""),
                    "effective_api_style": result.get("effective_api_style", ""),
                    "response_id": result.get("response_id", ""),
                    "created_at": result.get("created_at", ""),
                })
        return original, flattened

    def build_review_queue(self, round_id: str) -> list[dict[str, Any]]:
        by_model = self.model_results(round_id)
        if len(by_model) < 2:
            return []
        by_cluster: dict[str, dict[str, dict[str, Any]]] = {
            task["cluster_id"]: {} for task in self.cluster_tasks(round_id)
        }
        for model_id, rows in by_model.items():
            for row in rows:
                by_cluster.setdefault(row["cluster_id"], {})[model_id] = row
        rng = random.Random(int(round_id[1:]))
        workflow_settings = read_json(self.settings_path, {})
        audit_ratio = float(workflow_settings.get("review", {}).get("agreement_audit_ratio", 0.10))
        confidence_threshold = float(workflow_settings.get("review", {}).get("low_confidence_threshold", 0.75))
        queue = []
        for cluster_id, all_results in by_cluster.items():
            results = {model_id: row for model_id, row in all_results.items() if not row.get("error")}
            model_error = len(results) < len(by_model)
            signatures = {result_signature(row) for row in results.values()}
            has_change = any(row.get("changes") for row in results.values())
            requested = any(row.get("needs_human_review") for row in results.values())
            routed = any(
                row.get("decision") == "NO_STABLE_TOPIC" or row.get("follow_up_action") not in {"", "NONE"}
                for row in results.values()
            )
            conflict = len(signatures) > 1
            low_confidence = any(float(row.get("confidence", 0)) < confidence_threshold for row in results.values())
            sampled_audit = not conflict and not has_change and rng.random() < audit_ratio
            if model_error or conflict or has_change or routed or low_confidence or requested or sampled_audit:
                queue.append({
                    "review_id": f"{round_id}::{cluster_id}", "round_id": round_id,
                    "cluster_id": cluster_id, "reason": (
                        "model_error" if model_error else "model_conflict" if conflict else "taxonomy_change" if has_change
                        else "topic_routing" if routed else "low_confidence" if low_confidence
                        else "model_requested" if requested else "agreement_audit"
                    ),
                    "model_results": all_results,
                    "resolved": False,
                })
        queue.sort(key=lambda row: (row["reason"] == "agreement_audit", row["cluster_id"]))
        write_json(self.round_dir(round_id) / "review_queue.json", {"items": queue})
        round_state = self.read_round(round_id)
        if queue:
            round_state.update(status="human_review", stage=3, review_item_count=len(queue))
            self.write_round(round_state)
        return queue

    def review_queue(self, round_id: str) -> list[dict[str, Any]]:
        queue = read_json(self.round_dir(round_id) / "review_queue.json", {"items": []}).get("items", [])
        decisions = {row["review_id"]: row for row in read_jsonl(self.round_dir(round_id) / "human_decisions.jsonl")}
        for item in queue:
            if item["review_id"] in decisions:
                item["decision"] = decisions[item["review_id"]]
                item["resolved"] = True
        tasks = {row["cluster_id"]: row for row in self.cluster_tasks(round_id)}
        for item in queue:
            item["cluster_task"] = tasks.get(item["cluster_id"], {})
        return queue

    def save_human_decision(self, round_id: str, payload: dict[str, Any]) -> dict[str, Any]:
        review_id = str(payload.get("review_id", ""))
        queue = {row["review_id"]: row for row in self.review_queue(round_id)}
        if review_id not in queue:
            raise ValueError("人工审核任务不存在")
        action = payload.get("action")
        if action not in {"select_model", "custom", "no_change"}:
            raise ValueError("人工审核 action 无效")
        selected_model = str(payload.get("selected_model", ""))
        if action == "select_model" and selected_model not in queue[review_id]["model_results"]:
            raise ValueError("选择的模型结果不存在")
        if action == "select_model" and queue[review_id]["model_results"][selected_model].get("error"):
            raise ValueError("不能采用执行失败的模型结果")
        custom_result = copy.deepcopy(payload.get("custom_result") or {})
        human_comment = str(payload.get("comment", "")).strip()
        saved_at = now_iso()
        if action == "custom":
            supplied_review_id = str(custom_result.get("review_id", "")).strip()
            supplied_cluster_id = str(custom_result.get("cluster_id", "")).strip()
            expected_cluster_id = str(queue[review_id]["cluster_id"])
            if supplied_review_id and supplied_review_id != review_id:
                raise ValueError("人工自定义结果绑定了其他 review_id，请重新打开当前聚类后保存")
            if supplied_cluster_id and supplied_cluster_id != expected_cluster_id:
                raise ValueError("人工自定义结果绑定了其他 cluster_id，请重新打开当前聚类后保存")
            taxonomy = ensure_topic_ids(read_taxonomy(self.canonical_taxonomy))
            custom_result = validate_model_result(
                custom_result, taxonomy, allow_external_additions=True,
            )
            custom_result["review_id"] = review_id
            custom_result["cluster_id"] = expected_cluster_id
            custom_result["human_comment"] = human_comment
            custom_result["human_saved_at"] = saved_at
            if human_comment and custom_result.get("rationale") in {"", "人工审核结构化填写"}:
                custom_result["rationale"] = human_comment
        record = {
            "schema_version": WORKFLOW_SCHEMA_VERSION, "review_id": review_id,
            "round_id": round_id, "cluster_id": queue[review_id]["cluster_id"],
            "action": action, "selected_model": selected_model,
            "custom_result": custom_result, "comment": human_comment,
            "created_at": saved_at,
        }
        path = self.round_dir(round_id) / "human_decisions.jsonl"
        rows = [row for row in read_jsonl(path) if row.get("review_id") != review_id]
        rows.append(record)
        write_jsonl(path, rows)
        if (self.round_dir(round_id) / "round.json").exists():
            round_state = self.read_round(round_id)
            if round_state.get("published") or round_state.get("status") in {"published", "evaluated", "validation_failed"}:
                round_state.update(
                    status="human_review", stage=3, published=False, revision_pending=True,
                    last_published_taxonomy_version=round_state.get("published_taxonomy_version"),
                )
                round_state.pop("metrics", None)
                self.write_round(round_state)
        return record

    def publish_round(self, round_id: str) -> dict[str, Any]:
        queue = self.review_queue(round_id)
        unresolved = [row for row in queue if not row.get("resolved")]
        if unresolved:
            raise ValueError(f"仍有 {len(unresolved)} 条人工审核任务未完成")
        decisions = {row["review_id"]: row for row in read_jsonl(self.round_dir(round_id) / "human_decisions.jsonl")}
        by_model = self.model_results(round_id)
        model_ids = sorted(by_model)
        by_cluster = {
            cluster_id: {
                model_id: next((row for row in rows if row.get("cluster_id") == cluster_id and not row.get("error")), {})
                for model_id, rows in by_model.items()
            }
            for cluster_id in {row.get("cluster_id") for rows in by_model.values() for row in rows}
        }
        state = ensure_topic_ids(read_taxonomy(self.round_dir(round_id) / "baseline_taxonomy.json"))
        applied: list[dict[str, Any]] = []
        routing_records: list[dict[str, Any]] = []
        tasks_by_cluster = {row["cluster_id"]: row for row in self.cluster_tasks(round_id)}
        human_overrides = 0
        for cluster_id in sorted(by_cluster):
            results = {key: value for key, value in by_cluster[cluster_id].items() if value}
            if not results:
                continue
            review_id = f"{round_id}::{cluster_id}"
            decision = decisions.get(review_id)
            selected: dict[str, Any] | None = None
            if decision:
                if decision["action"] == "select_model":
                    selected = results.get(decision["selected_model"])
                    human_overrides += int(decision["selected_model"] != model_ids[0])
                elif decision["action"] == "custom":
                    selected = decision["custom_result"]
                    human_overrides += 1
                elif decision["action"] == "no_change":
                    selected = None
            else:
                signatures = Counter(result_signature(row) for row in results.values())
                winning = signatures.most_common(1)[0][0]
                selected = next(row for row in results.values() if result_signature(row) == winning)
            if selected:
                state = apply_changes(state, selected.get("changes", []), f"human_publish:{round_id}")
                applied.append({
                    "cluster_id": cluster_id,
                    "review_id": review_id,
                    "decision_source": f"human:{decision['action']}" if decision else "model_agreement",
                    "human_comment": decision.get("comment", "") if decision else "",
                    "result": selected,
                })
                follow_up_action = str(selected.get("follow_up_action", "NONE"))
                if selected.get("decision") == "NO_STABLE_TOPIC" or follow_up_action != "NONE":
                    task = tasks_by_cluster.get(cluster_id, {})
                    routing_records.append({
                        "schema_version": WORKFLOW_SCHEMA_VERSION,
                        "route_id": f"{round_id}::{cluster_id}",
                        "round_id": round_id,
                        "cluster_id": cluster_id,
                        "decision": selected.get("decision", ""),
                        "scope_status": selected.get("scope_status", "UNCERTAIN"),
                        "cluster_quality": selected.get("cluster_quality", "MIXED"),
                        "suggested_external_domain": selected.get("suggested_external_domain", ""),
                        "follow_up_action": follow_up_action,
                        "rationale": selected.get("rationale", ""),
                        "human_comment": decision.get("comment", "") if decision else "",
                        "representative_words": task.get("representative_words", []),
                        "representative_texts": task.get("representative_texts", []),
                        "created_at": now_iso(),
                    })
        previous_version = int(read_taxonomy(self.canonical_taxonomy).get("taxonomy_version", 1))
        state["taxonomy_version"] = previous_version + 1
        state["updated_at"] = now_iso()
        state.setdefault("change_log", []).append({
            "taxonomy_version": state["taxonomy_version"], "changed_at": state["updated_at"],
            "created_by": "human_adjudication", "source": round_id,
            "reason": "多模型独立投票与人工审核发布", "applied_cluster_count": len(applied),
        })
        snapshot = self.snapshots_root / f"taxonomy_v{state['taxonomy_version']:04d}_{round_id}.json"
        write_taxonomy(snapshot, state)
        write_taxonomy(self.canonical_taxonomy, state)
        write_json(self.round_dir(round_id) / "published_changes.json", {"applied": applied})
        write_jsonl(self.round_dir(round_id) / "topic_routing_pool.jsonl", routing_records)
        global_routing_path = self.workflow_root / "topic_routing_pool.jsonl"
        global_routing = {
            row.get("route_id"): row for row in read_jsonl(global_routing_path) if row.get("route_id")
        }
        global_routing.update({row["route_id"]: row for row in routing_records})
        write_jsonl(global_routing_path, list(global_routing.values()))
        round_state = self.read_round(round_id)
        round_state.update(
            status="published", stage=4, published=True,
            published_taxonomy_version=state["taxonomy_version"], human_override_count=human_overrides,
            revision_pending=False, published_at=now_iso(),
        )
        self.write_round(round_state)
        return {"round": round_state, "taxonomy": public_taxonomy(state), "snapshot": str(snapshot)}

    def publish_bertopic_test(self, round_id: str) -> dict[str, Any]:
        """Publish raw BERTopic clusters as an isolated, clearly marked test taxonomy."""
        round_state = self.read_round(round_id)
        tasks = [
            row for row in self.cluster_tasks(round_id)
            if str(row.get("cluster_id", "")) != "BT_OUTLIER" and int(row.get("unit_count", 0)) > 0
        ]
        if round_state.get("status") not in {"clustered", "models_completed", "human_review"} or not tasks:
            raise ValueError("测试直发只允许在聚类完成且没有运行中模型任务时使用，并且必须存在非离群聚类")

        canonical_version = int(read_taxonomy(self.canonical_taxonomy).get("taxonomy_version", 1))
        state = empty_taxonomy("BERTopic 聚类测试体系")
        state.update({
            "taxonomy_version": canonical_version + 1,
            "publish_mode": "bertopic_test",
            "test_only": True,
            "source_method": "bertopic_clusters_without_model_matching",
            "source_round_id": round_id,
        })
        actor = f"bertopic_test_publish:{round_id}"
        state["topics"].extend([
            topic_record(
                "BERTopic测试体系", source="bertopic_test_publish", created_by=actor,
                definition="仅用于快速验证流程的 BERTopic 聚类测试体系，不代表正式领域分类。",
            ),
            topic_record(
                "BERTopic测试体系 > 聚类结果", source="bertopic_test_publish", created_by=actor,
                definition="未经模型匹配和人工裁决的 BERTopic 聚类结果。",
            ),
        ])

        applied: list[dict[str, Any]] = []
        for task in tasks:
            cluster_id = str(task.get("cluster_id", "")).strip()
            keywords = [
                normalize_representative_term(
                    word.get("term", "") if isinstance(word, dict) else str(word)
                )
                for word in task.get("representative_words", [])
            ]
            keywords = [word.replace(">", " ").replace("/", " ").strip() for word in keywords if word]
            keywords = list(dict.fromkeys(word for word in keywords if word))[:12]
            label_words = "、".join(keywords[:4]) or "无代表词"
            leaf_name = f"{cluster_id}｜{label_words}"
            path = f"BERTopic测试体系 > 聚类结果 > {leaf_name}"
            definition = (
                f"BERTopic 测试聚类 {cluster_id}，包含 {int(task.get('unit_count', 0))} 个抽样单元；"
                f"代表词：{'、'.join(keywords) or '无'}。未经模型匹配和人工裁决。"
            )
            state["topics"].append(topic_record(
                path, source="bertopic_test_publish", created_by=actor,
                definition=definition, keywords=keywords,
            ))
            levels = split_topic_path(path)
            result = {
                "decision": "KEEP",
                "topic_assignments": [{
                    "level_1_topic": levels[0], "level_2_topic": levels[1],
                    "level_3_topic": levels[2], "full_path": path,
                }],
                "final_topic_paths": [path],
                "scope_status": "IN_SCOPE", "cluster_quality": "COHERENT",
                "suggested_external_domain": "", "follow_up_action": "NONE",
                "changes": [],
                "rationale": "BERTopic 测试直发：跳过模型匹配和人工裁决。",
                "confidence": 0.0, "needs_human_review": False,
                "test_only": True,
            }
            applied.append({
                "cluster_id": cluster_id,
                "review_id": f"{round_id}::{cluster_id}",
                "decision_source": "bertopic_test_publish",
                "human_comment": "",
                "result": result,
            })

        state = ensure_topic_ids(state)
        state["updated_at"] = now_iso()
        state["change_log"] = [{
            "taxonomy_version": state["taxonomy_version"],
            "changed_at": state["updated_at"],
            "created_by": actor,
            "source": round_id,
            "reason": "测试模式直接发布 BERTopic 聚类，未经过模型匹配或人工裁决",
            "applied_cluster_count": len(applied),
        }]
        test_taxonomy = self.round_dir(round_id) / "bertopic_test_taxonomy.json"
        snapshot = self.snapshots_root / f"taxonomy_test_v{state['taxonomy_version']:04d}_{round_id}.json"
        write_taxonomy(test_taxonomy, state)
        write_taxonomy(snapshot, state)
        write_json(self.round_dir(round_id) / "published_changes.json", {
            "publish_mode": "bertopic_test", "test_only": True, "applied": applied,
        })
        write_jsonl(self.round_dir(round_id) / "topic_routing_pool.jsonl", [])
        round_state.update(
            status="published_test", stage=4, published=True,
            publish_mode="bertopic_test", test_only=True, model_review_bypassed=True,
            published_taxonomy_version=state["taxonomy_version"], human_override_count=0,
            revision_pending=False, published_at=now_iso(),
        )
        self.write_round(round_state)
        return {
            "round": round_state, "taxonomy": public_taxonomy(state),
            "snapshot": str(snapshot), "test_taxonomy": str(test_taxonomy),
        }

    def start_validation(self, round_id: str, python_executable: str = "") -> dict[str, Any]:
        round_state = self.read_round(round_id)
        if not round_state.get("published"):
            raise ValueError("必须先完成人工审核并发布体系")
        executable = self._python_executable(python_executable, round_state)
        cluster_assignments = self.round_dir(round_id) / "clustering" / "demo_unit_assignments.jsonl"
        published_changes = self.round_dir(round_id) / "published_changes.json"
        if not cluster_assignments.exists():
            raise ValueError("缺少本轮聚类句子归属，不能建立簇级先验；请先完成本轮聚类")
        if not published_changes.exists():
            raise ValueError("缺少本轮发布结果，请先发布人工审核后的体系")
        total = int(round_state.get("sample_size", 0))
        job = self._new_job("validation", round_id, {
            "python_executable": executable, "completed": 0, "total": total,
            "message": "等待正式单句分类",
        })
        threading.Thread(
            target=self._run_validation, args=(job["job_id"], executable), daemon=True
        ).start()
        round_state.update(status="validating", stage=4, validation_python_executable=executable)
        self.write_round(round_state)
        return job

    def _run_validation(self, job_id: str, executable: str) -> None:
        job = self.jobs[job_id]
        round_id = job["round_id"]
        round_state = self.read_round(round_id)
        round_dir = self.round_dir(round_id)
        output = round_dir / "validation"
        dynamic_taxonomy = (
            round_dir / "bertopic_test_taxonomy.json"
            if round_state.get("publish_mode") == "bertopic_test"
            else self.canonical_taxonomy
        )
        command = [
            executable, str(self.root / "run_sentence_validation.py"),
            "--sections", round_state["sample_path"],
            "--taxonomy", str(self.root / "configs" / "new_energy_candidate_taxonomy.json"),
            "--dynamic-taxonomy", str(dynamic_taxonomy),
            "--term-table", str(self.root / "configs" / "topic_seed_terms.csv"),
            "--config", str(self.root / "configs" / "bertopic.json"), "--output-root", str(output),
            "--cluster-assignments", str(round_dir / "clustering" / "demo_unit_assignments.jsonl"),
            "--published-changes", str(round_dir / "published_changes.json"),
        ]
        self._update_job(job_id, status="running", started_at=now_iso(), message="正在载入正式分类模型")
        try:
            flags = subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0
            with (round_dir / "validation.log").open("w", encoding="utf-8") as log:
                process = subprocess.Popen(
                    command, cwd=self.root, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                    creationflags=flags, text=True, encoding="utf-8", errors="replace", bufsize=1,
                )
                assert process.stdout is not None
                for line in process.stdout:
                    log.write(line)
                    log.flush()
                    if line.startswith("TOPIC_PROGRESS "):
                        try:
                            progress = json.loads(line.removeprefix("TOPIC_PROGRESS "))
                            self._update_job(
                                job_id, completed=int(progress.get("completed", 0)),
                                total=int(progress.get("total", 0)),
                                message=str(progress.get("message", "正在正式分类并评估")),
                                heartbeat_at=now_iso(),
                            )
                        except (ValueError, TypeError, json.JSONDecodeError):
                            pass
                return_code = process.wait()
            if return_code:
                detail = self._validation_error_detail(round_dir / "validation.log")
                raise RuntimeError(detail or f"验证进程退出码 {return_code}")
            metrics = self.evaluate_round(round_id)
            self._prune_validation_outputs(output)
            round_state = self.read_round(round_id)
            round_state.update(status="evaluated", stage=4, metrics=metrics)
            self.write_round(round_state)
            total = int(self.jobs[job_id].get("total", 0))
            self._update_job(job_id, status="completed", completed=total, total=total, finished_at=now_iso(), message="正式分类与完善度评估完成")
        except Exception as error:  # noqa: BLE001
            round_state = self.read_round(round_id)
            round_state["status"] = "validation_failed"
            self.write_round(round_state)
            self._update_job(job_id, status="failed", finished_at=now_iso(), message=str(error))

    @staticmethod
    def _validation_error_detail(log_path: Path) -> str:
        if not log_path.exists():
            return "验证进程失败，且没有生成日志"
        lines = [line.strip() for line in log_path.read_text(encoding="utf-8", errors="replace").splitlines() if line.strip()]
        for line in reversed(lines):
            if line.startswith(("RuntimeError:", "ModuleNotFoundError:", "FileNotFoundError:", "ValueError:")):
                return line
        return lines[-1][:500] if lines else "验证进程失败，日志为空"

    def _prune_validation_outputs(self, output: Path) -> None:
        keep = {
            "demo_run_report.json", "demo_sampling_report.json", "demo_topic_quality.json",
            "demo_topic_summary.json", "demo_unit_assignments.jsonl", "demo_sampled_units.jsonl",
            "demo_sentence_classification.csv", "demo_sentence_validation.csv",
            "demo_term_topic_evidence.csv", "demo_exclusion_audit.csv",
        }
        for path in output.iterdir():
            if path.is_file() and path.name not in keep:
                path.unlink()

    def evaluate_round(self, round_id: str) -> dict[str, Any]:
        round_dir = self.round_dir(round_id)
        quality = read_json(round_dir / "clustering" / "demo_topic_quality.json", {})
        by_model = self.model_results(round_id)
        cluster_ids = sorted({row.get("cluster_id") for rows in by_model.values() for row in rows if not row.get("error")})
        full_agree = 0
        level1_agree = 0
        covered_ids: set[str] = set()
        new_changes = 0
        for cluster_id in cluster_ids:
            rows = [next((row for row in values if row.get("cluster_id") == cluster_id and not row.get("error")), None) for values in by_model.values()]
            rows = [row for row in rows if row]
            signatures = {result_signature(row) for row in rows}
            full_agree += int(len(signatures) == 1)
            level1_sets = {
                tuple(sorted({path.split(" > ")[0] for path in row.get("final_topic_paths", [])})) for row in rows
            }
            level1_agree += int(len(level1_sets) == 1)
            if any(row.get("final_topic_paths") for row in rows):
                covered_ids.add(cluster_id)
            new_changes += int(any(change.get("operation") == "ADD_TOPIC" for row in rows for change in row.get("changes", [])))
        validation_csv = round_dir / "validation" / "demo_sentence_classification.csv"
        classification_rows = []
        if validation_csv.exists():
            with validation_csv.open("r", encoding="utf-8-sig", newline="") as handle:
                classification_rows = list(csv.DictReader(handle))
        queue = self.review_queue(round_id)
        unresolved = sum(not row.get("resolved") for row in queue)
        round_state = self.read_round(round_id)
        current_assignments = {
            row.get("unit_id"): row.get("primary_topic", {}).get("path", "")
            for row in read_jsonl(round_dir / "validation" / "demo_unit_assignments.jsonl")
            if row.get("unit_id")
        }
        previous_rounds = [
            row for row in reversed(self.list_rounds())
            if row["round_id"] != round_id and (self.round_dir(row["round_id"]) / "validation" / "demo_unit_assignments.jsonl").exists()
        ]
        anchor_stability = 0.0
        if previous_rounds:
            previous_assignments = {
                row.get("unit_id"): row.get("primary_topic", {}).get("path", "")
                for row in read_jsonl(
                    self.round_dir(previous_rounds[0]["round_id"]) / "validation" / "demo_unit_assignments.jsonl"
                ) if row.get("unit_id")
            }
            anchor_ids = set(read_json(self.registry_path, {"anchor_ids": []}).get("anchor_ids", []))
            common = anchor_ids & current_assignments.keys() & previous_assignments.keys()
            anchor_stability = (
                sum(current_assignments[key] == previous_assignments[key] for key in common) / len(common)
                if common else 0.0
            )
        topic_count = max(1, len(public_taxonomy(read_taxonomy(self.canonical_taxonomy))["topics"]))
        published_results = {
            str(row.get("cluster_id")): row.get("result", {})
            for row in read_json(round_dir / "published_changes.json", {"applied": []}).get("applied", [])
        }
        if published_results:
            in_scope_cluster_ids = {
                cluster_id for cluster_id, result in published_results.items()
                if result.get("scope_status", "IN_SCOPE") != "OUT_OF_SCOPE"
                and result.get("cluster_quality", "COHERENT") != "NOISE"
            }
            out_of_scope_count = sum(
                result.get("scope_status") == "OUT_OF_SCOPE" for result in published_results.values()
            )
            mixed_count = sum(
                result.get("cluster_quality") == "MIXED" for result in published_results.values()
            )
            noise_count = sum(
                result.get("cluster_quality") == "NOISE" for result in published_results.values()
            )
            routing_denominator = len(published_results)
        else:
            in_scope_cluster_ids = set(cluster_ids)
            out_of_scope_count = mixed_count = noise_count = 0
            routing_denominator = len(cluster_ids)
        validation_rows = sentence_validation_rows(classification_rows, published_results)
        if validation_rows:
            write_csv(
                round_dir / "validation" / "demo_sentence_validation.csv",
                [
                    "unit_id", "doc_id", "cluster_id", "classification_status",
                    "accepted_topic_path", "accepted_topic_score", "validation_identity",
                    "accepted_topic_scope",
                    "validation_identity_label", "validation_route", "primary_metric_eligible",
                    "identity_source", "cluster_scope_status", "cluster_quality",
                ],
                validation_rows,
            )
        sentence_metrics = sentence_scope_metrics(classification_rows, published_results)
        values = {
            "outlier_rate": float(quality.get("outlier_unit_rate", 0)),
            "cluster_coverage": (
                len(covered_ids & in_scope_cluster_ids) / len(in_scope_cluster_ids)
                if in_scope_cluster_ids else 1.0
            ),
            "out_of_scope_rate": out_of_scope_count / routing_denominator if routing_denominator else 0.0,
            "mixed_cluster_rate": mixed_count / routing_denominator if routing_denominator else 0.0,
            "noise_cluster_rate": noise_count / routing_denominator if routing_denominator else 0.0,
            **sentence_metrics,
            "unresolved_conflict_rate": unresolved / len(queue) if queue else 0.0,
            "human_override_rate": int(round_state.get("human_override_count", 0)) / len(queue) if queue else 0.0,
            "new_topic_rate": new_changes / topic_count,
            "level1_agreement": level1_agree / len(cluster_ids) if cluster_ids else 0.0,
            "full_path_agreement": full_agree / len(cluster_ids) if cluster_ids else 0.0,
            "anchor_stability": anchor_stability,
        }
        configured_gates = read_json(self.settings_path, {"gates": DEFAULT_GATES}).get("gates", {})
        gates = {**DEFAULT_GATES, **configured_gates}
        checks = {
            "outlier_rate": values["outlier_rate"] <= gates["max_outlier_rate"],
            "cluster_coverage": values["cluster_coverage"] >= gates["min_cluster_coverage"],
            "sentence_coverage": values["sentence_coverage"] >= gates["min_sentence_coverage"],
            "unknown_rate": values["unknown_rate"] <= gates["max_unknown_rate"],
            "out_of_scope_false_acceptance_rate": (
                values["out_of_scope_false_acceptance_rate"]
                <= gates["max_out_of_scope_false_acceptance_rate"]
            ),
            "noise_leakage_rate": values["noise_leakage_rate"] <= gates["max_noise_leakage_rate"],
            "uncertain_sentence_rate": (
                values["uncertain_sentence_rate"] <= gates["max_uncertain_sentence_rate"]
            ),
            "unresolved_conflict_rate": values["unresolved_conflict_rate"] <= gates["max_unresolved_conflict_rate"],
            "human_override_rate": values["human_override_rate"] <= gates["max_human_override_rate"],
            "new_topic_rate": values["new_topic_rate"] <= gates["max_new_topic_rate"],
            "level1_agreement": values["level1_agreement"] >= gates["min_level1_agreement"],
            "full_path_agreement": values["full_path_agreement"] >= gates["min_full_path_agreement"],
            "anchor_stability": (
                True if not previous_rounds else values["anchor_stability"] >= gates["min_anchor_stability"]
            ),
        }
        previous = [row for row in self.list_rounds() if row["round_id"] != round_id and row.get("metrics")]
        consecutive = 1 + (
            int(previous[0]["metrics"].get("consecutive_passing_rounds", 0)) if previous else 0
        )
        all_passed = all(checks.values())
        return {
            "values": values, "gates": gates, "checks": checks,
            "all_primary_gates_passed": all_passed,
            "consecutive_passing_rounds": consecutive if all_passed else 0,
            "recommend_export": all_passed and consecutive >= int(gates["required_consecutive_rounds"]),
            "message": (
                "已连续满足完善度门槛，建议导出使用。"
                if all_passed and consecutive >= int(gates["required_consecutive_rounds"])
                else "尚未达到连续轮次完善标准，建议继续下一轮。"
            ),
        }

    def job(self, job_id: str) -> dict[str, Any]:
        if job_id not in self.jobs:
            raise ValueError("任务不存在或服务已重启")
        return self.jobs[job_id]


def sanitize_identifier(value: str) -> str:
    return "".join(character for character in str(value).strip() if character.isalnum() or character in {"-", "_"})[:64]


def re_fullmatch_round(value: str) -> bool:
    return len(value) == 5 and value.startswith("R") and value[1:].isdigit()
