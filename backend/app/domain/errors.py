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
