"""Agreement aggregation without treating model confidence as accuracy."""

from __future__ import annotations

import math
from collections import Counter, defaultdict
from typing import Mapping, Sequence

from .models import (
    AggregationStrategy,
    TermValidationResult,
    TopicValidationResult,
    ValidatorDecision,
    ValidatorResult,
)


VALID_STATUSES = {"succeeded", "degraded"}


class ConfigurableValidationAggregator:
    """Aggregate independent decisions by majority, configured weight or unanimity."""

    def aggregate(
        self,
        results: Sequence[ValidatorResult],
        strategy: AggregationStrategy,
        *,
        min_validators: int,
        weights: Mapping[str, float] | None = None,
    ) -> tuple[ValidatorDecision, float | None, bool]:
        if min_validators < 1:
            raise ValueError("min_validators must be at least 1")
        effective = [row for row in results if row.status in VALID_STATUSES]
        distinct = {row.decision for row in effective}
        conflict = len(distinct) > 1
        if len(effective) < min_validators:
            agreement = None if not effective else max(Counter(row.decision for row in effective).values()) / len(effective)
            return ValidatorDecision.UNCERTAIN, agreement, conflict

        counts = Counter(row.decision for row in effective)
        if strategy == AggregationStrategy.UNANIMOUS:
            if len(effective) != len(results):
                return ValidatorDecision.UNCERTAIN, 1.0 if len(distinct) == 1 else max(counts.values()) / len(effective), conflict
            if len(distinct) == 1:
                return effective[0].decision, 1.0, False
            return ValidatorDecision.REVIEW, max(counts.values()) / len(effective), True

        if strategy == AggregationStrategy.WEIGHTED:
            totals: dict[ValidatorDecision, float] = defaultdict(float)
            configured = dict(weights or {})
            for row in effective:
                weight = float(configured.get(row.validator, 1.0))
                if weight <= 0:
                    raise ValueError(f"validator weight must be positive: {row.validator}")
                totals[row.decision] += weight
            maximum = max(totals.values())
            winners = [decision for decision, value in totals.items() if math.isclose(value, maximum)]
            decision = winners[0] if len(winners) == 1 else ValidatorDecision.REVIEW
            return decision, maximum / sum(totals.values()), conflict or len(winners) > 1

        maximum = max(counts.values())
        winners = [decision for decision, count in counts.items() if count == maximum]
        decision = winners[0] if len(winners) == 1 else ValidatorDecision.REVIEW
        return decision, maximum / len(effective), conflict or len(winners) > 1

    def aggregate_term(
        self,
        candidate_term_id: str,
        results: Sequence[ValidatorResult],
        strategy: AggregationStrategy,
        *,
        min_validators: int,
        weights: Mapping[str, float] | None = None,
    ) -> TermValidationResult:
        decision, agreement, conflict = self.aggregate(
            results, strategy, min_validators=min_validators, weights=weights
        )
        counts = Counter(row.decision for row in results)
        return TermValidationResult(
            candidate_term_id=candidate_term_id,
            validator_count=len(results),
            accept_count=counts[ValidatorDecision.ACCEPT],
            reject_count=counts[ValidatorDecision.REJECT],
            review_count=counts[ValidatorDecision.REVIEW],
            uncertain_count=counts[ValidatorDecision.UNCERTAIN],
            agreement_score=agreement,
            aggregation_strategy=strategy,
            aggregated_decision=decision,
            conflict=conflict,
            validation_details=list(results),
        )

    def aggregate_topic(
        self,
        candidate_topic_id: str,
        results: Sequence[ValidatorResult],
        strategy: AggregationStrategy,
        *,
        min_validators: int,
        weights: Mapping[str, float] | None = None,
    ) -> TopicValidationResult:
        decision, agreement, conflict = self.aggregate(
            results, strategy, min_validators=min_validators, weights=weights
        )
        counts = Counter(row.decision for row in results)
        mapped = [row.existing_topic_id for row in results if row.existing_topic_id]
        mapped_topic_id = Counter(mapped).most_common(1)[0][0] if mapped else None
        return TopicValidationResult(
            candidate_topic_id=candidate_topic_id,
            validator_count=len(results),
            accept_count=counts[ValidatorDecision.ACCEPT],
            reject_count=counts[ValidatorDecision.REJECT],
            review_count=counts[ValidatorDecision.REVIEW],
            uncertain_count=counts[ValidatorDecision.UNCERTAIN],
            agreement_score=agreement,
            aggregation_strategy=strategy,
            aggregated_decision=decision,
            conflict=conflict,
            validation_details=list(results),
            mapped_topic_id=mapped_topic_id,
            new_topic=any(bool(row.evidence.get("new_topic")) for row in results),
        )
