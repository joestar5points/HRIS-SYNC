import json

import pytest
from sqlalchemy import func, select

from hris_sync.errors import LoadError
from hris_sync.loader import load_input, record_hash
from hris_sync.models import HrisWorker


@pytest.fixture
def write_input(tmp_path):
    def _write(records, name="input.json"):
        path = tmp_path / name
        path.write_text(json.dumps(records), encoding="utf-8")
        return path

    return _write


def rows(session):
    return session.scalar(select(func.count()).select_from(HrisWorker))


def worker(session, employee_id):
    return session.scalars(select(HrisWorker).where(HrisWorker.employee_id == employee_id)).one()


def test_load_returns_records_unchanged_and_stores_them(session, write_input, valid_record):
    records = load_input(session, write_input([valid_record]))

    assert records == [valid_record]
    assert worker(session, "E1042").department == "Quality"


def test_reloading_the_same_file_does_not_duplicate_rows(session, write_input, valid_record):
    path = write_input([valid_record])
    load_input(session, path)
    load_input(session, path)
    assert rows(session) == 1


def test_reload_updates_changed_fields(session, write_input, valid_record):
    load_input(session, write_input([valid_record]))
    load_input(session, write_input([{**valid_record, "department": "Finance"}], "second.json"))

    assert rows(session) == 1
    assert worker(session, "E1042").department == "Finance"


def test_reload_with_null_email_keeps_written_back_email(session, write_input, valid_record):
    load_input(session, write_input([valid_record]))
    worker(session, "E1042").email = "asha.patel@contoso.com"  # as if written back
    session.commit()

    load_input(session, write_input([valid_record], "again.json"))  # export still says null

    assert worker(session, "E1042").email == "asha.patel@contoso.com"


def test_reload_with_new_email_replaces_it(session, write_input, valid_record):
    load_input(session, write_input([{**valid_record, "email": "old@contoso.com"}]))
    load_input(session, write_input([{**valid_record, "email": "new@contoso.com"}], "b.json"))
    assert worker(session, "E1042").email == "new@contoso.com"


@pytest.mark.parametrize("missing", [None, "", "   "])
def test_record_without_employee_id_is_stored_not_rejected(session, write_input, valid_record, missing):
    records = load_input(session, write_input([{**valid_record, "employeeId": missing}]))

    assert len(records) == 1  # still in the work list, so validation can flag it
    assert rows(session) == 1


def test_record_without_employee_id_is_not_duplicated_on_reload(session, write_input, valid_record):
    path = write_input([{**valid_record, "employeeId": None}])
    load_input(session, path)
    load_input(session, path)
    assert rows(session) == 1


def test_invalid_records_are_loaded_as_is(session, write_input, valid_record):
    bad = {**valid_record, "status": "Retired", "startDate": "30/10/2026"}
    load_input(session, write_input([bad]))

    stored = worker(session, "E1042")
    assert stored.status == "Retired"
    assert stored.start_date == "30/10/2026"
    assert stored.to_record() == bad


def test_non_object_entries_are_returned_but_not_stored(session, write_input, valid_record):
    records = load_input(session, write_input([valid_record, "junk", 42]))

    assert records == [valid_record, "junk", 42]
    assert rows(session) == 1


def test_missing_file_raises_load_error(session, tmp_path):
    with pytest.raises(LoadError, match="not found"):
        load_input(session, tmp_path / "nope.json")


def test_invalid_json_raises_load_error_and_loads_nothing(session, tmp_path):
    path = tmp_path / "broken.json"
    path.write_text('[{"employeeId": "E1042",', encoding="utf-8")

    with pytest.raises(LoadError, match="not valid JSON"):
        load_input(session, path)
    assert rows(session) == 0


def test_file_that_is_not_a_list_raises_load_error(session, write_input, valid_record):
    with pytest.raises(LoadError, match="JSON array"):
        load_input(session, write_input(valid_record))


def test_record_hash_ignores_key_order():
    assert record_hash({"a": 1, "b": 2}) == record_hash({"b": 2, "a": 1})


def test_record_hash_changes_when_any_value_changes(valid_record):
    assert record_hash(valid_record) != record_hash({**valid_record, "department": "Finance"})
