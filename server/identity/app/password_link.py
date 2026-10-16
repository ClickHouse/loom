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
from datetime import UTC, datetime
from typing import Any

from fastapi import APIRouter, Request
from fastapi.responses import Response

from server.auth import oauth, sso_links
from server.auth.oauth_store import token_id
from server.core.ch import get_client
from server.identity.store import namespaces
from server.observability import audit
from server.settings import settings

log = logging.getLogger(__name__)
router = APIRouter()

LINK_TTL = 86400
_MIN_LENGTH, _MAX_LENGTH = 8, 256  # as self-signup asks

# The SSO accounts (no password) naming an email, active, in one org.
_SSO_ACCOUNTS = """
    SELECT org, namespace FROM loom.namespaces FINAL
    WHERE contact_email = {email:String} AND active = 1 AND password_hash = ''
      AND ({org:String} = '' OR org = {org:String})
    ORDER BY org, namespace
"""


async def sso_accounts(email: str, org: str = "") -> list[dict[str, Any]]:
    return await get_client().query_all(
        _SSO_ACCOUNTS, params={"email": email.strip().lower(), "org": org}
    )


async def make_link(email: str, accounts: list[dict[str, Any]]) -> str:
    """A code that sets the password of `accounts` (one org's), once."""
    code = secrets.token_urlsafe(32)
    await oauth.store.save_pending(
        token_id(code),
        {
            "kind": "set_password",
            "email": email.strip().lower(),
            "org": accounts[0]["org"],
            "namespaces": [a["namespace"] for a in accounts],
        },
        ttl=LINK_TTL,
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
       autocomplete="new-password" minlength="{_MIN_LENGTH}" required>
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
    if not _MIN_LENGTH <= len(password) <= _MAX_LENGTH:
        return _form(code, f"Use {_MIN_LENGTH} to {_MAX_LENGTH} characters.", 400)
    if password != f.get("repeat", ""):
        return _form(code, "The two passwords differ.", 400)
    if await oauth.store.take_pending(token_id(code)) is None:
        return _closed()  # the form sent twice: the other one set it
    email, org = held["email"], held["org"]
    rows = [r for r in [await namespaces.fetch_row(org, ns) for ns in held["namespaces"]] if r]
    # Still that email's SSO accounts: one changed since the link was made is left alone.
    rows = [
        r for r in rows if r["active"] and not r["password_hash"] and r["contact_email"] == email
    ]
    if not rows:
        return _closed()
    password_hash = await hash_password_off_loop(password)
    now = datetime.now(UTC).replace(tzinfo=None, microsecond=0)
    # Insert-as-replace carrying every other column forward.
    await get_client().insert(
        "loom.namespaces",
        [
            {**r, "password_hash": password_hash, "updated_at": max(now, r["updated_at"])}
            for r in rows
        ],
    )
    accounts = [(org, r["namespace"]) for r in rows]
    await sso_links.link(email, accounts, "password-link")
    await audit.emit(
        "auth.password_set",
        org=org,
        namespace="-",
        actor=email,
        subject={"namespaces": [ns for _, ns in accounts], "by": "password-link"},
    )
    return oauth.html_page(
        "Set your Loom password",
        f"<h2>Password set</h2><p>Sign in as <b>{html.escape(email)}</b>.</p>",
    )


def attach(app: Any) -> None:
    """Mounted wherever sign-in is: the page is for a person signing in."""
    if settings.auth_actually_required() or settings.oauth_enabled:
        app.include_router(router)
