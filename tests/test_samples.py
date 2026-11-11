"""The demo sample packs in samples/, run in order against the seeded tenant.

These are the outcomes samples/README.md promises. If this test fails, the
README and the demo are wrong too.
"""

import json
from datetime import date
from pathlib import Path

import pytest
from sqlalchemy import select

from hris_sync.db import init_db, make_engine, make_session_factory
from hris_sync.main import sync
from hris_sync.mocks.fixtures import seed
from hris_sync.state import AuditLog

SAMPLES = Path(__file__).parents[1] / "samples"
NO_SLEEP = lambda seconds: None  # noqa: E731

# (file or None for a run with no --input, --today, exit code, {(employeeId, phase): (proposed action, result)})
PACKS = [
    ("01-joiners.json", date(2026, 11, 6), 2, {
        ("E1042", "identity"): ("Create", "Applied"),
        ("E1043", "identity"): ("Create", "Applied"),
        ("E1045", "identity"): ("Create", "Applied"),
        ("E1050", "identity"): ("Create", "Applied"),
        ("E1051", "identity"): ("Update", "Applied"),
        ("E9001", "identity"): ("NoChange", "NoAction"),
        ("E1042", "mailbox"): ("NoChange", "Pending"),
        ("E1043", "writeback"): ("Update", "Applied"),
        ("E1045", "writeback"): ("ManualReview", "Conflict"),
        ("E1050", "writeback"): ("Update", "Applied"),
        ("E1051", "writeback"): ("Update", "Applied"),
        ("E9001", "writeback"): ("Update", "Applied"),
    }),
    ("02-movers-and-leavers.json", date(2026, 11, 6), 2, {
        ("E2001", "identity"): ("Update", "Applied"),
        ("E2002", "identity"): ("Update", "Applied"),
        ("E3001", "identity"): ("Disable", "Applied"),
        ("E3002", "identity"): ("Disable", "Applied"),
        ("E3003", "identity"): ("ManualReview", "NoAction"),
        ("E3006", "identity"): ("NoChange", "NoAction"),
        ("E1042", "writeback"): ("Update", "Applied"),
    }),
    ("03-bad-data.json", date(2026, 11, 8), 2, {
        ("E4001", "identity"): ("ManualReview", "NoAction"),
        ("E8001", "identity"): ("ManualReview", "NoAction"),
        (None, "identity"): ("ManualReview", "NoAction"),
        ("E7002", "identity"): ("ManualReview", "NoAction"),
        ("E7003", "identity"): ("ManualReview", "NoAction"),
    }),
    ("04-corrections.json", date(2026, 11, 17), 0, {
        ("E7003", "identity"): ("Create", "Applied"),
        ("E1050", "identity"): ("NoChange", "NoAction"),
        ("E1043", "identity"): ("Disable", "Applied"),
        ("E2001", "identity"): ("Update", "Applied"),
        ("E7003", "writeback"): ("Update", "Applied"),
    }),
    (None, date(2026, 11, 28), 0, {  # no --input: HRIS sent nothing
        ("E1050", "scheduled"): ("Update", "Applied"),
        ("E3006", "scheduled"): ("Disable", "Applied"),
    }),
]


@pytest.fixture(scope="module")
def results():
    engine = make_engine("sqlite://")
    init_db(engine)
    out = {}
    with make_session_factory(engine)() as session:
        seed(session)
        for name, today, _, _ in PACKS:
            summary = sync(session, SAMPLES / name if name else None, apply=True, today=today, sleep=NO_SLEEP)
            rows = session.scalars(select(AuditLog).where(AuditLog.correlation_id == summary["run_id"])).all()
            out[name] = (summary, {(r.employee_id, r.phase): (r.proposed_action.value, r.result) for r in rows})
    engine.dispose()
    return out


@pytest.mark.parametrize("name, today, exit_code, expected", PACKS, ids=[p[0] or "no-input" for p in PACKS])
def test_sample_pack_does_what_the_readme_says(results, name, today, exit_code, expected):
    summary, actual = results[name]
    assert actual == expected
    assert (2 if summary["needs_attention"] else 0) == exit_code


def test_every_sample_pack_is_small():
    for name, *_ in PACKS:
        if name is None:
            continue
        assert len(json.loads((SAMPLES / name).read_text())) <= 6, name
