"""Typed errors for the LangSC-backed corpus search adapter."""


class CorpusSearchError(RuntimeError):
    """Base error raised by the corpus search adapter."""


class CorpusSearchDependencyError(CorpusSearchError):
    """Raised when LangSC or one of its native dependencies cannot be loaded."""


class CorpusIndexError(CorpusSearchError):
    """Raised when an index cannot be built or an index directory is invalid."""


class CorpusIndexExistsError(CorpusIndexError):
    """Raised when a build would write into a non-empty index directory."""


class CorpusQueryError(CorpusSearchError):
    """Raised when the native engine returns an invalid query result."""


class CorpusSelectionError(CorpusSearchError):
    """Raised when a structured-corpus selection cannot be created."""


class CorpusSelectionExistsError(CorpusSelectionError):
    """Raised when a selection would overwrite an existing derived asset."""
