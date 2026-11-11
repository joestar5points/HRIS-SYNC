"""Abstracted HRIS Employee table.

Records are stored exactly as received, including invalid ones. Validation happens in
``hris_sync.validation`` and an invalid record becomes a ManualReview
outcome rather than a load failure.
"""

import json
from datetime import datetime
from typing import Any, Optional

from sqlalchemy import DateTime, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from hris_sync.db import Base, utcnow


def _text(value: Any) -> Optional[str]:
    return None if value is None else str(value)


class HrisWorker(Base):
    __tablename__ = "hris_employees"

    id: Mapped[int] = mapped_column(primary_key=True)
    employee_id: Mapped[Optional[str]] = mapped_column("employeeId", String(64), index=True)
    status: Mapped[Optional[str]] = mapped_column(String(64))
    first_name: Mapped[Optional[str]] = mapped_column("firstName", String(100))
    last_name: Mapped[Optional[str]] = mapped_column("lastName", String(100))
    department: Mapped[Optional[str]] = mapped_column(String(100))
    manager_employee_id: Mapped[Optional[str]] = mapped_column("managerEmployeeId", String(64))
    start_date: Mapped[Optional[str]] = mapped_column("startDate", String(32))
    end_date: Mapped[Optional[str]] = mapped_column("endDate", String(32))
    email: Mapped[Optional[str]] = mapped_column(String(255))
    # The original record, so validation and audit always see exactly what was received.
    raw: Mapped[str] = mapped_column(Text)
    loaded_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, onupdate=utcnow
    )

    @classmethod
    def from_record(cls, record: dict) -> "HrisWorker":
        return cls(
            employee_id=_text(record.get("employeeId")),
            status=_text(record.get("status")),
            first_name=_text(record.get("firstName")),
            last_name=_text(record.get("lastName")),
            department=_text(record.get("department")),
            manager_employee_id=_text(record.get("managerEmployeeId")),
            start_date=_text(record.get("startDate")),
            end_date=_text(record.get("endDate")),
            email=_text(record.get("email")),
            raw=json.dumps(record, sort_keys=True),
        )

    def to_record(self) -> dict:
        return json.loads(self.raw)

    def __repr__(self) -> str:
        return f"<HrisWorker {self.employee_id!r}>"
