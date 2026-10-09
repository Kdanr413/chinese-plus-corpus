from __future__ import annotations

import csv
import io
import unittest
import uuid

from corpus_recognition.annotations import (
    ANNOTATION_FIELDS,
    annotation_status,
    build_annotation_csv,
    parse_annotation_csv,
)


RUN_ID = uuid.UUID("aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa")
DECISION_ID = uuid.UUID("bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb")
DOCUMENT_ROW_ID = uuid.UUID("cccccccc-cccc-4ccc-8ccc-cccccccccccc")


def bundle(*, human: bool = True, required: int = 2) -> dict:
    return {
        "run": {
            "id": RUN_ID,
            "config_snapshot": {
                "human_validation": human,
                "required_annotators": required,
            },
        },
        "documents": [{
            "id": DOCUMENT_ROW_ID,
            "document_id": "doc-1:sentence-1",
            "clean_text": "动力电池需要定期维护。",
            "metadata": {"analysis_unit_type": "sentence"},
        }],
        "resource_decisions": [{
            "id": DECISION_ID,
            "recognition_document_id": DOCUMENT_ROW_ID,
        }],
        "topic_candidates": [{
            "id": uuid.uuid4(),
            "recognition_document_id": DOCUMENT_ROW_ID,
            "topic_name": "动力电池",
            "evidence": {"path": "新能源 > 新能源汽车 > 动力电池"},
        }],
        "term_candidates": [{
            "id": uuid.uuid4(),
            "recognition_document_id": DOCUMENT_ROW_ID,
            "normalized_form": "动力电池",
        }],
        "validator_results": [],
        "human_reviews": [],
    }


def review(scope: str, reviewer: str, value: str, *, reject: bool = False) -> dict:
    return {
        "resource_decision_id": DECISION_ID,
        "reviewer": reviewer,
        "action": "reject" if reject else "confirm",
        "after_state": {
            "review_scope": scope,
            "topics" if scope == "topic" else "terms": value,
        },
    }


class AnnotationWorkflowTests(unittest.TestCase):
    def test_template_is_portable_utf8_csv_and_round_trips(self) -> None:
        content = build_annotation_csv(bundle(required=1), "topic")
        self.assertTrue(content.startswith(b"\xef\xbb\xbf"))
        rows = list(csv.DictReader(io.StringIO(content.decode("utf-8-sig"))))
        self.assertEqual(tuple(rows[0]), ANNOTATION_FIELDS)
        self.assertEqual(rows[0]["text"], "动力电池需要定期维护。")
        self.assertIn("新能源 > 新能源汽车 > 动力电池", rows[0]["machine_candidate"])
        rows[0].update(
            annotator="annotator-a", decision="confirm",
            final_value=rows[0]["machine_candidate"], reason="与上下文一致",
        )
        output = io.StringIO(newline="")
        writer = csv.DictWriter(output, fieldnames=ANNOTATION_FIELDS)
        writer.writeheader()
        writer.writerows(rows)
        parsed = parse_annotation_csv(
            output.getvalue().encode("utf-8"), run_id=RUN_ID, scope="topic"
        )
        self.assertEqual(parsed[0]["annotator"], "annotator-a")

    def test_two_annotators_must_reach_a_real_majority(self) -> None:
        data = bundle(required=2)
        data["human_reviews"] = [
            review("topic", "a", "话题甲"),
            review("topic", "b", "话题乙"),
            review("term", "a", "动力电池"),
            review("term", "b", "动力电池"),
        ]
        status = annotation_status(data)
        self.assertFalse(status["ready"])
        self.assertEqual(status["scopes"]["topic"]["conflicts"], 1)
        data["human_reviews"].append(review("topic", "c", "话题甲"))
        status = annotation_status(data)
        self.assertTrue(status["ready"])
        self.assertEqual(status["consensus"]["topic"][str(DECISION_ID)]["value"], "话题甲")

    def test_disabled_human_validation_does_not_block_pipeline(self) -> None:
        status = annotation_status(bundle(human=False, required=3))
        self.assertTrue(status["ready"])
        self.assertFalse(status["human_validation"])


if __name__ == "__main__":
    unittest.main()
