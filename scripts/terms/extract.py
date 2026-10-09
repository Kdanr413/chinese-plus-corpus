#!/usr/bin/env python3
"""Auditable Chinese domain-term extraction with LLM consensus import.

The workflow follows the paper-backed high-performing design: domain-explicit
two-shot prompts, sentence-level extraction, five repetitions, consensus >= 2,
then exact-source and domain-rule validation. C-value/context scoring remains a
reproducible fallback and a verifier, rather than being presented as the paper's
best method.
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import math
import os
import re
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any, Iterable, Iterator, Mapping

HAN_RUN = re.compile(r"[\u4e00-\u9fff]{2,}")
BOUNDARY_NOISE = set("的了和与及或在对将为由是有中上下一其该本各等可并使从")


def read_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8-sig"))


def read_jsonl(path: Path | None) -> Iterator[dict[str, Any]]:
    if path is None or not path.is_file():
        return
    with path.open("r", encoding="utf-8-sig") as handle:
        for line in handle:
            if line.strip():
                yield json.loads(line)


def read_csv(path: Path | None) -> list[dict[str, str]]:
    if path is None or not path.is_file():
        return []
    with path.open("r", encoding="utf-8-sig", newline="") as handle:
        return list(csv.DictReader(handle))


def write_csv(path: Path, rows: Iterable[Mapping[str, Any]], fields: list[str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)


def write_jsonl(path: Path, rows: Iterable[Mapping[str, Any]]) -> int:
    path.parent.mkdir(parents=True, exist_ok=True)
    count = 0
    with path.open("w", encoding="utf-8", newline="\n") as handle:
        for row in rows:
            handle.write(json.dumps(dict(row), ensure_ascii=False) + "\n")
            count += 1
    return count


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="中文领域术语候选提取")
    parser.add_argument("--sections", type=Path, required=True)
    parser.add_argument("--sentences", type=Path, required=True)
    parser.add_argument("--config", type=Path, required=True)
    parser.add_argument("--taxonomy", type=Path)
    parser.add_argument("--domain-lexicon", type=Path)
    parser.add_argument("--terminology", type=Path)
    parser.add_argument("--stop-terms", type=Path)
    parser.add_argument("--reference-frequency", type=Path)
    parser.add_argument("--few-shot-examples", type=Path)
    parser.add_argument("--llm-results", type=Path)
    parser.add_argument("--llm-tasks", type=Path)
    parser.add_argument("--output", type=Path, required=True)
    return parser.parse_args()


def load_protected(domain_path: Path | None, terminology_path: Path | None) -> dict[str, str]:
    protected: dict[str, str] = {}
    for row in read_csv(domain_path):
        if (row.get("status") or "active").lower() == "active" and row.get("term"):
            protected[row["term"].strip()] = ""
    for row in read_csv(terminology_path):
        if (row.get("status") or "active").lower() != "active":
            continue
        term_id = row.get("term_id", "")
        values = [row.get("preferred_term", ""), row.get("abbreviation", "")]
        values.extend(re.split(r"[|；;]", row.get("aliases", "")))
        for value in values:
            if value.strip():
                protected[value.strip()] = term_id
    return protected


def load_stop_terms(path: Path | None) -> set[str]:
    if path is None or not path.is_file():
        return set()
    return {
        line.strip() for line in path.read_text(encoding="utf-8-sig").splitlines()
        if line.strip() and not line.lstrip().startswith("#")
    }


def entropy(counter: Counter[str]) -> float:
    total = sum(counter.values())
    if total <= 1 or len(counter) <= 1:
        return 0.0
    value = -sum((count / total) * math.log2(count / total) for count in counter.values())
    return value / math.log2(len(counter))


def normalize(values: Mapping[str, float]) -> dict[str, float]:
    maximum = max(values.values(), default=0.0)
    return {key: (value / maximum if maximum else 0.0) for key, value in values.items()}


def candidates_from_run(run: str, minimum: int, maximum: int) -> Iterator[tuple[str, str, str]]:
    run = run[:160]
    for size in range(minimum, min(maximum, len(run)) + 1):
        for start in range(len(run) - size + 1):
            term = run[start : start + size]
            yield term, (run[start - 1] if start else "<B>"), (run[start + size] if start + size < len(run) else "<E>")


def build_llm_tasks(sentences_path: Path, examples_path: Path, output_path: Path, domain: str, maximum: int, repetitions: int, minimum_consensus: int) -> tuple[dict[str, dict[str, str]], int]:
    examples = list(read_jsonl(examples_path))
    if len(examples) < 2:
        raise ValueError("few_shot_examples.jsonl must contain at least two reviewed examples")
    sentences = list(read_jsonl(sentences_path))
    sentences.sort(key=lambda row: hashlib.sha256(str(row.get("sentence_id", "")).encode("utf-8")).hexdigest())
    prefix = (
        f"任务：从以下{domain}领域中文句子中直接抽取专业术语。"
        "只返回原句中逐字出现的词或短语，不补充解释，不生成原句外内容，输出 JSON 字符串数组。"
        "不要输出思维过程。\n"
        f"示例1：{examples[0].get('text', '')}\n答案：{json.dumps(examples[0].get('terms', []), ensure_ascii=False)}\n"
        f"示例2：{examples[1].get('text', '')}\n答案：{json.dumps(examples[1].get('terms', []), ensure_ascii=False)}\n"
    )
    tasks, lookup = [], {}
    for sentence in sentences[:maximum]:
        sentence_id = str(sentence.get("sentence_id", ""))
        text = str(sentence.get("text", ""))
        lookup[sentence_id] = {"text": text, "doc_id": str(sentence.get("doc_id", ""))}
        tasks.append({"sentence_id": sentence_id, "doc_id": sentence.get("doc_id", ""), "repetitions": repetitions, "minimum_consensus": minimum_consensus, "prompt": prefix + f"待抽取句子：{text}\n答案："})
    return lookup, write_jsonl(output_path, tasks)


def load_llm_consensus(results_path: Path, sentence_lookup: Mapping[str, Mapping[str, str]], minimum_votes: int, stop_terms: set[str]) -> tuple[Counter[str], dict[str, set[str]], dict[str, list[str]], int]:
    votes: dict[str, Counter[str]] = defaultdict(Counter)
    for row in read_jsonl(results_path):
        sentence_id = str(row.get("sentence_id", ""))
        if sentence_id not in sentence_lookup:
            continue
        for term in set(map(str, row.get("terms", []))):
            votes[sentence_id][term.strip()] += 1
    accepted: Counter[str] = Counter()
    documents: dict[str, set[str]] = defaultdict(set)
    evidence: dict[str, list[str]] = defaultdict(list)
    rejected = 0
    for sentence_id, term_votes in votes.items():
        source = sentence_lookup[sentence_id]
        for term, count in term_votes.items():
            valid = count >= minimum_votes and term in source["text"] and 2 <= len(term) <= 30 and term not in stop_terms and term[0] not in BOUNDARY_NOISE and term[-1] not in BOUNDARY_NOISE
            if not valid:
                rejected += 1
                continue
            accepted[term] += count
            documents[term].add(source["doc_id"])
            if len(evidence[term]) < 3:
                evidence[term].append(sentence_id)
    return accepted, documents, evidence, rejected


def extract(args: argparse.Namespace) -> dict[str, Any]:
    config = read_json(args.config)
    minimum, maximum = int(config.get("min_length", 2)), int(config.get("max_length", 6))
    min_frequency, min_df = int(config.get("min_frequency", 8)), int(config.get("min_document_frequency", 2))
    use_domain_lexicon = os.environ.get("CORPUS_USE_DOMAIN_LEXICON", "1") != "0"
    use_terminology = os.environ.get("CORPUS_USE_TERMINOLOGY", "1") != "0"
    domain_path = getattr(args, "domain_lexicon", None)
    terminology_path = getattr(args, "terminology", None)
    protected = load_protected(
        domain_path if use_domain_lexicon else None,
        terminology_path if use_terminology else None,
    )
    taxonomy_path = getattr(args, "taxonomy", None)
    taxonomy_enabled = (
        os.environ.get("CORPUS_USE_TOPIC_TAXONOMY", "1") != "0"
        and taxonomy_path is not None
        and taxonomy_path.is_file()
    )
    taxonomy = read_json(taxonomy_path) if taxonomy_enabled else {}
    taxonomy_terms = {
        str(value).strip()
        for node in taxonomy.get("nodes", [])
        for value in [node.get("name", ""), *node.get("representative_keywords", node.get("keywords", []))]
        if str(value).strip()
    } if taxonomy_enabled else set()
    stop_terms = load_stop_terms(getattr(args, "stop_terms", None))
    repetitions, minimum_consensus = int(config.get("llm_repetitions", 5)), int(config.get("llm_min_consensus", 2))
    sentence_lookup = {
        str(row.get("sentence_id", "")): {
            "text": str(row.get("text", "")), "doc_id": str(row.get("doc_id", ""))
        }
        for row in read_jsonl(args.sentences)
    }
    few_shot_path = getattr(args, "few_shot_examples", None)
    llm_tasks_path = getattr(args, "llm_tasks", None)
    if few_shot_path is not None and few_shot_path.is_file() and llm_tasks_path is not None:
        sentence_lookup, task_count = build_llm_tasks(
            args.sentences, few_shot_path, llm_tasks_path, "新能源与新能源汽车",
            int(config.get("max_llm_tasks", 5000)), repetitions, minimum_consensus,
        )
    else:
        task_count = write_jsonl(llm_tasks_path, []) if llm_tasks_path is not None else 0
    llm_frequency, llm_documents, llm_examples, llm_rejected = load_llm_consensus(
        getattr(args, "llm_results", None), sentence_lookup, minimum_consensus, stop_terms
    )

    frequency: Counter[str] = Counter()
    heading_frequency: Counter[str] = Counter()
    doc_sets: dict[str, set[str]] = defaultdict(set)
    left_context: dict[str, Counter[str]] = defaultdict(Counter)
    right_context: dict[str, Counter[str]] = defaultdict(Counter)
    examples: dict[str, list[str]] = defaultdict(list)
    doc_ids: set[str] = set()
    for section in read_jsonl(args.sections):
        doc_id, section_id = str(section.get("doc_id", "")), str(section.get("section_id", ""))
        doc_ids.add(doc_id)
        observed: set[str] = set()
        for source_text, is_heading in ((str(section.get("text", "")), False), (str(section.get("heading", "")), True)):
            for match in HAN_RUN.finditer(source_text):
                for term, left, right in candidates_from_run(match.group(0), minimum, maximum):
                    if term in stop_terms or term[0] in BOUNDARY_NOISE or term[-1] in BOUNDARY_NOISE:
                        continue
                    frequency[term] += 1
                    observed.add(term)
                    left_context[term][left] += 1
                    right_context[term][right] += 1
                    heading_frequency[term] += int(is_heading)
                    if len(examples[term]) < 3 and section_id not in examples[term]:
                        examples[term].append(section_id)
        for term in observed:
            doc_sets[term].add(doc_id)

    eligible = {term for term, count in frequency.items() if (count >= min_frequency and len(doc_sets[term]) >= min_df) or term in protected or term in taxonomy_terms}
    eligible.update(llm_frequency)
    for term in llm_frequency:
        frequency[term] = max(frequency[term], len(llm_documents[term]))
        doc_sets[term].update(llm_documents[term])
        examples[term].extend(item for item in llm_examples[term] if item not in examples[term])

    nested_sum, nested_count, nested_max = Counter(), Counter(), Counter()
    for longer in eligible:
        contained = {longer[start : start + size] for size in range(minimum, len(longer)) for start in range(len(longer) - size + 1)}
        for shorter in contained & eligible:
            nested_sum[shorter] += frequency[longer]
            nested_count[shorter] += 1
            nested_max[shorter] = max(nested_max[shorter], frequency[longer])
    c_values, contexts, coverages, headings, specificity = {}, {}, {}, {}, {}
    reference_rows = read_csv(getattr(args, "reference_frequency", None))
    reference = {row.get("term", ""): float(row.get("frequency") or 0) for row in reference_rows if row.get("term")}
    reference_total = max((float(row.get("total_tokens") or 0) for row in reference_rows), default=0.0)
    corpus_total = max(sum(frequency.values()), 1)
    for term in eligible:
        adjusted = frequency[term] - (nested_sum[term] / nested_count[term] if nested_count[term] else 0)
        c_values[term] = max(0.0, math.log2(len(term) + 1) * adjusted)
        contexts[term] = (entropy(left_context[term]) + entropy(right_context[term])) / 2
        coverages[term] = math.log1p(len(doc_sets[term]))
        headings[term] = math.log1p(heading_frequency[term])
        if reference_total and term in reference:
            specificity[term] = max(0.0, math.log2(((frequency[term] + 0.5) / corpus_total) / ((reference[term] + 0.5) / reference_total)))
        else:
            specificity[term] = 0.0
    components = {"c_value": normalize(c_values), "context_entropy": contexts, "document_coverage": normalize(coverages), "heading_evidence": normalize(headings), "domain_specificity": normalize(specificity), "llm_consensus": normalize({term: float(llm_frequency[term]) for term in eligible})}
    weights = config.get("weights", {})
    rows = []
    for term in eligible:
        nested_dominance = nested_max[term] / max(frequency[term], 1)
        is_reference = term in protected or term in taxonomy_terms
        if not is_reference and nested_dominance >= 0.75:
            continue
        if not is_reference and len(term) == 2 and nested_dominance >= 0.45:
            continue
        if not is_reference and len(term) == 2 and not (heading_frequency[term] >= 3 and contexts[term] >= 0.40):
            continue
        if not is_reference and len(term) >= 3 and not (contexts[term] >= 0.15 or heading_frequency[term] >= 1 or term in llm_frequency):
            continue
        score = sum(float(weights.get(name, 0.0)) * values.get(term, 0.0) for name, values in components.items())
        if term in protected:
            score = max(score, 1.0)
        source = "protected_lexicon" if term in protected else ("llm_consensus_verified" if term in llm_frequency else ("taxonomy_seed" if term in taxonomy_terms else "statistical_prefilter"))
        rows.append({"candidate_id": "TERM_CAND_" + hashlib.sha256(term.encode("utf-8")).hexdigest()[:12], "term": term, "term_id": protected.get(term, ""), "score": round(score, 6), "c_value": round(c_values[term], 6), "context_entropy": round(contexts[term], 6), "nested_dominance": round(nested_dominance, 6), "document_count": len(doc_sets[term]), "occurrence_count": frequency[term], "heading_count": heading_frequency[term], "domain_specificity": round(specificity[term], 6), "llm_consensus_votes": llm_frequency[term], "evidence_ids": "|".join(examples[term][:3]), "candidate_source": source, "status": "known_reference" if term in protected else "needs_review", "preferred_term": term if term in protected else "", "human_decision": "", "reviewer": "", "review_notes": ""})
    rows.sort(key=lambda row: (row["score"], row["document_count"], row["occurrence_count"], len(row["term"])), reverse=True)
    known = [row for row in rows if row["candidate_source"] == "protected_lexicon"]
    automatic = [row for row in rows if row["candidate_source"] != "protected_lexicon"]
    rows = known + automatic[: int(config.get("max_candidates", 2000))]
    fields = ["candidate_id", "term", "term_id", "score", "c_value", "context_entropy", "nested_dominance", "document_count", "occurrence_count", "heading_count", "domain_specificity", "llm_consensus_votes", "evidence_ids", "candidate_source", "status", "preferred_term", "human_decision", "reviewer", "review_notes"]
    write_csv(args.output, rows, fields)
    return {"method": config.get("method"), "method_reference": config.get("method_reference"), "prompt_strategy": config.get("prompt_strategy"), "documents": len(doc_ids), "raw_candidates": len(frequency), "eligible_candidates_before_precision_filter": len(eligible), "output_candidates": len(rows), "protected_terms": len(known), "taxonomy_seed_terms": sum(row["candidate_source"] == "taxonomy_seed" for row in rows), "llm_tasks": task_count, "llm_repetitions": repetitions, "llm_min_consensus": minimum_consensus, "llm_results_present": bool(llm_frequency), "llm_verified_terms": len(llm_frequency), "llm_rejected_outputs": llm_rejected, "reference_frequency_available": bool(reference_total), "domain_lexicon_used": use_domain_lexicon and bool(domain_path and domain_path.is_file()), "terminology_used": use_terminology and bool(terminology_path and terminology_path.is_file()), "topic_taxonomy_used": taxonomy_enabled}


def main() -> int:
    args = parse_args()
    metrics = extract(args)
    args.output.with_suffix(".metrics.json").write_text(
        json.dumps(metrics, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    print(json.dumps(metrics, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
