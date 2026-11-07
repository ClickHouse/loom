"""A person claims an agent's namespace with the code its sign-up returned.

No password sign-in takes an unclaimed agent's code (`server.auth.claim_codes`).
This page checks the code and, while it holds the account, replaces it with a
password the person chooses and marks the account claimed: the code works once,
and from then on the person signs in as the namespace (its admin) with their own
password. Every token the namespace's subject held before is cut off: an account
made before the code was stored prefixed could be signed in to with it, by
whoever held it. The agent's own token is a separate subject and keeps working.
"""

from __future__ import annotations

import asyncio
import html
import logging
from datetime import UTC, datetime
from typing import Any

from fastapi import APIRouter, Request
from fastapi.responses import Response

from server.auth import oauth, revocation
from server.auth.attempts import client_address
from server.auth.claim_codes import CLAIMED_TAG, UNCLAIMED_SQL, verify_claim_code_off_loop
from server.auth.grant_claims import account_change
from server.auth.login import LoginThrottled, throttled, verify_as_unknown
from server.auth.passwords import PASSWORD_MAX_LENGTH, PASSWORD_MIN_LENGTH, hash_password_off_loop
from server.core.ch import get_client, own_write_reads
from server.identity.app import account_admin
from server.identity.store import namespaces
from server.observability import audit
from server.settings import settings

log = logging.getLogger(__name__)
router = APIRouter()

_TITLE = "Claim an agent's namespace"
_REASON = "agent namespace claimed"

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
       autocomplete="new-password" minlength="{PASSWORD_MIN_LENGTH}" required>
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
    if not PASSWORD_MIN_LENGTH <= len(password) <= PASSWORD_MAX_LENGTH:
        return _form(
            namespace, f"Use {PASSWORD_MIN_LENGTH} to {PASSWORD_MAX_LENGTH} characters.", 400
        )
    if password != f.get("repeat", ""):
        return _form(namespace, "The two passwords differ.", 400)
    try:
        # A code is a secret like a password: guesses count against the name
        # and the address under the sign-in limits, which hold a check slot only
        # for the code's own check.
        proven = await throttled(
            namespace, client_address(request), lambda: _proven(namespace, code)
        )
    except LoginThrottled as e:
        page = _form(namespace, e.message, 429)
        page.headers["Retry-After"] = str(e.retry_after_s)
        return page
    claimed = None
    if proven is not None:
        org, code_hash = proven
        # Hashed only once the code is proven, so a guess costs one hash.
        password_hash = await hash_password_off_loop(password)
        claimed = await _replace_code(org, namespace, code_hash, password_hash)
    if claimed is None:
        return _form(namespace, "That code does not claim this namespace, or it was used.", 400)
    # The person signs in next: past the cutoff on every replica's clock, so the
    # token they get is not one it ends.
    await revocation.wait_out(claimed)
    return oauth.html_page(
        _TITLE,
        f"<h2>Claimed</h2><p>Sign in as <b>{html.escape(namespace)}</b> with your password.</p>",
    )


async def _proven(namespace: str, code: str) -> tuple[str, str] | None:
    """(org, stored hash) of the unclaimed `namespace` the code verifies, or None."""
    rows = await get_client().query_all(_UNCLAIMED, params={"ns": namespace})
    if not rows:
        await verify_as_unknown(code)  # timed like a wrong code
        return None
    for r in rows:
        if await verify_claim_code_off_loop(r["password_hash"], code):
            return str(r["org"]), str(r["password_hash"])
    return None


async def _replace_code(
    org: str, namespace: str, code_hash: str, password_hash: str
) -> datetime | None:
    """Replace the code with `password_hash` and cut off the subject's earlier
    tokens; the cutoff the claimer waits out, or None when the row changed since
    the code was checked (a claim or release that landed first)."""
    # Held from the read to the write, as a release or a password link holds
    # the account.
    async with account_change(org, namespace):
        with own_write_reads():
            row = await namespaces.fetch_row(org, namespace)
        if row is None or not row["active"] or row["password_hash"] != code_hash:
            return None
        # Before the write, so a write that fails leaves the code to retry with,
        # never a spent code and live tokens. A subject cutoff also ends every
        # OAuth and device session the code started (`oauth.issue_tokens`).
        subject = str(row["owner_email"])
        first = await account_admin.end_tokens(subject, by=namespace, reason=_REASON)
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
        # The claim stands from here: audited before anything after it can fail.
        await audit.emit(
            "auth.agent_claimed", org=org, namespace=namespace, actor=namespace, subject={}
        )
    # Again once the write reads back: a replica of the build before signs in with
    # a plain-hash code, reading without sequential consistency, until its
    # ClickHouse replica has the write, and the first cutoff predates it.
    try:
        with own_write_reads():
            landed = await namespaces.fetch_row(org, namespace)
        if (landed or {}).get("password_hash") != password_hash:
            log.warning("agent claim of %s/%s: the write does not read back yet", org, namespace)
        # A replica whose clock runs up to CLOCK_SKEW_S ahead stamps later `iat`s:
        # after this wait the cutoff still ends what any replica issues for
        # CLOCK_SKEW_S + 1 seconds after the read back.
        await _skew_wait()
        return await account_admin.end_tokens(subject, by=namespace, reason=_REASON)
    except Exception as e:  # noqa: BLE001 — the claim is written; the first cutoff still holds
        log.warning(
            "agent claim of %s/%s: the cutoff after the write failed: %r", org, namespace, e
        )
        return first


async def _skew_wait() -> None:
    await asyncio.sleep(revocation.CLOCK_SKEW_S)


def attach(app: Any) -> None:
    """Mounted wherever sign-in is: the page is for a person signing in."""
    if settings.auth_actually_required() or settings.oauth_enabled:
        app.include_router(router)
