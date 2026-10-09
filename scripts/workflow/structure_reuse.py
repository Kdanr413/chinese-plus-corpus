"""Safe per-source reuse metadata for corpus annotation and structuring."""

from __future__ import annotations

import csv
import hashlib
import json
from functools import lru_cache
from pathlib import Path
from typing import Any, Mapping


MANIFEST_RELATIVE_PATH = Path("data/processed/index/structure_reuse_manifest.json")
SCHEMA_VERSION = "1.0"
_VOLATILE_KEYS = {
    "created_at", "updated_at", "generated_at", "published_at",
    "reviewed_at", "activated_at", "run_id", "recognition_run_id",
}


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


@lru_cache(maxsize=4096)
def _cached_file_sha256(path_value: str, size: int, modified_ns: int) -> str:
    del size, modified_ns
    return sha256_file(Path(path_value))


def source_sha256(path: Path, *, cached: bool = True) -> str:
    if not cached:
        return sha256_file(path)
    stat = path.stat()
    return _cached_file_sha256(str(path.resolve()), stat.st_size, stat.st_mtime_ns)


def _without_volatile(value: Any) -> Any:
    if isinstance(value, dict):
        return {
            str(key): _without_volatile(item)
            for key, item in sorted(value.items())
            if str(key) not in _VOLATILE_KEYS
        }
    if isinstance(value, list):
        return [_without_volatile(item) for item in value]
    return value


def _semantic_sha256(path: Path) -> str:
    """Hash resource meaning while ignoring timestamps and harmless CSV ordering."""
    suffix = path.suffix.lower()
    try:
        if suffix == ".json":
            value = json.loads(path.read_text(encoding="utf-8-sig"))
            payload = json.dumps(
                _without_volatile(value), ensure_ascii=False, sort_keys=True,
                separators=(",", ":"),
            ).encode("utf-8")
            return hashlib.sha256(payload).hexdigest()
        if suffix == ".csv":
            with path.open("r", encoding="utf-8-sig", newline="") as handle:
                rows = [_without_volatile(dict(row)) for row in csv.DictReader(handle)]
            rows.sort(key=lambda row: json.dumps(row, ensure_ascii=False, sort_keys=True))
            payload = json.dumps(
                rows, ensure_ascii=False, sort_keys=True, separators=(",", ":")
            ).encode("utf-8")
            return hashlib.sha256(payload).hexdigest()
    except (UnicodeError, csv.Error, json.JSONDecodeError):
        pass
    return sha256_file(path)


def _configured_path(
    project_root: Path, config: Mapping[str, Any], dotted: str
) -> Path | None:
    section, key = dotted.split(".", 1)
    value = config.get(section, {}).get(key)
    return (project_root / str(value)).resolve() if value else None


def resource_snapshot(project_root: Path, config: Mapping[str, Any]) -> dict[str, Any]:
    """Fingerprint every resource that can change structure annotations or tokens."""
    candidates: dict[str, list[Path | None]] = {
        "topic_system": [
            project_root / "data/curated/topics/taxonomy.json",
            _configured_path(project_root, config, "topics.reference_taxonomy"),
        ],
        "terminology": [
            _configured_path(project_root, config, "linguistic.terminology"),
            project_root / "data/curated/terms/terminology.csv",
            project_root / "data/curated/terms/term_variants.csv",
        ],
        "segmentation": [
            _configured_path(project_root, config, "linguistic.jieba_user_dict"),
            project_root / "data/curated/terms/segmentation_dictionary.txt",
        ],
        # Per-unit topic and term labels directly change the metadata written
        # into structured corpus items.  They must therefore invalidate a
        # previously reusable structure/index even when the taxonomy and term
        # dictionary files themselves have not changed.
        "sentence_annotations": [
            project_root / "data/curated/annotations/sentence_annotations.jsonl",
        ],
    }
    groups: dict[str, list[dict[str, Any]]] = {}
    for group, paths in candidates.items():
        entries: list[dict[str, Any]] = []
        seen: set[Path] = set()
        for candidate in paths:
            if candidate is None:
                continue
            path = candidate.resolve()
            if path in seen:
                continue
            seen.add(path)
            relative = path.relative_to(project_root).as_posix()
            entries.append({
                "path": relative,
                "exists": path.is_file(),
                "sha256": _semantic_sha256(path) if path.is_file() else "",
            })
        groups[group] = entries
    policy = {
        "pipeline_version": str(config.get("workflow", {}).get("pipeline_version", "")),
        "corpus_format_version": str(config.get("workflow", {}).get("corpus_format_version", "")),
        "index": dict(config.get("index", {})),
    }
    encoded = json.dumps(
        {"groups": groups, "policy": policy}, ensure_ascii=False,
        sort_keys=True, separators=(",", ":"),
    ).encode("utf-8")
    return {
        "fingerprint": hashlib.sha256(encoded).hexdigest(),
        "groups": groups,
        "policy": policy,
    }


def load_manifest(project_root: Path) -> dict[str, Any]:
    path = project_root / MANIFEST_RELATIVE_PATH
    if not path.is_file():
        return {}
    try:
        value = json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, json.JSONDecodeError):
        return {}
    return value if isinstance(value, dict) else {}


def reuse_status(
    project_root: Path,
    config: Mapping[str, Any],
    source_path: Path,
    *,
    manifest: Mapping[str, Any] | None = None,
    resources: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Explain whether one selected source can reuse its structured Item rows."""
    manifest = dict(manifest or load_manifest(project_root))
    resources = dict(resources or resource_snapshot(project_root, config))
    relative = source_path.resolve().relative_to(project_root.resolve()).as_posix()
    base = {
        "source_sha256": "",
        "resource_fingerprint": str(resources.get("fingerprint", "")),
        "manifest_path": MANIFEST_RELATIVE_PATH.as_posix(),
    }
    # Check cheap global invalidators before hashing the source.  The workbench
    # calls this function for every uploaded file; hashing the whole corpus is
    # wasted work when no compatible manifest exists or shared resources have
    # already changed.
    if manifest.get("schema_version") != SCHEMA_VERSION:
        return {**base, "reusable": False, "reason": "尚无逐文件结构化复用记录"}
    required = (
        project_root / "data/processed/index/corpus_index.txt",
        project_root / "data/processed/index/index_items.jsonl",
    )
    if not all(path.is_file() for path in required):
        return {**base, "reusable": False, "reason": "结构化索引成果不完整"}
    if str(manifest.get("resource_fingerprint", "")) != base["resource_fingerprint"]:
        return {**base, "reusable": False, "reason": "话题、术语或分词资源已经变化"}
    sources = manifest.get("sources", {})
    entry = sources.get(relative.casefold()) if isinstance(sources, dict) else None
    if not isinstance(entry, dict):
        return {**base, "reusable": False, "reason": "该文件没有对应的结构化缓存"}
    base["source_sha256"] = source_sha256(source_path)
    if str(entry.get("source_sha256", "")) != base["source_sha256"]:
        return {**base, "reusable": False, "reason": "原始语料内容已经变化"}
    if int(entry.get("item_count", 0)) < 1:
        return {**base, "reusable": False, "reason": "对应结构化条目为空"}
    return {
        **base,
        "reusable": True,
        "reason": "语料及话题、术语、分词资源均未变化，可复用",
        "doc_id": str(entry.get("doc_id", "")),
        "item_count": int(entry.get("item_count", 0)),
    }
