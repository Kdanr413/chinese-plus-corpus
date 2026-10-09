"""Public input and output models for corpus pipeline calls."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from scripts.workflow.core import StageResult


@dataclass(frozen=True)
class PipelineOptions:
    """Options shared by command-line, web, scheduled and Python callers."""

    rebuild: bool = True
    run_collection: bool = False
    continue_on_failure: bool = False
    term_limit: int = 100
    defer_topic_llm: bool = False
    finalize_reports: bool = True

    def __post_init__(self) -> None:
        if self.term_limit < 1:
            raise ValueError("term_limit must be at least 1")


@dataclass(frozen=True)
class DiagnosticCheck:
    """One environment or input check returned by ``diagnose``."""

    label: str
    path: Path | None = None
    passed: bool = True
    details: str = ""

    def as_dict(self) -> dict[str, Any]:
        return {
            "label": self.label,
            "path": str(self.path) if self.path else None,
            "passed": self.passed,
            "details": self.details,
        }


@dataclass
class PipelineExecution:
    """Structured result returned by a public pipeline call."""

    requested_stage: str
    project_root: Path
    config_path: Path
    stage_results: list[StageResult] = field(default_factory=list)
    report_paths: list[Path] = field(default_factory=list)
    messages: list[str] = field(default_factory=list)

    @property
    def status(self) -> str:
        statuses = {result.status for result in self.stage_results}
        if "failed" in statuses:
            return "failed"
        if "needs_human_review" in statuses:
            return "needs_human_review"
        return "complete"

    @property
    def outputs(self) -> list[Path]:
        return [path for result in self.stage_results for path in result.outputs]

    def as_dict(self) -> dict[str, Any]:
        return {
            "requested_stage": self.requested_stage,
            "status": self.status,
            "project_root": str(self.project_root),
            "config_path": str(self.config_path),
            "reports": [str(path) for path in self.report_paths],
            "outputs": [str(path) for path in self.outputs],
            "messages": list(self.messages),
            "stages": [
                {
                    "code": result.code,
                    "name": result.name,
                    "status": result.status,
                    "summary": result.summary,
                    "metrics": result.metrics,
                    "validations": result.validations,
                    "outputs": [str(path) for path in result.outputs],
                }
                for result in self.stage_results
            ],
        }

