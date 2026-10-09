"""Create immutable, per-document subset views for LangSC BCC indexing."""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable, Mapping

from .errors import CorpusSelectionError, CorpusSelectionExistsError


FilterValues = str | Iterable[str]


@dataclass(frozen=True)
class CorpusSelectionResult:
    """Files and counts produced for one reproducible corpus selection."""

    source_path: Path
    output_dir: Path
    manifest_path: Path
    corpus_files: tuple[Path, ...]
    total_items: int
    selected_items: int
    selected_documents: tuple[str, ...]
    selected_item_ids: tuple[str, ...]
    source_sha256: str
    corpus_sha256: tuple[tuple[str, str], ...]
    normalized_parenthesis_tokens: int
    output_encoding: str


@dataclass(frozen=True)
class _CorpusItem:
    document_number: int
    document_name: str
    body_lines: tuple[bytes, ...]
    global_metadata: Mapping[str, tuple[str, ...]]
    group_metadata: Mapping[str, tuple[str, ...]]
    document_id: str = ""
    item_id: str = ""


def create_corpus_selection(
    source_path: str | Path,
    output_dir: str | Path,
    *,
    catalog_path: str | Path | None = None,
    item_ids: Iterable[str] | None = None,
    document_names: Iterable[str] | None = None,
    global_filters: Mapping[str, FilterValues] | None = None,
    group_filters: Mapping[str, FilterValues] | None = None,
    normalize_bcc_parentheses: bool = True,
    output_encoding: str = "gb18030",
) -> CorpusSelectionResult:
    """Create one BCC input file per selected document.

    All supplied criteria are combined with AND. Multiple accepted values for
    one metadata key are combined with OR. The source file is never modified.
    Stable catalog ``doc_id`` values become BCC ``Doc`` identifiers; original
    document names remain in the selection manifest. ``output_encoding`` may be
    ``gb18030`` (the backward-compatible default) or ``gbk``.  The latter is
    useful with LangSC 2.0.7, whose wrapper recognizes only the literal encoding
    name ``gbk`` and may otherwise rewrite valid GB18030 input incorrectly.
    By default, literal ASCII
    parenthesis tokens in tree leaves are changed to full-width parentheses in
    this derived BCC view only, because BCC uses ASCII parentheses as its tree
    delimiters. The source corpus is never rewritten.
    """
    source = Path(source_path).expanduser().resolve()
    output = Path(output_dir).expanduser().resolve()
    catalog = Path(catalog_path).expanduser().resolve() if catalog_path else None
    manifest = output / "selection.json"

    if not isinstance(output_encoding, str):
        raise CorpusSelectionError("output_encoding must be 'gb18030' or 'gbk'")
    output_encoding = output_encoding.strip().lower()
    if output_encoding not in {"gb18030", "gbk"}:
        raise CorpusSelectionError("output_encoding must be 'gb18030' or 'gbk'")

    if not source.is_file():
        raise CorpusSelectionError(f"structured corpus does not exist: {source}")
    if output == source.parent:
        raise CorpusSelectionError("selection output directory must be separate")
    if output.exists() and any(output.iterdir()):
        raise CorpusSelectionExistsError(
            f"refusing to overwrite a non-empty selection directory: {output}"
        )

    raw = source.read_bytes()
    encoding = _detect_encoding(raw)
    items = _parse_items(raw, encoding)
    catalog_rows = _read_catalog(catalog) if catalog else None
    if catalog_rows is not None:
        items = _attach_catalog(items, catalog_rows)

    wanted_ids = _normalize_set(item_ids)
    wanted_documents = _normalize_set(document_names)
    wanted_global = _normalize_filters(global_filters)
    wanted_group = _normalize_filters(group_filters)
    if wanted_ids is not None and catalog_rows is None:
        raise CorpusSelectionError("item_ids selection requires catalog_path")

    selected = [
        item
        for item in items
        if _matches(
            item,
            wanted_ids,
            wanted_documents,
            wanted_global,
            wanted_group,
        )
    ]
    if not selected:
        raise CorpusSelectionError("the supplied criteria selected zero corpus items")

    grouped: dict[int, list[_CorpusItem]] = {}
    for item in selected:
        grouped.setdefault(item.document_number, []).append(item)

    output.mkdir(parents=True, exist_ok=True)
    corpus_files: list[Path] = []
    corpus_hashes: list[tuple[str, str]] = []
    document_manifest: list[dict[str, object]] = []
    normalized_parenthesis_tokens = 0
    try:
        for sequence, document_items in enumerate(grouped.values(), 1):
            first = document_items[0]
            stable_id = first.document_id or f"selection_doc_{first.document_number:04d}"
            bcc_document_id = _bcc_document_identifier(stable_id, sequence)
            safe_id = _safe_identifier(bcc_document_id, sequence)
            filename = f"corpus_{sequence:04d}_{safe_id}.txt"
            path = output / filename
            data, normalized_count = _render_document(
                bcc_document_id,
                document_items,
                encoding,
                output_encoding,
                normalize_bcc_parentheses=normalize_bcc_parentheses,
            )
            normalized_parenthesis_tokens += normalized_count
            with path.open("xb") as stream:
                stream.write(data)
            digest = _sha256(data)
            corpus_files.append(path)
            corpus_hashes.append((filename, digest))
            document_manifest.append(
                {
                    "document_id": stable_id,
                    "bcc_document_id": bcc_document_id,
                    "document_name": first.document_name,
                    "file": filename,
                    "sha256": digest,
                    "selected_items": len(document_items),
                }
            )

        manifest_data = {
            "schema_version": 2,
            "created_at": datetime.now(timezone.utc).isoformat(),
            "source_path": source.as_posix(),
            "source_sha256": _sha256(raw),
            "catalog_path": catalog.as_posix() if catalog else "",
            "catalog_sha256": _sha256(catalog.read_bytes()) if catalog else "",
            "output_dir": output.as_posix(),
            "output_encoding": output_encoding,
            "corpus_files": document_manifest,
            "total_items": len(items),
            "selected_items": len(selected),
            "selected_item_ids": [item.item_id for item in selected if item.item_id],
            "bcc_compatibility": {
                "normalize_ascii_parenthesis_tokens": normalize_bcc_parentheses,
                "normalized_token_count": normalized_parenthesis_tokens,
                "reason": (
                    "LangSC BCC uses ASCII parentheses as tree delimiters; "
                    "only the derived index input uses full-width equivalents."
                ),
            },
            "criteria": {
                "item_ids": sorted(wanted_ids) if wanted_ids is not None else None,
                "document_names": (
                    sorted(wanted_documents)
                    if wanted_documents is not None
                    else None
                ),
                "global_filters": _serializable_filters(wanted_global),
                "group_filters": _serializable_filters(wanted_group),
            },
        }
        with manifest.open("x", encoding="utf-8", newline="\n") as stream:
            json.dump(manifest_data, stream, ensure_ascii=False, indent=2)
            stream.write("\n")
    except (OSError, UnicodeError) as exc:
        raise CorpusSelectionError(
            f"failed to write corpus selection; partial output may remain: {exc}"
        ) from exc

    return CorpusSelectionResult(
        source_path=source,
        output_dir=output,
        manifest_path=manifest,
        corpus_files=tuple(corpus_files),
        total_items=len(items),
        selected_items=len(selected),
        selected_documents=tuple(
            dict.fromkeys(item.document_name for item in selected)
        ),
        selected_item_ids=tuple(item.item_id for item in selected if item.item_id),
        source_sha256=_sha256(raw),
        corpus_sha256=tuple(corpus_hashes),
        normalized_parenthesis_tokens=normalized_parenthesis_tokens,
        output_encoding=output_encoding,
    )


def _detect_encoding(raw: bytes) -> str:
    if raw.startswith(b"\xef\xbb\xbf"):
        return "utf-8-sig"
    for encoding in ("utf-8", "gb18030"):
        try:
            raw.decode(encoding)
            return encoding
        except UnicodeDecodeError:
            continue
    raise CorpusSelectionError("structured corpus must be UTF-8 or GB18030")


def _parse_items(raw: bytes, encoding: str) -> list[_CorpusItem]:
    items: list[_CorpusItem] = []
    document_name = ""
    document_number = 0
    pending: list[bytes] = []

    for raw_line in raw.splitlines(keepends=True):
        line = raw_line.decode(encoding).rstrip("\r\n")
        if line.startswith("Doc "):
            if any(value.strip() for value in pending):
                raise CorpusSelectionError("unexpected lines before the next Doc marker")
            document_number += 1
            document_name = line[4:].strip()
            pending = []
            continue
        if document_number == 0:
            if line.strip():
                raise CorpusSelectionError("content appears before the first Doc marker")
            continue
        pending.append(raw_line)
        if not line.startswith("Item:"):
            continue
        decoded = [value.decode(encoding).rstrip("\r\n") for value in pending]
        global_lines = [value for value in decoded if value.startswith("#Global")]
        group_lines = [value for value in decoded if value.startswith("#Group")]
        if not global_lines or not group_lines:
            raise CorpusSelectionError(
                "each Item must retain preceding #Global and #Group markers"
            )
        items.append(
            _CorpusItem(
                document_number=document_number,
                document_name=document_name,
                body_lines=tuple(pending),
                global_metadata=_parse_metadata(global_lines, "#Global"),
                group_metadata=_parse_metadata(group_lines, "#Group"),
            )
        )
        pending = []

    if any(value.strip() for value in pending):
        raise CorpusSelectionError("structured corpus ends without an Item marker")
    if not items:
        raise CorpusSelectionError("structured corpus contains no Item records")
    return items


def _parse_metadata(
    lines: Iterable[str], marker: str
) -> Mapping[str, tuple[str, ...]]:
    values: dict[str, list[str]] = {}
    for line in lines:
        body = line[len(marker):].strip()
        for pair in body.split(";"):
            if not pair.strip() or "=" not in pair:
                continue
            key, value = pair.split("=", 1)
            values.setdefault(key.strip(), []).append(value.strip())
    return {key: tuple(items) for key, items in values.items()}


def _read_catalog(path: Path) -> list[dict[str, object]]:
    if not path.is_file():
        raise CorpusSelectionError(f"selection catalog does not exist: {path}")
    rows: list[dict[str, object]] = []
    try:
        with path.open("r", encoding="utf-8-sig") as stream:
            for line_number, line in enumerate(stream, 1):
                if not line.strip():
                    continue
                value = json.loads(line)
                if not isinstance(value, dict):
                    raise CorpusSelectionError(
                        f"catalog line {line_number} must contain a JSON object"
                    )
                rows.append(value)
    except (OSError, json.JSONDecodeError) as exc:
        raise CorpusSelectionError(f"invalid selection catalog: {exc}") from exc
    return rows


def _attach_catalog(
    items: list[_CorpusItem], rows: list[dict[str, object]]
) -> list[_CorpusItem]:
    if len(items) != len(rows):
        raise CorpusSelectionError(
            f"catalog/item count mismatch: {len(rows)} catalog rows for {len(items)} items"
        )
    attached: list[_CorpusItem] = []
    seen_ids: set[str] = set()
    doc_ids: dict[str, str] = {}
    for number, (item, row) in enumerate(zip(items, rows), 1):
        item_id = row.get("item_id")
        doc_name = row.get("doc_name")
        doc_id = row.get("doc_id", "")
        if not isinstance(item_id, str) or not item_id:
            raise CorpusSelectionError(f"catalog row {number} has no item_id")
        if item_id in seen_ids:
            raise CorpusSelectionError(f"duplicate catalog item_id: {item_id}")
        if doc_name != item.document_name:
            raise CorpusSelectionError(
                f"catalog row {number} doc_name does not match the structured corpus"
            )
        if not isinstance(doc_id, str):
            raise CorpusSelectionError(f"catalog row {number} has an invalid doc_id")
        known = doc_ids.setdefault(item.document_name, doc_id)
        if known != doc_id:
            raise CorpusSelectionError(
                f"catalog uses multiple doc_id values for {item.document_name}"
            )
        seen_ids.add(item_id)
        attached.append(
            _CorpusItem(
                document_number=item.document_number,
                document_name=item.document_name,
                body_lines=item.body_lines,
                global_metadata=item.global_metadata,
                group_metadata=item.group_metadata,
                document_id=doc_id,
                item_id=item_id,
            )
        )
    return attached


def _normalize_set(values: Iterable[str] | None) -> set[str] | None:
    if values is None:
        return None
    if isinstance(values, str):
        values = [values]
    normalized = {
        value.strip()
        for value in values
        if isinstance(value, str) and value.strip()
    }
    if not normalized:
        raise CorpusSelectionError("selection values cannot be empty")
    return normalized


def _normalize_filters(
    filters: Mapping[str, FilterValues] | None,
) -> dict[str, set[str]] | None:
    if filters is None:
        return None
    normalized: dict[str, set[str]] = {}
    for key, values in filters.items():
        if not isinstance(key, str) or not key.strip():
            raise CorpusSelectionError("metadata filter keys must be non-empty strings")
        candidates = [values] if isinstance(values, str) else list(values)
        accepted = {
            value.strip()
            for value in candidates
            if isinstance(value, str) and value.strip()
        }
        if not accepted:
            raise CorpusSelectionError(f"metadata filter {key!r} has no values")
        normalized[key.strip()] = accepted
    return normalized


def _matches(
    item: _CorpusItem,
    item_ids: set[str] | None,
    document_names: set[str] | None,
    global_filters: dict[str, set[str]] | None,
    group_filters: dict[str, set[str]] | None,
) -> bool:
    if item_ids is not None and item.item_id not in item_ids:
        return False
    if document_names is not None and item.document_name not in document_names:
        return False
    if not _metadata_matches(item.global_metadata, global_filters):
        return False
    return _metadata_matches(item.group_metadata, group_filters)


def _metadata_matches(
    metadata: Mapping[str, tuple[str, ...]],
    filters: dict[str, set[str]] | None,
) -> bool:
    if filters is None:
        return True
    return all(set(metadata.get(key, ())) & accepted for key, accepted in filters.items())


def _render_document(
    document_id: str,
    items: list[_CorpusItem],
    encoding: str,
    output_encoding: str,
    *,
    normalize_bcc_parentheses: bool,
) -> tuple[bytes, int]:
    newline = _line_ending(items[0].body_lines[0])
    parts = [f"Doc {document_id}".encode(output_encoding) + newline]
    normalized_count = 0
    for item in items:
        for raw_line in item.body_lines:
            if normalize_bcc_parentheses:
                raw_line, count = _normalize_tree_parenthesis_tokens(
                    raw_line, encoding, output_encoding
                )
                normalized_count += count
            else:
                raw_line = raw_line.decode(encoding).encode(output_encoding)
            parts.append(raw_line)
    return b"".join(parts), normalized_count


def _normalize_tree_parenthesis_tokens(
    raw_line: bytes, source_encoding: str, output_encoding: str
) -> tuple[bytes, int]:
    """Remove BCC's delimiter ambiguity without touching the source asset."""
    text = raw_line.decode(source_encoding)
    if not text.startswith("Item:"):
        return text.encode(output_encoding), 0
    left_count = text.count("(x ()")
    right_count = text.count("(x ))")
    normalized = text.replace("(x ()", "(x （)").replace("(x ))", "(x ）)")
    return normalized.encode(output_encoding), left_count + right_count


def _line_ending(line: bytes) -> bytes:
    if line.endswith(b"\r\n"):
        return b"\r\n"
    return b"\n"


def _safe_identifier(value: str, sequence: int) -> str:
    safe = re.sub(r"[^A-Za-z0-9._-]+", "_", value).strip("._-")
    return safe or f"doc_{sequence:04d}"


def _bcc_document_identifier(value: str, sequence: int) -> str:
    """Return a stable ASCII identifier long enough for BCC's format probe."""
    safe = _safe_identifier(value, sequence)
    return f"bcc_document_{sequence:04d}_{safe}"


def _serializable_filters(
    filters: dict[str, set[str]] | None,
) -> dict[str, list[str]] | None:
    if filters is None:
        return None
    return {key: sorted(values) for key, values in filters.items()}


def _sha256(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()
