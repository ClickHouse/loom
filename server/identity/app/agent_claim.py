"""A person claims an agent's namespace with the code its sign-up returned.

No password sign-in takes an unclaimed agent's code (`server.auth.claim_codes`).
This page checks the code and, while it holds the account, replaces it with a
password the person chooses and marks the account claimed: the code works once,
and from then on the person signs in as the namespace (its admin) with their own
password. The agent's own token is a separate subject and keeps working.
"""

from __future__ import annotations

import html
from datetime import UTC, datetime
from typing import Any

from fastapi import APIRouter, Request
from fastapi.responses import Response

from server.auth import oauth
from server.auth.attempts import client_address
from server.auth.claim_codes import CLAIMED_TAG, UNCLAIMED_SQL, verify_claim_code_off_loop
from server.auth.grant_claims import account_change
from server.auth.login import LoginThrottled, throttled, verify_as_unknown
from server.auth.passwords import hash_password_off_loop
from server.core.ch import get_client
from server.identity.store import namespaces
from server.observability import audit
from server.settings import settings

router = APIRouter()

_MIN_LENGTH, _MAX_LENGTH = 8, 256  # as self-signup asks
_TITLE = "Claim an agent's namespace"

# The active accounts named `ns` that still hold a claim code, in every org.
_UNCLAIMED = """
    SELECT org, namespace, password_hash FROM loom.namespaces FINAL
    WHERE namespace = {ns:String} AND active = 1 AND {unclaimed}
    ORDER BY org
""".replace("{unclaimed}", UNCLAIMED_SQL)


def _form(namespace: str, error: str = "", status_code: int = 200) -> Response:
    message = f'<p class="error">{html.escape(error)}</p>' if error else ""
    return oauth.html_page(
        _TITLE,
        f"""<h2>{_TITLE}</h2>
<p>Enter the code the agent printed and choose your password. The code works
once; afterwards you sign in with the namespace name and this password.</p>{message}
<form method="post" action="/oauth/agent-claim">
<input name="namespace" placeholder="agent-…" value="{html.escape(namespace)}" required>
<input name="code" placeholder="claim code" autocomplete="off" required>
<input name="password" type="password" placeholder="new password"
       autocomplete="new-password" minlength="{_MIN_LENGTH}" required>
<input name="repeat" type="password" placeholder="the same again"
       autocomplete="new-password" required>
<button type="submit">Claim</button>
</form>""",
        status_code,
    )


def _closed() -> Response:
    return oauth.html_page(
        _TITLE,
        "<h2>Claims are closed here</h2><p>Single sign-on is on: ask your Loom operator.</p>",
        400,
    )


@router.get("/oauth/agent-claim")
async def agent_claim_page(namespace: str = "") -> Response:
    # With SSO on, a password would bypass the provider.
    if settings.oauth_login_provider == "oidc":
        return _closed()
    return _form(namespace)


@router.post("/oauth/agent-claim")
async def agent_claim(request: Request) -> Response:
    if settings.oauth_login_provider == "oidc":
        return _closed()
    f = await oauth.form_body(request)
    namespace = f.get("namespace", "").strip().lower()
    code, password = f.get("code", "").strip(), f.get("password", "")
    if not namespace or not code:
        return _form(namespace, "Enter the namespace and its claim code.", 400)
    if not _MIN_LENGTH <= len(password) <= _MAX_LENGTH:
        return _form(namespace, f"Use {_MIN_LENGTH} to {_MAX_LENGTH} characters.", 400)
    if password != f.get("repeat", ""):
        return _form(namespace, "The two passwords differ.", 400)
    try:
        # A code is a secret like a password: guesses count against the name
        # and the address under the sign-in limits.
        org = await throttled(
            namespace, client_address(request), lambda: _claim(namespace, code, password)
        )
    except LoginThrottled as e:
        page = _form(namespace, e.message, 429)
        page.headers["Retry-After"] = str(e.retry_after_s)
        return page
    if org is None:
        return _form(namespace, "That code does not claim this namespace, or it was used.", 400)
    await audit.emit(
        "auth.agent_claimed", org=org, namespace=namespace, actor=namespace, subject={}
    )
    return oauth.html_page(
        _TITLE,
        f"<h2>Claimed</h2><p>Sign in as <b>{html.escape(namespace)}</b> with your password.</p>",
    )


async def _claim(namespace: str, code: str, password: str) -> str | None:
    """The org whose `namespace` the code claimed, or None. The first claim
    replaces the code with the hash of `password`, so a second finds nothing."""
    rows = await get_client().query_all(_UNCLAIMED, params={"ns": namespace})
    if not rows:
        await verify_as_unknown(code)  # timed like a wrong code
        return None
    for r in rows:
        if await verify_claim_code_off_loop(r["password_hash"], code):
            # Hashed only once the code is proven, so a guess costs one hash.
            password_hash = await hash_password_off_loop(password)
            return await _replace_code(r["org"], namespace, r["password_hash"], password_hash)
    return None


async def _replace_code(org: str, namespace: str, code_hash: str, password_hash: str) -> str | None:
    # Held from the read to the write, as a release or a password link holds
    # the account: a claim or release that landed first changed the row, and
    # this one is refused.
    async with account_change(org, namespace):
        row = await namespaces.fetch_row(org, namespace)
        if row is None or not row["active"] or row["password_hash"] != code_hash:
            return None
        now = datetime.now(UTC).replace(tzinfo=None, microsecond=0)
        # Insert-as-replace carrying every other column forward.
        await get_client().insert(
            "loom.namespaces",
            [
                {
                    **row,
                    "password_hash": password_hash,
                    "tags": [*row["tags"], CLAIMED_TAG],
                    "updated_at": max(now, row["updated_at"]),
                }
            ],
        )
    return org


def attach(app: Any) -> None:
    """Mounted wherever sign-in is: the page is for a person signing in."""
    if settings.auth_actually_required() or settings.oauth_enabled:
        app.include_router(router)
