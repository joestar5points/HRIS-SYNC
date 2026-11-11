import pytest

from hris_sync.addresses import choose_upn, primary_smtp


def nothing_taken(upn):
    return False


# --- choose_upn ------------------------------------------------------------------


def test_choose_upn_uses_first_dot_last_at_contoso():
    assert choose_upn("Asha", "Patel", nothing_taken) == "asha.patel@contoso.com"


@pytest.mark.parametrize(
    "first, last, expected",
    [
        ("Zoë", "Brontë", "zoe.bronte@contoso.com"),
        ("Liam", "O'Brien", "liam.obrien@contoso.com"),
        ("Mary Ann", "Smith-Jones", "maryann.smith-jones@contoso.com"),
        ("  ASHA ", "PATEL", "asha.patel@contoso.com"),
    ],
)
def test_choose_upn_cleans_names(first, last, expected):
    assert choose_upn(first, last, nothing_taken) == expected


def test_choose_upn_numbers_when_taken():
    taken = {"asha.patel@contoso.com", "asha.patel2@contoso.com"}
    assert choose_upn("Asha", "Patel", taken.__contains__) == "asha.patel3@contoso.com"


def test_choose_upn_uses_given_domain():
    assert choose_upn("Asha", "Patel", nothing_taken, domain="fabrikam.com") == "asha.patel@fabrikam.com"


def test_choose_upn_rejects_names_with_nothing_usable():
    with pytest.raises(ValueError):
        choose_upn("李", "王", nothing_taken)


# --- primary_smtp ----------------------------------------------------------------


def user(status, proxy_addresses):
    return {
        "provisionedPlans": [{"service": "exchange", "provisioningStatus": status}],
        "proxyAddresses": proxy_addresses,
    }


def test_primary_smtp_when_ready():
    ready = user("Success", ["smtp:asha.patel@contoso.onmicrosoft.com", "SMTP:asha.patel@contoso.com"])
    assert primary_smtp(ready) == "asha.patel@contoso.com"


def test_not_ready_while_provisioning_is_pending():
    assert primary_smtp(user("PendingInput", [])) is None


def test_not_ready_if_only_secondary_aliases_exist():
    assert primary_smtp(user("Success", ["smtp:asha.patel@contoso.onmicrosoft.com"])) is None


def test_not_ready_if_exchange_reports_success_for_another_service_only():
    other = {
        "provisionedPlans": [{"service": "SharePoint", "provisioningStatus": "Success"}],
        "proxyAddresses": ["SMTP:asha.patel@contoso.com"],
    }
    assert primary_smtp(other) is None


def test_not_ready_for_a_user_with_no_mailbox_fields():
    assert primary_smtp({"id": "abc"}) is None
