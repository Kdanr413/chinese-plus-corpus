from __future__ import annotations

import json
import shutil
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch, MagicMock

from corpus_api.app import WEB_ROOT, create_app
from corpus_api.topic_workflow import (
    _matching_published_round,
    _platform_taxonomy,
    prepare_current,
)
from corpus_topic_workflow import WorkflowManager


class TopicWorkflowIntegrationTests(unittest.TestCase):
    def test_clustering_uses_the_existing_round_sample_without_resampling(self) -> None:
        with TemporaryDirectory() as directory:
            manager = MagicMock()
            manager.root = Path(directory)
            manager.jobs = {"job": {"round_id": "R0001"}}
            manager.round_dir.return_value = Path(directory) / "R0001"
            manager.read_round.return_value = {"sample_path": "sample.jsonl"}
            manager._consolidate_cluster_tasks.return_value = []
            with patch("corpus_topic_workflow.workflow_manager.subprocess.run") as run:
                run.return_value.returncode = 0
                WorkflowManager._run_clustering(manager, "job", "python")
            command = run.call_args.args[0]
            self.assertEqual(command[command.index("--sample-size") + 1], "0")
            self.assertEqual(command[command.index("--sections") + 1], "sample.jsonl")
            self.assertEqual(manager._update_job.call_args.kwargs["status"], "completed")

    def test_server_cache_override_is_used_before_umap_imports(self) -> None:
        from corpus_topic_workflow.run_topic_demo import configure_numba_cache
        config = {"dimensionality_reduction": {"method": "umap", "umap": {"numba_cache_dir": ".cache/numba"}}}
        with TemporaryDirectory() as directory:
            base = Path(directory)
            cache = base / "short" / "project-id"
            with patch.dict("os.environ", {"CORPUS_NUMBA_CACHE_DIR": str(cache)}):
                self.assertEqual(configure_numba_cache(config, base / "deep-project"), cache.resolve())
                import os
                self.assertEqual(os.environ["NUMBA_CACHE_DIR"], str(cache.resolve()))
            self.assertTrue(cache.is_dir())
            self.assertFalse((base / "deep-project").exists())

    def test_recovery_ignores_newer_unpublished_matching_round(self) -> None:
        class FakeManager:
            def list_rounds(self) -> list[dict[str, object]]:
                return [
                    {"round_id": "R0007", "source_sha256": "same", "sample_size": 2, "published": False},
                    {"round_id": "R0006", "source_sha256": "same", "sample_size": 2, "published": True},
                ]

        result = _matching_published_round(
            FakeManager(), {"source_sha256": "same", "sample_size": 2}
        )
        self.assertEqual(result["round_id"], "R0006")

    def test_prepare_current_creates_a_fresh_round_by_default(self) -> None:
        class FakeManager:
            def __init__(self) -> None:
                self.created = 0

            def list_rounds(self) -> list[dict[str, object]]:
                return [{
                    "round_id": "R0004", "source_sha256": "same",
                    "sample_size": 2, "published": True,
                }]

            def create_round(self, *_args: object, **_kwargs: object) -> dict[str, object]:
                self.created += 1
                return {"round_id": "R0005", "published": False}

        manager = FakeManager()
        profile = {"source_unit_count": 6, "sample_size": 2, "source_sha256": "same"}
        with patch("corpus_api.topic_workflow.workflow_manager", return_value=manager), patch(
            "corpus_api.topic_workflow.corpus_data_root", return_value=Path("data")
        ), patch("corpus_api.topic_workflow._source_profile", return_value=profile):
            result = prepare_current({})

        self.assertEqual(manager.created, 1)
        self.assertEqual(result["round"]["round_id"], "R0005")
        self.assertFalse(result["reused"])
        self.assertEqual(result["previous_matching_round_id"], "R0004")

    def test_prepare_current_can_explicitly_reuse_for_recovery(self) -> None:
        class FakeManager:
            def list_rounds(self) -> list[dict[str, object]]:
                return [{
                    "round_id": "R0004", "source_sha256": "same",
                    "sample_size": 2, "published": True,
                }]

        profile = {"source_unit_count": 6, "sample_size": 2, "source_sha256": "same"}
        with patch("corpus_api.topic_workflow.workflow_manager", return_value=FakeManager()), patch(
            "corpus_api.topic_workflow.corpus_data_root", return_value=Path("data")
        ), patch("corpus_api.topic_workflow._source_profile", return_value=profile):
            result = prepare_current({"force_new": False})

        self.assertTrue(result["reused"])
        self.assertEqual(result["round"]["round_id"], "R0004")

    def test_fastapi_exposes_topic_workflow_contract(self) -> None:
        paths = set(create_app().openapi()["paths"])
        self.assertIn("/api/workflow/bootstrap", paths)
        self.assertIn("/api/workflow/rounds/{round_id}/models", paths)
        self.assertIn("/api/workflow/rounds/{round_id}/decisions", paths)
        self.assertIn("/api/workflow/rounds/{round_id}/publish", paths)
        self.assertIn("/api/workflow/rounds/{round_id}/publish-bertopic-test", paths)

    def test_public_taxonomy_is_converted_without_losing_hierarchy(self) -> None:
        source = {
            "taxonomy_name": "测试话题体系",
            "taxonomy_version": 7,
            "topics": [
                {"topic_id": "L1", "name": "一级", "path": "一级", "parent_id": ""},
                {"topic_id": "L2", "name": "二级", "path": "一级 > 二级", "parent_id": "L1"},
                {"topic_id": "L3", "name": "三级", "path": "一级 > 二级 > 三级", "parent_id": "L2"},
            ],
        }
        result = _platform_taxonomy(source, "R0007")
        self.assertEqual(result["source_round_id"], "R0007")
        self.assertEqual([node["parent_id"] for node in result["nodes"]], [None, "L1", "L2"])
        self.assertEqual(result["nodes"][-1]["path"], "一级 > 二级 > 三级")

    def test_test_taxonomy_keeps_an_explicit_platform_marker(self) -> None:
        result = _platform_taxonomy({
            "taxonomy_name": "BERTopic 聚类测试体系",
            "taxonomy_version": 8,
            "publish_mode": "bertopic_test",
            "test_only": True,
            "source_method": "bertopic_clusters_without_model_matching",
            "topics": [],
        }, "R0008")
        self.assertEqual(result["status"], "test")
        self.assertEqual(result["source"], "topic_workflow_bertopic_test")
        self.assertEqual(result["publish_mode"], "bertopic_test")
        self.assertTrue(result["test_only"])

    def test_main_page_contains_modal_entry_and_embedded_workbench(self) -> None:
        main = (WEB_ROOT / "index.html").read_text(encoding="utf-8")
        workbench = (WEB_ROOT / "topic-workflow.html").read_text(encoding="utf-8")
        self.assertIn("topicWorkflowDialog", main)
        self.assertIn("打开话题体系工作台", main)
        self.assertIn("/api/workflow/rounds", workbench)
        self.assertIn("人工", workbench)
        self.assertIn("模型", workbench)
        self.assertIn("跳过模型匹配并发布测试体系", workbench)
        self.assertIn("force_new:true", main)
        self.assertIn("openTopicWorkflowRound(roundId,true)", main)
        self.assertIn("find(item=>item.round_id===pendingTopicRoundId)", main)
        self.assertIn("topic-workflow-operation", main)
        self.assertIn("published_matching_round", main)
        self.assertIn("OPERATION_LOG_KEY", main)
        self.assertIn("new URLSearchParams(location.search).get('round')", workbench)
        self.assertIn("topic-workflow-operation", workbench)

    def test_topic_workflow_package_has_no_fixed_python_path(self) -> None:
        config = Path(__file__).resolve().parents[1] / "corpus_topic_workflow" / "configs" / "workflow.json"
        self.assertNotIn("D:\\\\ProgramData", config.read_text(encoding="utf-8"))

    def test_full_topic_validation_uses_constant_time_document_lookup_and_progress(self) -> None:
        runtime = (
            Path(__file__).resolve().parents[1] / "corpus_recognition" / "runtime.py"
        ).read_text(encoding="utf-8")
        self.assertIn("documents_by_id = {document.document_id: document", runtime)
        self.assertIn("topic_validation_index % 250", runtime)
        self.assertNotIn("next(doc for doc in documents if doc.document_id", runtime)

    def test_packaged_manager_can_create_a_sentence_sampling_round(self) -> None:
        package = Path(__file__).resolve().parents[1] / "corpus_topic_workflow"
        with TemporaryDirectory() as folder:
            root = Path(folder)
            canonical = root / "canonical" / "dynamic_topic_taxonomy.json"
            canonical.parent.mkdir(parents=True)
            shutil.copy2(package / "configs" / "dynamic_topic_taxonomy.json", canonical)
            source = root / "sentences.jsonl"
            rows = [
                {
                    "doc_id": f"doc_{index}",
                    "sentence_id": f"doc_{index}__s_0001",
                    "text": f"第{index}条测试语料用于验证新能源话题体系的分层抽样与人工审核流程。",
                }
                for index in range(1, 5)
            ]
            source.write_text(
                "".join(json.dumps(row, ensure_ascii=False) + "\n" for row in rows),
                encoding="utf-8",
            )
            manager = WorkflowManager(package, root / "results", canonical)
            created = manager.create_round(source, sample_size=3, anchor_ratio=1 / 3, seed=7)
            self.assertEqual(created["round_id"], "R0001")
            self.assertEqual(created["sample_size"], 3)
            self.assertEqual(len(manager.list_rounds()), 1)

    def test_bertopic_clusters_can_be_published_as_isolated_test_taxonomy(self) -> None:
        package = Path(__file__).resolve().parents[1] / "corpus_topic_workflow"
        with TemporaryDirectory() as folder:
            root = Path(folder)
            canonical = root / "canonical" / "dynamic_topic_taxonomy.json"
            canonical.parent.mkdir(parents=True)
            shutil.copy2(package / "configs" / "dynamic_topic_taxonomy.json", canonical)
            source = root / "sentences.jsonl"
            source.write_text(
                "".join(
                    json.dumps({
                        "doc_id": f"doc_{index}",
                        "sentence_id": f"doc_{index}__s_0001",
                        "text": f"第{index}条新能源测试语料具有足够长度用于建立聚类测试话题。",
                    }, ensure_ascii=False) + "\n"
                    for index in range(1, 5)
                ),
                encoding="utf-8",
            )
            manager = WorkflowManager(package, root / "results", canonical)
            # Initialization normalizes a fresh empty schema; publication must then leave it unchanged.
            canonical_before = canonical.read_bytes()
            created = manager.create_round(source, sample_size=3, anchor_ratio=1 / 3, seed=11)
            round_dir = manager.round_dir(created["round_id"])
            tasks = [
                {
                    "round_id": created["round_id"], "cluster_id": "BT_TOPIC_001",
                    "unit_count": 2, "document_count": 2,
                    "representative_words": [{"term": "动力电池", "score": 0.9}, {"term": "荷电状态", "score": 0.8}],
                    "representative_texts": ["动力电池需要监测荷电状态。"],
                },
                {
                    "round_id": created["round_id"], "cluster_id": "BT_TOPIC_002",
                    "unit_count": 1, "document_count": 1,
                    "representative_words": [{"term": "光伏发电", "score": 0.9}],
                    "representative_texts": ["光伏发电系统输出电能。"],
                },
                {
                    "round_id": created["round_id"], "cluster_id": "BT_OUTLIER",
                    "unit_count": 1, "document_count": 1,
                    "representative_words": [{"term": "噪声", "score": 0.1}],
                    "representative_texts": ["无关内容。"],
                },
            ]
            (round_dir / "cluster_tasks.jsonl").write_text(
                "".join(json.dumps(row, ensure_ascii=False) + "\n" for row in tasks),
                encoding="utf-8",
            )
            round_state = manager.read_round(created["round_id"])
            round_state.update(status="clustered", stage=2, cluster_count=3)
            manager.write_round(round_state)

            result = manager.publish_bertopic_test(created["round_id"])

            self.assertEqual(result["taxonomy"]["publish_mode"], "bertopic_test")
            self.assertTrue(result["taxonomy"]["test_only"])
            leaf_paths = [row["path"] for row in result["taxonomy"]["topics"] if row["level"] == 3]
            self.assertEqual(len(leaf_paths), 2)
            self.assertTrue(any("BT_TOPIC_001" in path and "动力电池" in path for path in leaf_paths))
            self.assertFalse(any("BT_OUTLIER" in path for path in leaf_paths))
            self.assertEqual(canonical.read_bytes(), canonical_before)
            self.assertEqual(manager.read_round(created["round_id"])["status"], "published_test")
            published = json.loads((round_dir / "published_changes.json").read_text(encoding="utf-8"))
            self.assertEqual(published["publish_mode"], "bertopic_test")
            self.assertEqual(len(published["applied"]), 2)


if __name__ == "__main__":
    unittest.main()
