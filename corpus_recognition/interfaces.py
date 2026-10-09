"""Pluggable interfaces; implementations arrive in later phases."""

from __future__ import annotations

from typing import Any, Mapping, Protocol, Sequence, runtime_checkable

from .models import (
    AggregationStrategy,
    CrossValidationRequest,
    Document,
    TermCandidate,
    TermValidationRequest,
    TopicCandidate,
    TopicValidationRequest,
    ValidatorDecision,
    ValidatorResult,
)


@runtime_checkable
class TermExtractor(Protocol):
    name: str
    version: str

    def extract(self, document: Document) -> list[TermCandidate]: ...


@runtime_checkable
class TopicIdentifier(Protocol):
    name: str
    version: str

    def identify(
        self,
        documents: Sequence[Document],
        topic_table: Sequence[Mapping[str, Any]] | None,
    ) -> list[TopicCandidate]: ...


@runtime_checkable
class ValidatorProvider(Protocol):
    name: str
    version: str
    model: str
    model_version: str

    def validate_term(self, request: TermValidationRequest) -> ValidatorResult: ...

    def validate_topic(self, request: TopicValidationRequest) -> ValidatorResult: ...

    def cross_validate(self, request: CrossValidationRequest) -> ValidatorResult: ...


@runtime_checkable
class ValidationAggregator(Protocol):
    def aggregate(
        self,
        results: Sequence[ValidatorResult],
        strategy: AggregationStrategy,
        *,
        min_validators: int,
        weights: Mapping[str, float] | None = None,
    ) -> tuple[ValidatorDecision, float | None, bool]: ...

