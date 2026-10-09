from __future__ import annotations

import csv
import tempfile
import unittest
from pathlib import Path

from corpus_recognition.models import Document
from corpus_recognition.term import EvaluatedCorpusTermExtractor
from corpus_recognition.term_engine import (
    apply_boundary_completion_gate,
    apply_nested_candidate_gate,
)


PROJECT_ROOT = Path(__file__).resolve().parents[1]


def unit(document_id: str, text: str, *, source_document_id: str = "policy_001") -> Document:
    return Document(
        document_id=document_id,
        source="test",
        raw_text=text,
        clean_text=text,
        processing_version="test/1.0",
        metadata={
            "source_document_id": source_document_id,
            "analysis_unit_type": "sentence",
            "domain": "能源",
            "text_genre": "记叙文",
        },
    )


class EvaluatedCorpusTermExtractorTests(unittest.TestCase):
    def extractor(self, output: Path, *, maximum: int = 20) -> EvaluatedCorpusTermExtractor:
        return EvaluatedCorpusTermExtractor(
            config_path=PROJECT_ROOT / "configs/term_recognition/recognition.json",
            trusted_terms_path=PROJECT_ROOT / "configs/term_recognition/trusted_terms.csv",
            output_root=output,
            cache_path=output / "persistent_cache.json",
            offline=True,
            max_candidates_per_document=maximum,
        )

    def test_evaluated_engine_replaces_boundary_fragments_with_traceable_candidate(self) -> None:
        documents = [
            unit("s001", "2．大力发展新能源。"),
            unit("s002", "积极发展新能源和新型储能。"),
        ]
        with tempfile.TemporaryDirectory() as temporary:
            output = Path(temporary) / "engine_evidence"
            results = self.extractor(output).extract_many(documents)

            first_terms = {row.surface_form for row in results["s001"]}
            self.assertIn("新能源", first_terms)
            self.assertTrue(first_terms.isdisjoint({
                "大力发", "大力发展", "大力发展新", "大力发展新能", "发展新能源",
            }))
            candidate = next(row for row in results["s001"] if row.surface_form == "新能源")
            self.assertEqual(candidate.extractor, "evaluated_grammar_bcc")
            self.assertEqual(candidate.sentence, documents[0].clean_text)
            self.assertEqual(
                documents[0].clean_text[candidate.start_offset:candidate.end_offset],
                "新能源",
            )
            self.assertTrue(candidate.evidence["candidate_only"])
            self.assertTrue((output / "term_candidates.csv").is_file())
            self.assertTrue((output / "term_rejections.csv").is_file())
            self.assertTrue((output / "term_occurrences.jsonl").is_file())

    def test_corpus_frequency_and_grammar_evidence_are_preserved_per_unit(self) -> None:
        documents = [
            unit("s101", "虚拟电厂参与电力调峰。", source_document_id="doc_a"),
            unit("s102", "虚拟电厂能够提高调节能力。", source_document_id="doc_b"),
        ]
        with tempfile.TemporaryDirectory() as temporary:
            output = Path(temporary) / "engine_evidence"
            results = self.extractor(output).extract_many(documents)

            for document in documents:
                candidate = next(
                    row for row in results[document.document_id]
                    if row.surface_form == "虚拟电厂"
                )
                formal = candidate.evidence["formal_candidate"]
                self.assertEqual(formal["occurrence_count"], "2")
                self.assertIn("engineering_process", formal["grammar_profiles"])
                self.assertEqual(candidate.document_id, document.document_id)

            with (output / "term_candidates.csv").open(
                "r", encoding="utf-8-sig", newline=""
            ) as handle:
                engine_terms = {row["term"] for row in csv.DictReader(handle)}
            self.assertIn("虚拟电厂", engine_terms)

    def test_long_term_gates_expose_cancellable_progress_checkpoints(self) -> None:
        def cancel(*_args: object) -> None:
            raise RuntimeError("cancel checkpoint")

        with self.assertRaisesRegex(RuntimeError, "cancel checkpoint"):
            apply_boundary_completion_gate(
                [], [],
                [{"term": "新能源", "sentence_id": "s1", "start_offset": 0, "end_offset": 3}],
                [{"sentence_id": "s1", "tokens": []}],
                progress_callback=cancel,
            )
        with self.assertRaisesRegex(RuntimeError, "cancel checkpoint"):
            apply_nested_candidate_gate(
                [{"term": "新能源"}], [], [],
                progress_callback=cancel,
            )


if __name__ == "__main__":
    unittest.main()
