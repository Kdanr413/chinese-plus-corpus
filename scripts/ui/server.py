#!/usr/bin/env python3
"""Local browser console for the corpus workflow; standard-library only."""

from __future__ import annotations

import argparse
import csv
import hashlib
import io
import json
import mimetypes
import os
import re
import socket
import subprocess
import sys
import threading
import time
import urllib.parse
import urllib.request
import webbrowser
import zipfile
from datetime import datetime
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any


class ExclusiveThreadingHTTPServer(ThreadingHTTPServer):
    """Prevent multiple workbench versions from sharing the same Windows port."""

    allow_reuse_address = False

    def server_bind(self) -> None:
        if os.name == "nt" and hasattr(socket, "SO_EXCLUSIVEADDRUSE"):
            self.socket.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
        super().server_bind()


PROJECT_ROOT = Path(__file__).resolve().parents[2]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from scripts.workflow.prompting import build_messages, chat_completions_url, load_prompt_config, parse_json_content, save_prompt_config, validate_prompt_config
from scripts.workflow.structure_reuse import (
    load_manifest as load_structure_reuse_manifest,
    resource_snapshot as structure_resource_snapshot,
    reuse_status as structure_reuse_status,
)


WEB_ROOT = Path(__file__).resolve().parent / "web"
DEFAULT_CONFIG = PROJECT_ROOT / "configs" / "pipeline.json"
MAX_UPLOAD_BYTES = 512 * 1024 * 1024
USER_UPLOAD_ROOT = PROJECT_ROOT / "data" / "raw" / "reference_uploads"
USER_UPLOAD_MANIFEST = USER_UPLOAD_ROOT / "upload_manifest.csv"
USER_UPLOAD_LOCK = threading.Lock()
CORPUS_EXCLUSION_PATH = PROJECT_ROOT / "data" / "raw" / "corpus_exclusions.csv"
PROMPT_CONFIG_PATH = PROJECT_ROOT / "configs" / "llm" / "prompts.json"
PROMPT_TEST_ROOT = PROJECT_ROOT / "reports" / "prompt_tests"
PROMPT_KEYS = {"cleaning_validation", "topic_mapping", "taxonomy_validation", "term_validation"}

UPLOAD_TARGETS = {
    "topic_taxonomy": PROJECT_ROOT / "configs" / "topics" / "new_energy_candidate_taxonomy.json",
    "domain_lexicon": PROJECT_ROOT / "inputs" / "linguistic" / "domain_lexicon.csv",
    "terminology": PROJECT_ROOT / "inputs" / "linguistic" / "terminology.csv",
    "segmentation_notes": PROJECT_ROOT / "inputs" / "linguistic" / "segmentation_notes.csv",
    "jieba_user_dict": PROJECT_ROOT / "inputs" / "linguistic" / "jieba_user_dict.txt",
    "stop_terms": PROJECT_ROOT / "inputs" / "linguistic" / "stop_terms.txt",
    "reference_documents": PROJECT_ROOT / "inputs" / "domain" / "reference_documents.csv",
    "few_shot_examples": PROJECT_ROOT / "inputs" / "term_extraction" / "few_shot_examples.jsonl",
}
TEMPLATE_ROOT = PROJECT_ROOT / "参考文件中心" / "模板"
TEMPLATE_FILES = {
    "topic_taxonomy": "参考话题体系.json", "domain_lexicon": "领域词表.csv",
    "terminology": "规范术语表.csv", "segmentation_notes": "分词规则表.csv",
    "jieba_user_dict": "Jieba用户词典.txt", "stop_terms": "排除术语表.txt",
    "reference_documents": "典型领域文件登记.csv", "few_shot_examples": "术语抽取示例.jsonl",
}
UPLOAD_SCHEMAS = {
    "domain_lexicon": {"term", "category", "force_keep", "priority", "status"},
    "terminology": {"term_id", "preferred_term", "status", "priority"},
    "segmentation_notes": {"rule_id", "target", "action", "result", "status"},
    "reference_documents": {"reference_id", "title", "path_or_url", "document_type", "status"},
}
DOMAIN_LEXICON_FIELDS = ["term", "category", "force_keep", "priority", "status", "source", "notes"]
TERMINOLOGY_FIELDS = ["term_id", "preferred_term", "aliases", "english_name", "abbreviation", "status", "priority", "notes"]
SIMPLE_WORD_HEADERS = {"term", "word", "name", "preferred_term", "词", "词语", "术语", "名称", "规范词"}
PRESERVED_UPLOAD_CATEGORIES = set(UPLOAD_TARGETS)
REVIEW_DIR = PROJECT_ROOT / "data" / "review" / "topics"
REVIEW_PATH = REVIEW_DIR / "taxonomy_review_workbench.csv"
REVIEW_REPORT_PATH = PROJECT_ROOT / "data" / "curated" / "topics" / "taxonomy_validation_report.json"
REVIEW_SOURCE_MANIFEST_PATH = REVIEW_DIR / "review_source_manifest.json"
CURATED_TAXONOMY_PATH = PROJECT_ROOT / "data" / "curated" / "topics" / "taxonomy.json"


def read_preview_text(path: Path, byte_limit: int | None = None) -> str:
    """Read UTF-8 management files or GB18030 final corpus files for preview."""
    with path.open("rb") as handle:
        raw = handle.read() if byte_limit is None else handle.read(byte_limit)
    if raw.startswith(b"\xef\xbb\xbf"):
        return raw.decode("utf-8-sig", errors="replace")
    try:
        return raw.decode("utf-8")
    except UnicodeDecodeError:
        return raw.decode("gb18030", errors="replace")


TOPIC_REUSE_MANIFEST_PATH = PROJECT_ROOT / "data" / "processed" / "topics" / "topic_reuse_manifest.json"
TERM_REVIEW_PATH = PROJECT_ROOT / "data" / "review" / "terms" / "term_review_workbench.csv"
TERM_REVIEW_REPORT_PATH = PROJECT_ROOT / "data" / "curated" / "terms" / "term_validation_report.json"
CURATED_TERMS_PATH = PROJECT_ROOT / "data" / "curated" / "terms" / "terminology.csv"
REVIEW_EVENT = threading.Event()
REVIEW_HUMAN_FIELDS = ["human_decision", "approved_node_id", "approved_label", "approved_parent_id", "merge_target_id", "split_plan", "definition", "include_scope", "exclude_scope", "reviewer", "review_notes"]
REVIEW_DERIVED_FIELDS = ["duplicate_label_count", "duplicate_label_candidates", "duplicate_label_explanation"]
REVIEW_ALLOWED = {
    "controlled_seed_node": {"approve", "rename", "reparent", "rename_reparent", "reject", "defer"},
    "discovered_cluster": {"add", "map_existing", "merge", "split", "reject", "defer"},
}
REVIEW_DECISION_REQUIRED_FIELDS = {
    "approve": {"definition", "include_scope", "exclude_scope"},
    "rename": {"approved_label", "definition", "include_scope", "exclude_scope"},
    "reparent": {"approved_parent_id", "definition", "include_scope", "exclude_scope"},
    "rename_reparent": {"approved_label", "approved_parent_id", "definition", "include_scope", "exclude_scope"},
    "reject": set(),
    "defer": set(),
    "add": {"approved_node_id", "approved_label", "approved_parent_id", "definition", "include_scope", "exclude_scope"},
    "map_existing": {"merge_target_id"},
    "merge": {"merge_target_id"},
    "split": {"split_plan"},
}
REVIEW_DECISION_USED_FIELDS = {
    decision: fields | {"human_decision", "reviewer", "review_notes"}
    for decision, fields in REVIEW_DECISION_REQUIRED_FIELDS.items()
}


def read_json(path: Path) -> dict[str, Any]:
    try:
        return json.loads(path.read_text(encoding="utf-8-sig"))
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def line_count(path: Path) -> int:
    if not path.exists():
        return 0
    with path.open("r", encoding="utf-8-sig", errors="ignore") as handle:
        return sum(bool(line.strip()) for line in handle)


def prepare_prompt_update(prompt_key: str, template: Any) -> dict[str, Any]:
    if prompt_key not in PROMPT_KEYS:
        raise ValueError("未知提示词类型")
    current = load_prompt_config(PROMPT_CONFIG_PATH)
    current["prompts"][prompt_key] = template
    return validate_prompt_config(current)


def _execute_prompt_test_once(payload: dict[str, Any]) -> dict[str, Any]:
    prompt_key = str(payload.get("prompt_key", ""))
    config = prepare_prompt_update(prompt_key, payload.get("template"))
    task = payload.get("sample_payload")
    if not isinstance(task, dict):
        raise ValueError("测试任务必须是 JSON 对象")
    model = payload.get("model") or {}
    model_name = str(model.get("name", "")).strip()
    api_key = str(model.get("api_key", "")).strip()
    base_url = str(model.get("base_url", "")).strip() or "https://api.openai.com/v1"
    if not model_name or not api_key:
        raise ValueError("测试提示词必须填写模型名称和 API Key")
    messages = build_messages(config["prompts"][prompt_key], task)
    request_body = {"model": model_name, "temperature": 0.1, "messages": messages}
    request = urllib.request.Request(chat_completions_url(base_url), data=json.dumps(request_body, ensure_ascii=False).encode("utf-8"), headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"}, method="POST")
    started = time.perf_counter()
    with urllib.request.urlopen(request, timeout=180) as response:
        response_payload = json.loads(response.read().decode("utf-8"))
    raw_content = str(response_payload["choices"][0]["message"]["content"])
    parsed: Any = None
    parse_error = ""
    try:
        parsed = parse_json_content(raw_content)
    except (json.JSONDecodeError, TypeError, ValueError) as exc:
        parse_error = str(exc)
    record = {
        "tested_at": datetime.now().astimezone().isoformat(timespec="seconds"), "prompt_key": prompt_key,
        "model": model_name, "base_url": base_url, "elapsed_seconds": round(time.perf_counter() - started, 3),
        "json_valid": not parse_error, "parse_error": parse_error, "request_messages": messages,
        "sample_payload": task, "raw_content": raw_content, "parsed": parsed, "usage": response_payload.get("usage", {}),
    }
    PROMPT_TEST_ROOT.mkdir(parents=True, exist_ok=True)
    output = PROMPT_TEST_ROOT / f"prompt_test_{datetime.now().strftime('%Y%m%d_%H%M%S_%f')}_{prompt_key}.json"
    output.write_text(json.dumps(record, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    record["report_path"] = str(output.relative_to(PROJECT_ROOT)).replace("\\", "/")
    return record


def execute_prompt_test(payload):
    try:return _execute_prompt_test_once(payload)
    except Exception as error:
        model=payload.get('model') or {}
        if model.get('name') and model.get('api_key'):
            from scripts.workflow.model_errors import ModelErrors
            template=payload.get('template') or {}
            messages=build_messages(template,payload.get('sample_payload') or {}) if all(k in template for k in ('system_prompt','task_instruction','output_schema')) else []
            ModelErrors(PROJECT_ROOT).record('prompt_test',chat_completions_url(model.get('base_url') or 'https://api.openai.com/v1'),{'model':model['name'],'temperature':0.1,'messages':messages},error,model['api_key'])
        raise


def read_csv_rows(path: Path) -> tuple[list[str], list[dict[str, str]]]:
    if not path.exists():
        return [], []
    with path.open("r", encoding="utf-8-sig", newline="") as handle:
        reader = csv.DictReader(handle)
        return list(reader.fieldnames or []), [dict(row) for row in reader]


def write_csv_rows(path: Path, fields: list[str], rows: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)


def preserve_uploaded_source(
    category: str,
    batch_id: str,
    batch_index: int,
    filename: str,
    data: bytes,
    message: str,
) -> tuple[Path, bool]:
    """Archive one unique source file; return its path and duplicate status."""
    safe_batch = re.sub(r"[^0-9A-Za-z_-]", "_", batch_id).strip("_") or datetime.now().strftime("batch_%Y%m%d_%H%M%S")
    relative = safe_relative_path(filename)
    stored = USER_UPLOAD_ROOT / category / safe_batch / f"{batch_index + 1:03d}_{relative.name}"
    if not within(stored, USER_UPLOAD_ROOT):
        raise ValueError("上传归档路径无效")
    manifest_fields = ["category", "batch_id", "batch_index", "original_name", "stored_path", "bytes", "sha256", "imported_at", "message"]
    digest = hashlib.sha256(data).hexdigest()
    with USER_UPLOAD_LOCK:
        _, rows = read_csv_rows(USER_UPLOAD_MANIFEST)
        for row in rows:
            if row.get("category") != category or row.get("sha256") != digest:
                continue
            prior = PROJECT_ROOT / str(row.get("stored_path", ""))
            if prior.is_file() and within(prior, USER_UPLOAD_ROOT):
                return prior, True
        stored.parent.mkdir(parents=True, exist_ok=True)
        stored.write_bytes(data)
        relative_stored = str(stored.relative_to(PROJECT_ROOT)).replace("\\", "/")
        current = {
            "category": category, "batch_id": safe_batch, "batch_index": str(batch_index),
            "original_name": filename, "stored_path": relative_stored, "bytes": str(len(data)),
            "sha256": digest,
            "imported_at": datetime.now().astimezone().isoformat(timespec="seconds"), "message": message,
        }
        rows = [row for row in rows if row.get("stored_path") != relative_stored]
        rows.append(current)
        write_csv_rows(USER_UPLOAD_MANIFEST, manifest_fields, rows)
    return stored, False


def uploaded_resource_table(category: str = "all") -> list[dict[str, Any]]:
    """List unique uploaded resource originals and retain historical upload counts."""
    if category != "all" and category not in PRESERVED_UPLOAD_CATEGORIES:
        raise ValueError("未知上传资源类别")
    _, rows = read_csv_rows(USER_UPLOAD_MANIFEST)
    grouped: dict[tuple[str, str], dict[str, Any]] = {}
    for row in rows:
        row_category = str(row.get("category", ""))
        digest = str(row.get("sha256", ""))
        if row_category not in PRESERVED_UPLOAD_CATEGORIES or not digest:
            continue
        if category != "all" and row_category != category:
            continue
        key = (row_category, digest)
        current = grouped.get(key)
        if current is None:
            current = {
                "category": row_category,
                "original_name": str(row.get("original_name", "")),
                "stored_path": str(row.get("stored_path", "")),
                "bytes": int(row.get("bytes") or 0),
                "sha256": digest,
                "imported_at": str(row.get("imported_at", "")),
                "message": str(row.get("message", "")),
                "upload_count": 0,
            }
            grouped[key] = current
        current["upload_count"] += 1
        if str(row.get("imported_at", "")) >= current["imported_at"]:
            current.update({
                "original_name": str(row.get("original_name", "")),
                "stored_path": str(row.get("stored_path", "")),
                "bytes": int(row.get("bytes") or 0),
                "imported_at": str(row.get("imported_at", "")),
                "message": str(row.get("message", "")),
            })
    result = list(grouped.values())
    for row in result:
        path = PROJECT_ROOT / row["stored_path"]
        row["exists"] = path.is_file() and within(path, USER_UPLOAD_ROOT)
        row["preview_url"] = f"/api/preview?path={row['stored_path']}"
        row["download_url"] = f"/api/download?path={row['stored_path']}"
    return sorted(result, key=lambda row: (row["imported_at"], row["original_name"]), reverse=True)


def preserved_uploads(category: str) -> list[Path]:
    root = USER_UPLOAD_ROOT / category
    return sorted((path for path in root.rglob("*") if path.is_file()), key=lambda path: path.stat().st_mtime, reverse=True) if root.exists() else []


def decode_upload_text(data: bytes, label: str = "词表") -> str:
    try:
        return data.decode("utf-8-sig")
    except UnicodeDecodeError as exc:
        raise ValueError(f"{label}必须保存为 UTF-8 编码") from exc


def simple_word_rows(text: str) -> list[str]:
    """Read a headerless one-word-per-line TXT/CSV list without losing its first row."""
    words: list[str] = []
    seen: set[str] = set()
    for raw_line in text.splitlines():
        line = raw_line.strip().lstrip("\ufeff")
        if not line or line.startswith("#"):
            continue
        cells = [cell.strip() for cell in next(csv.reader([line])) if cell.strip()]
        if len(cells) != 1:
            raise ValueError("简单词表必须一行只有一个词；多字段文件请使用标准 CSV 模板")
        word = cells[0]
        if word.lower() in SIMPLE_WORD_HEADERS or word in SIMPLE_WORD_HEADERS:
            continue
        if word not in seen:
            seen.add(word)
            words.append(word)
    if not words:
        raise ValueError("词表中没有可导入的词语")
    return words


def normalize_word_table(category: str, filename: str, data: bytes) -> tuple[list[str], list[dict[str, str]], bool]:
    """Normalize a full CSV or a one-word-per-line file to the canonical table."""
    text = decode_upload_text(data)
    reader = csv.DictReader(io.StringIO(text))
    input_fields = set(reader.fieldnames or [])
    source = Path(filename).name
    if category == "domain_lexicon":
        fields = DOMAIN_LEXICON_FIELDS
        if "term" in input_fields:
            rows = []
            for raw in reader:
                term = str(raw.get("term", "")).strip()
                if not term:
                    continue
                rows.append({
                    "term": term,
                    "category": str(raw.get("category", "")).strip() or "user_domain",
                    "force_keep": str(raw.get("force_keep", "")).strip() or "1",
                    "priority": str(raw.get("priority", "")).strip() or "100",
                    "status": str(raw.get("status", "")).strip() or "active",
                    "source": str(raw.get("source", "")).strip() or f"user_upload:{source}",
                    "notes": str(raw.get("notes", "")).strip(),
                })
            if not rows:
                raise ValueError("领域词表中没有可导入的 term")
            return fields, rows, False
        words = simple_word_rows(text)
        return fields, [{
            "term": word, "category": "user_domain", "force_keep": "1", "priority": "100",
            "status": "active", "source": f"user_upload:{source}", "notes": "由一行一词文件自动转换",
        } for word in words], True
    if category == "terminology":
        fields = TERMINOLOGY_FIELDS
        if "preferred_term" in input_fields:
            rows = []
            for raw in reader:
                term = str(raw.get("preferred_term", "")).strip()
                if not term:
                    continue
                term_id = str(raw.get("term_id", "")).strip() or "USER_TERM_" + hashlib.sha256(term.encode("utf-8")).hexdigest()[:12].upper()
                rows.append({
                    "term_id": term_id, "preferred_term": term,
                    "aliases": str(raw.get("aliases", "")).strip(),
                    "english_name": str(raw.get("english_name", "")).strip(),
                    "abbreviation": str(raw.get("abbreviation", "")).strip(),
                    "status": str(raw.get("status", "")).strip() or "active",
                    "priority": str(raw.get("priority", "")).strip() or "100",
                    "notes": str(raw.get("notes", "")).strip(),
                })
            if not rows:
                raise ValueError("用户术语表中没有可导入的 preferred_term")
            return fields, rows, False
        words = simple_word_rows(text)
        return fields, [{
            "term_id": "USER_TERM_" + hashlib.sha256(word.encode("utf-8")).hexdigest()[:12].upper(),
            "preferred_term": word, "aliases": "", "english_name": "", "abbreviation": "",
            "status": "active", "priority": "100", "notes": f"由一行一词文件 {source} 自动转换",
        } for word in words], True
    raise ValueError("该上传类型不支持词表自动转换")


def merge_rows(existing: list[dict[str, str]], incoming: list[dict[str, str]], key: str, preserve_existing: bool = False) -> list[dict[str, str]]:
    """Merge deterministically; simple-list defaults never erase richer existing rows."""
    merged: dict[str, dict[str, str]] = {}
    order: list[str] = []
    for row in existing:
        value = str(row.get(key, "")).strip()
        if not value:
            continue
        if value not in merged:
            order.append(value)
        merged[value] = row
    for row in incoming:
        value = str(row.get(key, "")).strip()
        if not value:
            continue
        if value not in merged:
            order.append(value)
            merged[value] = row
        elif not preserve_existing:
            merged[value] = row
    return [merged[value] for value in order]


def normalize_jieba_dictionary(text: str) -> list[str]:
    entries: dict[str, str] = {}
    for raw_line in text.splitlines():
        line = raw_line.strip().lstrip("\ufeff")
        if not line or line.startswith("#") or line.lower() in SIMPLE_WORD_HEADERS or line in SIMPLE_WORD_HEADERS:
            continue
        if "," in line:
            cells = [cell.strip() for cell in next(csv.reader([line])) if cell.strip()]
            if len(cells) != 1:
                raise ValueError("Jieba 简单词表必须一行只有一个词")
            line = cells[0]
        parts = line.split()
        if len(parts) == 1:
            word, normalized = parts[0], f"{parts[0]} 200000 nz"
        elif 2 <= len(parts) <= 3 and parts[1].isdigit():
            word = parts[0]
            normalized = " ".join(parts)
        else:
            raise ValueError("Jieba 用户词典每行应为一个词，或“词语 词频 词性”")
        entries[word] = normalized
    if not entries:
        raise ValueError("Jieba 用户词典中没有可导入的词语")
    return list(entries.values())


def file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while chunk := handle.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


def topic_input_signature(config_path: Path = DEFAULT_CONFIG) -> dict[str, Any]:
    """Hash every input that can change topic clustering or fixed LLM matching."""
    config = read_json(config_path)
    topic_config = config.get("topics", {})
    paths = {
        "cleaned_sections": PROJECT_ROOT / "data" / "processed" / "corpus" / "jsonl" / "sections.jsonl",
        "reference_taxonomy": PROJECT_ROOT / str(topic_config.get("reference_taxonomy", "")),
        "bertopic_config": PROJECT_ROOT / str(topic_config.get("config", "")),
    }
    components = {
        name: file_sha256(path) if path.is_file() else ""
        for name, path in paths.items()
    }
    prompt_config = load_prompt_config(PROMPT_CONFIG_PATH)
    topic_prompt = prompt_config.get("prompts", {}).get("topic_mapping", {})
    components["topic_prompt"] = hashlib.sha256(
        json.dumps(topic_prompt, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()
    fingerprint = hashlib.sha256(
        json.dumps(components, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()
    return {"fingerprint": fingerprint, "components": components}


def write_topic_reuse_manifest(config_path: Path = DEFAULT_CONFIG) -> dict[str, Any]:
    signature = topic_input_signature(config_path)
    required_outputs = [
        PROJECT_ROOT / "data" / "processed" / "topics" / "bertopic_topic_summary.json",
        PROJECT_ROOT / "data" / "processed" / "topics" / "bertopic_unit_assignments.jsonl",
        PROJECT_ROOT / "data" / "processed" / "topics" / "topic_mapping_llm_tasks.jsonl",
        PROJECT_ROOT / "data" / "processed" / "topics" / "topic_mapping_llm_responses.jsonl",
        CURATED_TAXONOMY_PATH,
    ]
    manifest = {
        "schema_version": "1.0",
        "created_at": datetime.now().astimezone().isoformat(timespec="seconds"),
        **signature,
        "required_outputs": [str(path.relative_to(PROJECT_ROOT)).replace("\\", "/") for path in required_outputs],
        "curated_taxonomy_sha256": file_sha256(CURATED_TAXONOMY_PATH) if CURATED_TAXONOMY_PATH.exists() else "",
    }
    TOPIC_REUSE_MANIFEST_PATH.parent.mkdir(parents=True, exist_ok=True)
    TOPIC_REUSE_MANIFEST_PATH.write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return manifest


def topic_reuse_status(config_path: Path = DEFAULT_CONFIG) -> dict[str, Any]:
    manifest = read_json(TOPIC_REUSE_MANIFEST_PATH)
    if not manifest:
        return {"reusable": False, "reason": "尚无已审核话题结果的复用指纹；完成一次话题审核发布后即可复用。", "changed_inputs": []}
    current = topic_input_signature(config_path)
    old_components = manifest.get("components", {})
    labels = {
        "cleaned_sections": "清洗后文本",
        "reference_taxonomy": "参考话题体系",
        "bertopic_config": "BERTopic 配置",
        "topic_prompt": "固定匹配提示词",
    }
    changed = [labels.get(key, key) for key, value in current["components"].items() if value != old_components.get(key, "")]
    missing = [relative for relative in manifest.get("required_outputs", []) if not (PROJECT_ROOT / str(relative)).is_file()]
    curated = read_json(CURATED_TAXONOMY_PATH)
    current_taxonomy_hash = file_sha256(CURATED_TAXONOMY_PATH) if CURATED_TAXONOMY_PATH.exists() else ""
    if current_taxonomy_hash != str(manifest.get("curated_taxonomy_sha256", "")):
        changed.append("已发布话题体系")
    if curated.get("status") != "approved":
        changed.append("话题体系发布状态")
    if missing:
        return {"reusable": False, "reason": "旧话题结果不完整，缺少：" + "、".join(missing), "changed_inputs": changed, "manifest_created_at": manifest.get("created_at", "")}
    if changed or current["fingerprint"] != manifest.get("fingerprint"):
        return {"reusable": False, "reason": "以下输入已变化，必须重新识别：" + "、".join(dict.fromkeys(changed or ["话题输入"])), "changed_inputs": changed, "manifest_created_at": manifest.get("created_at", "")}
    return {"reusable": True, "reason": "清洗后文本、参考体系、BERTopic 配置和固定匹配提示词均未变化，可直接复用已审核结果。", "changed_inputs": [], "manifest_created_at": manifest.get("created_at", "")}


def resource_summary(config_path: Path) -> list[dict[str, Any]]:
    config = read_json(config_path)
    taxonomy_path = PROJECT_ROOT / config.get("topics", {}).get("reference_taxonomy", "configs/topics/new_energy_candidate_taxonomy.json")
    taxonomy = read_json(taxonomy_path)
    def count_tree(node: dict[str, Any]) -> int:
        return 1 + sum(count_tree(child) for child in node.get("children", []) if isinstance(child, dict))
    topic_count = count_tree(taxonomy.get("root", {})) if taxonomy.get("root") else 0
    dimension_count = len(taxonomy.get("cross_cutting_dimensions", []))
    definitions = [
        ("topic_taxonomy", "人工参考话题体系（原件只读）", taxonomy_path, topic_count, f"{topic_count} 个主树节点，{len(taxonomy.get('boundary_candidates', []))} 个边界候选，{dimension_count} 个横向维度"),
        ("domain_lexicon", "内置领域词表", PROJECT_ROOT / config["linguistic"]["domain_lexicon"], None, "控制领域词整体切分"),
        ("terminology", "内置规范术语表", PROJECT_ROOT / config["linguistic"]["terminology"], None, "规范词、别名、英文名和缩略语"),
        ("segmentation_notes", "分词备注表", PROJECT_ROOT / config["linguistic"]["segmentation_notes"], None, "强制合并和例外规则"),
        ("jieba_user_dict", "Jieba 用户词典", PROJECT_ROOT / config["linguistic"]["jieba_user_dict"], None, "每行：词语 词频 词性"),
        ("stop_terms", "排除术语表", PROJECT_ROOT / config["linguistic"]["stop_terms"], None, "不进入术语候选的排除词"),
        ("reference_documents", "典型领域文件登记", PROJECT_ROOT / config["topics"]["reference_documents"], None, "政策、标准、教材和综述依据"),
        ("few_shot_examples", "术语抽取示例", PROJECT_ROOT / config["term_extraction"]["few_shot_examples"], None, "大模型术语抽取少样本示例"),
    ]
    rows = []
    for key, label, path, fixed_count, description in definitions:
        _, csv_rows = read_csv_rows(path) if path.suffix.lower() == ".csv" else ([], [])
        count = fixed_count if fixed_count is not None else (len([row for row in csv_rows if any(value.strip() for value in row.values())]) if csv_rows else line_count(path))
        if key == "reference_documents":
            count = sum(bool(row.get("title", "").strip() or row.get("path_or_url", "").strip()) for row in csv_rows)
        template = TEMPLATE_ROOT / TEMPLATE_FILES[key]
        sources = preserved_uploads(key)
        rows.append({"key": key, "label": label, "path": str(path.relative_to(PROJECT_ROOT)).replace("\\", "/"), "template_path": str(template.relative_to(PROJECT_ROOT)).replace("\\", "/"), "count": count, "description": description, "exists": path.exists(), "source_file_count": len(sources), "source_files": [str(source.relative_to(PROJECT_ROOT)).replace("\\", "/") for source in sources[:20]]})
    return rows


def enrich_review_candidates(rows: list[dict[str, str]]) -> list[dict[str, str]]:
    """Add read-only duplicate-label context without changing the machine source file."""
    groups: dict[str, list[str]] = {}
    for row in rows:
        label = str(row.get("current_label", "")).strip()
        if label:
            groups.setdefault(label, []).append(str(row.get("candidate_id", "")).strip())
    enriched = []
    for original in rows:
        row = dict(original)
        label = str(row.get("current_label", "")).strip()
        candidate_ids = groups.get(label, [])
        row["duplicate_label_count"] = str(len(candidate_ids))
        row["duplicate_label_candidates"] = "|".join(candidate_ids) if len(candidate_ids) > 1 else ""
        row["duplicate_label_explanation"] = (
            "多个候选暂用同一建议名；逐行比较关键词、代表标题和代表摘录后，决定映射已有节点、新增子话题、合并或排除"
            if len(candidate_ids) > 1 else ""
        )
        enriched.append(row)
    return enriched


def required_review_fields(decision: str) -> set[str]:
    """Cell-level required fields; every row additionally requires reviewer."""
    return set(REVIEW_DECISION_REQUIRED_FIELDS.get(decision, set())) | {"reviewer"}


def prepare_topic_review() -> dict[str, Any]:
    queue_path = PROJECT_ROOT / "data" / "processed" / "topics" / "topic_review_queue.csv"
    term_path = PROJECT_ROOT / "data" / "processed" / "topics" / "canonical_term_candidates.csv"
    term_dictionary_path = PROJECT_ROOT / "data" / "processed" / "topics" / "segmentation_term_dictionary.csv"
    if not term_path.exists() or not term_dictionary_path.exists():
        raise ValueError("典型术语识别和规范化尚未完成，不能进入话题人工审核")
    machine_fields, candidates = read_csv_rows(queue_path)
    candidates = enrich_review_candidates(candidates)
    machine_fields = [*machine_fields, *(field for field in REVIEW_DERIVED_FIELDS if field not in machine_fields)]
    if not candidates:
        raise ValueError("话题候选审核队列尚未生成")
    old_fields, old_rows = read_csv_rows(REVIEW_PATH)
    previous = {row.get("candidate_id", ""): row for row in old_rows}
    fields = [field for field in machine_fields if field not in REVIEW_HUMAN_FIELDS] + REVIEW_HUMAN_FIELDS
    output = []
    for candidate in candidates:
        old = previous.get(candidate.get("candidate_id", ""), {})
        row = {field: candidate.get(field, "") for field in fields}
        unchanged = bool(old) and all(old.get(field, "") == candidate.get(field, "") for field in ("row_type", "current_label", "current_parent_id", "keywords"))
        for field in REVIEW_HUMAN_FIELDS:
            row[field] = old.get(field, "") if unchanged else ""
        output.append(row)
    write_csv_rows(REVIEW_PATH, fields, output)
    llm_response_path = PROJECT_ROOT / "data" / "processed" / "topics" / "topic_mapping_llm_responses.jsonl"
    manifest = {
        "schema_version": "1.0",
        "prepared_at": datetime.now().astimezone().isoformat(timespec="seconds"),
        "topic_candidate_rows": len(candidates),
        "canonical_term_candidates": max(line_count(term_path) - 1, 0),
        "segmentation_reference_terms": max(line_count(term_dictionary_path) - 1, 0),
        "topic_llm_response_rows": line_count(llm_response_path),
        "sources": {
            "topic_review_queue.csv": file_sha256(queue_path),
            "canonical_term_candidates.csv": file_sha256(term_path),
            "segmentation_term_dictionary.csv": file_sha256(term_dictionary_path),
        },
    }
    REVIEW_SOURCE_MANIFEST_PATH.parent.mkdir(parents=True, exist_ok=True)
    REVIEW_SOURCE_MANIFEST_PATH.write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
    # 新一轮候选已经生成时，旧版正式体系不得继续充当本轮审核结果；先归档以便审计和恢复。
    if CURATED_TAXONOMY_PATH.exists():
        history = CURATED_TAXONOMY_PATH.parent / "history"
        history.mkdir(parents=True, exist_ok=True)
        archived = history / f"taxonomy_{datetime.now().strftime('%Y%m%d_%H%M%S')}.json"
        CURATED_TAXONOMY_PATH.replace(archived)
    report = validate_topic_review(publish=False)
    report.update({key: value for key, value in manifest.items() if key not in {"sources"}})
    REVIEW_REPORT_PATH.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    return report


def validate_topic_review(publish: bool = True) -> dict[str, Any]:
    fields, rows = read_csv_rows(REVIEW_PATH)
    required_columns = {"row_type", "candidate_id", "current_label", "current_parent_id", *REVIEW_HUMAN_FIELDS}
    issues: list[dict[str, str]] = []
    warnings: list[dict[str, str]] = []
    if not required_columns.issubset(fields):
        issues.append({"candidate_id": "FILE", "code": "header_invalid", "message": "审核表列名被删除或修改；列必须保留不等于每个单元格都必须填写"})
    queue_fields, queue_rows = read_csv_rows(PROJECT_ROOT / "data" / "processed" / "topics" / "topic_review_queue.csv")
    queue_rows = enrich_review_candidates(queue_rows)
    queue_fields = [*queue_fields, *(field for field in REVIEW_DERIVED_FIELDS if field not in queue_fields)]
    protected_fields = [field for field in queue_fields if field in fields]
    machine = {row.get("candidate_id", ""): row for row in queue_rows}
    if len(rows) != len({row.get("candidate_id", "") for row in rows}):
        issues.append({"candidate_id": "FILE", "code": "candidate_id_duplicate", "message": "candidate_id 不得重复"})
    if set(machine) != {row.get("candidate_id", "") for row in rows}:
        issues.append({"candidate_id": "FILE", "code": "candidate_set_changed", "message": "不得删除、新增或修改 candidate_id"})
    manifest = read_json(REVIEW_SOURCE_MANIFEST_PATH)
    source_paths = {
        "topic_review_queue.csv": PROJECT_ROOT / "data" / "processed" / "topics" / "topic_review_queue.csv",
        "canonical_term_candidates.csv": PROJECT_ROOT / "data" / "processed" / "topics" / "canonical_term_candidates.csv",
        "segmentation_term_dictionary.csv": PROJECT_ROOT / "data" / "processed" / "topics" / "segmentation_term_dictionary.csv",
    }
    if not manifest:
        issues.append({"candidate_id": "FILE", "code": "review_manifest_missing", "message": "缺少本轮审核来源清单，请重新生成审核任务"})
    else:
        for name, path in source_paths.items():
            expected = str(manifest.get("sources", {}).get(name, ""))
            if not path.exists() or not expected or file_sha256(path) != expected:
                issues.append({"candidate_id": "FILE", "code": "review_source_changed", "message": f"审核期间机器结果 {name} 已变化，请重新生成审核任务"})
    accepted_nodes = []
    resolutions: list[dict[str, str]] = []
    for row in rows:
        candidate_id, row_type = row.get("candidate_id", ""), row.get("row_type", "")
        source = machine.get(candidate_id, {})
        for protected in protected_fields:
            if row.get(protected, "") != source.get(protected, ""):
                issues.append({"candidate_id": candidate_id, "code": "machine_field_changed", "message": f"不得修改系统字段 {protected}"})
        decision = row.get("human_decision", "").strip().lower()
        if decision not in REVIEW_ALLOWED.get(row_type, set()):
            issues.append({"candidate_id": candidate_id, "code": "decision_invalid", "message": "请选择规定的审核决定"})
            continue
        if decision == "defer":
            issues.append({"candidate_id": candidate_id, "code": "decision_deferred", "message": "暂缓项尚未完成审核"})
        for field in sorted(required_review_fields(decision)):
            if not row.get(field, "").strip():
                issues.append({"candidate_id": candidate_id, "code": f"{field}_missing", "message": f"human_decision={decision} 时必须填写 {field}"})
        used_fields = REVIEW_DECISION_USED_FIELDS.get(decision, {"human_decision", "reviewer", "review_notes"})
        for field in REVIEW_HUMAN_FIELDS:
            if field not in used_fields and row.get(field, "").strip():
                warnings.append({"candidate_id": candidate_id, "code": f"{field}_ignored", "message": f"human_decision={decision} 时 {field} 不参与发布，建议清空"})
        if decision == "split":
            issues.append({"candidate_id": candidate_id, "code": "split_unresolved", "message": "split_plan 只记录拆分方案；拆分尚未落实成正式新节点，因此本轮仍阻断发布"})
        if decision not in {"reject", "defer", "map_existing", "merge", "split"}:
            accepted_nodes.append({
                "id": row.get("approved_node_id", "").strip() or candidate_id,
                "name": row.get("approved_label", "").strip() or row.get("current_label", "").strip(),
                "parent_id": row.get("approved_parent_id", "").strip() or row.get("current_parent_id", "").strip(),
                "source_candidate_id": candidate_id, "decision": decision,
                "definition": row.get("definition", "").strip(), "include_scope": row.get("include_scope", "").strip(), "exclude_scope": row.get("exclude_scope", "").strip(),
            })
            resolutions.append({"candidate_id": candidate_id, "status": "accepted", "node_id": row.get("approved_node_id", "").strip() or candidate_id, "label": row.get("approved_label", "").strip() or row.get("current_label", "").strip()})
        elif decision == "reject":
            resolutions.append({"candidate_id": candidate_id, "status": "rejected", "node_id": "", "label": ""})
        elif decision in {"map_existing", "merge"}:
            resolutions.append({"candidate_id": candidate_id, "status": "mapped", "node_id": row.get("merge_target_id", "").strip(), "label": ""})
    node_ids = [str(node.get("id", "")) for node in accepted_nodes]
    if len(node_ids) != len(set(node_ids)):
        issues.append({"candidate_id": "FILE", "code": "approved_node_id_duplicate", "message": "正式话题节点 ID 不得重复"})
    root_id = str(read_json(PROJECT_ROOT / "data" / "processed" / "topics" / "candidate_taxonomy.json").get("root", {}).get("id", ""))
    node_labels = {str(node.get("id", "")): str(node.get("name", "")) for node in accepted_nodes}
    for node in accepted_nodes:
        parent_id = str(node.get("parent_id", ""))
        if parent_id and parent_id != root_id and parent_id not in node_labels:
            issues.append({"candidate_id": str(node.get("source_candidate_id", "")), "code": "parent_not_found", "message": f"正式父节点 {parent_id} 不存在或未获通过"})
    for resolution in resolutions:
        if resolution["status"] == "mapped":
            target_id = resolution["node_id"]
            if target_id not in node_labels:
                issues.append({"candidate_id": resolution["candidate_id"], "code": "target_not_found", "message": f"映射目标 {target_id} 不存在或未获通过"})
            else:
                resolution["label"] = node_labels[target_id]
    report = {"candidate_rows": len(rows), "completed_rows": sum(bool(row.get("human_decision", "").strip()) and row.get("human_decision", "").strip().lower() != "defer" for row in rows), "error_count": len(issues), "warning_count": len(warnings), "publishable": bool(rows) and not issues, "issues": issues, "warnings": warnings, "workbench": str(REVIEW_PATH.relative_to(PROJECT_ROOT)).replace("\\", "/"), "canonical_term_candidates": int(manifest.get("canonical_term_candidates", 0) or 0), "segmentation_reference_terms": int(manifest.get("segmentation_reference_terms", 0) or 0), "topic_llm_response_rows": int(manifest.get("topic_llm_response_rows", 0) or 0), "prepared_at": str(manifest.get("prepared_at", ""))}
    REVIEW_REPORT_PATH.parent.mkdir(parents=True, exist_ok=True)
    REVIEW_REPORT_PATH.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    if publish and report["publishable"]:
        candidate = read_json(PROJECT_ROOT / "data" / "processed" / "topics" / "candidate_taxonomy.json")
        curated = {"schema_version": "1.0", "taxonomy_version": str(candidate.get("taxonomy_version", "draft")) + "-reviewed", "status": "人工审核完成", "published_at": datetime.now().astimezone().isoformat(timespec="seconds"), "root": candidate.get("root", {}), "nodes": accepted_nodes, "candidate_resolution": resolutions, "review_source_manifest": manifest}
        CURATED_TAXONOMY_PATH.parent.mkdir(parents=True, exist_ok=True)
        CURATED_TAXONOMY_PATH.write_text(json.dumps(curated, ensure_ascii=False, indent=2), encoding="utf-8")
    return report


def safe_relative_path(value: str) -> Path:
    value = urllib.parse.unquote(value).replace("\\", "/")
    parts = [re.sub(r"[^\w\-.()（）\u4e00-\u9fff ]+", "_", part).strip(" .") for part in value.split("/")]
    parts = [part for part in parts if part and part not in {".", ".."}]
    if not parts:
        raise ValueError("文件名为空")
    return Path(*parts)


def within(path: Path, root: Path) -> bool:
    try:
        path.resolve().relative_to(root.resolve())
        return True
    except ValueError:
        return False


def prepare_taxonomy_review() -> dict[str, Any]:
    """Return the independently generated taxonomy workbench state."""
    if not REVIEW_PATH.exists():
        raise RuntimeError("话题体系识别未生成 taxonomy_review_workbench.csv")
    _, rows = read_csv_rows(REVIEW_PATH)
    report = validate_taxonomy_review(publish=False)
    report.update({"workbench": str(REVIEW_PATH.relative_to(PROJECT_ROOT)).replace("\\", "/"), "candidate_rows": len(rows), "completed_rows": sum(bool(row.get("human_decision", "").strip()) for row in rows)})
    return report


REVIEW_FEEDBACK_FIELDS = ["validation_status", "validation_codes", "validation_messages", "validation_suggestions"]


def annotate_taxonomy_workbench(report: dict[str, Any]) -> None:
    """Append current validation feedback to the downloadable/uploadable workbook."""
    if not REVIEW_PATH.exists():
        return
    fields, rows = read_csv_rows(REVIEW_PATH)
    base_fields = [field for field in fields if field not in REVIEW_FEEDBACK_FIELDS]
    grouped: dict[str, list[dict[str, Any]]] = {}
    for entry in report.get("issues", []):
        item_id = str(entry.get("review_item_id") or entry.get("candidate_id") or "").strip()
        if item_id:
            grouped.setdefault(item_id, []).append(entry)
    for row in rows:
        entries = grouped.get(str(row.get("review_item_id", "")).strip(), [])
        row["validation_status"] = "需修改" if entries else "通过"
        row["validation_codes"] = "|".join(dict.fromkeys(str(entry.get("code", "REVIEW_VALIDATION")) for entry in entries))
        row["validation_messages"] = "；".join(str(entry.get("message", "")) for entry in entries)
        row["validation_suggestions"] = "；".join(str(entry.get("suggestion", "")) for entry in entries)
    write_csv_rows(REVIEW_PATH, base_fields + REVIEW_FEEDBACK_FIELDS, rows)


def validate_taxonomy_review(publish: bool = True) -> dict[str, Any]:
    config = read_json(DEFAULT_CONFIG)
    command = [
        sys.executable, str(PROJECT_ROOT / "scripts" / "topics" / "publish_taxonomy.py"),
        "--reference", str(PROJECT_ROOT / config["topics"]["reference_taxonomy"]),
        "--workbench", str(REVIEW_PATH),
        "--topic-summary", str(PROJECT_ROOT / config["paths"]["topic_output"] / "bertopic_topic_summary.json"),
        "--output-root", str(PROJECT_ROOT / "data" / "curated" / "topics"),
    ]
    if not publish:
        command.append("--validate-only")
    process = subprocess.run(command, cwd=PROJECT_ROOT, capture_output=True, text=True, encoding="utf-8", errors="replace", creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0)
    report = read_json(REVIEW_REPORT_PATH)
    if not report:
        report = {"publishable": False, "error_count": 1, "errors": [process.stdout or process.stderr or "审核校验未生成报告"]}
    if not report.get("issues"):
        report["issues"] = [{"review_item_id": "", "severity": "error", "code": "REVIEW_VALIDATION", "message": message, "suggestion": "按页面动作—字段对照表修改后重新上传。"} for message in report.get("errors", [])]
    response_path = PROJECT_ROOT / "data" / "processed" / "topics" / "topic_mapping_llm_responses.jsonl"
    report["topic_llm_response_rows"] = line_count(response_path)
    annotate_taxonomy_workbench(report)
    if publish and report.get("publishable") and CURATED_TAXONOMY_PATH.exists():
        write_topic_reuse_manifest(DEFAULT_CONFIG)
    return report


def prepare_term_review() -> dict[str, Any]:
    if not TERM_REVIEW_PATH.exists():
        raise RuntimeError("术语识别未生成 term_review_workbench.csv")
    _, rows = read_csv_rows(TERM_REVIEW_PATH)
    report = read_json(TERM_REVIEW_REPORT_PATH)
    if not report:
        report = validate_term_review(publish=False)
    elif not report.get("issues"):
        report["issues"] = term_review_issues(report.get("errors", []))
    report.update({"workbench": str(TERM_REVIEW_PATH.relative_to(PROJECT_ROOT)).replace("\\", "/"), "candidate_rows": len(rows), "completed_rows": sum(bool(row.get("human_decision", "").strip()) for row in rows)})
    return report


def term_review_issues(errors: list[Any]) -> list[dict[str, str]]:
    issues = []
    for message in errors:
        candidate_id = str(message).split(" ", 1)[0] if str(message).startswith("TERM_") else "整表"
        issues.append({"candidate_id": candidate_id, "code": "TERM_REVIEW_INVALID", "message": str(message), "suggestion": "按术语审核动作—字段对照表补填该行，保持机器字段和列名不变。"})
    return issues


def validate_term_review(publish: bool = True) -> dict[str, Any]:
    command = [sys.executable, str(PROJECT_ROOT / "scripts" / "terms" / "publish_terms.py"), "--workbench", str(TERM_REVIEW_PATH), "--taxonomy", str(CURATED_TAXONOMY_PATH), "--output-root", str(PROJECT_ROOT / "data" / "curated" / "terms")]
    if not publish:
        command.append("--validate-only")
    process = subprocess.run(command, cwd=PROJECT_ROOT, capture_output=True, text=True, encoding="utf-8", errors="replace", creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0)
    report = read_json(TERM_REVIEW_REPORT_PATH)
    if not report:
        report = {"publishable": False, "error_count": 1, "errors": [process.stdout or process.stderr or "术语审核校验未生成报告"]}
    report["issues"] = term_review_issues(report.get("errors", []))
    return report


# 新版唯一审核接口；覆盖旧版候选簇审核函数，防止旧 topic_review_workbench 被继续使用。
prepare_topic_review = prepare_taxonomy_review
validate_topic_review = validate_taxonomy_review


class JobState:
    def __init__(self) -> None:
        self.lock = threading.Lock()
        self.running = False
        self.job_id = ""
        self.started_at = ""
        self.finished_at = ""
        self.current_step = ""
        self.progress = 0
        self.logs: list[str] = []
        self.error = ""
        self.completed_steps: list[str] = []
        self.awaiting_review = False
        self.review_kind = ""

    def snapshot(self) -> dict[str, Any]:
        with self.lock:
            return {
                "running": self.running, "job_id": self.job_id, "started_at": self.started_at,
                "finished_at": self.finished_at, "current_step": self.current_step,
                "progress": self.progress, "logs": self.logs[-600:], "error": self.error,
                "completed_steps": list(self.completed_steps),
                "awaiting_review": self.awaiting_review,
                "review_kind": self.review_kind,
            }

    def log(self, value: str) -> None:
        with self.lock:
            self.logs.append(value.rstrip())
            self.logs = self.logs[-1200:]


STATE = JobState()


def result_summary(config_path: Path) -> dict[str, Any]:
    config = read_json(config_path)
    version = str(config.get("workflow", {}).get("corpus_version", "unknown"))
    corpus = PROJECT_ROOT / "data" / "processed" / "corpus" / "jsonl"
    topics = PROJECT_ROOT / "data" / "processed" / "topics"
    terms = PROJECT_ROOT / "data" / "processed" / "terms"
    release = PROJECT_ROOT / "data" / "releases" / version
    profile = read_json(release / "corpus_profile.json")
    topic_run_path = topics / "bertopic_run_report.json"
    topic_run = read_json(topic_run_path) if topic_run_path.exists() else {}
    term_metrics_path = terms / "term_extraction_run_report.json"
    term_metrics = read_json(term_metrics_path) if term_metrics_path.exists() else {}
    index_errors = PROJECT_ROOT / "data" / "processed" / "index" / "index_validation_errors.csv"
    raw_files = raw_corpus_files(config_path)
    live_scale = {
        "documents": line_count(corpus / "documents.jsonl"),
        "sections": line_count(corpus / "sections.jsonl"),
        "paragraphs": line_count(corpus / "paragraphs.jsonl"),
        "sentences": line_count(corpus / "sentences.jsonl"),
    }
    return {
        "version": version,
        "raw_corpus": {"files": len(raw_files), "bytes": sum(path.stat().st_size for path in raw_files)},
        # The dashboard describes the current processing scope, not an older release profile.
        "scale": live_scale,
        "topics": {
            "clusters": topic_run.get("topics", 0),
            "controlled_nodes": read_json(topics / "reference_taxonomy_snapshot.json").get("node_count", 0),
            "llm_tasks": line_count(topics / "topic_mapping_llm_tasks.jsonl"),
            "llm_replies": line_count(topics / "topic_mapping_llm_responses.jsonl"),
        },
        "terms": {
            "ranked_candidates": term_metrics.get("candidate_count", 0),
            "canonical_candidates": term_metrics.get("candidate_count", 0),
            "duplicates_merged": 0,
            "llm_tasks": line_count(terms / "term_llm_tasks.jsonl"),
            "llm_replies": line_count(terms / "term_llm_responses.jsonl"),
        },
        "index": {
            "exists": (PROJECT_ROOT / "data" / "processed" / "index" / "corpus_index.txt").exists(),
            "validation_errors": max(line_count(index_errors) - 1, 0),
        },
        "outputs": output_catalog(version), "resources": resource_summary(config_path),
        "active_review_kind": STATE.review_kind,
        "topic_reuse": topic_reuse_status(config_path),
        "topic_review": prepare_taxonomy_review() if REVIEW_PATH.exists() else {"candidate_rows": 0, "completed_rows": 0, "error_count": 0, "publishable": False, "issues": []},
        "term_review": prepare_term_review() if TERM_REVIEW_PATH.exists() and CURATED_TAXONOMY_PATH.exists() else {"candidate_rows": 0, "completed_rows": 0, "error_count": 0, "publishable": False, "issues": []},
    }


def output_catalog(version: str) -> list[dict[str, Any]]:
    candidates = [
        ("cleaning", "清洗语料", "data/processed/corpus/jsonl/documents.jsonl"),
        ("cleaning", "清洗质量报告", "data/processed/corpus/quality/cleaning_report.json"),
        ("cleaning", "大模型清洗复核任务", "data/processed/cleaning_llm/tasks.jsonl"),
        ("cleaning", "大模型清洗原始回复", "data/processed/cleaning_llm/responses.jsonl"),
        ("cleaning", "大模型清洗安全应用报告", "data/processed/cleaning_llm/llm_cleaning_report.json"),
        ("linguistic", "分词结果", "data/processed/linguistic/sentences_tokens.jsonl"),
        ("linguistic", "词频统计", "data/processed/linguistic/word_frequency.csv"),
        ("topics", "人工参考体系快照", "data/processed/topics/reference_taxonomy_snapshot.json"),
        ("topics", "BERTopic 主题摘要", "data/processed/topics/bertopic_topic_summary.csv"),
        ("topics", "BERTopic 分层样本", "data/processed/topics/bertopic_topic_samples.jsonl"),
        ("topics", "话题体系审核工作表", "data/review/topics/taxonomy_review_workbench.csv"),
        ("topics", "话题体系审核校验报告", "data/curated/topics/taxonomy_validation_report.json"),
        ("topics", "人工审核后话题体系", "data/curated/topics/taxonomy.json"),
        ("topics", "必选聚类—体系匹配任务", "data/processed/topics/topic_mapping_llm_tasks.jsonl"),
        ("topics", "必选聚类—体系匹配回复", "data/processed/topics/topic_mapping_llm_responses.jsonl"),
        ("topics", "可选正式体系模型核验", "data/processed/topics/taxonomy_validation_llm_responses.jsonl"),
        ("terms", "领域术语候选", "data/processed/terms/term_candidates.csv"),
        ("terms", "术语上下文聚类摘要", "data/processed/terms/term_cluster_summary.csv"),
        ("terms", "术语人工审核工作表", "data/review/terms/term_review_workbench.csv"),
        ("terms", "正式术语表", "data/curated/terms/terminology.csv"),
        ("terms", "正式分词保护词典", "data/curated/terms/segmentation_dictionary.txt"),
        ("structure", "最终索引", "data/processed/index/corpus_index.txt"),
        ("structure", "索引 Item 与局部标注明细", "data/processed/index/index_items.jsonl"),
        ("syntax", "句法结构树版最终索引", "data/processed/index/corpus_index_syntax_tree.txt"),
        ("syntax", "Item 分词与词性结果", "data/processed/index/index_item_tokens.jsonl"),
        ("syntax", "句法服务原始回复记录", "data/processed/index/syntax_tree_responses.jsonl"),
        ("syntax", "句法服务失败台账", "data/processed/index/syntax_tree_errors.csv"),
        ("release", "版本数据说明", f"data/releases/{version}/corpus_profile.md"),
    ]
    rows = []
    for group, label, relative in candidates:
        path = PROJECT_ROOT / relative
        if path.exists():
            rows.append({"group": group, "label": label, "path": relative, "bytes": path.stat().st_size, "updated_at": datetime.fromtimestamp(path.stat().st_mtime).astimezone().isoformat(timespec="seconds")})
    return rows


def build_commands(payload: dict[str, Any], config_path: Path) -> tuple[list[tuple[str, list[str]]], dict[str, str]]:
    steps = payload.get("steps") or []
    model = payload.get("model") or {}
    python = sys.executable
    # Use the stable package CLI. The legacy scripts/pipeline.py entry remains
    # available for existing users, but the workbench no longer depends on its
    # file location.
    base = [python, "-m", "corpus_pipeline"]
    config_args = ["--config", str(config_path)]
    commands: list[tuple[str, list[str]]] = []
    if "cleaning" in steps:
        commands.append(("语料清洗", base + ["clean"] + config_args))
        if bool(model.get("cleaning_enabled", False)):
            clean_root = PROJECT_ROOT / "data" / "processed" / "corpus" / "jsonl"
            clean_llm = PROJECT_ROOT / "data" / "processed" / "cleaning_llm"
            commands.append(("生成大模型清洗复核任务（可选）", [python, str(PROJECT_ROOT / "scripts" / "prepare" / "llm_clean_review.py"), "prepare", "--corpus-root", str(clean_root), "--tasks", str(clean_llm / "tasks.jsonl"), "--output-root", str(clean_llm)]))
            commands.append(("大模型清洗复核（可选）", [python, str(PROJECT_ROOT / "scripts" / "workflow" / "run_validation_llm.py"), "--mode", "cleaning", "--tasks", str(clean_llm / "tasks.jsonl"), "--responses", str(clean_llm / "responses.jsonl"), "--limit", "0", "--reset", "--require-complete", "--continue-on-errors"]));
            commands.append(("安全应用大模型清洗建议", [python, str(PROJECT_ROOT / "scripts" / "prepare" / "llm_clean_review.py"), "apply", "--corpus-root", str(clean_root), "--responses", str(clean_llm / "responses.jsonl"), "--output-root", str(clean_llm)]))
    topic_selected = "topics" in steps
    term_selected = "terms" in steps
    domain_selected = topic_selected or term_selected
    taxonomy_validation_enabled = topic_selected and bool(model.get("taxonomy_validation_enabled", False))
    term_validation_enabled = term_selected and bool(model.get("term_enabled", False))
    cleaning_validation_enabled = "cleaning" in steps and bool(model.get("cleaning_enabled", False))
    reuse_requested = topic_selected and bool(payload.get("reuse_topic_results", False))
    reuse_preflight = reuse_requested and "cleaning" not in steps and topic_reuse_status(config_path).get("reusable", False)
    topic_llm_required = topic_selected and not reuse_preflight
    use_llm = cleaning_validation_enabled or topic_llm_required or taxonomy_validation_enabled or term_validation_enabled
    if topic_selected:
        if reuse_requested:
            commands.append(("检查话题加工结果是否可复用", ["__TOPIC_REUSE_CHECK__"]))
        command = base + ["topic-only", "--defer-topic-llm"] + config_args
        commands.append(("话题体系识别（BERTopic）与审核任务生成", command))
        commands.append(("BERTopic 聚类结果与参考体系固定匹配（必选）", [python, str(PROJECT_ROOT / "scripts" / "workflow" / "run_validation_llm.py"), "--mode", "topics", "--tasks", str(PROJECT_ROOT / "data" / "processed" / "topics" / "topic_mapping_llm_tasks.jsonl"), "--responses", str(PROJECT_ROOT / "data" / "processed" / "topics" / "topic_mapping_llm_responses.jsonl"), "--limit", "0", "--require-complete", "--continue-on-errors"]))
        commands.append(("将必选模型匹配结果写入话题审核表", [python, str(PROJECT_ROOT / "scripts" / "topics" / "merge_mapping_evidence.py"), "--tasks", str(PROJECT_ROOT / "data" / "processed" / "topics" / "topic_mapping_llm_tasks.jsonl"), "--responses", str(PROJECT_ROOT / "data" / "processed" / "topics" / "topic_mapping_llm_responses.jsonl"), "--workbench", str(PROJECT_ROOT / "data" / "review" / "topics" / "taxonomy_review_workbench.csv")]))
        commands.append(("话题体系人工审核与发布门禁", ["__TOPIC_REVIEW_GATE__"]))
        if taxonomy_validation_enabled:
            commands.append(("生成正式话题体系模型核验任务", [python, str(PROJECT_ROOT / "scripts" / "topics" / "build_taxonomy_validation_task.py"), "--taxonomy", str(CURATED_TAXONOMY_PATH), "--output", str(PROJECT_ROOT / "data" / "processed" / "topics" / "taxonomy_validation_llm_tasks.jsonl")]))
            commands.append(("正式话题体系大模型核验（可选）", [python, str(PROJECT_ROOT / "scripts" / "workflow" / "run_validation_llm.py"), "--mode", "topics", "--tasks", str(PROJECT_ROOT / "data" / "processed" / "topics" / "taxonomy_validation_llm_tasks.jsonl"), "--responses", str(PROJECT_ROOT / "data" / "processed" / "topics" / "taxonomy_validation_llm_responses.jsonl"), "--limit", "1", "--reset", "--require-complete", "--continue-on-errors"]))
    if term_selected:
        if not topic_selected and not CURATED_TAXONOMY_PATH.exists():
            raise ValueError("领域典型术语识别前必须先完成人工话题体系审核并发布正式 taxonomy.json")
        commands.append(("领域典型术语候选识别与上下文聚类", base + ["term-only"] + config_args))
        if term_validation_enabled:
            commands.append(("领域术语候选模型验证", [python, str(PROJECT_ROOT / "scripts" / "workflow" / "run_validation_llm.py"), "--mode", "terms", "--tasks", str(PROJECT_ROOT / "data" / "processed" / "terms" / "term_llm_tasks.jsonl"), "--responses", str(PROJECT_ROOT / "data" / "processed" / "terms" / "term_llm_responses.jsonl"), "--limit", str(max(1, min(int(payload.get("term_limit") or 100), 25000))) ]))
        commands.append(("领域典型术语人工审核与发布门禁", ["__TERM_REVIEW_GATE__"]))
    if "segmentation" in steps:
        commands.append(("基础语言处理与分词", base + ["linguistic"] + config_args))
    if "structure" in steps:
        if "term_score_threshold" not in payload or payload.get("term_score_threshold") in (None, ""):
            raise ValueError("语料结构化前必须明确填写并确认术语最低分（0–1）")
        try:
            term_score_threshold = float(payload["term_score_threshold"])
        except (TypeError, ValueError) as exc:
            raise ValueError("术语最低分必须是 0–1 之间的数值") from exc
        if not 0.0 <= term_score_threshold <= 1.0:
            raise ValueError("术语最低分必须在 0–1 之间")
        if not domain_selected and payload.get("topic_review_required", True) and not CURATED_TAXONOMY_PATH.exists():
            raise ValueError("语料结构化前必须先完成领域话题识别和人工审核")
        commands.append(("质量验证", base + ["quality"] + config_args))
        commands.append(("语料结构化与版本输出", base + ["release"] + config_args))
    if "syntax" in steps:
        commands.append(("Item 分词、词性与句法结构树", base + ["syntax"] + config_args))
    env = os.environ.copy()
    env.update({"PYTHONUNBUFFERED": "1", "PYTHONIOENCODING": "utf-8"})
    resources = payload.get("resources") or {}
    env.update({
        "CORPUS_USE_TOPIC_TAXONOMY": "1" if resources.get("topic_taxonomy", True) else "0",
        "CORPUS_USE_DOMAIN_LEXICON": "1" if resources.get("domain_lexicon", True) else "0",
        "CORPUS_USE_TERMINOLOGY": "1" if resources.get("terminology", True) else "0",
        "CORPUS_USE_SEGMENTATION_NOTES": "1" if resources.get("segmentation_notes", True) else "0",
        "CORPUS_USE_JIEBA_USER_DICT": "1" if resources.get("jieba_user_dict", True) else "0",
        "CORPUS_USE_REFERENCE_DOCUMENTS": "1" if resources.get("reference_documents", True) else "0",
        "CORPUS_REQUIRE_TOPIC_REVIEW": "1" if payload.get("topic_review_required", True) else "0",
        "CORPUS_TERM_SCORE_THRESHOLD": str(float(payload.get("term_score_threshold") or 0.0)),
    })
    if use_llm:
        api_key = str(model.get("api_key", "")).strip()
        model_name = str(model.get("name", "")).strip()
        base_url = str(model.get("base_url", "")).strip() or "https://api.openai.com/v1"
        if not api_key or not model_name:
            reason = "话题识别包含必选的聚类—体系固定匹配" if topic_selected else "已选择大模型验证"
            raise ValueError(f"{reason}，必须填写模型名称和 API Key")
        env.update({
            "CLEANING_LLM_API_KEY": api_key, "CLEANING_LLM_MODEL": model_name, "CLEANING_LLM_BASE_URL": base_url,
            "TOPIC_LLM_API_KEY": api_key, "TOPIC_LLM_MODEL": model_name, "TOPIC_LLM_BASE_URL": base_url,
            "TERM_LLM_API_KEY": api_key, "TERM_LLM_MODEL": model_name, "TERM_LLM_BASE_URL": base_url,
        })
    if not commands:
        raise ValueError("请至少选择一个加工步骤")
    return commands, env


def corpus_exclusions() -> set[str]:
    if not CORPUS_EXCLUSION_PATH.exists():
        return set()
    with CORPUS_EXCLUSION_PATH.open("r", encoding="utf-8-sig", newline="") as handle:
        return {str(row.get("path", "")).strip().replace("\\", "/").casefold() for row in csv.DictReader(handle) if str(row.get("path", "")).strip()}


def update_corpus_exclusions(paths: list[str], exclude: bool) -> dict[str, Any]:
    roots = [PROJECT_ROOT / "data" / "raw" / "manual", PROJECT_ROOT / "data" / "raw" / "web"]
    current = corpus_exclusions()
    clean_paths: list[str] = []
    for value in paths:
        relative = str(value).strip().replace("\\", "/")
        candidate = (PROJECT_ROOT / relative).resolve()
        if not relative or candidate.suffix.lower() != ".txt" or not candidate.is_file() or not any(within(candidate, root) for root in roots):
            raise ValueError(f"不是有效的原始 TXT：{value}")
        clean_paths.append(str(candidate.relative_to(PROJECT_ROOT)).replace("\\", "/"))
    if exclude:
        current.update(path.casefold() for path in clean_paths)
    else:
        current.difference_update(path.casefold() for path in clean_paths)
    CORPUS_EXCLUSION_PATH.parent.mkdir(parents=True, exist_ok=True)
    with CORPUS_EXCLUSION_PATH.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=["path"])
        writer.writeheader()
        writer.writerows({"path": path} for path in sorted(current))
    return {"changed": len(clean_paths), "excluded_total": len(current), "action": "exclude" if exclude else "restore"}


def raw_corpus_files(config_path: Path, include_excluded: bool = False) -> list[Path]:
    config = read_json(config_path)
    path_config = config.get("paths", {})
    roots = [PROJECT_ROOT / path_config.get("manual_sources", "data/raw/manual"), PROJECT_ROOT / path_config.get("web_sources", "data/raw/web")]
    excluded = corpus_exclusions() if not include_excluded else set()
    return sorted(path for root in roots if root.exists() for path in root.rglob("*.txt") if path.is_file() and str(path.relative_to(PROJECT_ROOT)).replace("\\", "/").casefold() not in excluded)


def raw_corpus_table(
    config_path: Path,
    query: str = "",
    source: str = "all",
    status: str = "all",
    page: int = 1,
    page_size: int = 50,
) -> dict[str, Any]:
    """Return a paged, reviewable view of the exact TXT corpus input scope."""
    config = read_json(config_path)
    inventory_path = PROJECT_ROOT / "data" / "interim" / "inventory" / "corpus_inventory_details.csv"
    summary_path = PROJECT_ROOT / "data" / "interim" / "inventory" / "corpus_inventory_summary.json"
    inventory: dict[str, dict[str, str]] = {}
    if inventory_path.exists():
        with inventory_path.open("r", encoding="utf-8-sig", newline="") as handle:
            for row in csv.DictReader(handle):
                raw_path = str(row.get("path", "")).strip()
                if raw_path:
                    inventory[str(Path(raw_path).resolve()).casefold()] = dict(row)
    inventory_summary = read_json(summary_path)
    processed_source_paths: set[str] = set()
    documents_path = PROJECT_ROOT / "data" / "processed" / "corpus" / "jsonl" / "documents.jsonl"
    if documents_path.is_file():
        with documents_path.open("r", encoding="utf-8-sig") as handle:
            for line in handle:
                if not line.strip():
                    continue
                try:
                    document = json.loads(line)
                    original_path = str(document.get("original_path", "")).strip()
                    if original_path:
                        processed_source_paths.add(str(Path(original_path).resolve()).casefold())
                except (json.JSONDecodeError, OSError):
                    continue
    generated_at = str(inventory_summary.get("generated_at", ""))
    try:
        inventory_time = datetime.fromisoformat(generated_at).timestamp()
    except ValueError:
        inventory_time = 0.0

    query_key = query.strip().casefold()
    rows: list[dict[str, Any]] = []
    user_excluded = corpus_exclusions()
    reuse_manifest = load_structure_reuse_manifest(PROJECT_ROOT)
    resource_state = structure_resource_snapshot(PROJECT_ROOT, config)
    totals = {"files": 0, "active_files": 0, "bytes": 0, "manual": 0, "web": 0, "processed": 0, "pending": 0, "excluded": 0, "removed": 0, "reusable": 0, "rebuild": 0}
    for path in raw_corpus_files(config_path, include_excluded=True):
        stat = path.stat()
        relative = str(path.relative_to(PROJECT_ROOT)).replace("\\", "/")
        source_name = "manual" if within(path, PROJECT_ROOT / "data" / "raw" / "manual") else "web"
        previous = inventory.get(str(path.resolve()).casefold())
        changed_after_inventory = bool(inventory_time and stat.st_mtime > inventory_time + 1)
        if relative.casefold() in user_excluded:
            processing_status = "removed"
            status_label = "已移出本次加工"
        elif not previous or changed_after_inventory:
            processing_status = "pending"
            status_label = "待重新清洗" if previous else "未进入上次清洗清单"
        elif previous.get("dedup_status") == "duplicate":
            processing_status = "excluded"
            status_label = "与其他已选文件重复"
        elif str(path.resolve()).casefold() not in processed_source_paths:
            processing_status = "excluded"
            status_label = "上次清洗未产出，请查看清洗报告"
        else:
            processing_status = "processed"
            status_label = "已纳入上次清洗"
        selected_for_processing = relative.casefold() not in user_excluded
        reuse = structure_reuse_status(
            PROJECT_ROOT, config, path,
            manifest=reuse_manifest, resources=resource_state,
        )
        structure_action = (
            "reuse" if selected_for_processing and reuse["reusable"]
            else "rebuild" if selected_for_processing else "not_selected"
        )
        if structure_action == "reuse":
            structure_label = f"可复用 · {int(reuse.get('item_count', 0))} 个条目"
            totals["reusable"] += 1
        elif structure_action == "rebuild":
            structure_label = "需重新加工"
            totals["rebuild"] += 1
        else:
            structure_label = "未选择"
        totals["files"] += 1
        totals["bytes"] += stat.st_size
        totals[source_name] += 1
        totals[processing_status] += 1
        if processing_status != "removed":
            totals["active_files"] += 1
        row = {
            "name": path.name,
            "path": relative,
            "source": source_name,
            "size_bytes": stat.st_size,
            "modified_at": datetime.fromtimestamp(stat.st_mtime).astimezone().isoformat(timespec="seconds"),
            "processing_status": processing_status,
            "status_label": status_label,
            "selected_for_processing": selected_for_processing,
            "structure_action": structure_action,
            "structure_label": structure_label,
            "reuse_reason": str(reuse.get("reason", "")),
            "cached_item_count": int(reuse.get("item_count", 0)),
            "dedup_status": previous.get("dedup_status", "") if previous else "",
            "quality": previous.get("quality", "") if previous else "",
            "quality_reason": previous.get("quality_reason", "") if previous else "",
            "raw_chars": int(previous.get("raw_chars") or 0) if previous else 0,
            "norm_chars": int(previous.get("norm_chars") or 0) if previous else 0,
        }
        if query_key and query_key not in f"{row['name']} {relative}".casefold():
            continue
        if source in {"manual", "web"} and source_name != source:
            continue
        if status in {"processed", "pending", "excluded", "removed"} and processing_status != status:
            continue
        if status in {"reuse", "rebuild", "not_selected"} and structure_action != status:
            continue
        rows.append(row)

    action_order = {"rebuild": 0, "reuse": 1, "not_selected": 2}
    rows.sort(key=lambda row: (action_order.get(row["structure_action"], 3), row["path"].casefold()))
    page_size = min(max(page_size, 10), 200)
    page = max(page, 1)
    filtered = len(rows)
    pages = max(1, (filtered + page_size - 1) // page_size)
    page = min(page, pages)
    start = (page - 1) * page_size
    return {
        "summary": {**totals, "inventory_generated_at": generated_at},
        "filters": {"query": query, "source": source, "status": status},
        "pagination": {"page": page, "page_size": page_size, "pages": pages, "filtered": filtered},
        "rows": rows[start:start + page_size],
    }


def validate_run_inputs(payload: dict[str, Any], config_path: Path) -> None:
    if 'cleaning' in payload.get('steps',[]) and payload.get('model',{}).get('cleaning_enabled'):
        from corpus_api import step_models
        if step_models.settings_path(PROJECT_ROOT).exists():
            selected=step_models.selected_models(step_models.read_settings(PROJECT_ROOT),'cleaning')
            actual=payload.get('model',{})
            if len(selected)!=1 or any(actual.get(k)!=selected[0].get(v) for k,v in [('name','model'),('base_url','base_url'),('api_key','api_key')]):raise ValueError('清洗模型与已保存的步骤选择不同')
            step_models.require_verified(PROJECT_ROOT,'cleaning',load_prompt_config(PROMPT_CONFIG_PATH)['prompts']['cleaning_validation'])
    """Fail before launching subprocesses when selected stages have no corpus input."""
    steps = set(payload.get("steps") or [])
    domain_selected = bool(steps & {"topics", "terms"})
    downstream_selected = domain_selected or bool(steps & {"segmentation", "structure", "syntax"})
    if "cleaning" in steps and not raw_corpus_files(config_path):
        raise ValueError("当前没有原始语料。已上传的 Jieba 词典和参考文件不能代替语料；请先在“原始语料”卡片上传一个或多个 TXT，或上传仅包含 TXT 的 ZIP，确认页面显示“当前已入库原始 TXT”大于 0 后再运行。")
    config = read_json(config_path)
    if "topics" in steps:
        topic_config = PROJECT_ROOT / str(config.get("topics", {}).get("config", "configs/topics/bertopic.json"))
        reference_taxonomy = PROJECT_ROOT / str(config.get("topics", {}).get("reference_taxonomy", "configs/topics/new_energy_candidate_taxonomy.json"))
        missing = [str(path.relative_to(PROJECT_ROOT)).replace("\\", "/") for path in (topic_config, reference_taxonomy) if not path.exists()]
        if missing:
            raise ValueError(f"话题识别缺少必需配置文件：{', '.join(missing)}。请恢复配置后再运行，流程尚未启动。")
        topic_settings = read_json(topic_config)
        required = ["embedding_model", "umap", "hdbscan", "representation"]
        absent = [field for field in required if field not in topic_settings]
        if absent:
            raise ValueError(f"BERTopic 配置不完整，缺少：{', '.join(absent)}。请修正 configs/topics/bertopic.json 后再运行。")
    corpus_root = PROJECT_ROOT / "data" / "processed" / "corpus" / "jsonl"
    if "cleaning" not in steps and downstream_selected:
        if domain_selected and line_count(corpus_root / "sections.jsonl") == 0:
            raise ValueError("当前没有清洗后的章节，不能执行话题或术语识别。请上传原始 TXT 并先运行“语料清洗”。")
        if "segmentation" in steps and line_count(corpus_root / "sentences.jsonl") == 0:
            raise ValueError("当前没有清洗后的句子，不能执行分词。请上传原始 TXT 并先运行“语料清洗”。")
        if "structure" in steps and line_count(corpus_root / "documents.jsonl") == 0:
            raise ValueError("当前没有清洗后的文档，不能执行结构化。请上传原始 TXT 并先运行“语料清洗”。")
        if "syntax" in steps and "structure" not in steps and not (PROJECT_ROOT / "data" / "processed" / "index" / "index_items.jsonl").exists():
            raise ValueError("当前没有结构化 Item，不能执行句法结构分析。请先勾选并运行“语料结构化”。")


def run_job(payload: dict[str, Any], config_path: Path) -> None:
    try:
        commands, env = build_commands(payload, config_path)
        total = len(commands)
        reuse_topic_outputs = False
        reusable_topic_labels = {
            "话题体系识别（BERTopic）与审核任务生成",
            "BERTopic 聚类结果与参考体系固定匹配（必选）",
            "将必选模型匹配结果写入话题审核表",
            "话题体系人工审核与发布门禁",
        }
        for index, (label, command) in enumerate(commands, 1):
            with STATE.lock:
                STATE.current_step = label
                STATE.progress = int((index - 1) / total * 100)
            STATE.log(f"\n▶ {label}")
            if command == ["__TOPIC_REUSE_CHECK__"]:
                reuse = topic_reuse_status(config_path)
                reuse_topic_outputs = bool(reuse.get("reusable"))
                if reuse_topic_outputs:
                    STATE.log("✓ 输入指纹一致：直接复用已审核的话题聚类、模型匹配和正式话题体系。")
                else:
                    STATE.log("⚠ 不能复用旧话题结果，将执行完整话题流程：" + str(reuse.get("reason", "输入已经变化")))
                with STATE.lock:
                    STATE.completed_steps.append(label)
                    STATE.progress = int(index / total * 100)
                continue
            if reuse_topic_outputs and label in reusable_topic_labels:
                STATE.log(f"↷ 已复用，跳过：{label}")
                with STATE.lock:
                    STATE.completed_steps.append(label + "（已复用）")
                    STATE.progress = int(index / total * 100)
                continue
            if command == ["__TOPIC_REVIEW_GATE__"]:
                if payload.get("topic_review_required", True):
                    report = prepare_topic_review()
                    REVIEW_EVENT.clear()
                    with STATE.lock:
                        STATE.awaiting_review = True
                        STATE.review_kind = "topics"
                        STATE.current_step = "等待领域专家完成人工话题审核"
                    STATE.log(f"\n⏸ BERTopic 话题证据与体系审核表已生成。请审核 {report['candidate_rows']} 条体系项目并上传 taxonomy_review_workbench.csv；正式话题体系发布后才会启动领域典型术语识别。")
                    REVIEW_EVENT.wait()
                    with STATE.lock:
                        STATE.awaiting_review = False
                        STATE.review_kind = ""
                    STATE.log("\n✓ 人工话题审核已通过，正式话题体系已发布，继续后续流程")
                else:
                    STATE.log("\n⚠ 已跳过人工审核门禁；本次只能形成内部候选结果")
                with STATE.lock:
                    STATE.completed_steps.append(label)
                    STATE.progress = int(index / total * 100)
                continue
            if command == ["__TERM_REVIEW_GATE__"]:
                report = prepare_term_review()
                REVIEW_EVENT.clear()
                with STATE.lock:
                    STATE.awaiting_review = True
                    STATE.review_kind = "terms"
                    STATE.current_step = "等待领域专家完成术语审核"
                STATE.log(f"\n⏸ 已生成 {report['candidate_rows']} 条领域术语审核项。请填写并上传 term_review_workbench.csv；正式术语发布后才继续分词。")
                REVIEW_EVENT.wait()
                with STATE.lock:
                    STATE.awaiting_review = False
                    STATE.review_kind = ""
                STATE.log("\n✓ 术语审核已通过，正式术语及分词保护词典已发布")
                with STATE.lock:
                    STATE.completed_steps.append(label)
                    STATE.progress = int(index / total * 100)
                continue
            creationflags = subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0
            process = subprocess.Popen(command, cwd=PROJECT_ROOT, env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, encoding="utf-8", errors="replace", bufsize=1, creationflags=creationflags)
            assert process.stdout is not None
            for line in process.stdout:
                progress_match = re.match(r"^CORPUS_PROGRESS\t(\d{1,3})\t(.+?)\s*$", line)
                if progress_match:
                    sub_progress = max(0, min(100, int(progress_match.group(1))))
                    detail = progress_match.group(2).strip()
                    with STATE.lock:
                        STATE.current_step = f"{label}：{detail}"
                        STATE.progress = int(((index - 1) + sub_progress / 100) / total * 100)
                    STATE.log(f"进度 {sub_progress}% · {detail}")
                else:
                    STATE.log(line)
            code = process.wait()
            if code:
                raise RuntimeError(f"{label}执行失败，退出码 {code}")
            if label == "语料清洗" and any(next_label in {"领域话题机器识别与可选模型复核", "典型术语识别、规范化与分词词典生成", "基础语言处理与分词", "质量验证", "语料结构化与版本输出"} for next_label, _ in commands[index:]):
                sections = line_count(PROJECT_ROOT / "data" / "processed" / "corpus" / "jsonl" / "sections.jsonl")
                if sections == 0:
                    raise RuntimeError("语料清洗已结束，但可用于后续处理的章节为 0。请检查上传的 TXT 是否为空、编码异常或被质量规则全部过滤；后续话题、术语和分词步骤已停止。")
            with STATE.lock:
                STATE.completed_steps.append(label)
                STATE.progress = int(index / total * 100)
        STATE.log("\n✓ 所选流程执行完成")
    except Exception as exc:
        with STATE.lock:
            STATE.error = str(exc)
        STATE.log(f"\n✕ {exc}")
    finally:
        with STATE.lock:
            STATE.running = False
            STATE.finished_at = datetime.now().astimezone().isoformat(timespec="seconds")
            if not STATE.error:
                STATE.progress = 100


class Handler(BaseHTTPRequestHandler):
    server_version = "CorpusConsole/1.0"

    @property
    def config_path(self) -> Path:
        return self.server.config_path  # type: ignore[attr-defined]

    def log_message(self, format: str, *args: Any) -> None:
        return

    def send_json(self, value: Any, status: int = 200) -> None:
        body = json.dumps(value, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def send_file(self, path: Path, attachment: bool = False) -> None:
        if not path.exists() or not path.is_file():
            self.send_error(404)
            return
        mime = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
        self.send_response(200)
        self.send_header("Content-Type", mime + ("; charset=utf-8" if mime.startswith("text/") else ""))
        self.send_header("Content-Length", str(path.stat().st_size))
        self.send_header("Cache-Control", "no-store")
        if attachment:
            self.send_header("Content-Disposition", f"attachment; filename*=UTF-8''{urllib.parse.quote(path.name)}")
        self.end_headers()
        with path.open("rb") as handle:
            while chunk := handle.read(1024 * 1024):
                self.wfile.write(chunk)

    def do_GET(self) -> None:
        parsed = urllib.parse.urlparse(self.path)
        if parsed.path == "/api/bootstrap":
            config = read_json(self.config_path)
            self.send_json({"project": config.get("project", {}), "workflow": config.get("workflow", {}), "summary": result_summary(self.config_path), "job": STATE.snapshot()})
            return
        if parsed.path == "/api/status":
            self.send_json({"job": STATE.snapshot(), "summary": result_summary(self.config_path)})
            return
        if parsed.path == "/api/prompts":
            self.send_json(load_prompt_config(PROMPT_CONFIG_PATH))
            return
        if parsed.path == "/api/raw-corpus":
            query = urllib.parse.parse_qs(parsed.query)
            try:
                page = int(query.get("page", ["1"])[0])
                page_size = int(query.get("page_size", ["50"])[0])
            except ValueError as exc:
                self.send_json({"error": "分页参数必须是整数"}, 400)
                return
            self.send_json(raw_corpus_table(
                self.config_path,
                query=query.get("q", [""])[0],
                source=query.get("source", ["all"])[0],
                status=query.get("status", ["all"])[0],
                page=page,
                page_size=page_size,
            ))
            return
        if parsed.path in {"/api/download", "/api/preview"}:
            query = urllib.parse.parse_qs(parsed.query)
            relative = query.get("path", [""])[0]
            if relative.replace("\\", "/") == "docs/话题体系构建指导.md":
                relative = "docs/话题审核操作说明.md"
            path = (PROJECT_ROOT / relative).resolve()
            allowed = any(within(path, PROJECT_ROOT / root) for root in ("data", "reports", "inputs", "configs", "docs", "参考文件中心"))
            if not relative or not allowed:
                self.send_json({"error": "无效文件路径"}, 400)
                return
            if parsed.path == "/api/preview" and path.exists() and path.stat().st_size > 2 * 1024 * 1024:
                text = read_preview_text(path, byte_limit=60000)[:20000]
                self.send_json({"path": relative, "text": text, "truncated": True})
            elif parsed.path == "/api/preview":
                try:
                    self.send_json({"path": relative, "text": read_preview_text(path), "truncated": False})
                except FileNotFoundError:
                    self.send_json({"error": "文件不存在"}, 404)
            else:
                self.send_file(path, attachment=True)
            return
        asset = "index.html" if parsed.path == "/" else parsed.path.lstrip("/")
        path = (WEB_ROOT / asset).resolve()
        if not within(path, WEB_ROOT):
            self.send_error(404)
            return
        self.send_file(path)

    def do_POST(self) -> None:
        parsed = urllib.parse.urlparse(self.path)
        try:
            length = int(self.headers.get("Content-Length", "0"))
            if length > MAX_UPLOAD_BYTES:
                raise ValueError("单个文件不得超过 512 MB")
            if parsed.path == "/api/upload":
                query = urllib.parse.parse_qs(parsed.query)
                category = query.get("category", [""])[0]
                filename = query.get("filename", [""])[0]
                batch_index = int(query.get("batch_index", ["0"])[0])
                if batch_index < 0:
                    raise ValueError("批次文件序号无效")
                batch_id = query.get("batch_id", [datetime.now().strftime("batch_%Y%m%d_%H%M%S")])[0]
                data = self.rfile.read(length)
                result = self.save_upload(category, filename, data, backup_existing=batch_index == 0, merge_existing=batch_index > 0)
                if result.get("ok") and category in PRESERVED_UPLOAD_CATEGORIES:
                    stored, duplicate = preserve_uploaded_source(category, batch_id, batch_index, filename, data, str(result.get("message", "")))
                    result["stored_path"] = str(stored.relative_to(PROJECT_ROOT)).replace("\\", "/")
                    result["duplicate_upload"] = duplicate
                    if duplicate:
                        result["message"] += "；相同内容的文件已上传过，本次未重复归档"
                self.send_json(result)
                return
            if parsed.path == "/api/run":
                payload = json.loads(self.rfile.read(length).decode("utf-8"))
                build_commands(payload, self.config_path)
                validate_run_inputs(payload, self.config_path)
                with STATE.lock:
                    if STATE.running:
                        raise ValueError("已有流程正在运行，请等待完成")
                    STATE.running = True
                    STATE.job_id = datetime.now().strftime("%Y%m%d_%H%M%S")
                    STATE.started_at = datetime.now().astimezone().isoformat(timespec="seconds")
                    STATE.finished_at = ""
                    STATE.current_step = "准备运行"
                    STATE.progress = 0
                    STATE.logs = []
                    STATE.error = ""
                    STATE.completed_steps = []
                    STATE.awaiting_review = False
                    STATE.review_kind = ""
                threading.Thread(target=run_job, args=(payload, self.config_path), daemon=True).start()
                self.send_json({"ok": True, "job_id": STATE.job_id}, 202)
                return
            if parsed.path == "/api/prompts/save":
                payload = json.loads(self.rfile.read(length).decode("utf-8"))
                config = save_prompt_config(PROMPT_CONFIG_PATH, payload)
                self.send_json({"ok": True, "config": config, "path": str(PROMPT_CONFIG_PATH.relative_to(PROJECT_ROOT)).replace("\\", "/")})
                return
            if parsed.path == "/api/prompts/test":
                payload = json.loads(self.rfile.read(length).decode("utf-8"))
                self.send_json({"ok": True, "result": execute_prompt_test(payload)})
                return
            if parsed.path in {"/api/raw-corpus/exclude", "/api/raw-corpus/restore"}:
                payload = json.loads(self.rfile.read(length).decode("utf-8"))
                paths = payload.get("paths") or []
                if not isinstance(paths, list) or not paths:
                    raise ValueError("请至少选择一个语料文件")
                result = update_corpus_exclusions([str(path) for path in paths], parsed.path.endswith("/exclude"))
                self.send_json({"ok": True, "result": result})
                return
            if parsed.path == "/api/review/continue":
                payload = json.loads(self.rfile.read(length).decode("utf-8") or "{}")
                requested_kind = str(payload.get("review_kind", "")).strip()
                if requested_kind not in {"", "topics", "terms"}:
                    raise ValueError("未知审核类型")
                if STATE.awaiting_review and requested_kind and STATE.review_kind and requested_kind != STATE.review_kind:
                    raise ValueError(f"当前流程正在等待 {STATE.review_kind} 审核，不能提交另一类审核")
                review_kind = requested_kind or STATE.review_kind or "topics"
                report = validate_term_review(publish=True) if review_kind == "terms" else validate_topic_review(publish=True)
                if not report["publishable"]:
                    self.send_json({"error": f"审核尚有 {report['error_count']} 个阻断问题", "report": report}, 400)
                    return
                with STATE.lock:
                    workflow_resumed = STATE.running and STATE.awaiting_review and STATE.review_kind == review_kind
                if workflow_resumed:
                    REVIEW_EVENT.set()
                self.send_json({"ok": True, "report": report, "workflow_resumed": workflow_resumed})
                return
            self.send_error(404)
        except (ValueError, json.JSONDecodeError) as exc:
            self.send_json({"error": str(exc)}, 400)
        except Exception as exc:
            self.send_json({"error": f"服务器处理失败：{exc}"}, 500)

    def save_upload(self, category: str, filename: str, data: bytes, backup_existing: bool = True, merge_existing: bool = False) -> dict[str, Any]:
        relative = safe_relative_path(filename)
        if category in {"topic_review", "term_review"}:
            if relative.suffix.lower() != ".csv":
                raise ValueError("人工审核表必须是 UTF-8 CSV")
            review_kind = "terms" if category == "term_review" else "topics"
            if STATE.awaiting_review and STATE.review_kind and review_kind != STATE.review_kind:
                raise ValueError(f"当前流程正在等待 {STATE.review_kind} 审核，不能上传另一类审核表")
            target_review = TERM_REVIEW_PATH if review_kind == "terms" else REVIEW_PATH
            target_review.parent.mkdir(parents=True, exist_ok=True)
            target_review.write_bytes(data)
            report = validate_term_review(publish=True) if review_kind == "terms" else validate_topic_review(publish=True)
            with STATE.lock:
                workflow_resumed = report["publishable"] and STATE.running and STATE.awaiting_review and STATE.review_kind == review_kind
            if workflow_resumed:
                REVIEW_EVENT.set()
            if report["publishable"]:
                message = "审核完成，流程将继续" if workflow_resumed else "审核完成并已发布；请在流程区选择后续步骤继续加工"
            else:
                message = f"已读取审核表，但仍有 {report['error_count']} 个阻断问题"
            return {"ok": report["publishable"], "category": category, "filename": target_review.name, "message": message, "report": report, "workflow_resumed": workflow_resumed}
        if category == "corpus":
            suffix = relative.suffix.lower()
            if suffix not in {".txt", ".zip"}:
                raise ValueError("当前清洗器接收 TXT；也可上传只包含 TXT 的 ZIP 压缩包")
            batch = datetime.now().strftime("upload_%Y%m%d")
            root = PROJECT_ROOT / "data" / "raw" / "manual" / batch
            root.mkdir(parents=True, exist_ok=True)
            target = root / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            if suffix == ".zip":
                target.write_bytes(data)
                extracted = 0
                with zipfile.ZipFile(target) as archive:
                    total_uncompressed = 0
                    for member in archive.infolist():
                        member_path = safe_relative_path(member.filename)
                        if member.is_dir() or member_path.suffix.lower() != ".txt":
                            continue
                        total_uncompressed += member.file_size
                        if total_uncompressed > MAX_UPLOAD_BYTES:
                            raise ValueError("ZIP 解压后的 TXT 总量不得超过 512 MB")
                        destination = root / target.stem / member_path
                        if not within(destination, root):
                            continue
                        destination.parent.mkdir(parents=True, exist_ok=True)
                        with archive.open(member) as source, destination.open("wb") as output:
                            output.write(source.read())
                        extracted += 1
                target.unlink()
                return {"ok": True, "category": category, "filename": filename, "message": f"已安全解压 {extracted} 个 TXT 文件"}
            target.write_bytes(data)
            return {"ok": True, "category": category, "filename": str(relative), "message": "语料已加入待处理区"}
        if category not in UPLOAD_TARGETS:
            raise ValueError("未知上传类型")
        target = UPLOAD_TARGETS[category]
        if category == "topic_taxonomy":
            if relative.suffix.lower() != ".json":
                raise ValueError("新版人工参考话题体系必须上传 UTF-8 JSON，不再接受旧版扁平 CSV")
            try:
                incoming_taxonomy = json.loads(data.decode("utf-8-sig"))
                from scripts.topics.taxonomy_workflow import flatten_reference
                nodes, taxonomy_errors = flatten_reference(incoming_taxonomy)
            except (UnicodeDecodeError, json.JSONDecodeError) as exc:
                raise ValueError("人工参考话题体系不是有效的 UTF-8 JSON") from exc
            if taxonomy_errors:
                raise ValueError("；".join(taxonomy_errors))
            if not incoming_taxonomy.get("taxonomy_version") or not incoming_taxonomy.get("classification_object"):
                raise ValueError("话题体系必须包含 taxonomy_version 和 classification_object")
            backup = target.with_suffix(target.suffix + ".previous")
            if backup_existing and target.exists():
                backup.write_bytes(target.read_bytes())
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(json.dumps(incoming_taxonomy, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
            return {"ok": True, "category": category, "filename": target.name, "message": f"已校验并更新人工参考体系：{len(nodes)} 个主树/边界/维度/排除节点；原件仅供识别输入，不由模型覆盖"}
        if category in {"domain_lexicon", "terminology"}:
            if relative.suffix.lower() not in {".txt", ".csv"}:
                raise ValueError("用户词典和用户术语支持 UTF-8 TXT 或 CSV")
            fields, incoming, simple_mode = normalize_word_table(category, filename, data)
            _, existing = read_csv_rows(target)
            key = "term" if category == "domain_lexicon" else "preferred_term"
            merged = merge_rows(existing, incoming, key, preserve_existing=simple_mode)
            if category == "terminology":
                ids = [str(row.get("term_id", "")).strip() for row in merged]
                if any(not value for value in ids) or len(ids) != len(set(ids)):
                    raise ValueError("术语的 term_id 必须填写且不能对应多个不同术语")
            target.parent.mkdir(parents=True, exist_ok=True)
            backup = target.with_suffix(target.suffix + ".previous")
            if backup_existing and target.exists():
                backup.write_bytes(target.read_bytes())
            write_csv_rows(target, fields, merged)
            label = "用户词典" if category == "domain_lexicon" else "用户术语"
            mode = "一行一词并已自动转换" if simple_mode else "标准 CSV"
            return {"ok": True, "category": category, "filename": target.name, "message": f"{label}已按{mode}导入 {len(incoming)} 条；合并去重后共 {len(merged)} 条"}
        if category == "jieba_user_dict":
            if relative.suffix.lower() not in {".txt", ".csv"}:
                raise ValueError("Jieba 用户词典支持 UTF-8 TXT 或单列 CSV")
            incoming_lines = normalize_jieba_dictionary(decode_upload_text(data, "Jieba 用户词典"))
            existing_lines = normalize_jieba_dictionary(target.read_text(encoding="utf-8-sig")) if target.exists() and target.read_text(encoding="utf-8-sig").strip() else []
            merged_by_word = {line.split()[0]: line for line in existing_lines}
            merged_by_word.update({line.split()[0]: line for line in incoming_lines})
            backup = target.with_suffix(target.suffix + ".previous")
            if backup_existing and target.exists():
                backup.write_bytes(target.read_bytes())
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text("\n".join(merged_by_word.values()) + "\n", encoding="utf-8")
            return {"ok": True, "category": category, "filename": target.name, "message": f"Jieba 用户词典已自动补齐可选词频和词性；新增或更新 {len(incoming_lines)} 条，共 {len(merged_by_word)} 条"}
        if category == "stop_terms":
            if relative.suffix.lower() not in {".txt", ".csv"}:
                raise ValueError("排除术语表支持 UTF-8 TXT 或单列 CSV")
            incoming_words = simple_word_rows(decode_upload_text(data, "排除术语表"))
            existing_words = simple_word_rows(target.read_text(encoding="utf-8-sig")) if target.exists() and target.read_text(encoding="utf-8-sig").strip() else []
            merged_words = list(dict.fromkeys([*existing_words, *incoming_words]))
            backup = target.with_suffix(target.suffix + ".previous")
            if backup_existing and target.exists():
                backup.write_bytes(target.read_bytes())
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text("\n".join(merged_words) + "\n", encoding="utf-8")
            return {"ok": True, "category": category, "filename": target.name, "message": f"排除术语已合并去重；本次 {len(incoming_words)} 条，共 {len(merged_words)} 条"}
        if relative.suffix.lower() != target.suffix.lower():
            raise ValueError(f"该接口要求 {target.suffix} 文件")
        if category in UPLOAD_SCHEMAS:
            try:
                text = data.decode("utf-8-sig")
            except UnicodeDecodeError as exc:
                raise ValueError("CSV 必须保存为 UTF-8 编码") from exc
            reader = csv.DictReader(io.StringIO(text))
            fields = set(reader.fieldnames or [])
            missing = sorted(UPLOAD_SCHEMAS[category] - fields)
            if missing:
                raise ValueError(f"文件缺少必填字段：{','.join(missing)}；请下载模板后填写")
            if category in {"segmentation_notes", "reference_documents"}:
                incoming = [dict(row) for row in reader]
                old_fields, existing = read_csv_rows(target)
                key = "rule_id" if category == "segmentation_notes" else "reference_id"
                merged = merge_rows(existing, incoming, key)
                output_fields = list(reader.fieldnames or old_fields)
                backup = target.with_suffix(target.suffix + ".previous")
                if backup_existing and target.exists():
                    backup.write_bytes(target.read_bytes())
                write_csv_rows(target, output_fields, merged)
                return {"ok": True, "category": category, "filename": target.name, "message": f"已合并 {len(incoming)} 条记录；按 {key} 去重后共 {len(merged)} 条"}
        if category == "few_shot_examples":
            try:
                rows = [json.loads(line) for line in data.decode("utf-8-sig").splitlines() if line.strip()]
            except (UnicodeDecodeError, json.JSONDecodeError) as exc:
                raise ValueError("术语抽取示例必须是 UTF-8 JSONL，每行一个 JSON 对象") from exc
            if not rows or any(not isinstance(row.get("text"), str) or not isinstance(row.get("terms"), list) for row in rows):
                raise ValueError("每条术语示例必须包含字符串 text 和数组 terms")
            existing_rows = []
            if target.exists():
                existing_rows = [json.loads(line) for line in target.read_text(encoding="utf-8-sig").splitlines() if line.strip()]
            merged_json: dict[str, dict[str, Any]] = {}
            for row in [*existing_rows, *rows]:
                merged_json[json.dumps(row, ensure_ascii=False, sort_keys=True)] = row
            backup = target.with_suffix(target.suffix + ".previous")
            if backup_existing and target.exists():
                backup.write_bytes(target.read_bytes())
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text("".join(json.dumps(row, ensure_ascii=False) + "\n" for row in merged_json.values()), encoding="utf-8")
            return {"ok": True, "category": category, "filename": target.name, "message": f"术语抽取示例已合并去重；本次 {len(rows)} 条，共 {len(merged_json)} 条"}
        target.parent.mkdir(parents=True, exist_ok=True)
        backup = target.with_suffix(target.suffix + ".previous")
        if backup_existing and target.exists():
            backup.write_bytes(target.read_bytes())
        target.write_bytes(data)
        return {"ok": True, "category": category, "filename": target.name, "message": "参考文件已更新；上一版保留为 .previous"}


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="新能源中文教学语料库本地操作台")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    parser.add_argument("--no-browser", action="store_true")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    url = f"http://{args.host}:{args.port}/"
    try:
        server = ExclusiveThreadingHTTPServer((args.host, args.port), Handler)
    except OSError as exc:
        print(f"操作台未重复启动：{url} 已由另一个实例占用（{exc}）。")
        if not args.no_browser:
            webbrowser.open(url)
        return 0
    server.config_path = args.config.resolve()  # type: ignore[attr-defined]
    print(f"语料库操作台已启动：{url}")
    print("关闭此窗口即可停止操作台。API Key 不会写入磁盘。")
    if not args.no_browser:
        threading.Timer(0.7, lambda: webbrowser.open(url)).start()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
