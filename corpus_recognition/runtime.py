"""End-to-end recognition coordinator for CLI/API/workbench reuse."""

from __future__ import annotations

import hashlib
import csv
import json
import os
import re
import shutil
import time
import uuid
from dataclasses import asdict, dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, Iterable, Mapping, Sequence

from corpus_registry import CorpusRegistry

from .annotations import annotation_status
from .config import RecognitionConfig, load_recognition_config
from .decision import DecisionEngine
from .documents import load_documents_jsonl
from .errors import RecognitionWorkflowCancelled
from .units import build_recognition_units
from .models import (
    AggregationStrategy,
    CandidateStatus,
    CrossValidationRequest,
    Document,
    FinalDecision,
    ResourceDecision,
    TermCandidate,
    TermValidationRequest,
    TopicCandidate,
    TopicValidationRequest,
    ValidatorDecision,
    ValidatorResult,
)
from .prompts import PromptSnapshot, load_prompt
from .providers import (
    BuiltinRuleValidatorProvider,
    OpenAICompatibleValidatorProvider,
    ResolvedValidatorSettings,
    resolve_validator_settings,
)
from .repository import RecognitionRepository
from .term import EvaluatedCorpusTermExtractor, LegacyStatisticalTermExtractor
from .term_intersection import EvaluatedBaselineTermProvider, SentenceTermIntersectionProvider
from .topic import (
    ContentUnitTopicIdentifier,
    normalize_topic_table,
)
from .validation import CrossValidationService, TermValidationService, TopicValidationService


PROJECT_ROOT = Path(__file__).resolve().parents[1]


def _json_default(value: Any) -> Any:
    if hasattr(value, "model_dump"):
        return value.model_dump(mode="json")
    return str(value)


def _write_jsonl(path: Path, rows: Iterable[Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="\n") as handle:
        for row in rows:
            handle.write(json.dumps(row, ensure_ascii=False, default=_json_default) + "\n")


def _read_jsonl(path: Path) -> list[dict[str, Any]]:
    if not path.is_file():
        return []
    rows: list[dict[str, Any]] = []
    with path.open("r", encoding="utf-8-sig") as handle:
        for line in handle:
            if line.strip():
                value = json.loads(line)
                if isinstance(value, dict):
                    rows.append(value)
    return rows


def _write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2, default=_json_default) + "\n", encoding="utf-8")


def _write_csv(path: Path, fieldnames: Sequence[str], rows: Iterable[Mapping[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(fieldnames), extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)


def _checksum(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class RuntimeValidatorModel:
    """One workbench-supplied validator; credentials remain memory-only."""

    id: str
    model: str
    base_url: str
    api_key: str
    model_version: str = ""
    weight: float = 1.0


@dataclass(frozen=True)
class RecognitionRunOptions:
    corpus_key: str = "chinese-plus"
    documents_path: str = "data/processed/corpus/jsonl/documents.jsonl"
    topic_table_path: str | None = "data/curated/topics/taxonomy.json"
    topic_mode: str = "auto"
    strategy: AggregationStrategy = AggregationStrategy.MAJORITY
    machine_validation: bool = True
    human_validation: bool = True
    required_annotators: int = 1
    use_external_model: bool = False
    include_builtin_rules: bool = True
    min_validators: int = 1
    cross_validation_enabled: bool = True
    validator_models: tuple[RuntimeValidatorModel, ...] = ()
    model_name: str = ""
    model_version: str = ""
    base_url: str = ""
    api_key: str = ""

    def __post_init__(self) -> None:
        if self.required_annotators < 1 or self.required_annotators > 50:
            raise ValueError("required_annotators must be between 1 and 50")
        object.__setattr__(
            self,
            "validator_models",
            tuple(
                row if isinstance(row, RuntimeValidatorModel) else RuntimeValidatorModel(**row)
                for row in self.validator_models
            ),
        )
        if not self.machine_validation and not self.human_validation:
            raise ValueError("机器检验和人工检验至少启用一种")
        if self.use_external_model and not self.machine_validation:
            raise ValueError("启用外部模型时必须同时启用机器检验")
        if self.min_validators < 1:
            raise ValueError("最少有效验证器数量必须大于等于 1")
        if self.use_external_model and not self.validator_models and not self.model_name:
            raise ValueError("启用外部模型时至少配置一个模型")
        ids = [row.id for row in self.validator_models]
        if len(ids) != len(set(ids)):
            raise ValueError("模型配置 ID 不得重复")


class RecognitionCoordinator:
    pipeline_version = "recognition-workflow/1.0"

    def __init__(
        self,
        registry: CorpusRegistry | None = None,
        repository: RecognitionRepository | None = None,
        config: RecognitionConfig | None = None,
        project_root: Path = PROJECT_ROOT,
    ) -> None:
        self.registry = registry or CorpusRegistry()
        self.repository = repository or RecognitionRepository()
        self.config = config or load_recognition_config()
        self.project_root = project_root.resolve()
        self.decision_engine = DecisionEngine()
        self.prompts = {
            "term": self._prompt(self.config.term_validation.prompt),
            "topic": self._prompt(self.config.topic_validation.prompt),
            "cross": self._prompt(self.config.cross_validation.prompt),
            "term_sentence": self._prompt(
                self.config.term_model_extraction.prompt
            ),
        }

    def _prompt(self, value: str) -> PromptSnapshot:
        return load_prompt(self.project_root / value, project_root=self.project_root)

    def _project_file(self, relative: str | None, *, required: bool = True) -> Path | None:
        if not relative:
            return None
        path = (self.project_root / relative).resolve()
        path.relative_to(self.project_root)
        if required and not path.is_file():
            raise FileNotFoundError(path)
        return path

    def _providers(self, options: RecognitionRunOptions, kind: str) -> list[object]:
        if not options.machine_validation:
            return [_HumanReviewGateProvider(self.prompts[kind])]
        baseline = BuiltinRuleValidatorProvider(
            self.prompts["term"], self.prompts["topic"], self.prompts["cross"]
        )
        providers: list[object] = [baseline] if options.include_builtin_rules else []
        if options.use_external_model and not options.validator_models and not options.model_name:
            for config in self.config.validation.models:
                if not config.enabled:
                    continue
                try:
                    settings = resolve_validator_settings(config)
                    providers.append(OpenAICompatibleValidatorProvider(settings, self.prompts[kind]))
                except Exception as exc:
                    providers.append(_UnavailableConfiguredProvider(config.id, str(exc)))
        for row in options.validator_models:
            if not all((row.id.strip(), row.model.strip(), row.base_url.strip(), row.api_key.strip())):
                raise ValueError("每个外部模型都必须填写名称、模型 ID、API 地址和 API Key")
            settings = ResolvedValidatorSettings(
                validator_id=row.id.strip(), provider="openai_compatible",
                model=row.model.strip(), model_version=(row.model_version or row.model).strip(),
                base_url=row.base_url.strip(), api_key=row.api_key.strip(),
            )
            providers.append(OpenAICompatibleValidatorProvider(settings, self.prompts[kind]))
        if options.use_external_model and options.model_name:
            if not all((options.model_name, options.base_url, options.api_key)):
                raise ValueError("启用外部模型时必须填写模型名称、API 地址和 API Key")
            settings = ResolvedValidatorSettings(
                validator_id="workbench_external_model",
                provider="openai_compatible",
                model=options.model_name,
                model_version=options.model_version or options.model_name,
                base_url=options.base_url,
                api_key=options.api_key,
            )
            providers.append(OpenAICompatibleValidatorProvider(settings, self.prompts[kind]))
        if not providers:
            raise ValueError("机器检验没有可执行的规则或模型")
        return providers

    @staticmethod
    def _apply_review_policy(
        decision: ResourceDecision,
        options: RecognitionRunOptions,
    ) -> ResourceDecision:
        if options.human_validation:
            return decision.model_copy(update={
                "final_decision": FinalDecision.MANUAL_REVIEW,
                "lifecycle_status": CandidateStatus.VALIDATED,
                "decision_rule": f"human_validation_required AFTER ({decision.decision_rule})",
                "reason": f"已保存机器判断；本次配置要求人工确认。{decision.reason}",
            })
        if decision.final_decision == FinalDecision.MANUAL_REVIEW:
            return decision.model_copy(update={
                "final_decision": FinalDecision.PENDING,
                "lifecycle_status": CandidateStatus.CANDIDATE,
                "decision_rule": "machine_conflict_without_human_reviewer",
                "reason": "机器检验存在分歧，但本次未启用人工检验，因此保持待处理状态",
            })
        return decision

    def _weights(self) -> dict[str, float]:
        weights = {"builtin_rule_baseline": 1.0, "workbench_external_model": 1.0}
        weights.update({row.id: row.weight for row in self.config.validation.models if row.enabled})
        return weights

    def _runtime_weights(self, options: RecognitionRunOptions) -> dict[str, float]:
        weights = self._weights()
        weights.update({row.id: row.weight for row in options.validator_models})
        return weights

    def _term_providers(
        self,
        options: RecognitionRunOptions,
        documents: Sequence[Document],
        notify: Callable[[str, int, str], None],
        checkpoint: Callable[[], None],
    ) -> list[object]:
        """Return the evaluated baseline or sentence-model intersection providers."""
        if not options.use_external_model:
            return [EvaluatedBaselineTermProvider(self.prompts["term_sentence"])]
        settings: list[ResolvedValidatorSettings] = []
        for row in options.validator_models:
            if not all((row.id.strip(), row.model.strip(), row.base_url.strip(), row.api_key.strip())):
                raise ValueError("每个外部模型都必须填写名称、模型 ID、API 地址和 API Key")
            settings.append(ResolvedValidatorSettings(
                validator_id=row.id.strip(), provider="openai_compatible",
                model=row.model.strip(), model_version=(row.model_version or row.model).strip(),
                base_url=row.base_url.strip(), api_key=row.api_key.strip(), weight=row.weight,
            ))
        if options.model_name:
            settings.append(ResolvedValidatorSettings(
                validator_id="workbench_external_model", provider="openai_compatible",
                model=options.model_name, model_version=options.model_version or options.model_name,
                base_url=options.base_url, api_key=options.api_key,
            ))
        if not settings:
            raise ValueError("启用大模型术语交集时至少需要一个可用模型")
        providers = []
        total_calls = max(len(settings) * len(documents), 1)
        completed_before = 0
        for setting in settings:
            offset = completed_before
            providers.append(SentenceTermIntersectionProvider(
                setting, self.prompts["term_sentence"], documents,
                checkpoint_callback=checkpoint,
                progress_callback=lambda completed, total, model, offset=offset: notify(
                    "term",
                    52 + min(12, round((offset + completed) / total_calls * 12)),
                    f"{model} 正在逐句识别术语：{completed}/{total}",
                ),
            ))
            completed_before += len(documents)
        return providers

    def run(
        self,
        options: RecognitionRunOptions,
        progress_callback: Callable[[str, int, str], None] | None = None,
        run_created_callback: Callable[[uuid.UUID], None] | None = None,
        checkpoint_callback: Callable[[], None] | None = None,
    ) -> dict[str, Any]:
        def notify(stage: str, percent: int, detail: str) -> None:
            if progress_callback is not None:
                progress_callback(stage, percent, detail)

        notify("prepare", 1, "读取清洗后的语料与参考知识表")
        corpus = self.registry.get_corpus(options.corpus_key)
        source = self._project_file(options.documents_path)
        topic_table = self._project_file(options.topic_table_path, required=False)
        source_documents = list(load_documents_jsonl(source))
        if not source_documents:
            raise ValueError("没有可用于识别的结构化文档")
        documents = build_recognition_units(
            source_documents,
            sentences_path=source.parent / "sentences.jsonl",
        )
        if not documents:
            raise ValueError("清洗语料未产生可识别的完整句子或对话话轮")
        run_token = datetime.now().strftime("recognition_%Y%m%d_%H%M%S_%f")
        workspace = self.registry.data_root / "workspaces" / options.corpus_key / run_token
        recognition_root = workspace / "recognition"
        recognition_root.mkdir(parents=True, exist_ok=False)
        safe_options = {
            **{key: value for key, value in options.__dict__.items() if key not in {"api_key", "validator_models"}},
            "strategy": options.strategy.value,
            "api_key": "<not persisted>",
            "external_model_configured": bool(options.use_external_model),
            "validator_models": [{
                "id": row.id, "model": row.model,
                "model_version": row.model_version or row.model,
                "base_url": row.base_url, "weight": row.weight,
                "api_key": "<not persisted>",
            } for row in options.validator_models],
        }
        job = self.registry.create_processing_job(
            options.corpus_key, workspace, "recognition_validation", parameters=safe_options
        )
        run = self.repository.create_run(
            job["id"], pipeline_version=self.pipeline_version,
            config_snapshot=safe_options,
            prompt_snapshot={key: value.model_dump(mode="json") for key, value in self.prompts.items()},
        )
        if run_created_callback is not None:
            run_created_callback(run["id"])
        self.registry.update_processing_job(job["id"], "running")
        self.repository.update_run_status(run["id"], "running")
        try:
            result = self._execute(
                run, documents, recognition_root, topic_table, options, notify,
                checkpoint_callback,
            )
            self.repository.update_run_status(run["id"], "succeeded")
            self.registry.update_processing_job(job["id"], "succeeded")
            return result
        except RecognitionWorkflowCancelled as exc:
            self.repository.update_run_status(run["id"], "cancelled", str(exc))
            self.registry.update_processing_job(job["id"], "cancelled", error_message=str(exc))
            raise
        except Exception as exc:
            self.repository.update_run_status(run["id"], "failed", str(exc))
            self.registry.update_processing_job(job["id"], "failed", error_message=str(exc))
            raise

    def _execute(
        self,
        run: Mapping[str, Any],
        documents: Sequence[Document],
        root: Path,
        topic_table: Path | None,
        options: RecognitionRunOptions,
        notify: Callable[[str, int, str], None] | None = None,
        checkpoint_callback: Callable[[], None] | None = None,
    ) -> dict[str, Any]:
        notify = notify or (lambda stage, percent, detail: None)
        checkpoint = checkpoint_callback or (lambda: None)
        run_id = run["id"]
        relative_root = root.relative_to(self.registry.data_root)
        document_rows: dict[str, Mapping[str, Any]] = {}
        document_payloads = []
        document_total = len(documents)
        notify("preprocess", 2, f"正在保存全量识别单元：0/{document_total}")
        for document_index, document in enumerate(documents, start=1):
            checkpoint()
            raw_path = root / "01_preprocessed" / f"{document.document_id}.raw.txt"
            clean_path = root / "01_preprocessed" / f"{document.document_id}.clean.txt"
            raw_path.parent.mkdir(parents=True, exist_ok=True)
            raw_path.write_text(document.raw_text, encoding="utf-8")
            clean_path.write_text(document.clean_text, encoding="utf-8")
            row = self.repository.add_document(
                run_id, document_id=document.document_id, source=document.source,
                raw_asset_path=raw_path.relative_to(self.registry.data_root),
                clean_asset_path=clean_path.relative_to(self.registry.data_root),
                raw_checksum_sha256=_checksum(document.raw_text),
                clean_checksum_sha256=_checksum(document.clean_text),
                processing_version=document.processing_version, metadata=document.metadata,
            )
            document_rows[document.document_id] = row
            document_payloads.append(document.model_dump(mode="json"))
            if document_index == document_total or document_index % 250 == 0:
                preprocess_progress = 2 + min(5, round(document_index / document_total * 5))
                notify(
                    "preprocess",
                    preprocess_progress,
                    f"正在保存全量识别单元：{document_index}/{document_total}",
                )
        _write_jsonl(root / "01_preprocessed" / "documents.jsonl", document_payloads)

        unit_counts: dict[str, int] = {}
        for document in documents:
            key = str(document.metadata.get("analysis_unit_type") or "text_unit")
            unit_counts[key] = unit_counts.get(key, 0) + 1
        source_document_count = len({
            str(document.metadata.get("source_document_id") or document.document_id)
            for document in documents
        })
        notify("preprocess", 8, f"已将 {source_document_count} 篇语料拆分为 {len(documents)} 个句子/完整话轮")
        topic_support_extractor = LegacyStatisticalTermExtractor.from_config(
            self.project_root / "configs/term_extraction.json",
            min_frequency_override=1,
            max_candidates_override=self.config.analysis_units.term_candidates_per_unit,
        )
        # Topic naming keeps its existing lightweight keyword support.  The
        # formal term stage below uses the independently evaluated corpus-level
        # recognizer and does not change topic-identification behaviour.
        topic_identifier = ContentUnitTopicIdentifier(topic_support_extractor)
        topic_candidates = topic_identifier.identify(
            documents,
            topic_table,
            progress_callback=lambda completed, total: notify(
                "topic", 8 + min(3, int(completed / max(1, total) * 3)),
                f"正在使用已发布话题体系进行全量分类：{completed}/{total}",
            ),
            checkpoint_callback=checkpoint,
        )
        topic_rows: dict[str, Mapping[str, Any]] = {}
        topic_by_document: dict[str, TopicCandidate] = {}
        pending_topic_rows: list[dict[str, Any]] = []
        topic_total = len(topic_candidates)

        def flush_topic_rows() -> None:
            if not pending_topic_rows:
                return
            stored_ids = self.repository.add_topic_candidates_batch(run_id, pending_topic_rows)
            topic_rows.update({key: {"id": value} for key, value in stored_ids.items()})
            pending_topic_rows.clear()

        for topic_index, candidate in enumerate(topic_candidates, start=1):
            checkpoint()
            document_row_id = (
                document_rows[candidate.document_id]["id"] if candidate.document_id else None
            )
            pending_topic_rows.append({
                "candidate_topic_id": candidate.topic_candidate_id,
                "topic_name": candidate.topic_name,
                "source_method": candidate.source_method,
                "resolution": candidate.resolution.value,
                "document_row_id": document_row_id,
                "cluster_id": candidate.cluster_id,
                "topic_definition": candidate.topic_definition,
                "keywords": candidate.keywords,
                "representative_texts": candidate.representative_texts,
                "evidence": candidate.evidence,
            })
            if len(pending_topic_rows) >= 1000 or topic_index == topic_total:
                flush_topic_rows()
                notify(
                    "topic", 11 + min(6, int(topic_index / max(1, topic_total) * 6)),
                    f"正在登记全量话题分类结果：{topic_index}/{topic_total}",
                )
            ids = [candidate.document_id] if candidate.document_id else candidate.evidence.get("document_ids", [])
            for document_id in ids:
                if document_id:
                    topic_by_document[str(document_id)] = candidate
        _write_jsonl(root / "02_topic_candidates" / "candidates.jsonl", topic_candidates)

        notify("topic", 18, f"已产生 {len(topic_candidates)} 个话题候选，正在独立检验")
        topic_run = self.repository.create_validation_run(
            run_id, validation_kind="topic", aggregation_strategy=options.strategy.value,
            min_validators=options.min_validators,
            validator_config=self._provider_snapshot(options),
        )
        topic_service = TopicValidationService(
            [BuiltinRuleValidatorProvider(
                self.prompts["term"], self.prompts["topic"], self.prompts["cross"]
            )], prompt=self.prompts["topic"],
            strategy=options.strategy, min_validators=options.min_validators,
            weights=self._runtime_weights(options), repository=self.repository,
        )
        topic_results: dict[str, Any] = {}
        documents_by_id = {document.document_id: document for document in documents}
        topic_table_payload = (
            [] if topic_table is None
            else [asdict(node) for node in normalize_topic_table(topic_table)]
        )
        valid_topic_paths = {
            str(row.get("path") or "").strip()
            for row in topic_table_payload
            if str(row.get("path") or "").strip()
        }
        topic_validation_total = len(topic_candidates)
        for topic_validation_index, candidate in enumerate(topic_candidates, start=1):
            checkpoint()
            representative_id = candidate.document_id or next(iter(candidate.evidence.get("document_ids", [])), None)
            document = documents_by_id.get(str(representative_id))
            if document is None:
                raise ValueError(
                    f"topic candidate {candidate.topic_candidate_id} references unknown document {representative_id}"
                )
            aggregate = topic_service.validate_and_persist(
                TopicValidationRequest(
                    document=document,
                    candidate=candidate,
                    topic_table=topic_table_payload,
                    context={
                        "candidate_path": candidate.evidence.get("path", ""),
                        "mapped_topic_id": candidate.evidence.get("mapped_topic_id", ""),
                        "validation_requirement": "validate level_1, level_2 and level_3 together",
                    },
                ),
                validation_run_id=topic_run["id"], topic_candidate_row_id=topic_rows[candidate.topic_candidate_id]["id"],
            )
            topic_results[candidate.topic_candidate_id] = aggregate
            status = "validated" if aggregate.aggregated_decision == ValidatorDecision.ACCEPT else (
                "rejected" if aggregate.aggregated_decision == ValidatorDecision.REJECT else "candidate"
            )
            self.repository.update_candidate_status("topic", topic_rows[candidate.topic_candidate_id]["id"], status)
            if (
                topic_validation_index == topic_validation_total
                or topic_validation_index % 250 == 0
            ):
                topic_progress = 18 + min(
                    22,
                    round(topic_validation_index / max(1, topic_validation_total) * 22),
                )
                notify(
                    "topic",
                    topic_progress,
                    "正在独立检验话题候选："
                    f"{topic_validation_index}/{topic_validation_total}",
                )
        _write_jsonl(root / "03_topic_validation" / "results.jsonl", list(topic_results.values()))
        documents_by_id = {document.document_id: document for document in documents}
        _write_csv(
            root / "03_topic_validation" / "topic_validation_table.csv",
            ("text", "topic_name", "resolution", "source_method", "decision", "agreement_score", "reason", "document_id", "topic_candidate_id"),
            [
                {
                    "text": documents_by_id.get(candidate.document_id).clean_text if candidate.document_id in documents_by_id else "",
                    "topic_candidate_id": candidate.topic_candidate_id,
                    "document_id": candidate.document_id or "",
                    "topic_name": candidate.topic_name,
                    "resolution": candidate.resolution.value,
                    "source_method": candidate.source_method,
                    "decision": topic_results[candidate.topic_candidate_id].aggregated_decision.value,
                    "agreement_score": topic_results[candidate.topic_candidate_id].agreement_score,
                    "reason": "；".join(row.reason for row in topic_results[candidate.topic_candidate_id].validation_details),
                }
                for candidate in topic_candidates
            ],
        )

        notify("term", 42, "话题检验完成，正在识别术语候选")
        term_extractor = EvaluatedCorpusTermExtractor(
            config_path=self.project_root / "configs/term_recognition/recognition.json",
            trusted_terms_path=self.project_root / "configs/term_recognition/trusted_terms.csv",
            output_root=root / "04_term_candidates" / "engine_evidence",
            cache_path=self.registry.data_root / "cache" / "term_recognition" / "bcc_frequency_cache.json",
            offline=os.environ.get("TERM_RECOGNITION_BCC_OFFLINE", "").strip().lower()
            in {"1", "true", "yes", "on"},
            max_candidates_per_document=self.config.analysis_units.term_candidates_per_unit,
        )
        def term_recognition_progress(percent: float, label: str) -> None:
            checkpoint()
            notify("term", 42 + min(9, round(percent * 0.09)), label)

        extracted_terms = term_extractor.extract_many(
            documents,
            progress_callback=term_recognition_progress,
        )
        terms_by_document: dict[str, list[TermCandidate]] = {}
        term_row_ids: dict[str, uuid.UUID] = {}
        all_terms: list[TermCandidate] = []
        pending_term_rows: list[dict[str, Any]] = []
        total_term_candidates = sum(len(rows) for rows in extracted_terms.values())
        notify("term", 50, f"正在登记术语候选：0/{total_term_candidates}")

        def flush_term_rows() -> None:
            if not pending_term_rows:
                return
            term_row_ids.update(
                self.repository.add_term_candidates_batch(run_id, pending_term_rows)
            )
            pending_term_rows.clear()

        for document in documents:
            checkpoint()
            candidates = extracted_terms.get(document.document_id, [])
            terms_by_document[document.document_id] = candidates
            all_terms.extend(candidates)
            for candidate in candidates:
                pending_term_rows.append({
                    "document_row_id": document_rows[document.document_id]["id"],
                    "candidate_term_id": candidate.term_id,
                    "surface_form": candidate.surface_form,
                    "normalized_form": candidate.normalized_form,
                    "start_offset": candidate.start_offset,
                    "end_offset": candidate.end_offset,
                    "sentence": candidate.sentence,
                    "extractor": candidate.extractor,
                    "extractor_version": candidate.extractor_version,
                    "score": candidate.score,
                    "evidence": candidate.evidence,
                })
                if len(pending_term_rows) >= 1000:
                    flush_term_rows()
                    notify(
                        "term", 50 + min(2, round(len(term_row_ids) / max(total_term_candidates, 1) * 2)),
                        f"正在登记术语候选：{len(term_row_ids)}/{total_term_candidates}",
                    )
        flush_term_rows()
        notify("term", 52, f"术语候选已全部登记：{len(term_row_ids)}/{total_term_candidates}")
        _write_jsonl(root / "04_term_candidates" / "candidates.jsonl", all_terms)

        direct_baseline = not options.use_external_model
        notify(
            "term", 52,
            (
                f"已产生 {len(all_terms)} 个术语候选；未启用外部模型，正在直接采用术语基础表"
                if direct_baseline else
                f"已产生 {len(all_terms)} 个术语候选，正在进行逐句模型交集检验"
            ),
        )
        term_run = self.repository.create_validation_run(
            run_id, validation_kind="term", aggregation_strategy=options.strategy.value,
            min_validators=options.min_validators,
            validator_config=self._provider_snapshot(options),
        )
        term_results: dict[str, Any] = {}
        accepted_terms: dict[str, list[TermCandidate]] = {doc.document_id: [] for doc in documents}

        if direct_baseline:
            accepted_terms = {
                document.document_id: list(terms_by_document[document.document_id])
                for document in documents
            }
            updated = self.repository.update_term_candidate_status_for_run(run_id, "validated")
            self.repository.update_validation_run_status(term_run["id"], "succeeded")
            _write_jsonl(root / "05_term_validation" / "results.jsonl", ({
                "mode": "evaluated_baseline_direct_output",
                "candidate_count": len(all_terms),
                "updated_candidate_count": updated,
                "decision": ValidatorDecision.ACCEPT.value,
                "reason": "未启用外部大模型，按配置直接采用经过效果核验的术语基础识别结果",
            },))
            notify("term", 68, f"已直接采用术语基础表，共 {updated} 条候选出现记录")
        else:
            term_service = TermValidationService(
                self._term_providers(options, documents, notify, checkpoint),
                prompt=self.prompts["term_sentence"],
                strategy=options.strategy, min_validators=options.min_validators,
                weights=self._runtime_weights(options), repository=self.repository,
            )
            validated_count = 0
            for document in documents:
                for candidate in terms_by_document[document.document_id]:
                    checkpoint()
                    aggregate = term_service.validate_and_persist(
                        TermValidationRequest(document=document, candidate=candidate),
                        validation_run_id=term_run["id"],
                        term_candidate_row_id=term_row_ids[candidate.term_id],
                    ).result
                    term_results[candidate.term_id] = aggregate
                    status = "validated" if aggregate.aggregated_decision == ValidatorDecision.ACCEPT else (
                        "rejected" if aggregate.aggregated_decision == ValidatorDecision.REJECT else "candidate"
                    )
                    self.repository.update_candidate_status(
                        "term", term_row_ids[candidate.term_id], status
                    )
                    if aggregate.aggregated_decision == ValidatorDecision.ACCEPT:
                        accepted_terms[document.document_id].append(candidate)
                    validated_count += 1
                    if validated_count % 1000 == 0:
                        notify(
                            "term", 64 + min(4, round(validated_count / max(len(all_terms), 1) * 4)),
                            f"正在登记模型交集结果：{validated_count}/{len(all_terms)}",
                        )
            _write_jsonl(root / "05_term_validation" / "results.jsonl", term_results.values())

        def term_decision(candidate: TermCandidate) -> ValidatorDecision:
            return (
                ValidatorDecision.ACCEPT
                if direct_baseline else term_results[candidate.term_id].aggregated_decision
            )

        def term_agreement(candidate: TermCandidate) -> float:
            return 1.0 if direct_baseline else term_results[candidate.term_id].agreement_score

        def term_reason(candidate: TermCandidate) -> str:
            if direct_baseline:
                return "未启用外部大模型，直接采用经过效果核验的术语基础识别结果"
            return "；".join(row.reason for row in term_results[candidate.term_id].validation_details)

        _write_csv(
            root / "05_term_validation" / "term_validation_table.csv",
            ("term_id", "document_id", "surface_form", "normalized_form", "sentence", "extractor", "score", "decision", "agreement_score", "reason"),
            (
                {
                    "term_id": candidate.term_id,
                    "document_id": candidate.document_id,
                    "surface_form": candidate.surface_form,
                    "normalized_form": candidate.normalized_form,
                    "sentence": candidate.sentence,
                    "extractor": candidate.extractor,
                    "score": candidate.score,
                    "decision": term_decision(candidate).value,
                    "agreement_score": term_agreement(candidate),
                    "reason": term_reason(candidate),
                }
                for candidate in all_terms
            ),
        )

        decision_rows: dict[str, Mapping[str, Any]] = {}
        human_consensus: dict[str, Any] | None = None
        if options.human_validation:
            for document in documents:
                topic = topic_by_document.get(document.document_id)
                if topic is None:
                    continue
                term_status = self._document_term_status(
                    [term_decision(item) for item in terms_by_document[document.document_id]]
                )
                topic_result = topic_results[topic.topic_candidate_id]
                decision_rows[document.document_id] = self.repository.create_resource_decision(
                    run_id, document_rows[document.document_id]["id"],
                    term_validation_status=term_status.value,
                    topic_validation_status=topic_result.aggregated_decision.value,
                    cross_validation_status=None,
                    final_decision=FinalDecision.MANUAL_REVIEW.value,
                    decision_rule="offline_annotation_required_before_cross_validation",
                    reason="话题与术语机器结果已生成；等待离线人工标注形成共识后才运行交叉检验。",
                    lifecycle_status=CandidateStatus.VALIDATED.value,
                )
            human_consensus = self._wait_for_annotation_consensus(run_id, notify, checkpoint)
            notify("cross", 74, "人工标注已形成共识，准备运行话题—术语交叉检验")

        if options.cross_validation_enabled:
            notify("cross", 76, "话题与术语独立识别完成，正在进行可选双向交叉检验")
            cross_run = self.repository.create_validation_run(
                run_id, validation_kind="cross", aggregation_strategy=options.strategy.value,
                min_validators=options.min_validators,
                validator_config=self._provider_snapshot(options),
            )
            cross_service: CrossValidationService | None = CrossValidationService(
                self._providers(options, "cross"), prompt=self.prompts["cross"],
                strategy=options.strategy, min_validators=options.min_validators,
                weights=self._runtime_weights(options), repository=self.repository,
            )
        else:
            notify("cross", 76, "已按配置跳过话题—术语交叉检验")
            cross_run = None
            cross_service = None
        cross_results: dict[str, Any] = {}
        decisions = []
        pending_decision_rows: list[dict[str, Any]] = []

        def flush_decision_rows() -> None:
            if not pending_decision_rows:
                return
            self.repository.add_resource_decisions_batch(run_id, pending_decision_rows)
            pending_decision_rows.clear()

        if cross_service is None:
            notify(
                "decision", 78,
                f"已跳过话题—术语交叉检验，正在生成综合决策：0/{len(documents)}",
            )
        for document_index, document in enumerate(documents, start=1):
            checkpoint()
            topic = topic_by_document.get(document.document_id)
            if topic is None:
                continue
            linked_terms = (
                accepted_terms[document.document_id]
                if options.machine_validation else terms_by_document[document.document_id]
            )
            terms = linked_terms
            if human_consensus is not None:
                stored_row = decision_rows[document.document_id]
                decision_key = str(stored_row["id"])
                topic_value = human_consensus["consensus"]["topic"][decision_key]["value"]
                term_value = human_consensus["consensus"]["term"][decision_key]["value"]
                if "__REJECT__" in {topic_value, term_value}:
                    decisions.append({
                        "document_id": document.document_id,
                        "decision_id": decision_key,
                        "final_decision": FinalDecision.REJECT.value,
                        "lifecycle_status": CandidateStatus.REJECTED.value,
                        "reason": "离线人工标注共识拒绝该分析单元。",
                    })
                    continue
                topic = topic.model_copy(update={
                    "topic_name": topic_value.split(">")[-1].strip() or topic.topic_name,
                    "evidence": {**topic.evidence, "human_consensus_path": topic_value},
                })
                requested_terms = [item.strip() for item in re.split(r"[,，;；|]", term_value) if item.strip()]
                matched = [item for item in linked_terms if item.normalized_form in requested_terms]
                if requested_terms and linked_terms:
                    base = linked_terms[0]
                    existing = {item.normalized_form for item in matched}
                    matched.extend(
                        base.model_copy(update={
                            "term_id": f"{base.term_id}_human_{index}",
                            "surface_form": value,
                            "normalized_form": value,
                            "evidence": {**base.evidence, "human_consensus": True},
                        })
                        for index, value in enumerate(requested_terms, start=1)
                        if value not in existing
                    )
                terms = matched
            term_status = (
                ValidatorDecision.ACCEPT if human_consensus is not None else self._document_term_status(
                    [term_decision(item) for item in terms_by_document[document.document_id]]
                )
            )
            topic_result = (
                _StatusProxy(ValidatorDecision.ACCEPT)
                if human_consensus is not None else topic_results[topic.topic_candidate_id]
            )
            if cross_service is not None and cross_run is not None:
                case = self.repository.add_cross_validation_case(
                    run_id, document_rows[document.document_id]["id"],
                    term_candidate_ids=[term_row_ids[item.term_id] for item in linked_terms],
                    topic_candidate_id=topic_rows[topic.topic_candidate_id]["id"],
                    mapped_topic_id=topic.evidence.get("mapped_topic_id"),
                )
                cross = cross_service.validate_and_persist(
                    CrossValidationRequest(
                        document=document, validated_terms=terms, validated_topic=topic,
                        topic_definition=topic.topic_definition,
                    ),
                    validation_run_id=cross_run["id"], cross_validation_case_id=case["id"],
                )
                cross_results[document.document_id] = cross
                revision = str(
                    getattr(cross, "possible_topic_revision", "")
                    or (getattr(cross, "evidence", {}) or {}).get("possible_topic_revision")
                    or ""
                ).strip()
                if (
                    cross.aggregated_decision != ValidatorDecision.ACCEPT
                    and revision
                    and revision in valid_topic_paths
                ):
                    corrected_evidence = {
                        **topic.evidence,
                        "original_path": topic.evidence.get("path", ""),
                        "corrected_path": revision,
                        "correction_source": "term_topic_cross_validation",
                    }
                    topic = topic.model_copy(update={
                        "topic_name": revision.split(">")[-1].strip(),
                        "evidence": corrected_evidence,
                    })
                    topic_by_document[document.document_id] = topic
                    self.repository.revise_topic_candidate(
                        topic_rows[topic.topic_candidate_id]["id"],
                        topic_name=topic.topic_name,
                        evidence=corrected_evidence,
                    )
                    cross = cross.model_copy(update={
                        "aggregated_decision": ValidatorDecision.ACCEPT,
                        "reason": f"交叉检验发现原话题不匹配，已按可用体系修正为：{revision}",
                        "evidence": {**cross.evidence, "applied_topic_revision": revision},
                    })
                    topic_result = _StatusProxy(ValidatorDecision.ACCEPT)
                cross_results[document.document_id] = cross
                decision = self.decision_engine.decide(
                    document.document_id, _StatusProxy(term_status), topic_result, cross,
                )
            else:
                topic_status = topic_result.aggregated_decision
                accepted = term_status == ValidatorDecision.ACCEPT and topic_status == ValidatorDecision.ACCEPT
                rejected = ValidatorDecision.REJECT in {term_status, topic_status}
                decision = ResourceDecision(
                    document_id=document.document_id,
                    term_validation_status=term_status,
                    topic_validation_status=topic_status,
                    cross_validation_status=None,
                    final_decision=(
                        FinalDecision.AUTO_ACCEPT if accepted else
                        FinalDecision.REJECT if rejected else FinalDecision.PENDING
                    ),
                    decision_rule="term_and_topic_without_optional_cross_validation",
                    reason=(
                        "术语与话题独立识别均通过；本轮未启用可选交叉检验"
                        if accepted else "术语或话题独立识别未满足自动通过条件"
                    ),
                    lifecycle_status=(
                        CandidateStatus.APPROVED if accepted else
                        CandidateStatus.REJECTED if rejected else CandidateStatus.CANDIDATE
                    ),
                )
            if human_consensus is None:
                decision = self._apply_review_policy(decision, options)
                decision_id = uuid.uuid4()
                pending_decision_rows.append({
                    "id": decision_id,
                    "document_row_id": document_rows[document.document_id]["id"],
                    "term_validation_status": decision.term_validation_status.value if decision.term_validation_status else None,
                    "topic_validation_status": decision.topic_validation_status.value if decision.topic_validation_status else None,
                    "cross_validation_status": decision.cross_validation_status.value if decision.cross_validation_status else None,
                    "final_decision": decision.final_decision.value,
                    "decision_rule": decision.decision_rule,
                    "reason": decision.reason,
                    "lifecycle_status": decision.lifecycle_status.value,
                })
                if len(pending_decision_rows) >= 1000:
                    flush_decision_rows()
                stored = {"id": decision_id}
            else:
                lifecycle = (
                    CandidateStatus.APPROVED.value
                    if decision.final_decision == FinalDecision.AUTO_ACCEPT else
                    CandidateStatus.REJECTED.value
                    if decision.final_decision == FinalDecision.REJECT else
                    CandidateStatus.CANDIDATE.value
                )
                stored = self.repository.update_resource_decision_result(
                    decision_rows[document.document_id]["id"],
                    term_validation_status=ValidatorDecision.ACCEPT.value,
                    topic_validation_status=ValidatorDecision.ACCEPT.value,
                    cross_validation_status=decision.cross_validation_status.value if decision.cross_validation_status else None,
                    final_decision=decision.final_decision.value,
                    decision_rule=f"offline_annotation_consensus THEN {decision.decision_rule}",
                    reason=decision.reason,
                    lifecycle_status=lifecycle,
                )
                decision = decision.model_copy(update={"lifecycle_status": CandidateStatus(lifecycle)})
            decisions.append({**decision.model_dump(mode="json"), "decision_id": str(stored["id"])})
            if document_index % 250 == 0 or document_index == len(documents):
                if cross_service is None:
                    notify(
                        "decision",
                        78 + min(13, round(document_index / max(len(documents), 1) * 13)),
                        f"已跳过话题—术语交叉检验，正在生成综合决策：{document_index}/{len(documents)}",
                    )
                else:
                    notify(
                        "cross",
                        76 + min(15, round(document_index / max(len(documents), 1) * 15)),
                        f"正在交叉检验并生成综合决策：{document_index}/{len(documents)}",
                    )
        flush_decision_rows()
        notify("decision", 92, "综合决策已完成，正在批量确认通过的术语与话题候选")
        self.repository.approve_candidates_for_approved_decisions(run_id)
        notify("decision", 94, "候选状态已更新，正在生成最终逐句标签与成果文件")
        _write_jsonl(root / "06_term_topic_cross_validation" / "results.jsonl", list(cross_results.values()))
        _write_jsonl(root / "07_decision" / "results.jsonl", decisions)
        decision_by_document = {str(row["document_id"]): row for row in decisions}
        sentence_labels = []
        notify("decision", 95, f"正在写入逐句标签：0/{len(documents)}")
        for document_index, document in enumerate(documents, start=1):
            topic = topic_by_document.get(document.document_id)
            evidence = topic.evidence if topic is not None else {}
            topic_path = str(
                evidence.get("corrected_path")
                or evidence.get("human_consensus_path")
                or evidence.get("path")
                or (topic.topic_name if topic is not None else "")
            ).strip()
            topic_levels = [item.strip() for item in topic_path.split(">") if item.strip()]
            if (
                len(topic_levels) >= 3
                and topic_levels[0] in {"BERTopic测试体系", "BERTopic 聚类测试体系"}
                and topic_levels[1] == "聚类结果"
            ):
                topic_levels = topic_levels[2:]
            topic_levels = [
                re.sub(r"^BT_TOPIC_\d+\s*[｜|]\s*", "", item).strip()
                for item in topic_levels[:3]
            ]
            topic_path = " > ".join(item for item in topic_levels if item)
            accepted_term_rows = accepted_terms[document.document_id]
            sentence_labels.append({
                "document_id": document.document_id,
                "source_document_id": document.metadata.get("source_document_id", ""),
                "source_sentence_id": document.metadata.get("source_sentence_id", ""),
                "sentence_sequence": document.metadata.get("analysis_unit_index"),
                "analysis_unit_type": document.metadata.get("analysis_unit_type", ""),
                "source": document.source,
                "clean_text": document.clean_text,
                "topic_path": topic_path,
                "topic_levels": topic_levels,
                "topics": [topic_path] if topic_path else [],
                "terms": [item.normalized_form for item in accepted_term_rows],
                "term_candidates": [
                    {"term": item.normalized_form, "score": item.score}
                    for item in accepted_term_rows
                ],
                "cross_validation_status": (
                    cross_results[document.document_id].aggregated_decision.value
                    if document.document_id in cross_results else "not_enabled"
                ),
                "final_decision": decision_by_document.get(document.document_id, {}).get(
                    "final_decision", FinalDecision.PENDING.value
                ),
                "provenance": {
                    "recognition_run_id": str(run_id),
                    "topic_source": "published_taxonomy_full_classification",
                    "term_source": (
                        "evaluated_baseline_intersect_external_models"
                        if options.use_external_model else "evaluated_baseline"
                    ),
                },
            })
            if document_index == len(documents) or document_index % 1000 == 0:
                notify(
                    "decision", 95 + min(3, round(document_index / max(len(documents), 1) * 3)),
                    f"正在写入逐句标签：{document_index}/{len(documents)}",
                )
        _write_jsonl(root / "08_approved_resources" / "sentence_labels.jsonl", sentence_labels)
        notify("decision", 92, "识别与可选交叉检验完成，正在形成逐句标签和成果表")
        approved = self.refresh_approved(run_id)
        summary = {
            "run_id": str(run_id), "workspace": str(relative_root).replace("\\", "/"),
            "source_documents": source_document_count, "documents": len(documents),
            "analysis_units": len(documents), "analysis_unit_counts": unit_counts,
            "term_candidates": len(all_terms),
            "topic_candidates": len(topic_candidates), "decisions": len(decisions),
            "auto_accept": sum(row["final_decision"] == "auto_accept" for row in decisions),
            "manual_review": sum(row["final_decision"] == "manual_review" for row in decisions),
            "rejected": sum(row["final_decision"] == "reject" for row in decisions),
            "approved_resources": len(approved), "status": "succeeded",
        }
        _write_json(root / "run_summary.json", summary)
        notify(
            "complete",
            100,
            (
                "识别、检验、交叉检验和综合决策已完成"
                if options.cross_validation_enabled else
                "识别、检验和综合决策已完成（本轮未启用交叉检验）"
            ),
        )
        return summary

    def _wait_for_annotation_consensus(
        self,
        run_id: uuid.UUID,
        notify: Callable[[str, int, str], None],
        checkpoint: Callable[[], None] | None = None,
        *,
        poll_interval: float = 1.0,
    ) -> dict[str, Any]:
        """Hold the workflow before cross validation until both offline files agree."""
        notify("human_review", 70, "已暂停：请下载并上传话题、术语人工标注文件")
        checkpoint = checkpoint or (lambda: None)
        while True:
            checkpoint()
            status = annotation_status(self.repository.get_run_bundle(run_id))
            if status["ready"]:
                return status
            time.sleep(poll_interval)

    def _provider_snapshot(self, options: RecognitionRunOptions) -> list[dict[str, Any]]:
        if not options.machine_validation:
            return [{
                "id": "human_review_gate",
                "model": "human",
                "version": "not-applicable",
                "validation_mode": "human_only",
            }]
        rows = ([{"id": "builtin_rule_baseline", "model": "deterministic-rules", "version": "1.0"}]
                if options.include_builtin_rules else [])
        for config in self.config.validation.models:
            if not config.enabled:
                continue
            configured = all(
                bool(os.environ.get(name or "", "").strip())
                for name in (config.model_env, config.base_url_env, config.api_key_env)
            )
            rows.append({
                "id": config.id, "provider": config.provider, "weight": config.weight,
                "configured": configured,
            })
        if options.use_external_model and options.model_name:
            rows.append({
                "id": "workbench_external_model", "model": options.model_name,
                "model_version": options.model_version or options.model_name,
                "base_url": options.base_url,
            })
        rows.extend({
            "id": row.id, "model": row.model,
            "model_version": row.model_version or row.model,
            "base_url": row.base_url, "weight": row.weight,
            "configured": bool(row.api_key),
        } for row in options.validator_models)
        return rows

    @staticmethod
    def _document_term_status(values: Sequence[ValidatorDecision]) -> ValidatorDecision:
        # A sentence with no domain term is a valid empty term annotation, not
        # a failed sentence.  It must still receive its topic and structure.
        if not values:
            return ValidatorDecision.ACCEPT
        if ValidatorDecision.ACCEPT in values:
            return ValidatorDecision.ACCEPT
        if ValidatorDecision.REVIEW in values or ValidatorDecision.UNCERTAIN in values:
            return ValidatorDecision.REVIEW
        return ValidatorDecision.REJECT

    def refresh_approved(self, run_id: uuid.UUID) -> list[dict[str, Any]]:
        bundle = self.repository.get_approved_resource_bundle(run_id)
        root = self.registry.data_root / str(bundle["run"]["workspace_path"]) / "recognition"
        docs = {row["id"]: row for row in bundle["documents"]}
        terms_by_doc: dict[Any, list[dict[str, Any]]] = {}
        for row in bundle["term_candidates"]:
            terms_by_doc.setdefault(row["recognition_document_id"], []).append(row)
        topics_by_doc: dict[Any, list[dict[str, Any]]] = {}
        for row in bundle["topic_candidates"]:
            if row.get("recognition_document_id"):
                topics_by_doc.setdefault(row["recognition_document_id"], []).append(row)

        def topic_path(row: Mapping[str, Any]) -> str:
            evidence = row.get("evidence") if isinstance(row.get("evidence"), Mapping) else {}
            return str(evidence.get("corrected_path") or evidence.get("human_consensus_path") or evidence.get("path") or row.get("topic_name") or "").strip()
        latest_review = {}
        for review in bundle["human_reviews"]:
            latest_review[review["resource_decision_id"]] = review
        approved = []
        for decision in bundle["resource_decisions"]:
            if decision["lifecycle_status"] != "approved":
                continue
            doc = docs[decision["recognition_document_id"]]
            clean_path = self.registry.data_root / doc["clean_asset_path"]
            review = latest_review.get(decision["id"])
            after_state = (review or {}).get("after_state") or {}
            reviewed_terms = after_state.get("terms")
            if isinstance(reviewed_terms, str):
                reviewed_terms = [value.strip() for value in reviewed_terms.replace("，", ",").split(",") if value.strip()]
            reviewed_topics = after_state.get("topics")
            if isinstance(reviewed_topics, str):
                reviewed_topics = [value.strip() for value in reviewed_topics.replace("，", ",").split(",") if value.strip()]
            approved.append({
                "document_id": doc["document_id"], "source": doc["source"],
                "clean_text": clean_path.read_text(encoding="utf-8"),
                "sentence_sequence": (doc.get("metadata") or {}).get("analysis_unit_index"),
                "source_document_id": (doc.get("metadata") or {}).get("source_document_id", ""),
                "source_sentence_id": (doc.get("metadata") or {}).get("source_sentence_id", ""),
                "analysis_unit_type": (doc.get("metadata") or {}).get("analysis_unit_type", ""),
                "terms": reviewed_terms if isinstance(reviewed_terms, list) else [row["normalized_form"] for row in terms_by_doc.get(doc["id"], [])],
                "topics": reviewed_topics if isinstance(reviewed_topics, list) else [topic_path(row) for row in topics_by_doc.get(doc["id"], []) if topic_path(row)],
                "decision_id": str(decision["id"]),
                "provenance": {
                    "recognition_run_id": str(run_id),
                    "human_reviewed": review is not None,
                },
            })
        _write_jsonl(root / "08_approved_resources" / "resources.jsonl", approved)
        term_meta: dict[str, Mapping[str, Any]] = {}
        for row in bundle["term_candidates"]:
            term_meta.setdefault(str(row["normalized_form"]), row)
        topic_meta = {str(row["topic_name"]): row for row in bundle["topic_candidates"]}
        unique_terms = {str(term).strip() for item in approved for term in item["terms"] if str(term).strip()}
        unique_topics = {str(topic).strip() for item in approved for topic in item["topics"] if str(topic).strip()}
        _write_csv(
            root / "08_approved_resources" / "terminology.csv",
            ("term_id", "preferred_term", "score", "definition", "term_type", "status"),
            [
                {
                    "term_id": term_meta.get(term, {}).get("candidate_term_id") or f"TERM_{index:05d}",
                    "preferred_term": term,
                    "score": term_meta.get(term, {}).get("score"),
                    "definition": "",
                    "term_type": "领域术语",
                    "status": "approved",
                }
                for index, term in enumerate(sorted(unique_terms), start=1)
            ],
        )
        _write_json(
            root / "08_approved_resources" / "taxonomy.json",
            {
                "taxonomy_name": "‘中文+’语料库话题体系",
                "taxonomy_version": str(run_id),
                "status": "approved",
                "source_recognition_run_id": str(run_id),
                "nodes": [
                    {
                        "id": topic_meta.get(topic, {}).get("mapped_topic_id") or topic_meta.get(topic, {}).get("candidate_topic_id") or f"TOPIC_{index:05d}",
                        "name": topic,
                        "parent_id": "",
                        "path": topic,
                        "definition": topic_meta.get(topic, {}).get("topic_definition") or "",
                        "keywords": topic_meta.get(topic, {}).get("keywords") or [],
                        "node_type": "recognized",
                    }
                    for index, topic in enumerate(sorted(unique_topics), start=1)
                ],
            },
        )
        _write_json(root / "08_approved_resources" / "release_provenance.json", {
            "recognition_run_id": str(run_id), "approved_count": len(approved),
            "generated_at": datetime.now().astimezone().isoformat(),
            "note": "Only approved resources may be consumed by a future Corpus Release.",
        })
        return approved

    def activate_approved(self, run_id: uuid.UUID) -> dict[str, Any]:
        """Activate one fully reviewed run as the mutable input for structuring.

        The automatic evidence stays in its Workspace and existing Corpus Releases
        remain immutable.  Replaced current tables are copied to a timestamped
        provenance directory first.
        """
        run = self.repository.get_run(run_id)
        human_validation = bool((run.get("config_snapshot") or {}).get("human_validation"))
        if human_validation:
            bundle = self.repository.get_run_bundle(run_id)
            annotation = annotation_status(bundle)
            decisions = bundle["resource_decisions"]
        else:
            annotation = {"human_validation": False, "ready": True, "scopes": {}}
            decisions = self.repository.list_resource_decisions(run_id)
        if human_validation and not annotation["ready"]:
            pending = sum(item["pending"] for item in annotation["scopes"].values())
            conflicts = sum(item["conflicts"] for item in annotation["scopes"].values())
            raise ValueError(
                f"仍有 {pending} 项离线人工标注未完成（其中 {conflicts} 项存在分歧），不能进入结构化"
            )
        unfinished = [
            row for row in decisions
            if row["lifecycle_status"] not in {"approved", "rejected"}
        ]
        if not decisions:
            raise ValueError(
                "当前任务没有综合决策结果，不能进入结构化"
            )
        # Human review is an explicit gate: every unit must be resolved before
        # activation.  In machine-only mode, however, unresolved candidates are
        # deliberately retained for later review and must not block the approved
        # subset from becoming the current structuring resources.
        if annotation["human_validation"] and unfinished:
            raise ValueError(
                f"仍有 {len(unfinished)} 条结果未完成人工确认，不能进入结构化"
            )
        source = self.registry.data_root / str(run["workspace_path"]) / "recognition" / "08_approved_resources"
        sentence_labels_path = source / "sentence_labels.jsonl"
        required_artifacts = (
            source / "terminology.csv",
            source / "taxonomy.json",
            source / "release_provenance.json",
            sentence_labels_path,
        )
        if human_validation or not all(path.is_file() for path in required_artifacts):
            approved_count = len(self.refresh_approved(run_id))
        else:
            summary_path = source.parent / "run_summary.json"
            summary_data = (
                json.loads(summary_path.read_text(encoding="utf-8"))
                if summary_path.is_file() else {}
            )
            approved_count = int(summary_data.get("approved_resources") or 0)
        sentence_label_count = 0
        if sentence_labels_path.is_file():
            with sentence_labels_path.open("r", encoding="utf-8") as handle:
                sentence_label_count = sum(1 for line in handle if line.strip())
        if not sentence_label_count:
            raise ValueError("当前任务没有全量语句标签，不能进入结构化")
        targets = {
            source / "terminology.csv": self.registry.data_root / "curated" / "terms" / "terminology.csv",
            source / "sentence_labels.jsonl": self.registry.data_root / "curated" / "annotations" / "sentence_annotations.jsonl",
        }
        stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        history = self.registry.data_root / "curated" / "history" / f"{stamp}_{run_id}"
        activated = []
        for source_path, target in targets.items():
            if not source_path.is_file():
                raise FileNotFoundError(source_path)
            target.parent.mkdir(parents=True, exist_ok=True)
            if target.is_file():
                history.mkdir(parents=True, exist_ok=True)
                shutil.copy2(target, history / f"{target.parent.name}_{target.name}")
            temporary = target.with_suffix(target.suffix + ".new")
            shutil.copy2(source_path, temporary)
            os.replace(temporary, target)
            activated.append(str(target.relative_to(self.registry.data_root)).replace("\\", "/"))
        _write_json(history / "activation.json", {
            "recognition_run_id": str(run_id),
            "activated_at": datetime.now().astimezone().isoformat(),
            "targets": activated,
        })
        return {
            "recognition_run_id": str(run_id),
            "approved_resources": approved_count,
            "sentence_labels": sentence_label_count,
            "pending_resources": len(unfinished),
            "activation_policy": (
                "all_human_confirmed"
                if annotation["human_validation"] else
                "full_sentence_labels_pending_decisions_retained"
            ),
            "activated": activated,
            "history": str(history.relative_to(self.registry.data_root)).replace("\\", "/"),
        }


class _StatusProxy:
    def __init__(self, decision: ValidatorDecision):
        self.aggregated_decision = decision


class _HumanReviewGateProvider:
    """Produce traceable review records when the user selects human-only validation."""

    name = "human_review_gate"
    version = "1.0"
    model = "human"
    model_version = "not-applicable"

    def __init__(self, prompt: PromptSnapshot) -> None:
        self.prompt = prompt

    def _result(self, evidence: Mapping[str, Any]) -> ValidatorResult:
        return ValidatorResult(
            validator=self.name,
            validator_version=self.version,
            model=self.model,
            model_version=self.model_version,
            prompt_id=self.prompt.template.prompt_id,
            prompt_version=self.prompt.template.prompt_version,
            decision=ValidatorDecision.REVIEW,
            reason="本次选择仅人工检验，候选已进入人工复核队列",
            evidence={"validation_mode": "human_only", **dict(evidence)},
        )

    def validate_term(self, request: TermValidationRequest) -> ValidatorResult:
        return self._result({"term_id": request.candidate.term_id})

    def validate_topic(self, request: TopicValidationRequest) -> ValidatorResult:
        return self._result({"topic_id": request.candidate.topic_candidate_id})

    def cross_validate(self, request: CrossValidationRequest) -> ValidatorResult:
        result = self._result({
            "document_id": request.document.document_id,
            "term_ids": [item.term_id for item in request.validated_terms],
            "topic_id": request.validated_topic.topic_candidate_id,
        })
        return result.model_copy(update={
            "semantic_consistency": None,
            "evidence": {**result.evidence, "term_supports_topic": None, "topic_supports_terms": None},
        })


class _UnavailableConfiguredProvider:
    """Preserve a configured-but-unavailable validator as traceable evidence."""

    version = "1.0"
    model = "unavailable"
    model_version = "unavailable"

    def __init__(self, name: str, reason: str) -> None:
        self.name = name
        self.reason = reason

    def validate_term(self, request: object) -> None:
        del request
        raise RuntimeError(self.reason)

    def validate_topic(self, request: object) -> None:
        del request
        raise RuntimeError(self.reason)

    def cross_validate(self, request: object) -> None:
        del request
        raise RuntimeError(self.reason)
