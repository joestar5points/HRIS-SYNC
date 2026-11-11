"""Sanity checks that the fixtures say what their comments claim."""

from pathlib import Path

from sqlalchemy import func, select

from hris_sync.loader import read_input
from hris_sync.mocks.entra import MockEntraClient
from hris_sync.mocks.fixtures import ENTRA_USERS, EXPECTED, MAILBOX_SETTINGS, seed
from hris_sync.mocks.models import EntraUser, ExoMailbox
from hris_sync.validation import validate

SAMPLE_INPUT = Path(__file__).parent / "data" / "all-cases.json"


def test_every_input_record_has_an_expected_outcome():
    ids = {record["employeeId"] for record in read_input(SAMPLE_INPUT)}
    assert ids == set(EXPECTED)


def test_invalid_records_fail_validation_and_valid_ones_pass():
    for record in read_input(SAMPLE_INPUT):
        reason = EXPECTED[record["employeeId"]][1]
        should_be_invalid = record["employeeId"] in (None, "E7002", "E7003", "E7004")
        assert bool(validate(record)) == should_be_invalid, reason


def test_seed_creates_every_entra_user_with_managers_resolved(session):
    seed(session)
    assert session.scalar(select(func.count()).select_from(EntraUser)) == len(ENTRA_USERS)

    entra = MockEntraClient(session)
    morgan = entra.find_by_employee_id("E1008")[0]
    sam = entra.find_by_employee_id("E1000")[0]
    assert morgan["manager"] == sam["id"]


def test_seed_includes_the_ambiguous_pair_and_the_name_clash(session):
    seed(session)
    entra = MockEntraClient(session)
    assert len(entra.find_by_employee_id("E4001")) == 2
    assert entra.upn_exists("asha.patel@contoso.com")  # held by E5001, not joiner E1042


def test_seed_gives_licensed_users_a_mailbox(session):
    seed(session)
    assert session.scalar(select(func.count()).select_from(ExoMailbox)) == 2  # E1051, E9001


def test_mailbox_settings_apply_to_joiners_created_during_the_run(session):
    seed(session)
    entra = MockEntraClient(session, mailbox_settings=MAILBOX_SETTINGS)
    user = entra.create_user(
        {
            "accountEnabled": True,
            "displayName": "Ben Carter",
            "mailNickname": "ben.carter",
            "userPrincipalName": "ben.carter@contoso.com",
            "employeeId": "E1043",
            "usageLocation": "GB",
        }
    )
    entra.assign_license(user["id"])

    mailbox = session.get(ExoMailbox, user["id"])
    assert mailbox.checks_until_ready == 0
    assert mailbox.primary_smtp_override == "b.carter@contoso.com"
