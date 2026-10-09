"""HTTP adapter for the recognition, validation and review workflow."""

from __future__ import annotations

import threading
import uuid
import re
import hashlib
import json
import csv
import io
from datetime import datetime
from pathlib import Path
from typing import Any, Callable

from fastapi import APIRouter, Request
from fastapi.encoders import jsonable_encoder
from fastapi.responses import FileResponse, JSONResponse, PlainTextResponse, Response
from pydantic import BaseModel, ConfigDict, Field

from corpus_recognition import (
    AggregationStrategy,
    BuiltinRuleValidatorProvider,
    CrossValidationRequest,
    Document,
    RecognitionRepository,
    TermCandidate,
    TermValidationRequest,
    TopicCandidate,
    TopicResolution,
    TopicValidationRequest,
)
from corpus_recognition.annotations import (
    annotation_status,
    build_annotation_csv,
    parse_annotation_csv,
)
from corpus_recognition.providers import (
    OpenAICompatibleValidatorProvider,
    ResolvedValidatorSettings,
)
from corpus_recognition.errors import (
    RecognitionWorkflowCancelled,
    ValidatorResponseError,
    ValidatorUnavailableError,
)
from corpus_recognition.config import load_recognition_config
from corpus_recognition.prompts import PromptSnapshot, load_prompt
from corpus_recognition.runtime import (
    RecognitionCoordinator, RecognitionRunOptions, RuntimeValidatorModel,
)
from corpus_recognition.term_intersection import (
    EvaluatedBaselineTermProvider,
    SentenceTermIntersectionProvider,
)
from scripts.ui import server as legacy

from .workbench import require_admin


router = APIRouter(prefix="/api/v1/recognition", tags=["Recognition Workflow"])
PROJECT_ROOT = Path(__file__).resolve().parents[1]


class StrictRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")


class RuntimeValidatorRequest(StrictRequest):
    id: str = Field(min_length=1, max_length=100)
    model: str = Field(min_length=1, max_length=200)
    model_version: str = Field(default="", max_length=200)
    base_url: str = Field(min_length=1, max_length=1000)
    api_key: str = Field(min_length=1, max_length=4000)
    weight: float = Field(default=1.0, gt=0, le=100)


class StartRecognitionRequest(StrictRequest):
    corpus_key: str = Field(default="chinese-plus", min_length=1, max_length=128)
    topic_mode: str = Field(default="auto", pattern=r"^(auto|classification|discovery)$")
    strategy: AggregationStrategy = AggregationStrategy.MAJORITY
    machine_validation: bool = True
    human_validation: bool = True
    required_annotators: int = Field(default=1, ge=1, le=50)
    use_external_model: bool = False
    include_builtin_rules: bool = True
    min_validators: int = Field(default=1, ge=1, le=20)
    cross_validation_enabled: bool = True
    validator_models: list[RuntimeValidatorRequest] = Field(default_factory=list, max_length=20)
    model_name: str = ""
    model_version: str = ""
    base_url: str = ""
    api_key: str = ""
    topic_table_path: str = ""


class HumanReviewRequest(StrictRequest):
    reviewer: str = Field(min_length=1, max_length=160)
    action: str = Field(pattern=r"^(confirm|modify_term|modify_topic|add_topic|merge_topic|reject)$")
    reason: str = Field(min_length=1, max_length=4000)
    after_state: dict[str, Any] = Field(default_factory=dict)


class PromptSentenceTestRequest(StrictRequest):
    sentence: str = Field(min_length=1, max_length=5000)
    candidate: str = Field(default="", max_length=200)
    previous_context: str = Field(default="", max_length=5000)
    next_context: str = Field(default="", max_length=5000)
    topic_level_1: str = Field(default="", max_length=200)
    topic_level_2: str = Field(default="", max_length=200)
    topic_level_3: str = Field(default="", max_length=200)
    topic_path: str = Field(default="", max_length=1000)
    model_name: str = Field(default="", max_length=200)
    model_version: str = Field(default="", max_length=200)
    base_url: str = Field(default="", max_length=1000)
    api_key: str = Field(default="", max_length=4000)
    models: list[RuntimeValidatorRequest] = Field(default_factory=list, max_length=20)
    system_prompt: str = Field(default="", max_length=30000)
    task_instruction: str = Field(default="", max_length=30000)
    topic_reference: str = Field(default="", max_length=30000)
    term_reference: str = Field(default="", max_length=30000)
    strategy: AggregationStrategy = AggregationStrategy.MAJORITY


class PromptUpdateRequest(StrictRequest):
    system_prompt: str = Field(min_length=1, max_length=30000)
    task_instruction: str = Field(min_length=1, max_length=30000)
    change_note: str = Field(default="操作台提示词迭代", min_length=1, max_length=1000)


class RecognitionJobState:
    def __init__(self) -> None:
        self.lock = threading.RLock()
        self.condition = threading.Condition(self.lock)
        self.running = False
        self.status = "idle"
        self.run_id = ""
        self.summary: dict[str, Any] = {}
        self.error = ""
        self.stage = "idle"
        self.progress = 0
        self.detail = "尚未启动"
        self.pause_requested = False
        self.paused = False
        self.cancel_requested = False
        self.active_stage = "idle"
        self.active_detail = "尚未启动"

    def snapshot(self) -> dict[str, Any]:
        with self.lock:
            return {
                "running": self.running, "status": self.status,
                "run_id": self.run_id, "summary": self.summary, "error": self.error,
                "stage": self.stage, "progress": self.progress, "detail": self.detail,
                "paused": self.paused, "pause_requested": self.pause_requested,
                "cancel_requested": self.cancel_requested,
            }

    def checkpoint(self) -> None:
        """Pause only between safe processing units and honour cancellation."""
        with self.condition:
            if self.cancel_requested:
                raise RecognitionWorkflowCancelled("任务已由用户结束")
            while self.pause_requested:
                self.paused = True
                self.status = "paused"
                self.detail = "任务已暂停；继续后将从当前安全检查点运行"
                self.condition.wait(timeout=0.5)
                if self.cancel_requested:
                    raise RecognitionWorkflowCancelled("任务已由用户结束")
            if self.paused:
                self.paused = False
                self.status = "running"
                self.stage = self.active_stage
                self.detail = self.active_detail


STATE = RecognitionJobState()
COORDINATOR_FACTORY: Callable[[], RecognitionCoordinator] = RecognitionCoordinator
REPOSITORY_FACTORY: Callable[[], RecognitionRepository] = RecognitionRepository


def _test_document(sentence: str) -> Document:
    return Document(
        document_id="prompt-test-document",
        source="workbench-prompt-test",
        raw_text=sentence,
        clean_text=sentence.strip(),
        processing_version="prompt-test/1.0",
    )


def _test_candidate(sentence: str, supplied: str) -> str:
    if supplied.strip():
        return supplied.strip()
    values = re.findall(r"[\u4e00-\u9fff]{2,8}", sentence)
    return values[0] if values else sentence.strip()[:20]


def _prompt_snapshots() -> dict[str, Any]:
    config = load_recognition_config(PROJECT_ROOT / "configs" / "recognition.json")
    return {
        "term": load_prompt(PROJECT_ROOT / config.term_model_extraction.prompt, project_root=PROJECT_ROOT),
        "topic": load_prompt(PROJECT_ROOT / config.topic_validation.prompt, project_root=PROJECT_ROOT),
        "cross": load_prompt(PROJECT_ROOT / config.cross_validation.prompt, project_root=PROJECT_ROOT),
    }


def _override_prompt(snapshot: PromptSnapshot, payload: PromptSentenceTestRequest) -> PromptSnapshot:
    if not payload.system_prompt.strip() and not payload.task_instruction.strip():
        return snapshot
    template = snapshot.template.model_copy(update={
        "system_prompt": payload.system_prompt.strip() or snapshot.template.system_prompt,
        "task_instruction": payload.task_instruction.strip() or snapshot.template.task_instruction,
    })
    encoded = json.dumps(template.model_dump(mode="json"), ensure_ascii=False, sort_keys=True).encode("utf-8")
    return snapshot.model_copy(update={
        "checksum_sha256": hashlib.sha256(encoded).hexdigest(), "template": template,
    })


def _next_prompt_version(value: str) -> str:
    parts = value.split(".")
    try:
        numbers = [int(part) for part in parts]
    except ValueError:
        return value + ".1"
    while len(numbers) < 3:
        numbers.append(0)
    numbers[-1] += 1
    return ".".join(str(number) for number in numbers)


def _run(options: RecognitionRunOptions) -> None:
    def update(stage: str, progress: int, detail: str) -> None:
        with STATE.lock:
            STATE.stage = stage
            STATE.active_stage = stage
            STATE.progress = progress
            STATE.detail = detail
            STATE.active_detail = detail
    try:
        def created(run_id: uuid.UUID) -> None:
            with STATE.lock:
                STATE.run_id = str(run_id)

        result = COORDINATOR_FACTORY().run(
            options, progress_callback=update, run_created_callback=created,
            checkpoint_callback=STATE.checkpoint,
        )
        with STATE.lock:
            STATE.run_id = str(result["run_id"])
            STATE.summary = jsonable_encoder(result)
            STATE.status = "succeeded"
    except RecognitionWorkflowCancelled as exc:
        with STATE.lock:
            STATE.status = "cancelled"
            STATE.error = ""
            STATE.detail = str(exc)
    except Exception as exc:
        with STATE.lock:
            STATE.status = "failed"
            STATE.error = str(exc)
            STATE.detail = str(exc)
    finally:
        with STATE.condition:
            STATE.running = False
            STATE.paused = False
            STATE.pause_requested = False
            STATE.cancel_requested = False
            STATE.condition.notify_all()


def _configured_topic_table_path() -> str | None:
    """Return the active published taxonomy, falling back to the uploaded reference.

    Full-corpus classification must consume the taxonomy that the topic workflow
    just published.  The uploaded reference is only a bootstrap input for topic
    construction and must not silently replace an active taxonomy.
    """
    active_relative = "data/curated/topics/taxonomy.json"
    active = (PROJECT_ROOT / active_relative).resolve()
    active.relative_to(PROJECT_ROOT.resolve())
    if active.is_file():
        return active_relative
    config = legacy.read_json(legacy.DEFAULT_CONFIG)
    relative = str(config.get("topics", {}).get("reference_taxonomy") or "").strip()
    if not relative:
        return None
    target = (PROJECT_ROOT / relative).resolve()
    target.relative_to(PROJECT_ROOT.resolve())
    return relative.replace("\\", "/") if target.is_file() else None


def _pending_human_review_scopes(bundle: dict[str, Any]) -> list[dict[str, str]]:
    """Return incomplete or conflicting offline annotation units."""
    status = annotation_status(bundle)
    if not status["human_validation"]:
        return []
    return [
        {"decision_id": decision_id, "scope": scope, "reason": "conflict" if item["conflict"] else "pending"}
        for scope, decisions in status["consensus"].items()
        for decision_id, item in decisions.items()
        if not item["complete"]
    ]


@router.post("/runs", status_code=202, summary="启动完整识别与验证流程")
def start_run(payload: StartRecognitionRequest, request: Request) -> dict[str, Any]:
    require_admin(request)
    with STATE.lock:
        if STATE.running:
            return JSONResponse({"error": "识别验证流程正在运行"}, status_code=409)
        STATE.running = True
        STATE.status = "running"
        STATE.run_id = ""
        STATE.summary = {}
        STATE.error = ""
        STATE.stage = "prepare"
        STATE.progress = 0
        STATE.detail = "正在准备识别任务"
        STATE.active_stage = "prepare"
        STATE.active_detail = "正在准备识别任务"
        STATE.pause_requested = False
        STATE.paused = False
        STATE.cancel_requested = False
    option_data = payload.model_dump()
    option_data["topic_table_path"] = (
        payload.topic_table_path.strip() or _configured_topic_table_path()
    )
    options = RecognitionRunOptions(**option_data)
    threading.Thread(target=_run, args=(options,), daemon=True, name="recognition-workflow").start()
    return {"ok": True, "message": "识别—验证—决策流程已启动"}


@router.get("/status", summary="查看当前识别任务状态")
def current_status(request: Request) -> dict[str, Any]:
    require_admin(request)
    return STATE.snapshot()


@router.post("/control/pause", summary="在安全检查点暂停当前识别任务")
def pause_run(request: Request) -> dict[str, Any]:
    require_admin(request)
    with STATE.condition:
        if not STATE.running:
            return JSONResponse({"error": "当前没有正在运行的识别任务"}, status_code=409)
        STATE.pause_requested = True
        STATE.condition.notify_all()
    return {"ok": True, "message": "已请求暂停；当前处理单元结束后生效"}


@router.post("/control/resume", summary="继续已暂停的识别任务")
def resume_run(request: Request) -> dict[str, Any]:
    require_admin(request)
    with STATE.condition:
        if not STATE.running:
            return JSONResponse({"error": "当前没有可继续的识别任务"}, status_code=409)
        STATE.pause_requested = False
        STATE.paused = False
        STATE.status = "running"
        STATE.stage = STATE.active_stage
        STATE.detail = STATE.active_detail
        STATE.condition.notify_all()
    return {"ok": True, "message": "任务已继续"}


@router.post("/control/cancel", summary="结束当前识别任务并保留审计记录")
def cancel_run(request: Request) -> dict[str, Any]:
    require_admin(request)
    with STATE.condition:
        if not STATE.running:
            return {"ok": True, "message": "当前没有正在运行的识别任务"}
        STATE.cancel_requested = True
        STATE.pause_requested = False
        STATE.condition.notify_all()
    return {"ok": True, "message": "已请求结束；当前处理单元结束后生效"}


@router.get("/prompt-stages", summary="列出分步骤提示词")
def prompt_stages(request: Request) -> dict[str, Any]:
    require_admin(request)
    prompts = _prompt_snapshots()
    cleaning = legacy.load_prompt_config(legacy.PROMPT_CONFIG_PATH)["prompts"]["cleaning_validation"]
    items = [{
        "stage": "cleaning",
        "name": cleaning["name"],
        "prompt_id": "legacy.cleaning_validation",
        "prompt_version": "1.1.0",
        "checksum_sha256": "",
        "system_prompt": cleaning["system_prompt"],
        "task_instruction": cleaning["task_instruction"],
        "objective": "逐段修复格式错误和有明确上下文依据的 OCR 错别字；原始文件不变，禁止润色改写。",
        "required_context": ["当前待清洗文段", "不可改写的安全边界（由系统内置）"],
    }, *[{
        "stage": key,
        "name": snapshot.template.name,
        "prompt_id": snapshot.template.prompt_id,
        "prompt_version": snapshot.template.prompt_version,
        "checksum_sha256": snapshot.checksum_sha256,
        "system_prompt": snapshot.template.system_prompt,
        "task_instruction": snapshot.template.task_instruction,
        "objective": {
            "topic": "同时选择并验证一级、二级和三级话题，检查父子路径是否合法；也允许 NEW_TOPIC 或 UNKNOWN。",
            "term": "让模型逐句独立识别术语，再与经过效果核验的术语基础表取交集。",
            "cross": "双向验证术语集合是否支持话题、话题语境是否支持候选术语。",
        }[key],
        "required_context": {
            "topic": ["当前文本", "一级/二级/三级候选", "含节点 ID、父节点和完整路径的话题体系（允许为空）"],
            "term": ["当前完整句子或完整对话话轮（输入格式由系统内置）"],
            "cross": ["当前文本", "已验证话题", "已验证术语集合", "话题定义"],
        }[key],
    } for key, snapshot in prompts.items()]]
    return {"items": items, "count": len(items)}


@router.post("/prompt-stages/{stage}", summary="保存并版本化本步骤提示词")
def save_stage_prompt(stage: str, payload: PromptUpdateRequest, request: Request) -> dict[str, Any]:
    require_admin(request)
    if stage not in {"cleaning", "topic", "term", "cross"}:
        return JSONResponse({"error": "未知提示词阶段"}, status_code=404)
    if stage == "cleaning":
        config = legacy.load_prompt_config(legacy.PROMPT_CONFIG_PATH)
        template = dict(config["prompts"]["cleaning_validation"])
        template.update({
            "system_prompt": payload.system_prompt.strip(),
            "task_instruction": payload.task_instruction.strip(),
        })
        config["prompts"]["cleaning_validation"] = template
        saved = legacy.save_prompt_config(legacy.PROMPT_CONFIG_PATH, config)
        return {
            "ok": True, "stage": stage, "prompt_version": saved["updated_at"],
            "path": legacy.PROMPT_CONFIG_PATH.relative_to(PROJECT_ROOT).as_posix(),
        }
    config_path = PROJECT_ROOT / "configs" / "recognition.json"
    config_data = json.loads(config_path.read_text(encoding="utf-8-sig"))
    config = load_recognition_config(config_path)
    field = {"topic": "topic_validation", "term": "term_model_extraction", "cross": "cross_validation"}[stage]
    current = load_prompt(PROJECT_ROOT / getattr(config, field).prompt, project_root=PROJECT_ROOT)
    version = _next_prompt_version(current.template.prompt_version)
    template = current.template.model_copy(update={
        "prompt_version": version,
        "system_prompt": payload.system_prompt.strip(),
        "task_instruction": payload.task_instruction.strip(),
        "changelog": f"{version}：{payload.change_note.strip()}",
    })
    directory = (PROJECT_ROOT / current.path).parent
    destination = directory / f"v{version}.json"
    if destination.exists():
        raise ValueError(f"提示词版本文件已存在：{destination.name}")
    with destination.open("x", encoding="utf-8", newline="\n") as handle:
        handle.write(json.dumps(template.model_dump(mode="json"), ensure_ascii=False, indent=2) + "\n")
    config_data[field]["prompt"] = destination.relative_to(PROJECT_ROOT).as_posix()
    temporary = config_path.with_suffix(".json.tmp")
    temporary.write_text(json.dumps(config_data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    load_recognition_config(temporary)
    temporary.replace(config_path)
    return {"ok": True, "stage": stage, "prompt_version": version,
            "path": destination.relative_to(PROJECT_ROOT).as_posix()}


@router.post("/prompt-stages/{stage}/test", summary="只用一条可读句子测试当前步骤提示词")
def test_stage_prompt(
    stage: str, payload: PromptSentenceTestRequest, request: Request
) -> dict[str, Any]:
    require_admin(request)
    if stage not in {"cleaning", "topic", "term", "cross"}:
        return JSONResponse({"error": "未知提示词阶段"}, status_code=404)
    supplied_models = list(payload.models)
    if not supplied_models and payload.model_name and payload.base_url and payload.api_key:
        supplied_models = [RuntimeValidatorRequest(
            id="workbench_prompt_test", model=payload.model_name,
            model_version=payload.model_version, base_url=payload.base_url,
            api_key=payload.api_key, weight=1.0,
        )]
    if stage == "cleaning":
        template = legacy.load_prompt_config(
            legacy.PROMPT_CONFIG_PATH
        )["prompts"]["cleaning_validation"]
        template = {
            **template,
            "system_prompt": payload.system_prompt.strip() or template["system_prompt"],
            "task_instruction": payload.task_instruction.strip() or template["task_instruction"],
        }
        if supplied_models:
            sample_payload = {
                    "task_type": "cleaning_text_review",
                    "paragraph_id": "prompt-test-document",
                    "previous_context": payload.previous_context,
                    "original_text": payload.sentence,
                    "next_context": payload.next_context,
                    "allowed_changes": ["标点和空格类安全修改", "有明确上下文依据的少量 OCR 错别字逐字替换"],
                    "forbidden_changes": ["改写措辞", "改变事实、数字和术语"],
            }
            feedback = []
            for row in supplied_models:
                tested = legacy.execute_prompt_test({
                    "prompt_key": "cleaning_validation", "template": template,
                    "sample_payload": sample_payload,
                    "model": {"name": row.model, "base_url": row.base_url, "api_key": row.api_key},
                })
                parsed = tested.get("parsed") if isinstance(tested.get("parsed"), dict) else {}
                feedback.append({
                    "validator": row.id, "model": row.model,
                    "decision": parsed.get("decision", "uncertain"),
                    "reason": parsed.get("reason") or "清洗提示词已返回有效 JSON",
                    "raw_content": tested.get("raw_content", ""), "details": parsed,
                })
            return {
                "ok": True, "stage": stage, "sentence": payload.sentence,
                "mode": "external_models", "schema_valid": all(row.get("details") is not None for row in feedback),
                "prompt": {"id": "legacy.cleaning_validation", "version": "1.1.0", "system_prompt": template["system_prompt"], "task_instruction": template["task_instruction"]},
                "request_context": sample_payload, "model_feedback": feedback,
                "result": feedback[0],
            }
        return {
            "ok": True, "stage": stage, "sentence": payload.sentence,
            "mode": "builtin_rule", "schema_valid": True,
            "prompt": {"id": "legacy.cleaning_validation", "version": "1.1.0", "system_prompt": template["system_prompt"], "task_instruction": template["task_instruction"]},
            "result": {"decision": "keep", "reason": "确定性清洗规则可运行；未调用外部模型"},
        }
    prompts = _prompt_snapshots()
    prompts[stage] = _override_prompt(prompts[stage], payload)
    document = _test_document(payload.sentence)
    value = _test_candidate(
        payload.sentence,
        payload.topic_level_3 or payload.topic_level_2 or payload.topic_level_1 or payload.candidate,
    )
    term = TermCandidate(
        term_id="prompt-test-term", document_id=document.document_id,
        surface_form=value, normalized_form=value, sentence=payload.sentence,
        extractor="prompt_test", extractor_version="1.0",
    )
    topic = TopicCandidate(
        topic_candidate_id="prompt-test-topic", document_id=document.document_id,
        topic_name=value, topic_definition=f"与“{value}”相关的话题",
        keywords=[value], source_method="prompt_test",
        resolution=TopicResolution.NEW_TOPIC,
        evidence={"proposed_path": {
            "level_1": payload.topic_level_1,
            "level_2": payload.topic_level_2,
            "level_3": payload.topic_level_3 or value,
        }},
    )
    if supplied_models:
        settings = [ResolvedValidatorSettings(
            validator_id=row.id, provider="openai_compatible", model=row.model,
            model_version=row.model_version or row.model,
            base_url=row.base_url, api_key=row.api_key,
        ) for row in supplied_models]
        providers = (
            [SentenceTermIntersectionProvider(row, prompts[stage], [document]) for row in settings]
            if stage == "term" else
            [OpenAICompatibleValidatorProvider(row, prompts[stage]) for row in settings]
        )
        mode = "external_models"
    else:
        providers = (
            [EvaluatedBaselineTermProvider(prompts["term"])]
            if stage == "term" else
            [BuiltinRuleValidatorProvider(prompts["term"], prompts["topic"], prompts["cross"])]
        )
        mode = "builtin_rule"
    try:
        parsed_topic_reference = json.loads(payload.topic_reference) if payload.topic_reference.strip() else []
    except json.JSONDecodeError:
        parsed_topic_reference = [{"reference_text": payload.topic_reference}]
    context = {
        "topic_reference": payload.topic_reference,
        "term_reference": payload.term_reference,
        "hierarchical_selection": {
            "level_1": payload.topic_level_1,
            "level_2": payload.topic_level_2,
            "level_3": payload.topic_level_3,
            "path": payload.topic_path,
        },
        "previous_context": payload.previous_context,
        "next_context": payload.next_context,
    }
    request_value: Any
    results = []
    failures: list[dict[str, Any]] = []
    for provider in providers:
        try:
            if stage == "topic":
                request_value = TopicValidationRequest(
                    document=document, candidate=topic,
                    topic_table=parsed_topic_reference if isinstance(parsed_topic_reference, list) else [parsed_topic_reference],
                    context=context,
                )
                result = provider.validate_topic(request_value)
            elif stage == "term":
                request_value = TermValidationRequest(document=document, candidate=term, context=context)
                result = provider.validate_term(request_value)
            else:
                request_value = CrossValidationRequest(
                    document=document, validated_terms=[term], validated_topic=topic,
                    topic_definition=topic.topic_definition, context=context,
                )
                result = provider.cross_validate(request_value)
            results.append(result)
        except ValidatorUnavailableError as exc:
            failures.append({
                "validator": provider.name, "model": provider.model,
                "status": "unavailable", "error_code": "validator_unavailable",
                "decision": "uncertain", "reason": f"模型服务不可用：{exc}",
            })
        except ValidatorResponseError as exc:
            failures.append({
                "validator": provider.name, "model": provider.model,
                "status": "failed", "error_code": "invalid_model_response",
                "decision": "uncertain", "reason": f"模型返回格式不符合本步骤要求：{exc}",
            })
        except Exception as exc:  # keep one provider failure from becoming an opaque HTTP 500
            failures.append({
                "validator": provider.name, "model": provider.model,
                "status": "failed", "error_code": "model_test_error",
                "decision": "uncertain", "reason": f"模型测试处理失败（{type(exc).__name__}）：{exc}",
            })
    decisions = [row.decision.value for row in results]
    counts = {value: decisions.count(value) for value in ("accept", "review", "reject", "uncertain")}
    if not decisions:
        aggregate = "uncertain"
    elif payload.strategy != AggregationStrategy.UNANIMOUS:
        aggregate = max(counts, key=counts.get)
    else:
        aggregate = decisions[0] if len(set(decisions)) == 1 else "review"
    return {
        "ok": True,
        "stage": stage,
        "sentence": payload.sentence,
        "mode": mode, "strategy": payload.strategy.value,
        "schema_valid": bool(results) and not failures,
        "prompt": {
            "id": prompts[stage].template.prompt_id,
            "version": prompts[stage].template.prompt_version,
            "checksum_sha256": prompts[stage].checksum_sha256,
            "system_prompt": prompts[stage].template.system_prompt,
            "task_instruction": prompts[stage].template.task_instruction,
        },
        "request_context": jsonable_encoder(request_value),
        "model_feedback": [*jsonable_encoder(results), *failures],
        "result": {
            "decision": aggregate,
            "reason": f"{len(results)} 个有效反馈，{len(failures)} 个失败：{counts}",
            "counts": counts, "failure_count": len(failures),
        },
    }


@router.get("/runs", summary="列出历史识别任务")
def list_runs(request: Request, limit: int = 20) -> dict[str, Any]:
    require_admin(request)
    rows = REPOSITORY_FACTORY().list_runs(limit=max(1, min(limit, 100)))
    return {"items": jsonable_encoder(rows), "count": len(rows)}


@router.get("/runs/{run_id}", summary="查看识别任务摘要或小规模完整证据")
def run_detail(
    run_id: uuid.UUID,
    request: Request,
    include_evidence: bool = False,
) -> Any:
    require_admin(request)
    repository = REPOSITORY_FACTORY()
    run = repository.get_run(run_id)
    counts = repository.get_run_counts(run_id)
    if not include_evidence:
        return {
            "run": jsonable_encoder(run),
            "counts": counts,
            "detail_mode": "summary",
            "evidence_hint": "使用 artifacts/exports 下载阶段成果；小规模任务可显式设置 include_evidence=true。",
        }
    evidence_rows = (
        counts["documents"] + counts["term_candidates"]
        + counts["topic_candidates"] + counts["resource_decisions"]
    )
    if evidence_rows > 20_000:
        return JSONResponse(
            {
                "error": "该任务证据量过大，禁止一次性返回全部记录；请使用 artifacts/exports 分阶段下载。",
                "counts": counts,
                "safe_row_limit": 20_000,
            },
            status_code=413,
        )
    bundle = repository.get_run_bundle(run_id)
    _hydrate_document_text(bundle)
    topic_path = str(bundle["run"].get("config_snapshot", {}).get("topic_table_path") or "")
    topic_target = (PROJECT_ROOT / topic_path).resolve() if topic_path else None
    if topic_target is not None:
        topic_target.relative_to(PROJECT_ROOT.resolve())
    bundle["topic_table_status"] = {
        "path": topic_path,
        "exists": bool(topic_target and topic_target.is_file()),
        "is_current_workbench_reference": topic_path == _configured_topic_table_path(),
    }
    bundle["annotation_status"] = annotation_status(bundle)
    return jsonable_encoder(bundle)


def _hydrate_document_text(bundle: dict[str, Any]) -> None:
    """Load immutable Workspace text assets for API/CSV presentation."""
    data_root = COORDINATOR_FACTORY().registry.data_root.resolve()
    for document in bundle["documents"]:
        for field, asset_field in (("raw_text", "raw_asset_path"), ("clean_text", "clean_asset_path")):
            path = (data_root / str(document[asset_field])).resolve()
            path.relative_to(data_root)
            document[field] = path.read_text(encoding="utf-8") if path.is_file() else ""


ANNOTATION_GUIDE = """# 话题与术语离线标注说明

## 基本规则

1. 每位标注员分别下载话题和术语 CSV，不要删除、改名或调整系统列。
2. 每个文件可以由一位标注员填写全部行；`annotator` 填写稳定且可区分的姓名或编号。
3. `decision` 只允许：`confirm`（确认机器候选）、`modify`（修改）或 `reject`（拒绝本条）。
4. `modify` 时必须填写 `final_value`；`confirm` 时可留空，系统将采用 `machine_candidate`。
5. `reason` 必填，应说明正文证据或修改依据。
6. 文件必须保存为 UTF-8 CSV。不同标注员分别上传自己的文件，不要合并覆盖。

## 系统只读列

- `text`：当前标注单元的清洗后正文；记叙文是一句话，对话体是一个完整话轮。
- `machine_candidate`：候选生成器给出的初步结果，不等于最终正确答案。
- `candidate_method`：候选来自参考话题体系关键词匹配、术语统计抽取或其他识别器。
- `candidate_evidence`：候选命中的参考节点、关键词、分值或抽取依据。
- `model_evidence`：规则验证器或外部模型对候选的独立意见。

启用人工标注后，系统在话题和术语机器结果生成后暂停。只有两类标注均达到人数要求并形成共识，才会开始话题—术语交叉检验。

## 话题标注

- 标注单位是一个完整句子或完整对话话轮。
- `final_value` 建议填写完整层级路径，例如：`新能源 > 太阳能 > 光伏发电`。
- 无法归入参考体系时，可以填写 `UNKNOWN` 或明确的新话题名称。

## 术语标注

- 多个术语用中文逗号 `，` 分隔。
- 普通高频词、残缺词组和无领域意义的表达不应保留。

## 多标注员汇总

- 系统按“每条至少需要几位标注员”的任务配置统计独立标注员。
- 同一标注员重复上传时，以其最新提交为准，旧记录仍保留用于追溯。
- 达到人数后按最终值多数一致汇总；无法形成多数意见时标记为分歧，阻止最终发布。
"""


@router.get("/annotation-guide", summary="下载离线人工标注说明")
def annotation_guide(request: Request) -> PlainTextResponse:
    require_admin(request)
    return PlainTextResponse(
        ANNOTATION_GUIDE,
        media_type="text/markdown; charset=utf-8",
        headers={"Content-Disposition": 'attachment; filename="annotation_guide.md"'},
    )


@router.get("/runs/{run_id}/annotations/{scope}/template", summary="下载话题或术语人工标注任务 CSV")
def annotation_template(run_id: uuid.UUID, scope: str, request: Request) -> Response:
    require_admin(request)
    bundle = REPOSITORY_FACTORY().get_run_bundle(run_id)
    _hydrate_document_text(bundle)
    try:
        content = build_annotation_csv(bundle, scope)
    except ValueError as exc:
        return JSONResponse({"error": str(exc)}, status_code=400)
    return Response(
        content=content,
        media_type="text/csv; charset=utf-8",
        headers={"Content-Disposition": f'attachment; filename="{scope}_annotation_{run_id}.csv"'},
    )


def _finalize_annotation_consensus(run_id: uuid.UUID) -> dict[str, Any]:
    repository = REPOSITORY_FACTORY()
    bundle = repository.get_run_bundle(run_id)
    _hydrate_document_text(bundle)
    status = annotation_status(bundle)
    if not status["human_validation"] or not status["ready"]:
        return status
    decisions = {str(row["id"]): row for row in bundle["resource_decisions"]}
    for decision_id, decision in decisions.items():
        topic = status["consensus"]["topic"][decision_id]
        term = status["consensus"]["term"][decision_id]
        values = {"topics": topic["value"], "terms": term["value"]}
        rejected = "__REJECT__" in values.values()
        signature = hashlib.sha256(json.dumps({
            "values": values,
            "topic_reviewers": topic["reviewers"],
            "term_reviewers": term["reviewers"],
        }, ensure_ascii=False, sort_keys=True).encode("utf-8")).hexdigest()
        already = any(
            str(row["resource_decision_id"]) == decision_id
            and (row.get("after_state") or {}).get("annotation_consensus_signature") == signature
            for row in bundle["human_reviews"]
        )
        if already:
            continue
        repository.apply_human_review(
            decision["id"], reviewer="annotation-consensus",
            action="reject" if rejected else "confirm",
            after_state={
                "review_scope": "aggregate",
                "annotation_aggregate": True,
                "annotation_consensus_signature": signature,
                "topics": "" if values["topics"] == "__REJECT__" else values["topics"],
                "terms": "" if values["terms"] == "__REJECT__" else values["terms"],
                "topic_reviewers": topic["reviewers"],
                "term_reviewers": term["reviewers"],
            },
            reason="离线人工标注达到人数要求并形成多数一致结果",
        )
    COORDINATOR_FACTORY().refresh_approved(run_id)
    return annotation_status(repository.get_run_bundle(run_id))


@router.post("/runs/{run_id}/annotations/{scope}", summary="上传话题或术语人工标注 CSV")
async def upload_annotations(run_id: uuid.UUID, scope: str, request: Request) -> dict[str, Any]:
    require_admin(request)
    data = await request.body()
    if len(data) > 32 * 1024 * 1024:
        return JSONResponse({"error": "标注文件不得超过 32 MB"}, status_code=413)
    repository = REPOSITORY_FACTORY()
    bundle = repository.get_run_bundle(run_id)
    if not bool(bundle["run"].get("config_snapshot", {}).get("human_validation")):
        return JSONResponse({"error": "本任务未启用人工检验，不能上传人工标注文件"}, status_code=409)
    try:
        rows = parse_annotation_csv(data, run_id=run_id, scope=scope)
    except ValueError as exc:
        return JSONResponse({"error": str(exc)}, status_code=400)
    decisions = {str(row["id"]): row for row in bundle["resource_decisions"]}
    unknown = sorted({row["decision_id"] for row in rows if row["decision_id"] not in decisions})
    if unknown:
        return JSONResponse({"error": f"标注文件包含 {len(unknown)} 个未知任务 ID"}, status_code=400)
    template_rows = {
        row["decision_id"]: row
        for row in csv.DictReader(io.StringIO(build_annotation_csv(bundle, scope).decode("utf-8-sig")))
    }
    protected_fields = (
        "run_id", "scope", "decision_id", "document_id", "analysis_unit_type",
        "text", "machine_candidate", "candidate_method", "candidate_evidence", "model_evidence",
    )
    for row in rows:
        expected = template_rows[row["decision_id"]]
        changed = [field for field in protected_fields if row[field] != str(expected.get(field) or "").strip()]
        if changed:
            return JSONResponse(
                {"error": f"任务 {row['decision_id']} 的系统列被修改：{', '.join(changed)}；请重新下载模板"},
                status_code=400,
            )
    value_key = "topics" if scope == "topic" else "terms"
    for row in rows:
        value = row["final_value"] if row["decision"] == "modify" else template_rows[row["decision_id"]]["machine_candidate"]
        action = "reject" if row["decision"] == "reject" else (
            "modify_topic" if scope == "topic" and row["decision"] == "modify" else
            "modify_term" if scope == "term" and row["decision"] == "modify" else "confirm"
        )
        repository.add_human_review(
            decisions[row["decision_id"]]["id"],
            reviewer=row["annotator"], action=action,
            before_state={"machine_candidate": row["machine_candidate"], "scope": scope},
            after_state={
                "review_scope": scope,
                "annotation_method": "csv_upload",
                value_key: value,
            },
            reason=row["reason"],
        )
    status = _finalize_annotation_consensus(run_id)
    return {"ok": True, "imported_rows": len(rows), "annotation_status": jsonable_encoder(status)}


@router.post("/decisions/{decision_id}/review", summary="提交人工复核并保留来源记录")
def review_decision(
    decision_id: uuid.UUID, payload: HumanReviewRequest, request: Request
) -> dict[str, Any]:
    require_admin(request)
    repository = REPOSITORY_FACTORY()
    result = repository.apply_human_review(
        decision_id, reviewer=payload.reviewer, action=payload.action,
        after_state=payload.after_state, reason=payload.reason,
    )
    run_id = result["decision"]["recognition_run_id"]
    approved = COORDINATOR_FACTORY().refresh_approved(run_id)
    return {"ok": True, "result": jsonable_encoder(result), "approved_count": len(approved)}


@router.post("/runs/{run_id}/activate", summary="确认审核完成并供结构化步骤使用")
def activate_run(run_id: uuid.UUID, request: Request) -> dict[str, Any]:
    require_admin(request)
    repository = REPOSITORY_FACTORY()
    run = repository.get_run(run_id)
    pending = (
        _pending_human_review_scopes(repository.get_run_bundle(run_id))
        if bool((run.get("config_snapshot") or {}).get("human_validation"))
        else []
    )
    if pending:
        return JSONResponse(
            {"error": f"仍有 {len(pending)} 项话题/术语人工核验未完成，不能进入结构化"},
            status_code=409,
        )
    try:
        result = COORDINATOR_FACTORY().activate_approved(run_id)
    except (ValueError, FileNotFoundError) as exc:
        return JSONResponse({"error": str(exc)}, status_code=409)
    return {"ok": True, "result": result}


@router.get("/runs/{run_id}/approved", summary="导出已确认教学资源")
def download_approved(run_id: uuid.UUID, request: Request) -> FileResponse:
    require_admin(request)
    run = REPOSITORY_FACTORY().get_run(run_id)
    path = (
        COORDINATOR_FACTORY().registry.data_root
        / str(run["workspace_path"])
        / "recognition" / "08_approved_resources" / "resources.jsonl"
    ).resolve()
    data_root = COORDINATOR_FACTORY().registry.data_root.resolve()
    path.relative_to(data_root)
    if not path.is_file():
        raise FileNotFoundError(path)
    return FileResponse(path, filename=f"approved_resources_{run_id}.jsonl")


@router.get("/runs/{run_id}/artifacts/{kind}", summary="下载识别流程阶段表")
def download_artifact(run_id: uuid.UUID, kind: str, request: Request) -> FileResponse:
    require_admin(request)
    choices = {
        "topic-table": ("03_topic_validation/topic_validation_table.csv", "topic_validation_table.csv"),
        "term-table": ("05_term_validation/term_validation_table.csv", "term_validation_table.csv"),
        "cross": (("06_term_topic_cross_validation/results.jsonl"), "cross_validation_results.jsonl"),
        "final-taxonomy": ("08_approved_resources/taxonomy.json", "taxonomy.json"),
        "final-terminology": ("08_approved_resources/terminology.csv", "terminology.csv"),
    }
    if kind not in choices:
        return JSONResponse({"error": "未知阶段结果类型"}, status_code=404)
    if kind in {"final-taxonomy", "final-terminology"}:
        bundle = REPOSITORY_FACTORY().get_run_bundle(run_id)
        pending = _pending_human_review_scopes(bundle)
        if pending:
            return JSONResponse(
                {"error": f"仍有 {len(pending)} 项话题/术语人工核验未完成，最终表尚未定稿"},
                status_code=409,
            )
    relative, filename = choices[kind]
    run = REPOSITORY_FACTORY().get_run(run_id)
    data_root = COORDINATOR_FACTORY().registry.data_root.resolve()
    path = (data_root / str(run["workspace_path"]) / "recognition" / relative).resolve()
    path.relative_to(data_root)
    if not path.is_file():
        raise FileNotFoundError(path)
    if kind == "topic-table":
        path = _topic_table_with_text(path)
    return FileResponse(path, filename=filename)


@router.get("/runs/{run_id}/exports/{asset}", summary="导出识别阶段表格")
def download_recognition_asset(
    run_id: uuid.UUID, asset: str, request: Request
) -> FileResponse:
    require_admin(request)
    assets = {
        "topic-table": ("03_topic_validation/topic_validation_table.csv", "topic_validation_table.csv"),
        "term-table": ("05_term_validation/term_validation_table.csv", "term_validation_table.csv"),
        "approved-topics": ("08_approved_resources/taxonomy.json", "taxonomy.json"),
        "approved-terms": ("08_approved_resources/terminology.csv", "terminology.csv"),
    }
    if asset not in assets:
        return JSONResponse({"error": "未知导出类型"}, status_code=404)
    if asset in {"approved-topics", "approved-terms"}:
        pending = _pending_human_review_scopes(REPOSITORY_FACTORY().get_run_bundle(run_id))
        if pending:
            return JSONResponse(
                {"error": f"仍有 {len(pending)} 项话题/术语人工核验未完成，最终表尚未定稿"},
                status_code=409,
            )
    run = REPOSITORY_FACTORY().get_run(run_id)
    relative, filename = assets[asset]
    data_root = COORDINATOR_FACTORY().registry.data_root.resolve()
    path = (data_root / str(run["workspace_path"]) / "recognition" / relative).resolve()
    path.relative_to(data_root)
    if not path.is_file():
        raise FileNotFoundError(path)
    if asset == "topic-table":
        path = _topic_table_with_text(path)
    return FileResponse(path, filename=filename)


def _topic_table_with_text(path: Path) -> Path:
    """Hydrate legacy per-sentence topic tables with the original sentence text."""
    with path.open("r", encoding="utf-8-sig", newline="") as handle:
        rows = list(csv.DictReader(handle))
        fieldnames = list(rows[0].keys()) if rows else []
    if "text" in fieldnames and all(str(row.get("text") or "").strip() for row in rows):
        return path
    documents_path = path.parents[1] / "01_preprocessed" / "documents.jsonl"
    text_by_id: dict[str, str] = {}
    if documents_path.is_file():
        with documents_path.open("r", encoding="utf-8-sig") as handle:
            for line in handle:
                if not line.strip():
                    continue
                document = json.loads(line)
                text_by_id[str(document.get("document_id") or "")] = str(
                    document.get("clean_text") or document.get("raw_text") or ""
                )
    hydrated = path.with_name("topic_validation_table_with_text.csv")
    output_fields = ["text", *[field for field in fieldnames if field != "text"]]
    with hydrated.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=output_fields, extrasaction="ignore")
        writer.writeheader()
        for row in rows:
            row["text"] = str(row.get("text") or text_by_id.get(str(row.get("document_id") or ""), ""))
            writer.writerow(row)
    return hydrated
