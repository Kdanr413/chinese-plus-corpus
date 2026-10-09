"""Unified publish, status, and structured-corpus export endpoints."""

from __future__ import annotations

import re
import os
import tempfile
import threading
import uuid
import zipfile
from datetime import datetime
from pathlib import Path
from typing import Any

from fastapi import APIRouter, Request
from fastapi.responses import FileResponse, JSONResponse
from pydantic import BaseModel, ConfigDict, Field
from starlette.background import BackgroundTask

from corpus_platform import PlatformCoordinator
from corpus_registry import CorpusRegistry
from corpus_releases import validate_release
from scripts.ui import server as legacy

from .workbench import require_admin


router = APIRouter()


class PublishRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    version: str | None = Field(default=None, max_length=128)
    make_default: bool = True


class PublishState:
    def __init__(self) -> None:
        self.lock = threading.Lock()
        self.running = False
        self.job_id = ""
        self.version = ""
        self.started_at = ""
        self.finished_at = ""
        self.progress = 0
        self.current_step = "等待构建"
        self.error = ""
        self.result: dict[str, Any] | None = None

    def snapshot(self) -> dict[str, Any]:
        with self.lock:
            return {
                "running": self.running,
                "job_id": self.job_id,
                "version": self.version,
                "started_at": self.started_at,
                "finished_at": self.finished_at,
                "progress": self.progress,
                "current_step": self.current_step,
                "error": self.error,
                "result": self.result,
            }

    def begin(self, version: str) -> str:
        with self.lock:
            if self.running:
                raise ValueError("已有索引与发布任务正在运行")
            self.running = True
            self.job_id = uuid.uuid4().hex
            self.version = version
            self.started_at = datetime.now().astimezone().isoformat(timespec="seconds")
            self.finished_at = ""
            self.progress = 5
            self.current_step = "准备 Workspace 与 GB18030 派生语料"
            self.error = ""
            self.result = None
            return self.job_id

    def update(self, progress: int, step: str) -> None:
        with self.lock:
            self.progress = progress
            self.current_step = step

    def finish(self, result: dict[str, Any] | None = None, error: str = "") -> None:
        with self.lock:
            self.running = False
            self.finished_at = datetime.now().astimezone().isoformat(timespec="seconds")
            self.progress = 100 if not error else self.progress
            self.current_step = "构建完成，可以进入查询" if not error else "构建失败"
            self.result = result
            self.error = error


PUBLISH_STATE = PublishState()


def _suggest_version(registry: CorpusRegistry, corpus_key: str) -> tuple[str, str | None]:
    corpus = registry.get_corpus(corpus_key)
    versions = registry.list_versions(corpus["id"])
    parent = None
    if corpus.get("default_version_id"):
        parent_row = next(
            (row for row in versions if row["id"] == corpus["default_version_id"]),
            None,
        )
        parent = str(parent_row["version"]) if parent_row else None
    numeric: list[tuple[int, int, int]] = []
    for row in versions:
        match = re.match(r"^(\d+)\.(\d+)\.(\d+)", str(row["version"]))
        if match:
            numeric.append(tuple(int(value) for value in match.groups()))
    major, minor, patch = max(numeric, default=(0, 0, 0))
    return f"{major}.{minor}.{patch + 1}", parent


def _run_publish(version: str, parent: str | None, make_default: bool) -> None:
    try:
        PUBLISH_STATE.update(15, "生成逐文档 GB18030 语料并构建 BCC 索引")
        coordinator = PlatformCoordinator()
        result = coordinator.publish(
            version=version,
            parent_version=parent,
            register=True,
            make_default=make_default,
        )
        PUBLISH_STATE.update(90, "校验 Corpus Release 与 PostgreSQL 登记")
        corpus_id = str(coordinator.config["project"]["corpus_id"])
        verification = coordinator.verify(corpus_id, version, check_database=True)
        if not verification["ok"]:
            raise RuntimeError("新版本文件与数据库登记不一致")
        result["verification"] = verification
        PUBLISH_STATE.finish(result=result)
    except Exception as exc:
        PUBLISH_STATE.finish(error=str(exc))


@router.get(
    "/api/v1/platform/status",
    tags=["Platform Operations"],
    summary="查看加工、发布和检索全流程状态",
)
def platform_status(request: Request) -> dict[str, Any]:
    require_admin(request)
    coordinator = PlatformCoordinator()
    registry = CorpusRegistry()
    suggested, parent = _suggest_version(registry, "chinese-plus")
    plan = coordinator.publish_plan(
        version=suggested,
        parent_version=parent,
        make_default=True,
    )
    return {
        "workflow": coordinator.status(check_database=True).as_dict(),
        "processing_job": legacy.STATE.snapshot(),
        "publish_job": PUBLISH_STATE.snapshot(),
        "suggested_version": suggested,
        "parent_version": parent,
        "publish_plan": plan,
    }


@router.post(
    "/api/v1/platform/publish",
    status_code=202,
    tags=["Platform Operations"],
    summary="构建索引、创建 Release、登记数据库并启用版本",
)
def start_publish(payload: PublishRequest, request: Request) -> Any:
    try:
        require_admin(request)
        if legacy.STATE.running:
            raise ValueError("语料清洗或结构化流程仍在运行，请完成后再构建索引")
        registry = CorpusRegistry()
        suggested, parent = _suggest_version(registry, "chinese-plus")
        version = (payload.version or suggested).strip()
        coordinator = PlatformCoordinator()
        plan = coordinator.publish_plan(
            version=version,
            parent_version=parent,
            make_default=payload.make_default,
        )
        if not plan["can_execute"]:
            raise ValueError(
                "不能开始构建：请确认句法树文件存在，且目标 Workspace 和 Release 尚不存在"
            )
        job_id = PUBLISH_STATE.begin(version)
        threading.Thread(
            target=_run_publish,
            args=(version, parent, payload.make_default),
            daemon=True,
            name=f"corpus-publish-{job_id[:8]}",
        ).start()
        return {"ok": True, "job_id": job_id, "version": version, "plan": plan}
    except PermissionError as exc:
        return JSONResponse({"error": str(exc)}, status_code=403)
    except ValueError as exc:
        return JSONResponse({"error": str(exc)}, status_code=409)


@router.get(
    "/api/v1/platform/publish-status",
    tags=["Platform Operations"],
    summary="查看索引与发布任务进度",
)
def publish_status(request: Request) -> dict[str, Any]:
    require_admin(request)
    return PUBLISH_STATE.snapshot()


def _remove_file(path: Path) -> None:
    path.unlink(missing_ok=True)


@router.get(
    "/api/v1/exports/structured",
    tags=["Corpus Export"],
    summary="导出一个已发布版本的结构化语料",
)
def export_structured(
    corpus_key: str = "chinese-plus",
    version: str | None = None,
) -> FileResponse:
    registry = CorpusRegistry()
    version_row = registry.get_version(corpus_key, version)
    release = registry.resolve_release_path(corpus_key, str(version_row["version"]))
    integrity = validate_release(release)
    if not integrity.valid:
        raise RuntimeError("Corpus Release 完整性校验失败，不能导出")
    manifest = release / "manifest.json"
    corpus_files = sorted((release / "corpus").rglob("*"))
    handle, temporary_name = tempfile.mkstemp(prefix="corpus-export-", suffix=".zip")
    os.close(handle)
    Path(temporary_name).unlink(missing_ok=True)
    with zipfile.ZipFile(temporary_name, "w", compression=zipfile.ZIP_STORED) as archive:
        archive.write(manifest, "manifest.json")
        for path in corpus_files:
            if path.is_file():
                archive.write(path, path.relative_to(release).as_posix())
    filename = f"{corpus_key}-{version_row['version']}-structured.zip"
    return FileResponse(
        temporary_name,
        media_type="application/zip",
        filename=filename,
        background=BackgroundTask(_remove_file, Path(temporary_name)),
    )
