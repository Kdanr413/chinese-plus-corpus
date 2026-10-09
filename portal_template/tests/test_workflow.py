from __future__ import annotations

import tempfile
import unittest
import csv
import json
import sys
from pathlib import Path
from unittest.mock import patch

from scripts.workflow.stages import (
    _bcc_safe_tree,
    _dialogue_heading_units,
    _match_item_terms,
    _parse_index_counts,
    _segment_text,
    _semantic_topic_levels,
    _split_metadata_values,
    _syntax_chunks,
    _syntax_tree_units,
    _validate_index_annotation_keys,
)
from scripts.terms.extract import load_llm_consensus
from scripts.terms.run_llm import parse_terms
from scripts.terms.publish_terms import main as publish_terms_main, stable_term_id
from scripts.topics.taxonomy_workflow import preserve_matching_llm_responses


class SegmentationTests(unittest.TestCase):
    def test_longest_domain_term_is_not_split(self) -> None:
        terms = [
            {"term": "电池", "term_id": "T1", "priority": "100"},
            {"term": "电池管理系统", "term_id": "T2", "priority": "100"},
        ]
        tokens = _segment_text("电池管理系统BMS", terms)
        self.assertEqual([row["text"] for row in tokens], ["电池管理系统", "BMS"])
        self.assertEqual(tokens[0]["term_id"], "T2")

    def test_ascii_technical_expression_is_kept(self) -> None:
        tokens = _segment_text("V2G+测试", [])
        self.assertEqual(tokens[0]["text"], "V2G")

    def test_bertopic_test_wrappers_are_not_exported_as_topic_levels(self) -> None:
        self.assertEqual(
            _semantic_topic_levels(
                "BERTopic测试体系 > 聚类结果 > BT_TOPIC_012｜储备、光伏发电、互补、片区"
            ),
            ["储备、光伏发电、互补、片区"],
        )
        self.assertEqual(
            _semantic_topic_levels("能源 > 光伏 > 分布式光伏"),
            ["能源", "光伏", "分布式光伏"],
        )


class IndexValidationTests(unittest.TestCase):
    def test_gb18030_index_validation_and_bcc_parentheses(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "index.txt"
            path.write_text(
                "Doc 示例\n#Global 语种=汉语\n#Group\nItem: 新能源。\n",
                encoding="gb18030",
            )
            self.assertEqual(
                _parse_index_counts(path, encoding="gb18030"), (1, 1, [])
            )
            self.assertEqual(
                _validate_index_annotation_keys(path, encoding="gb18030"), []
            )
        self.assertEqual(
            _bcc_safe_tree("(ROOT(CON(x ()(m 一)(x )))"),
            "(ROOT(CON(x （)(m 一)(x ）))",
        )

    def test_dialogue_uses_non_overlapping_leaf_headings(self) -> None:
        sections = [
            {"section_id": "root", "parent_section_id": "", "section_type": "document_root", "start_paragraph_index": 0, "text": "全部"},
            {"section_id": "unit", "parent_section_id": "root", "section_type": "heading", "start_paragraph_index": 0, "text": "第一单元及全部课文"},
            {"section_id": "lesson1", "parent_section_id": "unit", "section_type": "heading", "start_paragraph_index": 1, "text": "第一课\n甲：你好。"},
            {"section_id": "lesson2", "parent_section_id": "unit", "section_type": "heading", "start_paragraph_index": 5, "text": "第二课\n乙：你好。"},
        ]
        self.assertEqual([row["section_id"] for row in _dialogue_heading_units(sections)], ["lesson1", "lesson2"])
    def test_source_type_slash_is_split_without_splitting_other_terms(self) -> None:
        self.assertEqual(
            _split_metadata_values("中文政策/白皮书/权威报告", split_slash=True),
            ["中文政策", "权威报告", "白皮书"],
        )
        self.assertEqual(_split_metadata_values("DC/DC"), ["DC/DC"])
        self.assertEqual(_split_metadata_values(""), [])

    def test_valid_index_counts(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "index.txt"
            path.write_text("Doc demo\n#Global 语种=汉语\n#Group\nItem: 示例。\n", encoding="utf-8")
            self.assertEqual(_parse_index_counts(path), (1, 1, []))

    def test_item_before_doc_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "index.txt"
            path.write_text("Item: 示例。\n", encoding="utf-8")
            docs, items, errors = _parse_index_counts(path)
            self.assertEqual((docs, items), (0, 1))
            self.assertTrue(errors)

    def test_each_item_requires_global_and_group(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "index.txt"
            path.write_text("Doc demo\n#Global 语种=汉语\nItem: 示例。\n", encoding="utf-8")
            errors = _parse_index_counts(path)[2]
            self.assertTrue(any("missing Global or Group" in error for error in errors))

    def test_missing_annotations_are_allowed_but_english_keys_are_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "index.txt"
            path.write_text("Doc demo\n#Global 语种=汉语\n#Group\nItem: 示例。\n", encoding="utf-8")
            self.assertEqual(_validate_index_annotation_keys(path), [])
            path.write_text("Doc demo\n#Global domain=新能源\n#Group pos=Info\nItem: 示例。\n", encoding="utf-8")
            errors = _validate_index_annotation_keys(path)
            self.assertEqual(len(errors), 2)
            self.assertIn("domain", errors[0])

    def test_content_annotation_keys_are_accepted(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "index.txt"
            path.write_text(
                "Doc demo\n"
                "#Global 语种=汉语;语料领域=新能源;语料来源=教材;文体=记叙文;话题=动力电池;任务=动力电池\n"
                "#Group 话题=动力电池;知识维度=技术原理;术语=电池管理系统;任务=动力电池\n"
                "Item: 示例。\n",
                encoding="utf-8",
            )
            self.assertEqual(_validate_index_annotation_keys(path), [])

    def test_terms_are_matched_only_inside_current_item(self) -> None:
        mapping = {"动力电池": "动力电池", "电池包": "动力电池包", "充电桩": "充电桩"}
        self.assertEqual(_match_item_terms("该动力电池连接充电桩。", mapping), ["充电桩", "动力电池"])
        self.assertEqual(_match_item_terms("该系统运行正常。", mapping), [])

    def test_syntax_tree_response_requires_units(self) -> None:
        self.assertEqual(_syntax_tree_units({"Type": "Tree", "Units": "(ROOT (IP 示例))"}), "(ROOT (IP 示例))")
        with self.assertRaises(ValueError):
            _syntax_tree_units({"Type": "Tree"})

    def test_oversized_item_is_chunked_at_sentence_boundary(self) -> None:
        words = ["甲", "。", "乙", "乙", "。", "丙", "丙", "丙", "。"]
        pos = ["n", "w", "n", "n", "w", "n", "n", "n", "w"]
        chunks = _syntax_chunks(words, pos, 5)
        self.assertEqual([chunk_words for chunk_words, _ in chunks], [["甲", "。", "乙", "乙", "。"], ["丙", "丙", "丙", "。"]])


class TermConsensusTests(unittest.TestCase):
    def test_model_reply_parser_accepts_array_or_terms_object(self) -> None:
        self.assertEqual(parse_terms('["动力电池"]'), ["动力电池"])
        self.assertEqual(parse_terms('{"terms":["充电桩"]}'), ["充电桩"])

    def test_consensus_requires_two_runs_and_exact_source_text(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "results.jsonl"
            path.write_text(
                "\n".join([
                    '{"sentence_id":"S1","run_id":1,"terms":["动力电池","原句外术语"]}',
                    '{"sentence_id":"S1","run_id":2,"terms":["动力电池","原句外术语"]}',
                    '{"sentence_id":"S1","run_id":3,"terms":["电动汽车"]}',
                ]) + "\n",
                encoding="utf-8",
            )
            accepted, documents, evidence, rejected = load_llm_consensus(
                path,
                {"S1": {"text": "动力电池用于电动汽车。", "doc_id": "D1"}},
                2,
                set(),
            )
            self.assertIn("动力电池", accepted)
            self.assertNotIn("电动汽车", accepted)
            self.assertNotIn("原句外术语", accepted)
            self.assertEqual(documents["动力电池"], {"D1"})
            self.assertEqual(evidence["动力电池"], ["S1"])
            self.assertGreaterEqual(rejected, 2)


class TermReviewPublicationTests(unittest.TestCase):
    def test_approve_requires_only_reviewer_and_time_and_inherits_candidate(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            workbench = root / "term_review_workbench.csv"
            with workbench.open("w", encoding="utf-8-sig", newline="") as handle:
                writer = csv.DictWriter(handle, fieldnames=["candidate_id", "candidate_term", "human_decision", "reviewer", "reviewed_at"])
                writer.writeheader()
                writer.writerow({"candidate_id": "TERM_CAND_TEST", "candidate_term": "动力电池", "human_decision": "approve", "reviewer": "张老师", "reviewed_at": "2026-08-24"})
            taxonomy = root / "taxonomy.json"
            taxonomy.write_text(json.dumps({"status": "approved", "nodes": []}, ensure_ascii=False), encoding="utf-8")
            output = root / "curated_terms"
            with patch.object(sys, "argv", ["publish_terms.py", "--workbench", str(workbench), "--taxonomy", str(taxonomy), "--output-root", str(output)]):
                self.assertEqual(publish_terms_main(), 0)
            with (output / "terminology.csv").open("r", encoding="utf-8-sig", newline="") as handle:
                row = next(csv.DictReader(handle))
        self.assertEqual(row["term_id"], stable_term_id("动力电池"))
        self.assertEqual(row["preferred_term"], "动力电池")
        self.assertEqual(row["definition"], "")
        self.assertEqual(row["term_type"], "领域术语")


class TopicLlmResumeTests(unittest.TestCase):
    def test_only_responses_for_unchanged_indexed_tasks_are_preserved(self) -> None:
        old_tasks = [{"cluster": {"cluster_id": "A"}}, {"cluster": {"cluster_id": "B"}}]
        new_tasks = [{"cluster": {"cluster_id": "A"}}, {"cluster": {"cluster_id": "C"}}]
        responses = [
            {"task_index": 0, "status": "success"},
            {"task_index": 1, "status": "error"},
            {"task_index": "bad", "status": "success"},
        ]
        self.assertEqual(
            preserve_matching_llm_responses(old_tasks, new_tasks, responses),
            [{"task_index": 0, "status": "success"}],
        )

    def test_matching_error_is_kept_for_audit_without_marking_success(self) -> None:
        task = {"cluster": {"cluster_id": "A"}}
        responses = [{"task_index": 0, "status": "error", "error": "empty content"}]
        self.assertEqual(preserve_matching_llm_responses([task], [task], responses), responses)


if __name__ == "__main__":
    unittest.main()
