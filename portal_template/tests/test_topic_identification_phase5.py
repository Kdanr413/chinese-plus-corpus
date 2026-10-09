from __future__ import annotations

import json
import tempfile
import unittest
import uuid
from pathlib import Path
from unittest.mock import patch

from corpus_recognition import (
    AutoTopicIdentifier,
    ConstrainedTopicClassifier,
    DiscoveredCluster,
    DiscoveryTopicIdentifier,
    Document,
    LegacyBertopicDiscoveryBackend,
    OpenAICompatibleTopicLabelProvider,
    RecognitionRepository,
    ResolvedValidatorSettings,
    TopicResolution,
    SmallCorpusTopicDiscoveryBackend,
    SingleDocumentTopicDiscoveryBackend,
    load_prompt,
    normalize_topic_table,
    persist_topic_candidates,
)
from corpus_recognition.cli import main as recognition_main
from corpus_registry import CorpusRegistry, DatabaseConfigurationError, DatabaseSettings
from corpus_registry.db import connect
from corpus_registry.migrations import initialize_database


PROJECT_ROOT = Path(__file__).resolve().parents[1]


def document(document_id: str, text: str) -> Document:
    return Document(
        document_id=document_id,
        source="unit-test",
        raw_text=text,
        clean_text=text,
        processing_version="test/1",
    )


class FakeDiscoveryBackend:
    name = "fake_discovery"
    version = "1"

    def __init__(self):
        self.calls = 0

    def discover(self, documents):
        self.calls += 1
        return ([DiscoveredCluster(
            cluster_id="CLUSTER_1",
            document_ids=(documents[0].document_id,),
            topic_name="电池安全",
            topic_definition="动力电池安全管理",
            keywords=("动力电池", "热失控"),
            representative_texts=(documents[0].clean_text,),
            evidence={"algorithm": "fake"},
        )], [documents[1].document_id] if len(documents) > 1 else [])


class FakeTransport:
    def complete(self, **kwargs):
        return json.dumps({
            "topic_name": "动力电池安全",
            "topic_definition": "动力电池热失控风险及其处置。",
            "reason": "代表词与文本均指向电池安全",
            "evidence": {"keywords": ["动力电池", "热失控"]},
        }, ensure_ascii=False)


class TopicIdentificationUnitTests(unittest.TestCase):
    def setUp(self) -> None:
        self.documents = [
            document("doc-1", "动力电池发生热失控时应启动安全处置。"),
            document("doc-2", "这是一段无法匹配现有分类的文本。"),
        ]

    def identifier(self, backend=None, mode="auto"):
        backend = backend or FakeDiscoveryBackend()
        return AutoTopicIdentifier(DiscoveryTopicIdentifier(backend), mode=mode), backend

    def test_empty_topic_table_enters_discovery_and_emits_new_topic(self) -> None:
        identifier, backend = self.identifier()
        candidates = identifier.identify(self.documents, [])
        self.assertEqual(backend.calls, 1)
        self.assertEqual(candidates[0].resolution, TopicResolution.NEW_TOPIC)
        self.assertEqual(candidates[0].evidence["document_ids"], ["doc-1"])
        self.assertEqual(candidates[1].resolution, TopicResolution.UNKNOWN)

    def test_nonempty_topic_table_enters_constrained_classification(self) -> None:
        identifier, backend = self.identifier()
        table = [{
            "topic_id": "BATTERY_SAFETY",
            "topic_name": "电池安全",
            "definition": "动力电池的安全风险与处置",
            "keywords": ["动力电池", "热失控"],
        }]
        candidates = identifier.identify(self.documents, table)
        self.assertEqual(backend.calls, 0)
        self.assertEqual(candidates[0].resolution, TopicResolution.EXISTING)
        self.assertEqual(candidates[0].evidence["mapped_topic_id"], "BATTERY_SAFETY")

    def test_no_existing_topic_match_is_explicit_unknown(self) -> None:
        classifier = ConstrainedTopicClassifier()
        nodes = normalize_topic_table([{
            "topic_id": "SOLAR", "topic_name": "太阳能", "keywords": ["光伏"],
        }])
        candidate = classifier.identify([self.documents[1]], nodes)[0]
        self.assertEqual(candidate.topic_name, "UNKNOWN")
        self.assertEqual(candidate.resolution, TopicResolution.UNKNOWN)
        self.assertIn("reason", candidate.evidence)

    def test_current_curated_hierarchy_is_a_supported_topic_table(self) -> None:
        taxonomy_path = PROJECT_ROOT / "data/curated/topics/taxonomy.json"
        if not taxonomy_path.is_file():
            self.skipTest("完全重置后尚未生成新的正式话题体系")
        nodes = normalize_topic_table(taxonomy_path)
        self.assertGreater(len(nodes), 1)
        self.assertTrue(any(node.parent_topic_id for node in nodes))
        self.assertTrue(any(node.definition for node in nodes))

    def test_nested_topic_table_and_flat_table_normalize_to_same_contract(self) -> None:
        nested = {"root": {
            "id": "ROOT", "name": "能源", "children": [
                {"id": "SOLAR", "name": "太阳能", "representative_keywords": ["光伏"]}
            ],
        }}
        rows = normalize_topic_table(nested)
        self.assertEqual({row.topic_id for row in rows}, {"ROOT", "SOLAR"})
        solar = next(row for row in rows if row.topic_id == "SOLAR")
        self.assertEqual(solar.parent_topic_id, "ROOT")
        self.assertEqual(solar.path, "能源 > 太阳能")

    def test_legacy_bertopic_backend_reuses_existing_run_model(self) -> None:
        class FakeModel:
            def get_topic(self, topic_id):
                return [("动力电池", 0.9), ("热失控", 0.8)]

        backend = LegacyBertopicDiscoveryBackend({
            "random_seed": 7,
            "representation": {"top_words": 5},
        })
        with patch(
            "scripts.topics.taxonomy_workflow.run_model",
            return_value=(FakeModel(), [0, -1], [[0.9], [0.1]]),
        ) as existing_runner:
            clusters, outliers = backend.discover(self.documents)
        existing_runner.assert_called_once()
        self.assertEqual(clusters[0].topic_name, "动力电池 / 热失控")
        self.assertEqual(outliers, ["doc-2"])

    def test_single_document_discovery_uses_dedicated_fallback_without_bertopic(self) -> None:
        primary = FakeDiscoveryBackend()
        one = document("doc-one", "动力电池热失控时应启动安全处置。")
        one = one.model_copy(update={
            "metadata": {"title": "动力电池安全规范", "topics_auto": ["动力电池", "安全管理"]},
        })
        identifier = DiscoveryTopicIdentifier(
            primary,
            single_document_backend=SingleDocumentTopicDiscoveryBackend(),
        )
        candidates = identifier.identify([one])
        self.assertEqual(primary.calls, 0)
        self.assertEqual(len(candidates), 1)
        self.assertEqual(candidates[0].source_method, "single_document_metadata_keywords")
        self.assertEqual(candidates[0].resolution, TopicResolution.NEW_TOPIC)
        self.assertTrue(candidates[0].evidence["clustering_skipped"])
        self.assertEqual(candidates[0].evidence["document_ids"], ["doc-one"])

    def test_two_document_discovery_uses_small_corpus_backend_without_bertopic(self) -> None:
        primary = FakeDiscoveryBackend()
        identifier = DiscoveryTopicIdentifier(
            primary,
            small_corpus_backend=SmallCorpusTopicDiscoveryBackend(),
        )
        candidates = identifier.identify(self.documents)
        self.assertEqual(primary.calls, 0)
        self.assertEqual(len(candidates), 2)
        self.assertEqual(
            {candidate.source_method for candidate in candidates},
            {"small_corpus_metadata_keywords"},
        )
        self.assertEqual(
            {candidate.evidence["corpus_document_count"] for candidate in candidates},
            {2},
        )

    def test_optional_model_labeler_records_model_and_prompt_provenance(self) -> None:
        prompt = load_prompt(
            PROJECT_ROOT / "configs/prompts/recognition/topic_discovery_naming/v1.json",
            project_root=PROJECT_ROOT,
        )
        labeler = OpenAICompatibleTopicLabelProvider(
            ResolvedValidatorSettings(
                validator_id="topic-labeler", provider="openai_compatible",
                model="test-model", model_version="2026-09",
                base_url="http://model.invalid/v1", api_key="not-a-real-key",
            ),
            prompt,
            transport=FakeTransport(),
        )
        candidates = DiscoveryTopicIdentifier(FakeDiscoveryBackend(), labeler).identify(self.documents)
        candidate = candidates[0]
        self.assertEqual(candidate.topic_name, "动力电池安全")
        self.assertEqual(candidate.evidence["label"]["model"], "test-model")
        self.assertEqual(candidate.evidence["label"]["prompt_version"], "1.0.0")
        self.assertEqual(candidate.status.value, "candidate")

    def test_classification_cli_does_not_require_bertopic_runtime(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            documents = root / "documents.jsonl"
            table = root / "topics.json"
            output = root / "candidates.jsonl"
            documents.write_text(json.dumps({
                "doc_id": "doc-1", "text": "动力电池热失控", "source": "test",
            }, ensure_ascii=False) + "\n", encoding="utf-8")
            table.write_text(json.dumps({"nodes": [{
                "id": "BATTERY", "name": "电池安全", "keywords": ["动力电池", "热失控"],
            }]}, ensure_ascii=False), encoding="utf-8")
            status = recognition_main([
                "identify-topics", "--documents", str(documents), "--output", str(output),
                "--topic-table", str(table), "--mode", "auto",
            ])
            rows = [json.loads(line) for line in output.read_text(encoding="utf-8").splitlines()]
        self.assertEqual(status, 0)
        self.assertEqual(rows[0]["resolution"], "existing")
        self.assertEqual(rows[0]["status"], "candidate")


def postgresql_is_configured() -> bool:
    try:
        DatabaseSettings.from_environment()
    except DatabaseConfigurationError:
        return False
    return True


@unittest.skipUnless(
    postgresql_is_configured(),
    "set DB_NAME, DB_USER and DB_PASSWORD to run PostgreSQL integration tests",
)
class TopicIdentificationPostgreSQLTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.settings = DatabaseSettings.from_environment()
        initialize_database(cls.settings, PROJECT_ROOT / "db/migrations")

    def test_existing_and_new_topic_candidates_remain_candidates_in_database(self) -> None:
        corpus_key = f"topic-identification-{uuid.uuid4().hex[:10]}"
        with tempfile.TemporaryDirectory() as directory:
            registry = CorpusRegistry(self.settings, data_root=Path(directory))
            repository = RecognitionRepository(self.settings)
            corpus = registry.create_corpus(corpus_key, "话题候选测试")
            try:
                job = registry.create_processing_job(
                    corpus_key, Path(directory) / "workspaces/run-1", "recognition",
                    parameters={"test": True},
                )
                run = repository.create_run(
                    job["id"], pipeline_version="topic-identification/1.0",
                    config_snapshot={"mode": "auto"}, prompt_snapshot={},
                )
                row = repository.add_document(
                    run["id"], document_id="doc-1", source="test",
                    raw_asset_path="workspaces/run-1/recognition/01_preprocessed/raw.txt",
                    clean_asset_path="workspaces/run-1/recognition/01_preprocessed/clean.txt",
                    processing_version="1",
                )
                identifier = AutoTopicIdentifier(
                    DiscoveryTopicIdentifier(FakeDiscoveryBackend()), mode="auto"
                )
                existing = identifier.identify([document("doc-1", "动力电池热失控")], [{
                    "topic_id": "BATTERY", "topic_name": "电池安全", "keywords": ["动力电池"],
                }])
                discovered = identifier.identify(self_documents := [
                    document("doc-1", "动力电池热失控"), document("doc-2", "未知文本")
                ], [])
                stored = persist_topic_candidates(
                    repository, run["id"], [existing[0], discovered[0]],
                    document_row_ids={"doc-1": row["id"]},
                )
                self.assertEqual(len(stored), 2)
                self.assertEqual({item["resolution"] for item in stored}, {"existing", "new_topic"})
                self.assertEqual({item["status"] for item in stored}, {"candidate"})
                self.assertEqual(len(repository.list_topic_candidates(run["id"])), 2)
            finally:
                with connect(self.settings) as connection, connection.transaction():
                    connection.execute("DELETE FROM processing_jobs WHERE corpus_id=%s", (corpus["id"],))
                    connection.execute("DELETE FROM corpora WHERE id=%s", (corpus["id"],))


if __name__ == "__main__":
    unittest.main()
