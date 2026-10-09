"""Independent, pluggable term-candidate extractors.

These classes produce candidates only. They never validate, approve or publish a term.
"""

from __future__ import annotations

import csv
import hashlib
import json
import math
import re
import shutil
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any, Callable, Iterable, Mapping, Sequence

from .models import Document, TermCandidate
from .term_engine import run_recognition_core


HAN_RUN = re.compile(r"[\u4e00-\u9fff]{2,}")
SENTENCE = re.compile(r"[^\n。！？!?；;]+(?:[。！？!?；;]+|$)")
BOUNDARY_NOISE = set("的了和与及或在对将为由是有中上下一其该本各等可并使从")


def _candidate_id(document_id: str, extractor: str, term: str) -> str:
    key = f"{document_id}\0{extractor}\0{term}".encode("utf-8")
    return "TERM_CAND_" + hashlib.sha256(key).hexdigest()[:16]


def _entropy(counter: Counter[str]) -> float:
    total = sum(counter.values())
    if total <= 1 or len(counter) <= 1:
        return 0.0
    value = -sum((count / total) * math.log2(count / total) for count in counter.values())
    return value / math.log2(len(counter))


def _normalize(values: Mapping[str, float]) -> dict[str, float]:
    maximum = max(values.values(), default=0.0)
    return {key: value / maximum if maximum else 0.0 for key, value in values.items()}


def _sentence_spans(text: str) -> list[tuple[int, int, str]]:
    spans: list[tuple[int, int, str]] = []
    for match in SENTENCE.finditer(text):
        raw = match.group(0)
        leading = len(raw) - len(raw.lstrip())
        sentence = raw.strip()
        if sentence:
            start = match.start() + leading
            spans.append((start, start + len(sentence), sentence))
    return spans


def _find_occurrences(text: str, term: str) -> list[dict[str, Any]]:
    sentences = _sentence_spans(text)
    occurrences: list[dict[str, Any]] = []
    start = 0
    while True:
        offset = text.find(term, start)
        if offset < 0:
            break
        sentence = next((value for left, right, value in sentences if left <= offset < right), "")
        occurrences.append({
            "start_offset": offset,
            "end_offset": offset + len(term),
            "sentence": sentence,
        })
        start = offset + max(len(term), 1)
    return occurrences


class EvaluatedCorpusTermExtractor:
    """Adapter for the separately evaluated corpus-level recognition method.

    The underlying engine must see the whole batch so that corpus frequency,
    nested-candidate and neighbouring-context evidence remain meaningful.  The
    adapter converts its accepted *candidates* back to the platform's existing
    per-analysis-unit :class:`TermCandidate` contract.  It never validates or
    approves a term.
    """

    name = "evaluated_grammar_bcc"
    version = "1.0"

    def __init__(
        self,
        *,
        config_path: str | Path,
        trusted_terms_path: str | Path,
        output_root: str | Path,
        cache_path: str | Path | None = None,
        offline: bool = False,
        max_candidates_per_document: int = 12,
    ) -> None:
        self.config_path = Path(config_path)
        self.trusted_terms_path = Path(trusted_terms_path)
        self.output_root = Path(output_root)
        self.cache_path = Path(cache_path) if cache_path is not None else None
        self.offline = offline
        self.max_candidates_per_document = max_candidates_per_document
        for required in (self.config_path, self.trusted_terms_path):
            if not required.is_file():
                raise FileNotFoundError(f"term recognition input does not exist: {required}")
        if max_candidates_per_document < 1:
            raise ValueError("max_candidates_per_document must be positive")

    @staticmethod
    def _input_row(document: Document) -> dict[str, Any]:
        metadata = document.metadata
        return {
            # Keep neighbouring sentence/turn units under their source document
            # for context checks while retaining the unit id for exact tracing.
            "doc_id": str(metadata.get("source_document_id") or document.document_id),
            "sentence_id": document.document_id,
            "text": document.clean_text,
            "source": document.source,
            "relative_path": str(metadata.get("relative_path") or ""),
            "title": str(metadata.get("title") or ""),
            "domain": str(metadata.get("domain") or ""),
            "source_type": str(metadata.get("source_type") or document.source),
            "text_genre": str(metadata.get("text_genre") or ""),
        }

    @staticmethod
    def _read_jsonl(path: Path) -> list[dict[str, Any]]:
        with path.open("r", encoding="utf-8-sig") as handle:
            return [json.loads(line) for line in handle if line.strip()]

    def extract_many(
        self,
        documents: Sequence[Document],
        *,
        progress_callback: Callable[[float, str], None] | None = None,
    ) -> dict[str, list[TermCandidate]]:
        results = {document.document_id: [] for document in documents}
        if not documents:
            return results

        self.output_root.mkdir(parents=True, exist_ok=True)
        input_path = self.output_root / "input_units.jsonl"
        with input_path.open("w", encoding="utf-8", newline="\n") as handle:
            for document in documents:
                handle.write(json.dumps(self._input_row(document), ensure_ascii=False) + "\n")

        run_cache = self.output_root / "bcc_frequency_cache.json"
        if self.cache_path is not None and self.cache_path.is_file():
            shutil.copy2(self.cache_path, run_cache)

        report = run_recognition_core(
            input_path,
            self.trusted_terms_path,
            self.config_path,
            self.output_root,
            offline=self.offline,
            progress_callback=progress_callback,
        )
        if self.cache_path is not None and run_cache.is_file():
            self.cache_path.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(run_cache, self.cache_path)

        with (self.output_root / "term_candidates.csv").open(
            "r", encoding="utf-8-sig", newline=""
        ) as handle:
            candidate_rows = list(csv.DictReader(handle))
        occurrence_rows = self._read_jsonl(self.output_root / "term_occurrences.jsonl")
        occurrences_by_term_and_unit: dict[tuple[str, str], list[dict[str, Any]]] = defaultdict(list)
        for occurrence in occurrence_rows:
            unit_id = str(occurrence.get("sentence_id") or "")
            term = str(occurrence.get("term") or "")
            if unit_id in results and term:
                occurrences_by_term_and_unit[(term, unit_id)].append(occurrence)

        for candidate_row in candidate_rows:
            term = str(candidate_row.get("term") or "").strip()
            if not term:
                continue
            try:
                score = float(candidate_row["score"]) if candidate_row.get("score") else None
            except ValueError:
                score = None
            for document in documents:
                occurrences = occurrences_by_term_and_unit.get((term, document.document_id), [])
                if not occurrences:
                    continue
                first = occurrences[0]
                original_start = document.clean_text.find(term)
                if original_start >= 0:
                    start_offset = original_start
                    end_offset = original_start + len(term)
                    display_sentence = document.clean_text
                else:
                    # Some technical expressions change under NFKC (for
                    # example full-width Latin letters).  Retain the engine's
                    # trace rather than inventing an invalid original offset.
                    start_offset = int(first["start_offset"])
                    end_offset = int(first["end_offset"])
                    display_sentence = str(first.get("sentence") or document.clean_text)
                evidence = {
                    "method": "evaluated configurable grammar + boundary/nesting gates + BCC risk gate",
                    "formal_candidate": dict(candidate_row),
                    "occurrences": occurrences,
                    "recognition_report": {
                        "source_documents": report.get("source_documents", 0),
                        "analysis_sentences": report.get("analysis_sentences", 0),
                        "candidate_terms": report.get("candidate_terms", 0),
                        "candidate_grammar": report.get("candidate_grammar", {}),
                        "bcc_general_frequency": report.get("bcc_general_frequency", {}),
                    },
                    "candidate_only": True,
                }
                results[document.document_id].append(TermCandidate(
                    term_id=_candidate_id(document.document_id, self.name, term),
                    document_id=document.document_id,
                    surface_form=term,
                    normalized_form=term,
                    start_offset=start_offset,
                    end_offset=end_offset,
                    sentence=display_sentence,
                    extractor=self.name,
                    extractor_version=self.version,
                    score=score,
                    evidence=evidence,
                ))

        for document_id, rows in results.items():
            # ``term_candidates.csv`` is already ranked by the evaluated engine.
            results[document_id] = rows[: self.max_candidates_per_document]
        return results


class LegacyStatisticalTermExtractor:
    """Document-level form of the existing character n-gram/C-value fallback."""

    name = "legacy_statistical"
    version = "1.0"

    def __init__(
        self,
        *,
        min_length: int = 2,
        max_length: int = 6,
        min_frequency: int = 2,
        max_candidates: int = 2000,
        stop_terms: Iterable[str] = (),
        weights: Mapping[str, float] | None = None,
    ) -> None:
        if min_length < 2 or max_length < min_length:
            raise ValueError("term length range is invalid")
        self.min_length = min_length
        self.max_length = max_length
        self.min_frequency = min_frequency
        self.max_candidates = max_candidates
        self.stop_terms = {value.strip() for value in stop_terms if value.strip()}
        self.weights = dict(weights or {"c_value": 0.7, "context_entropy": 0.3})

    @classmethod
    def from_config(
        cls, path: str | Path, *, stop_terms: Iterable[str] = (),
        min_frequency_override: int | None = None,
        max_candidates_override: int | None = None,
    ) -> "LegacyStatisticalTermExtractor":
        config = json.loads(Path(path).read_text(encoding="utf-8-sig"))
        return cls(
            min_length=int(config.get("min_length", 2)),
            max_length=int(config.get("max_length", 6)),
            min_frequency=(
                int(min_frequency_override)
                if min_frequency_override is not None
                else int(config.get("min_frequency", 2))
            ),
            max_candidates=(
                int(max_candidates_override)
                if max_candidates_override is not None
                else int(config.get("max_candidates", 2000))
            ),
            stop_terms=stop_terms,
            weights=config.get("weights", {}),
        )

    def extract(self, document: Document) -> list[TermCandidate]:
        frequency: Counter[str] = Counter()
        left_context: dict[str, Counter[str]] = defaultdict(Counter)
        right_context: dict[str, Counter[str]] = defaultdict(Counter)
        occurrences: dict[str, list[dict[str, Any]]] = defaultdict(list)
        for sentence_start, _, sentence in _sentence_spans(document.clean_text):
            for run_match in HAN_RUN.finditer(sentence):
                run = run_match.group(0)[:160]
                base = sentence_start + run_match.start()
                for size in range(self.min_length, min(self.max_length, len(run)) + 1):
                    for local in range(len(run) - size + 1):
                        term = run[local : local + size]
                        if term in self.stop_terms or term[0] in BOUNDARY_NOISE or term[-1] in BOUNDARY_NOISE:
                            continue
                        frequency[term] += 1
                        left_context[term][run[local - 1] if local else "<B>"] += 1
                        right = local + size
                        right_context[term][run[right] if right < len(run) else "<E>"] += 1
                        if len(occurrences[term]) < 20:
                            occurrences[term].append({
                                "start_offset": base + local,
                                "end_offset": base + local + size,
                                "sentence": sentence,
                            })
        eligible = {term for term, count in frequency.items() if count >= self.min_frequency}
        nested_sum: Counter[str] = Counter()
        nested_count: Counter[str] = Counter()
        for longer in eligible:
            contained = {
                longer[start : start + size]
                for size in range(self.min_length, len(longer))
                for start in range(len(longer) - size + 1)
            }
            for shorter in contained & eligible:
                nested_sum[shorter] += frequency[longer]
                nested_count[shorter] += 1
        c_values = {
            term: max(
                0.0,
                math.log2(len(term) + 1)
                * (frequency[term] - (nested_sum[term] / nested_count[term] if nested_count[term] else 0)),
            )
            for term in eligible
        }
        context_values = {
            term: (_entropy(left_context[term]) + _entropy(right_context[term])) / 2
            for term in eligible
        }
        normalized_c = _normalize(c_values)
        rows: list[TermCandidate] = []
        for term in eligible:
            first = occurrences[term][0]
            score = (
                float(self.weights.get("c_value", 0.0)) * normalized_c[term]
                + float(self.weights.get("context_entropy", 0.0)) * context_values[term]
            )
            rows.append(TermCandidate(
                term_id=_candidate_id(document.document_id, self.name, term),
                document_id=document.document_id,
                surface_form=term,
                normalized_form=term,
                start_offset=first["start_offset"],
                end_offset=first["end_offset"],
                sentence=first["sentence"],
                extractor=self.name,
                extractor_version=self.version,
                score=round(score, 6),
                evidence={
                    "occurrence_count": frequency[term],
                    "c_value": round(c_values[term], 6),
                    "context_entropy": round(context_values[term], 6),
                    "occurrences": occurrences[term],
                    "candidate_only": True,
                },
            ))
        rows.sort(
            key=lambda row: (
                row.score or 0.0,
                int(row.evidence["occurrence_count"]),
                len(row.surface_form),
                row.surface_form,
            ),
            reverse=True,
        )
        return rows[: self.max_candidates]


class LegacyLlmConsensusTermExtractor:
    """Adapts already-recorded legacy multi-run LLM terms; it makes no model call."""

    name = "legacy_llm_consensus"
    version = "1.0"

    def __init__(self, terms_by_document: Mapping[str, Mapping[str, Mapping[str, Any]]]) -> None:
        self.terms_by_document = {
            str(doc_id): {str(term): dict(evidence) for term, evidence in terms.items()}
            for doc_id, terms in terms_by_document.items()
        }

    @classmethod
    def from_legacy_jsonl(
        cls,
        results_path: str | Path,
        sentences_path: str | Path,
        *,
        minimum_votes: int = 2,
    ) -> "LegacyLlmConsensusTermExtractor":
        sentences: dict[str, dict[str, Any]] = {}
        with Path(sentences_path).open("r", encoding="utf-8-sig") as handle:
            for line in handle:
                if line.strip():
                    row = json.loads(line)
                    sentences[str(row.get("sentence_id", ""))] = row
        votes: dict[str, Counter[str]] = defaultdict(Counter)
        with Path(results_path).open("r", encoding="utf-8-sig") as handle:
            for line in handle:
                if not line.strip():
                    continue
                row = json.loads(line)
                sentence_id = str(row.get("sentence_id", ""))
                for term in set(map(str, row.get("terms", []))):
                    votes[sentence_id][term.strip()] += 1
        by_document: dict[str, dict[str, dict[str, Any]]] = defaultdict(dict)
        for sentence_id, term_votes in votes.items():
            sentence = sentences.get(sentence_id)
            if not sentence:
                continue
            document_id = str(sentence.get("doc_id", ""))
            text = str(sentence.get("text", ""))
            for term, count in term_votes.items():
                if count < minimum_votes or term not in text or len(term) < 2:
                    continue
                evidence = by_document[document_id].setdefault(
                    term, {"votes": 0, "sentence_ids": [], "minimum_votes": minimum_votes}
                )
                evidence["votes"] += count
                evidence["sentence_ids"].append(sentence_id)
        return cls(by_document)

    def extract(self, document: Document) -> list[TermCandidate]:
        rows: list[TermCandidate] = []
        for term, llm_evidence in self.terms_by_document.get(document.document_id, {}).items():
            found = _find_occurrences(document.clean_text, term)
            if not found:
                continue
            first = found[0]
            rows.append(TermCandidate(
                term_id=_candidate_id(document.document_id, self.name, term),
                document_id=document.document_id,
                surface_form=term,
                normalized_form=term,
                start_offset=first["start_offset"],
                end_offset=first["end_offset"],
                sentence=first["sentence"],
                extractor=self.name,
                extractor_version=self.version,
                score=None,
                evidence={**llm_evidence, "occurrences": found, "candidate_only": True},
            ))
        return sorted(rows, key=lambda row: row.surface_form)


class KnowledgeSeedTermExtractor:
    """Optional exact-match seed extractor; an empty seed set is valid."""

    name = "legacy_knowledge_seed"
    version = "1.0"

    def __init__(self, seeds: Mapping[str, str] | Iterable[str] = ()) -> None:
        if isinstance(seeds, Mapping):
            self.seeds = {str(term).strip(): str(term_id) for term, term_id in seeds.items() if str(term).strip()}
        else:
            self.seeds = {str(term).strip(): "" for term in seeds if str(term).strip()}

    @classmethod
    def from_csv(cls, path: str | Path | None) -> "KnowledgeSeedTermExtractor":
        if path is None or not Path(path).is_file():
            return cls()
        seeds: dict[str, str] = {}
        with Path(path).open("r", encoding="utf-8-sig", newline="") as handle:
            for row in csv.DictReader(handle):
                term = str(row.get("preferred_term") or row.get("term") or "").strip()
                if term and str(row.get("status") or "active").lower() in {"active", "approved", "通过", "启用"}:
                    seeds[term] = str(row.get("term_id") or "")
        return cls(seeds)

    def extract(self, document: Document) -> list[TermCandidate]:
        rows: list[TermCandidate] = []
        for term, existing_term_id in self.seeds.items():
            found = _find_occurrences(document.clean_text, term)
            if not found:
                continue
            first = found[0]
            rows.append(TermCandidate(
                term_id=_candidate_id(document.document_id, self.name, term),
                document_id=document.document_id,
                surface_form=term,
                normalized_form=term,
                start_offset=first["start_offset"],
                end_offset=first["end_offset"],
                sentence=first["sentence"],
                extractor=self.name,
                extractor_version=self.version,
                score=None,
                evidence={
                    "existing_term_id": existing_term_id,
                    "occurrences": found,
                    "candidate_only": True,
                    "knowledge_is_optional": True,
                },
            ))
        return sorted(rows, key=lambda row: row.surface_form)


class CompositeTermExtractor:
    """Run any configured extractor list while preserving each extractor's evidence."""

    name = "composite"
    version = "1.0"

    def __init__(self, extractors: Sequence[Any]) -> None:
        self.extractors = list(extractors)

    def extract(self, document: Document) -> list[TermCandidate]:
        rows: list[TermCandidate] = []
        for extractor in self.extractors:
            rows.extend(extractor.extract(document))
        return rows
