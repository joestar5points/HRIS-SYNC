"""Abstraction of Microsoft Entra ID

Takes and returns Graph like objectss so the service code would look the same against the real Graph API.

Each call commits straight away

Real Graph equivalents:
    find_by_employee_id -> GET  /users?$filter=employeeId eq '{id}'
    upn_exists ->  GET  /users?$filter=userPrincipalName eq '{upn}'
    create_user -> POST /users
    update_user -> PATCH /users/{id}
    set_manager -> PUT  /users/{id}/manager/$ref
    assign_license -> POST /users/{id}/assignLicense
    get_user -> GET  /users/{id}?$select=mail,proxyAddresses,provisionedPlans
"""

import random
from typing import Optional

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from hris_sync.errors import ConflictError, PermanentError
from hris_sync.mocks.faults import FaultInjection, injects_faults
from hris_sync.mocks.models import EntraUser, ExoMailbox

# Domains the mock tenant has verified. A UPN must use one of these.
VERIFIED_DOMAINS = frozenset({"contoso.com"})

# The tenant's initial domain, used for the MOERA secondary address.
INITIAL_DOMAIN = "contoso.onmicrosoft.com"

# Attributes update_user may change. Manager has its own call (set_manager), as in Graph.
UPDATABLE_FIELDS = {"department": "department", "accountEnabled": "account_enabled"}

# Required by Graph on POST /users
REQUIRED_CREATE_FIELDS = ("accountEnabled", "displayName", "mailNickname", "userPrincipalName")


class MockEntraClient(FaultInjection):
    def __init__(
        self,
        session: Session,
        mailbox_checks_until_ready: int = 1,
        mailbox_settings: Optional[dict[str, dict]] = None,
        failure_rate: float = 0.0,
        lost_response_rate: float = 0.0,
        rng: Optional[random.Random] = None,
        fault_methods: Optional[set[str]] = None,
    ):
        self.session = session
        # Random 429/503s
        self._init_faults(failure_rate, lost_response_rate, rng, fault_methods)
        # How many readiness checks a new mailbox reports "not ready" before it appears.
        self.mailbox_checks_until_ready = mailbox_checks_until_ready
        # Per-employee overrides for test scenarios, keyed by employeeId
        self.mailbox_settings = mailbox_settings or {}

    #GETs

    @injects_faults
    def find_by_employee_id(self, employee_id: str) -> list[dict]:
        """Returns Every user with this employeeId."""
        users = self.session.scalars(
            select(EntraUser).where(EntraUser.employee_id == employee_id)
        ).all()
        return [_to_graph(user) for user in users]

    @injects_faults
    def upn_exists(self, user_principal_name: str) -> bool:
        """UPNs are unique per tenant, compared case-insensitively."""
        return self._find_by_upn(user_principal_name) is not None

    #POSTs/PATCHEs

    @injects_faults
    def create_user(self, body: dict) -> dict:
        """Create a user from a Graph like object. Returns the created user, including its "id"."""
        missing = [f for f in REQUIRED_CREATE_FIELDS if body.get(f) in (None, "")]
        if missing:
            raise PermanentError(400, f"missing required properties: {', '.join(missing)}")

        upn = body["userPrincipalName"]
        domain = upn.rpartition("@")[2].lower()
        if "@" not in upn or domain not in VERIFIED_DOMAINS:
            raise PermanentError(400, f"userPrincipalName domain '{domain}' is not verified")
        if self._find_by_upn(upn) is not None:
            raise ConflictError(400, f"userPrincipalName '{upn}' already exists")

        employee_id = body.get("employeeId")
        if employee_id is not None and len(employee_id) > 16:
            raise PermanentError(400, "employeeId cannot be longer than 16 characters")
        if len(body["mailNickname"]) > 64:
            raise PermanentError(400, "mailNickname cannot be longer than 64 characters")

        user = EntraUser(
            employee_id=employee_id,
            user_principal_name=upn,
            mail_nickname=body["mailNickname"],
            display_name=body["displayName"],
            given_name=body.get("givenName"),
            surname=body.get("surname"),
            department=body.get("department"),
            account_enabled=bool(body["accountEnabled"]),
            usage_location=body.get("usageLocation"),
        )
        self.session.add(user)
        self.session.commit()
        return _to_graph(user)

    @injects_faults
    def update_user(self, object_id: str, changes: dict) -> None:
        """PATCH only the given attributes"""
        unknown = set(changes) - set(UPDATABLE_FIELDS)
        if unknown:
            raise PermanentError(400, f"cannot update {', '.join(sorted(unknown))} with update_user")
        user = self._get(object_id)
        for graph_key, value in changes.items():
            setattr(user, UPDATABLE_FIELDS[graph_key], value)
        self.session.commit()

    @injects_faults
    def get_user(self, object_id: str) -> dict:
        """The user including mailbox fields, the way Graph returns them with $select.

        Each call while the mailbox is pending counts as one readiness check. After
        ``checks_until_ready`` "not ready" answers, the mailbox becomes ready and
        proxyAddresses is filled in the way Entra proxy calculation does it:
        primary SMTP from the UPN (or the fixture override), plus the MOERA
        (mailNickname@initial domain) as a secondary.
        """
        user = self._get(object_id)
        mailbox = self.session.get(ExoMailbox, object_id)

        if mailbox is not None and mailbox.provisioning_status == "PendingInput":
            if mailbox.checks_until_ready > 0:
                mailbox.checks_until_ready -= 1
            else:
                primary = mailbox.primary_smtp_override or user.user_principal_name
                mailbox.proxy_addresses = [f"SMTP:{primary}", f"smtp:{self._moera(user)}"]
                mailbox.provisioning_status = "Success"
            self.session.commit()

        result = _to_graph(user)
        if mailbox is None:
            result.update(mail=None, proxyAddresses=[], provisionedPlans=[])
        else:
            primary = next((a[5:] for a in mailbox.proxy_addresses if a.startswith("SMTP:")), None)
            result.update(
                mail=primary,
                proxyAddresses=list(mailbox.proxy_addresses),
                provisionedPlans=[
                    {"service": "exchange", "provisioningStatus": mailbox.provisioning_status}
                ],
            )
        return result

    @injects_faults
    def set_manager(self, object_id: str, manager_object_id: str) -> None:
        user = self._get(object_id)
        if object_id == manager_object_id:
            raise PermanentError(400, "a user cannot be their own manager")
        self._get(manager_object_id)  # 404 if the manager doesn't exist
        user.manager_object_id = manager_object_id
        self.session.commit()

    @injects_faults
    def assign_license(self, object_id: str) -> None:
        """Assign the Exchange Online licence, which starts mailbox provisioning."""
        user = self._get(object_id)
        if not user.usage_location:
            raise PermanentError(400, "usageLocation must be set before a licence can be assigned")

        if self.session.get(ExoMailbox, object_id) is None:
            settings = self.mailbox_settings.get(user.employee_id, {})
            self.session.add(
                ExoMailbox(
                    object_id=object_id,
                    provisioning_status="PendingInput",
                    proxy_addresses=[],
                    checks_until_ready=settings.get(
                        "checks_until_ready", self.mailbox_checks_until_ready
                    ),
                    primary_smtp_override=settings.get("primary_smtp_override"),
                )
            )
            self.session.commit()

    #Interal Only Helper Functions

    def _get(self, object_id: str) -> EntraUser:
        user = self.session.get(EntraUser, object_id)
        if user is None:
            raise PermanentError(404, f"user '{object_id}' not found")
        return user

    def _moera(self, user: EntraUser) -> str:
        """mailNickname@initial domain; if another mailbox already has it, Entra adds 4 random digits."""
        candidate = f"{user.mail_nickname}@{INITIAL_DOMAIN}"
        taken = {
            address.lower()
            for (addresses,) in self.session.execute(select(ExoMailbox.proxy_addresses))
            for address in (addresses or [])
        }
        while f"smtp:{candidate}".lower() in taken:
            candidate = f"{user.mail_nickname}{random.randint(1000, 9999)}@{INITIAL_DOMAIN}"
        return candidate

    def _find_by_upn(self, user_principal_name: str) -> Optional[EntraUser]:
        return self.session.scalars(
            select(EntraUser).where(
                func.lower(EntraUser.user_principal_name) == user_principal_name.lower()
            )
        ).first()


def _to_graph(user: EntraUser) -> dict:
    return {
        "id": user.object_id,
        "employeeId": user.employee_id,
        "userPrincipalName": user.user_principal_name,
        "mailNickname": user.mail_nickname,
        "displayName": user.display_name,
        "givenName": user.given_name,
        "surname": user.surname,
        "department": user.department,
        "manager": user.manager_object_id,
        "accountEnabled": user.account_enabled,
        "usageLocation": user.usage_location,
        # Not a real Graph property (Graph exposes admin roles via a separate call);
        "isPrivileged": user.is_privileged,
    }
