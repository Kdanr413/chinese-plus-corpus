"""Independent term validation orchestration and optional persistence."""

from __future__ import annotations

import time
import uuid
from dataclasses import dataclass
from typing import Mapping, Sequence

from .aggregators import ConfigurableValidationAggregator
from .models import (
    AggregationStrategy,
    TermValidationRequest,
    TermValidationResult,
    TopicValidationRequest,
    TopicValidationResult,
    CrossValidationRequest,
    CrossValidationResult,
    ValidatorDecision,
    ValidatorResult,
)
from .prompts import PromptSnapshot
from .repository import RecognitionRepository


@dataclass(frozen=True)
class PersistedTermValidation:
    result: TermValidationResult
    validator_result_ids: tuple[uuid.UUID, ...]
    aggregate_id: uuid.UUID


class TermValidationService:
    """Call each validator independently, preserve failures, then aggregate."""

    def __init__(
        self,
        providers: Sequence[object],
        *,
        strategy: AggregationStrategy = AggregationStrategy.MAJORITY,
        min_validators: int = 1,
        weights: Mapping[str, float] | None = None,
        prompt: PromptSnapshot,
        aggregator: ConfigurableValidationAggregator | None = None,
        repository: RecognitionRepository | None = None,
    ) -> None:
        if not providers:
            raise ValueError("at least one validator provider is required")
        if min_validators < 1:
            raise ValueError("min_validators must be at least 1")
        self.providers = list(providers)
        names = [str(getattr(provider, "name", type(provider).__name__)) for provider in self.providers]
        if len(names) != len(set(names)):
            raise ValueError("validator provider names must be unique")
        self.strategy = strategy
        self.min_validators = min_validators
        self.weights = dict(weights or {})
        self.prompt = prompt
        self.aggregator = aggregator or ConfigurableValidationAggregator()
        self.repository = repository

    def _unavailable(self, provider: object, exc: Exception) -> ValidatorResult:
        template = self.prompt.template
        return ValidatorResult(
            validator=str(getattr(provider, "name", type(provider).__name__)),
            validator_version=str(getattr(provider, "version", "unknown")),
            model=str(getattr(provider, "model", "unavailable")),
            model_version=str(getattr(provider, "model_version", "unavailable")),
            prompt_id=template.prompt_id,
            prompt_version=template.prompt_version,
            decision=ValidatorDecision.UNCERTAIN,
            reason=f"验证器不可用：{exc}",
            evidence={"error_type": type(exc).__name__, "error": str(exc)},
            status="unavailable",
            error_code=type(exc).__name__,
        )

    def _run(self, request: TermValidationRequest) -> tuple[list[ValidatorResult], list[int]]:
        if request.document.document_id != request.candidate.document_id:
            raise ValueError("candidate document_id does not match the validation document")
        results: list[ValidatorResult] = []
        durations: list[int] = []
        expected_prompt = self.prompt.template
        for provider in self.providers:
            started = time.perf_counter()
            try:
                result = provider.validate_term(request)
                if (
                    result.prompt_id != expected_prompt.prompt_id
                    or result.prompt_version != expected_prompt.prompt_version
                ):
                    raise ValueError("validator returned a different prompt id/version")
            except Exception as exc:
                result = self._unavailable(provider, exc)
            durations.append(max(0, round((time.perf_counter() - started) * 1000)))
            results.append(result)
        return results, durations

    def validate(self, request: TermValidationRequest) -> TermValidationResult:
        results, _ = self._run(request)
        return self.aggregator.aggregate_term(
            request.candidate.term_id,
            results,
            self.strategy,
            min_validators=self.min_validators,
            weights=self.weights,
        )

    def validate_and_persist(
        self,
        request: TermValidationRequest,
        *,
        validation_run_id: uuid.UUID,
        term_candidate_row_id: uuid.UUID,
    ) -> PersistedTermValidation:
        if self.repository is None:
            raise ValueError("repository is required for validate_and_persist")
        results, durations = self._run(request)
        aggregate = self.aggregator.aggregate_term(
            request.candidate.term_id,
            results,
            self.strategy,
            min_validators=self.min_validators,
            weights=self.weights,
        )
        stored = self.repository.persist_term_validation(
            validation_run_id,
            term_candidate_row_id,
            results=[{
                "validator": result.validator,
                "validator_version": result.validator_version,
                "model": result.model,
                "model_version": result.model_version,
                "prompt_id": result.prompt_id,
                "prompt_version": result.prompt_version,
                "decision": result.decision.value,
                "structured_result": result.model_dump(mode="json"),
                "reason": result.reason,
                "evidence": result.evidence,
                "status": result.status,
                "error_code": result.error_code,
            } for result in results],
            aggregate={
                "validator_count": aggregate.validator_count,
                "accept_count": aggregate.accept_count,
                "reject_count": aggregate.reject_count,
                "review_count": aggregate.review_count,
                "uncertain_count": aggregate.uncertain_count,
                "agreement_score": aggregate.agreement_score,
                "aggregation_strategy": aggregate.aggregation_strategy.value,
                "aggregated_decision": aggregate.aggregated_decision.value,
                "conflict": aggregate.conflict,
                "details": {
                    "candidate_term_id": aggregate.candidate_term_id,
                    "agreement_is_not_accuracy": True,
                },
            },
            durations_ms=durations,
        )
        return PersistedTermValidation(
            aggregate, stored["validator_result_ids"], stored["aggregate_id"]
        )


class _IndependentValidationService:
    method_name = ""

    def __init__(
        self,
        providers: Sequence[object],
        *,
        strategy: AggregationStrategy = AggregationStrategy.MAJORITY,
        min_validators: int = 1,
        weights: Mapping[str, float] | None = None,
        prompt: PromptSnapshot,
        aggregator: ConfigurableValidationAggregator | None = None,
        repository: RecognitionRepository | None = None,
    ) -> None:
        if not providers:
            raise ValueError("at least one validator provider is required")
        self.providers = list(providers)
        self.strategy = strategy
        self.min_validators = min_validators
        self.weights = dict(weights or {})
        self.prompt = prompt
        self.aggregator = aggregator or ConfigurableValidationAggregator()
        self.repository = repository

    def _run(self, request: object) -> tuple[list[ValidatorResult], list[int]]:
        results: list[ValidatorResult] = []
        durations: list[int] = []
        template = self.prompt.template
        for provider in self.providers:
            started = time.perf_counter()
            try:
                result = getattr(provider, self.method_name)(request)
                if result.prompt_id != template.prompt_id or result.prompt_version != template.prompt_version:
                    raise ValueError("validator returned a different prompt id/version")
            except Exception as exc:
                result = ValidatorResult(
                    validator=str(getattr(provider, "name", type(provider).__name__)),
                    validator_version=str(getattr(provider, "version", "unknown")),
                    model=str(getattr(provider, "model", "unavailable")),
                    model_version=str(getattr(provider, "model_version", "unavailable")),
                    prompt_id=template.prompt_id,
                    prompt_version=template.prompt_version,
                    decision=ValidatorDecision.UNCERTAIN,
                    reason=f"验证器不可用：{exc}",
                    evidence={"error_type": type(exc).__name__, "error": str(exc)},
                    status="unavailable",
                    error_code=type(exc).__name__,
                )
            durations.append(max(0, round((time.perf_counter() - started) * 1000)))
            results.append(result)
        return results, durations

    @staticmethod
    def _stored_rows(results: Sequence[ValidatorResult]) -> list[dict[str, object]]:
        return [{
            "validator": row.validator,
            "validator_version": row.validator_version,
            "model": row.model,
            "model_version": row.model_version,
            "prompt_id": row.prompt_id,
            "prompt_version": row.prompt_version,
            "decision": row.decision.value,
            "structured_result": row.model_dump(mode="json"),
            "reason": row.reason,
            "evidence": row.evidence,
            "status": row.status,
            "error_code": row.error_code,
        } for row in results]

    @staticmethod
    def _aggregate_row(aggregate: object) -> dict[str, object]:
        return {
            "validator_count": aggregate.validator_count,
            "accept_count": aggregate.accept_count,
            "reject_count": aggregate.reject_count,
            "review_count": aggregate.review_count,
            "uncertain_count": aggregate.uncertain_count,
            "agreement_score": aggregate.agreement_score,
            "aggregation_strategy": aggregate.aggregation_strategy.value,
            "aggregated_decision": aggregate.aggregated_decision.value,
            "conflict": aggregate.conflict,
            "details": {"agreement_is_not_accuracy": True},
        }


class TopicValidationService(_IndependentValidationService):
    method_name = "validate_topic"

    def validate(self, request: TopicValidationRequest) -> TopicValidationResult:
        results, _ = self._run(request)
        return self.aggregator.aggregate_topic(
            request.candidate.topic_candidate_id, results, self.strategy,
            min_validators=self.min_validators, weights=self.weights,
        )

    def validate_and_persist(
        self, request: TopicValidationRequest, *, validation_run_id: uuid.UUID,
        topic_candidate_row_id: uuid.UUID,
    ) -> TopicValidationResult:
        if self.repository is None:
            raise ValueError("repository is required for validate_and_persist")
        results, durations = self._run(request)
        aggregate = self.aggregator.aggregate_topic(
            request.candidate.topic_candidate_id, results, self.strategy,
            min_validators=self.min_validators, weights=self.weights,
        )
        self.repository.persist_validation(
            validation_run_id, "topic", topic_candidate_row_id,
            results=self._stored_rows(results), aggregate=self._aggregate_row(aggregate),
            durations_ms=durations,
        )
        return aggregate


class CrossValidationService(_IndependentValidationService):
    method_name = "cross_validate"

    def validate(self, request: CrossValidationRequest) -> CrossValidationResult:
        results, _ = self._run(request)
        decision, agreement, _ = self.aggregator.aggregate(
            results, self.strategy, min_validators=self.min_validators, weights=self.weights
        )
        usable = [row for row in results if row.status in {"succeeded", "degraded"}]
        evidence_rows = [row.evidence for row in usable]
        return CrossValidationResult(
            document_id=request.document.document_id,
            term_ids=[term.term_id for term in request.validated_terms],
            topic_id=request.validated_topic.topic_candidate_id,
            term_supports_topic=_majority_bool(evidence_rows, "term_supports_topic"),
            topic_supports_terms=_majority_bool(evidence_rows, "topic_supports_terms"),
            semantic_consistency=_mean_metric(usable, "semantic_consistency"),
            conflicting_terms=_merged_strings(evidence_rows, "conflicting_terms"),
            missing_expected_terms=_merged_strings(evidence_rows, "missing_expected_terms"),
            possible_topic_revision=_first_string(evidence_rows, "possible_topic_revision"),
            possible_term_revision=_merged_mapping(evidence_rows, "possible_term_revision"),
            validator_results=list(results),
            agreement_score=agreement,
            aggregation_strategy=self.strategy,
            aggregated_decision=decision,
            reason="；".join(row.reason for row in results),
            evidence={"validator_count": len(results), "agreement_is_not_accuracy": True},
        )

    def validate_and_persist(
        self, request: CrossValidationRequest, *, validation_run_id: uuid.UUID,
        cross_validation_case_id: uuid.UUID,
    ) -> CrossValidationResult:
        if self.repository is None:
            raise ValueError("repository is required for validate_and_persist")
        results, durations = self._run(request)
        aggregate = self.validate_from_results(request, results)
        counts = {decision: 0 for decision in ValidatorDecision}
        for row in results:
            counts[row.decision] += 1
        self.repository.persist_validation(
            validation_run_id, "cross", cross_validation_case_id,
            results=self._stored_rows(results),
            aggregate={
                "validator_count": len(results),
                "accept_count": counts[ValidatorDecision.ACCEPT],
                "reject_count": counts[ValidatorDecision.REJECT],
                "review_count": counts[ValidatorDecision.REVIEW],
                "uncertain_count": counts[ValidatorDecision.UNCERTAIN],
                "agreement_score": aggregate.agreement_score,
                "aggregation_strategy": aggregate.aggregation_strategy.value,
                "aggregated_decision": aggregate.aggregated_decision.value,
                "conflict": len({row.decision for row in results if row.status == "succeeded"}) > 1,
                "details": aggregate.model_dump(mode="json"),
            },
            durations_ms=durations,
        )
        return aggregate

    def validate_from_results(
        self, request: CrossValidationRequest, results: Sequence[ValidatorResult]
    ) -> CrossValidationResult:
        decision, agreement, _ = self.aggregator.aggregate(
            results, self.strategy, min_validators=self.min_validators, weights=self.weights
        )
        usable = [row for row in results if row.status in {"succeeded", "degraded"}]
        evidence_rows = [row.evidence for row in usable]
        return CrossValidationResult(
            document_id=request.document.document_id,
            term_ids=[term.term_id for term in request.validated_terms],
            topic_id=request.validated_topic.topic_candidate_id,
            term_supports_topic=_majority_bool(evidence_rows, "term_supports_topic"),
            topic_supports_terms=_majority_bool(evidence_rows, "topic_supports_terms"),
            semantic_consistency=_mean_metric(usable, "semantic_consistency"),
            conflicting_terms=_merged_strings(evidence_rows, "conflicting_terms"),
            missing_expected_terms=_merged_strings(evidence_rows, "missing_expected_terms"),
            possible_topic_revision=_first_string(evidence_rows, "possible_topic_revision"),
            possible_term_revision=_merged_mapping(evidence_rows, "possible_term_revision"),
            validator_results=list(results), agreement_score=agreement,
            aggregation_strategy=self.strategy, aggregated_decision=decision,
            reason="；".join(row.reason for row in results),
            evidence={"validator_count": len(results), "agreement_is_not_accuracy": True},
        )


def _majority_bool(rows: Sequence[Mapping[str, object]], key: str) -> bool | None:
    values = [row.get(key) for row in rows if isinstance(row.get(key), bool)]
    if not values:
        return None
    return sum(bool(value) for value in values) * 2 >= len(values)


def _merged_strings(rows: Sequence[Mapping[str, object]], key: str) -> list[str]:
    return sorted({str(value) for row in rows for value in (row.get(key) or []) if str(value)})


def _first_string(rows: Sequence[Mapping[str, object]], key: str) -> str | None:
    return next((str(row[key]) for row in rows if row.get(key)), None)


def _merged_mapping(rows: Sequence[Mapping[str, object]], key: str) -> dict[str, str]:
    merged: dict[str, str] = {}
    for row in rows:
        value = row.get(key)
        if isinstance(value, Mapping):
            merged.update({str(k): str(v) for k, v in value.items()})
    return merged


def _mean_metric(rows: Sequence[ValidatorResult], name: str) -> float | None:
    values = [getattr(row, name) for row in rows if getattr(row, name) is not None]
    return sum(values) / len(values) if values else None
