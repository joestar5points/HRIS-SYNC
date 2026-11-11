"""Tables the sync service needs to operate
The only Tables that will remain in production
"""

from datetime import date, datetime
from enum import StrEnum
from typing import Optional

from sqlalchemy import JSON, Boolean, Date, DateTime, Enum, ForeignKey, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from hris_sync.db import Base, utcnow


class Outcome(StrEnum):
    CREATE = "Create"
    UPDATE = "Update"
    DISABLE = "Disable"
    NO_CHANGE = "NoChange"
    MANUAL_REVIEW = "ManualReview"


class Stage(StrEnum):
    """Enum representing stage within joiner lifecycle"""

    MAILBOX_PENDING = "MailboxPending"
    MAILBOX_READY = "MailboxReady"
    WRITTEN_BACK = "WrittenBack"
    # Exception states
    MAILBOX_TIMED_OUT = "MailboxTimedOut"
    WRITEBACK_FAILED = "WritebackFailed"
    MANUAL_REVIEW = "ManualReview"


class ActionStatus(StrEnum):
    PENDING = "Pending"
    DONE = "Done"
    FAILED = "Failed"
    SKIPPED_DRY_RUN = "SkippedDryRun"


def _enum(enum_cls: type[StrEnum]) -> Enum:
    """Store enum values (not names) as text, with a CHECK constraint on allowed values."""
    return Enum(
        enum_cls,
        native_enum=False,
        create_constraint=True,
        length=32,
        values_callable=lambda members: [m.value for m in members],
    )


class Run(Base):
    """One execution of the CLI. run_id is the correlation ID carried through all audit output."""

    __tablename__ = "runs"

    run_id: Mapped[str] = mapped_column(String(36), primary_key=True)
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    finished_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))
    dry_run: Mapped[bool] = mapped_column(Boolean)
    input_file: Mapped[Optional[str]] = mapped_column(String(512))
    # Counts per outcome and the list of exception-state workers, written at the end of the run.
    summary: Mapped[Optional[dict]] = mapped_column(JSON)


class SyncState(Base):
    """Where each worker is in the joiner lifecycle (identity -> mailbox -> write-back)."""

    __tablename__ = "sync_state"

    employee_id: Mapped[str] = mapped_column(String(16), primary_key=True)
    stage: Mapped[Stage] = mapped_column(_enum(Stage))
    object_id: Mapped[Optional[str]] = mapped_column(String(36))
    # The UPN we created the user with. Usually becomes the primary SMTP, but never assumed to.
    user_principal_name: Mapped[Optional[str]] = mapped_column(String(255))
    confirmed_smtp: Mapped[Optional[str]] = mapped_column(String(255))
    mailbox_attempts: Mapped[int] = mapped_column(Integer, default=0)
    writeback_attempts: Mapped[int] = mapped_column(Integer, default=0)
    # After this, a pending mailbox moves to MailboxTimedOut.
    deadline_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))
    last_error: Mapped[Optional[str]] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, onupdate=utcnow
    )


class Action(Base):
    """Write-ahead log of external calls.

    The row is written as Pending *before* calling Entra/Exchange/HRIS and marked
    Done or Failed afterwards. A Pending row found on startup means the process
    died mid-call, and the next run must check the target system before trying again.
    """

    __tablename__ = "actions"

    id: Mapped[int] = mapped_column(primary_key=True)
    run_id: Mapped[str] = mapped_column(ForeignKey("runs.run_id"), index=True)
    employee_id: Mapped[str] = mapped_column(String(16), index=True)
    #CreateUser, UpdateUser, DisableUser, EnableUser, AssignLicense, WriteBackEmail
    action_type: Mapped[str] = mapped_column(String(32))
    payload: Mapped[Optional[dict]] = mapped_column(JSON)
    status: Mapped[ActionStatus] = mapped_column(_enum(ActionStatus), default=ActionStatus.PENDING)
    error: Mapped[Optional[str]] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    completed_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))


class AuditLog(Base):
    """Append-only audit trail. Never stores tokens, secrets or full personal records."""

    __tablename__ = "audit_log"

    id: Mapped[int] = mapped_column(primary_key=True)
    correlation_id: Mapped[str] = mapped_column(ForeignKey("runs.run_id"), index=True)
    timestamp: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    # Nullable: an invalid record may have no employeeId at all.
    employee_id: Mapped[Optional[str]] = mapped_column(String(64), index=True)
    object_id: Mapped[Optional[str]] = mapped_column(String(36))
    # identity, mailbox or writeback
    phase: Mapped[str] = mapped_column(String(16))
    proposed_action: Mapped[Outcome] = mapped_column(_enum(Outcome))
    reason: Mapped[str] = mapped_column(Text)
    # e.g. Applied, DryRun, Failed, Pending
    result: Mapped[str] = mapped_column(String(32))
    confirmed_smtp: Mapped[Optional[str]] = mapped_column(String(255))
    dry_run: Mapped[bool] = mapped_column(Boolean)


class ScheduledChange(Base):
    """A date-driven change to make later, without needing the HRIS record again."""

    __tablename__ = "scheduled_changes"

    employee_id: Mapped[str] = mapped_column(String(16), primary_key=True)
    # "Enable" (staged joiner's start date) or "Disable" (leaver's end date)
    change: Mapped[str] = mapped_column(String(16), primary_key=True)
    object_id: Mapped[str] = mapped_column(String(36))
    due_on: Mapped[date] = mapped_column(Date, index=True)
    scheduled_in_run: Mapped[str] = mapped_column(ForeignKey("runs.run_id"))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class ProcessedInput(Base):
    """Hash of every record already acted on, so replaying the same input changes nothing."""

    __tablename__ = "processed_inputs"

    record_hash: Mapped[str] = mapped_column(String(64), primary_key=True)
    employee_id: Mapped[Optional[str]] = mapped_column(String(64), index=True)
    first_run_id: Mapped[str] = mapped_column(ForeignKey("runs.run_id"))
    processed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
