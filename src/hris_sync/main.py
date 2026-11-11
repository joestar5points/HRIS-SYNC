"""hris-sync: HRIS -> Entra ID / Exchange Online identity sync.
    1. load_input          HRIS export -> mocked HRIS table; returns the records
    2. run_identity_pass   per record: skip if identical to one already handled,
                           else validate -> look up in Entra -> decide()
                           -> (only with --apply) create / update / disable,
                           and schedule any future-dated change (start/end date)
    3. run_scheduled_pass  apply scheduled changes that have fallen due, whether
                           or not the worker is in this input file
    4. run_mailbox_pass    per joiner in SyncState: check the mailbox once
                           -> read the confirmed SMTP address -> write it to HRIS
    5. summarise           counts + anything needing a human; exit code 2 if so
"""

import argparse
import json
import random
import sys
import time
import uuid
from collections import Counter
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Callable, Optional

from sqlalchemy import select
from sqlalchemy.orm import Session

from hris_sync.addresses import choose_upn, primary_smtp
from hris_sync.db import Base, init_db, make_engine, make_session_factory, utcnow
from hris_sync.errors import AuthError, ConflictError, LoadError, PermanentError, SyncError, TransientError
from hris_sync.loader import load_input, record_hash
from hris_sync.mocks.entra import MockEntraClient
from hris_sync.mocks.fixtures import DEFAULT_MAILBOX_CHECKS, MAILBOX_SETTINGS, seed
from hris_sync.mocks.hris import MockHrisClient
from hris_sync.state import (
    Action,
    ActionStatus,
    AuditLog,
    Outcome,
    ProcessedInput,
    Run,
    ScheduledChange,
    Stage,
    SyncState,
)
from hris_sync.validation import LEAVER_STATUSES, validate

# Globals

MAX_DISABLES_PER_RUN = 5  #To prevent flag suspicious bulk disables
MAILBOX_TIMEOUT = timedelta(hours=24)  #For MailboxTimedOut
MAX_WRITEBACK_ATTEMPTS = 5
EXCEPTION_STAGES = (Stage.MAILBOX_TIMED_OUT, Stage.WRITEBACK_FAILED, Stage.MANUAL_REVIEW)

Sleep = Callable[[float], None]


Decision = tuple[Outcome, str, dict]


def decide(
    record: dict,
    matches: list[dict],
    today: date,
    manager_object_id: Optional[str],
    created_by_sync: bool = False,
) -> Decision:
    """Decide the identity action for one HRIS record.

    record             the HRIS record exactly as received
    matches            Entra users with this employeeId (Graph-shaped dicts)
    today              the date to judge start/end dates against
    manager_object_id  Entra object id of the record's manager, or None if not found
    created_by_sync    True if this service created the Entra account (it has a
                       SyncState row), so a disabled account is our staged joiner

    """
    #Anything we can't handle safely goes to a human.
    errors = validate(record)
    if errors:
        return Outcome.MANUAL_REVIEW, "; ".join(errors), {}

    employee_id = record["employeeId"]

    #Never guess between two accounts.
    if len(matches) > 1:
        return (
            Outcome.MANUAL_REVIEW,
            f"{len(matches)} Entra identities share employeeId {employee_id}",
            {},
        )

    start = date.fromisoformat(record["startDate"])
    end = date.fromisoformat(record["endDate"]) if record.get("endDate") else None
    is_leaver = record["status"] in LEAVER_STATUSES or (end is not None and end <= today)
    no_manager_note = "" if manager_object_id else f"; manager {record['managerEmployeeId']} not found in Entra"

    #No account yet.
    if not matches:
        if is_leaver:
            return Outcome.NO_CHANGE, "leaver with no Entra identity, nothing to disable", {}
        if start > today:
            return (
                Outcome.CREATE,
                f"new joiner starting {start}: created disabled until start date (staged)"
                + no_manager_note,
                {"accountEnabled": False},
            )
        return Outcome.CREATE, "new joiner" + no_manager_note, {"accountEnabled": True}

    user = matches[0]

    # Leavers -> disable, never delete, never touch admin accounts.
    if is_leaver:
        why = f"status {record['status']}" if record["status"] in LEAVER_STATUSES else f"endDate {end} has passed"
        if not user["accountEnabled"]:
            return Outcome.NO_CHANGE, f"leaver ({why}), account already disabled", {}
        if user.get("isPrivileged"):
            return (
                Outcome.MANUAL_REVIEW,
                f"leaver ({why}) but account is privileged, will not auto-disable",
                {},
            )
        return Outcome.DISABLE, f"leaver ({why})", {"accountEnabled": False}

    #Active worker with an account: update only what differs. (No first or last name that would introduce UPN failure)
    changes = {}
    if not user["accountEnabled"]:
        if not created_by_sync:
            # Someone else disabled it (e.g. a suspension). Re-enabling isn't our call.
            return (
                Outcome.MANUAL_REVIEW,
                "HRIS says Active but the Entra account was disabled outside this service",
                {},
            )
        if start <= today:
            changes["accountEnabled"] = True  # staged joiner's start date has arrived
    if user.get("department") != record["department"]:
        changes["department"] = record["department"]
    # Only change the manager when we know who the new one is; never blank it out.
    if manager_object_id and user.get("manager") != manager_object_id:
        changes["manager"] = manager_object_id

    if changes:
        return Outcome.UPDATE, "changed: " + ", ".join(sorted(changes)) + no_manager_note, changes
    return Outcome.NO_CHANGE, "Entra already matches HRIS" + no_manager_note, {}


def with_retry(fn, *, attempts: int = 4, base_delay: float = 0.5, max_delay: float = 8.0,
               sleep: Optional[Sleep] = None):
    """Call fn(). Retry TransientError with exponential back-off and
    jitter. Re-raise after the last attempt. Anything else is raised straight away: retrying won't fix it.
    """
    for attempt in range(1, attempts + 1):
        try:
            return fn()
        except TransientError as exc:
            if attempt == attempts:
                raise
            if exc.retry_after is not None:
                delay = exc.retry_after
            else:
                delay = min(max_delay, base_delay * 2 ** (attempt - 1)) + random.uniform(0, base_delay)
            (sleep or time.sleep)(delay)


def audit(session: Session, run: Run, *, phase: str, action: Outcome, reason: str, result: str,
          employee_id: Optional[str] = None, object_id: Optional[str] = None,
          confirmed_smtp: Optional[str] = None) -> None:
    """Write one audit row and print it as one JSON line (JSON Lines, so log tools can ingest it such as Applications Insights).

    Only these fields can ever be logged. No Secrets or Creds in Log
    """
    timestamp = utcnow()
    session.add(AuditLog(
        correlation_id=run.run_id, timestamp=timestamp, employee_id=employee_id,
        object_id=object_id, phase=phase, proposed_action=action, reason=reason,
        result=result, confirmed_smtp=confirmed_smtp, dry_run=run.dry_run,
    ))
    session.commit()
    print(json.dumps({
        "timestamp": timestamp.isoformat(timespec="seconds"),
        "correlation_id": run.run_id,
        "employee_id": employee_id,
        "object_id": object_id,
        "phase": phase,
        "proposed_action": action.value,
        "reason": reason,
        "result": result,
        "confirmed_smtp": confirmed_smtp,
        "dry_run": run.dry_run,
    }))

def run_identity_pass(session: Session, entra: MockEntraClient, hris: MockHrisClient, run: Run,
                      records: list, today: date, max_disables: int = MAX_DISABLES_PER_RUN,
                      scheduled_disables: int = 0, sleep: Sleep = time.sleep,
                      now: Optional[datetime] = None) -> bool:
    """Returns True if the bulk-disable limit was hit (so scheduled disables are held too)."""
    # Same employeeId twice in one file: we can't tell which is right, so neither is acted on.
    id_counts = Counter(
        r["employeeId"] for r in records
        if isinstance(r, dict) and isinstance(r.get("employeeId"), str)
    )

    # Decide everything first, so the bulk-disable check can see the whole run
    # (including scheduled disables that fall due today).
    plans = [_plan(session, entra, record, today, id_counts, sleep) for record in records]

    disables = sum(1 for p in plans if p["outcome"] is Outcome.DISABLE) + scheduled_disables
    limit_hit = disables > max_disables
    if limit_hit:
        for p in plans:
            if p["outcome"] is Outcome.DISABLE:
                p["outcome"], p["changes"] = Outcome.MANUAL_REVIEW, {}
                p["reason"] = (f"bulk-disable limit: {disables} disables in this run, limit is "
                               f"{max_disables}; nothing disabled ({p['reason']})")

    for plan in plans:
        _act(session, entra, hris, run, plan, today, sleep, now or utcnow())
    return limit_hit


def _plan(session: Session, entra: MockEntraClient, record, today: date, id_counts: Counter,
          sleep: Sleep) -> dict:
    """Look up what's needed and call decide(). Changes nothing."""
    employee_id = record.get("employeeId") if isinstance(record, dict) else None
    key = employee_id if isinstance(employee_id, str) and employee_id.strip() else None
    plan = {"record": record, "employee_id": key, "matches": [], "manager_id": None,
            "hash": record_hash(record), "error": None, "skipped": False, "created_by_sync": False}

    if key and id_counts[key] > 1:
        return {**plan, "outcome": Outcome.MANUAL_REVIEW, "changes": {},
                "reason": f"employeeId {key} appears {id_counts[key]} times in this input file"}

    # Identical to a record already fully handled Date-driven
    seen = session.get(ProcessedInput, plan["hash"])
    if seen is not None:
        return {**plan, "outcome": Outcome.NO_CHANGE, "changes": {}, "skipped": True,
                "reason": f"duplicate input: identical record already handled in run {seen.first_run_id}"}

    if validate(record):
        outcome, reason, changes = decide(record, [], today, None)  # -> ManualReview with the errors
        return {**plan, "outcome": outcome, "reason": reason, "changes": changes}

    try:
        matches = with_retry(lambda: entra.find_by_employee_id(key), sleep=sleep)
        managers = with_retry(lambda: entra.find_by_employee_id(record["managerEmployeeId"]), sleep=sleep)
    except SyncError as exc:
        return {**plan, "outcome": Outcome.MANUAL_REVIEW, "changes": {}, "error": str(exc),
                "reason": f"Entra lookup failed: {exc}"}

    manager_id = managers[0]["id"] if len(managers) == 1 else None
    created_by_sync = _created_by_us(session, key)
    outcome, reason, changes = decide(record, matches, today, manager_id, created_by_sync)

    return {**plan, "matches": matches, "manager_id": manager_id, "created_by_sync": created_by_sync,
            "outcome": outcome, "reason": reason, "changes": changes}


def _act(session: Session, entra: MockEntraClient, hris: MockHrisClient, run: Run, plan: dict,
         today: date, sleep: Sleep = time.sleep, now: Optional[datetime] = None) -> None:
    """Carry out one planned decision audit in dry-run."""
    employee_id, outcome, reason = plan["employee_id"], plan["outcome"], plan["reason"]
    object_id = plan["matches"][0]["id"] if len(plan["matches"]) == 1 else None

    if plan["skipped"]:
        audit(session, run, phase="identity", action=outcome, reason=reason, result="Skipped",
              employee_id=employee_id)
        return

    if plan["error"]:
        audit(session, run, phase="identity", action=outcome, reason=reason, result="Failed",
              employee_id=employee_id)
        return

    if outcome is Outcome.MANUAL_REVIEW:
        if not run.dry_run and employee_id:
            reason += _hold_scheduled_enable(session, employee_id)
        audit(session, run, phase="identity", action=outcome, reason=reason,
              result="DryRun" if run.dry_run else "NoAction", employee_id=employee_id, object_id=object_id)
        return

    if outcome is Outcome.NO_CHANGE:
        if not run.dry_run:
            try:
                reason += _adopt_if_orphaned(session, entra, hris, plan, sleep, now)
            except SyncError as exc:
                audit(session, run, phase="identity", action=outcome, reason=f"{reason}; adopting the "
                      f"existing identity failed: {exc}; retried next run", result="Failed",
                      employee_id=employee_id, object_id=object_id)
                return
        reason += _schedule(session, run, plan, today, object_id)
        audit(session, run, phase="identity", action=outcome, reason=reason,
              result="DryRun" if run.dry_run else "NoAction", employee_id=employee_id, object_id=object_id)
        _remember(session, run, plan)
        return

    if run.dry_run:
        reason += _schedule(session, run, plan, today, object_id)
        audit(session, run, phase="identity", action=outcome, reason=reason, result="DryRun",
              employee_id=employee_id, object_id=object_id)
        return

    # Write-ahead: record the intent BEFORE calling Entra, so a crash mid-call is visible.
    action = Action(run_id=run.run_id, employee_id=employee_id, action_type=outcome.value,
                    payload=plan["changes"], status=ActionStatus.PENDING)
    session.add(action)
    session.commit()

    try:
        if outcome is Outcome.CREATE:
            object_id = _create(session, entra, plan, sleep, now)
        elif outcome is Outcome.UPDATE:
            _update(entra, plan, object_id, sleep)
            if _create_attempted(session, employee_id):  # a half-finished create of ours
                reason += _adopt_if_orphaned(session, entra, hris, plan, sleep, now)
        elif outcome is Outcome.DISABLE:
            with_retry(lambda: entra.update_user(object_id, {"accountEnabled": False}), sleep=sleep)
    except (SyncError, ValueError) as exc:
        action.status, action.error, action.completed_at = ActionStatus.FAILED, str(exc), utcnow()
        session.commit()
        audit(session, run, phase="identity", action=outcome, reason=f"{reason}; failed: {exc}",
              result="Failed", employee_id=employee_id, object_id=object_id)
        return

    action.status, action.completed_at = ActionStatus.DONE, utcnow()
    session.commit()
    reason += _schedule(session, run, plan, today, object_id)
    audit(session, run, phase="identity", action=outcome, reason=reason, result="Applied",
          employee_id=employee_id, object_id=object_id)
    _remember(session, run, plan)


def _schedule(session: Session, run: Run, plan: dict, today: date, object_id: Optional[str]) -> str:
    """Replace this worker's scheduled changes with whatever their latest record implies.

    - start date in the future and the account is (or will be) disabled -> Enable on startDate
    - end date in the future -> Disable on endDate
    The latest record always wins, so a moved date reschedules and a withdrawn
    joiner (now a Leaver) cancels the pending enable. Returns text for the audit
    reason. In dry-run nothing is saved; the reason says what would be scheduled.
    """
    record, employee_id = plan["record"], plan["employee_id"]
    if not employee_id or validate(record):
        return ""

    wanted = []
    if object_id and record["status"] not in LEAVER_STATUSES:
        start = date.fromisoformat(record["startDate"])
        end = date.fromisoformat(record["endDate"]) if record.get("endDate") else None
        user = plan["matches"][0] if plan["matches"] else None
        enabled_after = plan["changes"].get("accountEnabled", user["accountEnabled"] if user else None)
        ours = plan["outcome"] is Outcome.CREATE or plan["created_by_sync"]
        if start > today and enabled_after is False and ours:
            wanted.append(("Enable", start))
        if end and end > today:
            wanted.append(("Disable", end))

    note = "".join(f"; {'would schedule' if run.dry_run else 'scheduled'} {c.lower()} on {d}"
                   for c, d in wanted)
    if run.dry_run:
        return note

    existing = session.scalars(select(ScheduledChange).where(ScheduledChange.employee_id == employee_id)).all()
    for row in existing:
        session.delete(row)
    for change, due in wanted:
        session.add(ScheduledChange(employee_id=employee_id, change=change, object_id=object_id,
                                    due_on=due, scheduled_in_run=run.run_id))
    session.commit()
    if existing and not wanted:
        note = "; previously scheduled change cancelled by this record"
    return note


def _create(session: Session, entra: MockEntraClient, plan: dict, sleep: Sleep,
            now: Optional[datetime] = None) -> str:
    record = plan["record"]
    upn = choose_upn(record["firstName"], record["lastName"],
                     lambda candidate: with_retry(lambda: entra.upn_exists(candidate), sleep=sleep))
    user = with_retry(lambda: entra.create_user({
        "accountEnabled": plan["changes"]["accountEnabled"],
        "displayName": f"{record['firstName']} {record['lastName']}",
        "givenName": record["firstName"],
        "surname": record["lastName"],
        "mailNickname": upn.split("@")[0],
        "userPrincipalName": upn,
        "employeeId": record["employeeId"],
        "department": record["department"],
        "usageLocation": "GB",
    }), sleep=sleep)
    if plan["manager_id"]:
        with_retry(lambda: entra.set_manager(user["id"], plan["manager_id"]), sleep=sleep)
    with_retry(lambda: entra.assign_license(user["id"]), sleep=sleep)  # starts mailbox provisioning
    _start_mailbox_tracking(session, record["employeeId"], user["id"], upn, now)
    return user["id"]


def _update(entra: MockEntraClient, plan: dict, object_id: str, sleep: Sleep) -> None:
    changes = dict(plan["changes"])
    manager_id = changes.pop("manager", None)
    if manager_id:
        with_retry(lambda: entra.set_manager(object_id, manager_id), sleep=sleep)
    if changes:
        with_retry(lambda: entra.update_user(object_id, changes), sleep=sleep)


def _created_by_us(session: Session, employee_id: str) -> bool:
    """True if this service created the worker's Entra account"""
    return session.get(SyncState, employee_id) is not None or _create_attempted(session, employee_id)


def _create_attempted(session: Session, employee_id: str) -> bool:
    return session.scalar(select(Action.id).where(
        Action.employee_id == employee_id, Action.action_type == Outcome.CREATE.value).limit(1)) is not None


def _adopt_if_orphaned(session: Session, entra: MockEntraClient, hris: MockHrisClient, plan: dict,
                       sleep: Sleep, now: Optional[datetime] = None) -> str:
    """Crash recovery. One matching account and no SyncState, where either our
    write-ahead log shows we attempted the create, or HRIS has no email yet (the
    process died before even logging), means an earlier run created the user but
    died, or lost the response, before saving its state. Pick it up from here
    (licence, mailbox, write-back)
    Returns text for the audit reason ("" if there was nothing to adopt)."""
    employee_id = plan["employee_id"]
    if len(plan["matches"]) != 1 or session.get(SyncState, employee_id) is not None:
        return ""
    if not _create_attempted(session, employee_id):
        worker = with_retry(lambda: hris.get_worker(employee_id), sleep=sleep)
        if worker is None or worker.get("email"):
            return ""
    user = plan["matches"][0]
    with_retry(lambda: entra.assign_license(user["id"]), sleep=sleep)  # no-op if already assigned
    _start_mailbox_tracking(session, employee_id, user["id"], user["userPrincipalName"], now)
    return "; existing identity adopted for mailbox/write-back (no saved state found)"


def _start_mailbox_tracking(session: Session, employee_id: str, object_id: str, upn: str,
                            now: Optional[datetime] = None) -> None:
    state = session.get(SyncState, employee_id) or SyncState(employee_id=employee_id)
    state.stage = Stage.MAILBOX_PENDING
    state.object_id = object_id
    state.user_principal_name = upn
    state.mailbox_attempts = 0
    state.deadline_at = (now or utcnow()) + MAILBOX_TIMEOUT
    session.add(state)
    session.commit()


def _hold_scheduled_enable(session: Session, employee_id: str) -> str:
    """The worker's latest record needs a human, so don't grant access on a date
    taken from older data. A scheduled Disable is kept: removing access is still safe."""
    enable = session.get(ScheduledChange, (employee_id, "Enable"))
    if enable is None:
        return ""
    session.delete(enable)
    session.commit()
    return f"; scheduled enable on {enable.due_on} cancelled until a valid record arrives"


def _remember(session: Session, run: Run, plan: dict) -> None:
    """Hashes a record so no duplicate actions are performed."""
    if run.dry_run or session.get(ProcessedInput, plan["hash"]) is not None:
        return
    session.add(ProcessedInput(record_hash=plan["hash"], employee_id=plan["employee_id"],
                               first_run_id=run.run_id))
    session.commit()


# Functions for Scheduled changes



def due_scheduled_disables(session: Session, today: date) -> int:
    return len(session.scalars(select(ScheduledChange).where(
        ScheduledChange.change == "Disable", ScheduledChange.due_on <= today)).all())


def run_scheduled_pass(session: Session, entra: MockEntraClient, run: Run, today: date,
                       disables_blocked: bool = False, sleep: Sleep = time.sleep) -> None:
    """Apply every scheduled change that has fallen due."""
    due = session.scalars(
        select(ScheduledChange).where(ScheduledChange.due_on <= today)
        .order_by(ScheduledChange.due_on, ScheduledChange.employee_id)
    ).all()

    for change in due:
        details = dict(employee_id=change.employee_id, object_id=change.object_id)
        action = Outcome.UPDATE if change.change == "Enable" else Outcome.DISABLE
        why = f"{'start' if change.change == 'Enable' else 'end'} date {change.due_on} reached"

        if run.dry_run:
            audit(session, run, phase="scheduled", action=action, reason=f"would {change.change.lower()}: {why}",
                  result="DryRun", **details)
            continue

        try:
            users = with_retry(lambda: entra.find_by_employee_id(change.employee_id), sleep=sleep)
            user = next((u for u in users if u["id"] == change.object_id), None)
            if user is None:
                session.delete(change)
                session.commit()
                audit(session, run, phase="scheduled", action=Outcome.MANUAL_REVIEW,
                      reason=f"scheduled {change.change.lower()} ({why}) but the account no longer exists",
                      result="NoAction", **details)
                continue

            wanted_enabled = change.change == "Enable"
            if user["accountEnabled"] == wanted_enabled:
                session.delete(change)
                session.commit()
                audit(session, run, phase="scheduled", action=Outcome.NO_CHANGE,
                      reason=f"{why}; account already {'enabled' if wanted_enabled else 'disabled'}",
                      result="NoAction", **details)
                continue

            if not wanted_enabled and (user.get("isPrivileged") or disables_blocked):
                blocker = ("account is privileged, will not auto-disable" if user.get("isPrivileged")
                           else "bulk-disable limit hit this run")
                audit(session, run, phase="scheduled", action=Outcome.MANUAL_REVIEW,
                      reason=f"{why} but {blocker}; kept scheduled", result="NoAction", **details)
                continue

            with_retry(lambda: entra.update_user(change.object_id, {"accountEnabled": wanted_enabled}),
                       sleep=sleep)
        except SyncError as exc:
            audit(session, run, phase="scheduled", action=action,
                  reason=f"{why}; failed: {exc}; kept scheduled, retried next run", result="Failed", **details)
            continue

        session.delete(change)
        session.commit()
        audit(session, run, phase="scheduled", action=action, reason=why, result="Applied", **details)


# Functions for Pass 2: mailbox readiness and HRIS write-back

def run_mailbox_pass(session: Session, entra: MockEntraClient, hris: MockHrisClient, run: Run,
                     now: Optional[datetime] = None, sleep: Sleep = time.sleep) -> None:
    """Works from SyncState, not the input file, so joiners from earlier runs are picked
    up too. This pass never creates users or mailboxes"""
    now = now or utcnow()
    states = session.scalars(
        select(SyncState)
        .where(SyncState.stage.in_([Stage.MAILBOX_PENDING, Stage.MAILBOX_READY]))
        .order_by(SyncState.employee_id)
    ).all()

    for state in states:
        if run.dry_run:
            audit(session, run, phase="mailbox", action=Outcome.NO_CHANGE,
                  reason=f"would check mailbox and write back to HRIS (stage {state.stage.value})",
                  result="DryRun", employee_id=state.employee_id, object_id=state.object_id)
            continue
        if state.stage is Stage.MAILBOX_PENDING and not _check_mailbox(session, entra, run, state, now, sleep):
            continue
        _write_back(session, hris, run, state, sleep)


def _check_mailbox(session: Session, entra: MockEntraClient, run: Run, state: SyncState,
                   now: datetime, sleep: Sleep) -> bool:
    """One check per run, no waiting. True once the address is known."""
    if state.deadline_at and now > _as_utc(state.deadline_at):
        state.stage, state.last_error = Stage.MAILBOX_TIMED_OUT, "mailbox not provisioned before deadline"
        session.commit()
        audit(session, run, phase="mailbox", action=Outcome.MANUAL_REVIEW,
              reason=f"mailbox still not ready after {state.mailbox_attempts} check{'s' if state.mailbox_attempts != 1 else ''}; deadline "
                     f"{_as_utc(state.deadline_at):%Y-%m-%d %H:%M} UTC passed",
              result="TimedOut", employee_id=state.employee_id, object_id=state.object_id)
        return False

    state.mailbox_attempts += 1
    try:
        user = with_retry(lambda: entra.get_user(state.object_id), sleep=sleep)
    except SyncError as exc:
        state.last_error = str(exc)
        session.commit()
        audit(session, run, phase="mailbox", action=Outcome.NO_CHANGE,
              reason=f"mailbox check failed: {exc}; will retry next run", result="Failed",
              employee_id=state.employee_id, object_id=state.object_id)
        return False

    smtp = primary_smtp(user)  # authoritative address
    if not smtp:
        session.commit()
        audit(session, run, phase="mailbox", action=Outcome.NO_CHANGE,
              reason=f"mailbox not ready (check {state.mailbox_attempts}); no HRIS write-back; "
                     f"checked again next run until {_as_utc(state.deadline_at):%Y-%m-%d %H:%M} UTC",
              result="Pending", employee_id=state.employee_id, object_id=state.object_id)
        return False

    state.stage, state.confirmed_smtp, state.last_error = Stage.MAILBOX_READY, smtp, None
    session.commit()
    return True


def _write_back(session: Session, hris: MockHrisClient, run: Run, state: SyncState, sleep: Sleep) -> None:
    """Write the confirmed address to HRIS, but only into an empty email field.

    We read the worker first and validate, rather than relying on the HRIS
    API to refuse an overwrite (many would simply overwrite on a PATCH):
      empty            -> write it
      same address     -> NoChange, nothing written
      different address -> Conflict, ManualReview, nothing written
    """
    smtp = state.confirmed_smtp
    details = dict(employee_id=state.employee_id, object_id=state.object_id, confirmed_smtp=smtp)
    state.writeback_attempts += 1

    try:
        worker = with_retry(lambda: hris.get_worker(state.employee_id), sleep=sleep)
        if worker is None:
            raise PermanentError(404, f"no HRIS worker with employeeId '{state.employee_id}'")
        existing = worker.get("email")
        if existing and existing.lower() == smtp.lower():
            result = "NoChange"
        elif existing:
            raise ConflictError(409, f"HRIS already holds {existing}")
        else:
            # The HRIS may still refuse (409) if someone filled the field since our read.
            result = with_retry(lambda: hris.update_email(state.employee_id, smtp), sleep=sleep)
    except ConflictError as exc:
        state.stage, state.last_error = Stage.MANUAL_REVIEW, exc.message
        session.commit()
        audit(session, run, phase="writeback", action=Outcome.MANUAL_REVIEW,
              reason=f"{exc.message}, a different email; confirmed Exchange address not written",
              result="Conflict", **details)
        return
    except SyncError as exc:
        state.last_error = str(exc)
        # A permanent rejection (unknown worker, address refused) will never succeed. Otherwise, use Max Writebacks
        permanent = isinstance(exc, PermanentError) and not isinstance(exc, AuthError)
        gave_up = permanent or state.writeback_attempts >= MAX_WRITEBACK_ATTEMPTS
        if gave_up:
            state.stage = Stage.WRITEBACK_FAILED
        session.commit()
        if permanent:
            next_step = "HRIS rejected it, retrying won't help; enter the address in HRIS by hand"
        elif gave_up:
            next_step = "giving up; enter the address in HRIS by hand"
        else:
            next_step = "only the write-back will be retried next run"
        audit(session, run, phase="writeback", action=Outcome.UPDATE,
              reason=(f"HRIS write-back failed (attempt {state.writeback_attempts} of "
                      f"{MAX_WRITEBACK_ATTEMPTS}): {exc}; {next_step}"),
              result="Failed", **details)
        return

    state.stage, state.last_error = Stage.WRITTEN_BACK, None
    session.commit()
    if result == "Updated":
        audit(session, run, phase="writeback", action=Outcome.UPDATE,
              reason="confirmed primary SMTP address written to HRIS", result="Applied", **details)
    else:
        audit(session, run, phase="writeback", action=Outcome.NO_CHANGE,
              reason="HRIS already holds the confirmed address", result="NoAction", **details)


def _as_utc(value: datetime) -> datetime:
    # SQLite hands datetimes back without a timezone; everything we store is UTC.
    return value if value.tzinfo else value.replace(tzinfo=timezone.utc)



# Run Functions for CLI



def sync(session: Session, input_path: Optional[Path], apply: bool = False, today: Optional[date] = None,
         max_disables: int = MAX_DISABLES_PER_RUN, now: Optional[datetime] = None,
         sleep: Sleep = time.sleep, entra: Optional[MockEntraClient] = None,
         hris: Optional[MockHrisClient] = None) -> dict:
    """One full run. Returns the summary. Raises LoadError if the file is unusable.

    input_path None means HRIS sent nothing this run. Does all of the scheduled actions"""
    run = Run(run_id=str(uuid.uuid4()), dry_run=not apply, input_file=str(input_path) if input_path else None)
    session.add(run)
    session.commit()

    records = load_input(session, input_path) if input_path else []
    entra = entra or make_entra(session)
    hris = hris or MockHrisClient(session)
    # Works with the today flag so that time based events can occur
    if now is None:
        now = utcnow() if today is None else datetime.combine(today, utcnow().timetz())
    today = today or now.date()

    disables_blocked = run_identity_pass(session, entra, hris, run, records, today, max_disables,
                                         scheduled_disables=due_scheduled_disables(session, today),
                                         sleep=sleep, now=now)
    run_scheduled_pass(session, entra, run, today, disables_blocked, sleep=sleep)
    run_mailbox_pass(session, entra, hris, run, now=now, sleep=sleep)

    summary = summarise(session, run)
    run.summary, run.finished_at = summary, utcnow()
    session.commit()
    return summary


def make_entra(session: Session, **faults) -> MockEntraClient:
    """The mock tenant's Entra client, with the fixture mailbox behaviour."""
    return MockEntraClient(session, mailbox_checks_until_ready=DEFAULT_MAILBOX_CHECKS,
                           mailbox_settings=MAILBOX_SETTINGS, **faults)


def summarise(session: Session, run: Run) -> dict:
    rows = session.scalars(select(AuditLog).where(AuditLog.correlation_id == run.run_id)).all()
    counts: dict[str, Counter] = {}
    for row in rows:
        counts.setdefault(row.phase, Counter())[f"{row.proposed_action.value} ({row.result})"] += 1

    needs_attention = [
        {"employee_id": r.employee_id, "phase": r.phase, "result": r.result, "reason": r.reason}
        for r in rows
        if r.proposed_action is Outcome.MANUAL_REVIEW or r.result in ("Failed", "TimedOut", "Conflict")
    ]
    pending = [s.employee_id for s in session.scalars(
        select(SyncState).where(SyncState.stage == Stage.MAILBOX_PENDING)).all()]
    upcoming = [f"{c.employee_id} {c.change.lower()} on {c.due_on}" for c in session.scalars(
        select(ScheduledChange).order_by(ScheduledChange.due_on, ScheduledChange.employee_id)).all()]

    return {
        "run_id": run.run_id,
        "dry_run": run.dry_run,
        "counts": {phase: dict(c) for phase, c in counts.items()},
        "needs_attention": needs_attention,
        "mailbox_pending": pending,
        "scheduled": upcoming,
    }


def print_summary(summary: dict) -> None:
    line = "=" * 72
    print(f"\n{line}\nRun {summary['run_id']}" + ("   ** DRY RUN: no changes made **" if summary["dry_run"] else ""))
    print(line)
    for phase, counts in summary["counts"].items():
        print(f"{phase}:")
        for label, n in sorted(counts.items()):
            print(f"  {n:>3}  {label}")
    if summary["mailbox_pending"]:
        print(f"\nMailbox still pending (checked again next run): {', '.join(summary['mailbox_pending'])}")
    if summary["scheduled"]:
        print(f"\nScheduled changes: {'; '.join(summary['scheduled'])}")
    if summary["needs_attention"]:
        # Raised once, in this run's alert. Not stored separately: the audit log is the record.
        print(f"\nNeeds attention ({len(summary['needs_attention'])}); each is recorded (with the Entra "
              f"object id, if any) in audit_log where correlation_id = {summary['run_id']}:")
        for item in summary["needs_attention"]:
            print(f"  {item['employee_id'] or '(no employeeId)':<16} {item['phase']:<10} "
                  f"{item['result']:<9} {item['reason']}")
    print(line)


def print_status(session: Session) -> None:
    states = session.scalars(select(SyncState).order_by(SyncState.employee_id)).all()
    if not states:
        print("No joiners tracked yet.")
    else:
        print(f"{'employeeId':<12} {'stage':<16} {'checks':>6} {'w/b tries':>9}  confirmed SMTP / last error")
        for s in states:
            print(f"{s.employee_id:<12} {s.stage.value:<16} {s.mailbox_attempts:>6} {s.writeback_attempts:>9}  "
                  f"{s.confirmed_smtp or s.last_error or ''}")

    changes = session.scalars(select(ScheduledChange).order_by(ScheduledChange.due_on)).all()
    print("\nScheduled changes:" if changes else "\nNo scheduled changes.")
    for c in changes:
        print(f"  {c.due_on}  {c.change:<8} {c.employee_id}")


def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(prog="hris-sync", description=__doc__.split("\n")[0])
    parser.add_argument("--db", default="hris_sync.db", help="SQLite file (default: hris_sync.db)")
    commands = parser.add_subparsers(dest="command", required=True)

    seed_cmd = commands.add_parser("seed", help="wipe the database and load the mock tenant fixtures")
    seed_cmd.add_argument("--empty", action="store_true", help="wipe the database but load no fixtures")

    sync_cmd = commands.add_parser("sync", help="run the sync (dry run unless --apply)")
    sync_cmd.add_argument("--input", type=Path,
                          help="HRIS delivery (JSON array); leave out when HRIS sent nothing, to only "
                               "progress scheduled changes and pending mailboxes")
    sync_cmd.add_argument("--apply", action="store_true", help="make changes (default is a dry run)")
    sync_cmd.add_argument("--today", type=date.fromisoformat, help="judge dates as of YYYY-MM-DD")
    sync_cmd.add_argument("--max-disables", type=int, default=MAX_DISABLES_PER_RUN)
    faults = sync_cmd.add_argument_group("failure injection (demo only)")
    faults.add_argument("--failure-rate", type=float, default=0.0, metavar="P",
                        help="chance each mock call fails with 429/503 before taking effect")
    faults.add_argument("--lost-response-rate", type=float, default=0.0, metavar="P",
                        help="chance each mock call takes effect but its response is lost")
    faults.add_argument("--random-seed", type=int, help="make injected failures repeatable")

    commands.add_parser("status", help="show where every tracked joiner is up to")

    args = parser.parse_args(argv)
    engine = make_engine(f"sqlite:///{args.db}")
    init_db(engine)

    with make_session_factory(engine)() as session:
        if args.command == "seed":
            Base.metadata.drop_all(engine)
            Base.metadata.create_all(engine)
            if args.empty:
                print(f"Created an empty database in {args.db}")
                return 0
            seed(session)
            print(f"Seeded mock tenant into {args.db}")
            return 0

        if args.command == "status":
            print_status(session)
            return 0

        faults = dict(failure_rate=args.failure_rate, lost_response_rate=args.lost_response_rate,
                      rng=random.Random(args.random_seed))
        try:
            entra, hris = make_entra(session, **faults), MockHrisClient(session, **faults)
        except ValueError as exc:  # a rate outside 0..1
            parser.error(str(exc))
        try:
            summary = sync(session, args.input, apply=args.apply, today=args.today,
                           max_disables=args.max_disables, entra=entra, hris=hris)
        except LoadError as exc:
            print(f"ERROR: {exc}. Nothing was processed.", file=sys.stderr)
            return 1
        print_summary(summary)
        return 2 if summary["needs_attention"] else 0


if __name__ == "__main__":
    sys.exit(main())
