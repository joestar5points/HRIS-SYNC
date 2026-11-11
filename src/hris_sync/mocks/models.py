"""Models (Tables) backing the Abstracted Entra ID and Exchange Online services.

These stand in for systems we don't own. Do not keep in production
"""

import uuid
from datetime import datetime
from typing import Optional

from sqlalchemy import JSON, Boolean, DateTime, ForeignKey, Integer, String
from sqlalchemy.orm import Mapped, mapped_column

from hris_sync.db import Base, utcnow


def new_object_id() -> str:
    return str(uuid.uuid4())


class EntraUser(Base):
    """A user object as Entra ID would hold it.

    employee_id is intentionally NOT unique: two users sharing one employeeId is
    a real failure state (the ambiguous-match case) and the mock must be able to
    hold it.
    """

    __tablename__ = "entra_users"

    object_id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_object_id)
    employee_id: Mapped[Optional[str]] = mapped_column(String(16), index=True)
    user_principal_name: Mapped[str] = mapped_column(String(255), unique=True)
    mail_nickname: Mapped[str] = mapped_column(String(64))
    display_name: Mapped[str] = mapped_column(String(256))
    given_name: Mapped[Optional[str]] = mapped_column(String(64))
    surname: Mapped[Optional[str]] = mapped_column(String(64))
    department: Mapped[Optional[str]] = mapped_column(String(64))
    # Graph models manager as a reference to another user's object id.
    manager_object_id: Mapped[Optional[str]] = mapped_column(String(36))
    account_enabled: Mapped[bool] = mapped_column(Boolean, default=False)
    usage_location: Mapped[Optional[str]] = mapped_column(String(2))
    # Holds an admin role; the sync must never disable these automatically.
    is_privileged: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, onupdate=utcnow
    )

    def __repr__(self) -> str:
        return f"<EntraUser {self.object_id} employeeId={self.employee_id!r}>"


class ExoMailbox(Base):
    """Exchange Online mailbox provisioning state for one user.

    In the real system nobody "requests" a mailbox address. Assigning an Exchange
    licence triggers provisioning, and Entra/Exchange "proxy calculation" sets the
    addresses: for a cloud-only user with no ``mail`` set, the primary SMTP comes
    from the UPN, and a MOERA (``mailNickname@<initial domain>``) is added as a
    secondary. The mock follows the same rule, so the row is created by
    assign_license with no address at all; proxy_addresses is filled in when the
    mailbox becomes ready.

    The mock client exposes this the way Graph does: proxyAddresses with the
    primary address prefixed "SMTP:", and a provisionedPlans entry for the
    "exchange" service whose provisioningStatus becomes "Success".
    """

    __tablename__ = "exo_mailboxes"

    object_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("entra_users.object_id"), primary_key=True
    )
    # "PendingInput" until ready, then "Success". "Error" simulates a failed provisioning.
    provisioning_status: Mapped[str] = mapped_column(String(32), default="PendingInput")
    # Empty until ready, then e.g. ["SMTP:asha.patel@...", "smtp:asha.patel@<tenant>.onmicrosoft.example"].
    proxy_addresses: Mapped[list] = mapped_column(JSON, default=list)
    # Test/fixture hook only: simulates an Exchange admin (or tenant config) setting a
    # primary address other than the UPN. Lets tests prove the service writes back the
    # SMTP: value from proxyAddresses rather than assuming it equals the UPN.
    primary_smtp_override: Mapped[Optional[str]] = mapped_column(String(255))
    # How many more readiness checks return "not ready" before the mailbox appears.
    checks_until_ready: Mapped[int] = mapped_column(Integer, default=0)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

    def __repr__(self) -> str:
        return f"<ExoMailbox {self.object_id} status={self.provisioning_status}>"
