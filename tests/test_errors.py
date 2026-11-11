import pytest

from hris_sync.errors import (
    AuthError,
    ConflictError,
    PermanentError,
    SyncError,
    TransientError,
    error_for_status,
)


def test_transient_error_keeps_status_and_retry_after():
    err = TransientError(429, retry_after=2.5)
    assert err.status == 429
    assert err.retry_after == 2.5


def test_transient_error_defaults_to_503_without_retry_after():
    err = TransientError()
    assert err.status == 503
    assert err.retry_after is None


def test_negative_retry_after_is_rejected():
    with pytest.raises(ValueError):
        TransientError(429, retry_after=-1)


def test_auth_and_conflict_are_permanent_so_never_retried():
    assert issubclass(AuthError, PermanentError)
    assert issubclass(ConflictError, PermanentError)
    assert not issubclass(PermanentError, TransientError)


def test_every_error_shares_one_base_class():
    for cls in (TransientError, PermanentError, AuthError, ConflictError):
        assert issubclass(cls, SyncError)


def test_default_statuses():
    assert PermanentError().status == 400
    assert AuthError().status == 401
    assert ConflictError().status == 409


def test_message_includes_status():
    assert str(ConflictError(message="HRIS holds a different email")) == (
        "HRIS holds a different email (HTTP 409)"
    )


def test_message_without_status():
    assert str(PermanentError(status=None, message="unknown employeeId")) == "unknown employeeId"


@pytest.mark.parametrize(
    "status, expected",
    [
        (408, TransientError),
        (429, TransientError),
        (502, TransientError),
        (503, TransientError),
        (504, TransientError),
        (400, PermanentError),
        (404, PermanentError),
        (500, PermanentError),
        (401, AuthError),
        (403, AuthError),
        (409, ConflictError),
    ],
)
def test_error_for_status_maps_to_the_right_class(status, expected):
    err = error_for_status(status)
    assert type(err) is expected
    assert err.status == status


def test_error_for_status_passes_retry_after_to_transient_errors():
    err = error_for_status(429, retry_after=3)
    assert isinstance(err, TransientError)
    assert err.retry_after == 3
