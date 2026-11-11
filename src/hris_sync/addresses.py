"""Address helpers.

choose_upn    picks a unique sign-in name for a new user (our naming rule).
primary_smtp  reads the confirmed mailbox address back from a Graph user.

We never choose the mailbox address: Exchange sets it when the mailbox is
provisioned, and primary_smtp reads what Exchange decided.
"""

import re
import unicodedata
from typing import Callable, Optional

DEFAULT_DOMAIN = "contoso.com"


def _clean(name: str) -> str:
    """Internal Helper
    Cleanses First Name + Last Name. drop accents, keep a-z, 0-9 and hyphens.
    Example 'Zoë O'Brien-Smith' -> 'zoeobrien-smith'"""
    ascii_name = unicodedata.normalize("NFKD", name).encode("ascii", "ignore").decode()
    return re.sub(r"[^a-z0-9-]", "", ascii_name.lower())


def choose_upn(
    first_name: str,
    last_name: str,
    is_taken: Callable[[str], bool],
    domain: str = DEFAULT_DOMAIN,
) -> str:
    """first.last@domain, or first.last2@domain, first.last3@domain... if already taken."""
    first, last = _clean(first_name), _clean(last_name)
    if not first or not last:
        raise ValueError(f"cannot build a UPN from {first_name!r} {last_name!r}")

    alias = f"{first}.{last}"
    upn = f"{alias}@{domain}"
    number = 2
    while is_taken(upn):
        upn = f"{alias}{number}@{domain}"
        number += 1
    return upn


def primary_smtp(user: dict) -> Optional[str]:
    """The primary SMTP address once the mailbox is ready, otherwise None.

    Ready means Exchange reports provisioning as "Success" AND proxyAddresses has
    an entry starting with uppercase "SMTP:" (lowercase "smtp:" entries are aliases).
    """
    exchange_ready = any(
        plan.get("service") == "exchange" and plan.get("provisioningStatus") == "Success"
        for plan in user.get("provisionedPlans", [])
    )
    if not exchange_ready:
        return None

    for address in user.get("proxyAddresses", []):
        if address.startswith("SMTP:"):
            return address[len("SMTP:"):]
    return None
