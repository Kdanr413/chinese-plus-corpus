"""Importable facade over the established corpus processing implementation."""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Callable, Iterable

from scripts.workflow.core import StageResult, WorkflowContext, finalize_stage, run_python
from scripts.workflow.stages import STAGES

from .errors import (
    PipelineConfigurationError,
    PipelineExecutionError,
    PipelineStageError,
)
from .models import DiagnosticCheck, PipelineExecution, PipelineOptions


PACKAGE_PROJECT_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_CONFIG_RELATIVE = Path("configs") / "pipeline.json"

# Existing names are kept as public compatibility contracts. ``structure`` is
# the new convenience operation corresponding to the workbench's quality +
# release sequence.
STAGE_CODES: dict[str, tuple[str, ...]] = {
    "planning": ("01",),
    "design": ("02",),
    "rights": ("03",),
    "collect": ("04",),
    "clean": ("05",),
    "linguistic": ("06",),
    "quality": ("08",),
    "release": ("09",),
    "structure": ("08", "09"),
    "syntax": ("09_syntax",),
    "maintenance": ("10",),
}

SPECIAL_STAGES = {
    "topics",
    "topic-only",
    "term-only",
    "term-llm",
    "publish-topics",
    "publish-terms",
}

SUPPORTED_STAGES = tuple([*STAGE_CODES, *sorted(SPECIAL_STAGES)])


class CorpusPipeline:
    """Stable Python interface shared by all current and future callers.

    Existing algorithms remain in ``scripts``. This facade owns configuration,
    dispatch, typed errors and structured results so callers do not need to know
    individual script paths.
    """

    def __init__(
        self,
        config_path: str | Path | None = None,
        *,
        project_root: str | Path | None = None,
        event_handler: Callable[[str], None] | None = None,
    ) -> None:
        self.project_root, self.config_path = self._resolve_locations(
            config_path=config_path,
            project_root=project_root,
        )
        self.event_handler = event_handler
        try:
            self.context = WorkflowContext(self.project_root, self.config_path)
        except (OSError, KeyError, TypeError, ValueError) as exc:
            raise PipelineConfigurationError(
                f"无法读取 Pipeline 配置 {self.config_path}: {exc}"
            ) from exc

    @staticmethod
    def _resolve_locations(
        config_path: str | Path | None,
        project_root: str | Path | None,
    ) -> tuple[Path, Path]:
        if project_root is not None:
            root = Path(project_root).expanduser().resolve()
        elif config_path is not None:
            candidate = Path(config_path).expanduser().resolve()
            root = candidate.parent.parent if candidate.parent.name == "configs" else PACKAGE_PROJECT_ROOT
        else:
            root = PACKAGE_PROJECT_ROOT
        config = Path(config_path).expanduser() if config_path is not None else DEFAULT_CONFIG_RELATIVE
        if not config.is_absolute():
            config = root / config
        config = config.resolve()
        if not root.is_dir():
            raise PipelineConfigurationError(f"项目目录不存在：{root}")
        if not config.is_file():
            raise PipelineConfigurationError(f"Pipeline 配置不存在：{config}")
        return root, config

    def _emit(self, message: str, execution: PipelineExecution | None = None) -> None:
        if execution is not None:
            execution.messages.append(message)
        if self.event_handler is not None:
            self.event_handler(message)

    def plan(self, stage: str) -> tuple[tuple[str, str], ...]:
        """Return a no-side-effect description of the selected operation."""
        self._validate_stage_name(stage)
        if stage in {"topics", "topic-only"}:
            return (("topics", "reference snapshot + BERTopic + mandatory fixed cluster-to-taxonomy matching + taxonomy review workbook"),)
        if stage == "term-only":
            return (("term-only", "require approved taxonomy + candidate extraction + context TF-IDF/K-means + term review workbook"),)
        if stage in SPECIAL_STAGES:
            return ((stage, stage),)
        return tuple((code, STAGES[code].__name__) for code in STAGE_CODES[stage])

    def diagnose(self) -> list[DiagnosticCheck]:
        """Check declared files without changing project data."""
        ctx = self.context
        required = {
            "pipeline config": ctx.config_path,
            "cleaning config": ctx.resolve(ctx.config["cleaning"]["config"]),
            "topic reference taxonomy": ctx.resolve(ctx.config["topics"]["reference_taxonomy"]),
            "BERTopic config": ctx.resolve(ctx.config["topics"]["config"]),
            "term clustering config": ctx.resolve(ctx.config["term_extraction"]["clustering_config"]),
            "coverage targets": ctx.resolve(ctx.config["governance"]["coverage_targets"]),
            "source rights": ctx.resolve(ctx.config["governance"]["source_rights"]),
            "domain lexicon": ctx.resolve(ctx.config["linguistic"]["domain_lexicon"]),
            "terminology": ctx.resolve(ctx.config["linguistic"]["terminology"]),
            "segmentation notes": ctx.resolve(ctx.config["linguistic"]["segmentation_notes"]),
            "Jieba user dictionary": ctx.resolve(ctx.config["linguistic"]["jieba_user_dict"]),
            "vendored Jieba": ctx.project_root / "vendor" / "jieba" / "__init__.py",
            "topic references": ctx.resolve(ctx.config["topics"]["reference_documents"]),
            "term extraction config": ctx.resolve(ctx.config["term_extraction"]["config"]),
            "term few-shot examples": ctx.resolve(ctx.config["term_extraction"]["few_shot_examples"]),
            "term LLM results interface": ctx.resolve(ctx.config["term_extraction"]["llm_results"]),
            "term method registry": ctx.resolve(ctx.config["term_extraction"]["method_registry"]),
            "term stop list": ctx.resolve(ctx.config["linguistic"]["stop_terms"]),
            "general reference frequency": ctx.resolve(ctx.config["linguistic"]["general_reference_frequency"]),
            "manual source directory": ctx.configured_path("manual_sources"),
            "web source directory": ctx.configured_path("web_sources"),
        }
        checks = [
            DiagnosticCheck(label=label, path=path, passed=path.exists())
            for label, path in required.items()
        ]
        checks.extend(
            [
                DiagnosticCheck("Python", details=sys.executable),
                DiagnosticCheck("corpus version", details=ctx.version),
            ]
        )
        return checks

    def clean(self, *, rebuild: bool = True) -> PipelineExecution:
        return self.run("clean", PipelineOptions(rebuild=rebuild))

    def structure(self, *, validate_quality: bool = True) -> PipelineExecution:
        stage = "structure" if validate_quality else "release"
        return self.run(stage)

    def linguistic(self) -> PipelineExecution:
        return self.run("linguistic")

    def syntax(self) -> PipelineExecution:
        return self.run("syntax")

    def run(
        self,
        stage: str,
        options: PipelineOptions | None = None,
    ) -> PipelineExecution:
        """Run one named operation and return a structured result."""
        self._validate_stage_name(stage)
        options = options or PipelineOptions()
        execution = PipelineExecution(stage, self.project_root, self.config_path)
        try:
            if stage in SPECIAL_STAGES:
                self._run_special(stage, options, execution)
            else:
                self._run_stage_codes(STAGE_CODES[stage], options, execution)
        except (PipelineExecutionError, PipelineStageError):
            raise
        except Exception as exc:
            raise PipelineExecutionError(f"Pipeline 阶段 {stage} 执行失败：{exc}") from exc
        return execution

    def _validate_stage_name(self, stage: str) -> None:
        if stage not in STAGE_CODES and stage not in SPECIAL_STAGES:
            allowed = ", ".join(SUPPORTED_STAGES)
            raise ValueError(f"未知 Pipeline 阶段 {stage!r}；可用值：{allowed}")

    def _run_stage_codes(
        self,
        codes: Iterable[str],
        options: PipelineOptions,
        execution: PipelineExecution,
    ) -> None:
        for code in codes:
            function = STAGES[code]
            self._emit(f"=== {code} {function.__name__} ===", execution)
            result = function(
                self.context,
                run_collection=options.run_collection,
                rebuild=options.rebuild,
            )
            report = finalize_stage(self.context, result) if options.finalize_reports else None
            execution.stage_results.append(result)
            if report is not None:
                execution.report_paths.append(report)
            suffix = f"; report={report}" if report is not None else ""
            self._emit(f"status={result.status}{suffix}", execution)
            if result.status == "failed" and not options.continue_on_failure:
                raise PipelineStageError(
                    f"{result.name}未通过质量门禁；Pipeline 已停止。",
                    execution,
                )

    def _run_external(
        self,
        label: str,
        script: Path,
        arguments: list[str],
        execution: PipelineExecution,
    ) -> None:
        self._emit(label, execution)
        run_python(script, arguments, self.project_root)

    def _run_special(
        self,
        stage: str,
        options: PipelineOptions,
        execution: PipelineExecution,
    ) -> None:
        ctx = self.context
        root = self.project_root
        if stage in {"topic-only", "topics"}:
            command = [
                "--input-root", str(ctx.configured_path("corpus_output") / "jsonl"),
                "--reference-taxonomy", str(ctx.resolve(ctx.config["topics"]["reference_taxonomy"])),
                "--config", str(ctx.resolve(ctx.config["topics"]["config"])),
                "--output-root", str(ctx.configured_path("topic_output")),
                "--review-root", str(root / "data" / "review" / "topics"),
            ]
            self._run_external("话题体系识别与审核任务生成", root / "scripts" / "topics" / "taxonomy_workflow.py", command, execution)
            if not options.defer_topic_llm:
                topic_output = ctx.configured_path("topic_output")
                workbench = root / "data" / "review" / "topics" / "taxonomy_review_workbench.csv"
                self._run_external("话题固定体系模型匹配", root / "scripts" / "workflow" / "run_validation_llm.py", [
                    "--mode", "topics", "--tasks", str(topic_output / "topic_mapping_llm_tasks.jsonl"),
                    "--responses", str(topic_output / "topic_mapping_llm_responses.jsonl"),
                    "--limit", "25000", "--require-complete",
                ], execution)
                self._run_external("合并话题模型证据", root / "scripts" / "topics" / "merge_mapping_evidence.py", [
                    "--tasks", str(topic_output / "topic_mapping_llm_tasks.jsonl"),
                    "--responses", str(topic_output / "topic_mapping_llm_responses.jsonl"),
                    "--workbench", str(workbench),
                ], execution)
            execution.stage_results.append(StageResult("topics", "话题识别", "complete", "已运行现有话题识别流程。"))
            return
        if stage == "term-only":
            term_config = ctx.config["term_extraction"]
            curated_taxonomy = ctx.resolve(term_config["requires_curated_taxonomy"])
            if not curated_taxonomy.exists():
                raise PipelineExecutionError("无法执行术语识别：请先完成人工话题体系审核并发布正式 taxonomy.json。")
            term_output = ctx.configured_path("term_output")
            term_output.mkdir(parents=True, exist_ok=True)
            ranked = term_output / "term_candidates.csv"
            self._run_external("术语候选抽取", root / "scripts" / "terms" / "extract.py", [
                "--sections", str(ctx.configured_path("corpus_output") / "jsonl" / "sections.jsonl"),
                "--sentences", str(ctx.configured_path("corpus_output") / "jsonl" / "sentences.jsonl"),
                "--config", str(ctx.resolve(term_config["config"])), "--taxonomy", str(curated_taxonomy),
                "--domain-lexicon", str(ctx.resolve(ctx.config["linguistic"]["domain_lexicon"])),
                "--terminology", str(ctx.resolve(ctx.config["linguistic"]["terminology"])),
                "--stop-terms", str(ctx.resolve(ctx.config["linguistic"]["stop_terms"])),
                "--reference-frequency", str(ctx.resolve(ctx.config["linguistic"]["general_reference_frequency"])),
                "--few-shot-examples", str(ctx.resolve(term_config["few_shot_examples"])),
                "--llm-results", str(ctx.resolve(term_config["llm_results"])),
                "--llm-tasks", str(term_output / "sentence_term_llm_tasks.jsonl"), "--output", str(ranked),
            ], execution)
            self._run_external("术语上下文聚类", root / "scripts" / "terms" / "cluster_terms.py", [
                "--candidates", str(ranked), "--sentences", str(ctx.configured_path("corpus_output") / "jsonl" / "sentences.jsonl"),
                "--sections", str(ctx.configured_path("corpus_output") / "jsonl" / "sections.jsonl"),
                "--taxonomy", str(curated_taxonomy), "--config", str(ctx.resolve(term_config["clustering_config"])),
                "--output-root", str(term_output), "--review-root", str(root / "data" / "review" / "terms"),
            ], execution)
            execution.stage_results.append(StageResult("terms", "术语识别", "complete", "已运行现有术语识别流程。"))
            return
        if stage == "publish-topics":
            self._run_external("发布正式话题体系", root / "scripts" / "topics" / "publish_taxonomy.py", [
                "--reference", str(ctx.resolve(ctx.config["topics"]["reference_taxonomy"])),
                "--workbench", str(root / "data" / "review" / "topics" / "taxonomy_review_workbench.csv"),
                "--topic-summary", str(ctx.configured_path("topic_output") / "bertopic_topic_summary.json"),
                "--output-root", str(root / "data" / "curated" / "topics"),
            ], execution)
        elif stage == "publish-terms":
            self._run_external("发布正式术语", root / "scripts" / "terms" / "publish_terms.py", [
                "--workbench", str(root / "data" / "review" / "terms" / "term_review_workbench.csv"),
                "--taxonomy", str(root / "data" / "curated" / "topics" / "taxonomy.json"),
                "--output-root", str(root / "data" / "curated" / "terms"),
            ], execution)
        elif stage == "term-llm":
            term_config = ctx.config["term_extraction"]
            self._run_external("运行术语模型任务", root / "scripts" / "terms" / "run_llm.py", [
                "--tasks", str(ctx.configured_path("term_output") / "sentence_term_llm_tasks.jsonl"),
                "--responses", str(ctx.resolve(term_config["llm_responses"])),
                "--results", str(ctx.resolve(term_config["llm_results"])),
                "--limit", str(options.term_limit),
            ], execution)
        execution.stage_results.append(StageResult(stage, stage, "complete", f"已运行现有 {stage} 流程。"))
