import pytest

from hris_sync.errors import ConflictError, PermanentError
from hris_sync.mocks.hris import MockHrisClient
from hris_sync.models import HrisWorker


@pytest.fixture
def hris(session, valid_record):
    session.add(HrisWorker.from_record(valid_record))  # E1042, email null
    session.commit()
    return MockHrisClient(session)


def test_get_worker(hris):
    assert hris.get_worker("E1042")["lastName"] == "Patel"


def test_get_unknown_worker_returns_none(hris):
    assert hris.get_worker("E9999") is None


def test_update_email_writes_it(hris):
    assert hris.update_email("E1042", "asha.patel2@contoso.com") == "Updated"
    assert hris.get_worker("E1042")["email"] == "asha.patel2@contoso.com"


def test_same_email_again_is_no_change(hris):
    hris.update_email("E1042", "asha.patel2@contoso.com")
    assert hris.update_email("E1042", "Asha.Patel2@contoso.com") == "NoChange"


def test_different_existing_email_is_a_conflict_and_is_not_overwritten(hris):
    hris.update_email("E1042", "asha.patel2@contoso.com")
    with pytest.raises(ConflictError):
        hris.update_email("E1042", "a.patel@contoso.com")
    assert hris.get_worker("E1042")["email"] == "asha.patel2@contoso.com"


def test_unknown_employee_is_rejected(hris):
    with pytest.raises(PermanentError) as exc:
        hris.update_email("E9999", "x@contoso.com")
    assert exc.value.status == 404


@pytest.mark.parametrize("email", ["", "not-an-email", "a b@contoso.com", "a@contoso", None])
def test_invalid_email_is_rejected(hris, email):
    with pytest.raises(PermanentError, match="not a valid email"):
        hris.update_email("E1042", email)


def test_two_workers_with_one_employee_id_are_never_guessed_between(session, hris, valid_record):
    session.add(HrisWorker.from_record({**valid_record, "department": "Finance"}))
    session.commit()
    with pytest.raises(PermanentError, match="more than one"):
        hris.update_email("E1042", "asha.patel2@contoso.com")
