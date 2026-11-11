"""Abstraction of  HRIS API: read a worker and write back their confirmed email address.

Workers are only ever identified by employeeId, never by name or email.
"""

import random
import re
from typing import Optional

from sqlalchemy import select
from sqlalchemy.orm import Session

from hris_sync.errors import ConflictError, PermanentError
from hris_sync.mocks.faults import FaultInjection, injects_faults
from hris_sync.models import HrisWorker

# Simple Regex Pattern for Validating Emails
EMAIL_PATTERN = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")


class MockHrisClient(FaultInjection):
    def __init__(self, session: Session, failure_rate: float = 0.0, lost_response_rate: float = 0.0,
                 rng: Optional[random.Random] = None, fault_methods: Optional[set[str]] = None):
        self.session = session
        # Random 429/503s, before or after the call takes effect (see mocks/faults.py).
        self._init_faults(failure_rate, lost_response_rate, rng, fault_methods)

    @injects_faults
    def get_worker(self, employee_id: str) -> Optional[dict]:
        """The worker's current record"""
        rows = self._rows(employee_id)
        if not rows:
            return None
        if len(rows) > 1:
            raise PermanentError(409, f"more than one HRIS worker has employeeId '{employee_id}'")
        worker = rows[0]
        return {**worker.to_record(), "email": worker.email}

    @injects_faults
    def update_email(self, employee_id: str, email: str) -> str:
        """Write the confirmed address. Returns "Updated" or "NoChange".

        Raises PermanentError for a bad address or an unknown/ambiguous employeeId,
        and ConflictError if the worker already has a different email.
        """
        if not isinstance(email, str) or not EMAIL_PATTERN.match(email):
            raise PermanentError(400, f"'{email}' is not a valid email address")

        rows = self._rows(employee_id)
        if not rows:
            raise PermanentError(404, f"no HRIS worker with employeeId '{employee_id}'")
        if len(rows) > 1:
            raise PermanentError(409, f"more than one HRIS worker has employeeId '{employee_id}'")
        worker = rows[0]

        if worker.email and worker.email.lower() == email.lower():
            return "NoChange"
        if worker.email:
            raise ConflictError(409, "HRIS already holds a different email for this worker")

        worker.email = email
        self.session.commit()
        return "Updated"

    def _rows(self, employee_id: str) -> list[HrisWorker]:
        return list(
            self.session.scalars(select(HrisWorker).where(HrisWorker.employee_id == employee_id))
        )
