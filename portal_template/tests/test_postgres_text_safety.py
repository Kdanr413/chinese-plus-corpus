from __future__ import annotations

import json
import unittest

from corpus_recognition.documents import adapt_legacy_document
from corpus_recognition.repository import _json, _optional_text, _text
from corpus_recognition.term_engine import normalize_text
from scripts.prepare.clean import clean_text


class PostgreSqlTextSafetyTests(unittest.TestCase):
    def test_clean_text_removes_nul_but_raw_text_remains_auditable(self) -> None:
        document = adapt_legacy_document({
            "doc_id": "nul-1",
            "text": "电动座椅控制单元\x00RDM 开关",
        })

        self.assertEqual(document.raw_text, "电动座椅控制单元\x00RDM 开关")
        self.assertEqual(document.clean_text, "电动座椅控制单元RDM 开关")
        self.assertEqual(document.metadata["removed_database_nul_characters"], 1)

    def test_jsonb_payload_recursively_removes_nul(self) -> None:
        encoded = _json({
            "sentence": "控制单元\x00RDM",
            "nested": ["术语\x00证据", {"key\x00": "value\x00"}],
        })

        self.assertNotIn("\\u0000", encoded)
        self.assertEqual(json.loads(encoded), {
            "sentence": "控制单元RDM",
            "nested": ["术语证据", {"key": "value"}],
        })

    def test_plain_postgresql_text_values_remove_nul(self) -> None:
        self.assertEqual(_text("术语\x00原句"), "术语原句")
        self.assertEqual(_optional_text("理由\x00证据"), "理由证据")
        self.assertIsNone(_optional_text(None))

    def test_term_engine_normalization_removes_nul(self) -> None:
        self.assertEqual(normalize_text("  控制单元\x00 RDM  "), "控制单元 RDM")

    def test_pipeline_clean_copy_removes_nul(self) -> None:
        self.assertEqual(clean_text("控制单元\x00RDM 开关"), "控制单元RDM 开关")


if __name__ == "__main__":
    unittest.main()
