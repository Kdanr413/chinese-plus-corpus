#!/usr/bin/env python3
"""Prepare and safely apply optional LLM proofreading for cleaned Chinese corpus."""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import re
import shutil
import sys
from collections import Counter, defaultdict
from datetime import datetime
from pathlib import Path
from typing import Any, Iterable

PROJECT_ROOT = Path(__file__).resolve().parents[2]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

def split_sentences(paragraph: str) -> list[str]:
    """Keep terminal punctuation with each sentence; headings remain one item."""
    parts = re.split(r"(?<=[。！？!?；;])\s*", paragraph.strip())
    return [part.strip() for part in parts if part.strip()] or [paragraph.strip()]


def read_jsonl(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text(encoding="utf-8-sig").splitlines() if line.strip()]


def write_jsonl(path: Path, rows: Iterable[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join(json.dumps(row, ensure_ascii=False) + "\n" for row in rows), encoding="utf-8")


def digest(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def content_skeleton(text: str) -> str:
    """Keep semantic characters; punctuation/whitespace edits are allowed, content edits are not."""
    return "".join(re.findall(r"[\u3400-\u9fffA-Za-z0-9]", text)).casefold()


def safe_cleaning_correction(
    original: str, corrected: str, changes: list[dict[str, Any]]
) -> tuple[bool, str]:
    """Allow formatting edits and sparse, explicitly declared OCR substitutions."""
    before = content_skeleton(original)
    after = content_skeleton(corrected)
    if before == after:
        return True, "only formatting characters changed"
    if len(before) != len(after):
        return False, "修改增删了语义字符"
    substitutions = [
        (left, right) for left, right in zip(before, after) if left != right
    ]
    maximum = max(3, math.ceil(max(len(before), 1) * 0.01))
    if not substitutions or len(substitutions) > maximum:
        return False, f"OCR 字符替换数量超过安全阈值（{maximum}）"
    declared = {
        (
            content_skeleton(str(change.get("before", ""))),
            content_skeleton(str(change.get("after", ""))),
        )
        for change in changes
        if str(change.get("type", "")) == "ocr_typo"
        and str(change.get("reason", "")).strip()
    }
    if not all((left, right) in declared for left, right in substitutions):
        return False, "OCR 字符替换没有在 changes 中逐字声明依据"
    return True, "sparse declared OCR substitutions"


def prepare(args: argparse.Namespace) -> int:
    paragraphs = read_jsonl(args.corpus_root / "paragraphs.jsonl")
    tasks: list[dict[str, Any]] = []
    for index, row in enumerate(paragraphs):
        if row.get("is_heading") or not str(row.get("text", "")).strip():
            continue
        tasks.append({
            "task_type": "cleaning_text_review",
            "paragraph_id": row["paragraph_id"],
            "doc_id": row["doc_id"],
            "section_path": row.get("section_path", ""),
            "previous_context": str(paragraphs[index - 1].get("text", ""))[-160:] if index and paragraphs[index - 1].get("doc_id") == row.get("doc_id") else "",
            "original_text": row["text"],
            "next_context": str(paragraphs[index + 1].get("text", ""))[:160] if index + 1 < len(paragraphs) and paragraphs[index + 1].get("doc_id") == row.get("doc_id") else "",
            "allowed_changes": ["异常中英文空格", "明显错位标点", "明显句界标点", "OCR 造成的词内空格", "有明确上下文依据的少量 OCR 错别字逐字替换"],
            "forbidden_changes": ["润色或改写措辞", "补写或删除实词", "改变数字、专名和术语", "合并或拆分段落"],
        })
    write_jsonl(args.tasks, tasks)
    print(json.dumps({"status": "prepared", "tasks": len(tasks), "output": str(args.tasks)}, ensure_ascii=False))
    return 0


def apply(args: argparse.Namespace) -> int:
    paths = {name: args.corpus_root / name for name in ("documents.jsonl", "sections.jsonl", "paragraphs.jsonl", "sentences.jsonl")}
    paragraphs = read_jsonl(paths["paragraphs.jsonl"])
    responses = read_jsonl(args.responses)
    response_by_id: dict[str, dict[str, Any]] = {}
    rejected: list[dict[str, Any]] = []
    applied_details: list[dict[str, Any]] = []
    for record in responses:
        parsed = record.get("parsed") if record.get("status") == "success" else None
        if isinstance(parsed, dict) and parsed.get("paragraph_id"):
            response_by_id[str(parsed["paragraph_id"])] = parsed
    changed = 0
    for row in paragraphs:
        parsed = response_by_id.get(str(row.get("paragraph_id")))
        if not parsed or parsed.get("decision") != "correct":
            continue
        original = str(row.get("text", ""))
        corrected = str(parsed.get("corrected_text") or "").strip()
        if not corrected:
            rejected.append({"paragraph_id": row.get("paragraph_id"), "reason": "模型要求修改但 corrected_text 为空"})
            continue
        safe, safe_reason = safe_cleaning_correction(
            original, corrected, list(parsed.get("changes") or [])
        )
        if not safe:
            rejected.append({"paragraph_id": row.get("paragraph_id"), "reason": f"自动安全门拒绝应用：{safe_reason}", "original": original, "proposed": corrected})
            continue
        if corrected != original:
            applied_details.append({"paragraph_id": row.get("paragraph_id"), "original": original, "corrected": corrected, "issue_types": parsed.get("issue_types", []), "changes": parsed.get("changes", [])})
            row["text"] = corrected; row["char_count"] = len(corrected); row["text_hash"] = digest(corrected); changed += 1

    if changed:
        backup = args.output_root / "backup_before_apply"
        backup.mkdir(parents=True, exist_ok=True)
        for path in paths.values():
            if path.exists() and not (backup / path.name).exists(): shutil.copy2(path, backup / path.name)
        write_jsonl(paths["paragraphs.jsonl"], paragraphs)
        by_doc: dict[str, list[dict[str, Any]]] = defaultdict(list)
        for row in paragraphs: by_doc[str(row["doc_id"])].append(row)
        sections = read_jsonl(paths["sections.jsonl"])
        for section in sections:
            rows = by_doc[str(section["doc_id"])]
            start, end = int(section["start_paragraph_index"]), int(section["end_paragraph_index"])
            text = "\n\n".join(str(row["text"]) for row in rows[start:end + 1])
            section["text"] = text; section["text_hash"] = digest(text); section["paragraph_count"] = max(end - start + 1, 0)
        write_jsonl(paths["sections.jsonl"], sections)
        sentence_rows: list[dict[str, Any]] = []
        for doc_id, rows in by_doc.items():
            sentence_index = 0
            for paragraph in rows:
                for local_index, sentence in enumerate(split_sentences(str(paragraph["text"]))):
                    item = {key: paragraph.get(key) for key in ("doc_id", "paragraph_id", "paragraph_index", "section_id", "section_level", "section_path", "is_heading", "source_type", "source", "quality", "relative_path")}
                    item.update({"sentence_id": f"{doc_id}__s_{sentence_index + 1:06d}", "sentence_index": sentence_index, "sentence_index_in_paragraph": local_index, "text_hash": digest(sentence), "text": sentence, "char_count": len(sentence)})
                    sentence_rows.append(item); sentence_index += 1
        write_jsonl(paths["sentences.jsonl"], sentence_rows)
        docs = read_jsonl(paths["documents.jsonl"])
        sentence_counts: dict[str, int] = defaultdict(int)
        for sentence in sentence_rows: sentence_counts[str(sentence["doc_id"])] += 1
        for doc in docs:
            rows = by_doc[str(doc["doc_id"])]
            text = "\n\n".join(str(row["text"]) for row in rows)
            doc["text"] = text; doc["clean_chars"] = len(text); doc["norm_chars"] = len(re.sub(r"\s+", "", text)); doc["text_hash"] = digest(text); doc["sentence_count"] = sentence_counts[str(doc["doc_id"])]
            clean_path_value = str(doc.get("clean_text_path", "")).strip()
            if clean_path_value:
                clean_path = Path(clean_path_value); clean_path.parent.mkdir(parents=True, exist_ok=True); clean_path.write_text(text, encoding="utf-8")
        write_jsonl(paths["documents.jsonl"], docs)

    args.output_root.mkdir(parents=True, exist_ok=True)
    decisions = Counter(str(row.get("decision", "unknown")) for row in response_by_id.values())
    report = {"applied_at": datetime.now().astimezone().isoformat(timespec="seconds"), "response_records": len(responses), "parsed_records": len(response_by_id), "decisions": dict(decisions), "human_review_required": decisions.get("needs_human_review", 0), "paragraphs_changed": changed, "applied_details": applied_details, "unsafe_proposals_rejected": len(rejected), "rejected_details": rejected}
    (args.output_root / "llm_cleaning_report.json").write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="可审计的大模型清洗复核")
    sub = parser.add_subparsers(dest="command", required=True)
    for name in ("prepare", "apply"):
        child = sub.add_parser(name)
        child.add_argument("--corpus-root", type=Path, required=True)
        child.add_argument("--tasks", type=Path, default=PROJECT_ROOT / "data" / "processed" / "cleaning_llm" / "tasks.jsonl")
        child.add_argument("--responses", type=Path, default=PROJECT_ROOT / "data" / "processed" / "cleaning_llm" / "responses.jsonl")
        child.add_argument("--output-root", type=Path, default=PROJECT_ROOT / "data" / "processed" / "cleaning_llm")
    args = parser.parse_args()
    return prepare(args) if args.command == "prepare" else apply(args)


if __name__ == "__main__":
    raise SystemExit(main())
