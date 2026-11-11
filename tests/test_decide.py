from datetime import date
from pathlib import Path

import pytest

from hris_sync.loader import read_input
from hris_sync.main import decide
from hris_sync.mocks.entra import MockEntraClient
from hris_sync.mocks.fixtures import EXPECTED, FIXTURE_TODAY, seed
from hris_sync.state import Outcome, SyncState

TODAY = date(2026, 11, 6)
MANAGER = "manager-object-id"
SAMPLE_INPUT = Path(__file__).parent / "data" / "all-cases.json"


def record(**overrides):
    base = {
        "employeeId": "E1042",
        "status": "Active",
        "firstName": "Asha",
        "lastName": "Patel",
        "department": "Quality",
        "managerEmployeeId": "E1008",
        "startDate": "2026-10-30",
        "endDate": None,
        "email": None,
    }
    base.update(overrides)
    return base


def entra_user(**overrides):
    base = {
        "id": "user-object-id",
        "employeeId": "E1042",
        "department": "Quality",
        "manager": MANAGER,
        "accountEnabled": True,
        "isPrivileged": False,
    }
    base.update(overrides)
    return base


def outcome(rec, matches, manager=MANAGER, created_by_sync=False):
    return decide(rec, matches, TODAY, manager, created_by_sync)


# --- 1-2: unsafe input ------------------------------------------------------------


def test_invalid_record_goes_to_manual_review_with_the_errors_as_reason():
    result, reason, changes = outcome(record(employeeId=None, startDate="bad"), [])
    assert result is Outcome.MANUAL_REVIEW
    assert "employeeId is required" in reason
    assert changes == {}


def test_two_matching_identities_go_to_manual_review():
    result, reason, _ = outcome(record(), [entra_user(), entra_user(id="other")])
    assert result is Outcome.MANUAL_REVIEW
    assert "2 Entra identities" in reason


# --- 3-5: no account yet ----------------------------------------------------------


def test_new_joiner_is_created_enabled():
    assert outcome(record(), []) == (Outcome.CREATE, "new joiner", {"accountEnabled": True})


def test_joiner_starting_today_is_created_enabled():
    result, _, changes = outcome(record(startDate="2026-11-06"), [])
    assert result is Outcome.CREATE and changes == {"accountEnabled": True}


def test_future_joiner_is_created_disabled():
    result, reason, changes = outcome(record(startDate="2026-11-28"), [])
    assert result is Outcome.CREATE
    assert changes == {"accountEnabled": False}
    assert "staged" in reason


def test_joiner_whose_manager_is_not_in_entra_is_still_created_and_reason_says_so():
    result, reason, _ = outcome(record(managerEmployeeId="E9999"), [], manager=None)
    assert result is Outcome.CREATE
    assert "manager E9999 not found" in reason


def test_leaver_with_no_account_is_no_change():
    assert outcome(record(status="Leaver"), [])[0] is Outcome.NO_CHANGE


# --- 6-8: leavers -----------------------------------------------------------------


@pytest.mark.parametrize(
    "overrides",
    [
        {"status": "Leaver"},
        {"status": "Terminated"},
        {"status": "Active", "endDate": "2026-10-29"},  # end date passed
        {"status": "Active", "endDate": "2026-11-06"},  # ends today
    ],
)
def test_leaver_is_disabled(overrides):
    result, _, changes = outcome(record(startDate="2020-01-06", **overrides), [entra_user()])
    assert result is Outcome.DISABLE
    assert changes == {"accountEnabled": False}


def test_future_end_date_is_not_a_leaver_yet():
    assert outcome(record(endDate="2026-12-28"), [entra_user()])[0] is Outcome.NO_CHANGE


def test_leaver_already_disabled_is_no_change():
    assert outcome(record(status="Leaver"), [entra_user(accountEnabled=False)])[0] is Outcome.NO_CHANGE


def test_privileged_leaver_is_never_auto_disabled():
    result, reason, _ = outcome(record(status="Leaver"), [entra_user(isPrivileged=True)])
    assert result is Outcome.MANUAL_REVIEW
    assert "privileged" in reason


# --- 9-11: active worker with an account ------------------------------------------


def test_mover_department_change_updates_only_department():
    result, _, changes = outcome(record(department="Finance"), [entra_user()])
    assert result is Outcome.UPDATE
    assert changes == {"department": "Finance"}


def test_mover_manager_change_updates_only_manager():
    result, _, changes = outcome(record(), [entra_user()], manager="new-manager-id")
    assert result is Outcome.UPDATE
    assert changes == {"manager": "new-manager-id"}


def test_unknown_manager_never_blanks_out_the_existing_one():
    assert outcome(record(), [entra_user()], manager=None)[0] is Outcome.NO_CHANGE


def test_matching_account_is_no_change():
    assert outcome(record(), [entra_user()])[0] is Outcome.NO_CHANGE


def test_staged_joiner_is_enabled_once_start_date_arrives():
    result, _, changes = outcome(record(), [entra_user(accountEnabled=False)], created_by_sync=True)
    assert result is Outcome.UPDATE
    assert changes == {"accountEnabled": True}


def test_staged_joiner_stays_disabled_before_start_date():
    rec = record(startDate="2026-11-28")
    assert outcome(rec, [entra_user(accountEnabled=False)], created_by_sync=True)[0] is Outcome.NO_CHANGE


def test_account_disabled_by_someone_else_is_not_re_enabled():
    result, reason, _ = outcome(record(), [entra_user(accountEnabled=False)])
    assert result is Outcome.MANUAL_REVIEW
    assert "outside this service" in reason


# --- The whole sample file --------------------------------------------------------

# Handled by the caller rather than decide(): E8001 appears twice in one file.
CALLER_HANDLED = {"E8001"}


def test_every_sample_record_gets_its_expected_outcome(session):
    seed(session)
    entra = MockEntraClient(session)

    for rec in read_input(SAMPLE_INPUT):
        employee_id = rec["employeeId"]
        if employee_id in CALLER_HANDLED:
            continue

        matches = entra.find_by_employee_id(employee_id) if employee_id else []
        managers = entra.find_by_employee_id(rec.get("managerEmployeeId") or "")
        manager_id = managers[0]["id"] if len(managers) == 1 else None
        created_by_sync = employee_id is not None and session.get(SyncState, employee_id) is not None

        result, reason, _ = decide(rec, matches, FIXTURE_TODAY, manager_id, created_by_sync)

        expected, why = EXPECTED[employee_id]
        assert result == expected, f"{employee_id}: got {result} ({reason}), expected {expected} ({why})"
