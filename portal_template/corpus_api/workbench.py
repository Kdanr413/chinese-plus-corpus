"""FastAPI adapter for the established local corpus-processing workbench."""

from __future__ import annotations

import json
import os
import secrets
import threading
from datetime import datetime
from pathlib import Path
from typing import Any

from fastapi import APIRouter, Request
from fastapi.responses import FileResponse, JSONResponse, RedirectResponse

from scripts.ui import server as legacy


PROJECT_ROOT = Path(__file__).resolve().parents[1]
LOCAL_CLIENTS = {"127.0.0.1", "::1", "localhost", "testclient"}
router = APIRouter(tags=["Workbench / 操作台内部接口"])
_OPERATION_LOG_LOCK = threading.Lock()
_OPERATION_LOG_PATH = PROJECT_ROOT / "logs" / "runtime" / "platform-operations.log"

STRUCTURED_ARTIFACTS = (
    ("句法实时进度", "data/processed/index/syntax_tree_progress.json", "当前条目、成功数、失败片段数与更新时间"),
    ("句法已保存回复", "data/processed/index/syntax_tree_responses.jsonl", "逐片段保存的成功及失败记录；中途即可下载"),
    ("句法复用缓存", "data/processed/index/syntax_tree_cache.jsonl", "成功片段的断点续跑缓存，请保留"),
    ("句法失败台账", "data/processed/index/syntax_tree_errors.csv", "历史失败片段；重跑句法步骤会重试失败片段"),
    ("句法处理中间稿", "data/processed/index/corpus_index_syntax_tree.txt.partial", "本轮已完成条目的中间稿，不能作为正式索引输入"),
    (
        "结构化语料",
        "data/processed/index/corpus_index.txt",
        "保留项目特殊标记的结构化语料",
    ),
    (
        "句法树结构化语料",
        "data/processed/index/corpus_index_syntax_tree.txt",
        "供 LangSC.BCC 后续构建索引的 GB18030 转换源",
    ),
    (
        "标注明细",
        "data/processed/index/index_items.jsonl",
        "结构化条目及局部标注明细",
    ),
    (
        "分词与词性明细",
        "data/processed/index/index_item_tokens.jsonl",
        "结构化条目的分词与词性结果",
    ),
)


def _error(message: str, status: int = 400) -> JSONResponse:
    return JSONResponse({"error": message}, status_code=status)


def require_admin(request: Request) -> None:
    """Keep write-capable workbench routes local unless an admin token exists."""
    configured = os.getenv("ADMIN_API_TOKEN", "")
    if configured:
        supplied = request.headers.get("x-admin-token", "")
        if supplied and secrets.compare_digest(supplied, configured):
            return
        raise PermissionError("管理操作需要有效的 X-Admin-Token")
    host = request.client.host if request.client else ""
    if host not in LOCAL_CLIENTS:
        raise PermissionError("管理操作默认只允许从服务器本机访问")


@router.post("/api/operation-log", include_in_schema=False)
async def operation_log(payload: dict[str, Any], request: Request) -> dict[str, bool]:
    """Mirror safe, human-readable workbench activity to the local console log."""
    require_admin(request)
    message = " ".join(str(payload.get("message") or "").split())[:1000]
    level = " ".join(str(payload.get("level") or "操作").split())[:40]
    if not message:
        return {"ok": False}
    timestamp = datetime.now().astimezone().isoformat(timespec="seconds")
    line = f"[{timestamp}] {level}：{message}"
    _OPERATION_LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
    with _OPERATION_LOG_LOCK:
        with _OPERATION_LOG_PATH.open("a", encoding="utf-8") as stream:
            stream.write(line + "\n")
    print(f"WORKBENCH {line}", flush=True)
    return {"ok": True}


def _allowed_file(relative: str) -> Path:
    if relative.replace("\\", "/") == "docs/话题体系构建指导.md":
        relative = "docs/话题审核操作说明.md"
    path = (PROJECT_ROOT / relative).resolve()
    allowed = any(
        legacy.within(path, PROJECT_ROOT / root)
        for root in ("data", "reports", "inputs", "configs", "docs", "参考文件中心")
    )
    if not relative or not allowed:
        raise ValueError("无效文件路径")
    return path


@router.get("/workbench", include_in_schema=False)
def workbench_page() -> RedirectResponse:
    """Keep old bookmarks working while exposing only one human workbench."""
    return RedirectResponse(url="/", status_code=308)


@router.get("/api/bootstrap", summary="读取操作台项目配置、结果摘要和任务状态")
def bootstrap(request: Request) -> dict[str, Any]:
    require_admin(request)
    config = legacy.read_json(legacy.DEFAULT_CONFIG)
    return {
        "project": config.get("project", {}),
        "workflow": config.get("workflow", {}),
        "resources": legacy.resource_summary(legacy.DEFAULT_CONFIG),
        "summary": legacy.result_summary(legacy.DEFAULT_CONFIG),
        "job": legacy.STATE.snapshot(),
    }


@router.get("/api/status", summary="轮询清洗与结构化任务进度")
def status(request: Request) -> dict[str, Any]:
    require_admin(request)
    return legacy.pipeline_status()


@router.get("/api/prompts", summary="读取旧流程兼容提示词配置")
def prompts(request: Request) -> dict[str, Any]:
    require_admin(request)
    return legacy.load_prompt_config(legacy.PROMPT_CONFIG_PATH)


@router.get("/api/raw-corpus", summary="分页查看已上传的原始语料文件")
def raw_corpus(
    request: Request,
    q: str = "",
    source: str = "all",
    status: str = "all",
    page: int = 1,
    page_size: int = 50,
) -> dict[str, Any]:
    require_admin(request)
    return legacy.raw_corpus_table(
        legacy.DEFAULT_CONFIG,
        query=q,
        source=source,
        status=status,
        page=page,
        page_size=page_size,
    )


def require_topic_reference_idle() -> None:
    if legacy.STATE.snapshot().get("running"):
        raise ValueError("加工正在运行，请先停止任务再更换参考体系")
    jobs_path = PROJECT_ROOT / "data/workspaces/chinese-plus/topic-workflow/workflow/jobs.json"
    if any(row.get("status") in {"running", "queued", "cancelling"} for row in (json.loads(jobs_path.read_text(encoding="utf-8-sig")) if jobs_path.exists() else [])):
        raise ValueError("话题任务正在运行，请先停止任务再更换参考体系")


@router.post("/api/topic-reference/remove", summary="移除当前参考话题体系并保留归档备份")
def remove_topic_reference(request: Request) -> dict[str, Any]:
    require_admin(request)
    require_topic_reference_idle()
    from scripts.workflow.topic_reference import remove_reference
    return remove_reference(PROJECT_ROOT)


@router.get("/api/uploaded-resources", summary="查看去重后的上传处理资源原件")
def uploaded_resources(request: Request, category: str = "all") -> dict[str, Any]:
    require_admin(request)
    rows = legacy.uploaded_resource_table(category)
    return {"category": category, "count": len(rows), "items": rows}


@router.get("/api/preview", summary="安全预览项目数据文件的文本内容")
def preview(request: Request, path: str) -> Any:
    require_admin(request)
    target = _allowed_file(path)
    if not target.is_file():
        return _error("文件不存在", 404)
    if target.stat().st_size > 2 * 1024 * 1024:
        text = legacy.read_preview_text(target, byte_limit=60000)[:20000]
        return {"path": path, "text": text, "truncated": True}
    return {"path": path, "text": legacy.read_preview_text(target), "truncated": False}


@router.get("/api/download", summary="下载允许范围内的项目成果文件")
def download(request: Request, path: str) -> FileResponse:
    require_admin(request)
    target = _allowed_file(path)
    if not target.is_file():
        raise FileNotFoundError(target)
    return FileResponse(target, filename=target.name)


@router.get("/api/structured-artifacts", summary="列出步骤5已经生成的结构化成果")
def structured_artifacts(request: Request) -> dict[str, Any]:
    """List current workspace outputs produced by annotation/structuring.

    These are downloadable immediately after step 5. They are deliberately
    separate from an immutable Corpus Release produced by step 6.
    """
    require_admin(request)
    items: list[dict[str, Any]] = []
    for label, relative, description in STRUCTURED_ARTIFACTS:
        target = _allowed_file(relative)
        if not target.is_file():
            continue
        stat = target.stat()
        items.append(
            {
                "label": label,
                "description": description,
                "path": relative,
                "filename": target.name,
                "size_bytes": stat.st_size,
                "updated_at": datetime.fromtimestamp(stat.st_mtime).astimezone().isoformat(),
                "download_url": f"/api/download?path={relative}",
            }
        )
    return {
        "ready": any(item["path"].endswith("corpus_index.txt") for item in items),
        "count": len(items),
        "items": items,
    }


@router.post("/api/upload", summary="上传语料或可选处理资源")
async def upload(
    request: Request,
    category: str,
    filename: str,
    batch_index: int = 0,
    batch_id: str = "",
    source_category: str = "未分类",
    book_title: str = "",
) -> Any:
    try:
        require_admin(request)
        if category == "topic_taxonomy":
            require_topic_reference_idle()
        if category == "corpus":
            from .source_metadata import validate_source
            validate_source(source_category,book_title,require_book=False)
        data = await request.body()
        if len(data) > legacy.MAX_UPLOAD_BYTES:
            raise ValueError("单个文件不得超过 512 MB")
        if batch_index < 0:
            raise ValueError("批次文件序号无效")
        actual_batch = batch_id or datetime.now().strftime("batch_%Y%m%d_%H%M%S")
        result = legacy.Handler.save_upload(
            None,
            category,
            filename,
            data,
            backup_existing=batch_index == 0,
            merge_existing=batch_index > 0,
        )
        if result.get("ok") and category == "corpus":
            from .source_metadata import save_upload_sources
            save_upload_sources(PROJECT_ROOT,result.get("uploaded_files",[]),source_category,book_title)
            result["message"] += "；来源：" + source_category + ("（书名按每个TXT文件名登记）" if source_category == "教材" and not book_title else " · "+book_title if source_category == "教材" else "")
        if result.get("ok") and category in legacy.PRESERVED_UPLOAD_CATEGORIES:
            stored, duplicate = legacy.preserve_uploaded_source(
                category,
                actual_batch,
                batch_index,
                filename,
                data,
                str(result.get("message", "")),
            )
            result["stored_path"] = str(stored.relative_to(PROJECT_ROOT)).replace("\\", "/")
            result["duplicate_upload"] = duplicate
            if duplicate:
                result["message"] += "；相同内容的文件已上传过，本次未重复归档"
        return result
    except PermissionError as exc:
        return _error(str(exc), 403)
    except (ValueError, json.JSONDecodeError) as exc:
        return _error(str(exc))
    except Exception as exc:
        return _error(f"服务器处理失败：{exc}", 500)


@router.post("/api/run", summary="启动旧 Pipeline 的指定加工步骤")
async def run_pipeline(request: Request) -> JSONResponse:
    try:
        require_admin(request)
        payload = await request.json()
        legacy.build_commands(payload, legacy.DEFAULT_CONFIG)
        legacy.validate_run_inputs(payload, legacy.DEFAULT_CONFIG)
        with legacy.STATE.lock:
            if legacy.STATE.running:
                raise ValueError("已有流程正在运行，请等待完成")
            legacy.STATE.running = True
            legacy.STATE.job_id = datetime.now().strftime("%Y%m%d_%H%M%S")
            legacy.STATE.started_at = datetime.now().astimezone().isoformat(timespec="seconds")
            legacy.STATE.finished_at = ""
            legacy.STATE.current_step = "准备运行"
            legacy.STATE.progress = 0
            legacy.STATE.logs = []
            legacy.STATE.error = ""
            legacy.STATE.completed_steps = []
            legacy.STATE.awaiting_review = False
            legacy.STATE.review_kind = ""
        threading.Thread(
            target=legacy.run_job,
            args=(payload, legacy.DEFAULT_CONFIG),
            daemon=True,
            name="corpus-pipeline-job",
        ).start()
        return JSONResponse({"ok": True, "job_id": legacy.STATE.job_id}, status_code=202)
    except PermissionError as exc:
        return _error(str(exc), 403)
    except (ValueError, json.JSONDecodeError) as exc:
        return _error(str(exc))
    except Exception as exc:
        return _error(f"服务器处理失败：{exc}", 500)


@router.post("/api/prompts/save", summary="保存旧流程兼容提示词")
async def save_prompts(request: Request) -> Any:
    try:
        require_admin(request)
        config = legacy.save_prompt_config(legacy.PROMPT_CONFIG_PATH, await request.json())
        return {
            "ok": True,
            "config": config,
            "path": str(legacy.PROMPT_CONFIG_PATH.relative_to(PROJECT_ROOT)).replace("\\", "/"),
        }
    except PermissionError as exc:
        return _error(str(exc), 403)
    except Exception as exc:
        return _error(str(exc))


@router.post("/api/prompts/test", summary="用样例测试旧流程兼容提示词")
async def test_prompt(request: Request) -> Any:
    try:
        require_admin(request)
        return {"ok": True, "result": legacy.execute_prompt_test(await request.json())}
    except PermissionError as exc:
        return _error(str(exc), 403)
    except Exception as exc:
        return _error(str(exc))


@router.post("/api/raw-corpus/{action}", summary="排除、恢复或移出原始语料文件")
async def change_raw_scope(action: str, request: Request) -> Any:
    try:
        require_admin(request)
        if action not in {"exclude", "restore", "select", "unselect"}:
            return _error("未知操作", 404)
        payload = await request.json()
        paths = payload.get("paths") or []
        if not isinstance(paths, list) or not paths:
            raise ValueError("请至少选择一个语料文件")
        result = legacy.update_corpus_exclusions(
            [str(path) for path in paths], action in {"exclude", "unselect"}
        )
        return {"ok": True, "result": result}
    except PermissionError as exc:
        return _error(str(exc), 403)
    except Exception as exc:
        return _error(str(exc))


@router.post("/api/review/continue", summary="完成旧审核门并继续加工流程")
async def continue_review(request: Request) -> Any:
    try:
        require_admin(request)
        payload = await request.json()
        requested = str(payload.get("review_kind", "")).strip()
        if requested not in {"", "topics", "terms"}:
            raise ValueError("未知审核类型")
        if (
            legacy.STATE.awaiting_review
            and requested
            and legacy.STATE.review_kind
            and requested != legacy.STATE.review_kind
        ):
            raise ValueError(f"当前流程正在等待 {legacy.STATE.review_kind} 审核")
        kind = requested or legacy.STATE.review_kind or "topics"
        report = (
            legacy.validate_term_review(publish=True)
            if kind == "terms"
            else legacy.validate_topic_review(publish=True)
        )
        if not report["publishable"]:
            return _error(f"审核尚有 {report['error_count']} 个阻断问题")
        with legacy.STATE.lock:
            resumed = (
                legacy.STATE.running
                and legacy.STATE.awaiting_review
                and legacy.STATE.review_kind == kind
            )
        if resumed:
            legacy.REVIEW_EVENT.set()
        return {"ok": True, "report": report, "workflow_resumed": resumed}
    except PermissionError as exc:
        return _error(str(exc), 403)
    except Exception as exc:
        return _error(str(exc))
