from __future__ import annotations

import csv
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]


class SelectedFileCleaningTests(unittest.TestCase):
    def test_selected_nonduplicate_files_are_not_silently_removed_by_quality_label(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            manual = root / "manual"
            web = root / "web"
            output = root / "processed"
            inventory = root / "inventory.csv"
            manual.mkdir()
            web.mkdir()
            first = manual / "first.txt"
            second = manual / "second.txt"
            first.write_text("动力电池安全管理要求。" * 80, encoding="utf-8")
            second.write_text("新能源汽车维修人员应检查绝缘防护与高压连接。" * 60, encoding="utf-8")
            fields = [
                "path", "relative_path", "root", "category", "source",
                "dedup_status", "quality", "quality_reason",
            ]
            with inventory.open("w", encoding="utf-8-sig", newline="") as handle:
                writer = csv.DictWriter(handle, fieldnames=fields)
                writer.writeheader()
                writer.writerows([
                    {
                        "path": str(first), "relative_path": first.name, "root": "book",
                        "category": "测试", "source": "manual", "dedup_status": "kept",
                        "quality": "完整可用", "quality_reason": "",
                    },
                    {
                        "path": str(second), "relative_path": second.name, "root": "book",
                        "category": "测试", "source": "manual", "dedup_status": "kept",
                        "quality": "纯网站介绍/导航噪声", "quality_reason": "启发式误判样例",
                    },
                ])

            result = subprocess.run(
                [
                    sys.executable, str(PROJECT_ROOT / "scripts/prepare/clean.py"),
                    "--inventory", str(inventory), "--output-root", str(output),
                    "--manual-root", str(manual), "--web-root", str(web),
                    "--config", str(PROJECT_ROOT / "configs/cleaning.json"),
                ],
                cwd=PROJECT_ROOT,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                check=False,
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            documents = [
                json.loads(line)
                for line in (output / "jsonl/documents.jsonl").read_text(encoding="utf-8").splitlines()
                if line.strip()
            ]
            report = json.loads((output / "quality/cleaning_report.json").read_text(encoding="utf-8"))
            self.assertEqual(len(documents), 2)
            self.assertEqual(report["selected_inventory_rows"], 2)
            self.assertEqual(report["documents_written"], 2)
            warned = next(row for row in documents if row["relative_path"] == "second.txt")
            self.assertEqual(warned["selection_warnings"][0]["code"], "inventory_quality_warning")


if __name__ == "__main__":
    unittest.main()
