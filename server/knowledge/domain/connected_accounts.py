"""Caller-owned provider registrations, without credentials in their representation."""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from datetime import datetime

KINDS = frozenset({"gdrive", "notion", "linear"})
CREDENTIAL_PURPOSE = "connected_accounts.credential"
EMAIL_PURPOSE = "connected_accounts.email"
PKCE_PURPOSE = "connected_accounts.pkce"


@dataclass(frozen=True, repr=False)
class ConnectedAccount:
    org: str
    subject: str
    kind: str
    # A connection's identity survives refresh but changes on replacement or stop.
    connection_id: str
    account_id: str = ""
    workspace_id: str = ""
    account_email_enc: str = field(default="", repr=False)
    credential_enc: str = field(default="", repr=False)
    scopes: tuple[str, ...] = ()
    active: bool = False
    # An outstanding rotation fences another presenter without holding a lease over HTTP.
    refresh_id: str = ""
    connected_at: datetime | None = None
    disconnected_at: datetime | None = None
    version: int = 0

    def __repr__(self) -> str:
        return "ConnectedAccount(<redacted>)"


def source_matches(
    account: ConnectedAccount, *, org: str, owner: str, kind: str, connection_id: str
) -> bool:
    """An exact active registration, never an email or an audit attribution."""
    return bool(
        connection_id
        and org
        and owner
        and kind in KINDS
        and account.active
        and account.credential_enc
        and account.org == org
        and account.subject == owner
        and account.kind == kind
        and account.connection_id == connection_id
    )


def accepts_callback(account: ConnectedAccount | None, expected_connection: str) -> bool:
    """A stop of even an absent account invalidates a previously started callback."""
    return (account.connection_id if account is not None else "") == expected_connection


def stopped(account: ConnectedAccount, *, connection_id: str, at: datetime) -> ConnectedAccount:
    if not connection_id or connection_id == account.connection_id:
        raise ValueError("connection_identity_required")
    return replace(
        account,
        active=False,
        connection_id=connection_id,
        credential_enc="",
        account_email_enc="",
        account_id="",
        workspace_id="",
        scopes=(),
        refresh_id="",
        disconnected_at=at,
    )


def refreshing(account: ConnectedAccount, *, refresh_id: str) -> ConnectedAccount:
    if (
        not account.active
        or not account.credential_enc
        or not account.connection_id
        or account.refresh_id
        or not refresh_id
    ):
        raise ValueError("connection_unavailable")
    return replace(account, refresh_id=refresh_id)


def rotated(
    account: ConnectedAccount, *, expected_connection: str, refresh_id: str, credential_enc: str
) -> ConnectedAccount:
    if (
        not account.active
        or not account.credential_enc
        or not expected_connection
        or account.connection_id != expected_connection
        or not refresh_id
        or account.refresh_id != refresh_id
        or not credential_enc
    ):
        raise ValueError("connection_changed")
    return replace(account, credential_enc=credential_enc, refresh_id="")
