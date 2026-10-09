"""Gold Standard metrics; runtime agreement scores are deliberately excluded."""

from __future__ import annotations

from collections import Counter
from typing import Hashable, Iterable, Mapping, Sequence


def _prf(tp: int, fp: int, fn: int) -> dict[str, float | int]:
    precision = tp / (tp + fp) if tp + fp else 0.0
    recall = tp / (tp + fn) if tp + fn else 0.0
    f1 = 2 * precision * recall / (precision + recall) if precision + recall else 0.0
    return {"true_positive": tp, "false_positive": fp, "false_negative": fn,
            "precision": precision, "recall": recall, "f1": f1}


def evaluate_term_extraction(
    predicted: Iterable[Hashable], gold: Iterable[Hashable]
) -> dict[str, float | int]:
    predicted_set, gold_set = set(predicted), set(gold)
    return _prf(len(predicted_set & gold_set), len(predicted_set - gold_set), len(gold_set - predicted_set))


def evaluate_topic_identification(
    predicted: Sequence[str], gold: Sequence[str]
) -> dict[str, float | int]:
    if len(predicted) != len(gold):
        raise ValueError("predicted and gold topic labels must have equal length")
    labels = sorted(set(predicted) | set(gold))
    per_label = {}
    for label in labels:
        tp = sum(p == label and g == label for p, g in zip(predicted, gold))
        fp = sum(p == label and g != label for p, g in zip(predicted, gold))
        fn = sum(p != label and g == label for p, g in zip(predicted, gold))
        per_label[label] = _prf(tp, fp, fn)
    accuracy = sum(p == g for p, g in zip(predicted, gold)) / len(gold) if gold else 0.0
    macro_f1 = sum(float(row["f1"]) for row in per_label.values()) / len(labels) if labels else 0.0
    total_tp = sum(int(row["true_positive"]) for row in per_label.values())
    total_fp = sum(int(row["false_positive"]) for row in per_label.values())
    total_fn = sum(int(row["false_negative"]) for row in per_label.values())
    return {"accuracy": accuracy, "macro_f1": macro_f1,
            "micro_f1": _prf(total_tp, total_fp, total_fn)["f1"],
            "hierarchical_f1": None, "examples": len(gold), "per_label": per_label}


def evaluate_cross_validation(
    predicted_consistent: Sequence[bool], gold_consistent: Sequence[bool]
) -> dict[str, float | int]:
    if len(predicted_consistent) != len(gold_consistent):
        raise ValueError("predicted and gold cross labels must have equal length")
    consistency_accuracy = (
        sum(p == g for p, g in zip(predicted_consistent, gold_consistent)) / len(gold_consistent)
        if gold_consistent else 0.0
    )
    predicted_conflicts = {i for i, value in enumerate(predicted_consistent) if not value}
    gold_conflicts = {i for i, value in enumerate(gold_consistent) if not value}
    conflict = _prf(
        len(predicted_conflicts & gold_conflicts),
        len(predicted_conflicts - gold_conflicts),
        len(gold_conflicts - predicted_conflicts),
    )
    return {"consistency_accuracy": consistency_accuracy,
            "conflict_detection_precision": conflict["precision"],
            "conflict_detection_recall": conflict["recall"],
            "examples": len(gold_consistent)}
