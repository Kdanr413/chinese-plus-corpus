"""Stable data contracts shared by recognition pipeline components."""

from __future__ import annotations

from datetime import datetime, timezone
from enum import Enum
from typing import Any

from pydantic import AliasChoices, BaseModel, ConfigDict, Field, model_validator


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", populate_by_name=True)


class ValidatorDecision(str, Enum):
    ACCEPT = "accept"
    REVIEW = "review"
    REJECT = "reject"
    UNCERTAIN = "uncertain"


class AggregationStrategy(str, Enum):
    MAJORITY = "majority"
    WEIGHTED = "weighted"
    UNANIMOUS = "unanimous"


class FinalDecision(str, Enum):
    AUTO_ACCEPT = "auto_accept"
    MANUAL_REVIEW = "manual_review"
    REJECT = "reject"
    PENDING = "pending"


class CandidateStatus(str, Enum):
    CANDIDATE = "candidate"
    VALIDATED = "validated"
    REVIEWED = "reviewed"
    APPROVED = "approved"
    RELEASED = "released"
    REJECTED = "rejected"


class TopicResolution(str, Enum):
    EXISTING = "existing"
    UNKNOWN = "unknown"
    NEW_TOPIC = "new_topic"


class Document(StrictModel):
    document_id: str = Field(
        min_length=1,
        validation_alias=AliasChoices("document_id", "doc_id"),
    )
    source: str
    raw_text: str
    clean_text: str
    metadata: dict[str, Any] = Field(default_factory=dict)
    processing_version: str
    raw_checksum: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    clean_checksum: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")


class TermCandidate(StrictModel):
    term_id: str = Field(min_length=1)
    document_id: str = Field(min_length=1)
    surface_form: str = Field(min_length=1)
    normalized_form: str = Field(min_length=1)
    start_offset: int | None = Field(default=None, ge=0)
    end_offset: int | None = Field(default=None, ge=0)
    sentence: str = ""
    extractor: str = Field(min_length=1)
    extractor_version: str = Field(min_length=1)
    score: float | None = None
    evidence: dict[str, Any] = Field(default_factory=dict)
    status: CandidateStatus = CandidateStatus.CANDIDATE
    created_at: datetime = Field(default_factory=utc_now)

    @model_validator(mode="after")
    def validate_offsets(self) -> "TermCandidate":
        if (self.start_offset is None) != (self.end_offset is None):
            raise ValueError("start_offset and end_offset must both be set or both be null")
        if self.start_offset is not None and self.end_offset <= self.start_offset:
            raise ValueError("end_offset must be greater than start_offset")
        return self


class TopicCandidate(StrictModel):
    topic_candidate_id: str = Field(min_length=1)
    document_id: str | None = None
    cluster_id: str | None = None
    topic_name: str = Field(min_length=1)
    topic_definition: str = ""
    keywords: list[str] = Field(default_factory=list)
    representative_texts: list[str] = Field(default_factory=list)
    source_method: str = Field(min_length=1)
    parent_candidate: str | None = None
    resolution: TopicResolution = TopicResolution.NEW_TOPIC
    evidence: dict[str, Any] = Field(default_factory=dict)
    status: CandidateStatus = CandidateStatus.CANDIDATE
    created_at: datetime = Field(default_factory=utc_now)

    @model_validator(mode="after")
    def validate_subject(self) -> "TopicCandidate":
        if not self.document_id and not self.cluster_id:
            raise ValueError("document_id or cluster_id is required")
        return self


class ValidatorResult(StrictModel):
    validator: str = Field(min_length=1)
    validator_version: str = Field(min_length=1)
    model: str = Field(min_length=1)
    model_version: str = Field(min_length=1)
    prompt_id: str = Field(min_length=1)
    prompt_version: str = Field(min_length=1)
    decision: ValidatorDecision
    suggested_term: str | None = None
    suggested_topic: str | None = None
    existing_topic_id: str | None = None
    domain_relevance: float | None = None
    linguistic_integrity: float | None = None
    topic_fit: float | None = None
    granularity_assessment: str | None = None
    semantic_consistency: float | None = None
    reason: str = Field(min_length=1)
    evidence: dict[str, Any] = Field(default_factory=dict)
    status: str = Field(default="succeeded", pattern=r"^(succeeded|failed|unavailable|degraded)$")
    error_code: str = ""
    timestamp: datetime = Field(default_factory=utc_now)


class ValidationSummary(StrictModel):
    validator_count: int = Field(ge=0)
    accept_count: int = Field(ge=0)
    reject_count: int = Field(ge=0)
    review_count: int = Field(ge=0)
    uncertain_count: int = Field(default=0, ge=0)
    agreement_score: float | None = Field(default=None, ge=0, le=1)
    aggregation_strategy: AggregationStrategy
    aggregated_decision: ValidatorDecision
    conflict: bool
    validation_details: list[ValidatorResult] = Field(default_factory=list)

    @model_validator(mode="after")
    def validate_counts(self) -> "ValidationSummary":
        counted = self.accept_count + self.reject_count + self.review_count + self.uncertain_count
        if counted > self.validator_count:
            raise ValueError("decision counts cannot exceed validator_count")
        return self


class TermValidationResult(ValidationSummary):
    candidate_term_id: str = Field(min_length=1)


class TopicValidationResult(ValidationSummary):
    candidate_topic_id: str = Field(min_length=1)
    mapped_topic_id: str | None = None
    new_topic: bool = False


class CrossValidationResult(StrictModel):
    document_id: str = Field(min_length=1)
    term_ids: list[str]
    topic_id: str | None = None
    term_supports_topic: bool | None = None
    topic_supports_terms: bool | None = None
    semantic_consistency: float | None = None
    conflicting_terms: list[str] = Field(default_factory=list)
    missing_expected_terms: list[str] = Field(default_factory=list)
    possible_topic_revision: str | None = None
    possible_term_revision: dict[str, str] = Field(default_factory=dict)
    validator_results: list[ValidatorResult] = Field(default_factory=list)
    agreement_score: float | None = Field(default=None, ge=0, le=1)
    aggregation_strategy: AggregationStrategy
    aggregated_decision: ValidatorDecision
    reason: str = Field(min_length=1)
    evidence: dict[str, Any] = Field(default_factory=dict)


class ResourceDecision(StrictModel):
    document_id: str = Field(min_length=1)
    term_validation_status: ValidatorDecision | None = None
    topic_validation_status: ValidatorDecision | None = None
    cross_validation_status: ValidatorDecision | None = None
    final_decision: FinalDecision
    decision_rule: str = Field(min_length=1)
    reason: str = Field(min_length=1)
    lifecycle_status: CandidateStatus = CandidateStatus.CANDIDATE
    created_at: datetime = Field(default_factory=utc_now)


class TermValidationRequest(StrictModel):
    document: Document
    candidate: TermCandidate
    context: dict[str, Any] = Field(default_factory=dict)


class TopicValidationRequest(StrictModel):
    document: Document
    candidate: TopicCandidate
    topic_table: list[dict[str, Any]] = Field(default_factory=list)
    context: dict[str, Any] = Field(default_factory=dict)


class CrossValidationRequest(StrictModel):
    document: Document
    validated_terms: list[TermCandidate]
    validated_topic: TopicCandidate
    topic_definition: str = ""
    context: dict[str, Any] = Field(default_factory=dict)

