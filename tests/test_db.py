import pytest
from sqlalchemy import inspect, select
from sqlalchemy.exc import IntegrityError, StatementError

from hris_sync.mocks.models import EntraUser
from hris_sync.models import HrisWorker
from hris_sync.state import Stage, SyncState

EXPECTED_TABLES = {
    "hris_employees",
    "entra_users",
    "exo_mailboxes",
    "runs",
    "sync_state",
    "actions",
    "audit_log",
    "processed_inputs",
}


def test_init_db_creates_all_tables(engine):
    assert EXPECTED_TABLES <= set(inspect(engine).get_table_names())


def test_hris_table_accepts_invalid_records_as_is(session):
    """Invalid records must load so they can be reported as ManualReview, not crash the load."""
    bad = {"employeeId": None, "status": "Nonsense", "startDate": "2026-13-45"}
    session.add(HrisWorker.from_record(bad))
    session.commit()

    stored = session.scalars(select(HrisWorker)).one()
    assert stored.employee_id is None
    assert stored.start_date == "2026-13-45"
    assert stored.to_record() == bad


def test_entra_mock_can_hold_two_users_with_one_employee_id(session):
    """Needed to seed the ambiguous-match scenario."""
    for upn in ("a.one@example.test", "a.two@example.test"):
        session.add(
            EntraUser(employee_id="E2001", user_principal_name=upn, mail_nickname="a", display_name="A")
        )
    session.commit()

    matches = session.scalars(select(EntraUser).where(EntraUser.employee_id == "E2001")).all()
    assert len(matches) == 2
    assert matches[0].object_id != matches[1].object_id


def test_sync_state_rejects_unknown_stage(session):
    session.add(SyncState(employee_id="E1042", stage="NotAStage"))
    with pytest.raises((IntegrityError, StatementError, LookupError)):
        session.commit()


def test_sync_state_round_trips_stage(session):
    session.add(SyncState(employee_id="E1042", stage=Stage.MAILBOX_PENDING))
    session.commit()
    assert session.get(SyncState, "E1042").stage is Stage.MAILBOX_PENDING
