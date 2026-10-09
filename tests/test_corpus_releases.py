from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from corpus_releases import (
    ReleaseExistsError,
    ReleaseIntegrityError,
    WorkspaceError,
    create_release,
    create_workspace,
    create_workspace_from_release,
    read_release,
    validate_release,
)


class CorpusReleaseLifecycleTests(unittest.TestCase):
    def _workspace(self, root: Path) -> Path:
        workspace = create_workspace(root / "data/workspaces", "chinese-plus", "run-001")
        corpus_file = workspace.path / "corpus" / "corpus_index.txt"
        corpus_file.write_text(
            "Doc 示例\n#Global 领域=新能源\n#Group\nItem: 动力电池安全。\n",
            encoding="gb18030",
        )
        (workspace.path / "metadata" / "corpus_profile.json").write_text(
            '{"documents": 1}\n', encoding="utf-8"
        )
        (workspace.path / "statistics" / "counts.json").write_text(
            '{"items": 1}\n', encoding="utf-8"
        )
        return workspace.path

    def _release(self, root: Path):
        return create_release(
            self._workspace(root),
            root / "data/releases",
            corpus_id="chinese-plus",
            version="1.0.0",
            corpus_format_version="bcc-doc-global-group-item/1.0",
            pipeline_version="1.0",
            search_engine_version="LangSC.BCC 2.0.7",
            metadata={"release_level": "test"},
        )

    def test_workspace_release_read_and_verify_complete_lifecycle(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            release = self._release(root)

            self.assertEqual(
                release.path,
                root / "data/releases/chinese-plus/1.0.0",
            )
            self.assertTrue(release.integrity and release.integrity.valid)
            self.assertFalse(release.manifest["index_built"])
            self.assertEqual(release.manifest["asset_groups"]["corpus"], 1)
            self.assertEqual(release.manifest["checksum"]["algorithm"], "sha256")
            self.assertEqual(
                (release.path / "corpus/corpus_index.txt").read_bytes(),
                (root / "data/workspaces/chinese-plus/run-001/corpus/corpus_index.txt").read_bytes(),
            )

            loaded = read_release(
                root / "data/releases", "chinese-plus", "1.0.0"
            )
            self.assertEqual(loaded.version, "1.0.0")
            self.assertTrue(loaded.integrity and loaded.integrity.valid)

    def test_optional_index_is_recorded(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            workspace = self._workspace(root)
            (workspace / "index" / "Org.dat").write_bytes(b"bcc-index")
            release = create_release(
                workspace,
                root / "data/releases",
                corpus_id="chinese-plus",
                version="1.0.0-indexed",
                corpus_format_version="1.0",
                pipeline_version="1.0",
                search_engine_version="LangSC.BCC 2.0.7",
            )
            self.assertTrue(release.manifest["index_built"])
            self.assertEqual(release.manifest["index_files"], ["index/Org.dat"])

    def test_release_never_overwrites_an_existing_version(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            workspace = self._workspace(root)
            options = {
                "corpus_id": "chinese-plus",
                "version": "1.0.0",
                "corpus_format_version": "1.0",
                "pipeline_version": "1.0",
                "search_engine_version": "LangSC.BCC 2.0.7",
            }
            first = create_release(workspace, root / "data/releases", **options)
            before = (first.path / "manifest.json").read_bytes()
            with self.assertRaises(ReleaseExistsError):
                create_release(workspace, root / "data/releases", **options)
            self.assertEqual((first.path / "manifest.json").read_bytes(), before)

    def test_integrity_check_detects_changed_missing_and_unexpected_files(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            release = self._release(root)
            corpus = release.path / "corpus/corpus_index.txt"
            corpus.write_bytes(b"changed")
            (release.path / "metadata/corpus_profile.json").unlink()
            (release.path / "statistics/unlisted.txt").write_text("extra", encoding="utf-8")

            report = validate_release(release.path)
            self.assertFalse(report.valid)
            self.assertIn("corpus/corpus_index.txt", report.modified_files)
            self.assertIn("metadata/corpus_profile.json", report.missing_files)
            self.assertIn("statistics/unlisted.txt", report.unexpected_files)
            with self.assertRaises(ReleaseIntegrityError):
                read_release(root / "data/releases", "chinese-plus", "1.0.0")

    def test_old_release_creates_editable_reprocessing_task(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            release = self._release(root)
            workspace = create_workspace_from_release(
                root / "data/releases",
                root / "data/workspaces",
                corpus_id="chinese-plus",
                version="1.0.0",
                workspace_id="run-002",
            )
            self.assertEqual(
                workspace.base_release,
                {"corpus_id": "chinese-plus", "version": "1.0.0"},
            )
            task = json.loads(workspace.processing_task_path.read_text(encoding="utf-8"))
            self.assertEqual(task["task_type"], "reprocess_release")
            self.assertEqual(task["status"], "created")
            self.assertEqual(
                (workspace.path / "corpus/corpus_index.txt").read_bytes(),
                (release.path / "corpus/corpus_index.txt").read_bytes(),
            )

    def test_invalid_manifest_field_types_return_an_integrity_report(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            release = self._release(root)
            manifest_path = release.path / "manifest.json"
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            manifest["files"][0]["bytes"] = "not-an-integer"
            manifest["index_files"] = "not-a-list"
            manifest_path.write_text(
                json.dumps(manifest, ensure_ascii=False), encoding="utf-8"
            )
            report = validate_release(release.path)
            self.assertFalse(report.valid)
            self.assertTrue(any("invalid byte count" in item for item in report.errors))
            self.assertIn("index_files must be a list", report.errors)

    def test_release_requires_a_structured_corpus_asset(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            workspace = create_workspace(
                root / "data/workspaces", "chinese-plus", "empty"
            )
            with self.assertRaises(WorkspaceError):
                create_release(
                    workspace.path,
                    root / "data/releases",
                    corpus_id="chinese-plus",
                    version="empty",
                    corpus_format_version="1.0",
                    pipeline_version="1.0",
                    search_engine_version="LangSC.BCC 2.0.7",
                )


if __name__ == "__main__":
    unittest.main()
