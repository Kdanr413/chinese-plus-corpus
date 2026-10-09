from __future__ import annotations

import tempfile
import unittest
import json
from pathlib import Path
from unittest.mock import patch

from scripts.ui.server import (
    ExclusiveThreadingHTTPServer,
    Handler,
    build_commands,
    enrich_review_candidates,
    merge_rows,
    normalize_jieba_dictionary,
    normalize_word_table,
    preserve_uploaded_source,
    preserved_uploads,
    uploaded_resource_table,
    raw_corpus_table,
    read_csv_rows,
    read_preview_text,
    required_review_fields,
    safe_relative_path,
    validate_run_inputs,
    within,
    prepare_prompt_update,
    annotate_taxonomy_workbench,
    term_review_issues,
)
from scripts.workflow.prompting import build_messages, load_prompt_config, save_prompt_config


class UiServerTests(unittest.TestCase):
    def test_structure_term_score_threshold_is_forwarded_by_environment(self) -> None:
        _, env = build_commands(
            {
                "steps": ["structure"],
                "term_score_threshold": 0.75,
                "topic_review_required": False,
                "model": {"enabled": False},
            },
            Path("configs/pipeline.json").resolve(),
        )
        self.assertEqual(env["CORPUS_TERM_SCORE_THRESHOLD"], "0.75")

    def test_structure_requires_explicit_valid_term_score_threshold(self) -> None:
        base = {
            "steps": ["structure"],
            "topic_review_required": False,
            "model": {"enabled": False},
        }
        with self.assertRaisesRegex(ValueError, "必须明确填写并确认"):
            build_commands(base, Path("configs/pipeline.json").resolve())
        with self.assertRaisesRegex(ValueError, "必须在 0–1 之间"):
            build_commands(
                {**base, "term_score_threshold": 1.2},
                Path("configs/pipeline.json").resolve(),
            )

    def test_result_sections_surround_structure_and_require_confirmation(self) -> None:
        html = Path("corpus_api/web/index.html").read_text(encoding="utf-8")
        self.assertIn('id="termScoreThreshold"', html)
        self.assertIn('id="confirmStructureButton"', html)
        self.assertIn('id="state-structure" class="stage-state"', html)
        self.assertIn('id="state-publish" class="stage-state"', html)
        self.assertLess(html.index('id="knowledge-results"'), html.index('id="structure"'))
        self.assertLess(html.index('id="structure"'), html.index('id="structured-results"'))
        self.assertLess(html.index('id="structured-results"'), html.index('id="publish"'))
        self.assertIn("确认前不会启动结构化", html)
        self.assertNotIn("const d=await api('/api/v1/recognition/runs/'+id)", html)
        self.assertIn("已从数据库恢复成功任务，等待确认结构化参数", html)

    def test_preview_reads_utf8_and_gb18030_text(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            utf8 = root / "utf8.txt"
            gb18030 = root / "corpus.txt"
            utf8.write_text("管理文件：新能源", encoding="utf-8")
            gb18030.write_text("结构语料：新能源汽车", encoding="gb18030")
            self.assertEqual(read_preview_text(utf8), "管理文件：新能源")
            self.assertEqual(read_preview_text(gb18030), "结构语料：新能源汽车")

    def test_workbench_server_disallows_port_reuse(self) -> None:
        self.assertFalse(ExclusiveThreadingHTTPServer.allow_reuse_address)

    def test_reusable_topic_run_does_not_require_model_credentials(self) -> None:
        payload = {"steps": ["topics"], "reuse_topic_results": True, "model": {}}
        with patch("scripts.ui.server.topic_reuse_status", return_value={"reusable": True}):
            commands, env = build_commands(payload, Path("configs/pipeline.json").resolve())
        self.assertEqual(commands[0][1], ["__TOPIC_REUSE_CHECK__"])
        self.assertNotIn("TOPIC_LLM_API_KEY", env)

    def test_changed_topic_inputs_still_require_model_credentials(self) -> None:
        payload = {"steps": ["topics"], "reuse_topic_results": True, "model": {}}
        with patch("scripts.ui.server.topic_reuse_status", return_value={"reusable": False}):
            with self.assertRaisesRegex(ValueError, "必须填写模型名称和 API Key"):
                build_commands(payload, Path("configs/pipeline.json").resolve())

    def test_term_review_errors_identify_candidate_rows(self) -> None:
        issues = term_review_issues(["TERM_CAND_001 缺少：reviewer", "审核表为空"])
        self.assertEqual(issues[0]["candidate_id"], "TERM_CAND_001")
        self.assertEqual(issues[1]["candidate_id"], "整表")

    def test_web_page_has_integrated_recognition_and_review_controls(self) -> None:
        html = Path("corpus_api/web/index.html").read_text(encoding="utf-8")
        for element in ["machineValidation", "crossValidation", "topicWorkflowDialog", "topicWorkflowFrame", "termScoreThreshold", "termScoreConfirmation", "confirmStructureButton"]:
            self.assertIn(f'id="{element}"', html)
        self.assertIn("/api/v1/recognition/", html)
        self.assertIn("activate", html)
        self.assertIn("输入未变则复用完整索引", html)
        self.assertIn("openTopicWorkflow", html)

    def test_embedded_result_cards_do_not_force_horizontal_overflow(self) -> None:
        html = Path("corpus_api/web/index.html").read_text(encoding="utf-8")
        self.assertIn("repeat(2,minmax(0,1fr))", html)
        self.assertIn("@media(max-width:950px)", html)

    def test_topics_fail_preflight_when_bertopic_config_is_missing(self) -> None:
        with tempfile.TemporaryDirectory(dir=Path.cwd()) as directory:
            config_path = Path(directory) / "pipeline.json"
            config_path.write_text(json.dumps({"topics": {"config": "configs/topics/missing-bertopic.json", "reference_taxonomy": "configs/topics/new_energy_candidate_taxonomy.json"}}), encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "话题识别缺少必需配置文件"):
                validate_run_inputs({"steps": ["topics"]}, config_path)

    def test_taxonomy_validation_feedback_is_appended_to_workbench(self) -> None:
        with tempfile.TemporaryDirectory(dir=Path.cwd()) as directory:
            path = Path(directory) / "taxonomy_review_workbench.csv"
            path.write_text("review_item_id,human_decision,reviewer,reviewed_at\nR1,rename,ZL,2026-08-24\nR2,approve,ZL,2026-08-24\n", encoding="utf-8-sig")
            report = {"issues": [{"review_item_id": "R1", "code": "MISSING_ACTION_FIELDS", "message": "缺少 approved_name", "suggestion": "补填 approved_name"}]}
            with patch("scripts.ui.server.REVIEW_PATH", path):
                annotate_taxonomy_workbench(report)
            fields, rows = read_csv_rows(path)
        self.assertIn("validation_status", fields)
        self.assertEqual(rows[0]["validation_status"], "需修改")
        self.assertEqual(rows[0]["validation_codes"], "MISSING_ACTION_FIELDS")
        self.assertEqual(rows[1]["validation_status"], "通过")

    def test_prompt_templates_round_trip_and_feed_production_messages(self) -> None:
        with tempfile.TemporaryDirectory(dir=Path.cwd()) as directory:
            path = Path(directory) / "prompts.json"
            source = load_prompt_config(Path("missing-default-prompts.json"))
            source["prompts"]["topic_mapping"]["system_prompt"] += " 测试补充。"
            saved = save_prompt_config(path, source)
            loaded = load_prompt_config(path)
            messages = build_messages(loaded["prompts"]["topic_mapping"], {"task_type": "cluster_to_taxonomy_mapping"})
        self.assertIn("测试补充", messages[0]["content"])
        self.assertIn("cluster_to_taxonomy_mapping", messages[1]["content"])
        self.assertTrue(saved["updated_at"])

    def test_single_prompt_update_keeps_all_four_templates(self) -> None:
        template = load_prompt_config(Path("missing-default-prompts.json"))["prompts"]["term_validation"]
        with patch("scripts.ui.server.PROMPT_CONFIG_PATH", Path("missing-default-prompts.json")):
            config = prepare_prompt_update("term_validation", template)
        self.assertEqual(set(config["prompts"]), {"cleaning_validation", "topic_mapping", "taxonomy_validation", "term_validation"})

    def test_optional_cleaning_llm_is_ordered_after_cleaning(self) -> None:
        commands, env = build_commands(
            {"steps": ["cleaning"], "model": {"cleaning_enabled": True, "name": "demo-model", "api_key": "secret-value"}},
            Path("configs/pipeline.json").resolve(),
        )
        labels = [label for label, _ in commands]
        self.assertEqual(labels[0], "语料清洗")
        self.assertEqual(labels[1:], ["生成大模型清洗复核任务（可选）", "大模型清洗复核（可选）", "安全应用大模型清洗建议"])
        self.assertEqual(env["CLEANING_LLM_MODEL"], "demo-model")

    def test_approve_requires_only_definition_boundaries_and_reviewer(self) -> None:
        required = required_review_fields("approve")
        self.assertEqual(required, {"definition", "include_scope", "exclude_scope", "reviewer"})
        self.assertNotIn("approved_label", required)
        self.assertNotIn("approved_parent_id", required)

    def test_add_requires_new_identity_parent_boundaries_and_reviewer(self) -> None:
        required = required_review_fields("add")
        self.assertEqual(
            required,
            {"approved_node_id", "approved_label", "approved_parent_id", "definition", "include_scope", "exclude_scope", "reviewer"},
        )

    def test_duplicate_labels_are_enriched_for_grouped_review(self) -> None:
        rows = enrich_review_candidates(
            [
                {"candidate_id": "T1", "current_label": "动力电池"},
                {"candidate_id": "CLUSTER_001", "current_label": "动力电池"},
                {"candidate_id": "T2", "current_label": "驱动电机"},
            ]
        )
        self.assertEqual(rows[0]["duplicate_label_count"], "2")
        self.assertEqual(rows[0]["duplicate_label_candidates"], "T1|CLUSTER_001")
        self.assertIn("多个候选", rows[0]["duplicate_label_explanation"])
        self.assertEqual(rows[2]["duplicate_label_count"], "1")

    def test_topic_taxonomy_upload_target_is_supported(self) -> None:
        self.assertTrue(callable(Handler.save_upload))

    def test_upload_path_removes_parent_traversal(self) -> None:
        self.assertEqual(safe_relative_path("../../教材/第一章.txt"), Path("教材/第一章.txt"))

    def test_within_rejects_sibling(self) -> None:
        with tempfile.TemporaryDirectory(dir=Path.cwd()) as directory:
            root = Path(directory) / "root"
            root.mkdir()
            self.assertTrue(within(root / "ok.txt", root))
            self.assertFalse(within(root.parent / "bad.txt", root))

    def test_commands_do_not_include_secret(self) -> None:
        payload = {
            "steps": ["cleaning", "topics"],
            "model": {"enabled": True, "name": "demo-model", "api_key": "secret-value"},
            "term_limit": 10,
        }
        commands, env = build_commands(payload, Path("configs/pipeline.json").resolve())
        flattened = " ".join(part for _, command in commands for part in command)
        self.assertNotIn("secret-value", flattened)
        self.assertEqual(env["TERM_LLM_API_KEY"], "secret-value")

    def test_topics_and_terms_build_independent_review_gates(self) -> None:
        commands, _ = build_commands(
            {"steps": ["topics", "terms"], "model": {"name": "demo-model", "api_key": "secret-value"}},
            Path("configs/pipeline.json").resolve(),
        )
        self.assertIn("topic-only", commands[0][1])
        labels = [label for label, _ in commands]
        self.assertIn("BERTopic 聚类结果与参考体系固定匹配（必选）", labels)
        self.assertIn(["__TOPIC_REVIEW_GATE__"], [command for _, command in commands])
        self.assertTrue(any("term-only" in command for _, command in commands))

    def test_topics_and_terms_run_before_segmentation(self) -> None:
        commands, _ = build_commands(
            {"steps": ["cleaning", "topics", "terms", "segmentation", "structure"], "term_score_threshold": 0.5, "model": {"name": "demo-model", "api_key": "secret-value"}},
            Path("configs/pipeline.json").resolve(),
        )
        labels = [label for label, _ in commands]
        self.assertLess(labels.index("话题体系识别（BERTopic）与审核任务生成"), labels.index("话题体系人工审核与发布门禁"))
        self.assertLess(labels.index("话题体系人工审核与发布门禁"), labels.index("领域典型术语候选识别与上下文聚类"))
        self.assertLess(labels.index("领域典型术语候选识别与上下文聚类"), labels.index("基础语言处理与分词"))
        self.assertLess(labels.index("基础语言处理与分词"), labels.index("语料结构化与版本输出"))

    def test_syntax_is_a_separate_step_after_structure(self) -> None:
        commands, _ = build_commands(
            {
                "steps": ["structure", "syntax"],
                "term_score_threshold": 0.5,
                "model": {"enabled": False},
                "topic_review_required": False,
            },
            Path("configs/pipeline.json").resolve(),
        )
        labels = [label for label, _ in commands]
        self.assertLess(labels.index("语料结构化与版本输出"), labels.index("Item 分词、词性与句法结构树"))
        syntax_command = next(command for label, command in commands if label == "Item 分词、词性与句法结构树")
        self.assertIn("syntax", syntax_command)

    def test_terms_alone_requires_published_taxonomy(self) -> None:
        with patch("scripts.ui.server.CURATED_TAXONOMY_PATH", Path("missing-curated-taxonomy.json")):
            with self.assertRaisesRegex(ValueError, "正式 taxonomy.json"):
                build_commands({"steps": ["terms"], "model": {"enabled": False}}, Path("configs/pipeline.json").resolve())

    def test_model_fields_required_when_enabled(self) -> None:
        with self.assertRaises(ValueError):
            build_commands({"steps": ["topics"], "model": {"enabled": False}}, Path("configs/pipeline.json").resolve())

    def test_final_taxonomy_and_term_model_validation_are_independent_options(self) -> None:
        commands, _ = build_commands(
            {"steps": ["topics", "terms"], "model": {"name": "demo-model", "api_key": "secret-value", "taxonomy_validation_enabled": True, "term_enabled": False}},
            Path("configs/pipeline.json").resolve(),
        )
        labels = [label for label, _ in commands]
        self.assertIn("正式话题体系大模型核验（可选）", labels)
        self.assertNotIn("领域术语候选模型验证", labels)

    def test_one_word_per_line_domain_lexicon_is_normalized(self) -> None:
        fields, rows, simple = normalize_word_table("domain_lexicon", "我的词表.txt", "动力电池\n车网互动\n".encode("utf-8"))
        self.assertTrue(simple)
        self.assertIn("force_keep", fields)
        self.assertEqual(rows[0]["term"], "动力电池")
        self.assertEqual(rows[0]["force_keep"], "1")
        self.assertEqual(rows[0]["status"], "active")

    def test_one_word_per_line_terminology_gets_stable_unique_ids(self) -> None:
        _, first, _ = normalize_word_table("terminology", "术语一.txt", "换电站\n充电桩\n".encode("utf-8"))
        _, second, _ = normalize_word_table("terminology", "术语二.csv", "换电站\n".encode("utf-8"))
        self.assertTrue(first[0]["term_id"].startswith("USER_TERM_"))
        self.assertEqual(first[0]["term_id"], second[0]["term_id"])
        self.assertNotEqual(first[0]["term_id"], first[1]["term_id"])

    def test_simple_upload_does_not_erase_richer_existing_row(self) -> None:
        existing = [{"term": "动力电池", "category": "controlled", "notes": "人工维护"}]
        incoming = [{"term": "动力电池", "category": "user_domain", "notes": "自动转换"}, {"term": "换电站"}]
        merged = merge_rows(existing, incoming, "term", preserve_existing=True)
        self.assertEqual(merged[0]["notes"], "人工维护")
        self.assertEqual(len(merged), 2)

    def test_jieba_one_word_lines_are_completed_and_deduplicated(self) -> None:
        rows = normalize_jieba_dictionary("动力电池\n动力电池\n车网互动 300000 nz\n")
        self.assertEqual(rows, ["动力电池 200000 nz", "车网互动 300000 nz"])

    def test_each_file_in_one_batch_is_preserved_separately(self) -> None:
        with tempfile.TemporaryDirectory(dir=Path.cwd()) as directory:
            root = Path(directory) / "reference_uploads"
            manifest = root / "upload_manifest.csv"
            with patch("scripts.ui.server.USER_UPLOAD_ROOT", root), patch("scripts.ui.server.USER_UPLOAD_MANIFEST", manifest):
                first, first_duplicate = preserve_uploaded_source("domain_lexicon", "batch_demo", 0, "词表.txt", "动力电池\n".encode("utf-8"), "ok")
                second, second_duplicate = preserve_uploaded_source("domain_lexicon", "batch_demo", 1, "词表.txt", "换电站\n".encode("utf-8"), "ok")
                sources = preserved_uploads("domain_lexicon")
            self.assertNotEqual(first, second)
            self.assertFalse(first_duplicate)
            self.assertFalse(second_duplicate)
            self.assertEqual(len(sources), 2)
            self.assertTrue(manifest.exists())

    def test_duplicate_uploaded_resource_is_not_archived_twice(self) -> None:
        with tempfile.TemporaryDirectory(dir=Path.cwd()) as directory:
            project = Path(directory)
            root = project / "data" / "raw" / "reference_uploads"
            manifest = root / "upload_manifest.csv"
            payload = "动力电池\n".encode("utf-8")
            with patch("scripts.ui.server.PROJECT_ROOT", project), patch(
                "scripts.ui.server.USER_UPLOAD_ROOT", root
            ), patch("scripts.ui.server.USER_UPLOAD_MANIFEST", manifest):
                first, first_duplicate = preserve_uploaded_source(
                    "terminology", "batch_one", 0, "术语表.txt", payload, "首次"
                )
                second, second_duplicate = preserve_uploaded_source(
                    "terminology", "batch_two", 0, "术语表副本.txt", payload, "重复"
                )
                rows = uploaded_resource_table("terminology")
            self.assertEqual(first, second)
            self.assertFalse(first_duplicate)
            self.assertTrue(second_duplicate)
            self.assertEqual(len(list(root.rglob("*.txt"))), 1)
            self.assertEqual(len(rows), 1)
            self.assertEqual(rows[0]["upload_count"], 1)

    def test_nested_topic_json_replaces_reference_taxonomy(self) -> None:
        with tempfile.TemporaryDirectory(dir=Path.cwd()) as directory:
            project = Path(directory)
            target = project / "configs" / "topics" / "new_energy_candidate_taxonomy.json"
            target.parent.mkdir(parents=True)
            target.write_text("{}", encoding="utf-8")
            uploaded = json.dumps({"taxonomy_version": "test", "classification_object": "测试对象", "root": {"id": "ROOT", "name": "根", "definition": "根定义", "representative_keywords": ["能源"], "children": [{"id": "T1", "name": "一级话题", "definition": "定义", "representative_keywords": ["电池"]}]}, "boundary_candidates": [], "cross_cutting_dimensions": [], "excluded_label_types": []}, ensure_ascii=False).encode("utf-8")
            with patch("scripts.ui.server.PROJECT_ROOT", project), patch.dict("scripts.ui.server.UPLOAD_TARGETS", {"topic_taxonomy": target}, clear=True):
                Handler.save_upload(None, "topic_taxonomy", "taxonomy.json", uploaded, backup_existing=True, merge_existing=False)
            taxonomy = json.loads(target.read_text(encoding="utf-8"))
            self.assertEqual(taxonomy["root"]["children"][0]["id"], "T1")

    def test_full_run_is_blocked_when_only_reference_files_exist(self) -> None:
        with tempfile.TemporaryDirectory(dir=Path.cwd()) as directory:
            project = Path(directory)
            config_path = project / "pipeline.json"
            config_path.write_text(json.dumps({"paths": {"manual_sources": "data/raw/manual", "web_sources": "data/raw/web"}}), encoding="utf-8")
            with patch("scripts.ui.server.PROJECT_ROOT", project):
                with self.assertRaisesRegex(ValueError, "当前没有原始语料"):
                    validate_run_inputs({"steps": ["cleaning", "topics", "terms"]}, config_path)

    def test_full_run_accepts_uploaded_txt_corpus(self) -> None:
        with tempfile.TemporaryDirectory(dir=Path.cwd()) as directory:
            project = Path(directory)
            raw = project / "data" / "raw" / "manual"
            raw.mkdir(parents=True)
            (raw / "语料.txt").write_text("新能源汽车语料", encoding="utf-8")
            topic_root = project / "configs" / "topics"
            topic_root.mkdir(parents=True)
            (topic_root / "bertopic.json").write_text(json.dumps({"embedding_model": "demo", "umap": {}, "hdbscan": {}, "representation": {}}), encoding="utf-8")
            (topic_root / "new_energy_candidate_taxonomy.json").write_text("{}", encoding="utf-8")
            config_path = project / "pipeline.json"
            config_path.write_text(json.dumps({"paths": {"manual_sources": "data/raw/manual", "web_sources": "data/raw/web"}, "topics": {"config": "configs/topics/bertopic.json", "reference_taxonomy": "configs/topics/new_energy_candidate_taxonomy.json"}}), encoding="utf-8")
            with patch("scripts.ui.server.PROJECT_ROOT", project):
                validate_run_inputs({"steps": ["cleaning", "topics", "terms"]}, config_path)

    def test_raw_corpus_table_lists_exact_scope_and_pending_files(self) -> None:
        with tempfile.TemporaryDirectory(dir=Path.cwd()) as directory:
            project = Path(directory)
            manual = project / "data" / "raw" / "manual"
            web = project / "data" / "raw" / "web"
            manual.mkdir(parents=True)
            web.mkdir(parents=True)
            (manual / "教材.txt").write_text("动力电池", encoding="utf-8")
            (web / "政策.txt").write_text("新能源汽车政策", encoding="utf-8")
            config_path = project / "pipeline.json"
            config_path.write_text(json.dumps({"paths": {"manual_sources": "data/raw/manual", "web_sources": "data/raw/web"}}), encoding="utf-8")
            with patch("scripts.ui.server.PROJECT_ROOT", project):
                result = raw_corpus_table(config_path, source="manual")
            self.assertEqual(result["summary"]["files"], 2)
            self.assertEqual(result["summary"]["pending"], 2)
            self.assertEqual(result["pagination"]["filtered"], 1)
            self.assertEqual(result["rows"][0]["name"], "教材.txt")


if __name__ == "__main__":
    unittest.main()
