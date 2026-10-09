"""Rule-based final decisions and lifecycle gates."""

from __future__ import annotations

from .models import (
    CandidateStatus,
    CrossValidationResult,
    FinalDecision,
    ResourceDecision,
    TermValidationResult,
    TopicValidationResult,
    ValidatorDecision,
)


class DecisionEngine:
    """Apply explicit rules; never average unrelated model metrics."""

    version = "1.0"

    def decide(
        self,
        document_id: str,
        term: TermValidationResult | None,
        topic: TopicValidationResult | None,
        cross: CrossValidationResult | None,
    ) -> ResourceDecision:
        statuses = [
            term.aggregated_decision if term else None,
            topic.aggregated_decision if topic else None,
            cross.aggregated_decision if cross else None,
        ]
        if any(value == ValidatorDecision.REJECT for value in statuses):
            final = FinalDecision.REJECT
            rule = "any_reject"
            lifecycle = CandidateStatus.REJECTED
            reason = "术语、话题或交叉验证中至少一项被拒绝"
        elif any(value is None for value in statuses):
            final = FinalDecision.PENDING
            rule = "await_all_three"
            lifecycle = CandidateStatus.CANDIDATE
            reason = "三类验证尚未全部完成"
        elif all(value == ValidatorDecision.ACCEPT for value in statuses):
            final = FinalDecision.AUTO_ACCEPT
            rule = "term_accept AND topic_accept AND cross_accept"
            lifecycle = CandidateStatus.APPROVED
            reason = "三类独立验证均通过"
        else:
            final = FinalDecision.MANUAL_REVIEW
            rule = "any_review OR any_uncertain OR any_conflict"
            lifecycle = CandidateStatus.VALIDATED
            reason = "存在复核、未知或模型意见冲突"
        return ResourceDecision(
            document_id=document_id,
            term_validation_status=statuses[0],
            topic_validation_status=statuses[1],
            cross_validation_status=statuses[2],
            final_decision=final,
            decision_rule=rule,
            reason=reason,
            lifecycle_status=lifecycle,
        )
