"""A collection of common errors used by real APIS that can mock real Exceptions and Errors. All extend from Exception as top level super class"""

from typing import Optional

TRANSIENT_STATUSES = frozenset({408, 429, 502, 503, 504})


class SyncError(Exception):
    """Base class for every error a client may raise."""

    default_message = "dependency error"

    def __init__(self, status: Optional[int] = None, message: Optional[str] = None):
        self.status = status
        self.message = message or self.default_message
        super().__init__(self.message)

    def __str__(self) -> str:
        return f"{self.message} (HTTP {self.status})" if self.status else self.message


class TransientError(SyncError):
    """A temporary failure. The caller may retry, honouring retry_after if set."""

    default_message = "transient dependency failure"

    def __init__(
        self,
        status: Optional[int] = 503,
        retry_after: Optional[float] = None,
        message: Optional[str] = None,
    ):
        if retry_after is not None and retry_after < 0:
            raise ValueError("retry_after cannot be negative")
        self.retry_after = retry_after
        super().__init__(status, message)


class PermanentError(SyncError):
    """A failure that retrying will not fix (bad data, not found, server rejected it)."""

    default_message = "permanent dependency failure"

    def __init__(self, status: Optional[int] = 400, message: Optional[str] = None):
        super().__init__(status, message)


class AuthError(PermanentError):
    """Missing, expired or under-scoped token. Never retried: it needs a config fix."""

    default_message = "not authorised"

    def __init__(self, status: Optional[int] = 401, message: Optional[str] = None):
        super().__init__(status, message)


class ConflictError(PermanentError):
    """The target already holds a different value. Routed to ManualReview, never overwritten."""

    default_message = "conflicting value already present"

    def __init__(self, status: Optional[int] = 409, message: Optional[str] = None):
        super().__init__(status, message)


def error_for_status(
    status: int, message: Optional[str] = None, retry_after: Optional[float] = None
) -> SyncError:
    """Map an HTTP status to the matching error. Used by the fault injector and real clients."""
    if status in TRANSIENT_STATUSES:
        return TransientError(status, retry_after=retry_after, message=message)
    if status in (401, 403):
        return AuthError(status, message)
    if status == 409:
        return ConflictError(status, message)
    return PermanentError(status, message)


class LoadError(Exception):
    """The input file can't be read as a list of records. Nothing was loaded."""
