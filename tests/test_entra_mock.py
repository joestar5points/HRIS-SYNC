import pytest

from hris_sync.errors import ConflictError, PermanentError
from hris_sync.mocks.entra import MockEntraClient
from hris_sync.mocks.models import ExoMailbox


@pytest.fixture
def entra(session):
    return MockEntraClient(session)


def body(**overrides):
    user = {
        "accountEnabled": True,
        "displayName": "Asha Patel",
        "givenName": "Asha",
        "surname": "Patel",
        "mailNickname": "asha.patel",
        "userPrincipalName": "asha.patel@contoso.com",
        "employeeId": "E1042",
        "department": "Quality",
        "usageLocation": "GB",
    }
    user.update(overrides)
    return user


# --- create_user -----------------------------------------------------------------


def test_create_user_returns_graph_shaped_user_with_new_id(entra):
    user = entra.create_user(body())

    assert user["id"]
    assert user["employeeId"] == "E1042"
    assert user["userPrincipalName"] == "asha.patel@contoso.com"
    assert user["accountEnabled"] is True
    assert user["manager"] is None


def test_created_user_can_be_found_by_employee_id(entra):
    created = entra.create_user(body())
    assert entra.find_by_employee_id("E1042") == [created]


def test_staged_joiner_can_be_created_disabled(entra):
    assert entra.create_user(body(accountEnabled=False))["accountEnabled"] is False


@pytest.mark.parametrize("field", ["accountEnabled", "displayName", "mailNickname", "userPrincipalName"])
def test_create_user_requires_graph_mandatory_fields(entra, field):
    with pytest.raises(PermanentError, match=field):
        entra.create_user(body(**{field: None}))


def test_duplicate_upn_is_rejected_case_insensitively(entra):
    entra.create_user(body())
    with pytest.raises(ConflictError, match="already exists"):
        entra.create_user(body(employeeId="E5001", userPrincipalName="Asha.Patel@Contoso.com"))


def test_unverified_upn_domain_is_rejected(entra):
    with pytest.raises(PermanentError, match="not verified"):
        entra.create_user(body(userPrincipalName="asha.patel@gmail.com"))


def test_upn_without_domain_is_rejected(entra):
    with pytest.raises(PermanentError, match="not verified"):
        entra.create_user(body(userPrincipalName="asha.patel"))


def test_employee_id_over_16_characters_is_rejected(entra):
    with pytest.raises(PermanentError, match="employeeId"):
        entra.create_user(body(employeeId="E" * 17))


# --- reads -----------------------------------------------------------------------


def test_find_by_employee_id_returns_empty_list_when_no_match(entra):
    assert entra.find_by_employee_id("E9999") == []


def test_find_by_employee_id_returns_every_match_so_caller_can_spot_ambiguity(entra):
    entra.create_user(body(userPrincipalName="dup.one@contoso.com", employeeId="E4001"))
    entra.create_user(body(userPrincipalName="dup.two@contoso.com", employeeId="E4001"))
    assert len(entra.find_by_employee_id("E4001")) == 2


def test_upn_exists(entra):
    entra.create_user(body())
    assert entra.upn_exists("ASHA.PATEL@contoso.com")
    assert not entra.upn_exists("asha.patel2@contoso.com")


# --- set_manager -----------------------------------------------------------------


def test_set_manager(entra):
    manager = entra.create_user(body(employeeId="E1008", userPrincipalName="manager@contoso.com"))
    user = entra.create_user(body())

    entra.set_manager(user["id"], manager["id"])

    assert entra.find_by_employee_id("E1042")[0]["manager"] == manager["id"]


def test_set_manager_rejects_unknown_manager(entra):
    user = entra.create_user(body())
    with pytest.raises(PermanentError) as exc:
        entra.set_manager(user["id"], "no-such-id")
    assert exc.value.status == 404


def test_user_cannot_manage_themselves(entra):
    user = entra.create_user(body())
    with pytest.raises(PermanentError, match="own manager"):
        entra.set_manager(user["id"], user["id"])


# --- assign_license --------------------------------------------------------------


def test_assign_license_starts_a_pending_mailbox_with_no_address(entra, session):
    user = entra.create_user(body())
    entra.assign_license(user["id"])

    mailbox = session.get(ExoMailbox, user["id"])
    assert mailbox.provisioning_status == "PendingInput"
    assert mailbox.proxy_addresses == []


def test_assign_license_uses_configured_readiness_delay(session):
    entra = MockEntraClient(session, mailbox_checks_until_ready=3)
    user = entra.create_user(body())
    entra.assign_license(user["id"])
    assert session.get(ExoMailbox, user["id"]).checks_until_ready == 3


def test_assign_license_twice_does_not_create_a_second_mailbox(entra, session):
    user = entra.create_user(body())
    entra.assign_license(user["id"])
    entra.assign_license(user["id"])
    assert session.query(ExoMailbox).count() == 1


def test_assign_license_requires_usage_location(entra):
    user = entra.create_user(body(usageLocation=None))
    with pytest.raises(PermanentError, match="usageLocation"):
        entra.assign_license(user["id"])


def test_assign_license_to_unknown_user_is_404(entra):
    with pytest.raises(PermanentError) as exc:
        entra.assign_license("no-such-id")
    assert exc.value.status == 404
