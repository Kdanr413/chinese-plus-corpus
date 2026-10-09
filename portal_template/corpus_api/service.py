"""Service layer joining the Registry, Corpus Releases and LangSC.BCC."""

from __future__ import annotations

import uuid
import json
from collections import OrderedDict
from collections.abc import Callable
from pathlib import Path
from threading import RLock
from time import perf_counter
from typing import Any

from corpus_registry import (
    CorpusRegistry,
    CorpusRegistryError,
    RegisteredFileIntegrityError,
)
from corpus_search import (
    CorpusIndexError,
    CorpusQueryError,
    CorpusSearchDependencyError,
    CorpusSearchEngine,
    JSONValue,
    load_corpus,
)


class CorpusServiceUnavailableError(RuntimeError):
    """The registry could not be reached or read safely."""


class SearchTargetError(RuntimeError):
    """No unique searchable Corpus Version can be selected."""


class SearchAssetUnavailableError(RuntimeError):
    """A registered Corpus Release or BCC index cannot be read safely."""


class SearchRequestError(RuntimeError):
    """The supplied BCC query or query option is invalid."""


EngineLoader = Callable[[str | Path], CorpusSearchEngine]


class SearchEngineCache:
    """Small process-local cache of already loaded, immutable BCC indexes."""

    def __init__(
        self,
        *,
        loader: EngineLoader = load_corpus,
        max_entries: int = 4,
    ) -> None:
        if max_entries < 1:
            raise ValueError("max_entries must be at least 1")
        self._loader = loader
        self._max_entries = max_entries
        self._engines: OrderedDict[str, CorpusSearchEngine] = OrderedDict()
        self._lock = RLock()

    def get(
        self,
        key: str,
        index_dir: Path,
        *,
        before_load: Callable[[], None] | None = None,
    ) -> CorpusSearchEngine:
        with self._lock:
            engine = self._engines.get(key)
            if engine is not None:
                self._engines.move_to_end(key)
                return engine
            if before_load is not None:
                before_load()
            engine = self._loader(index_dir)
            self._engines[key] = engine
            self._engines.move_to_end(key)
            while len(self._engines) > self._max_entries:
                self._engines.popitem(last=False)
            return engine


class CorpusService:
    def __init__(
        self,
        registry: CorpusRegistry,
        search_cache: SearchEngineCache | None = None,
    ) -> None:
        self.registry = registry
        self.search_cache = search_cache or SearchEngineCache()
        self._context_metadata_cache: dict[str, dict[str, Any]] = {}
        self._context_metadata_lock = RLock()

    def list_corpora(self) -> list[dict[str, object]]:
        try:
            return self.registry.list_corpora()
        except CorpusRegistryError:
            raise
        except Exception as exc:
            raise CorpusServiceUnavailableError(
                "Corpus Registry is temporarily unavailable"
            ) from exc

    def list_versions(self, corpus_id: uuid.UUID) -> list[dict[str, object]]:
        try:
            return self.registry.list_versions(corpus_id)
        except CorpusRegistryError:
            raise
        except Exception as exc:
            raise CorpusServiceUnavailableError(
                "Corpus Registry is temporarily unavailable"
            ) from exc

    def search(
        self,
        query: str,
        *,
        corpus_key: str | None = None,
        version: str | None = None,
        limit: int = 100,
    ) -> dict[str, object]:
        return self._execute(
            query,
            command="Freq",
            corpus_key=corpus_key,
            version=version,
            options={"number": limit},
        )

    def kwic(
        self,
        query: str,
        *,
        corpus_key: str | None = None,
        version: str | None = None,
        limit: int = 20,
        page: int = 0,
        window: int = 50,
    ) -> dict[str, object]:
        return self._execute(
            query,
            command="Context",
            corpus_key=corpus_key,
            version=version,
            options={"number": limit, "page_no": page, "win_size": window},
        )

    def _execute(
        self,
        query: str,
        *,
        command: str,
        corpus_key: str | None,
        version: str | None,
        options: dict[str, object],
    ) -> dict[str, object]:
        try:
            corpus, version_row = self._resolve_target(corpus_key, version)
            engine = self._engine(corpus, version_row)
            started = perf_counter()
            if command == "Context":
                result: JSONValue = engine.kwic(query, **options)
                release_root = self.registry.resolve_release_path(
                    str(corpus["corpus_key"]), str(version_row["version"])
                )
                self._enrich_context_metadata(
                    result,
                    release_root,
                    cache_key=f"{version_row['id']}:{version_row['checksum_sha256']}",
                )
            else:
                result = engine.frequency(query, **options)
            duration_ms = max(0, round((perf_counter() - started) * 1000))
            return {
                "corpus_id": corpus["id"],
                "corpus_key": corpus["corpus_key"],
                "corpus_name": corpus["name"],
                "version_id": version_row["id"],
                "version": version_row["version"],
                "query": query,
                "command": command,
                "duration_ms": duration_ms,
                "result": result,
            }
        except RegisteredFileIntegrityError as exc:
            raise SearchAssetUnavailableError(
                "The selected Corpus Release or BCC index is unavailable"
            ) from exc
        except (CorpusRegistryError, SearchTargetError):
            raise
        except CorpusQueryError as exc:
            raise SearchRequestError(str(exc)) from exc
        except (
            CorpusIndexError,
            CorpusSearchDependencyError,
            RegisteredFileIntegrityError,
        ) as exc:
            raise SearchAssetUnavailableError(
                "The selected Corpus Release or BCC index is unavailable"
            ) from exc
        except (SearchRequestError, SearchAssetUnavailableError):
            raise
        except Exception as exc:
            raise CorpusServiceUnavailableError(
                "Corpus search is temporarily unavailable"
            ) from exc

    def _enrich_context_metadata(
        self, result: JSONValue, release_root: Path, *, cache_key: str
    ) -> None:
        """Attach the original #Global and #Group records to BCC KWIC rows.

        LangSC's Context response exposes the document name and #Group values in
        ``Source`` but omits #Global.  The immutable Release remains authoritative;
        this method only reads its GB18030 corpus files and enriches the API result.
        """
        if not isinstance(result, dict) or not isinstance(result.get("Context"), list):
            return
        metadata = self._release_context_metadata(release_root, cache_key=cache_key)
        for row in result["Context"]:
            if not isinstance(row, dict):
                continue
            source = str(row.get("Source") or "")
            values = metadata.get(source)
            if values is None:
                native_document_name, separator, group = source.partition(";")
                document_name = native_document_name
                while document_name and f"@DOC@{document_name}" not in metadata:
                    document_name = document_name.rsplit("_", 1)[0] if "_" in document_name else ""
                if document_name:
                    values = metadata.get(
                        document_name + (separator + group if separator else "")
                    )
                    if values is None and group:
                        wanted = set(group.split(";"))
                        candidates = metadata.get(f"@ITEMS@{document_name}", [])
                        if candidates:
                            values = max(
                                candidates,
                                key=lambda item: len(wanted & set(item["Group"].split(";"))),
                            )
                    values = values or metadata.get(f"@DOC@{document_name}")
            if values:
                row["Global"] = values["Global"]
                row["Group"] = values["Group"]
                row["SourceTitle"] = values.get("SourceTitle", "")

    def _release_context_metadata(
        self, release_root: Path, *, cache_key: str
    ) -> dict[str, Any]:
        with self._context_metadata_lock:
            cached = self._context_metadata_cache.get(cache_key)
            if cached is not None:
                return cached
            lookup: dict[str, Any] = {}
            corpus_root = release_root / "corpus"
            titles = {}
            selection = release_root / "metadata" / "selection.json"
            if selection.is_file():
                try:
                    entries = json.loads(selection.read_text(encoding="utf-8")).get("corpus_files", [])
                    titles = {entry.get("bcc_document_id"): entry.get("document_name", "") for entry in entries}
                except (ValueError, OSError):
                    pass  # Older releases can still be queried without a source catalogue.
            for path in sorted(corpus_root.glob("*.txt")):
                document_name = ""
                global_value = ""
                group_value = ""
                with path.open("r", encoding="gb18030", errors="replace") as handle:
                    for raw_line in handle:
                        line = raw_line.rstrip("\r\n")
                        if line.startswith("Doc "):
                            document_name = line[4:].strip()
                        elif line == "#Global":
                            global_value = ""
                        elif line.startswith("#Global "):
                            global_value = line[8:]
                        elif line == "#Group":
                            group_value = ""
                        elif line.startswith("#Group "):
                            group_value = line[7:]
                        elif line.startswith("Item:") and document_name:
                            source = document_name + (f";{group_value}" if group_value else "")
                            values = {"Global": global_value, "Group": group_value,
                                      "SourceTitle": titles.get(document_name, "")}
                            lookup.setdefault(source, values)
                            lookup.setdefault(f"@DOC@{document_name}", values)
                            lookup.setdefault(f"@ITEMS@{document_name}", []).append(values)
            self._context_metadata_cache[cache_key] = lookup
            return lookup

    def _resolve_target(
        self, corpus_key: str | None, version: str | None
    ) -> tuple[dict[str, Any], dict[str, Any]]:
        if corpus_key:
            corpus = self.registry.get_corpus(corpus_key)
        else:
            active = [
                item for item in self.registry.list_corpora()
                if item["status"] == "active"
            ]
            if len(active) != 1:
                raise SearchTargetError(
                    "corpus_key is required unless exactly one active corpus exists"
                )
            corpus = active[0]
        if version is None and corpus["default_version_id"] is None:
            raise SearchTargetError(
                f"no default searchable version is configured for {corpus['corpus_key']}"
            )
        version_row = self.registry.get_version(str(corpus["corpus_key"]), version)
        if not version_row["searchable"] or version_row["status"] not in {
            "ready",
            "published",
        }:
            raise SearchTargetError(
                f"Corpus Version is not searchable: {corpus['corpus_key']}/{version_row['version']}"
            )
        return corpus, version_row

    def _engine(
        self, corpus: dict[str, Any], version_row: dict[str, Any]
    ) -> CorpusSearchEngine:
        key = f"{version_row['id']}:{version_row['checksum_sha256']}"
        release_root = self.registry.resolve_release_path(
            str(corpus["corpus_key"]), str(version_row["version"])
        )

        def validate_release() -> None:
            integrity = self.registry.verify_registered_files(
                str(corpus["corpus_key"]), str(version_row["version"])
            )
            if not integrity["valid"]:
                raise RegisteredFileIntegrityError(
                    "registered Corpus Release files failed integrity verification"
                )
            files = self.registry.list_files(
                str(corpus["corpus_key"]), str(version_row["version"])
            )
            corpus_files = [item for item in files if item["role"] == "corpus"]
            if not corpus_files or any(
                str(item.get("encoding") or "").lower() not in {"gb18030", "gbk"}
                for item in corpus_files
            ):
                raise RegisteredFileIntegrityError(
                    "searchable corpus files must be registered as GB18030/GBK"
                )

        return self.search_cache.get(
            key,
            release_root / "index",
            before_load=validate_release,
        )
