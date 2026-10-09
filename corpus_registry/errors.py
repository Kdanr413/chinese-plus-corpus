class CorpusRegistryError(Exception):
    """Base Registry failure."""


class DatabaseConfigurationError(CorpusRegistryError):
    """Database connection configuration is absent or unsafe."""


class DatabaseDependencyError(CorpusRegistryError):
    """The PostgreSQL driver is unavailable."""


class RegistryConflictError(CorpusRegistryError):
    """A stable corpus or version identity already exists."""


class RegistryNotFoundError(CorpusRegistryError):
    """A requested Registry record does not exist."""


class RegisteredFileIntegrityError(CorpusRegistryError):
    """Registered release files no longer match disk."""
