from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from corpus_platform.paths import ProjectPaths, resolve_project_root
from corpus_platform.service import PlatformCoordinator


REAL_PROJECT_ROOT = Path(__file__).resolve().parents[1]


class ProjectPathsTests(unittest.TestCase):
    def test_project_root_can_move_and_be_supplied_by_environment(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "configs").mkdir()
            (root / "configs" / "pipeline.json").write_text("{}", encoding="utf-8")
            self.assertEqual(
                resolve_project_root(environment={"CORPUS_PROJECT_ROOT": str(root)}),
                root.resolve(),
            )

    def test_configured_workspace_and_release_paths_follow_project_root(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "configs").mkdir()
            (root / "configs" / "pipeline.json").write_text(
                json.dumps({"paths": {"workspaces_root": "assets/work", "releases_root": "assets/releases"}}),
                encoding="utf-8",
            )
            paths = ProjectPaths.discover(root)
            self.assertEqual(paths.workspaces, (root / "assets/work").resolve())
            self.assertEqual(paths.releases, (root / "assets/releases").resolve())

    def test_index_fingerprint_changes_with_corpus_bytes(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "corpus.txt"
            source.write_text("Doc A\nItem:动力电池\n", encoding="gb18030")
            before = PlatformCoordinator._index_fingerprint(
                [source], search_engine_version="LangSC.BCC 2.0.7"
            )
            source.write_text("Doc A\nItem:光伏发电\n", encoding="gb18030")
            after = PlatformCoordinator._index_fingerprint(
                [source], search_engine_version="LangSC.BCC 2.0.7"
            )
            self.assertNotEqual(before, after)

    def test_index_reuse_requires_complete_native_bcc_files(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            release = root / "releases" / "demo" / "1.0"
            index = release / "index"
            report = release / "metadata" / "index_validation.json"
            index.mkdir(parents=True)
            report.parent.mkdir(parents=True)
            report.write_text(
                json.dumps({"index_input_fingerprint": "same-input"}),
                encoding="utf-8",
            )
            coordinator = PlatformCoordinator.__new__(PlatformCoordinator)
            coordinator.paths = SimpleNamespace(
                releases=root / "releases",
                relative=lambda value: Path(value).relative_to(root).as_posix(),
            )
            destination = root / "destination"
            with patch("corpus_platform.service.validate_release") as validate:
                validate.return_value = SimpleNamespace(valid=True)
                self.assertIsNone(
                    coordinator._reuse_compatible_index("demo", "same-input", destination)
                )
                for name in ("IdxUnit.dat", "DatWord.dat", "Org.dat", "SentInfo.dat"):
                    (index / name).write_bytes(b"bcc")
                reused_from = coordinator._reuse_compatible_index(
                    "demo", "same-input", destination
                )
            self.assertEqual(reused_from, "releases/demo/1.0")
            self.assertTrue((destination / "Org.dat").is_file())


@unittest.skipUnless(REAL_PROJECT_ROOT.is_dir(), "real project is not available")
class CoordinatorReadOnlyTests(unittest.TestCase):
    def setUp(self) -> None:
        self.coordinator = PlatformCoordinator(REAL_PROJECT_ROOT)

    def test_status_explains_all_six_phases_without_database(self) -> None:
        status = self.coordinator.status(check_database=False)
        self.assertEqual([item.number for item in status.phases], [1, 2, 3, 4, 5, 6])
        self.assertIn("python -m corpus_platform", status.next_command)

    def test_publish_dry_run_plan_uses_portable_relative_targets(self) -> None:
        plan = self.coordinator.publish_plan(
            version="9.9.9-test",
            parent_version="0.2.1-bcc-test.3",
        )
        self.assertEqual(plan["output_encoding"], "gb18030")
        self.assertEqual(plan["release"], "data/releases/chinese-plus/9.9.9-test")
        self.assertFalse(plan["will_overwrite"])
        # “完全重置处理结果”后结构化源允许暂时不存在；发布计划必须如实报告，
        # 而不是让只读测试依赖一份历史加工产物。
        self.assertEqual(
            plan["can_execute"],
            plan["source_exists"] and not plan["workspace_exists"] and not plan["release_exists"],
        )

    def test_offline_doctor_does_not_require_database_password(self) -> None:
        result = self.coordinator.doctor(check_database=False)
        self.assertNotIn("postgresql", {item["name"] for item in result["checks"]})


class UnifiedLauncherTests(unittest.TestCase):
    def test_launcher_uses_one_platform_and_preflights_the_port(self) -> None:
        script = (REAL_PROJECT_ROOT / "run.ps1").read_text(encoding="utf-8-sig")
        self.assertIn("/health", script)
        self.assertIn("corpus-portal", script)
        self.assertIn("chinese-plus-corpus-api", script)
        self.assertIn("portal setup", script)
        self.assertIn("serve --setup", script)
        self.assertIn("Get-NetTCPConnection", script)
        self.assertIn("$Stage = 'api'", script)

    def test_default_serve_uses_accounts_before_any_legacy_coordinator(self):
        from corpus_platform.cli import main
        with patch.dict('os.environ',{'API_PORT':'8123','API_HOST':'127.0.0.1'}):
            with patch('corpus_portal.cli.main',return_value=0) as portal, patch('corpus_platform.cli.PlatformCoordinator') as old:
                self.assertEqual(main(['serve','--setup']),0)
                portal.assert_called_once_with(['serve','--host','127.0.0.1','--port','8123','--setup'])
                old.assert_not_called()
    def test_legacy_serve_is_an_explicit_compatibility_option(self):
        from corpus_platform.cli import main
        with patch('corpus_platform.cli.PlatformCoordinator'),patch('corpus_api.__main__.main') as legacy,patch('corpus_portal.cli.main') as portal:
            self.assertEqual(main(['serve','--legacy']),0)
            legacy.assert_called_once()
            portal.assert_not_called()
    def test_only_one_human_web_launcher_remains(self) -> None:
        self.assertTrue((REAL_PROJECT_ROOT / "启动语料库平台.cmd").is_file())
        self.assertFalse((REAL_PROJECT_ROOT / "启动语料库操作台.cmd").exists())


if __name__ == "__main__":
    unittest.main()
