"""Small coordinator that connects existing corpus packages without replacing them."""

from __future__ import annotations

import importlib.util
import hashlib
import json
import shutil
import sys
from dataclasses import asdict, dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

from corpus_pipeline import CorpusPipeline
from corpus_pipeline.models import PipelineOptions
from corpus_registry import CorpusRegistry, DatabaseSettings
from corpus_registry.errors import RegistryNotFoundError
from corpus_releases import create_release, create_workspace, validate_release
from corpus_search import build_index_isolated, build_scope_indexes, create_corpus_selection

from .paths import ProjectPaths


@dataclass(frozen=True)
class PhaseState:
    number: int
    name: str
    state: str
    details: str


@dataclass(frozen=True)
class PlatformStatus:
    project_root: str
    phases: tuple[PhaseState, ...]
    next_action: str
    next_command: str

    def as_dict(self) -> dict[str, Any]:
        return {
            "project_root": self.project_root,
            "phases": [asdict(item) for item in self.phases],
            "next_action": self.next_action,
            "next_command": self.next_command,
        }


class PlatformCoordinator:
    """Guide and run the existing modules through one stable entry point."""

    def __init__(self, project_root: str | Path | None = None) -> None:
        self.paths = ProjectPaths.discover(project_root)
        self.pipeline = CorpusPipeline(
            self.paths.config,
            project_root=self.paths.root,
        )
        self.config = self.pipeline.context.config

    def doctor(self, *, check_database: bool = True) -> dict[str, Any]:
        packages = ("fastapi", "uvicorn", "psycopg", "LangSC")
        checks: list[dict[str, Any]] = [
            {
                "name": "project_root",
                "ok": self.paths.root.is_dir(),
                "details": str(self.paths.root),
            },
            {
                "name": "python",
                "ok": sys.version_info >= (3, 11),
                "details": sys.executable,
            },
        ]
        checks.extend(
            {
                "name": f"package:{name}",
                "ok": importlib.util.find_spec(name) is not None,
                "details": "installed" if importlib.util.find_spec(name) else "missing",
            }
            for name in packages
        )
        checks.extend(
            {
                "name": f"pipeline:{item.label}",
                "ok": item.passed,
                "details": str(item.path or item.details or ""),
            }
            for item in self.pipeline.diagnose()
        )
        if check_database:
            try:
                settings = DatabaseSettings.from_environment()
                corpora = CorpusRegistry(settings=settings).list_corpora()
                checks.append(
                    {
                        "name": "postgresql",
                        "ok": True,
                        "details": f"{settings.host}:{settings.port}; corpora={len(corpora)}",
                    }
                )
            except Exception as exc:  # diagnostic boundary: report, never mutate
                checks.append({"name": "postgresql", "ok": False, "details": str(exc)})
        return {
            "ok": all(item["ok"] for item in checks),
            "project_root": str(self.paths.root),
            "checks": checks,
        }

    def status(self, *, check_database: bool = True) -> PlatformStatus:
        ctx = self.pipeline.context
        raw_extensions = {".txt", ".html", ".htm", ".jsonl"}
        raw_files: list[Path] = []
        for key in ("manual_sources", "web_sources"):
            directory = ctx.configured_path(key)
            if directory.is_dir():
                raw_files.extend(
                    path for path in directory.rglob("*")
                    if path.is_file() and path.suffix.lower() in raw_extensions
                )

        documents = ctx.configured_path("corpus_output") / "jsonl" / "documents.jsonl"
        structured = ctx.release_root / self.config["index"].get(
            "syntax_tree_file_name", "corpus_index_syntax_tree.txt"
        )
        workspace_controls = tuple(self.paths.workspaces.rglob("workspace.json"))
        release_manifests = tuple(self.paths.releases.rglob("manifest.json"))
        searchable = 0
        database_details = "未检查（离线模式）"
        if check_database:
            try:
                registry = CorpusRegistry()
                corpora = registry.list_corpora()
                for corpus in corpora:
                    searchable += sum(
                        bool(row.get("searchable"))
                        for row in registry.list_versions(corpus["id"])
                    )
                database_details = f"已连接；登记语料库 {len(corpora)} 套，可检索版本 {searchable} 个"
            except Exception as exc:  # status must remain usable while PostgreSQL is down
                database_details = f"未就绪：{exc}"

        phases = (
            PhaseState(1, "原始语料", "ready" if raw_files else "waiting", f"可处理文本文件 {len(raw_files)} 个"),
            PhaseState(2, "清洗与结构化", "ready" if structured.is_file() else ("partial" if documents.is_file() else "waiting"), self.paths.relative(structured if structured.is_file() else documents)),
            PhaseState(3, "Workspace", "ready" if workspace_controls else "waiting", f"工作区 {len(workspace_controls)} 个"),
            PhaseState(4, "Corpus Release 与索引", "ready" if release_manifests else "waiting", f"正式 Manifest {len(release_manifests)} 个"),
            PhaseState(5, "PostgreSQL Registry", "ready" if searchable else "waiting", database_details),
            PhaseState(6, "API 与网页检索", "available" if searchable else "blocked", "存在可检索版本时可启动服务"),
        )
        if not raw_files:
            action = "先通过本地操作台上传原始 TXT 语料"
            command = "python -m corpus_platform serve"
        elif not documents.is_file():
            action = "运行语料清洗"
            command = "python -m corpus_platform process clean"
        elif not structured.is_file():
            action = "完成结构化和句法树加工"
            command = "python -m corpus_platform process structure"
        elif not release_manifests:
            action = "把结构化结果冻结为带 BCC 索引的 Corpus Release"
            command = "python -m corpus_platform publish --version <新版本号> --dry-run"
        elif not searchable:
            action = "配置并连接 PostgreSQL，然后登记 Release"
            command = "python -m corpus_platform doctor"
        else:
            action = "系统已具备检索条件，可以启动本地服务"
            command = "python -m corpus_platform serve"
        return PlatformStatus(str(self.paths.root), phases, action, command)

    def process(self, stage: str, *, dry_run: bool = False, rebuild: bool = True):
        if dry_run:
            return {"stage": stage, "plan": self.pipeline.plan(stage)}
        return self.pipeline.run(stage, PipelineOptions(rebuild=rebuild))

    def publish_plan(
        self,
        *,
        version: str,
        source: str | Path | None = None,
        workspace_id: str | None = None,
        parent_version: str | None = None,
        register: bool = True,
        make_default: bool = False,
    ) -> dict[str, Any]:
        corpus_id = str(self.config["project"]["corpus_id"])
        source_path = self._source_path(source)
        workspace_id = workspace_id or f"release-{version}"
        workspace_path = self.paths.workspaces / corpus_id / workspace_id
        release_path = self.paths.releases / corpus_id / version
        return {
            "corpus_id": corpus_id,
            "version": version,
            "source": self.paths.relative(source_path),
            "workspace": self.paths.relative(workspace_path),
            "release": self.paths.relative(release_path),
            "parent_version": parent_version,
            "output_encoding": "gb18030",
            "build_scope_indexes": True,
            "reuse_unchanged_index": True,
            "register_in_postgresql": register,
            "make_default": make_default,
            "will_overwrite": False,
            "source_exists": source_path.is_file(),
            "workspace_exists": workspace_path.exists(),
            "release_exists": release_path.exists(),
            "can_execute": source_path.is_file() and not workspace_path.exists() and not release_path.exists(),
        }

    def publish(
        self,
        *,
        version: str,
        source: str | Path | None = None,
        catalog: str | Path | None = None,
        workspace_id: str | None = None,
        parent_version: str | None = None,
        register: bool = True,
        make_default: bool = False,
    ) -> dict[str, Any]:
        plan = self.publish_plan(
            version=version,
            source=source,
            workspace_id=workspace_id,
            parent_version=parent_version,
            register=register,
            make_default=make_default,
        )
        corpus_id = plan["corpus_id"]
        workspace_id = workspace_id or f"release-{version}"
        source_path = self._source_path(source)
        catalog_path = self._catalog_path(catalog)
        if not plan["source_exists"]:
            raise ValueError(f"句法树结构化语料不存在：{source_path}")
        if plan["workspace_exists"]:
            raise ValueError(f"Workspace 已存在，拒绝覆盖：{plan['workspace']}")
        if plan["release_exists"]:
            raise ValueError(f"Corpus Release 已存在，拒绝覆盖：{plan['release']}")
        workspace = create_workspace(self.paths.workspaces, corpus_id, workspace_id)
        selection = create_corpus_selection(
            source_path,
            workspace.path / "corpus",
            catalog_path=catalog_path,
            output_encoding="gb18030",
        )
        selection_metadata = json.loads(selection.manifest_path.read_text(encoding="utf-8"))
        selection_metadata.update(
            {
                "source_path": self.paths.relative(source_path),
                "catalog_path": self.paths.relative(catalog_path) if catalog_path else "",
                "output_dir": "corpus",
            }
        )
        metadata_path = workspace.path / "metadata" / "selection.json"
        metadata_path.write_text(
            json.dumps(selection_metadata, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        selection.manifest_path.unlink()
        from corpus_api.knowledge_assets import snapshot_knowledge
        snapshot_knowledge(self.paths.root, workspace.path / "metadata/knowledge", [entry["document_id"] for entry in selection_metadata.get("corpus_files", [])])

        workflow = self.config["workflow"]
        search_engine_version = str(workflow["search_engine_version"])
        index_fingerprint = self._index_fingerprint(
            selection.corpus_files,
            search_engine_version=search_engine_version,
        )
        reused_release = self._reuse_compatible_index(
            corpus_id,
            index_fingerprint,
            workspace.path / "index",
        )
        if reused_release is None:
            index_result = build_index_isolated(
                workspace.path / "index", selection.corpus_files
            )
            scope_result = build_scope_indexes(workspace.path / "index", selection.corpus_files)
            index_status = index_result.status_code
            index_files = len(index_result.files)
            scope_indexes = scope_result.scope_count
            index_action = "built"
        else:
            index_files = sum(
                1 for path in (workspace.path / "index").rglob("*") if path.is_file()
            )
            scope_indexes = sum(
                1
                for path in (workspace.path / "index" / "scope_indexes").glob("scope-*")
                if path.is_dir()
            )
            index_status = 1
            index_action = "reused"
        validation = {
            "created_at": datetime.now().astimezone().isoformat(timespec="seconds"),
            "source": self.paths.relative(source_path),
            "source_sha256": selection.source_sha256,
            "corpus_files": len(selection.corpus_files),
            "selected_documents": len(selection.selected_documents),
            "selected_items": selection.selected_items,
            "output_encoding": selection.output_encoding,
            "index_status": index_status,
            "index_files": index_files,
            "scope_indexes": scope_indexes,
            "index_action": index_action,
            "index_input_fingerprint": index_fingerprint,
            "index_reused_from": reused_release,
        }
        (workspace.path / "metadata" / "index_validation.json").write_text(
            json.dumps(validation, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        task = json.loads(workspace.processing_task_path.read_text(encoding="utf-8"))
        task.update({"task_type": "build_bcc_release", "status": "succeeded", "result": validation})
        workspace.processing_task_path.write_text(
            json.dumps(task, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
        )

        corpus_format = str(workflow["corpus_format_version"])
        if "syntax-tree" not in corpus_format:
            corpus_format = corpus_format.replace("bcc-", "bcc-syntax-tree-", 1)
        based_on = {"corpus_id": corpus_id, "version": parent_version} if parent_version else None
        release = create_release(
            workspace.path,
            self.paths.releases,
            corpus_id=corpus_id,
            version=version,
            corpus_format_version=corpus_format,
            pipeline_version=str(workflow["pipeline_version"]),
            search_engine_version=str(workflow["search_engine_version"]) + " + verified-scope-shards/1.0",
            based_on_release=based_on,
            metadata={
                "source_asset": self.paths.relative(source_path),
                "index_input_fingerprint": index_fingerprint,
                "index_action": index_action,
                "index_reused_from": reused_release,
            },
        )
        registered = None
        if register:
            registry = CorpusRegistry()
            try:
                registry.get_corpus(corpus_id)
            except RegistryNotFoundError:
                registry.create_corpus(corpus_id, str(self.config["project"]["name"]))
            registered = registry.register_release(
                corpus_key=corpus_id,
                version=version,
                release_path=release.path,
                corpus_format_version=corpus_format,
                pipeline_version=str(workflow["pipeline_version"]),
                search_engine_version=str(workflow["search_engine_version"]) + " + verified-scope-shards/1.0",
                status="ready",
                searchable=True,
                parent_version=parent_version,
                make_default=make_default,
            )
        return {
            **plan,
            "workspace_status": "succeeded",
            "release_checksum": release.manifest["checksum"]["value"],
            "release_files": len(release.manifest["files"]),
            "registered": registered is not None,
            "index_action": index_action,
            "index_reused_from": reused_release,
        }

    @staticmethod
    def _index_fingerprint(
        corpus_files: list[Path] | tuple[Path, ...],
        *,
        search_engine_version: str,
    ) -> str:
        """Identify the exact bytes and options that affect a BCC index."""
        digest = hashlib.sha256()
        digest.update(b"bcc-index-input/v1\0")
        digest.update(search_engine_version.encode("utf-8"))
        digest.update(b"\0Structure=HZ\0scope-indexes=v1\n")
        for number, path in enumerate(corpus_files):
            digest.update(f"{number}:{path.name}".encode("utf-8"))
            digest.update(b"\0")
            with path.open("rb") as handle:
                for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                    digest.update(chunk)
            digest.update(b"\n")
        return digest.hexdigest()

    def _reuse_compatible_index(
        self,
        corpus_id: str,
        fingerprint: str,
        destination: Path,
    ) -> str | None:
        """Copy a verified immutable index when its complete input is unchanged."""
        corpus_releases = self.paths.releases / corpus_id
        if not corpus_releases.is_dir():
            return None
        for release in sorted(corpus_releases.iterdir(), reverse=True):
            report_path = release / "metadata" / "index_validation.json"
            source_index = release / "index"
            if not report_path.is_file() or not source_index.is_dir():
                continue
            try:
                metadata = json.loads(report_path.read_text(encoding="utf-8-sig"))
            except (OSError, ValueError):
                continue
            if metadata.get("index_input_fingerprint") != fingerprint:
                continue
            if not validate_release(release).valid:
                continue
            # A valid Release manifest proves that recorded files are intact,
            # but an older or incomplete Release may still lack files that BCC
            # needs at load time.  Reuse only a complete native BCC index.
            required_bcc_files = (
                "IdxUnit.dat",
                "DatWord.dat",
                "Org.dat",
                "SentInfo.dat",
            )
            if any(not (source_index / name).is_file() for name in required_bcc_files):
                continue
            if destination.exists() and any(destination.iterdir()):
                raise ValueError(f"索引复用目标不是空目录：{destination}")
            shutil.copytree(source_index, destination, dirs_exist_ok=True)
            return self.paths.relative(release)
        return None

    def verify(self, corpus_id: str, version: str, *, check_database: bool = True) -> dict[str, Any]:
        release_path = self.paths.releases / corpus_id / version
        file_report = validate_release(release_path).as_dict()
        database_report = None
        if check_database:
            database_report = CorpusRegistry().verify_registered_files(corpus_id, version)
        return {
            "ok": bool(file_report["valid"]) and (database_report is None or bool(database_report.get("valid"))),
            "release": file_report,
            "registry": database_report,
        }

    def _source_path(self, source: str | Path | None) -> Path:
        if source is not None:
            path = Path(source)
            return (path if path.is_absolute() else self.paths.root / path).resolve()
        filename = self.config["index"].get("syntax_tree_file_name", "corpus_index_syntax_tree.txt")
        return (self.pipeline.context.release_root / filename).resolve()

    def _catalog_path(self, catalog: str | Path | None) -> Path | None:
        if catalog is not None:
            path = Path(catalog)
            return (path if path.is_absolute() else self.paths.root / path).resolve()
        candidate = self.pipeline.context.configured_path("index_output") / "index_items.jsonl"
        return candidate if candidate.is_file() else None
