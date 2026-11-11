""" Methods for Validation of incoming HRIS worker records.

``validate`` never raises

returns a list of human-readable errors. An empty
list means the record can be handled safely; anything else is a ManualReview
"""

import re
from datetime import date
from typing import Any, Optional

ALLOWED_STATUSES = frozenset({"Active", "Leaver", "Terminated"})
LEAVER_STATUSES = frozenset({"Leaver", "Terminated"})

REQUIRED_TEXT_FIELDS = (
    "employeeId",
    "status",
    "firstName",
    "lastName",
    "department",
    "managerEmployeeId",
)

# Entra ID's employeeId attribute holds at most 16 characters.
EMPLOYEE_ID_MAX_LENGTH = 16

_ISO_DATE = re.compile(r"^\d{4}-\d{2}-\d{2}$")


def validate(record: Any) -> list[str]:
    if not isinstance(record, dict):
        return ["record is not a JSON object"]

    errors: list[str] = []

    for field in REQUIRED_TEXT_FIELDS:
        value = record.get(field)
        if not isinstance(value, str) or not value.strip():
            errors.append(f"{field} is required")

    employee_id = record.get("employeeId")
    if isinstance(employee_id, str) and employee_id.strip():
        if employee_id != employee_id.strip():
            errors.append("employeeId has leading or trailing whitespace")
        if len(employee_id) > EMPLOYEE_ID_MAX_LENGTH:
            errors.append(f"employeeId is longer than {EMPLOYEE_ID_MAX_LENGTH} characters")

    status = record.get("status")
    if isinstance(status, str) and status.strip() and status not in ALLOWED_STATUSES:
        errors.append(f"status '{status}' is not recognised")

    start = _parse_date(record, "startDate", required=True, errors=errors)
    end = _parse_date(record, "endDate", required=False, errors=errors)
    if start and end and end < start:
        errors.append("endDate is before startDate")

    return errors


def _parse_date(record: dict, field: str, *, required: bool, errors: list[str]) -> Optional[date]:
    value = record.get(field)
    if value is None:
        if required:
            errors.append(f"{field} is required")
        return None
    if not isinstance(value, str) or not _ISO_DATE.match(value):
        errors.append(f"{field} '{value}' is not a YYYY-MM-DD date")
        return None
    try:
        return date.fromisoformat(value)
    except ValueError:
        errors.append(f"{field} '{value}' is not a real date")
        return None
