"""One-time links that give an SSO account a password, for a deployment that
turns single sign-on off: an account JIT made under SSO has none, and its
owner could not sign in again without one.

An operator prints a link (`loom sso password-link`) and hands it to the
person. It sets the password of that email's SSO accounts once, within a day,
and only while SSO is off. The account is also linked to the email
(sso_links), so turning SSO back on reaches the same account.
"""

from __future__ import annotations

import html
import logging
import secrets
from contextlib import AsyncExitStack
from datetime import UTC, datetime
from typing import Any

from fastapi import APIRouter, Request
from fastapi.responses import Response

from server.auth import oauth, sso_links
from server.auth.grant_claims import account_change
from server.auth.oauth_store import token_id
from server.auth.passwords import PASSWORD_MAX_LENGTH, PASSWORD_MIN_LENGTH
from server.core.ch import get_client
from server.identity.store import namespaces
from server.observability import audit
from server.settings import settings

log = logging.getLogger(__name__)
router = APIRouter()


# The SSO accounts (no password) naming an email, active, in one org.
_SSO_ACCOUNTS = """
    SELECT org, namespace FROM loom.namespaces FINAL
    WHERE contact_email = {email:String} AND active = 1 AND password_hash = ''
      AND ({org:String} = '' OR org = {org:String})
    ORDER BY org, namespace
"""


async def sso_accounts(email: str, org: str = "") -> list[dict[str, Any]]:
    email = email.strip().lower()
    if not email:  # agents' rows name no email: "" would match each of them
        return []
    return await get_client().query_all(_SSO_ACCOUNTS, params={"email": email, "org": org})


async def make_link(email: str, accounts: list[dict[str, Any]], *, by: str) -> str:
    """A code that sets the password of `accounts` (one org's), once; `by`
    (who made it) is audited now and again when it is used."""
    code, org = secrets.token_urlsafe(32), accounts[0]["org"]
    namespaces = [a["namespace"] for a in accounts]
    await oauth.store.save_pending(
        token_id(code),
        {
            "kind": "set_password",
            "email": email.strip().lower(),
            "org": org,
            "namespaces": namespaces,
            "by": by,
        },
        ttl=settings.password_link_ttl_seconds,
    )
    await audit.emit(
        "auth.password_link_made",
        org=org,
        namespace="-",
        actor=by,
        subject={"email": email.strip().lower(), "namespaces": namespaces},
    )
    return code


def _form(code: str, error: str = "", status_code: int = 200) -> Response:
    message = f'<p class="error">{html.escape(error)}</p>' if error else ""
    return oauth.html_page(
        "Set your Loom password",
        f"""<h2>Set your Loom password</h2>
<p>Single sign-on is off here, so you sign in with a password from now on.</p>{message}
<form method="post" action="/oauth/set-password">
<input type="hidden" name="code" value="{html.escape(code)}">
<input name="password" type="password" placeholder="new password"
       autocomplete="new-password" minlength="{PASSWORD_MIN_LENGTH}" required>
<input name="repeat" type="password" placeholder="the same again"
       autocomplete="new-password" required>
<button type="submit">Set password</button>
</form>""",
        status_code,
    )


def _closed() -> Response:
    return oauth.html_page(
        "Set your Loom password",
        "<h2>This link does not work here</h2>"
        "<p>It has expired, was used, or single sign-on is on.</p>",
        400,
    )


async def _held(code: str) -> dict[str, Any] | None:
    if settings.oauth_login_provider == "oidc":
        return None  # with SSO on, a password would bypass the provider
    held = await oauth.store.pending(token_id(code))
    return held if held is not None and held.get("kind") == "set_password" else None


@router.get("/oauth/set-password")
async def set_password_page(code: str = "") -> Response:
    return _form(code) if await _held(code) else _closed()


@router.post("/oauth/set-password")
async def set_password(request: Request) -> Response:
    from server.auth.passwords import hash_password_off_loop

    f = await oauth.form_body(request)
    code, password = f.get("code", ""), f.get("password", "")
    held = await _held(code)
    if held is None:
        return _closed()
    if not PASSWORD_MIN_LENGTH <= len(password) <= PASSWORD_MAX_LENGTH:
        return _form(code, f"Use {PASSWORD_MIN_LENGTH} to {PASSWORD_MAX_LENGTH} characters.", 400)
    if password != f.get("repeat", ""):
        return _form(code, "The two passwords differ.", 400)
    password_hash = await hash_password_off_loop(password)  # the slow step, before any claim
    email, org = held["email"], held["org"]
    async with AsyncExitStack() as claimed:
        # Each account held from its read to its write, as a release holds it.
        for ns in sorted(held["namespaces"]):
            await claimed.enter_async_context(account_change(org, ns))
        rows = []  # the claim reads as of every write that landed
        for ns in held["namespaces"]:
            row = await namespaces.fetch_row(org, ns)
            # Still that email's SSO account, with no password yet: one changed
            # since the link was made is left alone, and so is one this link set
            # already (a retry after a write that landed).
            if row and row["active"] and not row["password_hash"] and row["contact_email"] == email:
                rows.append(row)
        if not rows:
            return _closed()
        accounts = [(org, r["namespace"]) for r in rows]
        # All inside the claim, so a release after it finds them: the SSO link
        # first (kept if the rest fails), then the password, its audit row, and
        # last the spend, so a failed write leaves the link to retry.
        await sso_links.link(email, accounts, "password-link")
        now = datetime.now(UTC).replace(tzinfo=None, microsecond=0)
        # Insert-as-replace carrying every other column forward.
        await get_client().insert(
            "loom.namespaces",
            [
                {**r, "password_hash": password_hash, "updated_at": max(now, r["updated_at"])}
                for r in rows
            ],
        )
        await audit.emit(
            "auth.password_set",
            org=org,
            namespace="-",
            actor=email,
            subject={"namespaces": [ns for _, ns in accounts], "link_by": held.get("by", "")},
        )
        await oauth.store.take_pending(token_id(code))
    return oauth.html_page(
        "Set your Loom password",
        f"<h2>Password set</h2><p>Sign in as <b>{html.escape(email)}</b>.</p>",
    )


def attach(app: Any) -> None:
    """Mounted wherever sign-in is: the page is for a person signing in."""
    if settings.auth_actually_required() or settings.oauth_enabled:
        app.include_router(router)
