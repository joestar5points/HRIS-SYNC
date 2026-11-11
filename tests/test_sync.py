"""End-to-end runs against the seeded mock tenant and tests/data/all-cases.json."""

import json
import random
from collections import Counter
from datetime import date, timedelta
from pathlib import Path

import pytest
from sqlalchemy import func, select

from hris_sync.db import init_db, make_engine, make_session_factory
from hris_sync.errors import AuthError, PermanentError, TransientError
from hris_sync.main import MAX_WRITEBACK_ATTEMPTS, main, make_entra, sync, with_retry
from hris_sync.mocks.entra import MockEntraClient
from hris_sync.mocks.fixtures import FIXTURE_TODAY, seed
from hris_sync.mocks.hris import MockHrisClient
from hris_sync.mocks.models import EntraUser, ExoMailbox
from hris_sync.models import HrisWorker
from hris_sync.state import (
    Action,
    ActionStatus,
    AuditLog,
    ProcessedInput,
    ScheduledChange,
    Stage,
    SyncState,
)

SAMPLE_INPUT = Path(__file__).parent / "data" / "all-cases.json"
NO_SLEEP = lambda seconds: None  # noqa: E731


@pytest.fixture
def tenant(session):
    seed(session)
    return session


def run(session, apply=True, input_path=SAMPLE_INPUT, **kwargs):
    kwargs.setdefault("today", FIXTURE_TODAY)
    kwargs.setdefault("sleep", NO_SLEEP)
    return sync(session, input_path, apply=apply, **kwargs)


@pytest.fixture
def write_input(tmp_path):
    def _write(records, name="changes.json"):
        path = tmp_path / name
        path.write_text(json.dumps(records), encoding="utf-8")
        return path

    return _write


def sample_record(employee_id):
    return next(r for r in json.loads(SAMPLE_INPUT.read_text()) if r["employeeId"] == employee_id)


def scheduled(session):
    return {(c.employee_id, c.change): c.due_on for c in session.scalars(select(ScheduledChange)).all()}


def entra_count(session):
    return session.scalar(select(func.count()).select_from(EntraUser))


def user(session, employee_id):
    return MockEntraClient(session).find_by_employee_id(employee_id)[0]


def hris_email(session, employee_id):
    return MockHrisClient(session).get_worker(employee_id)["email"]


def stage(session, employee_id):
    return session.get(SyncState, employee_id).stage


# --- with_retry -------------------------------------------------------------------


def flaky(failures, error=TransientError(503)):
    calls = {"n": 0}

    def fn():
        calls["n"] += 1
        if calls["n"] <= failures:
            raise error
        return "ok"

    return fn, calls


def test_retry_succeeds_after_transient_failures():
    fn, calls = flaky(2)
    assert with_retry(fn, sleep=NO_SLEEP) == "ok"
    assert calls["n"] == 3


def test_retry_gives_up_after_the_last_attempt():
    fn, calls = flaky(10)
    with pytest.raises(TransientError):
        with_retry(fn, attempts=4, sleep=NO_SLEEP)
    assert calls["n"] == 4


def test_permanent_errors_are_never_retried():
    fn, calls = flaky(10, PermanentError(400))
    with pytest.raises(PermanentError):
        with_retry(fn, sleep=NO_SLEEP)
    assert calls["n"] == 1


def test_retry_after_is_honoured():
    waits = []
    fn, _ = flaky(1, TransientError(429, retry_after=7))
    with_retry(fn, sleep=waits.append)
    assert waits == [7]


# --- Dry run ----------------------------------------------------------------------


def test_dry_run_changes_nothing(tenant):
    before = entra_count(tenant)
    summary = run(tenant, apply=False)

    assert entra_count(tenant) == before
    assert user(tenant, "E3001")["accountEnabled"] is True  # leaver NOT disabled
    assert user(tenant, "E2001")["department"] == "Finance"  # mover NOT updated
    assert summary["dry_run"] is True


# --- First --apply run --------------------------------------------------------------


def test_apply_creates_updates_and_disables(tenant):
    run(tenant)

    assert user(tenant, "E1042")["userPrincipalName"] == "asha.patel2@contoso.com"  # E5001 has asha.patel@
    assert user(tenant, "E1050")["accountEnabled"] is False  # staged
    assert user(tenant, "E1051")["accountEnabled"] is True  # staged, start date arrived
    assert user(tenant, "E2001")["department"] == "Quality"  # mover
    assert user(tenant, "E3001")["accountEnabled"] is False  # leaver
    assert user(tenant, "E3002")["accountEnabled"] is False  # endDate passed
    assert user(tenant, "E3003")["accountEnabled"] is True  # privileged: left alone
    assert user(tenant, "E1000")["accountEnabled"] is True  # missing from the file: left alone


def test_ambiguous_and_duplicate_records_create_nothing(tenant):
    run(tenant)
    assert len(MockEntraClient(tenant).find_by_employee_id("E4001")) == 2  # still the same two
    assert MockEntraClient(tenant).find_by_employee_id("E8001") == []


def test_write_back_outcomes(tenant):
    run(tenant)

    assert hris_email(tenant, "E1043") == "b.carter@contoso.com"  # Exchange's address, not the UPN
    assert hris_email(tenant, "E1044") == "dan.wright@contoso.com"  # already the same
    assert hris_email(tenant, "E1045") == "e.green@contoso.com"  # conflict: not overwritten
    assert stage(tenant, "E1045") is Stage.MANUAL_REVIEW
    assert hris_email(tenant, "E9001") == "theo.grant@contoso.com"  # adopted after "crash"


def test_mailbox_not_ready_means_no_write_back_until_a_later_run(tenant):
    run(tenant)
    assert stage(tenant, "E1042") is Stage.MAILBOX_PENDING
    assert hris_email(tenant, "E1042") is None

    run(tenant)
    assert stage(tenant, "E1042") is Stage.WRITTEN_BACK
    assert hris_email(tenant, "E1042") == "asha.patel2@contoso.com"


def test_crash_recovery_adopts_instead_of_creating_a_second_account(tenant):
    run(tenant)
    assert len(MockEntraClient(tenant).find_by_employee_id("E9001")) == 1
    assert stage(tenant, "E9001") is Stage.WRITTEN_BACK


# --- Idempotency ------------------------------------------------------------------


def test_running_the_same_input_twice_makes_no_new_identity_changes(tenant):
    run(tenant)
    users_after_first = entra_count(tenant)
    second = run(tenant)

    assert entra_count(tenant) == users_after_first
    assert set(second["counts"]["identity"]) == {"NoChange (Skipped)", "ManualReview (NoAction)"}


def identity_results(session, summary):
    rows = session.scalars(select(AuditLog).where(
        AuditLog.correlation_id == summary["run_id"], AuditLog.phase == "identity")).all()
    return {row.employee_id: (row.result, row.reason) for row in rows}


def test_identical_records_already_handled_are_skipped(tenant):
    run(tenant)
    result, reason = identity_results(tenant, run(tenant))["E2001"]
    assert result == "Skipped"
    assert reason.startswith("duplicate input")


def test_manual_review_records_are_never_skipped_so_they_stay_flagged(tenant):
    run(tenant)
    second = identity_results(tenant, run(tenant))
    assert second["E7002"][0] == "NoAction"  # re-evaluated, still ManualReview
    assert second["E4001"][0] == "NoAction"


def test_dry_run_never_remembers_records(tenant):
    run(tenant, apply=False)
    assert identity_results(tenant, run(tenant))["E2001"][0] == "Applied"  # not skipped


def test_changed_record_is_processed_even_if_an_older_version_was_handled(tenant, write_input):
    run(tenant)
    moved = {**sample_record("E2001"), "department": "IT"}
    run(tenant, input_path=write_input([moved]))
    assert user(tenant, "E2001")["department"] == "IT"


# --- Scheduled changes (no need for the record to be re-sent) -----------------------


def test_future_dates_are_scheduled(tenant):
    run(tenant)
    assert scheduled(tenant) == {
        ("E1050", "Enable"): date(2026, 11, 28),  # staged joiner
        ("E3006", "Disable"): date(2026, 11, 27),  # leaving at the end of the month
    }


def test_scheduled_changes_apply_without_the_records_being_resent(tenant, write_input):
    run(tenant)
    assert user(tenant, "E1050")["accountEnabled"] is False
    assert user(tenant, "E3006")["accountEnabled"] is True

    run(tenant, input_path=write_input([]), today=date(2026, 11, 28))  # nothing new from HRIS

    assert user(tenant, "E1050")["accountEnabled"] is True
    assert user(tenant, "E3006")["accountEnabled"] is False
    assert scheduled(tenant) == {}


def test_scheduled_changes_wait_until_due(tenant, write_input):
    run(tenant)
    run(tenant, input_path=write_input([]), today=date(2026, 11, 17))
    assert user(tenant, "E1050")["accountEnabled"] is False
    assert user(tenant, "E3006")["accountEnabled"] is True


def test_a_moved_start_date_reschedules(tenant, write_input):
    run(tenant)
    moved = {**sample_record("E1050"), "startDate": "2026-12-12"}
    run(tenant, input_path=write_input([moved]))
    assert scheduled(tenant)[("E1050", "Enable")] == date(2026, 12, 12)


def test_a_withdrawn_joiner_cancels_the_scheduled_enable(tenant, write_input):
    run(tenant)
    withdrawn = {**sample_record("E1050"), "status": "Terminated", "endDate": "2026-11-28"}
    run(tenant, input_path=write_input([withdrawn]))

    assert ("E1050", "Enable") not in scheduled(tenant)
    run(tenant, input_path=write_input([]), today=date(2026, 11, 28))
    assert user(tenant, "E1050")["accountEnabled"] is False  # never enabled


def test_an_invalid_newer_record_cancels_a_scheduled_enable_but_keeps_a_disable(tenant, write_input):
    run(tenant)
    bad_joiner = {**sample_record("E1050"), "startDate": "28/11/2026"}
    bad_leaver = {**sample_record("E3006"), "status": "Gone"}
    run(tenant, input_path=write_input([bad_joiner, bad_leaver]))

    assert ("E1050", "Enable") not in scheduled(tenant)  # no access granted on doubtful data
    assert ("E3006", "Disable") in scheduled(tenant)  # removing access is still safe


def test_scheduled_disable_counts_towards_the_bulk_limit(tenant, write_input):
    run(tenant)
    summary = run(tenant, input_path=write_input([]), today=date(2026, 11, 28), max_disables=0)
    assert user(tenant, "E3006")["accountEnabled"] is True  # held
    assert ("E3006", "Disable") in scheduled(tenant)  # still scheduled, flagged again next run
    assert any(i["employee_id"] == "E3006" for i in summary["needs_attention"])


def test_dry_run_reports_scheduled_changes_without_applying_them(tenant, write_input):
    run(tenant)
    run(tenant, apply=False, input_path=write_input([]), today=date(2026, 11, 28))
    assert user(tenant, "E1050")["accountEnabled"] is False
    assert len(scheduled(tenant)) == 2


# --- Safety limits and failure paths ------------------------------------------------


def test_bulk_disable_limit_stops_all_disables(tenant):
    summary = run(tenant, max_disables=1)

    assert user(tenant, "E3001")["accountEnabled"] is True
    assert user(tenant, "E3002")["accountEnabled"] is True
    assert any("bulk-disable limit" in item["reason"] for item in summary["needs_attention"])


def test_mailbox_times_out_into_a_visible_exception_state(tenant):
    run(tenant)  # E1042 left pending
    # Only --today moves: two days later in story time, the 24h deadline has passed.
    summary = run(tenant, input_path=None, today=FIXTURE_TODAY + timedelta(days=2))

    assert stage(tenant, "E1042") is Stage.MAILBOX_TIMED_OUT
    assert any(i["employee_id"] == "E1042" and i["result"] == "TimedOut"
               for i in summary["needs_attention"])


def test_the_mailbox_deadline_follows_today_not_the_real_clock(tenant):
    run(tenant)
    deadline = tenant.get(SyncState, "E1042").deadline_at
    assert deadline.date() == FIXTURE_TODAY + timedelta(days=1)


def test_a_later_run_the_same_day_is_within_the_deadline(tenant):
    run(tenant)
    run(tenant, input_path=None)  # same --today: the next hourly run
    assert stage(tenant, "E1042") is Stage.WRITTEN_BACK


def test_a_run_with_no_input_only_progresses_scheduled_changes_and_mailboxes(tenant):
    run(tenant)
    summary = run(tenant, input_path=None, today=date(2026, 11, 28))

    assert "identity" not in summary["counts"]  # no records, so no identity decisions
    assert user(tenant, "E1050")["accountEnabled"] is True  # scheduled enable applied
    assert user(tenant, "E3006")["accountEnabled"] is False  # scheduled disable applied


# --- Injected failures (mocks/faults.py) ---------------------------------------------


def faulty(session, seed=0, entra_only=None, hris_only=None, **rates):
    """Mock clients with failure injection. *_only limits injection to those methods;
    pass an empty set to keep that client fault-free."""
    rng = random.Random(seed)
    entra = make_entra(session, rng=rng, fault_methods=entra_only, **rates)
    hris = MockHrisClient(session, rng=rng, fault_methods=hris_only, **rates)
    return dict(entra=entra, hris=hris)


def snapshot(session):
    """Everything the service is responsible for, without ids or counters, so two
    databases can be compared."""
    users = session.scalars(select(EntraUser)).all()
    employee_of = {u.object_id: u.employee_id for u in users}
    licensed = set(session.scalars(select(ExoMailbox.object_id)))
    return {
        "entra": sorted((u.employee_id, u.user_principal_name, u.department, u.account_enabled,
                         employee_of.get(u.manager_object_id), u.object_id in licensed) for u in users),
        "hris": sorted(((w.employee_id, w.email) for w in session.scalars(select(HrisWorker))), key=str),
        "stages": {s.employee_id: s.stage for s in session.scalars(select(SyncState))},
        "scheduled": scheduled(session),
    }


def attention(summary):
    return sorted({(i["employee_id"], i["phase"], i["result"]) for i in summary["needs_attention"]}, key=str)


CLEAN_RUNS = 3  # E1042's mailbox needs two checks; one more run proves it's settled


@pytest.fixture(scope="module")
def baseline():
    """The fault-free result: a fresh tenant, the sample file, CLEAN_RUNS runs."""
    engine = make_engine("sqlite://")
    init_db(engine)
    with make_session_factory(engine)() as session:
        seed(session)
        for _ in range(CLEAN_RUNS):
            summary = run(session)
        yield snapshot(session), attention(summary)
    engine.dispose()


def assert_converges_to(session, baseline):
    """Rerun with no failures; the tenant must end exactly where a fault-free run would."""
    for _ in range(CLEAN_RUNS):
        summary = run(session)
    assert snapshot(session) == baseline[0]
    assert attention(summary) == baseline[1]


def test_everything_failing_changes_nothing_and_flags_every_record(tenant):
    run(tenant, apply=False)  # loads the HRIS export; changes nothing else
    before = snapshot(tenant)

    summary = run(tenant, **faulty(tenant, failure_rate=1.0))

    assert snapshot(tenant) == before
    identity = identity_results(tenant, summary)
    valid = {r["employeeId"] for r in json.loads(SAMPLE_INPUT.read_text())
             if r.get("employeeId") and r["employeeId"] not in ("E7002", "E7003", "E7004", "E8001")}
    assert {e for e, (result, _) in identity.items() if result == "Failed"} == valid
    assert tenant.scalar(select(func.count()).select_from(ProcessedInput)) == 0  # nothing remembered


def test_everything_failing_then_recovering_ends_where_a_clean_run_would(tenant, baseline):
    run(tenant, **faulty(tenant, failure_rate=1.0))
    assert_converges_to(tenant, baseline)


def test_failed_create_is_recorded_and_other_records_still_processed(tenant, baseline):
    summary = run(tenant, **faulty(tenant, failure_rate=1.0, entra_only={"create_user"}, hris_only=set()))

    failed = {i["employee_id"] for i in summary["needs_attention"] if i["result"] == "Failed"}
    assert {"E1042", "E1043", "E1046", "E1050"} <= failed
    assert MockEntraClient(tenant).find_by_employee_id("E1042") == []
    assert user(tenant, "E3001")["accountEnabled"] is False  # other records still processed
    assert_converges_to(tenant, baseline)


def test_lost_create_response_never_makes_a_second_account(tenant, baseline):
    """Entra creates the user but the response is lost. The retry hits the UPN
    conflict, so the create is Failed, but the account exists with no licence and
    no SyncState. The next run must adopt it, not create another, and must still
    treat a staged joiner (E1050) as ours, so the enable gets scheduled."""
    summary = run(tenant, **faulty(tenant, lost_response_rate=1.0,
                                   entra_only={"create_user"}, hris_only=set()))
    reasons = {e: r for e, (result, r) in identity_results(tenant, summary).items() if result == "Failed"}
    assert "already exists" in reasons["E1042"]
    assert len(MockEntraClient(tenant).find_by_employee_id("E1042")) == 1

    second = identity_results(tenant, run(tenant))
    assert "adopted" in second["E1042"][1]
    assert second["E1050"][0] == "Applied"  # not ManualReview: our write-ahead log shows we created it
    assert len(MockEntraClient(tenant).find_by_employee_id("E1042")) == 1
    assert_converges_to(tenant, baseline)


def test_write_back_failure_retries_only_the_write_back(tenant):
    run(tenant, **faulty(tenant, failure_rate=1.0, entra_only=set(), hris_only={"update_email"}))
    users_after_first = entra_count(tenant)
    assert stage(tenant, "E1043") is Stage.MAILBOX_READY  # mailbox found, write-back failed
    assert hris_email(tenant, "E1043") is None

    run(tenant)
    assert stage(tenant, "E1043") is Stage.WRITTEN_BACK
    assert hris_email(tenant, "E1043") == "b.carter@contoso.com"
    assert entra_count(tenant) == users_after_first  # no new identity or mailbox
    assert len(MockEntraClient(tenant).find_by_employee_id("E1043")) == 1


def test_lost_write_back_response_is_safe_to_retry(tenant):
    """HRIS saved the email but we never heard back. The next attempt gets NoChange."""
    run(tenant, **faulty(tenant, lost_response_rate=1.0, entra_only=set(), hris_only={"update_email"}))
    assert hris_email(tenant, "E1043") == "b.carter@contoso.com"  # it was written...
    assert stage(tenant, "E1043") is Stage.MAILBOX_READY  # ...but we don't know that yet

    run(tenant)
    assert stage(tenant, "E1043") is Stage.WRITTEN_BACK


class OverwritingHris(MockHrisClient):
    """An HRIS API that, like many real ones, overwrites on a PATCH without complaint."""

    def __init__(self, session):
        super().__init__(session)
        self.writes = []

    def update_email(self, employee_id, email):
        self.writes.append(employee_id)
        worker = self.session.scalars(select(HrisWorker).where(HrisWorker.employee_id == employee_id)).one()
        worker.email = email
        self.session.commit()
        return "Updated"


def test_the_service_itself_never_overwrites_an_existing_hris_email(tenant):
    """The 'never overwrite' rule is ours: it must hold even against an HRIS that would overwrite."""
    hris = OverwritingHris(tenant)
    run(tenant, hris=hris)

    assert hris_email(tenant, "E1045") == "e.green@contoso.com"  # different address: untouched
    assert stage(tenant, "E1045") is Stage.MANUAL_REVIEW
    assert "E1045" not in hris.writes and "E1044" not in hris.writes  # never even attempted
    assert stage(tenant, "E1044") is Stage.WRITTEN_BACK  # same address: NoChange
    assert "E1043" in hris.writes  # empty field: written


def test_conflict_reason_names_the_address_hris_holds(tenant):
    summary = run(tenant)
    reason = next(i["reason"] for i in summary["needs_attention"] if i["employee_id"] == "E1045")
    assert "e.green@contoso.com" in reason


def test_email_filled_in_between_our_read_and_write_is_still_a_conflict(tenant):
    """Race: the field was empty when we read it, then someone filled it. The HRIS's
    own refusal (409) is the second line of defence."""
    class StaleRead(MockHrisClient):
        def get_worker(self, employee_id):
            worker = super().get_worker(employee_id)
            if employee_id == "E1043":
                tenant.scalars(select(HrisWorker).where(HrisWorker.employee_id == "E1043")).one().email = "ben@contoso.com"
                tenant.commit()
                return {**worker, "email": None}  # what we saw a moment earlier
            return worker

    run(tenant, hris=StaleRead(tenant))
    assert stage(tenant, "E1043") is Stage.MANUAL_REVIEW
    assert hris_email(tenant, "E1043") == "ben@contoso.com"


def test_a_permanent_write_back_rejection_goes_straight_to_a_person(tenant):
    class Rejects(MockHrisClient):
        def update_email(self, employee_id, email):
            raise PermanentError(400, "address rejected by HRIS")

    summary = run(tenant, hris=Rejects(tenant))
    assert stage(tenant, "E1043") is Stage.WRITEBACK_FAILED  # first run, not after 5
    reason = next(i["reason"] for i in summary["needs_attention"]
                  if i["employee_id"] == "E1043" and i["phase"] == "writeback")
    assert "by hand" in reason


def test_an_auth_failure_on_write_back_is_retried_next_run(tenant):
    """An expired secret or missing scope is a config fix, not a data problem."""
    class Unauthorised(MockHrisClient):
        def update_email(self, employee_id, email):
            raise AuthError(403, "missing Write.Employees.Email scope")

    run(tenant, hris=Unauthorised(tenant))
    assert stage(tenant, "E1043") is Stage.MAILBOX_READY  # kept for retry
    run(tenant)
    assert hris_email(tenant, "E1043") == "b.carter@contoso.com"


def test_write_back_gives_up_after_max_attempts_into_a_visible_state(tenant):
    """HRIS keeps failing: retried once per run, then WritebackFailed, never a new identity."""
    hris_down = dict(entra_only=set(), hris_only={"update_email"}, failure_rate=1.0)
    users_after_first = None
    for attempt in range(1, MAX_WRITEBACK_ATTEMPTS + 1):
        summary = run(tenant, **faulty(tenant, **hris_down))
        users_after_first = users_after_first or entra_count(tenant)
        assert any(i["employee_id"] == "E1043" and i["result"] == "Failed" for i in summary["needs_attention"])
        assert stage(tenant, "E1043") is (Stage.WRITEBACK_FAILED if attempt == MAX_WRITEBACK_ATTEMPTS
                                          else Stage.MAILBOX_READY)

    assert tenant.get(SyncState, "E1043").writeback_attempts == MAX_WRITEBACK_ATTEMPTS
    run(tenant)  # HRIS is back, but we gave up: a person has to look, nothing is retried silently
    assert stage(tenant, "E1043") is Stage.WRITEBACK_FAILED
    assert hris_email(tenant, "E1043") is None
    assert entra_count(tenant) == users_after_first


def test_a_failing_scheduled_change_stays_scheduled(tenant, write_input):
    run(tenant)
    run(tenant, input_path=write_input([]), today=date(2026, 11, 28),
        **faulty(tenant, failure_rate=1.0, hris_only=set()))
    assert set(scheduled(tenant)) == {("E1050", "Enable"), ("E3006", "Disable")}

    run(tenant, input_path=write_input([]), today=date(2026, 11, 28))
    assert scheduled(tenant) == {}


ORIGINAL_EMAILS = {r["employeeId"]: r["email"] for r in json.loads(SAMPLE_INPUT.read_text()) if r.get("email")}


@pytest.mark.parametrize("seed_value", range(8))
def test_invariants_hold_however_random_failures_land(tenant, baseline, seed_value):
    """Random failures, both before and after the call takes effect, across several
    runs. Whatever the dice do, these must always hold, and once the failures stop
    the tenant must end exactly where a fault-free run would."""
    for i in range(3):
        summary = run(tenant, **faulty(tenant, seed=seed_value * 10 + i,
                                       failure_rate=0.3, lost_response_rate=0.2))

        # One account per employeeId: never a duplicate (E4001 was seeded with two).
        counts = Counter(tenant.scalars(select(EntraUser.employee_id)))
        assert all(n == 1 for e, n in counts.items() if e != "E4001"), counts
        # An email HRIS already held is never overwritten.
        assert all(hris_email(tenant, e) == email for e, email in ORIGINAL_EMAILS.items())
        # Every Entra call's outcome is known: no write-ahead row left Pending.
        actions = tenant.scalars(select(Action).where(Action.run_id == summary["run_id"])).all()
        assert all(a.status is not ActionStatus.PENDING for a in actions)
        # Every failed action is in the audit log as Failed, so a human can see it.
        failed_audits = {(r.employee_id, r.result) for r in tenant.scalars(
            select(AuditLog).where(AuditLog.correlation_id == summary["run_id"]))}
        assert all((a.employee_id, "Failed") in failed_audits
                   for a in actions if a.status is ActionStatus.FAILED)

    assert_converges_to(tenant, baseline)


# --- CLI ----------------------------------------------------------------------------


def test_cli_seed_dry_run_and_apply(tmp_path, capsys):
    db = str(tmp_path / "cli.db")
    assert main(["--db", db, "seed"]) == 0
    assert main(["--db", db, "sync", "--input", str(SAMPLE_INPUT), "--today", "2026-11-06"]) == 2
    assert "DRY RUN" in capsys.readouterr().out
    assert main(["--db", db, "status"]) == 0


def test_cli_seed_empty_loads_no_fixtures(tmp_path):
    db = str(tmp_path / "cli.db")
    assert main(["--db", db, "seed"]) == 0
    assert main(["--db", db, "seed", "--empty"]) == 0
    with make_session_factory(make_engine(f"sqlite:///{db}"))() as session:
        assert session.scalar(select(func.count()).select_from(EntraUser)) == 0
        assert session.scalar(select(func.count()).select_from(HrisWorker)) == 0


def test_cli_rejects_a_broken_input_file(tmp_path, capsys):
    db, bad = str(tmp_path / "cli.db"), tmp_path / "bad.json"
    bad.write_text("not json", encoding="utf-8")
    assert main(["--db", db, "sync", "--input", str(bad)]) == 1
    assert "Nothing was processed" in capsys.readouterr().err
