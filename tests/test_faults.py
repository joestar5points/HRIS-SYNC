"""Random failure injection in the mocks (mocks/faults.py)."""

import random

import pytest

from hris_sync.errors import TransientError
from hris_sync.main import with_retry
from hris_sync.mocks.entra import MockEntraClient
from hris_sync.mocks.hris import MockHrisClient
from hris_sync.models import HrisWorker

NO_SLEEP = lambda seconds: None  # noqa: E731

USER = {
    "accountEnabled": True,
    "displayName": "Asha Patel",
    "mailNickname": "asha.patel",
    "userPrincipalName": "asha.patel@contoso.com",
    "employeeId": "E1042",
    "usageLocation": "GB",
}


def test_default_mocks_never_fail(session):
    entra = MockEntraClient(session)
    for _ in range(200):
        entra.find_by_employee_id("E1042")
    assert entra.injected_failures == 0


def test_failure_rate_1_fails_every_call_before_it_takes_effect(session):
    entra = MockEntraClient(session, failure_rate=1.0)
    with pytest.raises(TransientError) as exc:
        entra.create_user(USER)

    assert exc.value.status in (429, 503)
    assert "injected transient failure in create_user" in str(exc.value)
    assert MockEntraClient(session).find_by_employee_id("E1042") == []  # nothing created


def test_lost_response_means_the_change_happened_but_the_caller_got_an_error(session):
    entra = MockEntraClient(session, lost_response_rate=1.0)
    with pytest.raises(TransientError, match="lost-response"):
        entra.create_user(USER)

    assert len(MockEntraClient(session).find_by_employee_id("E1042")) == 1  # it WAS created


def test_retrying_a_lost_create_hits_the_upn_conflict_not_a_second_account(session):
    """Why lost responses are dangerous, and why a unique UPN keeps them safe."""
    entra = MockEntraClient(session, lost_response_rate=1.0, fault_methods={"create_user"})
    with pytest.raises(Exception) as exc:
        with_retry(lambda: entra.create_user(USER), sleep=NO_SLEEP)

    assert "already exists" in str(exc.value)
    assert len(entra.find_by_employee_id("E1042")) == 1


def test_429s_carry_retry_after_and_503s_do_not(session):
    entra = MockEntraClient(session, failure_rate=1.0, rng=random.Random(0))
    seen = {}
    for _ in range(50):
        try:
            entra.upn_exists("x@contoso.com")
        except TransientError as exc:
            seen[exc.status] = exc.retry_after
    assert seen == {429: 1.0, 503: None}


def test_fault_methods_limits_injection_to_the_named_calls(session):
    entra = MockEntraClient(session, failure_rate=1.0, fault_methods={"create_user"})
    entra.find_by_employee_id("E1042")  # not listed: never fails
    with pytest.raises(TransientError):
        entra.create_user(USER)


def test_a_seeded_rng_makes_failures_repeatable(session):
    def outcomes(seed):
        entra = MockEntraClient(session, failure_rate=0.5, rng=random.Random(seed))
        result = []
        for _ in range(30):
            try:
                entra.find_by_employee_id("E1042")
                result.append("ok")
            except TransientError as exc:
                result.append(exc.status)
        return result

    assert outcomes(42) == outcomes(42)
    assert outcomes(42) != outcomes(43)


def test_seeded_failures_are_hidden_by_retry(session, valid_record):
    """Fails, retries, then succeeds: deterministic because the RNG is seeded."""
    session.add(HrisWorker.from_record(valid_record))
    session.commit()
    hris = MockHrisClient(session, failure_rate=0.5, rng=random.Random(3))

    worker = with_retry(lambda: hris.get_worker("E1042"), sleep=NO_SLEEP)

    assert worker["employeeId"] == "E1042"
    assert hris.injected_failures >= 1


@pytest.mark.parametrize("kwargs", [{"failure_rate": -0.1}, {"failure_rate": 1.5},
                                    {"lost_response_rate": 2}])
def test_rates_outside_0_to_1_are_rejected(session, kwargs):
    with pytest.raises(ValueError):
        MockHrisClient(session, **kwargs)
