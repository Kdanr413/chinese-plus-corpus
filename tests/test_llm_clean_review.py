from __future__ import annotations

import unittest

from scripts.prepare.llm_clean_review import safe_cleaning_correction


class LlmCleaningSafetyTests(unittest.TestCase):
    def test_formatting_only_change_is_safe(self) -> None:
        safe, _ = safe_cleaning_correction("电 池，安全", "电池安全。", [])
        self.assertTrue(safe)

    def test_declared_sparse_ocr_typo_is_safe(self) -> None:
        safe, _ = safe_cleaning_correction(
            "系统通过传感嚣采集温度。",
            "系统通过传感器采集温度。",
            [{"type": "ocr_typo", "before": "嚣", "after": "器", "reason": "上下文为传感器数据"}],
        )
        self.assertTrue(safe)

    def test_undeclared_or_rewritten_content_is_rejected(self) -> None:
        undeclared, _ = safe_cleaning_correction(
            "系统通过传感嚣采集温度。", "系统通过传感器采集温度。", []
        )
        rewritten, _ = safe_cleaning_correction(
            "系统采集温度。", "系统实时自动采集温度。", []
        )
        self.assertFalse(undeclared)
        self.assertFalse(rewritten)


if __name__ == "__main__":
    unittest.main()
