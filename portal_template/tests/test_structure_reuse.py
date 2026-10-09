from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from scripts.workflow.structure_reuse import (
    SCHEMA_VERSION,
    resource_snapshot,
    reuse_status,
    source_sha256,
)
from unittest.mock import patch


class StructureReuseTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.source = self.root / "data/raw/manual/example.txt"
        self.source.parent.mkdir(parents=True)
        self.source.write_text("动力电池需要热管理。", encoding="utf-8")
        topic = self.root / "data/curated/topics/taxonomy.json"
        topic.parent.mkdir(parents=True)
        topic.write_text(
            json.dumps({"nodes": [{"id": "T1", "name": "动力电池"}], "generated_at": "old"}),
            encoding="utf-8",
        )
        terminology = self.root / "inputs/linguistic/terminology.csv"
        terminology.parent.mkdir(parents=True)
        terminology.write_text("term_id,preferred_term\n1,热管理\n", encoding="utf-8")
        jieba = self.root / "inputs/linguistic/jieba_user_dict.txt"
        jieba.write_text("动力电池 100 n\n", encoding="utf-8")
        index = self.root / "data/processed/index"
        index.mkdir(parents=True)
        (index / "corpus_index.txt").write_text("Doc demo\nItem: demo\n", encoding="gb18030")
        (index / "index_items.jsonl").write_text('{"doc_id":"doc-1"}\n', encoding="utf-8")
        self.config = {
            "workflow": {"pipeline_version": "1.0", "corpus_format_version": "bcc/1.0"},
            "topics": {"reference_taxonomy": "configs/topics/reference.json"},
            "linguistic": {
                "terminology": "inputs/linguistic/terminology.csv",
                "jieba_user_dict": "inputs/linguistic/jieba_user_dict.txt",
            },
            "index": {"encoding": "gb18030"},
        }

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def _manifest(self) -> dict[str, object]:
        resources = resource_snapshot(self.root, self.config)
        relative = self.source.relative_to(self.root).as_posix()
        return {
            "schema_version": SCHEMA_VERSION,
            "resource_fingerprint": resources["fingerprint"],
            "sources": {
                relative.casefold(): {
                    "path": relative,
                    "source_sha256": source_sha256(self.source, cached=False),
                    "doc_id": "doc-1",
                    "item_count": 1,
                }
            },
        }

    def test_matching_source_and_resources_are_reusable(self) -> None:
        resources = resource_snapshot(self.root, self.config)
        result = reuse_status(
            self.root, self.config, self.source,
            manifest=self._manifest(), resources=resources,
        )
        self.assertTrue(result["reusable"])
        self.assertEqual(result["item_count"], 1)

    def test_source_or_resource_change_requires_rebuild(self) -> None:
        manifest = self._manifest()
        self.source.write_text("动力电池内容已经变化。", encoding="utf-8")
        changed_source = reuse_status(self.root, self.config, self.source, manifest=manifest)
        self.assertFalse(changed_source["reusable"])
        self.assertIn("语料内容", changed_source["reason"])

        self.source.write_text("动力电池需要热管理。", encoding="utf-8")
        topic = self.root / "data/curated/topics/taxonomy.json"
        topic.write_text(json.dumps({"nodes": [{"id": "T2", "name": "电池安全"}]}), encoding="utf-8")
        changed_resource = reuse_status(self.root, self.config, self.source, manifest=manifest)
        self.assertFalse(changed_resource["reusable"])
        self.assertIn("话题、术语或分词资源", changed_resource["reason"])

    def test_timestamp_only_change_does_not_invalidate_topic_semantics(self) -> None:
        before = resource_snapshot(self.root, self.config)["fingerprint"]
        topic = self.root / "data/curated/topics/taxonomy.json"
        value = json.loads(topic.read_text(encoding="utf-8"))
        value["generated_at"] = "new"
        topic.write_text(json.dumps(value), encoding="utf-8")
        after = resource_snapshot(self.root, self.config)["fingerprint"]
        self.assertEqual(before, after)

    def test_missing_manifest_does_not_hash_source_file(self) -> None:
        resources = resource_snapshot(self.root, self.config)
        with patch(
            "scripts.workflow.structure_reuse.source_sha256",
            side_effect=AssertionError("source hash should not be computed"),
        ):
            result = reuse_status(
                self.root, self.config, self.source,
                manifest={}, resources=resources,
            )
        self.assertFalse(result["reusable"])
        self.assertEqual(result["source_sha256"], "")
        self.assertIn("尚无逐文件", result["reason"])


if __name__ == "__main__":
    unittest.main()
