"""Versioned prompt loading and checksum snapshots."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from .errors import PromptConfigurationError


class PromptTemplate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    prompt_id: str = Field(min_length=1)
    prompt_version: str = Field(min_length=1)
    name: str = Field(min_length=1)
    system_prompt: str = Field(min_length=1)
    task_instruction: str = Field(min_length=1)
    input_schema: dict[str, Any]
    output_schema: dict[str, Any]
    changelog: str


class PromptSnapshot(BaseModel):
    model_config = ConfigDict(extra="forbid")

    path: str
    checksum_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    template: PromptTemplate


def load_prompt(path: str | Path, *, project_root: str | Path | None = None) -> PromptSnapshot:
    source = Path(path)
    if not source.is_absolute() and project_root is not None:
        source = Path(project_root) / source
    try:
        raw = source.read_bytes()
        template = PromptTemplate.model_validate(json.loads(raw.decode("utf-8-sig")))
    except (OSError, UnicodeError, ValueError) as exc:
        raise PromptConfigurationError(f"invalid prompt {source}: {exc}") from exc
    display_path = str(source)
    if project_root is not None:
        try:
            display_path = source.resolve().relative_to(Path(project_root).resolve()).as_posix()
        except ValueError:
            pass
    return PromptSnapshot(
        path=display_path,
        checksum_sha256=hashlib.sha256(raw).hexdigest(),
        template=template,
    )
