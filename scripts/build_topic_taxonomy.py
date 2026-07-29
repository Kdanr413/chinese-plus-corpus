#!/usr/bin/env python3
"""Generate a corpus-grounded candidate topic taxonomy for human review.

The default path uses only the Python standard library:
1. clean section headings;
2. assign auditable seed-topic and aspect candidates using a controlled config;
3. create sparse TF-IDF features;
4. cluster sections with spherical k-means;
5. summarize clusters into candidate topics and review files.

An OpenAI-compatible chat-completions endpoint can optionally refine cluster names
and hierarchy suggestions. LLM output remains a proposal and never overwrites the
review status or the controlled seed taxonomy.
"""

from __future__ import annotations

import argparse
import csv
import json
import math
import os
import random
import re
import sys
import time
import urllib.error
import urllib.request
from collections import Counter, defaultdict
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, Iterable, List, Mapping, Optional, Sequence, Tuple


DEFAULT_INPUT_ROOT = Path(r"D:\production\corpus\processed\jsonl")
DEFAULT_OUTPUT_ROOT = Path(r"D:\production\corpus\processed\taxonomy_candidates")
DEFAULT_CONFIG = Path(__file__).resolve().parents[1] / "configs" / "topic_taxonomy_config.json"

NUMBERING_PREFIX = re.compile(
    r"^\s*(?:第[一二三四五六七八九十百千万\d]+[章节篇部分条]\s*|"
    r"[一二三四五六七八九十百]+[、.．]\s*|"
    r"[（(][一二三四五六七八九十百\d]+[）)]\s*|"
    r"\d+(?:\.\d+){0,4}[、.．]?\s*)"
)
PAGE_SUFFIX = re.compile(r"(?:[.．·…]{3,}|\s{3,})\s*\d+\s*$")
PAGE_MARK = re.compile(r"^\s*(?:=+\s*)?(?:Page|第)?\s*\d+\s*(?:页|=+)?\s*$", re.I)
MEASURE_FRAGMENT = re.compile(
    r"^\s*(?:约|超过|达到)?\s*[\d.,%％\s]*"
    r"(?:万|亿|千|百)?(?:美元|元|吨|万吨|亿吨|千瓦|兆瓦|吉瓦|千瓦时|%|％)"
    r"(?:锂|钴|镍|铜|稀土)?\s*$"
)
CHINESE_RUN = re.compile(r"[\u3400-\u9fff]{2,}")
ASCII_TOKEN = re.compile(r"[A-Za-z][A-Za-z0-9+./_-]{1,30}")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="从章节语料生成待人工核验的话题体系、章节标签和证据表。"
    )
    parser.add_argument("--input-root", type=Path, default=DEFAULT_INPUT_ROOT)
    parser.add_argument("--output-root", type=Path, default=DEFAULT_OUTPUT_ROOT)
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    parser.add_argument("--clusters", type=int, default=None)
    parser.add_argument("--max-features", type=int, default=None)
    parser.add_argument("--limit", type=int, default=None, help="仅调试：最多处理多少个章节")
    parser.add_argument("--use-llm", action="store_true", help="调用兼容 Chat Completions 的大模型")
    parser.add_argument("--llm-model", default=os.environ.get("TOPIC_LLM_MODEL"))
    parser.add_argument(
        "--llm-base-url",
        default=os.environ.get("TOPIC_LLM_BASE_URL", "https://api.openai.com/v1"),
    )
    parser.add_argument(
        "--llm-api-key-env",
        default="TOPIC_LLM_API_KEY",
        help="保存 API key 的环境变量名；程序不会输出或保存 key",
    )
    return parser.parse_args()


def read_json(path: Path) -> Dict[str, Any]:
    with path.open("r", encoding="utf-8-sig") as handle:
        return json.load(handle)


def read_jsonl(path: Path) -> Iterable[Dict[str, Any]]:
    with path.open("r", encoding="utf-8-sig") as handle:
        for line_number, line in enumerate(handle, 1):
            line = line.strip()
            if not line:
                continue
            try:
                yield json.loads(line)
            except json.JSONDecodeError as exc:
                raise ValueError(f"{path}:{line_number} 不是有效 JSONL: {exc}") from exc


def write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as handle:
        json.dump(value, handle, ensure_ascii=False, indent=2)


def write_jsonl(path: Path, rows: Iterable[Mapping[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as handle:
        for row in rows:
            handle.write(json.dumps(dict(row), ensure_ascii=False) + "\n")


def write_csv(path: Path, rows: Sequence[Mapping[str, Any]], fieldnames: Sequence[str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)


def normalize_text(text: Any) -> str:
    value = str(text or "")
    value = value.replace("\u3000", " ").replace("\ufeff", "")
    value = re.sub(r"\s+", " ", value)
    return value.strip()


def clean_heading(heading: Any) -> Tuple[str, List[str]]:
    original = normalize_text(heading)
    value = original
    flags: List[str] = []
    if not value:
        return "", ["empty"]
    if PAGE_MARK.fullmatch(value):
        return "", ["page_mark"]
    stripped = PAGE_SUFFIX.sub("", value).strip()
    if stripped != value:
        flags.append("page_suffix_removed")
        value = stripped
    value = re.sub(r"^[=＿_\-—–\s]+|[=＿_\-—–\s]+$", "", value).strip()
    value = NUMBERING_PREFIX.sub("", value).strip(" ：:、.．")
    if MEASURE_FRAGMENT.fullmatch(value):
        return "", flags + ["measure_fragment"]
    if len(value) > 120:
        flags.append("long_heading")
    if value and sum(ch.isdigit() for ch in value) / max(len(value), 1) > 0.45:
        flags.append("numeric_heavy")
    if value and len(re.findall(r"[\u3400-\u9fffA-Za-z]", value)) < 2:
        flags.append("low_text_content")
    if any(flag in flags for flag in ("numeric_heavy", "low_text_content")):
        return "", flags
    return value[:180], flags


def build_parent_index(nodes: Sequence[Mapping[str, Any]], root_id: str) -> Tuple[Dict[str, Any], Dict[str, int]]:
    by_id = {str(node["id"]): dict(node) for node in nodes}
    depth: Dict[str, int] = {root_id: 0}

    def get_depth(node_id: str, visiting: Optional[set] = None) -> int:
        if node_id in depth:
            return depth[node_id]
        visiting = visiting or set()
        if node_id in visiting:
            raise ValueError(f"分类配置存在循环父子关系: {node_id}")
        visiting.add(node_id)
        node = by_id.get(node_id)
        if not node:
            return 0
        parent = str(node.get("parent_id") or root_id)
        depth[node_id] = get_depth(parent, visiting) + 1
        return depth[node_id]

    for identifier in by_id:
        get_depth(identifier)
    return by_id, depth


def keyword_score(text: str, heading: str, keywords: Sequence[str]) -> Tuple[float, List[str]]:
    score = 0.0
    evidence: List[str] = []
    lower_text = text.lower()
    lower_heading = heading.lower()
    for keyword in keywords:
        key = str(keyword).strip()
        if not key:
            continue
        key_lower = key.lower()
        heading_count = lower_heading.count(key_lower)
        body_count = lower_text.count(key_lower)
        if heading_count or body_count:
            score += heading_count * 4.0 + min(body_count, 5) * 1.0
            evidence.append(key)
    return score, evidence


def assign_candidates(
    heading: str,
    text: str,
    items: Sequence[Mapping[str, Any]],
    min_score: float,
    limit: int,
    depth: Optional[Mapping[str, int]] = None,
) -> List[Dict[str, Any]]:
    scored = []
    for item in items:
        score, evidence = keyword_score(text, heading, item.get("keywords", []))
        if score >= min_score:
            identifier = str(item["id"])
            scored.append(
                {
                    "id": identifier,
                    "name": item["name"],
                    "score": round(score, 3),
                    "evidence_terms": evidence[:12],
                    "depth": int((depth or {}).get(identifier, 0)),
                }
            )
    scored.sort(key=lambda row: (row["score"], row["depth"], len(row["name"])), reverse=True)
    if depth:
        matched_ids = {row["id"] for row in scored}
        ancestor_ids = set()
        item_by_id = {str(item["id"]): item for item in items}
        for identifier in matched_ids:
            current = item_by_id.get(identifier, {}).get("parent_id")
            while current and current in item_by_id:
                ancestor_ids.add(str(current))
                current = item_by_id.get(str(current), {}).get("parent_id")
        scored = [row for row in scored if row["id"] not in ancestor_ids]
        scored.sort(
            key=lambda row: (row["depth"], row["score"], len(row["name"])),
            reverse=True,
        )
    selected: List[Dict[str, Any]] = []
    for row in scored:
        if any(row["id"] == chosen["id"] for chosen in selected):
            continue
        selected.append(row)
        if len(selected) >= limit:
            break
    return selected


def feature_counter(
    record: Mapping[str, Any],
    known_terms: Sequence[str],
    stop_terms: set,
) -> Counter:
    heading = str(record["clean_heading"])
    text = str(record["analysis_text"])
    combined = f"{heading} {heading} {heading} {text}"
    counter: Counter = Counter()

    lower = combined.lower()
    for term in known_terms:
        count = lower.count(term.lower())
        if count:
            counter[f"TERM:{term}"] += min(count, 8) * 2

    for token in ASCII_TOKEN.findall(combined):
        token = token.upper()
        if len(token) >= 2:
            counter[f"WORD:{token}"] += 1

    for run in CHINESE_RUN.findall(combined):
        if len(run) > 80:
            chunks = [run[pos : pos + 80] for pos in range(0, len(run), 80)]
        else:
            chunks = [run]
        for chunk in chunks:
            for size in (2, 3, 4):
                if len(chunk) < size:
                    continue
                for pos in range(len(chunk) - size + 1):
                    token = chunk[pos : pos + size]
                    if token in stop_terms:
                        continue
                    if any(stop in token for stop in stop_terms if len(stop) == 2):
                        continue
                    counter[f"NGRAM:{token}"] += 1

    if heading:
        for phrase in re.split(r"[，,；;：:/、（）()\[\]<>《》]+", heading):
            phrase = phrase.strip()
            if 2 <= len(phrase) <= 18 and phrase not in stop_terms:
                counter[f"HEAD:{phrase}"] += 8
    return counter


def build_tfidf(
    counters: Sequence[Counter],
    max_features: int,
    max_features_per_document: int,
    min_df: int,
    max_df_ratio: float,
) -> Tuple[List[Dict[str, float]], Dict[str, float], List[str]]:
    doc_count = len(counters)
    document_frequency: Counter = Counter()
    total_frequency: Counter = Counter()
    for counter in counters:
        document_frequency.update(counter.keys())
        total_frequency.update(counter)
    max_df = max(min_df, int(doc_count * max_df_ratio))
    eligible = [
        term
        for term, df in document_frequency.items()
        if min_df <= df <= max_df
    ]
    eligible.sort(
        key=lambda term: (
            total_frequency[term] * math.log((doc_count + 1) / (document_frequency[term] + 1)),
            document_frequency[term],
            term,
        ),
        reverse=True,
    )
    vocabulary = eligible[:max_features]
    vocab_set = set(vocabulary)
    idf = {
        term: math.log((doc_count + 1) / (document_frequency[term] + 1)) + 1.0
        for term in vocabulary
    }
    vectors: List[Dict[str, float]] = []
    for counter in counters:
        vector = {
            term: (1.0 + math.log(float(count))) * idf[term]
            for term, count in counter.items()
            if term in vocab_set and count > 0
        }
        if len(vector) > max_features_per_document:
            vector = dict(
                sorted(vector.items(), key=lambda item: item[1], reverse=True)[
                    :max_features_per_document
                ]
            )
        norm = math.sqrt(sum(value * value for value in vector.values()))
        if norm:
            vector = {term: value / norm for term, value in vector.items()}
        vectors.append(vector)
    return vectors, idf, vocabulary


def sparse_dot(left: Mapping[str, float], right: Mapping[str, float]) -> float:
    if len(left) > len(right):
        left, right = right, left
    return sum(value * right.get(term, 0.0) for term, value in left.items())


def normalize_vector(vector: Dict[str, float]) -> Dict[str, float]:
    norm = math.sqrt(sum(value * value for value in vector.values()))
    if not norm:
        return {}
    return {term: value / norm for term, value in vector.items()}


def spherical_kmeans(
    vectors: Sequence[Mapping[str, float]],
    cluster_count: int,
    iterations: int,
    seed: int,
) -> Tuple[List[int], List[Dict[str, float]], List[float]]:
    nonempty = [index for index, vector in enumerate(vectors) if vector]
    if not nonempty:
        return [0] * len(vectors), [{}], [0.0] * len(vectors)
    cluster_count = max(1, min(cluster_count, len(nonempty)))
    rng = random.Random(seed)
    centers = [dict(vectors[index]) for index in rng.sample(nonempty, cluster_count)]
    assignments = [-1] * len(vectors)
    similarities = [0.0] * len(vectors)

    for _ in range(iterations):
        changed = 0
        members: List[List[int]] = [[] for _ in range(cluster_count)]
        for index, vector in enumerate(vectors):
            if not vector:
                best_cluster, best_similarity = 0, 0.0
            else:
                scores = [sparse_dot(vector, center) for center in centers]
                best_cluster = max(range(cluster_count), key=lambda cid: scores[cid])
                best_similarity = scores[best_cluster]
            if assignments[index] != best_cluster:
                changed += 1
            assignments[index] = best_cluster
            similarities[index] = round(best_similarity, 6)
            members[best_cluster].append(index)

        new_centers: List[Dict[str, float]] = []
        for cluster_id, indices in enumerate(members):
            if not indices:
                new_centers.append(dict(vectors[rng.choice(nonempty)]))
                continue
            aggregate: Dict[str, float] = defaultdict(float)
            for index in indices:
                for term, value in vectors[index].items():
                    aggregate[term] += value
            scale = 1.0 / len(indices)
            new_centers.append(
                normalize_vector({term: value * scale for term, value in aggregate.items()})
            )
        centers = new_centers
        if changed == 0:
            break

    similarities = [
        round(sparse_dot(vectors[index], centers[assignments[index]]), 6)
        if vectors[index]
        else 0.0
        for index in range(len(vectors))
    ]
    return assignments, centers, similarities


def display_feature(term: str) -> str:
    return term.split(":", 1)[1] if ":" in term else term


def is_useful_heading_candidate(heading: str, generic: set) -> bool:
    if not heading or len(heading) < 4 or len(heading) > 60:
        return False
    if heading in generic:
        return False
    if len(heading) <= 12 and any(item in heading for item in generic):
        return False
    if sum(ch.isdigit() for ch in heading) > 2:
        return False
    if any(marker in heading for marker in ("%", "％", "亿美元", "亿元", "万吨", "Page")):
        return False
    if heading.endswith(("年的", "的是", "如下", "分别为")):
        return False
    if re.match(r"^(?:图|表|专栏)\s*\d", heading):
        return False
    return len(re.findall(r"[\u3400-\u9fff]", heading)) >= 3


def ancestors(node_id: str, by_id: Mapping[str, Mapping[str, Any]], root_id: str) -> List[str]:
    result = []
    current = node_id
    seen = set()
    while current and current != root_id and current not in seen:
        seen.add(current)
        result.append(current)
        node = by_id.get(current)
        current = str(node.get("parent_id")) if node else root_id
    result.reverse()
    return result


def build_cluster_summaries(
    records: Sequence[Dict[str, Any]],
    assignments: Sequence[int],
    similarities: Sequence[float],
    centers: Sequence[Mapping[str, float]],
    config: Mapping[str, Any],
    by_id: Mapping[str, Mapping[str, Any]],
) -> List[Dict[str, Any]]:
    algorithm = config["algorithm"]
    root_id = str(config["root"]["id"])
    generic = set(config.get("generic_headings", []))
    groups: Dict[int, List[int]] = defaultdict(list)
    for index, cluster_id in enumerate(assignments):
        groups[int(cluster_id)].append(index)

    summaries = []
    for cluster_id in sorted(groups):
        indices = groups[cluster_id]
        node_counts: Counter = Counter()
        aspect_counts: Counter = Counter()
        doc_ids = set()
        headings: Counter = Counter()
        for index in indices:
            record = records[index]
            doc_ids.add(record["doc_id"])
            if record["topic_candidates"]:
                node_counts[record["topic_candidates"][0]["id"]] += 1
            for candidate in record["aspect_candidates"]:
                aspect_counts[candidate["id"]] += 1
            heading = record["clean_heading"]
            if is_useful_heading_candidate(heading, generic):
                headings[heading] += 1

        dominant_node = node_counts.most_common(1)[0] if node_counts else None
        dominance = dominant_node[1] / len(indices) if dominant_node else 0.0
        dominant_aspect = aspect_counts.most_common(1)[0] if aspect_counts else None
        aspect_dominance = dominant_aspect[1] / len(indices) if dominant_aspect else 0.0
        aspect_by_id = {
            str(item["id"]): item for item in config.get("aspect_tags", [])
        }
        if dominant_node and dominance >= float(
            algorithm.get("cluster_label_min_seed_dominance", 0.25)
        ):
            node = by_id[dominant_node[0]]
            suggested_label = node["name"]
            parent_id = node.get("parent_id", root_id)
            proposal_type = "seed-supported"
        elif dominant_aspect and aspect_dominance >= 0.55:
            aspect_name = aspect_by_id[dominant_aspect[0]]["name"]
            suggested_label = f"待按技术对象拆分（当前呈现{aspect_name}文体特征）"
            parent_id = root_id
            proposal_type = "aspect-dominated"
        else:
            heading_choices = sorted(
                headings.items(),
                key=lambda item: (item[1], min(len(item[0]), 30), item[0]),
                reverse=True,
            )
            suggested_label = (
                f"待命名：{heading_choices[0][0][:30]}"
                if heading_choices
                else f"待命名话题簇 {cluster_id + 1:02d}"
            )
            first_level: Counter = Counter()
            for node_id, count in node_counts.items():
                path = ancestors(node_id, by_id, root_id)
                if path:
                    first_level[path[0]] += count
            parent_id = first_level.most_common(1)[0][0] if first_level else root_id
            proposal_type = "new-or-mixed"

        representative = sorted(
            indices,
            key=lambda index: (similarities[index], len(records[index]["analysis_text"])),
            reverse=True,
        )[: int(algorithm["representative_sections_per_cluster"])]
        top_features = sorted(
            centers[cluster_id].items(), key=lambda item: item[1], reverse=True
        )[:20]
        summaries.append(
            {
                "cluster_id": f"CLUSTER_{cluster_id + 1:03d}",
                "suggested_label": suggested_label,
                "suggested_parent_id": parent_id,
                "proposal_type": proposal_type,
                "review_status": "待人工核验",
                "section_count": len(indices),
                "document_count": len(doc_ids),
                "seed_dominance": round(dominance, 4),
                "top_seed_topics": [
                    {
                        "id": node_id,
                        "name": by_id[node_id]["name"],
                        "section_count": count,
                    }
                    for node_id, count in node_counts.most_common(8)
                ],
                "top_aspects": [
                    {"id": aspect_id, "section_count": count}
                    for aspect_id, count in aspect_counts.most_common(6)
                ],
                "keywords": [display_feature(term) for term, _ in top_features],
                "heading_candidates": [
                    {"heading": heading, "count": count}
                    for heading, count in headings.most_common(10)
                ],
                "representative_sections": [
                    {
                        "doc_id": records[index]["doc_id"],
                        "section_id": records[index]["section_id"],
                        "heading": records[index]["clean_heading"],
                        "heading_path": records[index]["heading_path"],
                        "similarity": similarities[index],
                        "excerpt": records[index]["analysis_text"][:360],
                    }
                    for index in representative
                ],
                "llm_suggestion": None,
            }
        )
    return summaries


def llm_request(
    base_url: str,
    api_key: str,
    model: str,
    clusters: Sequence[Mapping[str, Any]],
    taxonomy_nodes: Sequence[Mapping[str, Any]],
) -> Dict[str, Any]:
    compact_nodes = [
        {"id": node["id"], "name": node["name"], "parent_id": node.get("parent_id")}
        for node in taxonomy_nodes
    ]
    compact_clusters = []
    for cluster in clusters:
        compact_clusters.append(
            {
                "cluster_id": cluster["cluster_id"],
                "current_suggestion": cluster["suggested_label"],
                "parent_suggestion": cluster["suggested_parent_id"],
                "section_count": cluster["section_count"],
                "keywords": cluster["keywords"][:15],
                "headings": [row["heading"] for row in cluster["heading_candidates"][:6]],
                "examples": [
                    {
                        "heading": row["heading"],
                        "excerpt": row["excerpt"][:220],
                    }
                    for row in cluster["representative_sections"][:4]
                ],
            }
        )
    system_prompt = (
        "你是新能源与新能源汽车领域分类体系编辑。"
        "根据语料证据为每个聚类提出简洁、互斥性尽量高的话题名和父节点。"
        "政策法规、产业市场、安全、标准等属于横向知识维度，不要与技术对象混为同级主分类。"
        "不得虚构语料中没有证据的主题；混合簇应标记 split_required=true。"
        "只返回 JSON，不要返回 Markdown。"
    )
    user_payload = {
        "task": "为待核验聚类提出人工审核建议",
        "controlled_nodes": compact_nodes,
        "clusters": compact_clusters,
        "required_schema": {
            "clusters": [
                {
                    "cluster_id": "CLUSTER_001",
                    "suggested_label": "名称",
                    "suggested_parent_id": "已有父节点ID或ENERGY_NEV",
                    "definition": "一句话定义",
                    "include": ["应包含内容"],
                    "exclude": ["易混淆但不应包含内容"],
                    "split_required": False,
                    "merge_with": None,
                    "confidence": 0.0,
                    "reason": "基于关键词、标题和代表章节的简短说明",
                }
            ]
        },
    }
    request_body = json.dumps(
        {
            "model": model,
            "temperature": 0.1,
            "messages": [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": json.dumps(user_payload, ensure_ascii=False)},
            ],
        },
        ensure_ascii=False,
    ).encode("utf-8")
    endpoint = base_url.rstrip("/") + "/chat/completions"
    request = urllib.request.Request(
        endpoint,
        data=request_body,
        headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=120) as response:
            payload = json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")[:1000]
        raise RuntimeError(f"LLM HTTP {exc.code}: {detail}") from exc
    content = payload["choices"][0]["message"]["content"].strip()
    content = re.sub(r"^```(?:json)?\s*|\s*```$", "", content)
    try:
        return json.loads(content)
    except json.JSONDecodeError:
        match = re.search(r"\{.*\}", content, re.S)
        if not match:
            raise RuntimeError("大模型未返回可解析 JSON")
        return json.loads(match.group(0))


def enrich_with_llm(
    summaries: List[Dict[str, Any]],
    config: Mapping[str, Any],
    args: argparse.Namespace,
) -> List[str]:
    errors: List[str] = []
    api_key = os.environ.get(args.llm_api_key_env)
    if not api_key:
        raise RuntimeError(
            f"已指定 --use-llm，但环境变量 {args.llm_api_key_env} 未设置。"
        )
    if not args.llm_model:
        raise RuntimeError("已指定 --use-llm，但未通过 --llm-model 或 TOPIC_LLM_MODEL 指定模型。")
    batch_size = int(config["algorithm"].get("llm_batch_size", 8))
    by_cluster = {cluster["cluster_id"]: cluster for cluster in summaries}
    for start in range(0, len(summaries), batch_size):
        batch = summaries[start : start + batch_size]
        try:
            result = llm_request(
                args.llm_base_url,
                api_key,
                args.llm_model,
                batch,
                config["nodes"],
            )
            for suggestion in result.get("clusters", []):
                cluster_id = suggestion.get("cluster_id")
                if cluster_id in by_cluster:
                    by_cluster[cluster_id]["llm_suggestion"] = suggestion
        except Exception as exc:
            errors.append(f"批次 {start // batch_size + 1}: {exc}")
        if start + batch_size < len(summaries):
            time.sleep(0.5)
    return errors


def validate_inputs(input_root: Path, config: Mapping[str, Any]) -> Tuple[Path, Path, Path]:
    sections_path = input_root / "sections.jsonl"
    documents_path = input_root / "documents.jsonl"
    paragraphs_path = input_root / "paragraphs.jsonl"
    missing = [
        str(path)
        for path in (sections_path, documents_path, paragraphs_path)
        if not path.exists()
    ]
    if missing:
        raise FileNotFoundError("缺少输入文件: " + ", ".join(missing))
    root_id = str(config["root"]["id"])
    node_ids = {str(node["id"]) for node in config["nodes"]}
    if len(node_ids) != len(config["nodes"]):
        raise ValueError("配置中的话题 node id 不唯一")
    for node in config["nodes"]:
        parent = str(node.get("parent_id") or root_id)
        if parent != root_id and parent not in node_ids:
            raise ValueError(f"节点 {node['id']} 的父节点不存在: {parent}")
    return sections_path, documents_path, paragraphs_path


def main() -> int:
    args = parse_args()
    config = read_json(args.config)
    sections_path, documents_path, paragraphs_path = validate_inputs(args.input_root, config)
    algorithm = dict(config["algorithm"])
    if args.clusters is not None:
        algorithm["clusters"] = args.clusters
    if args.max_features is not None:
        algorithm["max_features"] = args.max_features
    config["algorithm"] = algorithm

    documents = {row["doc_id"]: row for row in read_jsonl(documents_path)}
    direct_paragraphs: Dict[str, List[str]] = defaultdict(list)
    for paragraph in read_jsonl(paragraphs_path):
        if paragraph.get("is_heading"):
            continue
        text = normalize_text(paragraph.get("text"))
        if text:
            direct_paragraphs[str(paragraph.get("section_id"))].append(text)
    by_id, depth = build_parent_index(config["nodes"], str(config["root"]["id"]))
    known_terms = sorted(
        {
            str(keyword)
            for node in config["nodes"]
            for keyword in node.get("keywords", [])
            if len(str(keyword).strip()) >= 2
        },
        key=lambda value: (len(value), value),
        reverse=True,
    )
    stop_terms = set(config.get("stop_terms", []))
    min_score = float(algorithm["seed_topic_min_score"])
    label_limit = int(algorithm["max_topic_labels_per_section"])

    records: List[Dict[str, Any]] = []
    heading_audit: List[Dict[str, Any]] = []
    skipped = Counter()
    for section in read_jsonl(sections_path):
        if args.limit and len(records) >= args.limit:
            break
        original_heading = normalize_text(section.get("heading"))
        clean, flags = clean_heading(original_heading)
        text = normalize_text(section.get("text"))
        level = int(section.get("level") or 0)
        heading_audit.append(
            {
                "doc_id": section.get("doc_id"),
                "section_id": section.get("section_id"),
                "level": level,
                "original_heading": original_heading,
                "clean_heading": clean,
                "quality_flags": "；".join(flags),
            }
        )
        if level == 0 or section.get("section_type") == "document_root":
            skipped["document_root_section"] += 1
            continue
        if section.get("section_type") not in (None, "", "heading"):
            skipped["non_heading_section"] += 1
            continue
        if not clean:
            skipped["empty_or_noise_heading"] += 1
            continue
        if any(
            flag in flags
            for flag in ("long_heading", "numeric_heavy", "low_text_content", "measure_fragment")
        ):
            skipped["invalid_heading"] += 1
            continue
        if re.search(r"(?:练习题?|习题|复习题|思考题|自测题|测试题|问答题|名词解释|实训任务)$", clean):
            skipped["exercise_section"] += 1
            continue
        direct_blocks = direct_paragraphs.get(str(section.get("section_id")), [])
        direct_text = normalize_text(" ".join(direct_blocks))
        if len(direct_text) < 30:
            skipped["no_direct_section_body"] += 1
            continue
        analysis_text = normalize_text(
            f"{clean} {section.get('heading_path') or ''} {direct_text}"
        )[: int(algorithm["max_section_chars"])]
        if len(analysis_text) < 20:
            skipped["too_short"] += 1
            continue
        topic_candidates = assign_candidates(
            clean,
            analysis_text,
            config["nodes"],
            min_score,
            label_limit,
            depth,
        )
        aspect_candidates = assign_candidates(
            clean,
            analysis_text,
            config.get("aspect_tags", []),
            1.0,
            5,
        )
        document = documents.get(section.get("doc_id"), {})
        records.append(
            {
                "doc_id": section.get("doc_id"),
                "section_id": section.get("section_id"),
                "parent_section_id": section.get("parent_section_id"),
                "level": level,
                "original_heading": original_heading,
                "clean_heading": clean,
                "heading_quality_flags": flags,
                "heading_path": normalize_text(section.get("heading_path")),
                "analysis_text": analysis_text,
                "direct_paragraph_count": len(direct_blocks),
                "heading_confidence": section.get("heading_confidence"),
                "heading_rule": section.get("heading_rule"),
                "source_type": document.get("source_type"),
                "source": document.get("source"),
                "document_title": document.get("title"),
                "topic_candidates": topic_candidates,
                "aspect_candidates": aspect_candidates,
            }
        )
    if not records:
        raise RuntimeError("没有可用于话题发现的章节")

    counters = [feature_counter(record, known_terms, stop_terms) for record in records]
    vectors, _, vocabulary = build_tfidf(
        counters,
        int(algorithm["max_features"]),
        int(algorithm.get("max_features_per_section", 220)),
        int(algorithm["min_df"]),
        float(algorithm["max_df_ratio"]),
    )
    assignments, centers, similarities = spherical_kmeans(
        vectors,
        int(algorithm["clusters"]),
        int(algorithm["kmeans_iterations"]),
        int(algorithm["random_seed"]),
    )
    for index, record in enumerate(records):
        record["cluster_id"] = f"CLUSTER_{assignments[index] + 1:03d}"
        record["cluster_similarity"] = similarities[index]

    summaries = build_cluster_summaries(
        records, assignments, similarities, centers, config, by_id
    )
    llm_errors: List[str] = []
    if args.use_llm:
        llm_errors = enrich_with_llm(summaries, config, args)

    section_counts: Counter = Counter()
    for record in records:
        for candidate in record["topic_candidates"]:
            section_counts[candidate["id"]] += 1
    controlled_nodes = []
    for node in config["nodes"]:
        item = dict(node)
        item["observed_section_count"] = section_counts[item["id"]]
        item["review_status"] = "种子节点待核验"
        controlled_nodes.append(item)

    run_id = datetime.now().strftime("%Y%m%d_%H%M%S")
    generated_at = datetime.now().astimezone().isoformat(timespec="seconds")
    candidate_taxonomy = {
        "schema_version": "1.0",
        "taxonomy_version": config["taxonomy_version"],
        "run_id": run_id,
        "generated_at": generated_at,
        "status": "待人工核验",
        "root": config["root"],
        "principles": config["principles"],
        "controlled_seed_nodes": controlled_nodes,
        "candidate_clusters": summaries,
        "aspect_tags": config.get("aspect_tags", []),
        "review_layers": {
            "controlled_seed_nodes": "人工预设的领域骨架，质量通常高于聚类建议，需核验定义和边界。",
            "candidate_clusters": "由章节文本聚类得到的发现性建议，可能受文体、单一文档或残余噪声影响。",
            "topic_review_queue.csv": "同时包含上述两层，row_type 用于区分。",
            "cluster_review_queue.csv": "只包含机器发现聚类，用于新建、拆分、合并或排噪审核。",
        },
        "important_notice": "聚类、规则和大模型结果均为候选，不得未经人工审核直接发布为正式分类体系。",
    }

    output_root = args.output_root
    output_root.mkdir(parents=True, exist_ok=True)
    write_json(output_root / "candidate_taxonomy.json", candidate_taxonomy)
    write_jsonl(output_root / "section_topic_candidates.jsonl", records)
    write_csv(
        output_root / "heading_cleaning_audit.csv",
        heading_audit,
        [
            "doc_id",
            "section_id",
            "level",
            "original_heading",
            "clean_heading",
            "quality_flags",
        ],
    )

    review_fields = [
        "row_type",
        "candidate_id",
        "current_label",
        "current_parent_id",
        "proposal_type",
        "section_count",
        "document_count",
        "seed_dominance",
        "top_seed_topics",
        "keywords",
        "representative_headings",
        "representative_excerpts",
        "quality_flags",
        "recommended_action",
        "llm_suggested_label",
        "llm_parent_id",
        "llm_definition",
        "llm_split_required",
        "llm_merge_with",
        "llm_confidence",
        "human_decision",
        "approved_label",
        "approved_parent_id",
        "definition",
        "include_scope",
        "exclude_scope",
        "reviewer",
        "review_notes",
    ]
    seed_review_rows = []
    for node in controlled_nodes:
        seed_review_rows.append(
            {
                "row_type": "controlled_seed_node",
                "candidate_id": node["id"],
                "current_label": node["name"],
                "current_parent_id": node.get("parent_id", ""),
                "proposal_type": "controlled-seed",
                "section_count": node["observed_section_count"],
                "document_count": "",
                "seed_dominance": "",
                "top_seed_topics": "",
                "keywords": "；".join(node.get("keywords", [])),
                "representative_headings": "",
                "representative_excerpts": "",
                "quality_flags": "零覆盖" if node["observed_section_count"] == 0 else "",
                "recommended_action": "核验名称、父节点、定义及纳入/排除边界",
                "human_decision": "",
                "approved_label": "",
                "approved_parent_id": "",
                "definition": "",
                "include_scope": "",
                "exclude_scope": "",
                "reviewer": "",
                "review_notes": "",
            }
        )

    cluster_review_rows = []
    for cluster in summaries:
        llm = cluster.get("llm_suggestion") or {}
        quality_flags = []
        if cluster["seed_dominance"] < 0.15:
            quality_flags.append("低种子一致性")
        if cluster["proposal_type"] == "aspect-dominated":
            quality_flags.append("可能按文体而非技术对象聚类")
        if cluster["document_count"] <= 1:
            quality_flags.append("单文档主导")
        cluster_review_rows.append(
            {
                "row_type": "discovered_cluster",
                "candidate_id": cluster["cluster_id"],
                "current_label": cluster["suggested_label"],
                "current_parent_id": cluster["suggested_parent_id"],
                "proposal_type": cluster["proposal_type"],
                "section_count": cluster["section_count"],
                "document_count": cluster["document_count"],
                "seed_dominance": cluster["seed_dominance"],
                "top_seed_topics": "；".join(
                    f"{item['name']}({item['section_count']})"
                    for item in cluster["top_seed_topics"]
                ),
                "llm_suggested_label": llm.get("suggested_label", ""),
                "llm_parent_id": llm.get("suggested_parent_id", ""),
                "llm_definition": llm.get("definition", ""),
                "llm_split_required": llm.get("split_required", ""),
                "llm_merge_with": llm.get("merge_with", ""),
                "llm_confidence": llm.get("confidence", ""),
                "keywords": "；".join(cluster["keywords"]),
                "representative_headings": "；".join(
                    row["heading"] for row in cluster["representative_sections"]
                ),
                "representative_excerpts": " || ".join(
                    re.sub(r"\s+", " ", row["excerpt"])[:260]
                    for row in cluster["representative_sections"][:3]
                ),
                "quality_flags": "；".join(quality_flags),
                "recommended_action": (
                    "优先映射到已有种子节点，必要时细化"
                    if cluster["proposal_type"] == "seed-supported"
                    else "判断是否新建、拆分、合并或作为噪声排除"
                ),
                "human_decision": "",
                "approved_label": "",
                "approved_parent_id": "",
                "definition": "",
                "include_scope": "",
                "exclude_scope": "",
                "reviewer": "",
                "review_notes": "",
            }
        )
    review_rows = seed_review_rows + cluster_review_rows
    write_csv(
        output_root / "topic_review_queue.csv",
        review_rows,
        review_fields,
    )
    write_csv(
        output_root / "cluster_review_queue.csv",
        cluster_review_rows,
        review_fields,
    )

    term_frequency: Counter = Counter()
    term_doc_frequency: Dict[str, set] = defaultdict(set)
    for record in records:
        for term in known_terms:
            count = record["analysis_text"].lower().count(term.lower())
            if count:
                term_frequency[term] += count
                term_doc_frequency[term].add(record["doc_id"])
    term_rows = []
    keyword_to_nodes: Dict[str, List[str]] = defaultdict(list)
    for node in config["nodes"]:
        for keyword in node.get("keywords", []):
            keyword_to_nodes[str(keyword)].append(str(node["id"]))
    for term, count in term_frequency.most_common():
        term_rows.append(
            {
                "term": term,
                "occurrence_count": count,
                "document_count": len(term_doc_frequency[term]),
                "seed_node_ids": "；".join(keyword_to_nodes.get(term, [])),
                "status": "种子术语待核验",
                "preferred_term": "",
                "aliases": "",
                "review_notes": "",
            }
        )
    write_csv(
        output_root / "term_review_queue.csv",
        term_rows,
        [
            "term",
            "occurrence_count",
            "document_count",
            "seed_node_ids",
            "status",
            "preferred_term",
            "aliases",
            "review_notes",
        ],
    )

    report = {
        "run_id": run_id,
        "generated_at": generated_at,
        "input_root": str(args.input_root.resolve()),
        "output_root": str(output_root.resolve()),
        "config": str(args.config.resolve()),
        "documents_loaded": len(documents),
        "sections_processed": len(records),
        "headings_audited": len(heading_audit),
        "skipped": dict(skipped),
        "tfidf_features": len(vocabulary),
        "clusters_requested": int(algorithm["clusters"]),
        "clusters_created": len(summaries),
        "controlled_seed_nodes": len(controlled_nodes),
        "topic_review_rows": len(review_rows),
        "sections_with_seed_topic": sum(bool(row["topic_candidates"]) for row in records),
        "sections_with_aspect": sum(bool(row["aspect_candidates"]) for row in records),
        "llm_enabled": bool(args.use_llm),
        "llm_model": args.llm_model if args.use_llm else None,
        "llm_errors": llm_errors,
        "outputs": [
            "candidate_taxonomy.json",
            "section_topic_candidates.jsonl",
            "topic_review_queue.csv",
            "cluster_review_queue.csv",
            "term_review_queue.csv",
            "heading_cleaning_audit.csv",
            "run_report.json",
        ],
    }
    write_json(output_root / "run_report.json", report)
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except KeyboardInterrupt:
        raise
    except Exception as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        raise SystemExit(1)
