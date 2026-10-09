"""Immutable Corpus Release management built around ordinary files.

The processing algorithms do not live here.  This module only moves verified
assets between an editable Workspace and an immutable-by-policy Release.
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import uuid
from datetime import datetime
from pathlib import Path
from typing import Any, Iterable, Mapping

from .errors import (
    InvalidReleaseIdentityError,
    ReleaseExistsError,
    ReleaseIntegrityError,
    ReleaseNotFoundError,
    WorkspaceError,
)
from .models import IntegrityReport, ReleaseInfo, WorkspaceInfo


MANIFEST_SCHEMA_VERSION = "1.0"
ASSET_DIRECTORIES = ("corpus", "index", "metadata", "statistics")
CONTROL_FILES = {"workspace.json", "processing_task.json"}


def _now_iso() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


def _validate_identifier(label: str, value: str) -> str:
    value = str(value).strip()
    if (
        not value
        or value in {".", ".."}
        or any(char in value for char in ("/", "\\", "\0", "\r", "\n"))
        or len(value) > 128
    ):
        raise InvalidReleaseIdentityError(f"invalid {label}: {value!r}")
    return value


def _write_json(path: Path, value: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(dict(value), ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )


def _read_json(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ReleaseIntegrityError(f"cannot read {path}: {exc}") from exc
    if not isinstance(value, dict):
        raise ReleaseIntegrityError(f"{path} must contain a JSON object")
    return value


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _release_checksum(files: Iterable[Mapping[str, Any]]) -> str:
    digest = hashlib.sha256()
    for item in sorted(files, key=lambda row: str(row["path"])):
        digest.update(str(item["path"]).encode("utf-8"))
        digest.update(b"\0")
        digest.update(str(item["sha256"]).encode("ascii"))
        digest.update(b"\n")
    return digest.hexdigest()


def _asset_files(root: Path) -> list[Path]:
    files: list[Path] = []
    for directory_name in ASSET_DIRECTORIES:
        directory = root / directory_name
        if not directory.exists():
            continue
        if not directory.is_dir() or directory.is_symlink():
            raise WorkspaceError(f"asset location must be a real directory: {directory}")
        for path in directory.rglob("*"):
            if path.is_symlink():
                raise WorkspaceError(f"symbolic links are not release assets: {path}")
            if path.is_file():
                files.append(path)
    return sorted(files, key=lambda path: path.relative_to(root).as_posix())


def _file_records(root: Path) -> list[dict[str, Any]]:
    records = []
    for path in _asset_files(root):
        relative = path.relative_to(root).as_posix()
        records.append(
            {
                "path": relative,
                "role": relative.split("/", 1)[0],
                "bytes": path.stat().st_size,
                "sha256": _sha256_file(path),
            }
        )
    return records


def _copy_assets(source: Path, destination: Path) -> None:
    for directory_name in ASSET_DIRECTORIES:
        source_directory = source / directory_name
        if source_directory.exists():
            shutil.copytree(source_directory, destination / directory_name)


def create_workspace(
    workspaces_root: str | Path,
    corpus_id: str,
    workspace_id: str,
) -> WorkspaceInfo:
    """Create an empty editable workspace without replacing an existing one."""
    corpus_id = _validate_identifier("corpus_id", corpus_id)
    workspace_id = _validate_identifier("workspace_id", workspace_id)
    destination = Path(workspaces_root).resolve() / corpus_id / workspace_id
    if destination.exists():
        raise WorkspaceError(f"workspace already exists: {destination}")
    destination.mkdir(parents=True)
    for name in ASSET_DIRECTORIES:
        (destination / name).mkdir()
    created_at = _now_iso()
    workspace = {
        "schema_version": MANIFEST_SCHEMA_VERSION,
        "corpus_id": corpus_id,
        "workspace_id": workspace_id,
        "created_at": created_at,
        "status": "editable",
        "base_release": None,
    }
    task = {
        "schema_version": MANIFEST_SCHEMA_VERSION,
        "task_id": workspace_id,
        "task_type": "new_corpus",
        "status": "created",
        "created_at": created_at,
        "base_release": None,
    }
    _write_json(destination / "workspace.json", workspace)
    _write_json(destination / "processing_task.json", task)
    return WorkspaceInfo(destination, corpus_id, workspace_id, None, destination / "processing_task.json")


def create_release(
    workspace_dir: str | Path,
    releases_root: str | Path,
    *,
    corpus_id: str,
    version: str,
    corpus_format_version: str,
    pipeline_version: str,
    search_engine_version: str,
    based_on_release: Mapping[str, str] | None = None,
    metadata: Mapping[str, Any] | None = None,
) -> ReleaseInfo:
    """Freeze one workspace into a new release using copy-verify-rename."""
    corpus_id = _validate_identifier("corpus_id", corpus_id)
    version = _validate_identifier("version", version)
    workspace = Path(workspace_dir).resolve()
    if not workspace.is_dir():
        raise WorkspaceError(f"workspace does not exist: {workspace}")
    source_files = _asset_files(workspace)
    if not any(path.relative_to(workspace).parts[0] == "corpus" for path in source_files):
        raise WorkspaceError("workspace corpus/ must contain at least one structured corpus file")

    releases = Path(releases_root).resolve()
    destination = releases / corpus_id / version
    if destination.exists():
        raise ReleaseExistsError(f"release already exists and will not be overwritten: {destination}")
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = destination.parent / f".{version}.creating-{uuid.uuid4().hex}"
    if temporary.exists():
        raise ReleaseExistsError(f"temporary release path already exists: {temporary}")

    if based_on_release is None:
        workspace_control = workspace / "workspace.json"
        if workspace_control.is_file():
            based_on_release = _read_json(workspace_control).get("base_release")
    normalized_base = None
    if based_on_release:
        normalized_base = {
            "corpus_id": _validate_identifier(
                "base corpus_id", str(based_on_release.get("corpus_id", ""))
            ),
            "version": _validate_identifier(
                "base version", str(based_on_release.get("version", ""))
            ),
        }

    try:
        temporary.mkdir()
        _copy_assets(workspace, temporary)
        files = _file_records(temporary)
        index_files = [item["path"] for item in files if item["role"] == "index"]
        manifest = {
            "schema_version": MANIFEST_SCHEMA_VERSION,
            "corpus_id": corpus_id,
            "version": version,
            "created_at": _now_iso(),
            "corpus_format_version": str(corpus_format_version),
            "pipeline_version": str(pipeline_version),
            "search_engine_version": str(search_engine_version),
            "based_on_release": normalized_base,
            "source_workspace": {
                "workspace_id": workspace.name,
            },
            "index_built": bool(index_files),
            "index_files": index_files,
            "asset_groups": {
                name: sum(item["role"] == name for item in files)
                for name in ASSET_DIRECTORIES
            },
            "files": files,
            "checksum": {
                "algorithm": "sha256",
                "value": _release_checksum(files),
            },
            "metadata": dict(metadata or {}),
        }
        _write_json(temporary / "manifest.json", manifest)
        report = validate_release(temporary)
        if not report.valid:
            raise ReleaseIntegrityError(f"new release failed validation: {report.as_dict()}")
        try:
            os.rename(temporary, destination)
        except FileExistsError as exc:
            raise ReleaseExistsError(
                f"release was created concurrently and will not be overwritten: {destination}"
            ) from exc
    except Exception:
        if temporary.exists():
            shutil.rmtree(temporary)
        raise
    return ReleaseInfo(destination, manifest, validate_release(destination))


def validate_release(release_dir: str | Path) -> IntegrityReport:
    """Check manifest structure, file presence, size, and SHA-256 values."""
    release = Path(release_dir).resolve()
    manifest_path = release / "manifest.json"
    if not release.is_dir() or not manifest_path.is_file():
        return IntegrityReport(release, False, 0, errors=("manifest.json is missing",))
    try:
        manifest = _read_json(manifest_path)
    except ReleaseIntegrityError as exc:
        return IntegrityReport(release, False, 0, errors=(str(exc),))

    required = {
        "schema_version",
        "corpus_id",
        "version",
        "created_at",
        "corpus_format_version",
        "pipeline_version",
        "search_engine_version",
        "files",
        "checksum",
        "index_built",
        "index_files",
        "based_on_release",
    }
    errors = []
    missing_fields = sorted(required - set(manifest))
    if missing_fields:
        errors.append("missing manifest fields: " + ", ".join(missing_fields))
    if manifest.get("schema_version") != MANIFEST_SCHEMA_VERSION:
        errors.append(f"unsupported manifest schema_version: {manifest.get('schema_version')!r}")
    raw_files = manifest.get("files", [])
    if not isinstance(raw_files, list):
        return IntegrityReport(release, False, 0, errors=("manifest files must be a list",))

    expected: dict[str, dict[str, Any]] = {}
    for item in raw_files:
        if not isinstance(item, dict) or not all(key in item for key in ("path", "bytes", "sha256")):
            errors.append("manifest contains an invalid file record")
            continue
        relative = str(item["path"])
        candidate = Path(relative)
        if (
            not relative
            or "\\" in relative
            or candidate.is_absolute()
            or ".." in candidate.parts
            or not candidate.parts
            or candidate.parts[0] not in ASSET_DIRECTORIES
        ):
            errors.append(f"unsafe release file path: {relative}")
            continue
        size = item.get("bytes")
        digest = item.get("sha256")
        if isinstance(size, bool) or not isinstance(size, int) or size < 0:
            errors.append(f"invalid byte count for {relative}")
        if (
            not isinstance(digest, str)
            or len(digest) != 64
            or any(char not in "0123456789abcdef" for char in digest)
        ):
            errors.append(f"invalid sha256 for {relative}")
        if relative in expected:
            errors.append(f"duplicate release file record: {relative}")
            continue
        expected[relative] = item

    actual: dict[str, Path] = {}
    for path in release.rglob("*"):
        if path == manifest_path:
            continue
        relative = path.relative_to(release).as_posix()
        if path.is_symlink():
            errors.append(f"symbolic link is not a valid release asset: {relative}")
        elif path.is_file():
            actual[relative] = path
    missing = sorted(set(expected) - set(actual))
    unexpected = sorted(set(actual) - set(expected))
    modified = []
    for relative in sorted(set(expected) & set(actual)):
        record = expected[relative]
        path = actual[relative]
        expected_role = relative.split("/", 1)[0]
        if record.get("role") != expected_role:
            errors.append(f"incorrect asset role for {relative}")
        valid_size = isinstance(record.get("bytes"), int) and not isinstance(record.get("bytes"), bool)
        valid_digest = isinstance(record.get("sha256"), str)
        if (
            not valid_size
            or not valid_digest
            or path.stat().st_size != record["bytes"]
            or _sha256_file(path) != record["sha256"]
        ):
            modified.append(relative)

    expected_checksum = manifest.get("checksum", {})
    if not isinstance(expected_checksum, dict) or expected_checksum.get("algorithm") != "sha256":
        errors.append("manifest checksum algorithm must be sha256")
    elif expected_checksum.get("value") != _release_checksum(expected.values()):
        errors.append("release checksum does not match the manifest file list")
    has_corpus = any(relative.startswith("corpus/") for relative in expected)
    if not has_corpus:
        errors.append("release contains no structured corpus asset")
    index_files = sorted(
        relative for relative in expected if relative.startswith("index/")
    )
    if bool(manifest.get("index_built")) != bool(index_files):
        errors.append("index_built does not match the index asset list")
    declared_index_files = manifest.get("index_files", [])
    if not isinstance(declared_index_files, list):
        errors.append("index_files must be a list")
    elif sorted(map(str, declared_index_files)) != index_files:
        errors.append("index_files does not match the manifest file list")
    valid = not (missing or unexpected or modified or errors)
    return IntegrityReport(
        release,
        valid,
        len(expected),
        tuple(missing),
        tuple(unexpected),
        tuple(modified),
        tuple(errors),
    )


def read_release(
    releases_root: str | Path,
    corpus_id: str,
    version: str,
    *,
    verify: bool = True,
) -> ReleaseInfo:
    """Read one release and optionally refuse corrupted assets."""
    corpus_id = _validate_identifier("corpus_id", corpus_id)
    version = _validate_identifier("version", version)
    release = Path(releases_root).resolve() / corpus_id / version
    if not release.is_dir():
        raise ReleaseNotFoundError(f"release does not exist: {release}")
    manifest = _read_json(release / "manifest.json")
    report = validate_release(release) if verify else None
    if report is not None and not report.valid:
        raise ReleaseIntegrityError(f"release integrity check failed: {report.as_dict()}")
    return ReleaseInfo(release, manifest, report)


def create_workspace_from_release(
    releases_root: str | Path,
    workspaces_root: str | Path,
    *,
    corpus_id: str,
    version: str,
    workspace_id: str,
) -> WorkspaceInfo:
    """Create an editable reprocessing task from a verified old release."""
    workspace_id = _validate_identifier("workspace_id", workspace_id)
    source = read_release(releases_root, corpus_id, version, verify=True)
    destination = Path(workspaces_root).resolve() / source.corpus_id / workspace_id
    if destination.exists():
        raise WorkspaceError(f"workspace already exists: {destination}")
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = destination.parent / f".{workspace_id}.creating-{uuid.uuid4().hex}"
    base_release = {"corpus_id": source.corpus_id, "version": source.version}
    created_at = _now_iso()
    try:
        temporary.mkdir()
        _copy_assets(source.path, temporary)
        _write_json(
            temporary / "workspace.json",
            {
                "schema_version": MANIFEST_SCHEMA_VERSION,
                "corpus_id": source.corpus_id,
                "workspace_id": workspace_id,
                "created_at": created_at,
                "status": "editable",
                "base_release": base_release,
            },
        )
        task_path = temporary / "processing_task.json"
        _write_json(
            task_path,
            {
                "schema_version": MANIFEST_SCHEMA_VERSION,
                "task_id": workspace_id,
                "task_type": "reprocess_release",
                "status": "created",
                "created_at": created_at,
                "base_release": base_release,
            },
        )
        os.rename(temporary, destination)
    except Exception:
        if temporary.exists():
            shutil.rmtree(temporary)
        raise
    return WorkspaceInfo(
        destination,
        source.corpus_id,
        workspace_id,
        base_release,
        destination / "processing_task.json",
    )
