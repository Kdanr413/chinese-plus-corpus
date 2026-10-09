from __future__ import annotations

import unittest
import uuid
import io
import json
import zipfile
from unittest.mock import patch
from datetime import datetime, timezone
from pathlib import Path
from tempfile import TemporaryDirectory

import httpx

from corpus_api import create_app
from corpus_api.service import (
    CorpusService,
    CorpusServiceUnavailableError,
    SearchAssetUnavailableError,
    SearchEngineCache,
    SearchRequestError,
    SearchTargetError,
)
from corpus_registry import DatabaseConfigurationError, DatabaseSettings
from corpus_registry.errors import RegistryNotFoundError
from corpus_recognition.errors import ValidatorResponseError
from corpus_api.recognition import (
    _configured_topic_table_path,
    _hydrate_document_text,
    _pending_human_review_scopes,
    _topic_table_with_text,
)
from corpus_api.topic_workflow import _source_profile


NOW = datetime(2026, 9, 9, tzinfo=timezone.utc)
CORPUS_ID = uuid.UUID("11111111-1111-4111-8111-111111111111")
VERSION_ID = uuid.UUID("22222222-2222-4222-8222-222222222222")


class FakeService:
    def list_corpora(self) -> list[dict[str, object]]:
        return [
            {
                "id": CORPUS_ID,
                "corpus_key": "chinese-plus",
                "name": "‘中文+’语料库",
                "description": "fixture",
                "status": "active",
                "default_version_id": None,
                "created_at": NOW,
                "updated_at": NOW,
            }
        ]

    def list_versions(self, corpus_id: uuid.UUID) -> list[dict[str, object]]:
        if corpus_id != CORPUS_ID:
            raise RegistryNotFoundError(f"corpus not found: {corpus_id}")
        return [
            {
                "id": VERSION_ID,
                "corpus_id": CORPUS_ID,
                "version": "0.2.1-bcc-test.3",
                "status": "ready",
                "searchable": True,
                "parent_version_id": None,
                "release_path": "releases/chinese-plus/0.2.1-bcc-test.3",
                "corpus_format_version": "fixture/1.0",
                "pipeline_version": "1.0",
                "search_engine_version": "LangSC.BCC 2.0.7",
                "checksum_sha256": "a" * 64,
                "created_at": NOW,
                "updated_at": NOW,
            }
        ]

    def search(
        self,
        query: str,
        *,
        corpus_key: str | None = None,
        version: str | None = None,
        limit: int = 100,
    ) -> dict[str, object]:
        del corpus_key, version, limit
        return self._result(query, "Freq", {"Type": "Freq", "Freq": {query: 11}})

    def kwic(
        self,
        query: str,
        *,
        corpus_key: str | None = None,
        version: str | None = None,
        limit: int = 20,
        page: int = 0,
        window: int = 50,
    ) -> dict[str, object]:
        del corpus_key, version, limit, page, window
        result = {"Type": "Context", "Context": [{"Context": f"<Q>{query}</Q>"}]}
        return self._result(query, "Context", result)

    def _result(
        self, query: str, command: str, result: dict[str, object]
    ) -> dict[str, object]:
        return {
            "corpus_id": CORPUS_ID,
            "corpus_key": "chinese-plus",
            "corpus_name": "‘中文+’语料库",
            "version_id": VERSION_ID,
            "version": "0.2.1-bcc-test.3",
            "query": query,
            "command": command,
            "duration_ms": 12,
            "result": result,
        }


async def request(app: object, method: str, path: str, **kwargs: object) -> httpx.Response:
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(
        transport=transport, base_url="http://testserver"
    ) as client:
        return await client.request(method, path, **kwargs)


class CorpusApiTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.app = create_app(lambda: FakeService())

    async def test_health_does_not_require_database_access(self) -> None:
        response = await request(self.app, "GET", "/health")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["status"], "ok")

    def test_topic_taxonomy_profile_uses_exact_ceiling_one_third(self) -> None:
        with TemporaryDirectory() as temporary:
            source = Path(temporary) / "sentences.jsonl"
            source.write_text('{"text":"测试"}\n', encoding="utf-8")
            with patch(
                "corpus_api.topic_workflow.build_units",
                return_value=([object()] * 7, [], {"unit": "sentence"}),
            ):
                profile = _source_profile(source)
        self.assertEqual(profile["source_unit_count"], 7)
        self.assertEqual(profile["sample_size"], 3)
        self.assertEqual(profile["sampling_fraction"], 1 / 3)

    async def test_unified_platform_page_is_available(self) -> None:
        response = await request(self.app, "GET", "/")
        self.assertEqual(response.status_code, 200)
        self.assertIn("text/html", response.headers["content-type"])
        self.assertIn("“中文+”语料库一体化操作台", response.text)
        self.assertIn("建立话题体系并标注全量语句", response.text)
        self.assertIn("话题—术语交叉检验（可选）", response.text)
        self.assertIn("开始完整加工流程", response.text)
        self.assertNotIn('src="/workbench?', response.text)
        self.assertNotIn('src="/search"', response.text)
        self.assertIn('href="/search"', response.text)
        self.assertIn("location.protocol==='file:'", response.text)
        self.assertIn("http://127.0.0.1:8000/", response.text)
        self.assertIn("本区域只提供步骤 5 生成的结构化语料", response.text)
        self.assertIn('id="checkHealthButton"', response.text)
        self.assertIn("进度超过 3 分钟没有更新", response.text)
        self.assertIn("1/3", response.text)
        self.assertIn("记叙文按完整句子识别", response.text)
        self.assertIn("话题体系在独立工作台完成", response.text)
        self.assertIn('class="stage-state"', response.text)
        self.assertIn("查看并确认已上传语料", response.text)
        self.assertIn('id="fileDialog"', response.text)
        self.assertIn("showModal()", response.text)
        self.assertIn("查看正文", response.text)
        self.assertIn('data-file-select', response.text)
        self.assertIn("本页全部选择", response.text)
        self.assertIn("可以直接复用", response.text)
        self.assertIn("只有已勾选文件会进入本次加工", response.text)
        self.assertIn('id="uploadStatus-terminology"', response.text)
        self.assertIn("上传失败：", response.text)
        self.assertIn("d.ok===false", response.text)
        self.assertIn("loadResourceStatus()", response.text)
        self.assertIn('id="resourceDialog"', response.text)
        self.assertIn("查看已上传术语文件", response.text)
        self.assertIn("function stageLab", response.text)
        self.assertIn("if(s.id==='topic')return''", response.text)
        self.assertNotIn("一级话题候选", response.text)
        self.assertIn("function promptFeedback", response.text)
        self.assertIn("corpus.activeRecognitionRun", response.text)
        self.assertIn("restoreRecognitionRun", response.text)
        self.assertIn("resumeRecognitionPipeline", response.text)
        self.assertIn("corpus.downstream.", response.text)
        self.assertIn("Promise.allSettled(backgroundLoads)", response.text)
        self.assertIn("page_size=200", response.text)
        self.assertIn("停止当前任务并新建", response.text)
        self.assertIn("清空页面并开始新一轮", response.text)
        self.assertIn("最终版话题体系表", response.text)
        self.assertIn('target="_blank"', response.text)
        self.assertNotIn("AnnotationDownload", response.text)
        self.assertIn("打开话题体系工作台", response.text)
        self.assertIn("人工冲突裁决", response.text)
        self.assertIn("watchTopicPublication", response.text)
        self.assertIn("corpus.pendingTopicContinuation", response.text)
        self.assertIn("正在对全量语句进行话题分类", response.text)
        self.assertIn("从已发布话题体系继续全量分类", response.text)
        self.assertIn("checkPublishedTopicRecovery", response.text)
        self.assertIn("mirrorOperation", response.text)
        self.assertIn("/api/operation-log", response.text)
        self.assertIn("topic_table_path:'data/curated/topics/taxonomy.json'", response.text)
        self.assertNotIn("scope-action-", response.text)
        self.assertNotIn("saveScopeReview", response.text)
        self.assertNotIn('id="previewModal"', response.text)

        styles = await request(self.app, "GET", "/assets/ui-polish.css")
        self.assertEqual(styles.status_code, 200)
        self.assertIn("text/css", styles.headers["content-type"])
        self.assertIn(".runbar { position: static", styles.text)

    async def test_workbench_operation_log_is_written_without_request_payloads(self) -> None:
        with TemporaryDirectory() as temporary, patch(
            "corpus_api.workbench._OPERATION_LOG_PATH",
            Path(temporary) / "platform-operations.log",
        ) as path:
            response = await request(
                self.app,
                "POST",
                "/api/operation-log",
                json={"level": "进度", "message": "正在检验话题候选：250/60587"},
            )
            self.assertEqual(response.status_code, 200)
            self.assertTrue(response.json()["ok"])
            content = path.read_text(encoding="utf-8")
            self.assertIn("进度：正在检验话题候选：250/60587", content)

    async def test_bootstrap_includes_current_uploaded_resource_status(self) -> None:
        resource = {
            "key": "terminology", "count": 385, "exists": True,
            "source_file_count": 3,
        }
        with patch("corpus_api.workbench.require_admin"), patch(
            "corpus_api.workbench.legacy.resource_summary", return_value=[resource]
        ):
            response = await request(self.app, "GET", "/api/bootstrap")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["resources"], [resource])

    def test_recognition_prefers_the_active_published_taxonomy(self) -> None:
        with TemporaryDirectory() as directory:
            root = Path(directory)
            active = root / "data/curated/topics/taxonomy.json"
            active.parent.mkdir(parents=True)
            active.write_text('{"topics": []}', encoding="utf-8")
            reference = root / "configs/reference.json"
            reference.parent.mkdir(parents=True)
            reference.write_text('{"topics": []}', encoding="utf-8")
            with patch("corpus_api.recognition.PROJECT_ROOT", root), patch(
                "corpus_api.recognition.legacy.read_json",
                return_value={"topics": {"reference_taxonomy": "configs/reference.json"}},
            ):
                self.assertEqual(_configured_topic_table_path(), "data/curated/topics/taxonomy.json")
                active.unlink()
                self.assertEqual(_configured_topic_table_path(), "configs/reference.json")

    def test_annotation_template_hydrates_text_from_workspace_asset(self) -> None:
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            asset = root / "workspaces/run/recognition/01_preprocessed/unit.clean.txt"
            asset.parent.mkdir(parents=True)
            asset.write_text("这是一条待标注的完整句子。", encoding="utf-8")
            bundle = {"documents": [{
                "raw_asset_path": "workspaces/run/recognition/01_preprocessed/unit.clean.txt",
                "clean_asset_path": "workspaces/run/recognition/01_preprocessed/unit.clean.txt",
            }]}
            coordinator = type("Coordinator", (), {
                "registry": type("Registry", (), {"data_root": root})()
            })()
            with patch("corpus_api.recognition.COORDINATOR_FACTORY", return_value=coordinator):
                _hydrate_document_text(bundle)
            self.assertEqual(bundle["documents"][0]["clean_text"], "这是一条待标注的完整句子。")

    def test_final_artifacts_require_separate_topic_and_term_reviews(self) -> None:
        decision_id = uuid.uuid4()
        bundle = {
            "run": {"config_snapshot": {"human_validation": True}},
            "resource_decisions": [{"id": decision_id}],
            "human_reviews": [],
        }
        self.assertEqual(len(_pending_human_review_scopes(bundle)), 2)
        bundle["human_reviews"].append({
            "resource_decision_id": decision_id,
            "after_state": {"review_scope": "topic", "topics": "新能源"},
        })
        self.assertEqual(
            [row["scope"] for row in _pending_human_review_scopes(bundle)],
            ["term"],
        )
        bundle["human_reviews"].append({
            "resource_decision_id": decision_id,
            "after_state": {"review_scope": "term", "terms": "光伏发电"},
        })
        self.assertEqual(_pending_human_review_scopes(bundle), [])

    async def test_raw_corpus_selection_actions_reuse_existing_exclusion_store(self) -> None:
        paths = ["data/raw/manual/example.txt"]
        with patch("corpus_api.workbench.legacy.update_corpus_exclusions") as update:
            update.return_value = {"excluded": 0}
            selected = await request(
                self.app, "POST", "/api/raw-corpus/select", json={"paths": paths}
            )
            self.assertEqual(selected.status_code, 200)
            update.assert_called_once_with(paths, False)

        with patch("corpus_api.workbench.legacy.update_corpus_exclusions") as update:
            update.return_value = {"excluded": 1}
            unselected = await request(
                self.app, "POST", "/api/raw-corpus/unselect", json={"paths": paths}
            )
            self.assertEqual(unselected.status_code, 200)
            update.assert_called_once_with(paths, True)

    async def test_uploaded_resource_list_is_deduplicated_and_previewable(self) -> None:
        item = {
            "category": "terminology", "original_name": "术语.txt",
            "stored_path": "data/raw/reference_uploads/terminology/a.txt",
            "sha256": "a" * 64, "bytes": 10, "upload_count": 2,
            "preview_url": "/api/preview?path=data/raw/reference_uploads/terminology/a.txt",
        }
        with patch("corpus_api.workbench.require_admin"), patch(
            "corpus_api.workbench.legacy.uploaded_resource_table", return_value=[item]
        ) as listing:
            response = await request(
                self.app, "GET", "/api/uploaded-resources?category=terminology"
            )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["items"], [item])
        listing.assert_called_once_with("terminology")

    async def test_file_viewer_is_a_separate_page(self) -> None:
        response = await request(self.app, "GET", "/files/view?path=data/example.txt")
        self.assertEqual(response.status_code, 200)
        self.assertIn("语料文件查看器", response.text)
        self.assertIn("/api/preview?path=", response.text)
        self.assertIn("下载原文件", response.text)

    async def test_api_guide_explains_purpose_usage_and_parameters(self) -> None:
        guide = await request(self.app, "GET", "/docs")
        self.assertEqual(guide.status_code, 200)
        self.assertIn("从用途出发理解每一个 API", guide.text)
        self.assertIn("参数 Parameters", guide.text)
        self.assertIn("调用示例", guide.text)
        self.assertIn("/api/v1/search", guide.text)
        self.assertIn("人工确认、修改话题", guide.text)
        reference = await request(self.app, "GET", "/api-reference")
        self.assertEqual(reference.status_code, 200)
        self.assertIn("swagger-ui", reference.text.lower())

    async def test_structured_artifacts_are_listed_separately_from_release(self) -> None:
        response = await request(self.app, "GET", "/api/structured-artifacts")
        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertIn("ready", body)
        self.assertEqual(body["count"], len(body["items"]))
        for item in body["items"]:
            self.assertTrue(item["download_url"].startswith("/api/download?path="))
            self.assertTrue(item["path"].startswith("data/processed/index/"))

    async def test_file_viewer_and_api_guide_are_available(self) -> None:
        viewer = await request(
            self.app,
            "GET",
            "/files/view?path=data%2Fprocessed%2Findex%2Fcorpus_index.txt",
        )
        self.assertEqual(viewer.status_code, 200)
        self.assertIn("语料文件查看器", viewer.text)
        self.assertIn("/api/preview?path=", viewer.text)
        self.assertIn("下载原文件", viewer.text)

        guide = await request(self.app, "GET", "/docs")
        self.assertEqual(guide.status_code, 200)
        self.assertIn("从用途出发理解每一个 API", guide.text)
        self.assertIn("参数 Parameters", guide.text)
        self.assertIn("/api/v1/search", guide.text)
        self.assertIn("/api-reference", guide.text)

        interactive = await request(self.app, "GET", "/api-reference")
        self.assertEqual(interactive.status_code, 200)
        self.assertIn("swagger-ui", interactive.text)

    async def test_prompt_testing_exposes_context_and_model_feedback(self) -> None:
        stages = await request(self.app, "GET", "/api/v1/recognition/prompt-stages")
        self.assertEqual(stages.status_code, 200)
        self.assertEqual(
            {row["stage"] for row in stages.json()["items"]},
            {"cleaning", "topic", "term", "cross"},
        )
        tested = await request(
            self.app,
            "POST",
            "/api/v1/recognition/prompt-stages/term/test",
            json={"sentence": "动力电池需要进行热管理。", "candidate": "热管理", "topic_reference": "动力电池"},
        )
        self.assertEqual(tested.status_code, 200)
        self.assertTrue(tested.json()["schema_valid"])
        self.assertIn("request_context", tested.json())
        self.assertEqual(len(tested.json()["model_feedback"]), 1)

    async def test_topic_prompt_test_carries_all_three_hierarchy_levels(self) -> None:
        table = [
            {"topic_id": "L1", "topic_name": "新能源技术", "parent_topic_id": ""},
            {"topic_id": "L2", "topic_name": "新能源汽车", "parent_topic_id": "L1"},
            {"topic_id": "L3", "topic_name": "动力电池", "parent_topic_id": "L2"},
        ]
        tested = await request(
            self.app, "POST", "/api/v1/recognition/prompt-stages/topic/test",
            json={
                "sentence": "动力电池系统包括电芯和模组。",
                "topic_level_1": "新能源技术",
                "topic_level_2": "新能源汽车",
                "topic_level_3": "动力电池",
                "topic_reference": json.dumps(table, ensure_ascii=False),
            },
        )
        self.assertEqual(tested.status_code, 200)
        context = tested.json()["request_context"]
        self.assertEqual(context["candidate"]["topic_name"], "动力电池")
        self.assertEqual(
            context["candidate"]["evidence"]["proposed_path"]["level_1"],
            "新能源技术",
        )
        self.assertEqual(len(context["topic_table"]), 3)

    async def test_invalid_external_model_result_is_feedback_not_http_500(self) -> None:
        with patch("corpus_api.recognition.SentenceTermIntersectionProvider") as provider:
            instance = provider.return_value
            instance.name = "bad-model"
            instance.model = "bad-model-id"
            instance.validate_term.side_effect = ValidatorResponseError("缺少 reason 字段")
            tested = await request(
                self.app, "POST", "/api/v1/recognition/prompt-stages/term/test",
                json={
                    "sentence": "动力电池需要热管理。", "candidate": "热管理",
                    "models": [{
                        "id": "bad-model", "model": "bad-model-id",
                        "base_url": "http://model.invalid/v1", "api_key": "local-test-key",
                    }],
                },
            )
        self.assertEqual(tested.status_code, 200)
        self.assertFalse(tested.json()["schema_valid"])
        feedback = tested.json()["model_feedback"][0]
        self.assertEqual(feedback["error_code"], "invalid_model_response")
        self.assertIn("缺少 reason 字段", feedback["reason"])

    async def test_workbench_describes_rule_and_multi_model_modes(self) -> None:
        response = await request(self.app, "GET", "/")
        self.assertIn("术语基础表（无大模型）", response.text)
        self.assertIn("基础表 ∩ 多个外部大模型", response.text)
        self.assertIn("每个模型先逐句独立识别术语", response.text)
        self.assertIn("机器标注和人工裁决统一放在话题工作台", response.text)

    async def test_search_page_can_save_results_and_export_release(self) -> None:
        response = await request(self.app, "GET", "/search")
        self.assertEqual(response.status_code, 200)
        self.assertIn("语料库构建", response.text)
        self.assertIn("语料库查询", response.text)
        self.assertIn("1 选择语料库与版本", response.text)
        self.assertIn("2 设置并执行检索", response.text)
        self.assertIn("3 查看检索结果", response.text)
        self.assertIn("4 保存与导出", response.text)
        self.assertIn("第一版", response.text)
        self.assertIn("查看原始 JSON", response.text)
        self.assertIn("保存查询结果（JSON）", response.text)
        self.assertIn("保存查询结果（CSV）", response.text)
        self.assertIn("导出当前版本结构化语料", response.text)
        self.assertIn("/api/v1/exports/structured", response.text)
        self.assertIn("/api/v1/search", response.text)
        self.assertIn("/api/v1/kwic", response.text)

    async def test_lists_corpora_as_json(self) -> None:
        response = await request(self.app, "GET", "/api/v1/corpora")
        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertEqual(body["count"], 1)
        self.assertEqual(body["items"][0]["id"], str(CORPUS_ID))
        self.assertEqual(body["items"][0]["corpus_key"], "chinese-plus")

    async def test_lists_versions_for_database_uuid(self) -> None:
        response = await request(
            self.app, "GET", f"/api/v1/corpora/{CORPUS_ID}/versions"
        )
        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertEqual(body["corpus_id"], str(CORPUS_ID))
        self.assertEqual(body["items"][0]["version"], "0.2.1-bcc-test.3")

    async def test_missing_corpus_is_404_and_invalid_uuid_is_422(self) -> None:
        missing = uuid.UUID("33333333-3333-4333-8333-333333333333")
        response = await request(
            self.app, "GET", f"/api/v1/corpora/{missing}/versions"
        )
        self.assertEqual(response.status_code, 404)
        self.assertEqual(response.json()["error"]["code"], "registry_not_found")
        self.assertEqual(
            (await request(
                self.app, "GET", "/api/v1/corpora/not-a-uuid/versions"
            )).status_code,
            422,
        )

    async def test_database_failure_is_a_structured_503(self) -> None:
        class UnavailableService(FakeService):
            def list_corpora(self) -> list[dict[str, object]]:
                raise CorpusServiceUnavailableError(
                    "Corpus Registry is temporarily unavailable"
                )

        app = create_app(lambda: UnavailableService())
        response = await request(app, "GET", "/api/v1/corpora")
        self.assertEqual(response.status_code, 503)
        self.assertEqual(response.json()["error"]["code"], "registry_unavailable")

    async def test_frequency_and_kwic_endpoints_return_search_metadata(self) -> None:
        frequency = await request(self.app, "GET", "/api/v1/search?q=汽车&limit=5")
        self.assertEqual(frequency.status_code, 200)
        self.assertEqual(frequency.json()["command"], "Freq")
        self.assertEqual(frequency.json()["result"]["Freq"]["汽车"], 11)
        context = await request(self.app, "GET", "/api/v1/kwic?q=汽车&limit=3")
        self.assertEqual(context.status_code, 200)
        self.assertEqual(context.json()["command"], "Context")

    async def test_search_parameters_are_validated(self) -> None:
        self.assertEqual((await request(self.app, "GET", "/api/v1/search")).status_code, 422)
        self.assertEqual(
            (await request(self.app, "GET", "/api/v1/kwic?q=汽车&limit=101")).status_code,
            422,
        )

    async def test_search_errors_have_stable_http_codes(self) -> None:
        errors = (
            (SearchRequestError("bad query"), 400, "search_request_invalid"),
            (SearchTargetError("choose version"), 409, "search_target_invalid"),
            (SearchAssetUnavailableError("missing index"), 503, "search_asset_unavailable"),
        )
        for error, status, code in errors:
            class ErrorService(FakeService):
                def search(self, *args: object, **kwargs: object) -> dict[str, object]:
                    raise error

            response = await request(
                create_app(lambda: ErrorService()), "GET", "/api/v1/search?q=汽车"
            )
            self.assertEqual(response.status_code, status)
            self.assertEqual(response.json()["error"]["code"], code)

    async def test_openapi_documents_all_current_endpoints(self) -> None:
        response = await request(self.app, "GET", "/openapi.json")
        self.assertEqual(response.status_code, 200)
        paths = response.json()["paths"]
        self.assertIn("/health", paths)
        self.assertIn("/api/v1/corpora", paths)
        self.assertIn("/api/v1/corpora/{corpus_id}/versions", paths)
        self.assertIn("/api/v1/search", paths)
        self.assertIn("/api/v1/kwic", paths)
        self.assertIn("/api/v1/platform/status", paths)
        self.assertIn("/api/v1/platform/publish", paths)
        self.assertIn("/api/v1/platform/publish-status", paths)
        self.assertIn("/api/v1/exports/structured", paths)
        self.assertIn("/api/raw-corpus", paths)
        self.assertIn("/api/structured-artifacts", paths)
        self.assertIn("/api/v1/recognition/runs", paths)
        self.assertIn("/api/raw-corpus", paths)
        self.assertIn("/api/structured-artifacts", paths)


class FakeEngine:
    def frequency(self, query: str, **options: object) -> dict[str, object]:
        return {"Type": "Freq", "Freq": {query: options["number"]}}

    def kwic(self, query: str, **options: object) -> dict[str, object]:
        return {"Type": "Context", "Context": [{"Context": query, **options}]}


class FakeRegistry:
    def __init__(self, release_root: Path) -> None:
        self.release_root = release_root
        self.verify_calls = 0
        self.corpus = {
            "id": CORPUS_ID,
            "corpus_key": "chinese-plus",
            "name": "‘中文+’语料库",
            "status": "active",
            "default_version_id": VERSION_ID,
        }
        self.version = {
            "id": VERSION_ID,
            "version": "0.2.1-bcc-test.3",
            "status": "ready",
            "searchable": True,
            "checksum_sha256": "a" * 64,
        }

    def list_corpora(self) -> list[dict[str, object]]:
        return [self.corpus]

    def get_corpus(self, corpus_key: str) -> dict[str, object]:
        if corpus_key != "chinese-plus":
            raise RegistryNotFoundError("corpus not found")
        return self.corpus

    def get_version(self, corpus_key: str, version: str | None = None) -> dict[str, object]:
        del corpus_key, version
        return self.version

    def resolve_release_path(self, corpus_key: str, version: str) -> Path:
        del corpus_key, version
        return self.release_root

    def verify_registered_files(self, corpus_key: str, version: str) -> dict[str, object]:
        del corpus_key, version
        self.verify_calls += 1
        return {"valid": True}

    def list_files(self, corpus_key: str, version: str) -> list[dict[str, object]]:
        del corpus_key, version
        return [
            {"role": "corpus", "encoding": "gb18030"},
            {"role": "index", "encoding": None},
        ]


class CorpusSearchServiceTests(unittest.TestCase):
    def test_legacy_topic_table_export_is_hydrated_with_sentence_text(self) -> None:
        with TemporaryDirectory() as temporary:
            root = Path(temporary) / "recognition"
            table = root / "03_topic_validation" / "topic_validation_table.csv"
            documents = root / "01_preprocessed" / "documents.jsonl"
            table.parent.mkdir(parents=True)
            documents.parent.mkdir(parents=True)
            table.write_text(
                "document_id,topic_name\ndoc-1,动力电池\n", encoding="utf-8"
            )
            documents.write_text(
                json.dumps({"document_id": "doc-1", "clean_text": "动力电池正在充电。"}, ensure_ascii=False) + "\n",
                encoding="utf-8",
            )
            hydrated = _topic_table_with_text(table)
            exported = hydrated.read_text(encoding="utf-8-sig")
            self.assertTrue(exported.startswith("text,document_id,topic_name"))
            self.assertIn("动力电池正在充电。", exported)

    def test_kwic_metadata_reads_both_global_and_group_from_release(self) -> None:
        with TemporaryDirectory() as temporary:
            release = Path(temporary)
            corpus = release / "corpus"
            corpus.mkdir()
            (corpus / "sample.txt").write_text(
                "Doc demo\n#Global 来源=测试;一级话题=能源\n"
                "#Group 术语=动力电池\nItem: 动力电池测试\n",
                encoding="gb18030",
            )
            service = CorpusService(FakeRegistry(release))
            result = {
                "Context": [{"Source": "demo;术语=动力电池", "Context": "<Q>动力电池</Q>"}]
            }
            (release / "metadata").mkdir()
            (release / "metadata" / "selection.json").write_text(
                '{"corpus_files":[{"bcc_document_id":"demo","document_name":"电池说明书"}]}',
                encoding="utf-8",
            )
            service._enrich_context_metadata(result, release, cache_key="test")
            self.assertEqual(result["Context"][0]["Global"], "来源=测试;一级话题=能源")
            self.assertEqual(result["Context"][0]["Group"], "术语=动力电池")
            self.assertEqual(result["Context"][0]["SourceTitle"], "电池说明书")

    def test_loaded_engine_and_integrity_check_are_reused(self) -> None:
        with TemporaryDirectory() as temporary:
            loads: list[Path] = []

            def loader(path: str | Path) -> FakeEngine:
                loads.append(Path(path))
                return FakeEngine()

            registry = FakeRegistry(Path(temporary))
            service = CorpusService(
                registry, SearchEngineCache(loader=loader)  # type: ignore[arg-type]
            )
            first = service.search("汽车", limit=5)
            second = service.search("汽车", limit=5)
            self.assertEqual(first["result"], second["result"])
            self.assertEqual(loads, [Path(temporary) / "index"])
            self.assertEqual(registry.verify_calls, 1)

    def test_non_gb18030_corpus_is_rejected_before_loading(self) -> None:
        with TemporaryDirectory() as temporary:
            registry = FakeRegistry(Path(temporary))
            registry.list_files = lambda *args: [  # type: ignore[method-assign]
                {"role": "corpus", "encoding": "utf-8"}
            ]
            service = CorpusService(
                registry,
                SearchEngineCache(loader=lambda path: FakeEngine()),  # type: ignore[arg-type]
            )
            with self.assertRaises(SearchAssetUnavailableError):
                service.search("汽车")


def _postgresql_is_configured() -> bool:
    try:
        DatabaseSettings.from_environment()
    except DatabaseConfigurationError:
        return False
    return True


@unittest.skipUnless(
    _postgresql_is_configured()
    and (Path(__file__).resolve().parents[1] / "data/releases/chinese-plus/0.2.1-bcc-test.3/manifest.json").is_file(),
    "requires local DB_* plus the historical 0.2.1-bcc-test.3 Release fixture",
)
class CorpusApiPostgreSQLTests(unittest.IsolatedAsyncioTestCase):
    async def test_real_api_returns_registered_chinese_plus_versions(self) -> None:
        app = create_app()
        corpora_response = await request(app, "GET", "/api/v1/corpora")
        self.assertEqual(corpora_response.status_code, 200)
        corpora = corpora_response.json()["items"]
        corpus = next(item for item in corpora if item["corpus_key"] == "chinese-plus")

        versions_response = await request(
            app, "GET", f"/api/v1/corpora/{corpus['id']}/versions"
        )
        self.assertEqual(versions_response.status_code, 200)
        versions = {item["version"]: item for item in versions_response.json()["items"]}
        self.assertIn("0.2.0-draft", versions)
        self.assertIn("0.2.1-bcc-test.3", versions)
        self.assertTrue(versions["0.2.1-bcc-test.3"]["searchable"])

    async def test_real_registry_release_bcc_frequency_and_kwic(self) -> None:
        app = create_app()
        search = await request(
            app,
            "GET",
            "/api/v1/search?q=汽车",
        )
        self.assertEqual(search.status_code, 200, search.text)
        self.assertEqual(search.json()["version"], "0.2.1-bcc-test.3")
        self.assertEqual(search.json()["result"]["Freq"]["汽车"], 11)
        context = await request(
            app,
            "GET",
            "/api/v1/kwic?q=汽车&limit=2",
        )
        self.assertEqual(context.status_code, 200, context.text)
        self.assertEqual(context.json()["result"]["Type"], "Context")
        self.assertEqual(context.json()["result"]["Count"], 2)

    async def test_unified_status_and_structured_export_use_real_release(self) -> None:
        app = create_app()
        status = await request(app, "GET", "/api/v1/platform/status")
        self.assertEqual(status.status_code, 200, status.text)
        self.assertIn("publish_plan", status.json())
        self.assertTrue(status.json()["publish_plan"]["source_exists"])

        exported = await request(
            app,
            "GET",
            "/api/v1/exports/structured?corpus_key=chinese-plus&version=0.2.1-bcc-test.3",
        )
        self.assertEqual(exported.status_code, 200, exported.text)
        self.assertEqual(exported.headers["content-type"], "application/zip")
        with zipfile.ZipFile(io.BytesIO(exported.content)) as archive:
            names = archive.namelist()
            self.assertIn("manifest.json", names)
            corpus_names = [name for name in names if name.startswith("corpus/")]
            self.assertTrue(corpus_names)
            sample = archive.read(corpus_names[0])
            sample.decode("gb18030")


if __name__ == "__main__":
    unittest.main()
