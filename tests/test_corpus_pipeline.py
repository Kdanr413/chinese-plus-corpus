from __future__ import annotations

import json
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from corpus_pipeline import (
    CorpusPipeline,
    PipelineConfigurationError,
)
from scripts.ui.server import build_commands
from scripts.workflow.core import WorkflowContext
from scripts.workflow.stages import release_stage


PROJECT_ROOT = Path(__file__).resolve().parents[1]


def write_json(path: Path, value: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False), encoding="utf-8")


def write_jsonl(path: Path, rows: list[dict[str, object]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        "".join(json.dumps(row, ensure_ascii=False) + "\n" for row in rows),
        encoding="utf-8",
    )


class CorpusPipelineApiTests(unittest.TestCase):
    def test_package_can_be_imported_and_plans_structure(self) -> None:
        pipeline = CorpusPipeline(PROJECT_ROOT / "configs" / "pipeline.json")
        self.assertEqual(
            pipeline.plan("structure"),
            (("08", "quality_stage"), ("09", "release_stage")),
        )

    def test_invalid_config_raises_typed_error(self) -> None:
        with self.assertRaises(PipelineConfigurationError):
            CorpusPipeline(PROJECT_ROOT / "configs" / "missing.json")

    def test_workbench_uses_stable_module_cli(self) -> None:
        commands, _ = build_commands(
            {"steps": ["cleaning"], "model": {}},
            PROJECT_ROOT / "configs" / "pipeline.json",
        )
        self.assertEqual(commands[0][1][:3], [sys.executable, "-m", "corpus_pipeline"])

    def test_legacy_and_new_cli_have_the_same_clean_plan(self) -> None:
        config = PROJECT_ROOT / "configs" / "pipeline.json"
        legacy = subprocess.run(
            [sys.executable, str(PROJECT_ROOT / "scripts" / "pipeline.py"), "clean", "--config", str(config), "--dry-run"],
            cwd=PROJECT_ROOT,
            capture_output=True,
            text=True,
            encoding="utf-8",
            check=False,
        )
        package = subprocess.run(
            [sys.executable, "-m", "corpus_pipeline", "clean", "--config", str(config), "--dry-run"],
            cwd=PROJECT_ROOT,
            capture_output=True,
            text=True,
            encoding="utf-8",
            check=False,
        )
        self.assertEqual(legacy.returncode, 0, legacy.stderr)
        self.assertEqual(package.returncode, 0, package.stderr)
        self.assertEqual(legacy.stdout, package.stdout)

    def test_new_release_interface_matches_existing_index_builder(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            raw_source = root / "data" / "raw" / "manual" / "demo.txt"
            raw_source.parent.mkdir(parents=True, exist_ok=True)
            raw_source.write_text("动力电池安全。", encoding="utf-8")
            config = root / "configs" / "pipeline.json"
            write_json(
                config,
                {
                    "paths": {
                        "corpus_output": "data/processed/corpus",
                        "topic_output": "data/processed/topics",
                        "term_output": "data/processed/terms",
                        "linguistic_output": "data/processed/linguistic",
                        "governance_output": "data/interim/governance",
                        "index_output": "data/processed/index",
                        "reports_root": "reports",
                        "releases_root": "data/releases",
                    },
                    "workflow": {
                        "corpus_version": "test-parity",
                        "release_level": "internal_draft",
                        "previous_version": "",
                    },
                    "index": {
                        "item_level": "genre_aware",
                        "file_name": "corpus_index.txt",
                    },
                    "linguistic": {
                        "terminology": "inputs/linguistic/terminology.csv",
                    },
                    "quality": {
                        "max_exact_duplicate_rate": 0.01,
                    },
                },
            )
            write_jsonl(
                root / "data" / "processed" / "corpus" / "jsonl" / "documents.jsonl",
                [
                    {
                        "doc_id": "doc_demo",
                        "title": "示例文档",
                        "domain": "新能源",
                        "source_type": "开放教材",
                        "language": "zh",
                        "text_genre": "记叙文",
                        "clean_chars": 8,
                        "original_path": str(raw_source),
                    }
                ],
            )
            write_jsonl(
                root / "data" / "processed" / "corpus" / "jsonl" / "sections.jsonl",
                [
                    {
                        "doc_id": "doc_demo",
                        "section_id": "sec_demo",
                        "parent_section_id": "",
                        "section_type": "document_root",
                        "start_paragraph_index": 0,
                        "text": "动力电池安全。",
                    }
                ],
            )
            write_jsonl(
                root / "data" / "processed" / "corpus" / "jsonl" / "paragraphs.jsonl",
                [{"doc_id": "doc_demo", "paragraph_id": "p_demo", "section_id": "sec_demo", "text": "动力电池安全。"}],
            )
            write_jsonl(
                root / "data" / "processed" / "corpus" / "jsonl" / "sentences.jsonl",
                [{"doc_id": "doc_demo", "sentence_id": "s_demo", "paragraph_id": "p_demo", "section_id": "sec_demo", "text": "动力电池安全。"}],
            )
            write_json(root / "data" / "curated" / "topics" / "taxonomy.json", {"status": "approved", "nodes": []})
            write_json(root / "data" / "processed" / "topics" / "bertopic_run_report.json", {"topics": 0})
            write_jsonl(
                root / "data" / "processed" / "linguistic" / "sentences_tokens.jsonl",
                [{"sentence_id": "s_demo", "text": "动力电池安全。", "tokens": []}],
            )

            old_context = WorkflowContext(root, config)
            first_release = release_stage(old_context)
            self.assertEqual(first_release.metrics["rebuilt_documents"], 1)
            self.assertEqual(first_release.metrics["reused_documents"], 0)
            old_index = (root / "data" / "processed" / "index" / "corpus_index.txt").read_bytes()
            old_items = (root / "data" / "processed" / "index" / "index_items.jsonl").read_bytes()

            second_release = release_stage(old_context)
            self.assertEqual(second_release.metrics["reused_documents"], 1)
            self.assertEqual(second_release.metrics["rebuilt_documents"], 0)
            self.assertEqual(
                old_index,
                (root / "data" / "processed" / "index" / "corpus_index.txt").read_bytes(),
            )

            # The integrated recognition workflow does not run BERTopic and
            # therefore has no legacy BERTopic report.  Release creation must
            # still succeed and record that the cluster metric is inapplicable.
            (root / "data" / "processed" / "topics" / "bertopic_run_report.json").unlink()
            release_without_legacy_topic_report = release_stage(old_context)
            profile = json.loads(
                (old_context.release_root / "corpus_profile.json").read_text(encoding="utf-8")
            )
            self.assertEqual(release_without_legacy_topic_report.metrics["reused_documents"], 1)
            self.assertEqual(profile["topics"]["candidate_clusters"], 0)
            self.assertEqual(profile["topics"]["candidate_cluster_source"], "not_applicable")

            shutil.rmtree(root / "data" / "processed" / "index")
            shutil.rmtree(root / "data" / "releases")

            execution = CorpusPipeline(config, project_root=root).structure()
            new_index = (root / "data" / "processed" / "index" / "corpus_index.txt").read_bytes()
            new_items = (root / "data" / "processed" / "index" / "index_items.jsonl").read_bytes()

            self.assertEqual(execution.status, "needs_human_review")
            self.assertEqual(old_index, new_index)
            self.assertEqual(old_items, new_items)
            self.assertIn(b"Doc ", new_index)
            self.assertIn(b"#Global ", new_index)
            self.assertIn(b"#Group", new_index)
            self.assertIn(b"Item: ", new_index)
            decoded = new_index.decode("gb18030")
            self.assertIn("示例文档", decoded)
            with self.assertRaises(UnicodeDecodeError):
                new_index.decode("utf-8")
            json.dumps(execution.as_dict(), ensure_ascii=False)


if __name__ == "__main__":
    unittest.main()
