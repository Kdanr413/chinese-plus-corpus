from __future__ import annotations

import argparse
import copy
import csv
import hashlib
import json
import os
import re
import subprocess
import sys
import types
import unicodedata
from collections import Counter, defaultdict
from datetime import datetime
from pathlib import Path
from typing import Any

try:
    from .topic_taxonomy_store import (
        assignable_paths, initialize_or_merge_taxonomy, normalize_three_level_taxonomy,
        read_taxonomy, taxonomy_tree, write_taxonomy,
    )
    from .sentence_sampler import sample_units_by_doc
except ImportError:  # Direct script execution by WorkflowManager.
    from topic_taxonomy_store import (
        assignable_paths, initialize_or_merge_taxonomy, normalize_three_level_taxonomy,
        read_taxonomy, taxonomy_tree, write_taxonomy,
    )
    from sentence_sampler import sample_units_by_doc


STOPWORDS = {
    "一个", "一些", "以及", "为了", "其中", "主要", "相关", "方面", "目前", "可以", "通过",
    "进行", "采用", "形成", "实现", "提高", "提升", "促进", "推进", "加强", "建设", "发展",
    "工作", "研究", "技术", "系统", "内容", "方法", "方式", "过程", "目标", "要求", "任务",
    "学习", "课程", "步骤", "认识", "了解", "掌握", "课文", "本课", "本章", "单元", "未识别",
    "是否", "推动", "加快", "重点", "体系", "我们", "你们", "他们", "是什么", "识别", "信息",
    "什么", "这是", "哪里", "怎么", "哪个", "旁边", "公司", "员工", "完善", "行动", "消耗",
}

EXTRA_PROTECTED_TERMS = {
    "新能源汽车", "纯电动汽车", "插电式混合动力汽车", "动力电池", "驱动电机", "整车控制器",
    "充电桩", "制动液", "冷却液", "雨刮器", "散热器", "蓄电池", "电机冷却液",
}

DEFAULT_CONFIG: dict[str, Any] = {
    "schema_version": "2.0",
    "method": "BERTopic",
    "embedding_model": "BAAI/bge-small-zh-v1.5",
    "random_seed": 20260817,
    "bertopic": {"calculate_probabilities": True, "verbose": True},
    "input": {
        # build_units treats this value as an inclusive minimum. 11 therefore
        # implements the corpus rule "sentence length must be greater than 10".
        "min_analysis_characters": 11,
        "max_analysis_characters": 4000,
        "exclude_structural_noise": True,
        "structural_noise_exact": [
            "参考文献", "参考资料", "文献", "目录", "摘要", "关键词", "关键字",
            "致谢", "作者简介", "附录",
        ],
        "structural_noise_regex": [r"^附录[A-Za-z0-9一二三四五六七八九十]*$"],
    },
    "sampling": {
        "enabled": True,
        "sample_size": 800,
        "strategy": "balanced_systematic",
        "seed": 20260817,
    },
    "dimensionality_reduction": {
        "method": "umap",
        "pca": {"n_components": 25, "whiten": False},
        "umap": {
            "n_neighbors": 15, "n_components": 5, "min_dist": 0.0,
            "metric": "cosine", "low_memory": True, "import_timeout_seconds": 60,
            "numba_cache_dir": ".cache/numba",
        },
    },
    "clustering": {
        "min_cluster_size": 12,
        "min_samples": 3,
        "metric": "euclidean",
        "cluster_selection_method": "leaf",
        "cluster_selection_epsilon": 0.0,
        "prediction_data": True,
    },
    "representation": {
        "ngram_range": [1, 2], "min_df": 2, "max_df": 1.0,
        "top_words": 15, "center_samples": 5, "boundary_samples": 3,
        "reference_candidate_limit": 3,
        "bm25_weighting": True, "reduce_frequent_words": True,
    },
    "matching": {
        "weights": {"vector": 0.50, "approved_terms": 0.25, "taxonomy_keywords": 0.20, "hierarchy": 0.05},
        "cluster_match_threshold": 0.50,
        "unit_match_threshold": 0.52,
        "unit_review_threshold": 0.40,
        "unit_margin_threshold": 0.05,
        "vector_evidence_threshold": 0.70,
        "dimension_threshold": 0.50,
        "dimension_vector_threshold": 0.72,
        "secondary_score_window": 0.12,
        "max_secondary_topics": 2,
        "max_dimensions": 2,
        "unit_candidate_limit": 8,
        "dimension_candidate_limit": 4,
        "stored_candidate_limit": 5,
    },
    "workflow": {
        "default_stage": "taxonomy_build",
        "cluster_prior": {
            "enabled": True,
            "weight": 0.25,
            "minimum_score": 0.40,
            "vector_evidence_threshold": 0.65,
            "score_window": 0.12,
            "max_topics": 3,
        },
    },
    "stability_random_seeds": [20260817, 20260831, 20260913],
}


def deep_merge(base: dict[str, Any], override: dict[str, Any]) -> dict[str, Any]:
    result = copy.deepcopy(base)
    for key, value in override.items():
        if isinstance(value, dict) and isinstance(result.get(key), dict):
            result[key] = deep_merge(result[key], value)
        else:
            result[key] = copy.deepcopy(value)
    return result


def load_bertopic_config(path: Path) -> dict[str, Any]:
    source = read_json(path)
    # Backward compatibility for schema 1.x files.
    if "min_analysis_characters" in source:
        source.setdefault("input", {})["min_analysis_characters"] = source.pop("min_analysis_characters")
    if "max_analysis_characters" in source:
        source.setdefault("input", {})["max_analysis_characters"] = source.pop("max_analysis_characters")
    if "umap" in source:
        source.setdefault("dimensionality_reduction", {}).setdefault("umap", {}).update(source.pop("umap"))
    if "hdbscan" in source:
        legacy = source.pop("hdbscan")
        for old_key in ("min_topic_size_floor", "min_topic_size_ratio", "min_samples_floor"):
            legacy.pop(old_key, None)
        source.setdefault("clustering", {}).update(legacy)
    config = deep_merge(DEFAULT_CONFIG, source)
    validate_bertopic_config(config)
    return config


def validate_bertopic_config(config: dict[str, Any]) -> None:
    method = str(config["dimensionality_reduction"]["method"]).lower()
    if method not in {"pca", "umap"}:
        raise ValueError("dimensionality_reduction.method 只支持 pca 或 umap")
    ngram = config["representation"]["ngram_range"]
    if not isinstance(ngram, list) or len(ngram) != 2 or not (1 <= int(ngram[0]) <= int(ngram[1])):
        raise ValueError("representation.ngram_range 必须是两个递增正整数")
    if int(config["clustering"]["min_cluster_size"]) < 2:
        raise ValueError("clustering.min_cluster_size 必须至少为2")
    if int(config["clustering"]["min_samples"]) < 1:
        raise ValueError("clustering.min_samples 必须至少为1")
    for key in ("structural_noise_exact", "structural_noise_regex"):
        if not isinstance(config["input"][key], list):
            raise ValueError(f"input.{key} 必须是字符串数组")
    for pattern in config["input"]["structural_noise_regex"]:
        try:
            re.compile(str(pattern))
        except re.error as error:
            raise ValueError(f"input.structural_noise_regex 含无效正则：{pattern}") from error
    if int(config["sampling"]["sample_size"]) < 6:
        raise ValueError("sampling.sample_size 必须至少为6")
    if config["sampling"]["strategy"] not in {"balanced_systematic", "balanced_random"}:
        raise ValueError("sampling.strategy 只支持 balanced_systematic 或 balanced_random")
    if config["clustering"]["cluster_selection_method"] not in {"eom", "leaf"}:
        raise ValueError("clustering.cluster_selection_method 只支持 eom 或 leaf")
    weights = config["matching"]["weights"]
    required = {"vector", "approved_terms", "taxonomy_keywords", "hierarchy"}
    if set(weights) != required or any(float(value) < 0 for value in weights.values()):
        raise ValueError(f"matching.weights 必须且只能包含 {sorted(required)}，权重不得为负")
    if abs(sum(float(value) for value in weights.values()) - 1.0) > 1e-9:
        raise ValueError("matching.weights 总和必须等于1")
    workflow = config["workflow"]
    if workflow["default_stage"] not in {"taxonomy_build", "sentence_classification"}:
        raise ValueError("workflow.default_stage 只支持 taxonomy_build 或 sentence_classification")
    prior = workflow["cluster_prior"]
    for key in ("weight", "minimum_score", "vector_evidence_threshold", "score_window"):
        if not 0.0 <= float(prior[key]) <= 1.0:
            raise ValueError(f"workflow.cluster_prior.{key} 必须在0到1之间")
    if int(prior["max_topics"]) < 1:
        raise ValueError("workflow.cluster_prior.max_topics 必须至少为1")
    if abs(sum(float(value) for value in weights.values()) - 1.0) > 1e-6:
        raise ValueError("matching.weights 的总和必须等于1")
    if float(config["representation"]["min_df"]) <= 0:
        raise ValueError("representation.min_df 必须大于0")
    if not 0 < float(config["representation"]["max_df"]) <= 1:
        raise ValueError("representation.max_df 必须在0到1之间")
    for key in (
        "cluster_match_threshold", "unit_match_threshold", "unit_review_threshold",
        "unit_margin_threshold", "vector_evidence_threshold", "dimension_threshold",
        "dimension_vector_threshold", "secondary_score_window",
    ):
        value = float(config["matching"][key])
        if not 0.0 <= value <= 1.0:
            raise ValueError(f"matching.{key} 必须在0到1之间")
    for key in (
        "top_words", "center_samples", "boundary_samples", "reference_candidate_limit",
    ):
        if int(config["representation"][key]) < 1:
            raise ValueError(f"representation.{key} 必须至少为1")
    for key in (
        "max_secondary_topics", "max_dimensions", "unit_candidate_limit",
        "dimension_candidate_limit", "stored_candidate_limit",
    ):
        if int(config["matching"][key]) < 1:
            raise ValueError(f"matching.{key} 必须至少为1")


def read_json(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8-sig"))


def read_jsonl(path: Path) -> list[dict[str, Any]]:
    with path.open("r", encoding="utf-8-sig") as handle:
        return [json.loads(line) for line in handle if line.strip()]


def write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8")


def write_jsonl(path: Path, rows: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="") as handle:
        for row in rows:
            handle.write(json.dumps(row, ensure_ascii=False) + "\n")


def write_csv(path: Path, fields: list[str], rows: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)


def read_csv(path: Path) -> list[dict[str, str]]:
    with path.open("r", encoding="utf-8-sig", newline="") as handle:
        return [dict(row) for row in csv.DictReader(handle)]


def flatten_taxonomy(reference: dict[str, Any]) -> list[dict[str, Any]]:
    if "nodes" in reference:
        if __package__:
            from .topic_taxonomy_store import flatten_published_taxonomy
        else:
            from topic_taxonomy_store import flatten_published_taxonomy
        return flatten_published_taxonomy(reference)
    nodes: list[dict[str, Any]] = []

    def visit(node: dict[str, Any], parent_id: str = "", path: tuple[str, ...] = ()) -> None:
        node_id = str(node.get("id", "")).strip()
        name = str(node.get("name", "")).strip()
        current_path = (*path, name)
        nodes.append({
            "id": node_id,
            "name": name,
            "parent_id": parent_id,
            "path": " > ".join(part for part in current_path if part),
            "depth": len(current_path) - 1,
            "node_type": "root" if not parent_id else "main_tree",
            "definition": str(node.get("definition", "")).strip(),
            "keywords": [str(value).strip() for value in node.get("representative_keywords", []) if str(value).strip()],
            "assignable": len(current_path) == 3 and not bool(node.get("children")),
        })
        for child in node.get("children", []):
            visit(child, node_id, current_path)

    if isinstance(reference.get("root"), dict):
        visit(reference["root"])
    boundary_paths = {
        "BOUNDARY_HYDROPOWER": "新能源边界领域 > 水能与传统储能 > 常规水电与抽水蓄能",
        "BOUNDARY_NUCLEAR": "新能源边界领域 > 非化石能源 > 核能",
        "BOUNDARY_NEV": "新能源边界领域 > 交通能源应用 > 新能源汽车",
    }
    for key, node_type in (("boundary_candidates", "boundary"), ("cross_cutting_dimensions", "dimension")):
        for item in reference.get(key, []):
            path = boundary_paths.get(str(item.get("id", "")), str(item.get("name", "")).strip())
            nodes.append({
                "id": str(item.get("id", "")).strip(),
                "name": str(item.get("name", "")).strip(),
                "parent_id": "",
                "path": path,
                "depth": len(split_topic_path(path)) - 1,
                "node_type": node_type,
                "definition": str(item.get("definition", "")).strip(),
                "keywords": [str(value).strip() for value in item.get("representative_keywords", []) if str(value).strip()],
                "assignable": node_type == "boundary" and len(split_topic_path(path)) == 3,
            })
    return nodes


def flatten_dynamic_taxonomy(
    state: dict[str, Any], reference_nodes: list[dict[str, Any]] | None = None,
) -> list[dict[str, Any]]:
    """Convert the maintained taxonomy into matchable nodes while preserving seeded IDs."""
    seeded_ids = {node["path"]: node["id"] for node in (reference_nodes or []) if node.get("path")}
    active_rows = [
        row for row in state.get("topics", [])
        if row.get("status", "active") == "active" and str(row.get("path", "")).strip()
    ]
    id_by_path: dict[str, str] = {}
    for row in active_rows:
        path = " > ".join(split_topic_path(str(row.get("path", ""))))
        id_by_path[path] = seeded_ids.get(path) or f"DYN_{hashlib.sha1(path.encode('utf-8')).hexdigest()[:12].upper()}"
    nodes: list[dict[str, Any]] = []
    for row in active_rows:
        path = " > ".join(split_topic_path(str(row.get("path", ""))))
        parts = split_topic_path(path)
        parent_path = " > ".join(parts[:-1])
        nodes.append({
            "id": id_by_path[path],
            "name": str(row.get("name") or parts[-1]).strip(),
            "parent_id": id_by_path.get(parent_path, ""),
            "path": path,
            "depth": len(parts) - 1,
            "node_type": "main_tree",
            "scope_status": str(row.get("scope_status") or "IN_SCOPE"),
            "definition": str(row.get("definition", "")).strip(),
            "keywords": [str(value).strip() for value in row.get("keywords", []) if str(value).strip()],
            "assignable": len(parts) == 3,
        })
    return sorted(nodes, key=lambda row: (row["depth"], row["path"]))


def split_multi(value: str) -> list[str]:
    return [part.strip() for part in re.split(r"[|;,，；]+", value or "") if part.strip()]


def read_term_table(path: Path | None) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    if path is None or not path.exists() or path.stat().st_size == 0:
        return [], {"status": "ignored", "reason": "term_table_missing_or_empty", "rows": 0}
    with path.open("r", encoding="utf-8-sig", newline="") as handle:
        source_rows = list(csv.DictReader(handle))
    terms: list[dict[str, Any]] = []
    for row in source_rows:
        term = str(row.get("canonical_term") or row.get("term") or "").strip()
        status = str(row.get("status") or "approved").strip().lower()
        if not term or status in {"rejected", "disabled", "停用", "拒绝"}:
            continue
        aliases = split_multi(str(row.get("aliases") or ""))
        try:
            weight = float(row.get("term_weight") or 1.0)
        except (TypeError, ValueError):
            weight = 1.0
        terms.append({
            "term": term,
            "aliases": aliases,
            "forms": [term, *[alias for alias in aliases if alias != term]],
            "term_type": str(row.get("term_type") or "").strip(),
            "status": status,
            "source": str(row.get("source") or "").strip(),
            "topic_node_ids": split_multi(str(row.get("topic_node_ids") or "")),
            "weight": max(0.0, weight),
            "role": str(row.get("term_role") or "related").strip().lower(),
            "approved": status in {"approved", "active", "通过", "已审核"},
        })
    return terms, {
        "status": "loaded" if terms else "ignored",
        "reason": "" if terms else "no_usable_rows",
        "rows": len(source_rows),
        "usable_terms": len(terms),
        "approved_terms": sum(term["approved"] for term in terms),
        "mapped_terms": sum(bool(term["topic_node_ids"]) for term in terms),
        "source_path": str(path),
    }


def collect_terms(reference: dict[str, Any]) -> tuple[set[str], set[str]]:
    protected = set(EXTRA_PROTECTED_TERMS)

    def visit(value: Any) -> None:
        if isinstance(value, dict):
            name = str(value.get("name", "")).strip()
            if len(name) >= 2:
                protected.add(name)
            for keyword in value.get("representative_keywords", []):
                keyword = str(keyword).strip()
                if len(keyword) >= 2:
                    protected.add(keyword)
            for child in value.values():
                visit(child)
        elif isinstance(value, list):
            for child in value:
                visit(child)

    visit(reference.get("root", {}))
    visit(reference.get("boundary_candidates", []))
    visit(reference.get("cross_cutting_dimensions", []))
    excluded = {
        str(keyword).strip()
        for item in reference.get("excluded_label_types", [])
        for keyword in item.get("representative_keywords", [])
        if str(keyword).strip()
    }
    return protected, excluded


def compact_structure_text(text: str) -> str:
    """Normalize a short structural marker without altering ordinary analysis text."""
    return re.sub(r"[\s\u3000:：\-—_·•.。]+", "", unicodedata.normalize("NFKC", text)).strip()


def is_structural_noise(text: str, exact_texts: list[str], regex_patterns: list[str]) -> bool:
    compact = compact_structure_text(text)
    normalized_exact = {compact_structure_text(value) for value in exact_texts if str(value).strip()}
    return compact in normalized_exact or any(re.fullmatch(pattern, compact) for pattern in regex_patterns)


def build_units(
    path: Path, minimum: int, maximum: int,
    structural_noise_exact: list[str] | None = None,
    structural_noise_regex: list[str] | None = None,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], str]:
    sections = read_jsonl(path)
    sentence_mode = any(str(row.get("sentence_id", "")).strip() for row in sections)
    parent_ids = {
        str(row.get("parent_section_id", "")).strip()
        for row in sections
        if str(row.get("parent_section_id", "")).strip()
    }
    units: list[dict[str, Any]] = []
    excluded: list[dict[str, Any]] = []
    for row in sections:
        section_id = str(row.get("section_id", "")).strip()
        sentence_id = str(row.get("sentence_id", "")).strip()
        unit_id = sentence_id if sentence_mode else section_id
        heading = str(row.get("section_path") if sentence_mode else row.get("heading", "")).strip()
        heading_path = "" if sentence_mode else str(row.get("heading_path", "")).strip()
        body = str(row.get("text", "")).strip()
        if not sentence_mode and heading and body.startswith(heading):
            body = body[len(heading):].lstrip()
        text = body if sentence_mode else "\n".join(part for part in (heading_path or heading, body) if part).strip()
        reason = ""
        if sentence_mode and row.get("is_heading") is True:
            reason = "heading_sentence"
        elif sentence_mode and is_structural_noise(
            text, structural_noise_exact or [], structural_noise_regex or []
        ):
            reason = "structural_noise"
        elif not sentence_mode and row.get("section_type") == "document_root":
            reason = "document_root"
        elif not sentence_mode and section_id in parent_ids:
            reason = "non_leaf_overlapping_parent"
        elif len(text) < minimum:
            reason = "too_short"
        elif not unit_id:
            reason = "missing_sentence_id" if sentence_mode else "missing_section_id"
        if reason:
            excluded.append({"unit_id": unit_id, "doc_id": row.get("doc_id", ""), "heading": heading, "reason": reason})
        else:
            units.append({
                "unit_id": unit_id,
                "doc_id": str(row.get("doc_id", "")),
                "heading": heading,
                "section_id": section_id,
                "paragraph_id": str(row.get("paragraph_id", "")).strip(),
                "sentence_index": row.get("sentence_index", ""),
                "analysis_text": text[:maximum],
            })
    return units, excluded, "sentence_level" if sentence_mode else "leaf_sections_only"


def build_representation_texts(
    units: list[dict[str, Any]], protected: set[str], excluded: set[str]
) -> tuple[list[str], dict[str, Any]]:
    import jieba

    speaker_pattern = re.compile(r"(?:^|\n)\s*([\u4e00-\u9fff]{2,4})\s*[：:]")
    speakers = {
        match.group(1)
        for unit in units
        for match in speaker_pattern.finditer(unicodedata.normalize("NFKC", unit["analysis_text"]))
    }
    stopwords = STOPWORDS | speakers | (excluded - protected)
    for term in sorted(protected, key=len, reverse=True):
        jieba.add_word(term, freq=2_000_000)

    lesson_pattern = re.compile(r"第\s*[0-9一二三四五六七八九十百]+\s*[章节课]")
    enumerator_pattern = re.compile(r"(?:^|\s)[（(]?[0-9一二三四五六七八九十百]+[）)、.．、]?\s*")
    non_text_pattern = re.compile(r"[^0-9A-Za-z\u4e00-\u9fff+#]+")
    valid_pattern = re.compile(r"[A-Za-z\u4e00-\u9fff]")
    raw_tokens: list[list[str]] = []
    document_frequency: Counter[str] = Counter()
    for unit in units:
        text = unicodedata.normalize("NFKC", unit["analysis_text"])
        text = non_text_pattern.sub(" ", enumerator_pattern.sub(" ", lesson_pattern.sub(" ", text)))
        tokens: list[str] = []
        for token in jieba.lcut(text, cut_all=False):
            token = token.strip()
            if not token or token.isdigit() or not valid_pattern.search(token):
                continue
            if token in protected:
                tokens.append(token)
            elif token not in stopwords and len(token) >= 2:
                tokens.append(token)
        raw_tokens.append(tokens)
        document_frequency.update(set(tokens))

    threshold = max(2, int(len(units) * 0.55))
    frequent = {
        token for token, count in document_frequency.items()
        if count > threshold and token not in protected
    }
    texts = [" ".join(token for token in tokens if token not in frequent) for tokens in raw_tokens]
    empty = [index for index, text in enumerate(texts) if not text]
    for index in empty:
        texts[index] = "无有效代表词"
    return texts, {
        "stopword_count": len(stopwords),
        "detected_speaker_names": sorted(speakers),
        "protected_term_count": len(protected),
        "excluded_term_count": len(excluded - protected),
        "high_frequency_removed": sorted(frequent),
        "empty_representation_units": len(empty),
    }


def keyword_match(text: str, nodes: list[dict[str, Any]]) -> tuple[str, str, float]:
    scores: list[tuple[int, int, str, str]] = []
    lowered = text.casefold()
    for node in nodes:
        score = sum(lowered.count(word.casefold()) for word in node["keywords"])
        if score:
            scores.append((score, len(node["path"]), node["id"], node["name"]))
    if not scores:
        return "", "", 0.0
    score, _, node_id, name = max(scores)
    return node_id, name, float(score)


def node_prototype(node: dict[str, Any], term_rows: list[dict[str, Any]]) -> str:
    linked_terms = [
        form
        for term in term_rows
        if term["approved"] and node["id"] in term["topic_node_ids"] and term["role"] != "negative"
        for form in term["forms"]
    ]
    return " ".join([
        node["path"], node["name"], node["definition"], *node["keywords"], *linked_terms,
    ]).strip()


def score_node_candidates(
    *, text: str, vector: Any, nodes: list[dict[str, Any]], prototype_vectors: Any,
    term_rows: list[dict[str, Any]], np: Any, allowed_types: set[str], limit: int = 5,
    weights: dict[str, float] | None = None,
) -> list[dict[str, Any]]:
    weights = weights or DEFAULT_CONFIG["matching"]["weights"]
    lowered = text.casefold()
    scored: list[dict[str, Any]] = []
    for index, node in enumerate(nodes):
        if node["node_type"] not in allowed_types or node["node_type"] == "root":
            continue
        if node.get("assignable") is False:
            continue
        keyword_hits = []
        if node["name"] and node["name"].casefold() in lowered:
            keyword_hits.append(node["name"])
        keyword_hits.extend(
            keyword for keyword in node["keywords"]
            if keyword.casefold() in lowered and keyword not in keyword_hits
        )
        keyword_score = min(1.0, (2.0 * (node["name"] in keyword_hits) + max(0, len(keyword_hits) - (node["name"] in keyword_hits))) / 4.0)
        term_hits: list[str] = []
        positive_weight = 0.0
        negative_weight = 0.0
        for term in term_rows:
            if not term["approved"] or node["id"] not in term["topic_node_ids"]:
                continue
            matched_forms = [form for form in term["forms"] if form.casefold() in lowered]
            if not matched_forms:
                continue
            term_hits.extend(matched_forms)
            if term["role"] == "negative":
                negative_weight += term["weight"]
            else:
                positive_weight += term["weight"] * (1.25 if term["role"] == "core" else 1.0)
        term_score = max(0.0, min(1.0, positive_weight / 2.5) - min(0.5, negative_weight / 2.0))
        vector_score = max(0.0, float(np.dot(vector, prototype_vectors[index])))
        hierarchy_score = min(1.0, 0.35 + 0.25 * int(node["depth"])) if keyword_hits or term_hits else 0.0
        composite = (
            float(weights["vector"]) * vector_score
            + float(weights["approved_terms"]) * term_score
            + float(weights["taxonomy_keywords"]) * keyword_score
            + float(weights["hierarchy"]) * hierarchy_score
        )
        scored.append({
            "node_id": node["id"], "name": node["name"], "path": node["path"],
            "node_type": node["node_type"], "depth": node["depth"],
            "scope_status": str(node.get("scope_status") or "IN_SCOPE"),
            "score": round(composite, 6), "vector_score": round(vector_score, 6),
            "term_score": round(term_score, 6), "keyword_score": round(keyword_score, 6),
            "hierarchy_score": round(hierarchy_score, 6),
            "keyword_hits": sorted(set(keyword_hits)), "term_hits": sorted(set(term_hits)),
        })
    return sorted(scored, key=lambda row: (row["score"], row["depth"], row["node_id"]), reverse=True)[:limit]


def select_cluster_matches(candidates: list[dict[str, Any]], match_threshold: float) -> list[dict[str, Any]]:
    """Select every independently qualified topic; a close second no longer invalidates the first."""
    return [
        candidate for candidate in candidates
        if candidate["score"] >= match_threshold
        and bool(candidate.get("keyword_hits") or candidate.get("term_hits"))
    ]


def select_cluster_prior_topics(
    candidates: list[dict[str, Any]], prior_config: dict[str, Any],
) -> list[dict[str, Any]]:
    """Keep the coherent cluster tendencies that are strong enough to guide sentences."""
    if not prior_config.get("enabled", True) or not candidates:
        return []
    minimum = float(prior_config["minimum_score"])
    vector_minimum = float(prior_config["vector_evidence_threshold"])
    eligible = [
        candidate for candidate in candidates
        if float(candidate.get("score", 0.0)) >= minimum
        and bool(
            candidate.get("keyword_hits") or candidate.get("term_hits")
            or float(candidate.get("vector_score", 0.0)) >= vector_minimum
        )
    ]
    if not eligible:
        return []
    top_score = float(eligible[0]["score"])
    window = float(prior_config["score_window"])
    return [
        copy.deepcopy(candidate) for candidate in eligible
        if float(candidate["score"]) >= top_score - window
    ][:int(prior_config["max_topics"])]


def classify_sentence_candidates(
    candidates: list[dict[str, Any]], cluster_prior_topics: list[dict[str, Any]],
    matching_config: dict[str, Any], prior_config: dict[str, Any],
    node_by_id: dict[str, dict[str, Any]],
) -> dict[str, Any]:
    """Add a bounded cluster prior, then classify while preserving deviations and multi-labels."""
    prior_by_id = {row["node_id"]: float(row["score"]) for row in cluster_prior_topics}
    prior_weight = float(prior_config["weight"]) if prior_config.get("enabled", True) else 0.0
    adjusted: list[dict[str, Any]] = []
    for source in candidates:
        candidate = copy.deepcopy(source)
        sentence_score = float(candidate.get("score", 0.0))
        cluster_score = prior_by_id.get(candidate.get("node_id", ""), 0.0)
        contribution = prior_weight * cluster_score
        candidate["sentence_score"] = round(sentence_score, 6)
        candidate["cluster_prior_score"] = round(cluster_score, 6)
        candidate["cluster_prior_contribution"] = round(contribution, 6)
        candidate["score"] = round(min(1.0, sentence_score + contribution), 6)
        adjusted.append(candidate)
    adjusted.sort(key=lambda row: (row["score"], row["depth"], row["node_id"]), reverse=True)
    first = adjusted[0] if adjusted else None
    second_score = float(adjusted[1]["score"]) if len(adjusted) > 1 else 0.0
    margin = round(float(first["score"]) - second_score, 6) if first else 0.0
    evidence_present = bool(first and (
        first.get("keyword_hits") or first.get("term_hits")
        or float(first.get("vector_score", 0.0)) >= float(matching_config["vector_evidence_threshold"])
        or float(first.get("cluster_prior_score", 0.0)) >= float(prior_config["minimum_score"])
    ))
    if (
        first and float(first["score"]) >= float(matching_config["unit_match_threshold"])
        and margin >= float(matching_config["unit_margin_threshold"])
        and evidence_present
    ):
        status = "classified"
    elif first and float(first["score"]) >= float(matching_config["unit_review_threshold"]):
        status = "needs_review"
    else:
        status = "unknown"
    secondary: list[dict[str, Any]] = []
    if first:
        for candidate in adjusted[1:]:
            if len(secondary) >= int(matching_config["max_secondary_topics"]):
                break
            if float(candidate["score"]) < max(
                float(matching_config["unit_review_threshold"]),
                float(first["score"]) - float(matching_config["secondary_score_window"]),
            ):
                continue
            if is_ancestor_or_descendant(first["node_id"], candidate["node_id"], node_by_id):
                continue
            secondary.append(candidate)
    cluster_primary = cluster_prior_topics[0] if cluster_prior_topics else {}
    return {
        "classification_status": status,
        "primary_topic": first or {},
        "secondary_topics": secondary,
        "topic_match_margin": margin,
        "topic_candidates": adjusted,
        "cluster_primary_topic": cluster_primary,
        "cluster_prior_topics": cluster_prior_topics,
        "sentence_deviates_from_cluster": bool(
            first and cluster_primary and first.get("node_id") != cluster_primary.get("node_id")
        ),
    }


def select_review_candidates(
    candidates: list[dict[str, Any]],
    matched_candidates: list[dict[str, Any]],
    reference_limit: int = 3,
) -> list[tuple[int, dict[str, Any], bool]]:
    """Return one review item per reference candidate, plus any formal match beyond the reference limit."""
    matched_ids = {candidate["node_id"] for candidate in matched_candidates}
    return [
        (rank, candidate, candidate["node_id"] in matched_ids)
        for rank, candidate in enumerate(candidates, start=1)
        if rank <= reference_limit or candidate["node_id"] in matched_ids
    ]


def split_topic_path(value: str) -> list[str]:
    return [part.strip() for part in re.split(r"\s*>\s*", value or "") if part.strip()]


def build_nested_taxonomy(topic_records: list[dict[str, Any]], status: str) -> dict[str, Any]:
    """Merge reviewed or preliminary topic paths into one nested corpus taxonomy."""
    roots: list[dict[str, Any]] = []

    def child_named(children: list[dict[str, Any]], name: str) -> dict[str, Any]:
        for child in children:
            if child["name"] == name:
                return child
        child = {"name": name, "children": [], "source_clusters": [], "representative_words": [], "representative_texts": []}
        children.append(child)
        return child

    accepted_paths = 0
    for record in topic_records:
        parts = split_topic_path(str(record.get("topic_path", "")))
        if not parts:
            continue
        accepted_paths += 1
        children = roots
        node: dict[str, Any] | None = None
        for level, name in enumerate(parts, start=1):
            node = child_named(children, name)
            node["level"] = level
            node["path"] = " > ".join(parts[:level])
            children = node["children"]
        if node is None:
            continue
        cluster_id = str(record.get("cluster_id", "")).strip()
        if cluster_id and cluster_id not in node["source_clusters"]:
            node["source_clusters"].append(cluster_id)
        for word in record.get("representative_words", []):
            if word not in node["representative_words"]:
                node["representative_words"].append(word)
        for sample in record.get("representative_texts", []):
            if sample not in node["representative_texts"]:
                node["representative_texts"].append(sample)

    def clean(node: dict[str, Any]) -> dict[str, Any]:
        result = {
            "level": node["level"], "name": node["name"], "path": node["path"],
            "children": [clean(child) for child in node["children"]],
        }
        if node["source_clusters"]:
            result["source_clusters"] = sorted(node["source_clusters"])
            result["representative_words"] = node["representative_words"]
            result["representative_texts"] = node["representative_texts"]
        return result

    return {
        "status": status,
        "accepted_path_count": accepted_paths,
        "topics": [clean(node) for node in roots],
    }


def reviewed_topic_records(
    rows: list[dict[str, str]],
    cluster_evidence: dict[str, dict[str, Any]],
) -> tuple[list[dict[str, Any]], dict[str, int]]:
    accepted: list[dict[str, Any]] = []
    counts: Counter[str] = Counter()
    pass_values = {"通过", "pass", "approved", "accept"}
    assign_existing_values = {"归入现有", "assign_existing", "existing"}
    reject_values = {"不通过", "reject", "rejected", "fail"}
    add_values = {"新增", "add", "new"}
    for row in rows:
        decision = str(row.get("review_decision", "")).strip().casefold()
        revised_path = str(row.get("final_topic_path") or row.get("revised_topic_path") or "").strip()
        matched_path = str(row.get("matched_topic_path", "")).strip()
        reference_path = str(row.get("reference_topic_path", "")).strip()
        if decision in pass_values:
            final_path = revised_path or matched_path or reference_path
            counts["passed"] += 1
        elif decision in assign_existing_values:
            final_path = revised_path
            counts["assigned_existing"] += 1
        elif decision in add_values:
            final_path = revised_path
            counts["added"] += 1
        elif decision in reject_values:
            final_path = revised_path
            counts["rejected"] += 1
            if final_path:
                counts["revised"] += 1
        else:
            counts["pending"] += 1
            continue
        if not final_path:
            counts["missing_final_path"] += 1
            continue
        cluster_id = str(row.get("cluster_id", "")).strip()
        evidence = cluster_evidence.get(cluster_id, {})
        accepted.append({
            "cluster_id": cluster_id,
            "topic_path": final_path,
            "representative_words": evidence.get("representative_words", []),
            "representative_texts": evidence.get("representative_texts", []),
        })
    return accepted, dict(counts)


def build_sentence_review_rows(
    assignments: list[dict[str, Any]], reference_limit: int = 3,
    review_threshold: float = 0.40, vector_evidence_threshold: float = 0.70,
) -> list[dict[str, Any]]:
    """Expand only qualified candidates; unknown sentences receive one blank review row."""
    rows: list[dict[str, Any]] = []
    for assignment in assignments:
        ranked_candidates = list(enumerate(assignment.get("topic_candidates", []), start=1))
        status = assignment.get("classification_status", "")
        qualified = [
            (rank, candidate) for rank, candidate in ranked_candidates
            if float(candidate.get("score", 0.0)) >= review_threshold
            and bool(
                candidate.get("keyword_hits") or candidate.get("term_hits")
                or float(candidate.get("vector_score", 0.0)) >= vector_evidence_threshold
            )
        ]
        if status == "classified":
            primary_id = assignment.get("primary_topic", {}).get("node_id", "")
            accepted_primary = [
                item for item in ranked_candidates if item[1].get("node_id", "") == primary_id
            ][:1]
            selected = accepted_primary + [
                item for item in qualified if item[1].get("node_id", "") != primary_id
            ]
            review_candidates = selected[:reference_limit]
        elif status == "needs_review":
            review_candidates = qualified[:reference_limit]
        else:
            review_candidates = []
        review_items: list[tuple[int | str, dict[str, Any] | None]] = review_candidates or [("", None)]
        for rank, candidate in review_items:
            accepted = bool(
                candidate
                and status == "classified"
                and candidate.get("node_id", "") == assignment.get("primary_topic", {}).get("node_id", "")
            )
            rows.append({
                "review_id": f"{assignment['unit_id']}::{candidate['node_id'] if candidate else 'UNMATCHED'}",
                "sentence_id": assignment["unit_id"],
                "doc_id": assignment["doc_id"],
                "section_id": assignment.get("section_id", ""),
                "paragraph_id": assignment.get("paragraph_id", ""),
                "cluster_id": assignment["cluster_id"],
                "sentence_text": assignment.get("excerpt", ""),
                "classification_status": assignment.get("classification_status", ""),
                "review_scope": "audit" if assignment.get("classification_status") == "classified" else "required",
                "reference_rank": rank,
                "reference_topic_path": candidate.get("path", "") if candidate else "",
                "combined_score": candidate.get("score", "") if candidate else "",
                "matched_topic_path": candidate.get("path", "") if accepted and candidate else "",
                "review_decision": "",
                "inappropriate_labels": "",
                "revised_topic_path": "",
                "revision_comment": "",
                "new_topic_definition": "",
                "new_topic_keywords": "",
            })
    return rows


def is_ancestor_or_descendant(first_id: str, second_id: str, node_by_id: dict[str, dict[str, Any]]) -> bool:
    def ancestors(node_id: str) -> set[str]:
        result: set[str] = set()
        current = node_by_id.get(node_id)
        while current and current.get("parent_id"):
            parent_id = current["parent_id"]
            result.add(parent_id)
            current = node_by_id.get(parent_id)
        return result
    return first_id in ancestors(second_id) or second_id in ancestors(first_id)


def import_stack(reduction_method: str, umap_import_timeout: int = 20) -> tuple[Any, ...]:
    reduction_method = reduction_method.lower()
    UMAP = None
    if reduction_method == "umap":
        try:
            subprocess.run(
                [sys.executable, "-c", "from umap import UMAP"],
                check=True, timeout=umap_import_timeout, capture_output=True, text=True,
            )
        except subprocess.TimeoutExpired as error:
            raise RuntimeError(
                f"UMAP 导入超过 {umap_import_timeout} 秒。请修复 umap/numba/pynndescent 环境，"
                "或将 dimensionality_reduction.method 改为 pca。"
            ) from error
        except subprocess.CalledProcessError as error:
            raise RuntimeError(f"UMAP 导入失败：{error.stderr.strip()}") from error
        from umap import UMAP as ImportedUMAP
        UMAP = ImportedUMAP
    else:
        placeholder = types.ModuleType("umap")
        placeholder.UMAP = object
        sys.modules.setdefault("umap", placeholder)
    import numpy as np
    from bertopic import BERTopic
    from bertopic.vectorizers import ClassTfidfTransformer
    from hdbscan import HDBSCAN
    from sentence_transformers import SentenceTransformer
    from sklearn.decomposition import PCA
    from sklearn.feature_extraction.text import CountVectorizer
    return np, BERTopic, ClassTfidfTransformer, HDBSCAN, SentenceTransformer, PCA, UMAP, CountVectorizer


def build_reducer(config: dict[str, Any], PCA: Any, UMAP: Any, unit_count: int, embedding_dimensions: int) -> Any:
    reduction = config["dimensionality_reduction"]
    method = str(reduction["method"]).lower()
    seed = int(config["random_seed"])
    if method == "umap":
        options = reduction["umap"]
        return UMAP(
            n_neighbors=min(int(options["n_neighbors"]), max(2, unit_count - 1)),
            n_components=min(int(options["n_components"]), max(2, unit_count - 2)),
            min_dist=float(options["min_dist"]),
            metric=str(options["metric"]),
            low_memory=bool(options.get("low_memory", True)),
            random_state=seed,
        )
    options = reduction["pca"]
    components = min(int(options["n_components"]), unit_count - 1, embedding_dimensions)
    return PCA(n_components=max(2, components), whiten=bool(options.get("whiten", False)), random_state=seed)


def configure_numba_cache(config: dict[str, Any], project_root: Path) -> Path | None:
    """Put Numba's JIT cache in a writable project directory before UMAP imports."""
    if str(config["dimensionality_reduction"]["method"]).lower() != "umap":
        return None
    configured = str(
        os.environ.get("CORPUS_NUMBA_CACHE_DIR")
        or config["dimensionality_reduction"]["umap"].get("numba_cache_dir", ".cache/numba")
    ).strip()
    cache_path = Path(configured).expanduser()
    if not cache_path.is_absolute():
        cache_path = project_root / cache_path
    cache_path.mkdir(parents=True, exist_ok=True)
    probe = cache_path / ".write_probe"
    try:
        probe.write_text("ok", encoding="utf-8")
        probe.unlink()
    except OSError as error:
        raise RuntimeError(f"Numba 缓存目录不可写：{cache_path}") from error
    os.environ["NUMBA_CACHE_DIR"] = str(cache_path.resolve())
    return cache_path.resolve()


def is_noise(term: str) -> bool:
    parts = term.split()
    return not parts or all(part in STOPWORDS for part in parts) or any(
        part.isdigit() or not re.search(r"[A-Za-z\u4e00-\u9fff]", part) for part in parts
    )


def normalize_representative_term(term: str) -> str:
    """Compact only Chinese token boundaries while preserving meaningful English spaces."""
    normalized = re.sub(r"\s+", " ", str(term or "").strip())
    return re.sub(r"(?<=[\u4e00-\u9fff])\s+(?=[\u4e00-\u9fff])", "", normalized)


def normalize_representative_words(
    topic_terms: list[tuple[str, float]], top_n: int,
) -> list[dict[str, Any]]:
    """Prepare readable/matchable topic words and retain raw c-TF-IDF terms for audit only."""
    by_term: dict[str, dict[str, Any]] = {}
    for raw_term, score in topic_terms:
        term = normalize_representative_term(raw_term)
        if not term:
            continue
        value = round(float(score), 6)
        existing = by_term.get(term)
        if existing is None or value > float(existing["score"]):
            by_term[term] = {"term": term, "raw_term": str(raw_term), "score": value}
    return sorted(by_term.values(), key=lambda row: (-float(row["score"]), row["term"]))[:top_n]


def main() -> int:
    root = Path(__file__).resolve().parent
    parser = argparse.ArgumentParser(description="独立话题识别 Demo")
    parser.add_argument("--sections", type=Path, required=True)
    parser.add_argument("--taxonomy", type=Path, default=root.parent / "configs" / "topics" / "reference_taxonomy.json")
    parser.add_argument("--term-table", type=Path, default=root / "configs" / "topic_seed_terms.csv")
    parser.add_argument("--config", type=Path, default=root / "configs" / "bertopic.json")
    parser.add_argument(
        "--dynamic-taxonomy", type=Path, default=root / "configs" / "dynamic_topic_taxonomy.json",
        help="运行中持续维护的话题体系状态文件",
    )
    parser.add_argument("--output-root", type=Path, default=root / "results" / "topic_demo")
    parser.add_argument("--review-file", type=Path, default=None, help="已完成人工/大模型标注的审核 CSV；提供后生成最终嵌套体系")
    parser.add_argument("--min-cluster-size", type=int, default=None, help="临时覆盖配置中的 clustering.min_cluster_size")
    parser.add_argument("--min-samples", type=int, default=None, help="临时覆盖配置中的 clustering.min_samples")
    parser.add_argument("--match-threshold", type=float, default=None, help="临时覆盖配置中的 matching.cluster_match_threshold")
    parser.add_argument("--unit-match-threshold", type=float, default=None, help="临时覆盖配置中的 matching.unit_match_threshold")
    parser.add_argument("--unit-review-threshold", type=float, default=None, help="临时覆盖配置中的 matching.unit_review_threshold")
    parser.add_argument("--sample-size", type=int, default=None, help="临时覆盖配置中的 sampling.sample_size；设为0可关闭抽样")
    parser.add_argument("--sampling-strategy", choices=("balanced_systematic", "balanced_random"), default=None)
    parser.add_argument(
        "--stage", choices=("taxonomy_build", "sentence_classification"), default=None,
        help="taxonomy_build 只构建/审核体系；sentence_classification 使用动态体系进行正式单句分类",
    )
    args = parser.parse_args()

    reference = read_json(args.taxonomy) if args.taxonomy.exists() and args.taxonomy.stat().st_size else {}
    config = load_bertopic_config(args.config)
    if args.min_cluster_size is not None:
        config["clustering"]["min_cluster_size"] = args.min_cluster_size
    if args.min_samples is not None:
        config["clustering"]["min_samples"] = args.min_samples
    if args.match_threshold is not None:
        config["matching"]["cluster_match_threshold"] = args.match_threshold
    if args.unit_match_threshold is not None:
        config["matching"]["unit_match_threshold"] = args.unit_match_threshold
    if args.unit_review_threshold is not None:
        config["matching"]["unit_review_threshold"] = args.unit_review_threshold
    if args.sample_size is not None:
        config["sampling"]["enabled"] = args.sample_size > 0
        if args.sample_size > 0:
            config["sampling"]["sample_size"] = args.sample_size
    if args.sampling_strategy is not None:
        config["sampling"]["strategy"] = args.sampling_strategy
    validate_bertopic_config(config)
    workflow_stage = args.stage or str(config["workflow"]["default_stage"])
    clustering_config = config["clustering"]
    representation_config = config["representation"]
    matching_config = config["matching"]
    matching_weights = matching_config["weights"]
    reference_nodes = flatten_taxonomy(reference)
    if args.dynamic_taxonomy.exists():
        pre_normalized, pre_changed = normalize_three_level_taxonomy(read_taxonomy(args.dynamic_taxonomy))
        if pre_changed:
            write_taxonomy(args.dynamic_taxonomy, pre_normalized)
    dynamic_taxonomy = initialize_or_merge_taxonomy(
        args.dynamic_taxonomy, reference_nodes,
        taxonomy_name=str(reference.get("taxonomy_name") or "动态话题体系"),
    )
    dynamic_taxonomy, taxonomy_normalized = normalize_three_level_taxonomy(dynamic_taxonomy)
    if taxonomy_normalized:
        write_taxonomy(args.dynamic_taxonomy, dynamic_taxonomy)
    nodes = (
        flatten_dynamic_taxonomy(dynamic_taxonomy, reference_nodes)
        if workflow_stage == "sentence_classification" else reference_nodes
    )
    if workflow_stage == "sentence_classification" and not nodes:
        raise RuntimeError("正式单句分类需要非空的动态话题体系，请先完成 taxonomy_build 阶段的审核")
    protected, excluded_terms = collect_terms(reference)
    if workflow_stage == "sentence_classification":
        protected.update(
            value
            for row in dynamic_taxonomy.get("topics", [])
            if row.get("status", "active") == "active"
            for value in [str(row.get("name", "")).strip(), *[
                str(keyword).strip() for keyword in row.get("keywords", [])
            ]]
            if value
        )
    term_rows, term_table_audit = read_term_table(args.term_table)
    protected.update(
        form for term in term_rows for form in term["forms"] if term["status"] not in {"rejected", "disabled"}
    )
    units, excluded_units, unit_policy = build_units(
        args.sections,
        int(config["input"]["min_analysis_characters"]),
        int(config["input"]["max_analysis_characters"]),
        config["input"]["structural_noise_exact"] if config["input"]["exclude_structural_noise"] else [],
        config["input"]["structural_noise_regex"] if config["input"]["exclude_structural_noise"] else [],
    )
    eligible_before_sampling = len(units)
    if config["sampling"]["enabled"]:
        units, sampling_audit = sample_units_by_doc(
            units,
            int(config["sampling"]["sample_size"]),
            strategy=str(config["sampling"]["strategy"]),
            seed=int(config["sampling"]["seed"]),
        )
    else:
        sampling_audit = {
            "status": "disabled", "strategy": "none",
            "eligible_size": eligible_before_sampling, "sampled_size": len(units),
            "document_count": len({unit["doc_id"] for unit in units}),
            "documents_represented": len({unit["doc_id"] for unit in units}),
        }
    if len(units) < 6:
        unit_name = "有效句子" if unit_policy == "sentence_level" else "有效叶子章节"
        raise RuntimeError(f"Demo 至少需要6个{unit_name}")
    representation_texts, representation_audit = build_representation_texts(units, protected, excluded_terms)

    reduction_method = str(config["dimensionality_reduction"]["method"]).lower()
    configure_numba_cache(config, root)
    umap_timeout = int(config["dimensionality_reduction"]["umap"].get("import_timeout_seconds", 20))
    np, BERTopic, ClassTfidfTransformer, HDBSCAN, SentenceTransformer, PCA, UMAP, CountVectorizer = import_stack(
        reduction_method, umap_timeout
    )
    embedder = SentenceTransformer(config["embedding_model"])
    embeddings = embedder.encode([unit["analysis_text"] for unit in units], show_progress_bar=True, normalize_embeddings=True)
    reducer = build_reducer(config, PCA, UMAP, len(units), int(embeddings.shape[1]))
    ngram_range = tuple(int(value) for value in representation_config["ngram_range"])
    model = BERTopic(
        embedding_model=embedder,
        umap_model=reducer,
        hdbscan_model=HDBSCAN(
            min_cluster_size=int(clustering_config["min_cluster_size"]),
            min_samples=int(clustering_config["min_samples"]),
            metric=str(clustering_config["metric"]),
            cluster_selection_method=str(clustering_config["cluster_selection_method"]),
            cluster_selection_epsilon=float(clustering_config["cluster_selection_epsilon"]),
            prediction_data=bool(clustering_config["prediction_data"]),
        ),
        vectorizer_model=CountVectorizer(
            analyzer="word", tokenizer=str.split, preprocessor=None, token_pattern=None, lowercase=False,
            ngram_range=ngram_range, min_df=representation_config["min_df"], max_df=representation_config["max_df"],
        ),
        ctfidf_model=ClassTfidfTransformer(
            bm25_weighting=bool(representation_config["bm25_weighting"]),
            reduce_frequent_words=bool(representation_config["reduce_frequent_words"]),
        ),
        top_n_words=int(representation_config["top_words"]),
        calculate_probabilities=bool(config["bertopic"]["calculate_probabilities"]),
        verbose=bool(config["bertopic"]["verbose"]),
    )
    raw_topics, _ = model.fit_transform(representation_texts, embeddings=embeddings)
    topics = [int(value) for value in raw_topics]
    probabilities = getattr(model.hdbscan_model, "probabilities_", [0.0] * len(topics))
    prototype_texts = [node_prototype(node, term_rows) for node in nodes]
    prototype_vectors = (
        embedder.encode(prototype_texts, show_progress_bar=False, normalize_embeddings=True)
        if prototype_texts else np.empty((0, embeddings.shape[1]))
    )
    node_by_id = {node["id"]: node for node in nodes}
    by_topic: dict[int, list[int]] = defaultdict(list)
    for index, topic in enumerate(topics):
        by_topic[topic].append(index)

    centroids: dict[int, Any] = {}
    for topic, indexes in by_topic.items():
        if topic == -1:
            continue
        centroid = np.asarray(embeddings[indexes]).mean(axis=0)
        norm = float(np.linalg.norm(centroid))
        centroids[topic] = centroid / norm if norm else centroid

    similarities: list[float] = []
    margins: list[float] = []
    for index, topic in enumerate(topics):
        if topic == -1:
            similarities.append(0.0)
            margins.append(0.0)
            continue
        scores = {candidate: float(np.dot(embeddings[index], centroid)) for candidate, centroid in centroids.items()}
        own = scores[topic]
        second = max((score for candidate, score in scores.items() if candidate != topic), default=0.0)
        similarities.append(round(own, 6))
        margins.append(round(own - second, 6))

    assignments: list[dict[str, Any]] = []
    for index, topic in enumerate(topics):
        primary_candidates = score_node_candidates(
            text=units[index]["analysis_text"], vector=embeddings[index], nodes=nodes,
            prototype_vectors=prototype_vectors, term_rows=term_rows, np=np,
            allowed_types={"main_tree", "boundary"},
            limit=max(int(matching_config["unit_candidate_limit"]), len(nodes)), weights=matching_weights,
        ) if nodes else []
        dimension_candidates = score_node_candidates(
            text=units[index]["analysis_text"], vector=embeddings[index], nodes=nodes,
            prototype_vectors=prototype_vectors, term_rows=term_rows, np=np,
            allowed_types={"dimension"},
            limit=int(matching_config["dimension_candidate_limit"]), weights=matching_weights,
        ) if nodes else []
        dimensions = [
            candidate for candidate in dimension_candidates
            if candidate["score"] >= float(matching_config["dimension_threshold"])
            and (
                candidate["keyword_hits"] or candidate["term_hits"]
                or candidate["vector_score"] >= float(matching_config["dimension_vector_threshold"])
            )
        ][:int(matching_config["max_dimensions"])]
        assignments.append({
            "unit_id": units[index]["unit_id"], "doc_id": units[index]["doc_id"], "heading": units[index]["heading"],
            "section_id": units[index].get("section_id", ""),
            "paragraph_id": units[index].get("paragraph_id", ""),
            "sentence_index": units[index].get("sentence_index", ""),
            "cluster_id": "BT_OUTLIER" if topic == -1 else f"BT_TOPIC_{topic:03d}", "bertopic_id": topic,
            "membership_confidence": round(float(probabilities[index]), 6),
            "centroid_similarity": similarities[index], "cluster_margin": margins[index],
            "classification_stage": "formal" if workflow_stage == "sentence_classification" else "provisional",
            "classification_status": "pending_cluster_prior",
            "primary_topic": {}, "secondary_topics": [],
            "cross_cutting_dimensions": dimensions, "topic_match_margin": 0.0,
            "topic_candidates": primary_candidates,
            "representation_text": representation_texts[index], "excerpt": units[index]["analysis_text"][:500],
        })

    summaries: list[dict[str, Any]] = []
    review_rows: list[dict[str, Any]] = []
    cluster_priors_by_id: dict[str, list[dict[str, Any]]] = {}
    for topic, indexes in sorted(by_topic.items()):
        cluster_id = "BT_OUTLIER" if topic == -1 else f"BT_TOPIC_{topic:03d}"
        words = [] if topic == -1 else normalize_representative_words(
            model.get_topic(topic) or [], int(representation_config["top_words"])
        )
        ranked = sorted(indexes, key=lambda index: similarities[index], reverse=True)
        center_count = int(representation_config["center_samples"])
        boundary_count = int(representation_config["boundary_samples"])
        center = ranked[: min(center_count, len(ranked))]
        boundary = sorted(indexes, key=lambda index: margins[index])[: min(boundary_count, len(indexes))]
        sample = lambda index: (
            f"{units[index]['unit_id']}｜{units[index]['heading']}｜中心相似度={similarities[index]:.3f}｜"
            f"边界差值={margins[index]:.3f}｜成员概率={float(probabilities[index]):.3f}｜{units[index]['analysis_text'][:220]}"
        )
        covered = sum(
            any(word["raw_term"] in representation_texts[index] for word in words)
            for index in indexes
        )
        noise_words = [word["term"] for word in words if is_noise(word["term"])]
        evidence_text = " ".join(
            [word["term"] for word in words]
            + [units[index]["analysis_text"][:800] for index in center]
        )
        cluster_candidates = (
            score_node_candidates(
                text=evidence_text, vector=centroids[topic], nodes=nodes,
                prototype_vectors=prototype_vectors, term_rows=term_rows, np=np,
                allowed_types={"main_tree", "boundary"}, limit=max(1, len(nodes)), weights=matching_weights,
            )
            if topic != -1 and nodes else []
        )
        matched_candidates = select_cluster_matches(
            cluster_candidates, float(matching_config["cluster_match_threshold"])
        )
        cluster_prior_topics = (
            select_cluster_prior_topics(cluster_candidates, config["workflow"]["cluster_prior"])
            if topic != -1 else []
        )
        cluster_priors_by_id[cluster_id] = cluster_prior_topics
        if topic == -1:
            cluster_mapping_status = "needs_review"
        elif not nodes:
            cluster_mapping_status = "unmatched"
        elif len(matched_candidates) > 1:
            cluster_mapping_status = "matched_multiple"
        elif matched_candidates:
            cluster_mapping_status = "matched_single"
        else:
            cluster_mapping_status = "unmatched"
        representative_texts = [
            f"{units[index]['unit_id']}｜{units[index]['heading']}｜{units[index]['analysis_text'][:500]}"
            for index in center
        ]
        summary = {
            "cluster_id": cluster_id, "unit_count": len(indexes),
            "document_count": len({units[index]["doc_id"] for index in indexes}),
            "representative_words": words, "noise_words": noise_words,
            "representative_word_usable_rate": round((len(words) - len(noise_words)) / len(words), 4) if words else 0.0,
            "keyword_coverage_rate": round(covered / len(indexes), 4) if indexes else 0.0,
            "mapping_status": cluster_mapping_status,
            "matched_topics": matched_candidates,
            "cluster_prior_topics": cluster_prior_topics,
            "mapping_candidates": cluster_candidates,
            "representative_texts": representative_texts,
            "center_samples": [sample(index) for index in center],
            "boundary_samples": [sample(index) for index in boundary],
        }
        summaries.append(summary)
        review_candidates = select_review_candidates(
            cluster_candidates, matched_candidates,
            reference_limit=int(representation_config["reference_candidate_limit"]),
        )
        review_items: list[tuple[int | str, dict[str, Any] | None, bool]] = review_candidates or [("", None, False)]
        for reference_rank, candidate, is_formal_match in review_items:
            review_rows.append({
                "review_id": f"{cluster_id}::{candidate['node_id'] if candidate else 'UNMATCHED'}",
                "cluster_id": cluster_id,
                "unit_count": len(indexes),
                "representative_words": "|".join(word["term"] for word in words),
                "representative_texts": " || ".join(representative_texts),
                "matched_topic_path": candidate.get("path", "") if candidate and is_formal_match else "",
                "reference_rank": reference_rank,
                "reference_topic_path": candidate.get("path", "") if candidate else "",
                "combined_score": candidate.get("score", "") if candidate else "",
                "review_decision": "",
                "inappropriate_labels": "",
                "revised_topic_path": "",
                "revision_comment": "",
                "new_topic_definition": "",
                "new_topic_keywords": "",
            })

    for assignment in assignments:
        classified = classify_sentence_candidates(
            assignment["topic_candidates"],
            cluster_priors_by_id.get(assignment["cluster_id"], []),
            matching_config,
            config["workflow"]["cluster_prior"],
            node_by_id,
        )
        classified["topic_candidates"] = classified["topic_candidates"][
            :int(matching_config["stored_candidate_limit"])
        ]
        assignment.update(classified)

    evaluable = [summary for summary in summaries if summary["cluster_id"] != "BT_OUTLIER"]
    terms = [word["term"] for summary in evaluable for word in summary["representative_words"]]
    noise_count = sum(len(summary["noise_words"]) for summary in evaluable)
    alignment_rows: list[dict[str, Any]] = []
    for summary in evaluable:
        members = [row for row in assignments if row["cluster_id"] == summary["cluster_id"]]
        candidate_counts = Counter(
            row.get("primary_topic", {}).get("path", "") or "(no_candidate)" for row in members
        )
        top_path, top_count = candidate_counts.most_common(1)[0] if candidate_counts else ("", 0)
        alignment_rows.append({
            "cluster_id": summary["cluster_id"],
            "unit_count": len(members),
            "top_candidate_topic_path": top_path,
            "top_candidate_count": top_count,
            "candidate_topic_count": len(candidate_counts),
            "candidate_purity": round(top_count / len(members), 4) if members else 0.0,
            "accepted_topic_rate": round(
                sum(row.get("classification_status") == "classified" for row in members) / len(members), 4
            ) if members else 0.0,
        })
    aligned_unit_count = sum(row["unit_count"] for row in alignment_rows)
    quality = {
        "representative_term_count": len(terms), "representative_term_noise_count": noise_count,
        "representative_term_noise_rate": round(noise_count / len(terms), 4) if terms else 0.0,
        "topic_diversity": round(len(set(terms)) / len(terms), 4) if terms else 0.0,
        "average_keyword_coverage_rate": round(sum(summary["keyword_coverage_rate"] for summary in evaluable) / len(evaluable), 4) if evaluable else 0.0,
        "single_document_cluster_count": sum(summary["document_count"] == 1 for summary in evaluable),
        "evaluated_cluster_count": len(evaluable), "outlier_cluster_count": len(summaries) - len(evaluable),
        "outlier_unit_count": len(by_topic.get(-1, [])),
        "outlier_unit_rate": round(len(by_topic.get(-1, [])) / len(units), 4) if units else 0.0,
        "cluster_size_distribution": {
            summary["cluster_id"]: summary["unit_count"] for summary in summaries
        },
        "largest_non_outlier_cluster_rate": round(
            max((summary["unit_count"] for summary in evaluable), default=0) / len(units), 4
        ) if units else 0.0,
        "taxonomy_alignment": {
            "weighted_candidate_purity": round(
                sum(row["unit_count"] * row["candidate_purity"] for row in alignment_rows) / aligned_unit_count, 4
            ) if aligned_unit_count else 0.0,
            "median_candidate_purity": round(
                sorted(row["candidate_purity"] for row in alignment_rows)[len(alignment_rows) // 2], 4
            ) if alignment_rows else 0.0,
            "clusters_at_least_60_percent_pure": sum(
                row["candidate_purity"] >= 0.6 for row in alignment_rows
            ),
            "weighted_accepted_topic_rate": round(
                sum(row["unit_count"] * row["accepted_topic_rate"] for row in alignment_rows) / aligned_unit_count, 4
            ) if aligned_unit_count else 0.0,
            "per_cluster": alignment_rows,
            "interpretation": "该指标衡量BERTopic簇与参考体系候选是否同向；代表词无符号噪声不等于簇具有单一领域话题。",
        },
        "limitations": [
            "自动噪声率不能代替人工语义判断。",
            (
                "当前来源文档较少，可能按文体或来源聚类。"
                if len({unit["doc_id"] for unit in units}) < 10
                else "按 doc_id 均衡抽样用于扩大体系覆盖，不代表完整语料中的自然话题频率。"
            ),
            "正式发布前需进行多随机种子稳定性评估。",
        ],
    }
    mapping_counts = Counter(summary["mapping_status"] for summary in summaries)
    classification_counts = Counter(row["classification_status"] for row in assignments)
    canonical_topic_paths = assignable_paths(dynamic_taxonomy)
    llm_naming_rules = [
        "先在 taxonomy_context_file 的 active_topic_paths 中选择；语义相同或近似时必须原样复用已有完整路径，不得改写同义名称。",
        "active_topic_paths 只包含可用于标注的三级叶子；一级、二级节点仅用于组织层级，禁止作为最终标签。",
        "reference_status 为空或为 no_qualified_candidate 时，表示没有机器推荐候选，须直接判断文本话题并优先归入动态体系。",
        "active_topic_paths 为空表示本次没有可用体系，此时根据文本证据构建新路径，并严格遵守后续命名规则。",
        "找到合适的现有路径时，review_decision 填“归入现有”，topic_path_source 填 existing_taxonomy，并把该路径原样写入 revised_topic_path。",
        "只有现有路径确实无法覆盖且文本具有稳定明确的话题时，才可填写“新增”。",
        "新增路径必须恰好包含三级（一级 > 二级 > 三级），尽量复用已有一级、二级名称，只新增必要的三级叶子；层级分隔符固定为‘ > ’。",
        "新增标签使用简洁、稳定的名词或名词短语，不使用完整句子，不随意使用‘相关、方面、问题、其他、综合’等泛化名称。",
        "判断现有话题时同时参考路径、definition和keywords，不得只凭名称或单个词判断。",
        "新增话题必须生成可区分相邻话题的说明，并提供3—8个来自文本证据的典型词。",
        "同一批任务中语义相同的新话题必须复用首次确定的名称和路径，不得产生简称、全称或近义词变体。",
        "文本缺乏稳定话题、仅为结构提示或信息不足时填“不通过”，topic_path_source 填 none，路径留空。",
    ]
    llm_system_instruction = (
        "你是话题体系审核员。你的首要目标是复用并统一现有标准话题名称，而不是自由改写名称。"
        "先判断参考候选，再检索标准路径表；只有标准体系确实无法覆盖时才新增话题。"
        "必须严格按 required_output 返回结构化结果。"
    )
    taxonomy_context = {
        "schema_version": "1.0",
        "context_type": "dynamic_topic_taxonomy",
        "source_path": str(args.dynamic_taxonomy),
        "taxonomy_name": dynamic_taxonomy.get("taxonomy_name", "动态话题体系"),
        "taxonomy_version": dynamic_taxonomy.get("taxonomy_version", 1),
        "max_levels": 3,
        "path_separator": " > ",
        "active_topic_paths": canonical_topic_paths,
        "taxonomy_tree": taxonomy_tree(dynamic_taxonomy),
        "topics": [
            {
                "path": topic_row.get("path", ""),
                "level": topic_row.get("level", ""),
                "parent_path": topic_row.get("parent_path", ""),
                "name": topic_row.get("name", ""),
                "definition": topic_row.get("definition", ""),
                "keywords": topic_row.get("keywords", []),
            }
            for topic_row in dynamic_taxonomy.get("topics", [])
            if topic_row.get("status", "active") == "active"
        ],
        "naming_rules": llm_naming_rules,
        "decision_matrix": {
            "without_reference": {
                "condition": "reference_status 为空、为 no_qualified_candidate，或 reference_topic_path 为空",
                "allowed_decisions": ["归入现有", "新增", "不通过"],
                "forbidden_decisions": ["通过"],
                "procedure": "先检索动态体系；可覆盖则归入现有，不能覆盖且话题稳定才新增，信息不足则不通过。",
            },
            "with_reference": {
                "condition": "reference_status 为 candidate_available 且 reference_topic_path 非空",
                "allowed_decisions": ["通过", "不通过", "归入现有", "新增"],
                "procedure": "先审核当前候选；候选不当时可改归其他现有路径，确实无法覆盖时才新增。",
            },
        },
        "update_policy": {
            "existing": "归入现有时路径必须已经存在于 active_topic_paths。",
            "new": "新增时优先复用已有一级、二级；无法归入时允许同时创建缺失上级。",
            "persistence": "审核通过的新路径必须立即写回动态话题体系，后续任务必须读取更新后的体系版本。",
            "sequential_api": "逐条调用时先读取 /api/bootstrap 的最新体系，再将结果 POST 到 /api/llm-annotation。",
        },
    }
    llm_tasks = [{
        "task_type": (
            "review_topic_mapping" if row["matched_topic_path"]
            else "review_reference_candidate" if row["reference_topic_path"]
            else "name_unmatched_cluster"
        ),
        "review_id": row["review_id"],
        "cluster_id": row["cluster_id"],
        "representative_words": row["representative_words"],
        "representative_texts": row["representative_texts"],
        "matched_topic_path": row["matched_topic_path"],
        "reference_rank": row["reference_rank"],
        "reference_topic_path": row["reference_topic_path"],
        "combined_score": row["combined_score"],
        "system_instruction": llm_system_instruction,
        "reference_status": "candidate_available" if row["reference_topic_path"] else "no_qualified_candidate",
        "taxonomy_availability": "loaded" if canonical_topic_paths else "not_available",
        "taxonomy_context_file": "demo_llm_taxonomy_context.json",
        "taxonomy_version": dynamic_taxonomy.get("taxonomy_version", 1),
        "review_rules": [
            "有匹配话题时，判断完整话题路径是否适合当前聚类。",
            "多个匹配话题分别审核，不合并作答。",
            "reference_topic_path 只是未必过阈值的参考候选，不得直接视为已匹配结果。",
            "若人工确认参考候选合适，可直接标记“通过”，无需重复填写 revised_topic_path。",
            "无候选时依据 taxonomy_context_file 归入现有路径；确实不能覆盖时才新增。",
            "reference_status 为空、为 no_qualified_candidate 或 reference_topic_path 为空时禁止填写“通过”。",
            "修改或新增名称时必须分别给出一级、二级、三级，并使用“一级话题 > 二级话题 > 三级话题”完整形式；不得以一级或二级作为终止标签。",
        ],
        "required_output": {
            "schema_version": "1.2",
            "review_id": row["review_id"],
            "unit_type": "cluster",
            "unit_id": row["cluster_id"],
            "annotator_type": "llm",
            "taxonomy_version": dynamic_taxonomy.get("taxonomy_version", 1),
            "reference_status": "candidate_available" if row["reference_topic_path"] else "no_qualified_candidate",
            "reference_topic_path": row["reference_topic_path"],
            "review_decision": "通过|不通过|归入现有|新增",
            "topic_path_source": "reference_candidate|existing_taxonomy|new_topic|none",
            "inappropriate_labels": "",
            "level_1_topic": "一级话题",
            "level_2_topic": "二级话题",
            "level_3_topic": "三级话题；优先从已有体系选择",
            "final_topic_path": "最终采用的完整标准路径；不通过时为空",
            "canonicalization_note": "说明复用现有名称或新增名称的依据",
            "revision_comment": "",
            "new_topic_definition": "新增三级话题时必填；说明三级话题的对象、范围和边界，其他结论为空",
            "new_topic_keywords": ["仅新增三级话题时填写3—8个聚类代表词，其他结论为空"],
            "confidence": "0到1",
        },
    } for row in review_rows] if workflow_stage == "taxonomy_build" else []
    sentence_review_rows = (
        build_sentence_review_rows(
            assignments,
            reference_limit=int(representation_config["reference_candidate_limit"]),
            review_threshold=float(matching_config["unit_review_threshold"]),
            vector_evidence_threshold=float(matching_config["vector_evidence_threshold"]),
        )
        if workflow_stage == "sentence_classification" else []
    )
    sentence_llm_tasks = [{
        "task_type": (
            "review_sentence_topic_candidate" if row["reference_topic_path"]
            else "assign_or_name_unmatched_sentence"
        ),
        "review_id": row["review_id"],
        "sentence_id": row["sentence_id"],
        "cluster_id": row["cluster_id"],
        "sentence_text": row["sentence_text"],
        "classification_status": row["classification_status"],
        "review_scope": row["review_scope"],
        "reference_rank": row["reference_rank"],
        "reference_topic_path": row["reference_topic_path"],
        "combined_score": row["combined_score"],
        "matched_topic_path": row["matched_topic_path"],
        "system_instruction": llm_system_instruction,
        "reference_status": "candidate_available" if row["reference_topic_path"] else "no_qualified_candidate",
        "taxonomy_availability": "loaded" if canonical_topic_paths else "not_available",
        "taxonomy_context_file": "demo_llm_taxonomy_context.json",
        "taxonomy_version": dynamic_taxonomy.get("taxonomy_version", 1),
        "review_rules": [
            "每条任务只审核当前句子与当前参考话题路径是否匹配。",
            "reference_topic_path 是候选路径；只有 matched_topic_path 非空时才是机器已接受结果。",
            "候选合适时填写“通过”；不合适时填写“不通过”并指出不合适标签。",
            "reference_status 为空或 reference_topic_path 为空时，直接判断文本话题，并优先从 taxonomy_context_file 选择标准路径。",
            "无有效参考候选时禁止填写“通过”，只能选择“归入现有”“新增”或“不通过”。",
            "找到现有路径时填写“归入现有”；现有体系不能覆盖时，才填写“新增”。",
            "先判一级，再判二级，最后优先选择已有三级；新增时优先挂到已有一级、二级话题下，无法归入时须同时给出新的一级、二级、三级完整路径。",
            "新增时必须生成话题说明和3—8个典型词；判断现有路径时必须参考体系中的说明和典型词。",
            "语义不足以形成稳定话题时填写“不通过”，不要为了完成任务强行命名。",
        ],
        "required_output": {
            "schema_version": "1.2",
            "review_id": row["review_id"],
            "unit_type": "sentence",
            "unit_id": row["sentence_id"],
            "annotator_type": "llm",
            "taxonomy_version": dynamic_taxonomy.get("taxonomy_version", 1),
            "reference_status": "candidate_available" if row["reference_topic_path"] else "no_qualified_candidate",
            "reference_topic_path": row["reference_topic_path"],
            "review_decision": "通过|不通过|归入现有|新增",
            "topic_path_source": "reference_candidate|existing_taxonomy|new_topic|none",
            "inappropriate_labels": "",
            "level_1_topic": "一级话题",
            "level_2_topic": "二级话题",
            "level_3_topic": "三级话题；优先从已有体系选择",
            "final_topic_path": "最终采用的完整标准路径；不通过时为空",
            "canonicalization_note": "说明复用现有名称或新增名称的依据",
            "revision_comment": "",
            "new_topic_definition": "新增三级话题时必填；说明三级话题的对象、范围和边界，其他结论为空",
            "new_topic_keywords": ["仅新增三级话题时填写3—8个聚类代表词，其他结论为空"],
            "confidence": "0到1",
        },
    } for row in sentence_review_rows]
    adjustment_candidates = [{
        "cluster_id": summary["cluster_id"],
        "reason": summary["mapping_status"],
        "representative_words": [word["term"] for word in summary["representative_words"]],
        "best_existing_candidate": summary["mapping_candidates"][0] if summary["mapping_candidates"] else {},
        "center_samples": summary["center_samples"],
    } for summary in summaries if summary["mapping_status"] in {"unmatched", "needs_review"}]

    cluster_evidence = {
        summary["cluster_id"]: {
            "representative_words": [word["term"] for word in summary["representative_words"]],
            "representative_texts": summary["representative_texts"],
        }
        for summary in summaries
    }
    preliminary_records = [
        {
            "cluster_id": summary["cluster_id"],
            "topic_path": candidate["path"],
            **cluster_evidence[summary["cluster_id"]],
        }
        for summary in summaries
        for candidate in summary["matched_topics"]
    ]
    preliminary_taxonomy = build_nested_taxonomy(preliminary_records, "preliminary_machine_matched")

    reviewed_taxonomy: dict[str, Any] | None = None
    review_audit: dict[str, Any] = {"status": "not_provided"}
    if args.review_file is not None:
        if not args.review_file.exists():
            raise FileNotFoundError(f"审核文件不存在：{args.review_file}")
        completed_rows = read_csv(args.review_file)
        final_records, decision_counts = reviewed_topic_records(completed_rows, cluster_evidence)
        reviewed_taxonomy = build_nested_taxonomy(final_records, "reviewed")
        review_audit = {
            "status": "loaded",
            "source_path": str(args.review_file),
            "row_count": len(completed_rows),
            "decision_counts": decision_counts,
            "accepted_path_count": reviewed_taxonomy["accepted_path_count"],
        }
    classification_rows = [{
        "unit_id": row["unit_id"], "doc_id": row["doc_id"], "heading": row["heading"],
        "classification_stage": row["classification_stage"],
        "cluster_id": row["cluster_id"], "classification_status": row["classification_status"],
        "cluster_primary_topic_path": row.get("cluster_primary_topic", {}).get("path", ""),
        "cluster_primary_topic_score": row.get("cluster_primary_topic", {}).get("score", ""),
        "sentence_deviates_from_cluster": row.get("sentence_deviates_from_cluster", False),
        "best_candidate_topic_id": row["primary_topic"].get("node_id", ""),
        "best_candidate_topic_path": row["primary_topic"].get("path", ""),
        "best_candidate_score": row["primary_topic"].get("score", ""),
        "sentence_score_before_prior": row["primary_topic"].get("sentence_score", ""),
        "cluster_prior_contribution": row["primary_topic"].get("cluster_prior_contribution", ""),
        "accepted_topic_id": row["primary_topic"].get("node_id", "") if workflow_stage == "sentence_classification" and row["classification_status"] == "classified" else "",
        "accepted_topic_path": row["primary_topic"].get("path", "") if workflow_stage == "sentence_classification" and row["classification_status"] == "classified" else "",
        "accepted_topic_score": row["primary_topic"].get("score", "") if workflow_stage == "sentence_classification" and row["classification_status"] == "classified" else "",
        "secondary_topic_ids": "|".join(item["node_id"] for item in row["secondary_topics"]),
        "dimension_ids": "|".join(item["node_id"] for item in row["cross_cutting_dimensions"]),
        "matched_terms": "|".join(row["primary_topic"].get("term_hits", [])),
        "matched_keywords": "|".join(row["primary_topic"].get("keyword_hits", [])),
        "topic_match_margin": row["topic_match_margin"],
    } for row in assignments]
    term_evidence_rows: list[dict[str, Any]] = []
    for term in term_rows:
        matched_indexes = [
            index for index, unit in enumerate(units)
            if any(form.casefold() in unit["analysis_text"].casefold() for form in term["forms"])
        ]
        term_evidence_rows.append({
            "term": term["term"], "aliases": "|".join(term["aliases"]), "status": term["status"],
            "term_role": term["role"], "term_weight": term["weight"],
            "configured_topic_node_ids": "|".join(term["topic_node_ids"]),
            "matched_unit_count": len(matched_indexes),
            "matched_cluster_ids": "|".join(sorted({assignments[index]["cluster_id"] for index in matched_indexes})),
            "observed_primary_topic_ids": "|".join(sorted({
                assignments[index]["primary_topic"].get("node_id", "") for index in matched_indexes
                if assignments[index]["primary_topic"].get("node_id")
            })),
        })
    report = {
        "status": "demo_review_completed" if reviewed_taxonomy is not None else "demo_ready_for_review",
        "created_at": datetime.now().astimezone().isoformat(timespec="seconds"),
        "mode": f"BERTopic_with_{reduction_method.upper()}",
        "workflow_stage": workflow_stage,
        "classification_semantics": (
            "正式分类：使用已维护动态体系并写入 accepted_topic_*"
            if workflow_stage == "sentence_classification"
            else "体系构建：句子结果仅作簇内诊断，不写入正式 accepted_topic_*"
        ),
        "warning": (
            "当前明确配置为PCA降维；适合稳定调试，但与UMAP的局部流形结构不同。"
            if reduction_method == "pca" else "当前配置为UMAP降维。"
        ),
        "config_path": str(args.config),
        "effective_config": config,
        "input_sections": str(args.sections),
        "sampling": sampling_audit,
        "reference_taxonomy": str(args.taxonomy) if reference else "",
        "taxonomy_status": "loaded" if nodes else "ignored",
        "taxonomy_node_count": len(nodes),
        "term_table": term_table_audit,
        "dynamic_taxonomy": {
            "path": str(args.dynamic_taxonomy),
            "taxonomy_version": dynamic_taxonomy.get("taxonomy_version", 1),
            "active_topic_count": len(canonical_topic_paths),
        },
        "embedding_model": config["embedding_model"], "valid_units": len(units), "excluded_units": len(excluded_units),
        "cluster_count_excluding_outliers": len(evaluable), "outlier_units": len(by_topic.get(-1, [])),
        "cluster_mapping_counts": dict(mapping_counts),
        "text_classification_counts": dict(classification_counts),
        "matching_weights": matching_weights,
        "matching_thresholds": {
            "cluster_match": matching_config["cluster_match_threshold"],
            "cluster_match_policy": "each_candidate_independently",
            "unit_match": matching_config["unit_match_threshold"],
            "unit_review": matching_config["unit_review_threshold"],
            "unit_margin": matching_config["unit_margin_threshold"],
        },
        "review_file": review_audit,
        "review_outputs": {
            "cluster_review_rows": len(review_rows),
            "cluster_llm_tasks": len(llm_tasks),
            "sentence_review_rows": len(sentence_review_rows),
            "sentence_llm_tasks": len(sentence_llm_tasks),
        },
        "unit_policy": unit_policy,
        "representation": {
            "analyzer": "word", "tokenizer": "jieba_with_domain_dictionary",
            "ngram_range": representation_config["ngram_range"],
            "min_df": representation_config["min_df"], "max_df": representation_config["max_df"],
            "ctfidf": {
                "bm25_weighting": representation_config["bm25_weighting"],
                "reduce_frequent_words": representation_config["reduce_frequent_words"],
            },
            **representation_audit,
        },
        "quality_summary": quality,
    }
    common_files = [
        {"file": "demo_run_report.json", "role": "本次阶段、有效配置和数量汇总。"},
        {"file": "demo_output_manifest.json", "role": "本阶段实际会生成的文件清单。"},
        {"file": "demo_topic_summary.json", "role": "簇级代表词、代表文本、倾向话题和评分。"},
        {"file": "demo_unit_assignments.jsonl", "role": "句子原始分、簇先验分、合成分及偏离标记。"},
        {"file": "demo_topic_quality.json", "role": "聚类质量、离群率和体系一致性诊断。"},
        {"file": "demo_sampled_units.jsonl", "role": "实际参与本次运行的句子。"},
        {"file": "demo_sampling_report.json", "role": "按 doc_id 的抽样审计。"},
        {"file": "demo_exclusion_audit.csv", "role": "被长度、标题或结构噪声规则排除的句子。"},
        {"file": "demo_term_topic_evidence.csv", "role": "术语、句子、簇与话题的交叉证据。"},
        {"file": "demo_llm_taxonomy_context.json", "role": "标注端引用的动态体系快照。"},
    ]
    stage_files = (
        [
            {"file": "demo_review_workbench.csv", "role": "聚类级人工体系审核表。"},
            {"file": "demo_llm_review_tasks.jsonl", "role": "聚类级大模型体系审核任务。"},
            {"file": "demo_preliminary_taxonomy.json", "role": "机器匹配形成的初步体系。"},
            {"file": "demo_final_taxonomy.json", "role": "导入聚类审核后生成的审核体系。"},
            {"file": "demo_taxonomy_adjustment_candidates.json", "role": "未匹配簇的体系调整候选。"},
        ]
        if workflow_stage == "taxonomy_build" else
        [
            {"file": "demo_sentence_classification.csv", "role": "使用动态体系和簇先验得到的正式单句分类。"},
            {"file": "demo_sentence_review_workbench.csv", "role": "正式单句分类的人工复核表。"},
            {"file": "demo_sentence_llm_review_tasks.jsonl", "role": "正式单句分类的大模型复核任务。"},
        ]
    )
    output_manifest = {
        "workflow_stage": workflow_stage,
        "files": common_files + stage_files,
        "persistent_taxonomy": {
            "file": "../configs/dynamic_topic_taxonomy.json",
            "role": "体系构建审核持续维护；正式分类只读取其活动话题。",
        },
    }
    write_json(args.output_root / "demo_run_report.json", report)
    write_json(args.output_root / "demo_output_manifest.json", output_manifest)
    write_json(args.output_root / "demo_llm_taxonomy_context.json", taxonomy_context)
    write_json(args.output_root / "demo_sampling_report.json", sampling_audit)
    write_json(args.output_root / "demo_topic_quality.json", quality)
    write_json(args.output_root / "demo_topic_summary.json", {"topics": summaries})
    write_jsonl(args.output_root / "demo_unit_assignments.jsonl", assignments)
    write_jsonl(args.output_root / "demo_sampled_units.jsonl", units)
    write_csv(args.output_root / "demo_term_topic_evidence.csv", [
        "term", "aliases", "status", "term_role", "term_weight", "configured_topic_node_ids",
        "matched_unit_count", "matched_cluster_ids", "observed_primary_topic_ids",
    ], term_evidence_rows)
    if workflow_stage == "taxonomy_build":
        write_json(args.output_root / "demo_preliminary_taxonomy.json", preliminary_taxonomy)
        write_json(args.output_root / "demo_final_taxonomy.json", reviewed_taxonomy or {
            "status": "review_not_provided", "accepted_path_count": 0, "topics": [],
        })
        write_jsonl(args.output_root / "demo_llm_review_tasks.jsonl", llm_tasks)
        write_json(args.output_root / "demo_taxonomy_adjustment_candidates.json", {"candidates": adjustment_candidates})
        review_output = args.output_root / "demo_review_workbench.csv"
        if args.review_file is None or args.review_file.resolve() != review_output.resolve():
            write_csv(review_output, [
                "review_id", "cluster_id", "unit_count", "representative_words", "representative_texts",
                "matched_topic_path", "reference_rank", "reference_topic_path", "combined_score",
                "review_decision", "inappropriate_labels", "revised_topic_path", "revision_comment",
                "new_topic_definition", "new_topic_keywords",
            ], review_rows)
    else:
        write_jsonl(args.output_root / "demo_sentence_llm_review_tasks.jsonl", sentence_llm_tasks)
        write_csv(args.output_root / "demo_sentence_classification.csv", [
            "unit_id", "doc_id", "heading", "classification_stage", "cluster_id", "classification_status",
            "cluster_primary_topic_path", "cluster_primary_topic_score", "sentence_deviates_from_cluster",
            "best_candidate_topic_id", "best_candidate_topic_path", "best_candidate_score",
            "sentence_score_before_prior", "cluster_prior_contribution",
            "accepted_topic_id", "accepted_topic_path", "accepted_topic_score", "secondary_topic_ids",
            "dimension_ids", "matched_terms", "matched_keywords", "topic_match_margin",
        ], classification_rows)
        write_csv(args.output_root / "demo_sentence_review_workbench.csv", [
            "review_id", "sentence_id", "doc_id", "section_id", "paragraph_id", "cluster_id",
            "sentence_text", "classification_status", "review_scope", "reference_rank",
            "reference_topic_path", "combined_score", "matched_topic_path",
            "review_decision", "inappropriate_labels", "revised_topic_path", "revision_comment",
            "new_topic_definition", "new_topic_keywords",
        ], sentence_review_rows)
    write_csv(args.output_root / "demo_exclusion_audit.csv", ["unit_id", "doc_id", "heading", "reason"], excluded_units)
    print(json.dumps(report, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
