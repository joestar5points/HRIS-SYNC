import pytest

from hris_sync.validation import validate


def test_valid_record_has_no_errors(valid_record):
    assert validate(valid_record) == []


def test_valid_leaver_with_end_date(valid_record):
    valid_record.update(status="Leaver", endDate="2026-12-28")
    assert validate(valid_record) == []


@pytest.mark.parametrize("value", [None, "", "   "])
def test_missing_or_blank_employee_id_is_rejected(valid_record, value):
    valid_record["employeeId"] = value
    assert "employeeId is required" in validate(valid_record)


def test_absent_employee_id_key_is_rejected(valid_record):
    del valid_record["employeeId"]
    assert "employeeId is required" in validate(valid_record)


def test_non_string_employee_id_is_rejected(valid_record):
    valid_record["employeeId"] = 1042
    assert "employeeId is required" in validate(valid_record)


def test_employee_id_with_whitespace_is_rejected(valid_record):
    valid_record["employeeId"] = " E1042"
    assert "employeeId has leading or trailing whitespace" in validate(valid_record)


def test_employee_id_longer_than_entra_limit_is_rejected(valid_record):
    valid_record["employeeId"] = "E" * 17
    assert "employeeId is longer than 16 characters" in validate(valid_record)


@pytest.mark.parametrize("status", ["active", "Retired", "ACTIVE"])
def test_unrecognised_status_is_rejected(valid_record, status):
    valid_record["status"] = status
    assert f"status '{status}' is not recognised" in validate(valid_record)


@pytest.mark.parametrize("value", ["30/10/2026", "2026-11-2", "20261102", "tomorrow", 20261102])
def test_malformed_start_date_is_rejected(valid_record, value):
    valid_record["startDate"] = value
    assert f"startDate '{value}' is not a YYYY-MM-DD date" in validate(valid_record)


def test_impossible_date_is_rejected(valid_record):
    valid_record["startDate"] = "2026-13-45"
    assert "startDate '2026-13-45' is not a real date" in validate(valid_record)


def test_missing_start_date_is_rejected(valid_record):
    valid_record["startDate"] = None
    assert "startDate is required" in validate(valid_record)


def test_end_date_before_start_date_is_rejected(valid_record):
    valid_record["endDate"] = "2026-01-01"
    assert "endDate is before startDate" in validate(valid_record)


def test_all_errors_are_reported_together(valid_record):
    valid_record.update(employeeId=None, status="Unknown", startDate="bad")
    assert len(validate(valid_record)) == 3


@pytest.mark.parametrize("record", [None, [], "E1042"])
def test_non_object_record_is_rejected(record):
    assert validate(record) == ["record is not a JSON object"]
