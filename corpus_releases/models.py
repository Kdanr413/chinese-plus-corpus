"""Data models returned by the Corpus Release public API."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any


@dataclass(frozen=True)
class WorkspaceInfo:
    path: Path
    corpus_id: str
    workspace_id: str
    base_release: dict[str, str] | None
    processing_task_path: Path


@dataclass(frozen=True)
class IntegrityReport:
    release_path: Path
    valid: bool
    checked_files: int
    missing_files: tuple[str, ...] = ()
    unexpected_files: tuple[str, ...] = ()
    modified_files: tuple[str, ...] = ()
    errors: tuple[str, ...] = ()

    def as_dict(self) -> dict[str, Any]:
        return {
            "release_path": str(self.release_path),
            "valid": self.valid,
            "checked_files": self.checked_files,
            "missing_files": list(self.missing_files),
            "unexpected_files": list(self.unexpected_files),
            "modified_files": list(self.modified_files),
            "errors": list(self.errors),
        }


@dataclass(frozen=True)
class ReleaseInfo:
    path: Path
    manifest: dict[str, Any]
    integrity: IntegrityReport | None = None

    @property
    def corpus_id(self) -> str:
        return str(self.manifest["corpus_id"])

    @property
    def version(self) -> str:
        return str(self.manifest["version"])
