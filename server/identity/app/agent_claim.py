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
once; afterwards you sign in with the namespace name and this password.
Claiming takes about 20 seconds: submit once and wait for the answer.</p>{message}
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
    if proven is None:
        return _form(namespace, "That code does not claim this namespace, or it was used.", 400)
    org, code_hash = proven
    # Hashed only once the code is proven, so a guess costs one hash.
    password_hash = await hash_password_off_loop(password)
    outcome, cutoff = await claim_shielded(org, namespace, code_hash, password_hash)
    if outcome == "refused":
        return _form(namespace, "That code does not claim this namespace, or it was used.", 400)
    # The person signs in next: past the cutoff on every replica's clock, so the
    # token they get is not one it ends.
    await revocation.wait_out(cutoff)
    if outcome == "taken":
        return _form(
            namespace,
            "This namespace was claimed a moment ago, perhaps by an earlier submit of this "
            "form: sign in with the namespace name and the password you chose.",
            400,
        )
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


# A claim's write and the cutoffs after it, kept here while they run so a
# cancelled request does not drop them.
_running: set[asyncio.Task[Any]] = set()


async def claim_shielded(
    org: str, namespace: str, code_hash: str, password_hash: str
) -> tuple[str, datetime]:
    """`_replace_code` in a task of its own: a client that disconnects cancels
    the request, never the write's cutoffs."""
    task = asyncio.create_task(_replace_code(org, namespace, code_hash, password_hash))
    _running.add(task)
    task.add_done_callback(_running.discard)
    return await asyncio.shield(task)


async def _replace_code(
    org: str, namespace: str, code_hash: str, password_hash: str
) -> tuple[str, datetime]:
    """("claimed", the cutoff the claimer waits out) when this call replaced the
    code with `password_hash`; ("taken", the subject's stored cutoff) when a claim
    landed first, as an earlier submit of the same form does; ("refused", now)
    when the row changed otherwise (a release)."""
    # Held from the read to the last cutoff, as a release or a password link
    # holds the account: a second claim waits for the first's last cutoff.
    written: list[bool] = []
    try:
        async with account_change(org, namespace):
            with own_write_reads():
                row = await namespaces.fetch_row(org, namespace)
            if row is None or not row["active"]:
                return "refused", revocation.from_now_on()
            if row["password_hash"] != code_hash:
                if CLAIMED_TAG not in (row.get("tags") or []):
                    return "refused", revocation.from_now_on()
                return "taken", await _stored_cutoff(str(row["owner_email"]))
            return "claimed", await _write_claim(row, org, namespace, password_hash, written)
    except Exception as e:  # once written, the claim stands whatever follows
        if not written:
            raise
        # The claim is written; the lease's own bound ran out after it.
        log.warning("agent claim of %s/%s: finishing the claim failed: %r", org, namespace, e)
        return "claimed", revocation.from_now_on()


async def _stored_cutoff(subject: str) -> datetime:
    """The subject's latest stored cutoff, or now when none can be read."""
    try:
        stored = (await revocation.cutoffs([subject])).get(subject)
    except Exception as e:  # noqa: BLE001 — waiting from now is the safe answer
        log.warning("agent claim: reading %s's cutoff failed: %r", subject, e)
        stored = None
    return datetime.fromtimestamp(stored, UTC) if stored else revocation.from_now_on()


async def _write_claim(
    row: dict[str, Any], org: str, namespace: str, password_hash: str, written: list[bool]
) -> datetime:
    """Under the account's lease: cut off, write the claim (then mark `written`),
    audit it, read it back, wait out the clock skew and cut off again."""
    # Before the write, so a write that fails leaves the code to retry with,
    # never a spent code and live tokens. A subject cutoff also ends every
    # OAuth and device session the code started (`oauth.issue_tokens`).
    subject = str(row["owner_email"])
    await account_admin.end_tokens(subject, by=namespace, reason=_REASON)
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
    written.append(True)
    # The claim stands from here: audited before anything after it can fail.
    await audit.emit(
        "auth.agent_claimed", org=org, namespace=namespace, actor=namespace, subject={}
    )
    # Again once the write reads back: a replica of the build before signs in with
    # a plain-hash code, reading without sequential consistency, until its
    # ClickHouse replica has the write, and the first cutoff predates it.
    # The claim is written, so nothing below answers an error.
    try:
        with own_write_reads():
            landed = await namespaces.fetch_row(org, namespace)
        if (landed or {}).get("password_hash") != password_hash:
            log.warning("agent claim of %s/%s: the write does not read back yet", org, namespace)
    except Exception as e:  # noqa: BLE001 — the read only times the cutoff below, which still runs
        log.warning("agent claim of %s/%s: reading the write back failed: %r", org, namespace, e)
    # A replica whose clock runs up to CLOCK_SKEW_S ahead stamps later `iat`s:
    # after this wait the cutoff still ends what any replica issues for
    # CLOCK_SKEW_S + 1 seconds after the read back.
    await _skew_wait()
    try:
        return await account_admin.end_tokens(subject, by=namespace, reason=_REASON)
    except Exception as e:  # noqa: BLE001 — the claim is written; the earlier cutoff holds
        log.warning(
            "agent claim of %s/%s: the cutoff after the write failed: %r", org, namespace, e
        )
        # Later than any cutoff the failed call could have stored, so the claimer's
        # next sign-in is not ended by it either.
        return revocation.from_now_on()


async def _skew_wait() -> None:
    await asyncio.sleep(revocation.CLOCK_SKEW_S)


def attach(app: Any) -> None:
    """Mounted wherever sign-in is: the page is for a person signing in."""
    if settings.auth_actually_required() or settings.oauth_enabled:
        app.include_router(router)
