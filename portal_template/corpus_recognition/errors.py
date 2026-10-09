"""Errors raised by the recognition and validation domain layer."""


class RecognitionError(Exception):
    """Base class for recognition-domain errors."""


class RecognitionConfigurationError(RecognitionError):
    """Raised when recognition configuration is invalid or incomplete."""


class PromptConfigurationError(RecognitionConfigurationError):
    """Raised when a versioned prompt cannot be validated."""


class RecognitionNotFoundError(RecognitionError):
    """Raised when a requested recognition record does not exist."""


class RecognitionWorkflowCancelled(RecognitionError):
    """Raised at a safe checkpoint when the operator ends a workflow."""


class ValidatorUnavailableError(RecognitionError):
    """Raised when a configured validator cannot perform a request."""


class ValidatorResponseError(RecognitionError):
    """Raised when a validator response violates the shared contract."""
