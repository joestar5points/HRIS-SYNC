"""Test and demo fixtures: the state of the mocked tenant before the first run.

Two pieces work together:
  tests/data/all-cases.json   an HRIS export with every scenario (one or more records each)
  this file            what already exists in Entra/Exchange, plus per-joiner
                       mailbox behaviour, and the outcome each record should get

JSON can't hold comments, so every record in tests/data/all-cases.json is explained in
EXPECTED below. All people are fictional; all addresses use contoso.com.
"""

from datetime import date

from sqlalchemy.orm import Session

from hris_sync.mocks.models import EntraUser, ExoMailbox
from hris_sync.state import Stage, SyncState

# Every date in the fixtures is relative to this. Pass it as "today" in tests
FIXTURE_TODAY = date(2026, 11, 6)


# Base State (see Samples/README.md for explanations)

ENTRA_USERS = [
    dict(employee_id="E1000", first="Sam", last="Okafor", department="Executive", manager=None),
    dict(employee_id="E1008", first="Morgan", last="Hale", department="Quality", manager="E1000"),
    dict(employee_id="E5001", first="Asha", last="Patel", department="Finance", manager="E1000"),
    dict(employee_id="E1051", first="Ivo", last="Novak", department="Quality", manager="E1008",
         enabled=False, licensed=True),
    dict(employee_id="E2001", first="Jo", last="Mills", department="Finance", manager="E1008"),
    dict(employee_id="E2002", first="Kai", last="Lund", department="Finance", manager="E1000"),
    dict(employee_id="E3001", first="Lena", last="Ross", department="Quality", manager="E1008"),
    dict(employee_id="E3002", first="Max", last="Ortiz", department="Finance", manager="E1008"),
    dict(employee_id="E3003", first="Nia", last="Shaw", department="IT", manager="E1000",
         privileged=True),
    dict(employee_id="E3006", first="Yara", last="Ali", department="Finance", manager="E1008"),
    dict(employee_id="E3004", first="Omar", last="Haddad", department="Quality", manager="E1008",
         enabled=False),
    dict(employee_id="E4001", first="Quinn", last="Doyle", department="IT", manager="E1008",
         upn="quinn.doyle@contoso.com"),
    dict(employee_id="E4001", first="Quinn", last="Doyle", department="IT", manager="E1008",
         upn="quinn.doyle2@contoso.com"),
    dict(employee_id="E9001", first="Theo", last="Grant", department="Operations", manager="E1008",
         licensed=True),
]


#Mailbox behaviour for joiners created during the run

DEFAULT_MAILBOX_CHECKS = 0

MAILBOX_SETTINGS = {
    # Not ready on the first check: the first --apply run leaves it pending with NO
    # HRIS write-back; the next run finds it ready and writes back.
    "E1042": {"checks_until_ready": 1},
    # Ready straight away, and Exchange's primary address differs from the UPN
    # (simulates an admin change). The write-back must use b.carter@, the address
    # Exchange reports, not ben.carter@, the UPN we created.
    "E1043": {"checks_until_ready": 0, "primary_smtp_override": "b.carter@contoso.com"},
    # Ready straight away; HRIS already holds dan.wright@ -> write-back NoChange.
    "E1044": {"checks_until_ready": 0},
    # Ready straight away as eve.green@; HRIS holds e.green@ -> ManualReview conflict.
    "E1045": {"checks_until_ready": 0},
}


#What each input record should produce (Used for Tests)
#Identity pass outcome, then what should happen in the mailbox / write-back pass.

EXPECTED = {
    "E1008": ("NoChange", "already matches Entra"),
    "E1042": ("Create", "UPN asha.patel2@ (clash with E5001); mailbox not ready -> no write-back yet"),
    "E1043": ("Create", "mailbox ready; write back b.carter@ (override), not the UPN"),
    "E1044": ("Create", "mailbox ready; HRIS already has the same address -> write-back NoChange"),
    "E1045": ("Create", "mailbox ready; HRIS has a different address -> write-back ManualReview"),
    "E1046": ("Create", "manager E9999 isn't in Entra -> created without a manager, reason says so"),
    "E1050": ("Create", "start 2026-11-28 is in the future -> created disabled, enable scheduled"),
    "E1051": ("Update", "staged joiner's start date has arrived -> accountEnabled=true"),
    "E2001": ("Update", "department Finance -> Quality, nothing else"),
    "E2002": ("Update", "manager E1000 -> E1008, nothing else"),
    "E3001": ("Disable", "status Leaver"),
    "E3002": ("Disable", "endDate 2026-10-29 has passed, even though status says Active"),
    "E3003": ("ManualReview", "privileged account, will not auto-disable"),
    "E3004": ("NoChange", "already disabled"),
    "E3006": ("NoChange", "endDate 2026-11-27 is in the future -> disable scheduled"),
    "E3005": ("NoChange", "leaver with no Entra identity, nothing to disable"),
    "E4001": ("ManualReview", "two Entra identities share this employeeId"),
    "E8001": ("ManualReview", "appears twice in one file with different data; both rows"),
    "E9001": ("NoChange", "adopted after a crash: SyncState created, no second account"),
    None: ("ManualReview", "employeeId is missing"),
    "E7002": ("ManualReview", "status 'Retired' is not recognised"),
    "E7003": ("ManualReview", "startDate '30/10/2026' is not YYYY-MM-DD"),
    "E7004": ("ManualReview", "endDate is before startDate"),
}

def seed(session: Session) -> None:
    """Load ENTRA_USERS (and their mailboxes) into an empty mock tenant."""
    by_employee_id: dict[str, EntraUser] = {}
    users = []
    for spec in ENTRA_USERS:
        local = f"{spec['first']}.{spec['last']}".lower()
        user = EntraUser(
            employee_id=spec["employee_id"],
            user_principal_name=spec.get("upn", f"{local}@contoso.com"),
            mail_nickname=local,
            display_name=f"{spec['first']} {spec['last']}",
            given_name=spec["first"],
            surname=spec["last"],
            department=spec["department"],
            account_enabled=spec.get("enabled", True),
            usage_location="GB",
            is_privileged=spec.get("privileged", False),
        )
        users.append((spec, user))
        by_employee_id.setdefault(spec["employee_id"], user)
        session.add(user)
    session.flush()  # assigns object ids

    for spec, user in users:
        if spec["manager"]:
            user.manager_object_id = by_employee_id[spec["manager"]].object_id
        if spec["employee_id"] == "E1051":
            session.add(
                SyncState(
                    employee_id="E1051",
                    stage=Stage.MAILBOX_PENDING,
                    object_id=user.object_id,
                    user_principal_name=user.user_principal_name,
                )
            )
        if spec.get("licensed"):
            session.add(
                ExoMailbox(
                    object_id=user.object_id,
                    provisioning_status="PendingInput",
                    proxy_addresses=[],
                    checks_until_ready=0,  # ready on the first check
                )
            )
    session.commit()
