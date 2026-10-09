"""Stable public interface for the existing corpus processing workflow."""

from __future__ import annotations

from pathlib import Path

from .errors import (
    PipelineConfigurationError,
    PipelineError,
    PipelineExecutionError,
    PipelineStageError,
)
from .models import DiagnosticCheck, PipelineExecution, PipelineOptions
from .service import CorpusPipeline


def run(
    stage: str,
    *,
    config_path: str | Path | None = None,
    project_root: str | Path | None = None,
    options: PipelineOptions | None = None,
) -> PipelineExecution:
    """Run any supported operation through the stable Python API."""
    return CorpusPipeline(config_path, project_root=project_root).run(stage, options)


def clean(
    *,
    config_path: str | Path | None = None,
    project_root: str | Path | None = None,
    rebuild: bool = True,
) -> PipelineExecution:
    """Run inventory and cleaning with the established output format."""
    return CorpusPipeline(config_path, project_root=project_root).clean(rebuild=rebuild)


def structure(
    *,
    config_path: str | Path | None = None,
    project_root: str | Path | None = None,
    validate_quality: bool = True,
) -> PipelineExecution:
    """Validate and generate the established corpus index and release output."""
    return CorpusPipeline(config_path, project_root=project_root).structure(
        validate_quality=validate_quality
    )


__all__ = [
    "CorpusPipeline",
    "DiagnosticCheck",
    "PipelineConfigurationError",
    "PipelineError",
    "PipelineExecution",
    "PipelineExecutionError",
    "PipelineOptions",
    "PipelineStageError",
    "clean",
    "run",
    "structure",
]
