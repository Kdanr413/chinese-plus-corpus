from __future__ import annotations

import argparse
import importlib.util
import json
import tempfile
import unittest
from pathlib import Path

from corpus_recognition import (
    CompositeTermExtractor,
    Document,
    KnowledgeSeedTermExtractor,
    LegacyLlmConsensusTermExtractor,
    LegacyStatisticalTermExtractor,
    TermExtractor,
    adapt_legacy_document,
)
from corpus_recognition.cli import main as recognition_main


PROJECT_ROOT = Path(__file__).resolve().parents[1]


def _load_legacy_extract_module():
    path = PROJECT_ROOT / "scripts/terms/extract.py"
    spec = importlib.util.spec_from_file_location("phase3_legacy_term_extract", path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class IndependentTermExtractorTests(unittest.TestCase):
    def test_statistical_extractor_needs_no_topic_or_term_table(self) -> None:
        document = Document(
            document_id="doc-1", source="test", raw_text="动力电池用于汽车。动力电池需要安全管理。动力电池可以回收。",
            clean_text="动力电池用于汽车。动力电池需要安全管理。动力电池可以回收。",
            processing_version="test/1", metadata={},
        )
        extractor = LegacyStatisticalTermExtractor(
            min_length=2, max_length=4, min_frequency=2, max_candidates=100
        )
        self.assertIsInstance(extractor, TermExtractor)
        rows = extractor.extract(document)
        battery = next(row for row in rows if row.surface_form == "动力电池")
        self.assertEqual(battery.document_id, "doc-1")
        self.assertEqual(battery.start_offset, document.clean_text.index("动力电池"))
        self.assertEqual(battery.end_offset, battery.start_offset + 4)
        self.assertIn("动力电池", battery.sentence)
        self.assertEqual(battery.extractor, "legacy_statistical")
        self.assertEqual(battery.evidence["occurrence_count"], 3)
        self.assertGreaterEqual(len(battery.evidence["occurrences"]), 3)
        self.assertTrue(battery.evidence["candidate_only"])

    def test_empty_knowledge_table_is_valid_and_optional(self) -> None:
        document = Document(
            document_id="doc-1", source="test", raw_text="动力电池", clean_text="动力电池",
            processing_version="test/1", metadata={},
        )
        self.assertEqual(KnowledgeSeedTermExtractor().extract(document), [])
        rows = CompositeTermExtractor([
            LegacyStatisticalTermExtractor(min_frequency=1),
            KnowledgeSeedTermExtractor(),
        ]).extract(document)
        self.assertTrue(rows)
        self.assertEqual({row.extractor for row in rows}, {"legacy_statistical"})

    def test_knowledge_seed_is_evidence_not_automatic_approval(self) -> None:
        document = Document(
            document_id="doc-1", source="test", raw_text="动力电池安全", clean_text="动力电池安全",
            processing_version="test/1", metadata={},
        )
        row = KnowledgeSeedTermExtractor({"动力电池": "TERM_001"}).extract(document)[0]
        self.assertEqual(row.status.value, "candidate")
        self.assertEqual(row.evidence["existing_term_id"], "TERM_001")
        self.assertTrue(row.evidence["knowledge_is_optional"])

    def test_legacy_llm_consensus_is_exact_source_and_traceable(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            sentences = root / "sentences.jsonl"
            results = root / "results.jsonl"
            sentences.write_text(json.dumps({
                "doc_id": "doc-1", "sentence_id": "s1", "text": "动力电池需要安全管理。"
            }, ensure_ascii=False) + "\n", encoding="utf-8")
            results.write_text("".join(json.dumps({
                "sentence_id": "s1", "terms": terms
            }, ensure_ascii=False) + "\n" for terms in (["动力电池", "原句外术语"], ["动力电池"])), encoding="utf-8")
            extractor = LegacyLlmConsensusTermExtractor.from_legacy_jsonl(
                results, sentences, minimum_votes=2
            )
            document = Document(
                document_id="doc-1", source="test", raw_text="动力电池需要安全管理。",
                clean_text="动力电池需要安全管理。", processing_version="test/1", metadata={},
            )
            rows = extractor.extract(document)
        self.assertEqual([row.surface_form for row in rows], ["动力电池"])
        self.assertEqual(rows[0].evidence["votes"], 2)
        self.assertEqual(rows[0].evidence["sentence_ids"], ["s1"])

    def test_legacy_document_adapter_preserves_id_source_and_checksums(self) -> None:
        row = adapt_legacy_document({
            "doc_id": "legacy-1", "source": "book", "text": "动力电池。", "title": "示例"
        })
        self.assertEqual(row.document_id, "legacy-1")
        self.assertEqual(row.source, "book")
        self.assertEqual(row.metadata["title"], "示例")
        self.assertEqual(len(row.clean_checksum or ""), 64)

    def test_cli_outputs_candidates_without_optional_resources(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            documents = root / "documents.jsonl"
            config = root / "term.json"
            output = root / "candidates.jsonl"
            documents.write_text(json.dumps({
                "doc_id": "doc-1", "source": "test",
                "text": "动力电池用于汽车。动力电池需要回收。"
            }, ensure_ascii=False) + "\n", encoding="utf-8")
            config.write_text(json.dumps({
                "min_length": 2, "max_length": 4, "min_frequency": 2,
                "max_candidates": 100, "weights": {"c_value": 0.7, "context_entropy": 0.3}
            }), encoding="utf-8")
            status = recognition_main([
                "extract-terms", "--documents", str(documents), "--config", str(config),
                "--output", str(output),
            ])
            rows = [json.loads(line) for line in output.read_text(encoding="utf-8").splitlines()]
        self.assertEqual(status, 0)
        self.assertTrue(any(row["surface_form"] == "动力电池" for row in rows))
        self.assertTrue(all(row["status"] == "candidate" for row in rows))


class LegacyCliCompatibilityTests(unittest.TestCase):
    def test_optional_empty_resources_match_explicit_empty_legacy_resources(self) -> None:
        legacy = _load_legacy_extract_module()
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            sections = root / "sections.jsonl"
            sentences = root / "sentences.jsonl"
            config = root / "config.json"
            taxonomy = root / "taxonomy.json"
            domain = root / "domain.csv"
            terminology = root / "terminology.csv"
            stop = root / "stop.txt"
            reference = root / "reference.csv"
            examples = root / "examples.jsonl"
            results = root / "results.jsonl"
            sections.write_text(json.dumps({
                "doc_id": "doc-1", "section_id": "sec-1",
                "heading": "动力电池", "text": "动力电池 动力电池 动力电池"
            }, ensure_ascii=False) + "\n", encoding="utf-8")
            sentences.write_text(json.dumps({
                "doc_id": "doc-1", "sentence_id": "s1", "text": "动力电池"
            }, ensure_ascii=False) + "\n", encoding="utf-8")
            config.write_text(json.dumps({
                "min_length": 2, "max_length": 4, "min_frequency": 1,
                "min_document_frequency": 1, "max_candidates": 100,
                "llm_repetitions": 2, "llm_min_consensus": 2, "max_llm_tasks": 10,
                "weights": {"c_value": 1.0}
            }), encoding="utf-8")
            taxonomy.write_text('{"nodes": []}', encoding="utf-8")
            domain.write_text("term,status\n", encoding="utf-8")
            terminology.write_text("term_id,preferred_term,status\n", encoding="utf-8")
            stop.write_text("", encoding="utf-8")
            reference.write_text("term,frequency,total_tokens\n", encoding="utf-8")
            examples.write_text(
                json.dumps({"text": "动力电池", "terms": ["动力电池"]}, ensure_ascii=False) + "\n"
                + json.dumps({"text": "驱动电机", "terms": ["驱动电机"]}, ensure_ascii=False) + "\n",
                encoding="utf-8",
            )
            results.write_text("", encoding="utf-8")

            explicit_output = root / "explicit.csv"
            optional_output = root / "optional.csv"
            common = dict(sections=sections, sentences=sentences, config=config, llm_results=None)
            explicit = argparse.Namespace(
                **common, taxonomy=taxonomy, domain_lexicon=domain, terminology=terminology,
                stop_terms=stop, reference_frequency=reference, few_shot_examples=examples,
                llm_tasks=root / "explicit_tasks.jsonl", output=explicit_output,
            )
            optional = argparse.Namespace(
                **common, taxonomy=None, domain_lexicon=None, terminology=None,
                stop_terms=None, reference_frequency=None, few_shot_examples=None,
                llm_tasks=None, output=optional_output,
            )
            explicit_metrics = legacy.extract(explicit)
            optional_metrics = legacy.extract(optional)
            self.assertEqual(explicit_output.read_bytes(), optional_output.read_bytes())
        self.assertGreater(optional_metrics["output_candidates"], 0)
        self.assertFalse(optional_metrics["topic_taxonomy_used"])
        self.assertFalse(optional_metrics["domain_lexicon_used"])
        self.assertEqual(explicit_metrics["output_candidates"], optional_metrics["output_candidates"])


if __name__ == "__main__":
    unittest.main()
