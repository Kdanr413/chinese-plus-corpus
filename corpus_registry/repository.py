"""CRUD and transactional Corpus Release registration for PostgreSQL."""

from __future__ import annotations

import hashlib
import json
import mimetypes
import uuid
from pathlib import Path
from typing import Any, Iterable, Mapping

from .config import DatabaseSettings, corpus_data_root
from .db import connect
from .errors import (
    RegisteredFileIntegrityError,
    RegistryConflictError,
    RegistryNotFoundError,
)


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _combined_checksum(files: Iterable[Mapping[str, Any]]) -> str:
    digest = hashlib.sha256()
    for item in sorted(files, key=lambda row: str(row["relative_path"])):
        digest.update(str(item["relative_path"]).encode("utf-8"))
        digest.update(b"\0")
        digest.update(str(item["checksum_sha256"]).encode("ascii"))
        digest.update(b"\n")
    return digest.hexdigest()


def _encoding(path: Path) -> str | None:
    if path.suffix.lower() not in {".txt", ".csv", ".json", ".jsonl", ".md"}:
        return None
    data = path.read_bytes()
    for name in ("utf-8-sig", "gb18030"):
        try:
            # Release registration already reads every file to calculate its
            # checksum.  Decode the complete text asset as well: a fixed-size
            # sample can end between GB18030 bytes and report a false failure.
            data.decode(name)
            return name
        except UnicodeDecodeError:
            continue
    return None


def _role(relative: str) -> str:
    first = relative.split("/", 1)[0]
    if first in {"corpus", "index", "metadata", "statistics"}:
        return first
    name = Path(relative).name.lower()
    if name.startswith("corpus_index") and name.endswith(".txt"):
        return "corpus"
    if "profile" in name or "manifest" in name or "snapshot" in name or name.endswith(".md"):
        return "metadata"
    if name.endswith(".csv"):
        return "statistics"
    return "legacy"


def inspect_release_files(release_path: str | Path) -> tuple[list[dict[str, Any]], str]:
    """Read formal or legacy release assets without changing them."""
    release = Path(release_path).resolve()
    if not release.is_dir():
        raise RegistryNotFoundError(f"release path does not exist: {release}")
    manifest_path = release / "manifest.json"
    formal: dict[str, Any] | None = None
    if manifest_path.is_file():
        from corpus_releases import validate_release

        report = validate_release(release)
        if not report.valid:
            raise RegisteredFileIntegrityError(
                f"formal release failed its manifest check: {report.as_dict()}"
            )
        formal = json.loads(manifest_path.read_text(encoding="utf-8-sig"))

    files: list[dict[str, Any]] = []
    candidates = (
        [release / str(item["path"]) for item in formal["files"]]
        if formal
        else sorted(path for path in release.rglob("*") if path.is_file())
    )
    for path in candidates:
        relative = path.relative_to(release).as_posix()
        files.append(
            {
                "relative_path": relative,
                "role": _role(relative),
                "byte_size": path.stat().st_size,
                "checksum_sha256": _sha256(path),
                "media_type": mimetypes.guess_type(path.name)[0] or "application/octet-stream",
                "encoding": _encoding(path),
            }
        )
    if not files:
        raise RegisteredFileIntegrityError("release contains no files")
    checksum = (
        str(formal["checksum"]["value"])
        if formal
        else _combined_checksum(files)
    )
    return files, checksum


def _conflict(exc: Exception) -> bool:
    return getattr(exc, "sqlstate", None) == "23505"


class CorpusRegistry:
    def __init__(
        self,
        settings: DatabaseSettings | None = None,
        *,
        data_root: str | Path | None = None,
    ) -> None:
        self.settings = settings or DatabaseSettings.from_environment()
        self.data_root = corpus_data_root(data_root)

    def _relative_asset_path(self, path: str | Path, label: str) -> str:
        resolved = Path(path).expanduser().resolve()
        try:
            return resolved.relative_to(self.data_root).as_posix()
        except ValueError as exc:
            raise RegistryConflictError(
                f"{label} must be inside CORPUS_DATA_ROOT: {resolved}"
            ) from exc

    def _resolve_asset_path(self, stored_path: str) -> Path:
        relative = Path(stored_path)
        if relative.is_absolute() or ".." in relative.parts:
            raise RegisteredFileIntegrityError(f"unsafe registered release path: {stored_path}")
        resolved = (self.data_root / relative).resolve()
        try:
            resolved.relative_to(self.data_root)
        except ValueError as exc:
            raise RegisteredFileIntegrityError(
                f"registered release path escapes CORPUS_DATA_ROOT: {stored_path}"
            ) from exc
        return resolved

    def create_corpus(self, corpus_key: str, name: str, description: str = "") -> dict[str, Any]:
        try:
            with connect(self.settings) as connection, connection.transaction():
                return connection.execute(
                    """INSERT INTO corpora(id, corpus_key, name, description)
                       VALUES (%s, %s, %s, %s) RETURNING *""",
                    (uuid.uuid4(), corpus_key, name, description),
                ).fetchone()
        except Exception as exc:
            if _conflict(exc):
                raise RegistryConflictError(f"corpus already exists: {corpus_key}") from exc
            raise

    def list_corpora(self) -> list[dict[str, Any]]:
        """Return every registered corpus in a stable display order."""
        with connect(self.settings) as connection:
            return list(
                connection.execute(
                    """SELECT * FROM corpora
                       ORDER BY created_at ASC, corpus_key ASC"""
                ).fetchall()
            )

    def get_corpus(self, corpus_key: str) -> dict[str, Any]:
        with connect(self.settings) as connection:
            row = connection.execute(
                "SELECT * FROM corpora WHERE corpus_key = %s", (corpus_key,)
            ).fetchone()
        if not row:
            raise RegistryNotFoundError(f"corpus not found: {corpus_key}")
        return row

    def update_corpus(
        self,
        corpus_key: str,
        *,
        name: str,
        description: str,
        status: str,
    ) -> dict[str, Any]:
        with connect(self.settings) as connection, connection.transaction():
            row = connection.execute(
                """UPDATE corpora SET name=%s, description=%s, status=%s
                   WHERE corpus_key=%s RETURNING *""",
                (name, description, status, corpus_key),
            ).fetchone()
        if not row:
            raise RegistryNotFoundError(f"corpus not found: {corpus_key}")
        return row

    def delete_corpus(self, corpus_key: str) -> None:
        """Delete only an empty registry entry; version foreign keys prevent asset loss."""
        with connect(self.settings) as connection, connection.transaction():
            row = connection.execute(
                "DELETE FROM corpora WHERE corpus_key=%s RETURNING id", (corpus_key,)
            ).fetchone()
        if not row:
            raise RegistryNotFoundError(f"corpus not found: {corpus_key}")

    def register_release(
        self,
        *,
        corpus_key: str,
        version: str,
        release_path: str | Path,
        corpus_format_version: str,
        pipeline_version: str,
        search_engine_version: str,
        status: str = "ready",
        searchable: bool = False,
        parent_version: str | None = None,
        make_default: bool = False,
    ) -> dict[str, Any]:
        files, checksum = inspect_release_files(release_path)
        stored_release_path = self._relative_asset_path(release_path, "release_path")
        version_id = uuid.uuid4()
        try:
            with connect(self.settings) as connection, connection.transaction():
                corpus = connection.execute(
                    "SELECT id FROM corpora WHERE corpus_key=%s FOR UPDATE", (corpus_key,)
                ).fetchone()
                if not corpus:
                    raise RegistryNotFoundError(f"corpus not found: {corpus_key}")
                parent_id = None
                if parent_version:
                    parent = connection.execute(
                        "SELECT id FROM corpus_versions WHERE corpus_id=%s AND version=%s",
                        (corpus["id"], parent_version),
                    ).fetchone()
                    if not parent:
                        raise RegistryNotFoundError(f"parent version not found: {parent_version}")
                    parent_id = parent["id"]
                row = connection.execute(
                    """INSERT INTO corpus_versions(
                           id, corpus_id, version, status, searchable, parent_version_id,
                           release_path, corpus_format_version, pipeline_version,
                           search_engine_version, checksum_sha256)
                       VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s) RETURNING *""",
                    (
                        version_id, corpus["id"], version, status, searchable, parent_id,
                        stored_release_path, corpus_format_version, pipeline_version,
                        search_engine_version, checksum,
                    ),
                ).fetchone()
                for item in files:
                    connection.execute(
                        """INSERT INTO corpus_files(
                               id, corpus_version_id, relative_path, role, byte_size,
                               checksum_sha256, media_type, encoding)
                           VALUES (%s,%s,%s,%s,%s,%s,%s,%s)""",
                        (
                            uuid.uuid4(), version_id, item["relative_path"], item["role"],
                            item["byte_size"], item["checksum_sha256"], item["media_type"],
                            item["encoding"],
                        ),
                    )
                if make_default:
                    if not searchable:
                        raise RegistryConflictError("a default version must be searchable")
                    connection.execute(
                        "UPDATE corpora SET default_version_id=%s WHERE id=%s",
                        (version_id, corpus["id"]),
                    )
                return row
        except Exception as exc:
            if _conflict(exc):
                raise RegistryConflictError(
                    f"release already registered: {corpus_key}/{version}"
                ) from exc
            raise

    def get_version(self, corpus_key: str, version: str | None = None) -> dict[str, Any]:
        with connect(self.settings) as connection:
            if version is None:
                row = connection.execute(
                    """SELECT v.* FROM corpora c JOIN corpus_versions v
                       ON v.id=c.default_version_id WHERE c.corpus_key=%s""",
                    (corpus_key,),
                ).fetchone()
            else:
                row = connection.execute(
                    """SELECT v.* FROM corpora c JOIN corpus_versions v ON v.corpus_id=c.id
                       WHERE c.corpus_key=%s AND v.version=%s""",
                    (corpus_key, version),
                ).fetchone()
        if not row:
            raise RegistryNotFoundError(f"version not found: {corpus_key}/{version or 'default'}")
        return row

    def list_versions(self, corpus_id: uuid.UUID) -> list[dict[str, Any]]:
        """Return versions for one database corpus UUID, newest first."""
        with connect(self.settings) as connection:
            corpus = connection.execute(
                "SELECT id FROM corpora WHERE id=%s", (corpus_id,)
            ).fetchone()
            if not corpus:
                raise RegistryNotFoundError(f"corpus not found: {corpus_id}")
            return list(
                connection.execute(
                    """SELECT * FROM corpus_versions
                       WHERE corpus_id=%s
                       ORDER BY created_at DESC, version DESC""",
                    (corpus_id,),
                ).fetchall()
            )

    def list_files(self, corpus_key: str, version: str) -> list[dict[str, Any]]:
        version_row = self.get_version(corpus_key, version)
        with connect(self.settings) as connection:
            return list(
                connection.execute(
                    "SELECT * FROM corpus_files WHERE corpus_version_id=%s ORDER BY relative_path",
                    (version_row["id"],),
                ).fetchall()
            )

    def resolve_release_path(self, corpus_key: str, version: str) -> Path:
        """Resolve a registered relative Release path inside ``data_root``.

        Callers such as the API must use this boundary instead of joining a
        database value to a filesystem path themselves.
        """
        version_row = self.get_version(corpus_key, version)
        return self._resolve_asset_path(str(version_row["release_path"]))

    def update_version_status(
        self,
        corpus_key: str,
        version: str,
        *,
        status: str,
        searchable: bool,
    ) -> dict[str, Any]:
        current = self.get_version(corpus_key, version)
        with connect(self.settings) as connection, connection.transaction():
            row = connection.execute(
                """UPDATE corpus_versions SET status=%s, searchable=%s
                   WHERE id=%s RETURNING *""",
                (status, searchable, current["id"]),
            ).fetchone()
        return row

    def set_default_version(self, corpus_key: str, version: str) -> dict[str, Any]:
        corpus = self.get_corpus(corpus_key)
        selected = self.get_version(corpus_key, version)
        if not selected["searchable"]:
            raise RegistryConflictError("default version must be searchable")
        with connect(self.settings) as connection, connection.transaction():
            return connection.execute(
                "UPDATE corpora SET default_version_id=%s WHERE id=%s RETURNING *",
                (selected["id"], corpus["id"]),
            ).fetchone()

    def verify_registered_files(self, corpus_key: str, version: str) -> dict[str, Any]:
        version_row = self.get_version(corpus_key, version)
        root = self._resolve_asset_path(str(version_row["release_path"]))
        files = self.list_files(corpus_key, version)
        missing, modified = [], []
        for item in files:
            path = root / item["relative_path"]
            if not path.is_file():
                missing.append(item["relative_path"])
            elif path.stat().st_size != item["byte_size"] or _sha256(path) != item["checksum_sha256"]:
                modified.append(item["relative_path"])
        return {
            "valid": not missing and not modified,
            "release_path": str(root),
            "registered_files": len(files),
            "missing_files": missing,
            "modified_files": modified,
        }

    def create_processing_job(
        self,
        corpus_key: str,
        workspace_path: str | Path,
        job_type: str,
        *,
        parent_version: str | None = None,
        parameters: Mapping[str, Any] | None = None,
    ) -> dict[str, Any]:
        stored_workspace_path = self._relative_asset_path(workspace_path, "workspace_path")
        with connect(self.settings) as connection, connection.transaction():
            corpus = connection.execute(
                "SELECT id FROM corpora WHERE corpus_key=%s", (corpus_key,)
            ).fetchone()
            if not corpus:
                raise RegistryNotFoundError(f"corpus not found: {corpus_key}")
            parent_id = None
            if parent_version:
                parent = connection.execute(
                    "SELECT id FROM corpus_versions WHERE corpus_id=%s AND version=%s",
                    (corpus["id"], parent_version),
                ).fetchone()
                if not parent:
                    raise RegistryNotFoundError(f"parent version not found: {parent_version}")
                parent_id = parent["id"]
            return connection.execute(
                """INSERT INTO processing_jobs(
                       id, corpus_id, parent_version_id, workspace_path, job_type, status, parameters)
                   VALUES (%s,%s,%s,%s,%s,'created',%s::jsonb) RETURNING *""",
                (
                    uuid.uuid4(), corpus["id"], parent_id, stored_workspace_path,
                    job_type, json.dumps(dict(parameters or {}), ensure_ascii=False),
                ),
            ).fetchone()

    def update_processing_job(self, job_id: uuid.UUID, status: str, error_message: str = "") -> dict[str, Any]:
        with connect(self.settings) as connection, connection.transaction():
            row = connection.execute(
                """UPDATE processing_jobs SET status=%s, error_message=%s,
                       started_at=CASE WHEN %s='running' THEN COALESCE(started_at, CURRENT_TIMESTAMP) ELSE started_at END,
                       completed_at=CASE WHEN %s IN ('succeeded','failed','cancelled') THEN CURRENT_TIMESTAMP ELSE completed_at END
                   WHERE id=%s RETURNING *""",
                (status, error_message, status, status, job_id),
            ).fetchone()
        if not row:
            raise RegistryNotFoundError(f"processing job not found: {job_id}")
        return row

    def log_query(
        self,
        corpus_key: str,
        version: str,
        query_text: str,
        command: str,
        *,
        scope_ranges: list[list[int]] | None = None,
        result_count: int | None = None,
        duration_ms: int | None = None,
        status: str = "succeeded",
        error_code: str = "",
    ) -> dict[str, Any]:
        corpus = self.get_corpus(corpus_key)
        version_row = self.get_version(corpus_key, version)
        with connect(self.settings) as connection, connection.transaction():
            return connection.execute(
                """INSERT INTO query_logs(
                       id, corpus_id, corpus_version_id, query_text, command, scope_ranges,
                       result_count, duration_ms, status, error_code)
                   VALUES (%s,%s,%s,%s,%s,%s::jsonb,%s,%s,%s,%s) RETURNING *""",
                (
                    uuid.uuid4(), corpus["id"], version_row["id"], query_text, command,
                    json.dumps(scope_ranges or []), result_count, duration_ms, status, error_code,
                ),
            ).fetchone()
