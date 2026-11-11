import pytest

from hris_sync.db import init_db, make_engine, make_session_factory


@pytest.fixture
def engine():
    engine = make_engine("sqlite://")  # fresh in-memory database per test
    init_db(engine)
    yield engine
    engine.dispose()


@pytest.fixture
def session(engine):
    with make_session_factory(engine)() as session:
        yield session


@pytest.fixture
def valid_record():
    return {
        "employeeId": "E1042",
        "status": "Active",
        "firstName": "Asha",
        "lastName": "Patel",
        "department": "Quality",
        "managerEmployeeId": "E1008",
        "startDate": "2026-10-30",
        "endDate": None,
        "email": None,
    }
