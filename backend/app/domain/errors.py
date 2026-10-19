class PersistenceError(Exception):
    """Public diagnostic contains no SQL, parameters or connection strings."""

    def __init__(
        self, message: str = "persistence operation rejected", sqlstate: str | None = None
    ):
        super().__init__(message)
        self.sqlstate = sqlstate


class ScopeViolation(PersistenceError):
    pass


class DuplicateConflict(PersistenceError):
    pass


class InvalidTransition(PersistenceError):
    pass


class StaleWrite(PersistenceError):
    pass


class NotPublished(PersistenceError):
    pass


class DatabaseUnavailable(PersistenceError):
    pass


class StaleAttempt(PersistenceError):
    pass


class CapacityExceeded(PersistenceError):
    pass


class FinalizationOutcomeUnknown(PersistenceError):
    pass


class UploadRejected(PersistenceError):
    def __init__(self, code: str):
        self.code = code
        super().__init__(code)


class RetryRejected(UploadRejected):
    pass


class ProviderCapacityUnsupported(PersistenceError):
    pass
