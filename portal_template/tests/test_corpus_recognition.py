from __future__ import annotations

import json
import os
import tempfile
import unittest
import uuid
from pathlib import Path

from pydantic import ValidationError

from corpus_recognition import (
    AggregationStrategy,
    Document,
    PromptConfigurationError,
    RecognitionRepository,
    TermCandidate,
    TopicCandidate,
    ValidatorDecision,
    ValidatorResult,
    load_prompt,
    load_recognition_config,
)
from corpus_registry import CorpusRegistry, DatabaseConfigurationError, DatabaseSettings
from corpus_registry.db import connect
from corpus_registry.migrations import initialize_database


PROJECT_ROOT = Path(__file__).resolve().parents[1]


class RecognitionContractTests(unittest.TestCase):
    def test_document_accepts_legacy_doc_id_without_changing_public_name(self) -> None:
        document = Document.model_validate({
            "doc_id": "legacy-1", "source": "test", "raw_text": "原文",
            "clean_text": "原文", "metadata": {}, "processing_version": "1.0",
        })
        self.assertEqual(document.document_id, "legacy-1")
        self.assertIn("document_id", document.model_dump())
        self.assertNotIn("doc_id", document.model_dump())

    def test_term_offsets_are_consistent_and_score_can_be_null(self) -> None:
        candidate = TermCandidate(
            term_id="term-1", document_id="doc-1", surface_form="劳动合同",
            normalized_form="劳动合同", extractor="test", extractor_version="1",
        )
        self.assertIsNone(candidate.score)
        with self.assertRaises(ValidationError):
            TermCandidate(
                term_id="bad", document_id="doc-1", surface_form="合同",
                normalized_form="合同", start_offset=5, end_offset=2,
                extractor="test", extractor_version="1",
            )

    def test_topic_candidate_requires_document_or_cluster(self) -> None:
        with self.assertRaises(ValidationError):
            TopicCandidate(
                topic_candidate_id="topic-1", topic_name="入职",
                source_method="test",
            )

    def test_validator_result_requires_reason_evidence_and_prompt_version(self) -> None:
        result = ValidatorResult(
            validator="validator-a", validator_version="1", model="model-a",
            model_version="2026-01", prompt_id="recognition.term_validation",
            prompt_version="1.0.0", decision=ValidatorDecision.ACCEPT,
            reason="文本中具有稳定领域含义", evidence={"sentence": "劳动合同"},
        )
        self.assertEqual(result.decision, ValidatorDecision.ACCEPT)

    def test_config_is_model_configurable_and_contains_no_secret(self) -> None:
        source = PROJECT_ROOT / "configs/recognition.json"
        config = load_recognition_config(source)
        self.assertEqual(config.topic_identification.mode, "auto")
        self.assertEqual(config.validation.strategy, AggregationStrategy.MAJORITY)
        self.assertGreaterEqual(len(config.validation.models), 1)
        text = source.read_text(encoding="utf-8")
        self.assertNotIn("sk-", text)
        self.assertIn("RECOGNITION_API_KEY", text)

    def test_three_versioned_prompts_load_with_checksums(self) -> None:
        prompt_root = PROJECT_ROOT / "configs/prompts/recognition"
        paths = [
            prompt_root / "term_validation/v1.json",
            prompt_root / "topic_validation/v1.json",
            prompt_root / "term_topic_cross_validation/v1.json",
        ]
        ids = set()
        for path in paths:
            snapshot = load_prompt(path)
            ids.add(snapshot.template.prompt_id)
            self.assertEqual(len(snapshot.checksum_sha256), 64)
            self.assertEqual(snapshot.template.prompt_version, "1.0.0")
        self.assertEqual(len(ids), 3)

    def test_invalid_prompt_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "bad.json"
            path.write_text("{}", encoding="utf-8")
            with self.assertRaises(PromptConfigurationError):
                load_prompt(path)

    def test_migration_is_additive_and_defines_first_class_results(self) -> None:
        sql = (PROJECT_ROOT / "db/migrations/0002_recognition_validation.sql").read_text(encoding="utf-8")
        tables = (
            "recognition_runs", "recognition_documents", "term_candidates",
            "topic_candidates", "validation_runs", "cross_validation_cases",
            "cross_validation_terms", "validator_results", "validation_aggregates",
            "resource_decisions", "human_reviews",
        )
        for table in tables:
            self.assertIn(f"CREATE TABLE {table}", sql)
        self.assertNotIn("ALTER TABLE corpus_versions", sql)
        self.assertIn("prompt_version", sql)
        self.assertIn("raw_response_asset_path", sql)
        self.assertIn("before_state", sql)
        self.assertIn("after_state", sql)


def _postgresql_is_configured() -> bool:
    try:
        DatabaseSettings.from_environment()
    except DatabaseConfigurationError:
        return False
    return True


@unittest.skipUnless(
    _postgresql_is_configured(),
    "set DB_NAME, DB_USER and DB_PASSWORD to run PostgreSQL integration tests",
)
class RecognitionPostgreSQLTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.settings = DatabaseSettings.from_environment()
        initialize_database(cls.settings, PROJECT_ROOT / "db/migrations")

    def test_migration_is_registered_idempotent_and_schema_is_real(self) -> None:
        self.assertEqual(initialize_database(self.settings), ())
        expected = {
            "recognition_runs", "recognition_documents", "term_candidates",
            "topic_candidates", "validation_runs", "cross_validation_cases",
            "cross_validation_terms", "validator_results", "validation_aggregates",
            "resource_decisions", "human_reviews",
        }
        with connect(self.settings) as connection:
            applied = connection.execute(
                "SELECT 1 FROM schema_migrations WHERE version=%s",
                ("0002_recognition_validation.sql",),
            ).fetchone()
            tables = {
                row["tablename"]
                for row in connection.execute(
                    "SELECT tablename FROM pg_tables WHERE schemaname='public' AND tablename = ANY(%s)",
                    (list(expected),),
                ).fetchall()
            }
            constraint_count = connection.execute(
                """SELECT count(*) AS count FROM pg_constraint
                   WHERE conrelid IN (
                       SELECT oid FROM pg_class WHERE relname = ANY(%s)
                   ) AND contype IN ('f', 'c', 'u', 'p')""",
                (list(expected),),
            ).fetchone()["count"]
            index_count = connection.execute(
                """SELECT count(*) AS count FROM pg_indexes
                   WHERE schemaname='public' AND tablename = ANY(%s)""",
                (list(expected),),
            ).fetchone()["count"]
        self.assertIsNotNone(applied)
        self.assertEqual(tables, expected)
        self.assertGreaterEqual(constraint_count, 30)
        self.assertGreaterEqual(index_count, 20)

    def test_recognition_evidence_crud_and_provenance(self) -> None:
        corpus_key = f"recognition-{uuid.uuid4().hex[:10]}"
        with tempfile.TemporaryDirectory() as directory:
            data_root = Path(directory)
            registry = CorpusRegistry(self.settings, data_root=data_root)
            recognition = RecognitionRepository(self.settings)
            try:
                corpus = registry.create_corpus(corpus_key, "识别测试语料库")
                job = registry.create_processing_job(
                    corpus_key, data_root / "workspaces" / "run-1", "recognition",
                    parameters={"test": True},
                )
                run = recognition.create_run(
                    job["id"], pipeline_version="recognition-contract/1.0",
                    config_snapshot={"strategy": "majority"},
                    prompt_snapshot={"term": {"version": "1.0.0"}},
                )
                document = recognition.add_document(
                    run["id"], document_id="doc-1", source="unit-test",
                    raw_asset_path="workspaces/run-1/recognition/01_preprocessed/raw.txt",
                    clean_asset_path="workspaces/run-1/recognition/01_preprocessed/clean.txt",
                    processing_version="1.0", metadata={"language": "zh"},
                )
                second_document = recognition.add_document(
                    run["id"], document_id="doc-2", source="unit-test",
                    raw_asset_path="workspaces/run-1/recognition/01_preprocessed/raw-2.txt",
                    clean_asset_path="workspaces/run-1/recognition/01_preprocessed/clean-2.txt",
                    processing_version="1.0", metadata={"language": "zh"},
                )
                batch_decision_id = uuid.uuid4()
                self.assertEqual(recognition.add_resource_decisions_batch(run["id"], [{
                    "id": batch_decision_id,
                    "document_row_id": second_document["id"],
                    "term_validation_status": "accept",
                    "topic_validation_status": "accept",
                    "cross_validation_status": None,
                    "final_decision": "auto_accept",
                    "decision_rule": "batch-test",
                    "reason": "批量决策写入测试",
                    "lifecycle_status": "approved",
                }]), 1)
                term = recognition.add_term_candidate(
                    run["id"], document["id"], candidate_term_id="term-1",
                    surface_form="劳动合同", normalized_form="劳动合同",
                    start_offset=0, end_offset=4, sentence="签订劳动合同。",
                    extractor="contract-test", extractor_version="1", score=0.75,
                    evidence={"rule": "noun-pattern"},
                )
                batch_term_ids = recognition.add_term_candidates_batch(run["id"], [
                    {
                        "document_row_id": document["id"],
                        "candidate_term_id": "term-2",
                        "surface_form": "试用期",
                        "normalized_form": "试用期",
                        "sentence": "约定试用期。",
                        "extractor": "contract-test",
                        "extractor_version": "1",
                        "evidence": {"rule": "batch"},
                    },
                    {
                        "document_row_id": document["id"],
                        "candidate_term_id": "term-3",
                        "surface_form": "合同期限",
                        "normalized_form": "合同期限",
                        "sentence": "明确合同期限。",
                        "extractor": "contract-test",
                        "extractor_version": "1",
                        "evidence": {"rule": "batch"},
                    },
                ])
                self.assertEqual(set(batch_term_ids), {"term-2", "term-3"})
                self.assertEqual(
                    recognition.update_term_candidate_status_for_run(run["id"], "validated"),
                    3,
                )
                topic = recognition.add_topic_candidate(
                    run["id"], candidate_topic_id="topic-1", topic_name="录用与入职",
                    source_method="classification", resolution="existing",
                    document_row_id=document["id"], keywords=["合同", "入职"],
                )
                batch_topic_ids = recognition.add_topic_candidates_batch(run["id"], [{
                    "candidate_topic_id": "topic-2",
                    "topic_name": "合同管理",
                    "source_method": "classification",
                    "resolution": "existing",
                    "document_row_id": second_document["id"],
                    "keywords": ["合同"],
                    "evidence": {"rule": "batch"},
                }])
                self.assertEqual(set(batch_topic_ids), {"topic-2"})
                counts = recognition.get_run_counts(run["id"])
                self.assertEqual(counts["documents"], 2)
                self.assertEqual(counts["term_candidates"], 3)
                self.assertEqual(counts["topic_candidates"], 2)
                validation_run = recognition.create_validation_run(
                    run["id"], validation_kind="term", aggregation_strategy="majority",
                    min_validators=1, validator_config=[{"id": "validator-a"}],
                )
                result = recognition.record_validator_result(
                    validation_run["id"], subject_type="term", subject_id=term["id"],
                    validator="validator-a", validator_version="1", model="test-model",
                    model_version="1", prompt_id="recognition.term_validation",
                    prompt_version="1.0.0", decision="accept", reason="领域表达稳定",
                    structured_result={"domain_relevance": 0.8},
                    evidence={"sentence": "签订劳动合同。"}, duration_ms=2,
                )
                aggregate = recognition.record_aggregate(
                    validation_run["id"], subject_type="term", subject_id=term["id"],
                    validator_count=1, accept_count=1, reject_count=0,
                    review_count=0, uncertain_count=0, agreement_score=1.0,
                    aggregation_strategy="majority", aggregated_decision="accept",
                    conflict=False, details={"validator_result_ids": [str(result["id"])]},
                )
                cross_case = recognition.add_cross_validation_case(
                    run["id"], document["id"], term_candidate_ids=[term["id"]],
                    topic_candidate_id=topic["id"],
                )
                decision = recognition.create_resource_decision(
                    run["id"], document["id"], term_validation_status="accept",
                    topic_validation_status=None, cross_validation_status=None,
                    final_decision="pending", decision_rule="await_all_three",
                    reason="话题和交叉验证尚未运行",
                )
                review = recognition.add_human_review(
                    decision["id"], reviewer="test-reviewer", action="modify_term",
                    before_state={"term": "合同"}, after_state={"term": "劳动合同"},
                    reason="保留完整领域表达",
                )
                applied = recognition.apply_human_review(
                    decision["id"], reviewer="test-reviewer", action="confirm",
                    after_state={"term": "劳动合同", "topic": "录用与入职"},
                    reason="人工核对原文及三类证据后确认",
                )
                promoted = recognition.approve_candidates_for_approved_decisions(run["id"])

                self.assertEqual(recognition.get_run(run["id"])["corpus_id"], corpus["id"])
                self.assertEqual(len(recognition.list_term_candidates(run["id"])), 3)
                self.assertEqual(len(recognition.list_validator_results(validation_run["id"])), 1)
                self.assertEqual(aggregate["aggregated_decision"], "accept")
                self.assertEqual(cross_case["topic_candidate_id"], topic["id"])
                self.assertNotEqual(review["before_state"], review["after_state"])
                self.assertEqual(applied["decision"]["lifecycle_status"], "approved")
                self.assertEqual(promoted["term"], 3)
                self.assertIn(
                    batch_decision_id,
                    {row["id"] for row in recognition.list_resource_decisions(run["id"])},
                )
                bundle = recognition.get_run_bundle(run["id"])
                self.assertTrue(all(row["status"] == "approved" for row in bundle["term_candidates"]))
                self.assertEqual(bundle["topic_candidates"][0]["status"], "approved")
                self.assertEqual(len(bundle["human_reviews"]), 2)
                approved_bundle = recognition.get_approved_resource_bundle(run["id"])
                self.assertEqual(len(approved_bundle["term_candidates"]), 3)
                self.assertNotIn("validation_runs", approved_bundle)
                scored_term = next(row for row in approved_bundle["term_candidates"] if row["candidate_term_id"] == "term-1")
                self.assertAlmostEqual(float(scored_term["score"]), 0.75)
                self.assertEqual(
                    set(approved_bundle["term_candidates"][0]),
                    {"recognition_document_id", "candidate_term_id", "normalized_form", "score"},
                )
                self.assertEqual(recognition.update_run_status(run["id"], "succeeded")["status"], "succeeded")
            finally:
                with connect(self.settings) as connection, connection.transaction():
                    found = connection.execute(
                        "SELECT id FROM corpora WHERE corpus_key=%s", (corpus_key,)
                    ).fetchone()
                    if found:
                        connection.execute(
                            """DELETE FROM cross_validation_terms
                               WHERE cross_validation_case_id IN (
                                   SELECT c.id FROM cross_validation_cases c
                                   JOIN recognition_runs r ON r.id=c.recognition_run_id
                                   WHERE r.corpus_id=%s
                               )""",
                            (found["id"],),
                        )
                        connection.execute("DELETE FROM processing_jobs WHERE corpus_id=%s", (found["id"],))
                        connection.execute("DELETE FROM corpora WHERE id=%s", (found["id"],))

    def test_database_rejects_cross_run_document_reference(self) -> None:
        # The composite FK is the database-level guard that prevents mixed-run evidence.
        sql = (PROJECT_ROOT / "db/migrations/0002_recognition_validation.sql").read_text(encoding="utf-8")
        self.assertIn("FOREIGN KEY (recognition_run_id, recognition_document_id)", sql)
        self.assertIn("REFERENCES recognition_documents(recognition_run_id, id)", sql)


if __name__ == "__main__":
    unittest.main()
