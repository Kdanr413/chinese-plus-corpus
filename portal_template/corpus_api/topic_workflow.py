"""FastAPI adapter for the topic-taxonomy construction workflow."""

from __future__ import annotations

import base64
import csv
import hashlib
import io
import json
import math
import os
import shutil
import sys
import threading
from functools import lru_cache
from datetime import datetime
from pathlib import Path
from typing import Any, Callable

from fastapi import APIRouter, Body, HTTPException, Query, Request
from fastapi.responses import Response

from corpus_registry.config import corpus_data_root
from corpus_topic_workflow import WorkflowManager
from corpus_topic_workflow.llm_annotation_api import ModelAPITimeoutError
from corpus_topic_workflow.run_topic_demo import build_units, load_bertopic_config
from corpus_topic_workflow.topic_taxonomy_store import taxonomy_changes_path


router = APIRouter(prefix="/api/workflow", tags=["Topic Taxonomy Workflow"])
PROJECT_ROOT = Path(__file__).resolve().parents[1]
PACKAGE_ROOT = PROJECT_ROOT / "corpus_topic_workflow"
_MANAGER: WorkflowManager | None = None
_MANAGER_KEY: str | None = None
_MANAGER_LOCK = threading.Lock()


def _paths() -> tuple[Path, Path, Path]:
    data = corpus_data_root()
    workspace = data / "workspaces" / "chinese-plus" / "topic-workflow"
    canonical = workspace / "canonical" / "dynamic_topic_taxonomy.json"
    active = data / "curated" / "topics" / "taxonomy.json"
    return workspace, canonical, active


def _recommended_python() -> str:
    """Prefer an explicitly configured or discoverable BERTopic environment."""
    configured = os.environ.get("TOPIC_WORKFLOW_PYTHON", "").strip()
    if configured:
        return configured
    executable = Path(sys.executable).resolve()
    executable_name = "python.exe" if os.name == "nt" else "python"
    sibling = executable.parents[1] / "bertopic_env" / executable_name
    return str(sibling if sibling.exists() else executable)


def _source_profile(path: Path) -> dict[str, Any]:
    if not path.is_file():
        return {"source_path": str(path), "source_unit_count": 0, "sample_size": 0}
    config = load_bertopic_config(PACKAGE_ROOT / "configs" / "bertopic.json")["input"]
    stat=path.stat()
    return dict(_cached_source_profile(str(path.resolve()),stat.st_mtime_ns,stat.st_size,json.dumps(config,sort_keys=True)))

@lru_cache(maxsize=4)
def _cached_source_profile(filename: str, modified: int, size: int, config_json: str) -> dict[str, Any]:
    path=Path(filename)
    config=json.loads(config_json)
    units, excluded, policy = build_units(
        path,
        int(config["min_analysis_characters"]),
        int(config["max_analysis_characters"]),
        config["structural_noise_exact"] if config["exclude_structural_noise"] else [],
        config["structural_noise_regex"] if config["exclude_structural_noise"] else [],
    )
    count = len(units)
    return {
        "source_path": str(path), "source_unit_count": count,
        "sample_size": math.ceil(count / 3) if count else 0,
        "sampling_fraction": 1 / 3, "excluded_count": len(excluded), "policy": policy,
        "source_sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
    }


def _matching_round(manager: WorkflowManager, profile: dict[str, Any]) -> dict[str, Any] | None:
    return next(
        (
            row for row in manager.list_rounds()
            if row.get("source_sha256") == profile.get("source_sha256")
            and int(row.get("sample_size", 0)) == int(profile.get("sample_size", 0))
        ),
        None,
    )


def _matching_published_round(
    manager: WorkflowManager, profile: dict[str, Any]
) -> dict[str, Any] | None:
    return next(
        (
            row for row in manager.list_rounds()
            if row.get("published")
            and row.get("source_sha256") == profile.get("source_sha256")
            and int(row.get("sample_size", 0)) == int(profile.get("sample_size", 0))
        ),
        None,
    )


def _seed_canonical(canonical: Path) -> None:
    canonical.parent.mkdir(parents=True, exist_ok=True)
    seed = PACKAGE_ROOT / "configs" / "dynamic_topic_taxonomy.json"
    if not canonical.exists():
        shutil.copy2(seed, canonical)
    changes = taxonomy_changes_path(canonical)
    seed_changes = PACKAGE_ROOT / "configs" / "dynamic_topic_taxonomy_changes.json"
    if not changes.exists() and seed_changes.exists():
        shutil.copy2(seed_changes, changes)


def workflow_manager() -> WorkflowManager:
    global _MANAGER, _MANAGER_KEY
    workspace, canonical, _ = _paths()
    key = str(workspace.resolve())
    with _MANAGER_LOCK:
        if _MANAGER is None or _MANAGER_KEY != key:
            _seed_canonical(canonical)
            _MANAGER = WorkflowManager(PACKAGE_ROOT, workspace, canonical)
            _MANAGER_KEY = key
    return _MANAGER


def _call(operation: Callable[..., Any], *args: Any, **kwargs: Any) -> Any:
    try:
        return operation(*args, **kwargs)
    except ModelAPITimeoutError as exc:
        raise HTTPException(status_code=504, detail=str(exc)) from exc
    except RuntimeError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


def _download(body: bytes, filename: str, media_type: str) -> Response:
    return Response(
        body,
        media_type=media_type,
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


def _platform_taxonomy(public_taxonomy: dict[str, Any], round_id: str) -> dict[str, Any]:
    topics = public_taxonomy.get("topics") or []
    nodes = []
    for topic in topics:
        topic_id = str(topic.get("topic_id") or "").strip()
        if not topic_id:
            continue
        nodes.append({
            "id": topic_id,
            "name": str(topic.get("name") or "").strip(),
            "parent_id": str(topic.get("parent_id") or "").strip() or None,
            "path": str(topic.get("path") or "").strip(),
            "definition": str(topic.get("definition") or "").strip(),
            "keywords": list(topic.get("keywords") or []),
            "node_type": "controlled",
            "scope_status": str(topic.get("scope_status") or "IN_SCOPE"),
            "status": str(topic.get("status") or "active"),
        })
    return {
        "taxonomy_name": public_taxonomy.get("taxonomy_name", "中文+话题体系"),
        "taxonomy_version": str(public_taxonomy.get("taxonomy_version", "")),
        "status": "test" if public_taxonomy.get("test_only") else "approved",
        "source": (
            "topic_workflow_bertopic_test"
            if public_taxonomy.get("publish_mode") == "bertopic_test"
            else "topic_workflow"
        ),
        "publish_mode": public_taxonomy.get("publish_mode", "reviewed"),
        "test_only": bool(public_taxonomy.get("test_only", False)),
        "source_method": public_taxonomy.get("source_method", "model_and_human_review"),
        "reference_matching_summary": public_taxonomy.get("reference_matching_summary", {}),
        "source_round_id": round_id,
        "activated_at": datetime.now().astimezone().isoformat(timespec="seconds"),
        "nodes": nodes,
    }


def _activate_taxonomy(public_taxonomy: dict[str, Any], round_id: str) -> dict[str, Any]:
    _, _, active = _paths()
    active.parent.mkdir(parents=True, exist_ok=True)
    history = active.parent / "history"
    history.mkdir(parents=True, exist_ok=True)
    backup = None
    if active.exists():
        stamp = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
        backup = history / f"taxonomy_before_{round_id}_{stamp}.json"
        shutil.copy2(active, backup)
    payload = _platform_taxonomy(public_taxonomy, round_id)
    temp = active.with_suffix(".json.tmp")
    temp.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    os.replace(temp, active)
    return {
        "active_taxonomy": str(active),
        "backup": str(backup) if backup else None,
        "node_count": len(payload["nodes"]),
    }


@router.get("/bootstrap", summary="读取话题体系工作台状态")
def bootstrap() -> dict[str, Any]:
    manager = workflow_manager()
    result = manager.bootstrap()
    _, _, active = _paths()
    default_source = corpus_data_root() / "processed" / "corpus" / "jsonl" / "sentences.jsonl"
    profile = _source_profile(default_source)
    matching = _matching_round(manager, profile)
    published_matching = _matching_published_round(manager, profile)
    result.update(
        default_source_path=str(default_source) if default_source.exists() else "",
        platform_taxonomy_path=str(active),
        source_profile=profile,
        matching_round=matching,
        published_matching_round=published_matching,
        ready_for_full_classification=bool(published_matching),
    )
    result.setdefault("execution", {})["python_executable"] = _recommended_python()
    return result


def reuse_approved_reference() -> dict[str, Any]:
    """Import an already approved reference; no clustering or model calls."""
    from scripts.workflow.topic_reference import flatten_published_taxonomy
    from corpus_topic_workflow.workflow_manager import public_taxonomy
    from corpus_topic_workflow.topic_taxonomy_store import write_taxonomy
    config=json.loads((PROJECT_ROOT/"configs/pipeline.json").read_text(encoding="utf-8-sig"))
    relative=config.get("topics",{}).get("reference_taxonomy","")
    path=(PROJECT_ROOT/relative).resolve()
    if not relative or not path.is_relative_to((PROJECT_ROOT/"configs").resolve()) or not path.is_file():
        raise ValueError("请先上传平台导出的已批准参考体系")
    reference=json.loads(path.read_text(encoding="utf-8-sig"))
    if reference.get("status")!="approved" or reference.get("test_only"):
        raise ValueError("只能直接复用已批准的正式体系；测试聚类体系需先审核")
    nodes=flatten_published_taxonomy(reference)
    source=corpus_data_root()/"processed/corpus/jsonl/sentences.jsonl"
    profile=_source_profile(source)
    if not profile["source_unit_count"]:raise ValueError("请先完成语料清洗")
    manager=workflow_manager()
    checksum=hashlib.sha256(path.read_bytes()).hexdigest()
    active_path=_paths()[2]
    active_metadata=json.loads(active_path.read_text(encoding="utf-8-sig")) if active_path.is_file() else {}
    for previous in manager.list_rounds():
        if previous.get("published") and previous.get("reference_sha256")==checksum and previous.get("source_sha256")==profile.get("source_sha256") and active_metadata.get("source_round_id")==previous.get("round_id") and not active_metadata.get("test_only"):
            return {"round":previous,"ready_for_full_classification":True,"reused":True}
    created=manager.create_round(source,sample_size=profile["sample_size"],use_initial_taxonomy=True)
    round_id=created["round_id"];round_dir=manager.round_dir(round_id)
    state=json.loads((round_dir/"baseline_taxonomy.json").read_text(encoding="utf-8-sig"))
    state["taxonomy_version"]=int(reference.get("taxonomy_version") or 1)
    state["taxonomy_name"]=reference.get("taxonomy_name",state.get("taxonomy_name"))
    if manager.canonical_taxonomy.is_file():
        shutil.copy2(manager.canonical_taxonomy,manager.snapshots_root/("before_reference_"+round_id+".json"))
    write_taxonomy(manager.canonical_taxonomy,state)
    snapshot=manager.snapshots_root/("imported_reference_"+round_id+".json")
    write_taxonomy(snapshot,state)
    public=public_taxonomy(state);public.update(source_method="imported_approved_reference",publish_mode="imported_reference",test_only=False)
    activation=_activate_taxonomy(public,round_id)
    created.update(status="published",stage=4,published=True,publish_mode="imported_reference",test_only=False,reference_sha256=checksum,source_reference_version=str(reference.get("taxonomy_version")),source_reference_round=reference.get("source_round_id"),published_at=datetime.now().astimezone().isoformat(),published_taxonomy_version=state["taxonomy_version"])
    manager.write_round(created)
    return {"round":created,"ready_for_full_classification":True,"platform_activation":activation,"node_count":len(nodes),"message":"已复用正式参考体系；现有标注和发布版本保留。下一步从已发布话题体系继续全量分类。"}

@router.post("/reuse-approved-reference",summary="复用已批准参考体系，直接准备全量分类")
def reuse_reference(request:Request):
    from .workbench import require_admin,require_topic_reference_idle
    require_admin(request);require_topic_reference_idle()
    return _call(reuse_approved_reference)

@router.post("/prepare-current", summary="按全量语句的三分之一准备话题体系建设轮次")
def prepare_current(payload: dict[str, Any] = Body(default={})) -> dict[str, Any]:
    manager = workflow_manager()
    source = corpus_data_root() / "processed" / "corpus" / "jsonl" / "sentences.jsonl"
    profile = _source_profile(source)
    if not profile["source_unit_count"]:
        raise HTTPException(status_code=409, detail="当前清洗结果没有可用于话题体系识别的句子")
    existing = _matching_round(manager, profile)
    force_new = bool(payload.get("force_new", True))
    if existing and not force_new:
        return {
            "round": existing, "profile": profile,
            "ready_for_full_classification": bool(existing.get("published")),
            "reused": True, "forced_new": False,
        }
    created = _call(
        manager.create_round,
        source,
        sample_size=int(profile["sample_size"]),
        anchor_ratio=0.30,
        seed=20260817,
        use_initial_taxonomy=True,
    )
    return {
        "round": created, "profile": profile,
        "ready_for_full_classification": False, "reused": False,
        "forced_new": force_new,
        "previous_matching_round_id": existing.get("round_id") if existing else None,
    }


@router.get("/jobs/{job_id}", summary="读取后台任务进度")
def job(job_id: str) -> dict[str, Any]:
    return _call(workflow_manager().job, job_id)


@router.get("/rounds/{round_id}/tasks", summary="读取本轮聚类标注任务")
def tasks(round_id: str) -> dict[str, Any]:
    return {"items": _call(workflow_manager().cluster_tasks, round_id)}


@router.get("/rounds/{round_id}/review-queue", summary="读取人工冲突裁决队列")
def review_queue(round_id: str) -> dict[str, Any]:
    return {"items": _call(workflow_manager().review_queue, round_id)}


@router.get("/rounds/{round_id}/models", summary="读取各机器模型的独立标注结果")
def model_results(round_id: str) -> dict[str, Any]:
    return {"models": _call(workflow_manager().model_results, round_id)}


@router.get("/rounds/{round_id}/models/{model_id}/export", summary="导出单个机器模型结果")
def export_model(round_id: str, model_id: str, format: str = Query("csv", pattern="^(csv|jsonl)$")) -> Response:
    original, flattened = _call(workflow_manager().model_result_export, round_id, model_id)
    if format == "jsonl":
        body = "".join(json.dumps(row, ensure_ascii=False) + "\n" for row in original).encode("utf-8")
        return _download(body, f"{round_id}_{model_id}_cluster_judgments.jsonl", "application/x-ndjson")
    fields = list(flattened[0]) if flattened else ["round_id", "model_id", "cluster_id", "decision"]
    output = io.StringIO(newline="")
    writer = csv.DictWriter(output, fieldnames=fields, extrasaction="ignore")
    writer.writeheader()
    writer.writerows(flattened)
    return _download(("\ufeff" + output.getvalue()).encode("utf-8"), f"{round_id}_{model_id}_cluster_classifications.csv", "text/csv; charset=utf-8")


@router.get("/rounds/{round_id}/validation/export", summary="导出逐句话题验证结果")
def export_validation(round_id: str) -> Response:
    target = workflow_manager().round_dir(round_id) / "validation" / "demo_sentence_validation.csv"
    if not target.exists():
        raise HTTPException(status_code=404, detail="当前轮次尚未生成逐句分层验证结果")
    return _download(target.read_bytes(), f"{round_id}_sentence_validation.csv", "text/csv; charset=utf-8")


@router.get("/export-taxonomy", summary="导出当前动态话题体系")
def export_taxonomy() -> Response:
    manager = workflow_manager()
    return _download(manager.canonical_taxonomy.read_bytes(), "topic_taxonomy.json", "application/json; charset=utf-8")


@router.get("/export-taxonomy-changes", summary="导出话题体系变更记录")
def export_taxonomy_changes() -> Response:
    target = taxonomy_changes_path(workflow_manager().canonical_taxonomy)
    if not target.exists():
        raise HTTPException(status_code=404, detail="当前尚未生成话题体系维护记录")
    return _download(target.read_bytes(), "topic_taxonomy_changes.json", "application/json; charset=utf-8")


@router.post("/upload", status_code=201, summary="上传话题体系建设用 JSONL 语料")
def upload(payload: dict[str, Any] = Body(...)) -> dict[str, str]:
    filename = str(payload.get("filename", ""))
    encoded = str(payload.get("content_base64", ""))
    if not filename or not encoded:
        raise HTTPException(status_code=400, detail="缺少语料文件名或内容")
    try:
        content = base64.b64decode(encoded, validate=True)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail="文件内容不是有效 Base64") from exc
    return {"path": str(_call(workflow_manager().save_upload, filename, content))}


@router.post("/rounds", status_code=201, summary="创建话题体系建设轮次")
def create_round(payload: dict[str, Any] = Body(...)) -> dict[str, Any]:
    return _call(
        workflow_manager().create_round,
        Path(str(payload.get("source_path", ""))),
        sample_size=int(payload.get("sample_size", 800)),
        anchor_ratio=float(payload.get("anchor_ratio", 0.30)),
        seed=int(payload.get("seed", 20260817)),
        use_initial_taxonomy=bool(payload.get("use_initial_taxonomy", True)),
    )


@router.post("/rounds/{round_id}/cluster", status_code=202, summary="启动 BERTopic 候选话题发现")
def cluster(round_id: str, payload: dict[str, Any] = Body(default={})) -> dict[str, Any]:
    return _call(workflow_manager().start_clustering, round_id, str(payload.get("python_executable", "")))


@router.post("/rounds/{round_id}/models", status_code=202, summary="启动一个独立机器标注模型")
def start_model(round_id: str, payload: dict[str, Any] = Body(...)) -> dict[str, Any]:
    return _call(workflow_manager().start_model, round_id, str(payload.get("model_id", "")), dict(payload.get("settings") or {}), str(payload.get("api_key", "")))


@router.post("/rounds/{round_id}/models/{model_id}/resume", status_code=202, summary="从断点继续机器标注")
def resume_model(round_id: str, model_id: str, payload: dict[str, Any] = Body(...)) -> dict[str, Any]:
    return _call(workflow_manager().start_model, round_id, model_id, dict(payload.get("settings") or {}), str(payload.get("api_key", "")), resume=True)


@router.post("/rounds/{round_id}/models/test", summary="测试机器标注模型与提示词")
def test_model(round_id: str, payload: dict[str, Any] = Body(...)) -> dict[str, Any]:
    return _call(workflow_manager().test_model, round_id, dict(payload.get("settings") or {}), str(payload.get("api_key", "")))


@router.post("/rounds/{round_id}/decisions", summary="保存人工冲突裁决")
def save_decision(round_id: str, payload: dict[str, Any] = Body(...)) -> dict[str, Any]:
    return _call(workflow_manager().save_human_decision, round_id, payload)


@router.post("/rounds/{round_id}/publish", summary="发布人工确认的话题体系并激活到平台")
def publish(round_id: str) -> dict[str, Any]:
    result = _call(workflow_manager().publish_round, round_id)
    result["platform_activation"] = _activate_taxonomy(result["taxonomy"], round_id)
    return result


@router.post(
    "/rounds/{round_id}/publish-bertopic-test",
    summary="测试模式：跳过模型匹配，直接发布 BERTopic 聚类",
)
def publish_bertopic_test(round_id: str, payload: dict[str, Any] = Body(...)) -> dict[str, Any]:
    if payload.get("confirm_test_mode") is not True:
        raise HTTPException(status_code=400, detail="必须确认这是跳过外部大模型审核、保留机器匹配证据的草案发布")
    result = _call(workflow_manager().publish_bertopic_test, round_id)
    result["platform_activation"] = _activate_taxonomy(result["taxonomy"], round_id)
    return result


@router.post("/rounds/{round_id}/validate", status_code=202, summary="启动发布体系的逐句验证")
def validate(round_id: str, payload: dict[str, Any] = Body(default={})) -> dict[str, Any]:
    return _call(workflow_manager().start_validation, round_id, str(payload.get("python_executable", "")))


@router.post("/jobs/{job_id}/cancel", status_code=202, summary="取消话题体系后台任务")
def cancel(job_id: str) -> dict[str, Any]:
    return _call(workflow_manager().cancel_job, job_id)


__all__ = ["router", "workflow_manager", "_platform_taxonomy"]
