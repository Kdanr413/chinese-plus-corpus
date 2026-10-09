"""Adapters from existing corpus JSONL rows to the shared Document contract."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any, Iterable, Iterator, Mapping

from .models import Document


def _checksum(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def remove_database_nul(value: str) -> tuple[str, int]:
    """Remove U+0000, which PostgreSQL text/jsonb cannot represent."""
    count = value.count("\x00")
    return (value.replace("\x00", "") if count else value), count


def adapt_legacy_document(
    row: Mapping[str, Any], *, processing_version: str = "legacy-jsonl/1.0"
) -> Document:
    """Map historical ``doc_id``/``text`` without rewriting the source row."""
    document_id = str(row.get("document_id") or row.get("doc_id") or "").strip()
    if not document_id:
        raise ValueError("legacy document row is missing document_id/doc_id")
    clean_text_source = str(row.get("clean_text") or row.get("text") or "")
    clean_text, removed_nul_count = remove_database_nul(clean_text_source)
    raw_text = str(row.get("raw_text") or row.get("text") or clean_text)
    excluded = {"document_id", "doc_id", "raw_text", "clean_text", "text"}
    metadata = {str(key): value for key, value in row.items() if key not in excluded}
    if removed_nul_count:
        metadata["removed_database_nul_characters"] = removed_nul_count
    return Document(
        document_id=document_id,
        source=str(row.get("source") or row.get("source_type") or "unknown"),
        raw_text=raw_text,
        clean_text=clean_text,
        metadata=metadata,
        processing_version=processing_version,
        raw_checksum=_checksum(raw_text),
        clean_checksum=_checksum(clean_text),
    )


def load_documents_jsonl(
    path: str | Path, *, processing_version: str = "legacy-jsonl/1.0"
) -> Iterator[Document]:
    source = Path(path)
    with source.open("r", encoding="utf-8-sig") as handle:
        for line_number, line in enumerate(handle, start=1):
            if not line.strip():
                continue
            try:
                row = json.loads(line)
                yield adapt_legacy_document(row, processing_version=processing_version)
            except (TypeError, ValueError) as exc:
                raise ValueError(f"invalid document JSONL at {source}:{line_number}: {exc}") from exc


def write_term_candidates_jsonl(path: str | Path, rows: Iterable[Any]) -> int:
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    count = 0
    with target.open("w", encoding="utf-8", newline="\n") as handle:
        for row in rows:
            payload = row.model_dump(mode="json") if hasattr(row, "model_dump") else dict(row)
            handle.write(json.dumps(payload, ensure_ascii=False) + "\n")
            count += 1
    return count


def write_topic_candidates_jsonl(path: str | Path, rows: Iterable[Any]) -> int:
    """Write inspectable candidate data; this is not a curated taxonomy or Release."""
    return write_term_candidates_jsonl(path, rows)
