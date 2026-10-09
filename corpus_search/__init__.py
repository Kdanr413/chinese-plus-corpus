"""Corpus Search Engine backed by the existing LangSC.BCC package."""

from .engine import (
    CorpusSearchEngine,
    IndexBuildResult,
    ScopeIndexBuildResult,
    JSONValue,
    OrgScope,
    ScopeRange,
    build_index,
    build_index_isolated,
    build_scope_indexes,
    load_corpus,
    read_org_scopes,
)
from .errors import (
    CorpusIndexError,
    CorpusIndexExistsError,
    CorpusQueryError,
    CorpusSearchDependencyError,
    CorpusSearchError,
    CorpusSelectionError,
    CorpusSelectionExistsError,
)
from .selection import CorpusSelectionResult, create_corpus_selection

__all__ = [
    "CorpusIndexError",
    "CorpusIndexExistsError",
    "CorpusQueryError",
    "CorpusSearchDependencyError",
    "CorpusSearchEngine",
    "CorpusSearchError",
    "CorpusSelectionError",
    "CorpusSelectionExistsError",
    "CorpusSelectionResult",
    "IndexBuildResult",
    "ScopeIndexBuildResult",
    "JSONValue",
    "OrgScope",
    "ScopeRange",
    "build_index",
    "build_index_isolated",
    "build_scope_indexes",
    "create_corpus_selection",
    "load_corpus",
    "read_org_scopes",
]
