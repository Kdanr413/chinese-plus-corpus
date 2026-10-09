"""Stable application interface around the existing :class:`LangSC.BCC` engine.

This module deliberately contains no corpus search algorithm.  It separates the
native engine's index-build lifecycle from its read-only query lifecycle and
normalizes JSON results for callers such as a future FastAPI application.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import threading
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Iterable, Protocol, TypeAlias

from .errors import (
    CorpusIndexError,
    CorpusIndexExistsError,
    CorpusQueryError,
    CorpusSearchDependencyError,
)


JSONValue: TypeAlias = dict[str, Any] | list[Any] | str | int | float | bool | None
BackendFactory: TypeAlias = Callable[[str], "BCCBackend"]
ScopeRange: TypeAlias = tuple[int, int]

# LangSC 2.0.7 has process-global initialization state in bcc.py, while each
# BCC object reuses a mutable return buffer.  Serializing every native call is
# the conservative boundary for server use until the DLL documents otherwise.
_NATIVE_CALL_LOCK = threading.RLock()

REQUIRED_INDEX_FILES = (
    "IdxUnit.dat",
    "DatWord.dat",
    "Org.dat",
    "SentInfo.dat",
)


class BCCBackend(Protocol):
    """The small portion of LangSC.BCC used by this adapter."""

    dataPath: str

    def IndexBCC(self, filelistname: object, **options: object) -> int: ...

    def Run(self, query: str, **options: object) -> object: ...

    def CallBCC(self, query: str) -> object: ...

    def AddBCCKV(self, key: str, value: str) -> object: ...

    def GetBCCKV(self, key: str = "") -> object: ...

    def ClearBCCKV(self, key: str = "") -> object: ...


@dataclass(frozen=True)
class IndexBuildResult:
    """Description of an index produced by the existing BCC engine."""

    index_dir: Path
    status_code: int
    files: tuple[str, ...]


@dataclass(frozen=True)
class ScopeIndexBuildResult:
    """Description of verified per-scope BCC indexes."""

    index_dir: Path
    manifest_path: Path
    scope_count: int


@dataclass(frozen=True)
class ScopeIndex:
    """Mapping from one global ``Org.txt`` range to one independent index."""

    start: int
    end: int
    title: str
    index_dir: Path


@dataclass(frozen=True)
class OrgScope:
    """One human-readable BCC organization range from ``Org.txt``."""

    start: int
    end: int
    title: str
    extras: tuple[str, ...] = ()

    @property
    def query_range(self) -> ScopeRange:
        return (self.start, self.end)


class CorpusSearchEngine:
    """A loaded, reusable, read-only view of one BCC index directory."""

    def __init__(
        self,
        index_dir: Path,
        backend: BCCBackend,
        scope_indexes: tuple[ScopeIndex, ...] = (),
    ) -> None:
        self.index_dir = index_dir
        self._backend = backend
        self._scope_indexes = scope_indexes

    def available_scopes(self) -> tuple[OrgScope, ...]:
        """Return the index ranges that a user may select for a query."""
        return read_org_scopes(self.index_dir)

    def raw_search(
        self,
        query: str,
        *,
        command: str = "Freq",
        scope_ranges: Iterable[ScopeRange] | None = None,
        **options: object,
    ) -> str:
        """Run a BCC query and preserve LangSC's original JSON-string result."""
        query = _validate_query(query)
        params = _query_options(command, options)
        selected_scope = self._resolve_scope_index(scope_ranges)
        if selected_scope is not None:
            result = _run_scope_worker(selected_scope.index_dir, query, params)
            return json.dumps(result, ensure_ascii=False)
        if scope_ranges is not None and self._scope_indexes:
            # Selecting every registered range is exactly the whole corpus.
            scope_ranges = None
        elif scope_ranges is not None:
            raise CorpusQueryError(
                "verified scoped search requires scope_indexes.json; "
                "build per-scope indexes before using scope_ranges"
            )
        try:
            with _NATIVE_CALL_LOCK:
                result = self._backend.Run(query, **params)
        except Exception as exc:
            raise CorpusQueryError(f"LangSC BCC query failed: {exc}") from exc

        if isinstance(result, bytes):
            try:
                return result.decode("utf-8")
            except UnicodeDecodeError as exc:
                raise CorpusQueryError("LangSC returned non-UTF-8 bytes") from exc
        if not isinstance(result, str):
            raise CorpusQueryError(
                f"LangSC returned {type(result).__name__}, expected a JSON string"
            )
        return result

    def _resolve_scope_index(
        self, scope_ranges: Iterable[ScopeRange] | None
    ) -> ScopeIndex | None:
        if scope_ranges is None:
            return None
        normalized = _normalize_scope_ranges(scope_ranges)
        if not self._scope_indexes:
            return None
        known = {(item.start, item.end): item for item in self._scope_indexes}
        unknown = [value for value in normalized if value not in known]
        if unknown:
            raise CorpusQueryError(
                f"scope range is not registered in scope_indexes.json: {unknown[0]}"
            )
        if len(normalized) == 1:
            return known[normalized[0]]
        if set(normalized) == set(known) and len(normalized) == len(known):
            return None
        raise CorpusQueryError(
            "partial multi-scope aggregation is not yet verified; select one "
            "Org.txt range or all registered ranges"
        )

    def search(
        self,
        query: str,
        *,
        command: str = "Freq",
        scope_ranges: Iterable[ScopeRange] | None = None,
        **options: object,
    ) -> JSONValue:
        """Run a query and return JSON-compatible Python data."""
        raw = self.raw_search(
            query,
            command=command,
            scope_ranges=scope_ranges,
            **options,
        )
        try:
            return json.loads(raw)
        except json.JSONDecodeError as exc:
            raise CorpusQueryError("LangSC returned invalid JSON") from exc

    def frequency(
        self,
        query: str,
        *,
        number: int | None = None,
        target: str | None = None,
        context_num: int | None = None,
        scope_ranges: Iterable[ScopeRange] | None = None,
        **options: object,
    ) -> JSONValue:
        """Return BCC ``Freq`` results."""
        _add_native_option(options, "Number", number, minimum=0)
        _add_native_option(options, "Target", target)
        _add_native_option(options, "ContextNum", context_num, minimum=0)
        return self.search(
            query, command="Freq", scope_ranges=scope_ranges, **options
        )

    def kwic(
        self,
        query: str,
        *,
        number: int | None = None,
        page_no: int | None = None,
        win_size: int | None = None,
        scope_ranges: Iterable[ScopeRange] | None = None,
        **options: object,
    ) -> JSONValue:
        """Return BCC ``Context`` (KWIC) results."""
        _add_native_option(options, "Number", number, minimum=0)
        _add_native_option(options, "PageNo", page_no, minimum=0)
        _add_native_option(options, "WinSize", win_size, minimum=1)
        return self.search(
            query, command="Context", scope_ranges=scope_ranges, **options
        )

    def count(
        self,
        query: str,
        *,
        scope_ranges: Iterable[ScopeRange] | None = None,
        **options: object,
    ) -> JSONValue:
        """Return BCC ``Count`` results without changing their native shape."""
        return self.search(
            query, command="Count", scope_ranges=scope_ranges, **options
        )

    def call_bcc(self, query: str) -> object:
        """Execute an already-formed raw BCC query through ``CallBCC``."""
        query = _validate_query(query)
        try:
            with _NATIVE_CALL_LOCK:
                result = self._backend.CallBCC(query)
        except Exception as exc:
            raise CorpusQueryError(f"raw LangSC BCC query failed: {exc}") from exc
        return _decode_native_bytes(result)

    def add_bcc_kv(self, key: str, value: str) -> object:
        """Add a native BCC search-list value (shared mutable engine state)."""
        key = _validate_kv_text("key", key)
        value = _validate_kv_text("value", value)
        try:
            with _NATIVE_CALL_LOCK:
                result = self._backend.AddBCCKV(key, value)
        except Exception as exc:
            raise CorpusQueryError(f"LangSC AddBCCKV failed: {exc}") from exc
        return _decode_native_bytes(result)

    def get_bcc_kv(self, key: str = "") -> object:
        """Read one BCC search list, or all lists when ``key`` is empty."""
        if not isinstance(key, str):
            raise CorpusQueryError("key must be a string")
        try:
            with _NATIVE_CALL_LOCK:
                result = self._backend.GetBCCKV(key)
        except Exception as exc:
            raise CorpusQueryError(f"LangSC GetBCCKV failed: {exc}") from exc
        return _decode_native_bytes(result)

    def clear_bcc_kv(self, key: str | None = None) -> object:
        """Clear all BCC search lists.

        LangSC 2.0.7 ignores the ``Key`` argument of ``ClearBCCKV`` and always
        calls ``ClearKV()``.  Key-specific clearing is rejected to prevent an
        accidental global clear.
        """
        if key is not None and (not isinstance(key, str) or key.strip()):
            raise CorpusQueryError(
                "LangSC 2.0.7 cannot safely clear one BCC KV key; "
                "clear all lists with clear_bcc_kv() instead"
            )
        try:
            with _NATIVE_CALL_LOCK:
                result = self._backend.ClearBCCKV()
        except Exception as exc:
            raise CorpusQueryError(f"LangSC ClearBCCKV failed: {exc}") from exc
        return _decode_native_bytes(result)


def load_corpus(
    index_dir: str | Path,
    *,
    backend_factory: BackendFactory | None = None,
) -> CorpusSearchEngine:
    """Load an existing BCC index without building or modifying it."""
    target = Path(index_dir).expanduser().resolve()
    _validate_index(target)
    scope_indexes = _load_scope_indexes(target)
    backend = _create_backend(target, backend_factory)
    return CorpusSearchEngine(target, backend, scope_indexes)


def build_scope_indexes(
    index_dir: str | Path,
    sources: str | Path | Iterable[str | Path],
    *,
    structure: str = "HZ",
    backend_factory: BackendFactory | None = None,
) -> ScopeIndexBuildResult:
    """Build one immutable BCC child index for each main ``Org.txt`` scope.

    This is a compatibility layer for Langilho/LangSC releases whose native
    ``AddLimit`` returns empty results on otherwise valid locally-built
    indexes.  Search semantics remain those of BCC; SQL is not involved.
    """
    target = Path(index_dir).expanduser().resolve()
    _validate_index(target)
    main_scopes = read_org_scopes(target)
    _, corpus_sources = _normalize_sources(sources)
    if len(corpus_sources) != len(main_scopes):
        raise CorpusIndexError(
            "scope index sources must map one-to-one to Org.txt ranges: "
            f"{len(corpus_sources)} sources for {len(main_scopes)} ranges"
        )

    shard_root = target / "scope_indexes"
    manifest_path = target / "scope_indexes.json"
    if manifest_path.exists() or (shard_root.exists() and any(shard_root.iterdir())):
        raise CorpusIndexExistsError(
            f"refusing to overwrite existing scope indexes in: {target}"
        )
    shard_root.mkdir(parents=True, exist_ok=True)
    entries: list[dict[str, object]] = []
    try:
        for number, (scope, source) in enumerate(
            zip(main_scopes, corpus_sources, strict=True), start=1
        ):
            relative_index = Path("scope_indexes") / f"scope-{number:04d}"
            child_dir = target / relative_index
            if backend_factory is None:
                # LangSC.BCC 2.0.7 keeps native process-global state.  Building
                # many indexes in one long-lived API process eventually makes
                # bcclib.dll raise 0xe06d7363 even though each input is valid.
                # One short-lived worker per scope gives every native build a
                # clean DLL lifecycle and keeps a failure out of the API server.
                _build_scope_worker(child_dir, source, structure)
            else:
                build_index(
                    child_dir,
                    [source],
                    structure=structure,
                    backend_factory=backend_factory,
                )
            child_scopes = read_org_scopes(child_dir)
            expected_length = scope.end - scope.start
            if len(child_scopes) != 1 or (
                child_scopes[0].end - child_scopes[0].start != expected_length
            ):
                raise CorpusIndexError(
                    f"scope index does not match main Org.txt range: {source}"
                )
            entries.append(
                {
                    "start": scope.start,
                    "end": scope.end,
                    "title": scope.title,
                    "index_path": relative_index.as_posix(),
                    "source_name": source.name,
                }
            )
        manifest_path.write_text(
            json.dumps(
                {"schema_version": 1, "scopes": entries},
                ensure_ascii=False,
                indent=2,
            ) + "\n",
            encoding="utf-8",
        )
        _load_scope_indexes(target)
    except Exception:
        # Leave partial files for diagnosis; never hide a failed native build.
        raise
    return ScopeIndexBuildResult(target, manifest_path, len(entries))


def _build_scope_worker(index_dir: Path, source: Path, structure: str) -> None:
    build_index_isolated(index_dir, [source], structure=structure)


def build_index_isolated(
    index_dir: str | Path,
    sources: str | Path | Iterable[str | Path],
    *,
    structure: str = "HZ",
) -> IndexBuildResult:
    """Build a native BCC index in a short-lived process.

    Use this entry point from long-lived services.  It prevents LangSC's
    process-global native state from leaking from indexing into later queries.
    """
    target = Path(index_dir).expanduser().resolve()
    _, normalized_sources = _normalize_sources(sources)
    marker = "__CORPUS_INDEX_RESULT__="
    request = json.dumps(
        {
            "sources": [str(path) for path in normalized_sources],
            "structure": structure,
        },
        ensure_ascii=False,
    )
    creationflags = subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0
    try:
        completed = subprocess.run(
            [
                sys.executable,
                "-m",
                "corpus_search.index_worker",
                str(target),
            ],
            input=request,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=1800,
            check=False,
            creationflags=creationflags,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise CorpusIndexError(f"isolated BCC index worker failed: {exc}") from exc
    payload = next(
        (
            line[len(marker):]
            for line in reversed(completed.stdout.splitlines())
            if line.startswith(marker)
        ),
        None,
    )
    if completed.returncode != 0 or payload is None:
        detail = completed.stderr.strip() or completed.stdout.strip()
        raise CorpusIndexError(
            f"isolated BCC index worker failed with code "
            f"{completed.returncode}: {detail[-2000:]}"
        )
    try:
        envelope = json.loads(payload)
    except json.JSONDecodeError as exc:
        raise CorpusIndexError(
            "isolated BCC index worker returned invalid JSON"
        ) from exc
    if not isinstance(envelope, dict) or not envelope.get("ok"):
        message = envelope.get("error", "unknown scope index worker error") \
            if isinstance(envelope, dict) else "invalid scope index worker response"
        raise CorpusIndexError(f"isolated BCC index worker failed: {message}")
    _validate_index(target)
    raw_files = envelope.get("files", [])
    files = tuple(str(name) for name in raw_files) if isinstance(raw_files, list) else ()
    return IndexBuildResult(target, int(envelope.get("status_code", 1)), files)


def read_org_scopes(index_dir: str | Path) -> tuple[OrgScope, ...]:
    """Parse BCC's human-readable ``Org.txt`` without touching ``Org.dat``."""
    target = Path(index_dir).expanduser().resolve()
    org_path = target / "Org.txt"
    if not org_path.is_file():
        raise CorpusIndexError(f"BCC organization map does not exist: {org_path}")
    try:
        lines = org_path.read_text(encoding="gb18030").splitlines()
    except (OSError, UnicodeDecodeError) as exc:
        raise CorpusIndexError(f"cannot read BCC organization map: {exc}") from exc

    scopes: list[OrgScope] = []
    position = 0
    while position < len(lines):
        header = lines[position]
        if not header.startswith("#"):
            position += 1
            continue
        values = header[1:].split("\t")
        if len(values) != 2 or not all(value.isdigit() for value in values):
            raise CorpusIndexError(f"invalid Org.txt range line: {header!r}")
        start, end = (int(value) for value in values)
        if start < 0 or end < start:
            raise CorpusIndexError(f"invalid Org.txt range: ({start}, {end})")
        position += 1
        title = ""
        extras: list[str] = []
        while position < len(lines) and not lines[position].startswith("#"):
            line = lines[position]
            if line.startswith("T:") and not title:
                title = line[2:]
            elif line:
                extras.append(line)
            position += 1
        scopes.append(OrgScope(start, end, title, tuple(extras)))
    if not scopes:
        raise CorpusIndexError(f"BCC organization map contains no ranges: {org_path}")
    return tuple(scopes)


def build_index(
    index_dir: str | Path,
    sources: str | Path | Iterable[str | Path],
    *,
    structure: str = "HZ",
    backend_factory: BackendFactory | None = None,
) -> IndexBuildResult:
    """Build a new BCC index in an empty directory.

    Existing non-empty directories are rejected so a published Corpus Release
    cannot be overwritten accidentally.  ``sources`` retains the input forms
    accepted by ``BCC.IndexBCC``: directory, single corpus file, file-list file,
    or an iterable of corpus files.
    """
    if structure not in {"HZ", "Segment"}:
        raise CorpusIndexError("structure must be 'HZ' or 'Segment'")

    target = Path(index_dir).expanduser().resolve()
    if target.exists() and any(target.iterdir()):
        raise CorpusIndexExistsError(
            f"refusing to overwrite non-empty index directory: {target}"
        )
    target.mkdir(parents=True, exist_ok=True)
    normalized_sources, corpus_sources = _normalize_sources(sources)
    for source in corpus_sources:
        _validate_bcc_source_encoding(source)
    backend = _create_backend(target, backend_factory)

    try:
        with _NATIVE_CALL_LOCK:
            with _langsc_gb18030_passthrough(backend):
                status = int(
                    backend.IndexBCC(normalized_sources, Structure=structure)
                )
    except Exception as exc:
        raise CorpusIndexError(
            f"LangSC BCC index build failed; partial output may remain in {target}: {exc}"
        ) from exc

    if status <= 0:
        raise CorpusIndexError(
            f"LangSC BCC index build returned status {status}; "
            f"partial output may remain in {target}"
        )
    _validate_index(target)
    files = tuple(
        path.relative_to(target).as_posix()
        for path in sorted(target.rglob("*"))
        if path.is_file()
    )
    return IndexBuildResult(target, status, files)


@contextmanager
def _langsc_gb18030_passthrough(backend: BCCBackend):
    """Avoid LangSC 2.0.7 rewriting valid GB18030 corpus files as UTF-8.

    LangSC's ``file2corpus`` compares its detected encoding with the literal
    string ``gbk``.  A valid file reported as ``GB18030`` is therefore sent
    through a conversion helper that writes with Python's default encoding.
    The native BCC engine can consume these GBK-compatible GB18030 bytes
    directly, so normalize only this detector result for the duration of the
    locked native build call.  No source bytes or installed package files are
    changed.
    """
    if not backend.__class__.__module__.startswith("LangSC."):
        yield
        return
    utils = sys.modules.get("LangSC._utils")
    detector = getattr(utils, "detect_file_encoding", None) if utils else None
    if detector is None:
        yield
        return

    def compatible_detector(*args: object, **kwargs: object) -> object:
        detected = detector(*args, **kwargs)
        if isinstance(detected, str) and detected.lower() == "gb18030":
            return "gbk"
        return detected

    utils.detect_file_encoding = compatible_detector
    try:
        yield
    finally:
        utils.detect_file_encoding = detector


def _default_backend_factory(data_path: str) -> BCCBackend:
    try:
        from LangSC import BCC
    except Exception as exc:
        raise CorpusSearchDependencyError(
            "LangSC could not be imported. Install the pinned search dependency "
            "and verify its native library/pywin32 runtime."
        ) from exc
    return BCC(data_path)


def _create_backend(
    target: Path,
    backend_factory: BackendFactory | None,
) -> BCCBackend:
    engine_path = target.as_posix()
    factory = backend_factory or _default_backend_factory
    try:
        with _NATIVE_CALL_LOCK:
            backend = factory(engine_path)
    except CorpusSearchDependencyError:
        raise
    except Exception as exc:
        raise CorpusSearchDependencyError(
            f"LangSC BCC could not open the engine for {target}: {exc}"
        ) from exc

    # LangSC 2.0.7 rewrites Windows absolute paths as ``./C:/...``.  Resetting
    # the path before IndexBCC/Run keeps its native search behavior unchanged.
    try:
        backend.dataPath = engine_path
    except Exception as exc:
        raise CorpusSearchDependencyError(
            "the configured backend does not expose LangSC BCC.dataPath"
        ) from exc
    return backend


def _validate_index(target: Path) -> None:
    if not target.is_dir():
        raise CorpusIndexError(f"BCC index directory does not exist: {target}")
    missing = [name for name in REQUIRED_INDEX_FILES if not (target / name).is_file()]
    if missing:
        raise CorpusIndexError(
            f"invalid or incomplete BCC index at {target}; missing: {', '.join(missing)}"
        )


def _normalize_sources(
    sources: str | Path | Iterable[str | Path],
) -> tuple[str | list[str], tuple[Path, ...]]:
    if isinstance(sources, (str, Path)):
        source = Path(sources).expanduser().resolve()
        if not source.exists():
            raise CorpusIndexError(f"corpus source does not exist: {source}")
        if source.is_file() and not _looks_like_file_list(source):
            return [source.as_posix()], (source,)
        if source.is_file():
            entries = _read_file_list(source)
            return source.as_posix(), entries
        corpus_files = tuple(path for path in source.rglob("*") if path.is_file())
        return source.as_posix(), corpus_files

    normalized: list[str] = []
    for value in sources:
        source = Path(value).expanduser().resolve()
        if not source.is_file():
            raise CorpusIndexError(f"corpus source file does not exist: {source}")
        normalized.append(source.as_posix())
    if not normalized:
        raise CorpusIndexError("at least one corpus source is required")
    return normalized, tuple(Path(value) for value in normalized)


def _looks_like_file_list(path: Path) -> bool:
    for encoding in ("utf-8-sig", "gb18030"):
        try:
            found = False
            with path.open("r", encoding=encoding) as stream:
                for line in stream:
                    entry = line.strip()
                    if not entry:
                        continue
                    found = True
                    if not Path(entry).expanduser().is_file():
                        return False
            return found
        except UnicodeDecodeError:
            continue
    return False


def _read_file_list(path: Path) -> tuple[Path, ...]:
    for encoding in ("utf-8-sig", "gb18030"):
        try:
            entries = tuple(
                Path(line.strip()).expanduser().resolve()
                for line in path.read_text(encoding=encoding).splitlines()
                if line.strip()
            )
            if entries and all(entry.is_file() for entry in entries):
                return entries
        except UnicodeDecodeError:
            continue
    raise CorpusIndexError(f"invalid BCC file list: {path}")


def _validate_bcc_source_encoding(path: Path) -> None:
    raw = path.read_bytes()
    if raw.startswith((b"\xef\xbb\xbf", b"\xff\xfe", b"\xfe\xff")):
        raise CorpusIndexError(
            f"BCC corpus must use GB18030 without a Unicode BOM: {path}"
        )
    try:
        decoded_utf8 = raw.decode("utf-8")
    except UnicodeDecodeError:
        decoded_utf8 = ""
    if decoded_utf8 and any(ord(character) > 127 for character in decoded_utf8):
        raise CorpusIndexError(
            f"BCC corpus is UTF-8; convert the final structured TXT to GB18030: {path}"
        )
    try:
        raw.decode("gb18030")
    except UnicodeDecodeError as exc:
        raise CorpusIndexError(
            f"BCC corpus is not valid GB18030: {path}"
        ) from exc


def _validate_query(query: str) -> str:
    if not isinstance(query, str) or not query.strip():
        raise CorpusQueryError("query must be a non-empty string")
    return query


def _normalize_scope_ranges(
    scope_ranges: Iterable[ScopeRange],
) -> tuple[ScopeRange, ...]:
    normalized: list[ScopeRange] = []
    for value in scope_ranges:
        if (
            not isinstance(value, (tuple, list))
            or len(value) != 2
            or not all(
                isinstance(number, int) and not isinstance(number, bool)
                for number in value
            )
        ):
            raise CorpusQueryError("each scope range must contain two integers")
        start, end = value
        if start < 0 or end < start:
            raise CorpusQueryError("scope range must satisfy 0 <= start <= end")
        normalized.append((start, end))
    if not normalized:
        raise CorpusQueryError("scope_ranges cannot be empty")
    if len(set(normalized)) != len(normalized):
        raise CorpusQueryError("scope_ranges cannot contain duplicates")
    return tuple(normalized)


def _load_scope_indexes(index_dir: Path) -> tuple[ScopeIndex, ...]:
    manifest_path = index_dir / "scope_indexes.json"
    if not manifest_path.exists():
        return ()
    try:
        document = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise CorpusIndexError(f"cannot read scope index manifest: {exc}") from exc
    if not isinstance(document, dict) or document.get("schema_version") != 1:
        raise CorpusIndexError("unsupported scope_indexes.json schema")
    raw_scopes = document.get("scopes")
    if not isinstance(raw_scopes, list) or not raw_scopes:
        raise CorpusIndexError("scope_indexes.json must contain scopes")

    org_ranges = {(scope.start, scope.end): scope for scope in read_org_scopes(index_dir)}
    loaded: list[ScopeIndex] = []
    seen: set[ScopeRange] = set()
    for raw in raw_scopes:
        if not isinstance(raw, dict):
            raise CorpusIndexError("invalid scope index manifest entry")
        start, end = raw.get("start"), raw.get("end")
        title, relative = raw.get("title"), raw.get("index_path")
        if (
            not isinstance(start, int)
            or isinstance(start, bool)
            or not isinstance(end, int)
            or isinstance(end, bool)
            or not isinstance(title, str)
            or not isinstance(relative, str)
        ):
            raise CorpusIndexError("invalid scope index manifest field types")
        pair = (start, end)
        if pair in seen or pair not in org_ranges:
            raise CorpusIndexError(f"scope index range is duplicate or unknown: {pair}")
        relative_path = Path(relative)
        if relative_path.is_absolute() or ".." in relative_path.parts:
            raise CorpusIndexError("scope index path must stay inside the main index")
        child = (index_dir / relative_path).resolve()
        try:
            child.relative_to(index_dir)
        except ValueError as exc:
            raise CorpusIndexError("scope index path escapes the main index") from exc
        _validate_index(child)
        seen.add(pair)
        loaded.append(ScopeIndex(start, end, title, child))
    if seen != set(org_ranges):
        raise CorpusIndexError("scope_indexes.json does not cover every Org.txt range")
    return tuple(loaded)


def _run_scope_worker(
    index_dir: Path,
    query: str,
    params: dict[str, object],
) -> JSONValue:
    marker = "__CORPUS_SCOPE_RESULT__="
    request = json.dumps(
        {"query": query, "command": params["Command"], "options": {
            key: value for key, value in params.items() if key != "Command"
        }},
        ensure_ascii=False,
    )
    creationflags = subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0
    try:
        completed = subprocess.run(
            [sys.executable, "-m", "corpus_search.scope_worker", str(index_dir)],
            input=request,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=60,
            check=False,
            creationflags=creationflags,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise CorpusQueryError(f"scope query worker failed: {exc}") from exc
    payload = next(
        (line[len(marker):] for line in reversed(completed.stdout.splitlines())
         if line.startswith(marker)),
        None,
    )
    if completed.returncode != 0 or payload is None:
        detail = completed.stderr.strip() or completed.stdout.strip()
        raise CorpusQueryError(
            f"scope query worker exited with code {completed.returncode}: {detail[-1000:]}"
        )
    try:
        envelope = json.loads(payload)
    except json.JSONDecodeError as exc:
        raise CorpusQueryError("scope query worker returned invalid JSON") from exc
    if not isinstance(envelope, dict) or not envelope.get("ok"):
        message = envelope.get("error", "unknown scope worker error") \
            if isinstance(envelope, dict) else "invalid scope worker response"
        raise CorpusQueryError(str(message))
    return envelope.get("result")


def _apply_scope_ranges(
    query: str,
    scope_ranges: Iterable[ScopeRange] | None,
) -> str:
    if scope_ranges is None:
        return query
    if "\n" in query or " AND " in query or " NOT " in query:
        raise CorpusQueryError(
            "scope_ranges currently supports one BCC expression; "
            "use call_bcc() for an advanced compound scoped query"
        )
    if "[" in query or "]" in query:
        raise CorpusQueryError("query already contains a BCC range expression")
    if "{" in query and not query.endswith("}"):
        raise CorpusQueryError(
            "scoped query conditions must end with the closing } character"
        )
    normalized = _normalize_scope_ranges(scope_ranges)
    condition = query if "{" in query else query + "{}"
    ranges = ";".join(f"({start},{end})" for start, end in normalized)
    return f"{condition}[{ranges}]"


def _query_options(command: str, options: dict[str, object]) -> dict[str, object]:
    if not isinstance(command, str) or not command.strip():
        raise CorpusQueryError("command must be a non-empty string")
    if "Command" in options or "Output" in options:
        raise CorpusQueryError(
            "pass the query mode with command=, not Command= or Output="
        )
    return {"Command": command, **options}


def _add_native_option(
    options: dict[str, object],
    native_name: str,
    value: object,
    *,
    minimum: int | None = None,
) -> None:
    if value is None:
        return
    if native_name in options:
        raise CorpusQueryError(
            f"pass {native_name} with its named adapter parameter, not both forms"
        )
    if minimum is not None:
        if not isinstance(value, int) or isinstance(value, bool) or value < minimum:
            raise CorpusQueryError(
                f"{native_name} must be an integer greater than or equal to {minimum}"
            )
    if native_name == "Target" and (
        not isinstance(value, str) or not value.strip()
    ):
        raise CorpusQueryError("Target must be a non-empty string")
    options[native_name] = value


def _validate_kv_text(name: str, value: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise CorpusQueryError(f"{name} must be a non-empty string")
    return value


def _decode_native_bytes(value: object) -> object:
    if isinstance(value, bytes):
        try:
            return value.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise CorpusQueryError("LangSC returned non-UTF-8 bytes") from exc
    return value
