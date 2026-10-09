from __future__ import annotations

import unittest
import uuid
import threading
import time
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from corpus_recognition import (
    AggregationStrategy,
    BuiltinRuleValidatorProvider,
    ConfigurableValidationAggregator,
    CrossValidationRequest,
    CrossValidationService,
    DecisionEngine,
    Document,
    EvaluatedBaselineTermProvider,
    FinalDecision,
    RecognitionRunOptions,
    ResourceDecision,
    ResolvedValidatorSettings,
    SentenceTermIntersectionProvider,
    TermCandidate,
    TermValidationRequest,
    TermValidationResult,
    TopicCandidate,
    TopicResolution,
    TopicValidationRequest,
    TopicValidationService,
    ValidatorDecision,
    ValidatorResult,
    evaluate_cross_validation,
    evaluate_term_extraction,
    evaluate_topic_identification,
    load_prompt,
)
from corpus_recognition.runtime import RecognitionCoordinator, RuntimeValidatorModel
from corpus_recognition.topic import ContentUnitTopicIdentifier
from corpus_api.recognition import (
    RecognitionJobState,
    RecognitionWorkflowCancelled,
    _next_prompt_version,
)


ROOT = Path(__file__).resolve().parents[1]


def result(name: str, decision: ValidatorDecision) -> ValidatorResult:
    return ValidatorResult(
        validator=name, validator_version="1", model=name, model_version="1",
        prompt_id="test", prompt_version="1", decision=decision,
        reason=f"{name} decision", evidence={"trace": name},
    )


class RecognitionWorkflowUnitTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        prompt_root = ROOT / "configs/prompts/recognition"
        cls.term_prompt = load_prompt(prompt_root / "term_validation/v1.json")
        cls.topic_prompt = load_prompt(prompt_root / "topic_validation/v1.json")
        cls.cross_prompt = load_prompt(prompt_root / "term_topic_cross_validation/v1.json")
        cls.term_sentence_prompt = load_prompt(prompt_root / "term_sentence_extraction/v1.json")
        cls.provider = BuiltinRuleValidatorProvider(
            cls.term_prompt, cls.topic_prompt, cls.cross_prompt
        )
        cls.document = Document(
            document_id="doc-1", source="test", raw_text="劳动合同规定试用期",
            clean_text="劳动合同规定试用期", processing_version="test/1",
        )
        cls.term = TermCandidate(
            term_id="term-1", document_id="doc-1", surface_form="劳动合同",
            normalized_form="劳动合同", sentence="劳动合同规定试用期",
            extractor="test", extractor_version="1",
        )
        cls.topic = TopicCandidate(
            topic_candidate_id="topic-1", document_id="doc-1", topic_name="劳动合同",
            topic_definition="劳动合同订立与履行", keywords=["劳动合同", "试用期"],
            source_method="classification", resolution=TopicResolution.EXISTING,
            evidence={"mapped_topic_id": "HR-CONTRACT"},
        )

    def test_topic_validation_is_independent_and_traceable(self) -> None:
        service = TopicValidationService(
            [self.provider], prompt=self.topic_prompt,
            strategy=AggregationStrategy.MAJORITY,
        )
        value = service.validate(TopicValidationRequest(
            document=self.document, candidate=self.topic, topic_table=[]
        ))
        self.assertEqual(value.aggregated_decision, ValidatorDecision.ACCEPT)
        self.assertEqual(value.mapped_topic_id, "HR-CONTRACT")
        self.assertTrue(value.validation_details[0].reason)

    def test_three_validator_strategies_and_conflict(self) -> None:
        rows = [result("a", ValidatorDecision.ACCEPT), result("b", ValidatorDecision.ACCEPT),
                result("c", ValidatorDecision.REJECT)]
        aggregator = ConfigurableValidationAggregator()
        majority = aggregator.aggregate(rows, AggregationStrategy.MAJORITY, min_validators=1)
        unanimous = aggregator.aggregate(rows, AggregationStrategy.UNANIMOUS, min_validators=1)
        weighted = aggregator.aggregate(
            rows, AggregationStrategy.WEIGHTED, min_validators=1,
            weights={"a": 1, "b": 1, "c": 3},
        )
        self.assertEqual(majority[0], ValidatorDecision.ACCEPT)
        self.assertEqual(unanimous[0], ValidatorDecision.REVIEW)
        self.assertEqual(weighted[0], ValidatorDecision.REJECT)
        self.assertTrue(majority[2])

    def test_cross_validation_checks_both_directions(self) -> None:
        value = CrossValidationService(
            [self.provider], prompt=self.cross_prompt
        ).validate(CrossValidationRequest(
            document=self.document, validated_terms=[self.term],
            validated_topic=self.topic, topic_definition=self.topic.topic_definition,
        ))
        self.assertTrue(value.term_supports_topic)
        self.assertTrue(value.topic_supports_terms)
        self.assertEqual(value.aggregated_decision, ValidatorDecision.ACCEPT)
        self.assertTrue(value.reason)

    def test_decision_requires_all_three_accepts(self) -> None:
        term_value = TermValidationResult(
            candidate_term_id="term-1", validator_count=1, accept_count=1,
            reject_count=0, review_count=0, agreement_score=1,
            aggregation_strategy=AggregationStrategy.MAJORITY,
            aggregated_decision=ValidatorDecision.ACCEPT, conflict=False,
        )
        topic_value = TopicValidationService(
            [self.provider], prompt=self.topic_prompt
        ).validate(TopicValidationRequest(document=self.document, candidate=self.topic))
        cross_value = CrossValidationService(
            [self.provider], prompt=self.cross_prompt
        ).validate(CrossValidationRequest(
            document=self.document, validated_terms=[self.term], validated_topic=self.topic
        ))
        accepted = DecisionEngine().decide("doc-1", term_value, topic_value, cross_value)
        self.assertEqual(accepted.final_decision, FinalDecision.AUTO_ACCEPT)
        review_cross = cross_value.model_copy(update={"aggregated_decision": ValidatorDecision.REVIEW})
        reviewed = DecisionEngine().decide("doc-1", term_value, topic_value, review_cross)
        self.assertEqual(reviewed.final_decision, FinalDecision.MANUAL_REVIEW)

    def test_gold_evaluation_is_separate_from_runtime_agreement(self) -> None:
        self.assertEqual(evaluate_term_extraction({"a", "b"}, {"b", "c"})["f1"], 0.5)
        self.assertEqual(evaluate_topic_identification(["a", "b"], ["a", "a"])["accuracy"], 0.5)
        self.assertEqual(evaluate_cross_validation([True, False], [True, False])["consistency_accuracy"], 1)

    def test_validation_mode_requires_machine_or_human(self) -> None:
        with self.assertRaises(ValueError):
            RecognitionRunOptions(machine_validation=False, human_validation=False)

    def test_multiple_runtime_models_and_duplicate_ids(self) -> None:
        options = RecognitionRunOptions(
            use_external_model=True,
            include_builtin_rules=False,
            validator_models=(
                RuntimeValidatorModel("model_a", "alpha", "https://a.example/v1", "secret-a"),
                RuntimeValidatorModel("model_b", "beta", "https://b.example/v1", "secret-b", weight=2),
            ),
        )
        self.assertEqual(len(options.validator_models), 2)
        self.assertFalse(options.include_builtin_rules)
        with self.assertRaises(ValueError):
            RecognitionRunOptions(
                use_external_model=True,
                validator_models=(
                    RuntimeValidatorModel("same", "a", "https://a.example", "a"),
                    RuntimeValidatorModel("same", "b", "https://b.example", "b"),
                ),
            )

    def test_prompt_version_increments_for_saved_workbench_edits(self) -> None:
        self.assertEqual(_next_prompt_version("1.0.0"), "1.0.1")
        self.assertEqual(_next_prompt_version("2.3"), "2.3.1")

    def test_human_mode_forces_traceable_manual_review(self) -> None:
        automatic = ResourceDecision(
            document_id="doc-1",
            term_validation_status=ValidatorDecision.ACCEPT,
            topic_validation_status=ValidatorDecision.ACCEPT,
            cross_validation_status=ValidatorDecision.ACCEPT,
            final_decision=FinalDecision.AUTO_ACCEPT,
            decision_rule="all_accept",
            reason="automatic",
            lifecycle_status="approved",
        )
        result = RecognitionCoordinator._apply_review_policy(
            automatic, RecognitionRunOptions(human_validation=True)
        )
        self.assertEqual(result.final_decision, FinalDecision.MANUAL_REVIEW)
        self.assertEqual(result.lifecycle_status.value, "validated")

    def test_configured_but_empty_topic_table_does_not_silently_discover(self) -> None:
        identifier = ContentUnitTopicIdentifier(type("Extractor", (), {"extract": lambda *_: []})())
        with self.assertRaisesRegex(ValueError, "没有解析出有效节点"):
            identifier.identify([self.document], {"nodes": []})

    def test_human_gate_waits_before_cross_validation(self) -> None:
        coordinator = RecognitionCoordinator.__new__(RecognitionCoordinator)
        repository = type("Repository", (), {"get_run_bundle": lambda *_: {}})()
        coordinator.repository = repository
        events: list[tuple[str, int, str]] = []
        waiting = {"ready": False}
        ready = {"ready": True, "consensus": {"topic": {}, "term": {}}}
        checkpoint = unittest.mock.Mock()
        with patch("corpus_recognition.runtime.annotation_status", side_effect=[waiting, ready]), patch(
            "corpus_recognition.runtime.time.sleep"
        ) as sleep:
            result = coordinator._wait_for_annotation_consensus(
                uuid.UUID("dddddddd-dddd-4ddd-8ddd-dddddddddddd"),
                lambda stage, percent, detail: events.append((stage, percent, detail)),
                checkpoint,
            )
        self.assertIs(result, ready)
        self.assertEqual(events[0][0], "human_review")
        sleep.assert_called_once_with(1.0)
        self.assertEqual(checkpoint.call_count, 2)

    def test_job_control_cancels_at_a_safe_checkpoint(self) -> None:
        state = RecognitionJobState()
        state.running = True
        state.cancel_requested = True
        with self.assertRaisesRegex(RecognitionWorkflowCancelled, "用户结束"):
            state.checkpoint()

    def test_job_control_pauses_and_resumes_at_a_safe_checkpoint(self) -> None:
        state = RecognitionJobState()
        state.running = True
        state.status = "running"
        state.active_stage = "term"
        state.active_detail = "正在检验术语"
        state.pause_requested = True
        finished = threading.Event()
        worker = threading.Thread(target=lambda: (state.checkpoint(), finished.set()))
        worker.start()
        for _ in range(50):
            if state.snapshot()["paused"]:
                break
            time.sleep(0.01)
        self.assertTrue(state.snapshot()["paused"])
        self.assertFalse(finished.is_set())
        with state.condition:
            state.pause_requested = False
            state.condition.notify_all()
        self.assertTrue(finished.wait(1))
        worker.join(1)
        self.assertEqual(state.snapshot()["status"], "running")
        self.assertEqual(state.snapshot()["stage"], "term")

    def test_machine_only_activation_keeps_all_sentence_labels_and_pending_provenance(self) -> None:
        run_id = uuid.UUID("eeeeeeee-eeee-4eee-8eee-eeeeeeeeeeee")
        bundle = {
            "run": {
                "workspace_path": "workspaces/chinese-plus/test-run",
                "config_snapshot": {"human_validation": False},
            },
            "resource_decisions": [
                {"lifecycle_status": "approved"},
                {"lifecycle_status": "candidate"},
            ],
        }
        with TemporaryDirectory() as temporary:
            data_root = Path(temporary)
            source = data_root / "workspaces/chinese-plus/test-run/recognition/08_approved_resources"
            source.mkdir(parents=True)
            (source / "terminology.csv").write_text("term_id,term\n", encoding="utf-8")
            (source / "sentence_labels.jsonl").write_text(
                '{"document_id":"approved"}\n{"document_id":"pending"}\n',
                encoding="utf-8",
            )
            coordinator = RecognitionCoordinator.__new__(RecognitionCoordinator)
            coordinator.repository = type(
                "Repository", (), {
                    "get_run": lambda *_: bundle["run"],
                    "get_run_bundle": lambda *_: bundle,
                    "list_resource_decisions": lambda *_: bundle["resource_decisions"],
                }
            )()
            coordinator.registry = type("Registry", (), {"data_root": data_root})()
            coordinator.refresh_approved = lambda *_: [{"document_id": "approved"}]
            with patch(
                "corpus_recognition.runtime.annotation_status",
                return_value={"human_validation": False, "ready": True, "scopes": {}},
            ):
                result = coordinator.activate_approved(run_id)
        self.assertEqual(result["approved_resources"], 1)
        self.assertEqual(result["sentence_labels"], 2)
        self.assertEqual(result["pending_resources"], 1)
        self.assertEqual(result["activation_policy"], "full_sentence_labels_pending_decisions_retained")

    def test_term_baseline_is_direct_when_external_model_is_disabled(self) -> None:
        provider = EvaluatedBaselineTermProvider(self.term_sentence_prompt)
        checked = provider.validate_term(
            TermValidationRequest(document=self.document, candidate=self.term)
        )
        self.assertEqual(checked.decision, ValidatorDecision.ACCEPT)
        self.assertEqual(checked.evidence["policy"], "evaluated_baseline_direct_output")

    def test_sentence_model_terms_are_intersected_with_evaluated_baseline(self) -> None:
        class Transport:
            calls = 0

            def complete(self, **_: object) -> str:
                self.calls += 1
                return '{"terms":[{"term":"劳动合同","evidence":"领域制度名称"}],"reason":"逐句识别"}'

        transport = Transport()
        provider = SentenceTermIntersectionProvider(
            ResolvedValidatorSettings(
                validator_id="model-a", provider="openai_compatible",
                model="model-a", model_version="1", base_url="http://local/v1",
                api_key="test",
            ),
            self.term_sentence_prompt,
            [self.document],
            transport=transport,
        )
        accepted = provider.validate_term(
            TermValidationRequest(document=self.document, candidate=self.term)
        )
        rejected = provider.validate_term(
            TermValidationRequest(
                document=self.document,
                candidate=self.term.model_copy(update={
                    "term_id": "term-2", "surface_form": "试用期", "normalized_form": "试用期"
                }),
            )
        )
        self.assertEqual(transport.calls, 1)
        self.assertEqual(accepted.decision, ValidatorDecision.ACCEPT)
        self.assertEqual(rejected.decision, ValidatorDecision.REJECT)
        self.assertEqual(
            accepted.evidence["policy"],
            "model_sentence_terms_intersect_evaluated_baseline",
        )

    def test_workbench_and_api_are_wired(self) -> None:
        html = (ROOT / "corpus_api/web/index.html").read_text(encoding="utf-8")
        app = (ROOT / "corpus_api/app.py").read_text(encoding="utf-8")
        self.assertIn("开始完整加工流程", html)
        self.assertIn('data-test="${s.api}"', html)
        self.assertIn("machineValidation", html)
        self.assertIn("machineEngine", html)
        self.assertIn("modelList", html)
        self.assertIn("fileRows", html)
        self.assertIn("最终版话题体系表", html)
        self.assertIn('id="pauseRunButton"', html)
        self.assertIn('id="resumeRunButton"', html)
        self.assertIn('id="newRunButton"', html)
        self.assertIn('@router.post("/control/pause"', (ROOT / "corpus_api/recognition.py").read_text(encoding="utf-8"))
        self.assertIn("recognition_router", app)


if __name__ == "__main__":
    unittest.main()
