"""Portable project and data paths shared by the operations layer."""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Mapping


PROJECT_ROOT_ENV = "CORPUS_PROJECT_ROOT"
PACKAGE_PROJECT_ROOT = Path(__file__).resolve().parents[1]


def resolve_project_root(
    explicit: str | Path | None = None,
    environment: Mapping[str, str] | None = None,
) -> Path:
    """Resolve the repository without depending on its Windows drive letter."""
    values = os.environ if environment is None else environment
    raw = explicit or values.get(PROJECT_ROOT_ENV) or PACKAGE_PROJECT_ROOT
    root = Path(raw).expanduser().resolve()
    if not (root / "configs" / "pipeline.json").is_file():
        raise ValueError(f"不是有效的语料库项目根目录：{root}")
    return root


@dataclass(frozen=True)
class ProjectPaths:
    root: Path
    config: Path
    data: Path
    workspaces: Path
    releases: Path

    @classmethod
    def discover(
        cls,
        project_root: str | Path | None = None,
        environment: Mapping[str, str] | None = None,
    ) -> "ProjectPaths":
        root = resolve_project_root(project_root, environment)
        config = root / "configs" / "pipeline.json"
        value = json.loads(config.read_text(encoding="utf-8-sig"))
        configured = value.get("paths", {})
        data = root / "data"
        return cls(
            root=root,
            config=config,
            data=data,
            workspaces=(root / configured.get("workspaces_root", "data/workspaces")).resolve(),
            releases=(root / configured.get("releases_root", "data/releases")).resolve(),
        )

    def relative(self, path: str | Path) -> str:
        target = Path(path).resolve()
        try:
            return target.relative_to(self.root).as_posix()
        except ValueError:
            return str(target)
