"""Load an HRIS export (JSON array of worker records).

Rules:
- The whole file must be a JSON array, otherwise nothing is loaded (LoadError).
- Records with an employeeId are upserted by employeeId.
- An existing email is never overwritten with null: once the write-back has set
  it, a reload of an export that still says "email": null must not wipe it.
- Records with no usable employeeId can't be upserted, so they're inserted as
  their own row, but only once: an identical record already in the table is
  skipped, so reloading the same file doesn't pile up copies.
- Every record from the file is returned unchanged (including invalid ones) as
  the work list for this run. Validation happens later, per record.
"""

import hashlib
import json
from pathlib import Path
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from hris_sync.errors import LoadError
from hris_sync.models import HrisWorker

FIELDS = (
    "employee_id",
    "status",
    "first_name",
    "last_name",
    "department",
    "manager_employee_id",
    "start_date",
    "end_date",
)


def record_hash(record: Any) -> str:
    """Stable fingerprint of a record. Used for duplicate detection."""
    canonical = json.dumps(record, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode()).hexdigest()


def read_input(path: Path) -> list:
    try:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
    except FileNotFoundError:
        raise LoadError(f"input file not found: {path}") from None
    except json.JSONDecodeError as exc:
        raise LoadError(f"input file is not valid JSON: {exc}") from None
    if not isinstance(data, list):
        raise LoadError("input file must contain a JSON array of worker records")
    return data


def load_input(session: Session, path: Path) -> list:
    """Load the file into hris_employees and return its records as the work list."""
    records = read_input(path)
    for record in records:
        _store(session, record)
    session.commit()
    return records


def _store(session: Session, record: Any) -> None:
    if not isinstance(record, dict):
        return  # can't be stored; validation will report it as ManualReview

    incoming = HrisWorker.from_record(record)
    employee_id = record.get("employeeId")

    if not isinstance(employee_id, str) or not employee_id.strip():
        # No key to upsert on. Insert once; skip if an identical record is already there.
        already_there = session.scalars(
            select(HrisWorker.id).where(HrisWorker.raw == incoming.raw)
        ).first()
        if already_there is None:
            session.add(incoming)
        return

    existing = session.scalars(
        select(HrisWorker).where(HrisWorker.employee_id == employee_id)
    ).first()
    if existing is None:
        session.add(incoming)
        return

    for field in FIELDS:
        setattr(existing, field, getattr(incoming, field))
    existing.raw = incoming.raw
    if incoming.email is not None:
        existing.email = incoming.email
