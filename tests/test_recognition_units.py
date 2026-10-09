from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from corpus_recognition import ContentUnitTopicIdentifier, Document, build_recognition_units
from corpus_recognition.term import LegacyStatisticalTermExtractor


def source_document(document_id: str, text: str, genre: str) -> Document:
    return Document(
        document_id=document_id,
        source="test",
        raw_text=text,
        clean_text=text,
        metadata={"text_genre": genre, "title": ""},
        processing_version="test/1",
    )


class RecognitionUnitTests(unittest.TestCase):
    def test_stored_sentence_nul_is_removed_before_term_recognition(self) -> None:
        document = source_document("doc-nul", "完整文档正文。", "记叙文")
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "sentences.jsonl"
            path.write_text(
                json.dumps({
                    "doc_id": document.document_id,
                    "sentence_id": "source-sentence-1",
                    "sentence_index": 1,
                    "text": "电动座椅控制单元\x00RDM 开关。",
                }, ensure_ascii=False) + "\n",
                encoding="utf-8",
            )
            units = build_recognition_units([document], sentences_path=path)

        self.assertEqual(units[0].clean_text, "电动座椅控制单元RDM 开关。")
        self.assertNotIn("\x00", units[0].raw_text)

    def test_narrative_uses_existing_non_heading_sentence_rows(self) -> None:
        document = source_document("doc-n", "第一句。第二句！", "记叙文")
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "sentences.jsonl"
            rows = [
                {"doc_id": "doc-n", "sentence_id": "s0", "sentence_index": 0, "text": "小标题", "is_heading": True},
                {"doc_id": "doc-n", "sentence_id": "s1", "sentence_index": 1, "text": "第一句。", "is_heading": False},
                {"doc_id": "doc-n", "sentence_id": "s2", "sentence_index": 2, "text": "第二句！", "is_heading": False},
            ]
            path.write_text("".join(json.dumps(row, ensure_ascii=False) + "\n" for row in rows), encoding="utf-8")
            units = build_recognition_units([document], sentences_path=path)
        self.assertEqual([row.clean_text for row in units], ["第一句。", "第二句！"])
        self.assertTrue(all(row.metadata["analysis_unit_type"] == "sentence" for row in units))

    def test_dialogue_uses_one_complete_speaker_turn(self) -> None:
        document = source_document(
            "doc-d",
            "教师：请检查动力电池。\n还要确认绝缘状态。\n\n学生：好的，我现在检查。",
            "对话",
        )
        units = build_recognition_units([document])
        self.assertEqual(len(units), 2)
        self.assertIn("还要确认绝缘状态", units[0].clean_text)
        self.assertEqual(units[0].metadata["speaker"], "教师")
        self.assertEqual(units[1].metadata["speaker"], "学生")

    def test_content_topic_candidate_does_not_require_title_or_heading(self) -> None:
        unit = source_document("unit-1", "动力电池发生热失控时应立即断开高压电源。", "记叙文")
        extractor = LegacyStatisticalTermExtractor(min_frequency=1, max_candidates=20)
        candidate = ContentUnitTopicIdentifier(extractor).identify([unit], [])[0]
        self.assertEqual(candidate.document_id, "unit-1")
        self.assertFalse(candidate.evidence["title_used_as_fallback"])
        self.assertTrue(candidate.keywords)
        self.assertEqual(candidate.representative_texts, [unit.clean_text])

    def test_topic_identifier_reports_progress_and_checks_control(self) -> None:
        units = [
            source_document(f"unit-{index}", "光伏发电设备运行正常。", "记叙文")
            for index in range(501)
        ]
        progress: list[tuple[int, int]] = []
        checkpoints: list[int] = []
        identifier = ContentUnitTopicIdentifier(
            type("Extractor", (), {"extract": lambda *_: []})()
        )
        candidates = identifier.identify(
            units,
            [{"topic_id": "T1", "topic_name": "光伏发电", "path": "能源 > 光伏发电"}],
            progress_callback=lambda completed, total: progress.append((completed, total)),
            checkpoint_callback=lambda: checkpoints.append(1),
        )
        self.assertEqual(len(candidates), 501)
        self.assertEqual(progress, [(500, 501), (501, 501)])
        self.assertEqual(len(checkpoints), 501)


if __name__ == "__main__":
    unittest.main()
