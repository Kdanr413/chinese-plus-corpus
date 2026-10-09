"""Typed errors exposed by the public corpus pipeline API."""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .models import PipelineExecution


class PipelineError(RuntimeError):
    """Base class for errors raised through the public pipeline interface."""


class PipelineConfigurationError(PipelineError):
    """The project root or pipeline configuration cannot be loaded."""


class PipelineExecutionError(PipelineError):
    """A pipeline implementation failed before returning a stage result."""


class PipelineStageError(PipelineError):
    """A stage completed its checks but reported a failed status."""

    def __init__(self, message: str, execution: "PipelineExecution") -> None:
        super().__init__(message)
        self.execution = execution

