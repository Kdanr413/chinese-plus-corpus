"""Typed failures for the Workspace and Corpus Release lifecycle."""


class CorpusReleaseError(Exception):
    """Base class for release-management failures."""


class InvalidReleaseIdentityError(CorpusReleaseError):
    """A corpus, workspace, or version identifier is unsafe."""


class WorkspaceError(CorpusReleaseError):
    """A workspace is missing, invalid, or already exists."""


class ReleaseExistsError(CorpusReleaseError):
    """A release would overwrite an existing path."""


class ReleaseNotFoundError(CorpusReleaseError):
    """A requested release does not exist."""


class ReleaseIntegrityError(CorpusReleaseError):
    """A release failed its manifest or checksum validation."""
