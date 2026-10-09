from __future__ import annotations

import json
import tempfile
import unittest
import uuid
from pathlib import Path

from corpus_recognition import (
    AggregationStrategy,
    Document,
    OpenAICompatibleValidatorProvider,
    RecognitionRepository,
    TermCandidate,
    TermValidationRequest,
    TermValidationService,
    ValidatorDecision,
    ValidatorResult,
    load_prompt,
)
from corpus_recognition.config import ValidatorModelConfig
from corpus_registry import CorpusRegistry, DatabaseConfigurationError, DatabaseSettings
from corpus_registry.db import connect
from corpus_registry.migrations import initialize_database


PROJECT_ROOT = Path(__file__).resolve().parents[1]
PROMPT_PATH = PROJECT_ROOT / "configs/prompts/recognition/term_validation/v1.json"


def make_request() -> TermValidationRequest:
    document = Document(
        document_id="doc-1",
        source="unit-test",
        raw_text="劳动合同应当明确试用期。",
        clean_text="劳动合同应当明确试用期。",
        processing_version="test/1",
    )
    candidate = TermCandidate(
        term_id="term-1",
        document_id=document.document_id,
        surface_form="劳动合同",
        normalized_form="劳动合同",
        start_offset=0,
        end_offset=4,
        sentence=document.clean_text,
        extractor="test-extractor",
        extractor_version="1",
        evidence={"matched_text": "劳动合同"},
    )
    return TermValidationRequest(document=document, candidate=candidate)


class FakeProvider:
    version = "fake-provider/1"

    def __init__(self, name: str, decision: ValidatorDecision | None = None, error: Exception | None = None):
        self.name = name
        self.model = f"{name}-model"
        self.model_version = "2026-09"
        self.decision = decision
        self.error = error
        self.prompt = load_prompt(PROMPT_PATH, project_root=PROJECT_ROOT)

    def validate_term(self, request: TermValidationRequest) -> ValidatorResult:
        if self.error:
            raise self.error
        template = self.prompt.template
        return ValidatorResult(
            validator=self.name,
            validator_version=self.version,
            model=self.model,
            model_version=self.model_version,
            prompt_id=template.prompt_id,
            prompt_version=template.prompt_version,
            decision=self.decision,
            suggested_term=request.candidate.normalized_form,
            domain_relevance=0.8,
            linguistic_integrity=0.9,
            reason=f"{self.name} 独立判断为 {self.decision.value}",
            evidence={"sentence": request.candidate.sentence, "rule": "fake-test"},
        )


class FakeTransport:
    def __init__(self, content: str):
        self.content = content
        self.calls: list[dict] = []

    def complete(self, **kwargs) -> str:
        self.calls.append(kwargs)
        return self.content


class TermValidationUnitTests(unittest.TestCase):
    def setUp(self) -> None:
        self.prompt = load_prompt(PROMPT_PATH, project_root=PROJECT_ROOT)
        self.request = make_request()

    def service(self, providers, strategy=AggregationStrategy.MAJORITY, min_validators=1, weights=None):
        return TermValidationService(
            providers,
            prompt=self.prompt,
            strategy=strategy,
            min_validators=min_validators,
            weights=weights,
        )

    def test_single_validator_runs_without_knowledge_tables(self) -> None:
        result = self.service([FakeProvider("one", ValidatorDecision.ACCEPT)]).validate(self.request)
        self.assertEqual(result.aggregated_decision, ValidatorDecision.ACCEPT)
        self.assertEqual(result.validator_count, 1)
        self.assertEqual(result.validation_details[0].evidence["rule"], "fake-test")

    def test_three_validator_majority_and_conflict_are_traceable(self) -> None:
        providers = [
            FakeProvider("a", ValidatorDecision.ACCEPT),
            FakeProvider("b", ValidatorDecision.ACCEPT),
            FakeProvider("c", ValidatorDecision.REJECT),
        ]
        result = self.service(providers, min_validators=3).validate(self.request)
        self.assertEqual(result.aggregated_decision, ValidatorDecision.ACCEPT)
        self.assertAlmostEqual(result.agreement_score, 2 / 3)
        self.assertTrue(result.conflict)
        self.assertEqual(len(result.validation_details), 3)

    def test_weighted_strategy_uses_configured_weights_not_model_confidence(self) -> None:
        providers = [
            FakeProvider("a", ValidatorDecision.ACCEPT),
            FakeProvider("b", ValidatorDecision.ACCEPT),
            FakeProvider("c", ValidatorDecision.REJECT),
        ]
        result = self.service(
            providers,
            AggregationStrategy.WEIGHTED,
            min_validators=3,
            weights={"a": 1, "b": 1, "c": 3},
        ).validate(self.request)
        self.assertEqual(result.aggregated_decision, ValidatorDecision.REJECT)
        self.assertAlmostEqual(result.agreement_score, 3 / 5)

    def test_unanimous_conflict_enters_review(self) -> None:
        providers = [
            FakeProvider("a", ValidatorDecision.ACCEPT),
            FakeProvider("b", ValidatorDecision.REJECT),
        ]
        result = self.service(
            providers, AggregationStrategy.UNANIMOUS, min_validators=2
        ).validate(self.request)
        self.assertEqual(result.aggregated_decision, ValidatorDecision.REVIEW)
        self.assertTrue(result.conflict)

    def test_unavailable_validator_is_preserved_and_causes_explicit_degradation(self) -> None:
        providers = [
            FakeProvider("ok", ValidatorDecision.ACCEPT),
            FakeProvider("offline", error=RuntimeError("service unavailable")),
        ]
        result = self.service(providers, min_validators=2).validate(self.request)
        self.assertEqual(result.aggregated_decision, ValidatorDecision.UNCERTAIN)
        failed = result.validation_details[1]
        self.assertEqual(failed.status, "unavailable")
        self.assertEqual(failed.error_code, "RuntimeError")
        self.assertIn("service unavailable", failed.reason)

    def test_provider_count_can_change_without_changing_orchestrator(self) -> None:
        one = self.service([FakeProvider("a", ValidatorDecision.ACCEPT)]).validate(self.request)
        three = self.service([
            FakeProvider("a", ValidatorDecision.ACCEPT),
            FakeProvider("b", ValidatorDecision.ACCEPT),
            FakeProvider("c", ValidatorDecision.ACCEPT),
        ], min_validators=3).validate(self.request)
        self.assertEqual(one.aggregated_decision, three.aggregated_decision)
        self.assertEqual((one.validator_count, three.validator_count), (1, 3))

    def test_openai_compatible_adapter_uses_versioned_prompt_and_hides_secret(self) -> None:
        content = "```json\n" + json.dumps({
            "decision": "accept",
            "suggested_term": "劳动合同",
            "domain_relevance": 0.8,
            "linguistic_integrity": 0.9,
            "reason": "具有稳定的劳动法领域意义",
            "evidence": {"sentence": "劳动合同应当明确试用期。"},
        }, ensure_ascii=False) + "\n```"
        transport = FakeTransport(content)
        config = ValidatorModelConfig(
            id="configured-model",
            provider="openai_compatible",
            model_env="MODEL_NAME",
            model_version_env="MODEL_VERSION",
            base_url_env="MODEL_URL",
            api_key_env="MODEL_KEY",
        )
        provider = OpenAICompatibleValidatorProvider.from_config(
            config,
            self.prompt,
            environment={
                "MODEL_NAME": "test-model",
                "MODEL_VERSION": "2026-09",
                "MODEL_URL": "http://model.invalid/v1",
                "MODEL_KEY": "never-print-this",
            },
            transport=transport,
        )
        result = provider.validate_term(self.request)
        self.assertEqual(result.prompt_version, "1.0.0")
        self.assertEqual(result.validator, "configured-model")
        self.assertIn("术语", transport.calls[0]["messages"][0]["content"])
        self.assertNotIn("never-print-this", repr(provider.settings))

    def test_adapter_accepts_prose_wrapped_json_and_normalizes_common_fields(self) -> None:
        content = "验证结果如下：\n" + json.dumps({
            "decision": "ACCEPT",
            "suggested_term": "劳动合同",
            "reason": ["具有稳定领域意义"],
            "evidence": "原句中完整出现",
        }, ensure_ascii=False)
        transport = FakeTransport(content)
        config = ValidatorModelConfig(
            id="compatible-model", provider="openai_compatible",
            model_env="MODEL_NAME", base_url_env="MODEL_URL", api_key_env="MODEL_KEY",
        )
        provider = OpenAICompatibleValidatorProvider.from_config(
            config, self.prompt,
            environment={
                "MODEL_NAME": "test-model", "MODEL_URL": "http://model.invalid/v1",
                "MODEL_KEY": "secret",
            },
            transport=transport,
        )
        result = provider.validate_term(self.request)
        self.assertEqual(result.decision, ValidatorDecision.ACCEPT)
        self.assertIn("具有稳定领域意义", result.reason)
        self.assertEqual(result.evidence["model_evidence"], "原句中完整出现")


def postgresql_is_configured() -> bool:
    try:
        DatabaseSettings.from_environment()
    except DatabaseConfigurationError:
        return False
    return True


@unittest.skipUnless(
    postgresql_is_configured(),
    "set DB_NAME, DB_USER and DB_PASSWORD to run PostgreSQL integration tests",
)
class TermValidationPostgreSQLTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.settings = DatabaseSettings.from_environment()
        initialize_database(cls.settings, PROJECT_ROOT / "db/migrations")

    def setUp(self) -> None:
        self.corpus_key = f"term-validation-{uuid.uuid4().hex[:10]}"
        self.tempdir = tempfile.TemporaryDirectory()
        registry = CorpusRegistry(self.settings, data_root=Path(self.tempdir.name))
        self.recognition = RecognitionRepository(self.settings)
        corpus = registry.create_corpus(self.corpus_key, "术语验证测试")
        job = registry.create_processing_job(
            self.corpus_key,
            Path(self.tempdir.name) / "workspaces" / "run-1",
            "recognition",
            parameters={"test": True},
        )
        self.run = self.recognition.create_run(
            job["id"], pipeline_version="recognition-validation/1.0",
            config_snapshot={"strategy": "majority"},
            prompt_snapshot={"term": {"version": "1.0.0"}},
        )
        self.document = self.recognition.add_document(
            self.run["id"], document_id="doc-1", source="unit-test",
            raw_asset_path="workspaces/run-1/recognition/01_preprocessed/raw.txt",
            clean_asset_path="workspaces/run-1/recognition/01_preprocessed/clean.txt",
            processing_version="1.0",
        )
        self.term = self.recognition.add_term_candidate(
            self.run["id"], self.document["id"], candidate_term_id="term-1",
            surface_form="劳动合同", normalized_form="劳动合同",
            start_offset=0, end_offset=4, sentence="劳动合同应当明确试用期。",
            extractor="test", extractor_version="1",
        )
        self.corpus_id = corpus["id"]

    def tearDown(self) -> None:
        with connect(self.settings) as connection, connection.transaction():
            connection.execute("DELETE FROM processing_jobs WHERE corpus_id=%s", (self.corpus_id,))
            connection.execute("DELETE FROM corpora WHERE id=%s", (self.corpus_id,))
        self.tempdir.cleanup()

    def validation_run(self, validators) -> dict:
        return self.recognition.create_validation_run(
            self.run["id"], validation_kind="term", aggregation_strategy="majority",
            min_validators=1, validator_config=[{"id": name} for name in validators],
        )

    def test_independent_results_and_aggregate_are_committed_together(self) -> None:
        validation_run = self.validation_run(["a", "b", "offline"])
        prompt = load_prompt(PROMPT_PATH, project_root=PROJECT_ROOT)
        service = TermValidationService(
            [
                FakeProvider("a", ValidatorDecision.ACCEPT),
                FakeProvider("b", ValidatorDecision.ACCEPT),
                FakeProvider("offline", error=RuntimeError("offline")),
            ],
            prompt=prompt,
            min_validators=2,
            repository=self.recognition,
        )
        stored = service.validate_and_persist(
            make_request(), validation_run_id=validation_run["id"],
            term_candidate_row_id=self.term["id"],
        )
        rows = self.recognition.list_validator_results(validation_run["id"])
        aggregate = self.recognition.get_validation_aggregate(
            validation_run["id"], term_candidate_id=self.term["id"]
        )
        self.assertEqual(len(rows), 3)
        self.assertEqual(len(stored.validator_result_ids), 3)
        self.assertEqual(aggregate["aggregated_decision"], "accept")
        self.assertEqual(aggregate["validator_count"], 3)
        self.assertEqual({row["prompt_version"] for row in rows}, {"1.0.0"})
        self.assertEqual({row["status"] for row in rows}, {"succeeded", "unavailable"})
        with connect(self.settings) as connection:
            status = connection.execute(
                "SELECT status FROM validation_runs WHERE id=%s", (validation_run["id"],)
            ).fetchone()["status"]
        self.assertEqual(status, "degraded")

    def test_invalid_second_result_rolls_back_the_whole_transaction(self) -> None:
        validation_run = self.validation_run(["duplicate"])
        result = {
            "validator": "duplicate", "validator_version": "1", "model": "fake",
            "model_version": "1", "prompt_id": "recognition.term_validation",
            "prompt_version": "1.0.0", "decision": "accept",
            "structured_result": {}, "reason": "test", "evidence": {},
            "status": "succeeded", "error_code": "",
        }
        aggregate = {
            "validator_count": 2, "accept_count": 2, "reject_count": 0,
            "review_count": 0, "uncertain_count": 0, "agreement_score": 1.0,
            "aggregation_strategy": "majority", "aggregated_decision": "accept",
            "conflict": False, "details": {},
        }
        with self.assertRaises(Exception):
            self.recognition.persist_term_validation(
                validation_run["id"], self.term["id"],
                results=[result, result], aggregate=aggregate, durations_ms=[1, 1],
            )
        self.assertEqual(self.recognition.list_validator_results(validation_run["id"]), [])
        with connect(self.settings) as connection:
            aggregate_count = connection.execute(
                "SELECT count(*) AS n FROM validation_aggregates WHERE validation_run_id=%s",
                (validation_run["id"],),
            ).fetchone()["n"]
        self.assertEqual(aggregate_count, 0)


if __name__ == "__main__":
    unittest.main()
