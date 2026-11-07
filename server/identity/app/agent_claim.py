"""A person claims an agent's namespace with the code its sign-up returned.

No password sign-in takes an unclaimed agent's code (`server.auth.claim_codes`).
This page checks the code and, while it holds the account, replaces it with a
password the person chooses and marks the account claimed; it cuts off again
after it releases the account. The code works once, and from then on the person
signs in as the namespace (its admin) with their own password. Every token the
namespace's subject held before is cut off: an account made before the code was
stored prefixed could be signed in to with it, by whoever held it. The agent's
own token is a separate subject and keeps working.
"""

from __future__ import annotations

import asyncio
import html
import logging
from datetime import UTC, datetime, timedelta
from typing import Any

from fastapi import APIRouter, Request
from fastapi.responses import Response

from server.auth import oauth, revocation, service_accounts
from server.auth.attempts import client_address
from server.auth.claim_codes import CLAIMED_TAG, UNCLAIMED_SQL, verify_claim_code_off_loop
from server.auth.grant_claims import account_change
from server.auth.login import LoginBusy, LoginThrottled, throttled, verify_as_unknown
from server.auth.passwords import PASSWORD_MAX_LENGTH, PASSWORD_MIN_LENGTH, hash_password_off_loop
from server.auth.self_signup import (
    AGENT_DESCRIPTION,
    AGENT_DESCRIPTION_UNCLAIMED,
    AGENT_NAME_PREFIX,
)
from server.core.ch import get_client, own_write_reads
from server.core.errors import (
    BackendTimeoutError,
    BackendUnavailableError,
    GrantBusyError,
    OverloadedError,
)
from server.identity.app import account_admin
from server.identity.store import grants, namespaces
from server.observability import audit
from server.settings import settings

log = logging.getLogger(__name__)
router = APIRouter()

_TITLE = "Claim an agent's namespace"
_EARLIER_SUBMIT = (
    "An earlier submit may still be claiming this namespace: wait a minute and sign in "
    "with the password you chose; if that fails, submit again."
)
# What ClickHouse or the account's lease answer when a step cannot run now.
_BUSY = (GrantBusyError, BackendUnavailableError, BackendTimeoutError, OverloadedError)
_REASON = "agent namespace claimed"


def _form(namespace: str, error: str = "", status_code: int = 200, notice: str = "") -> Response:
    # `notice` is HTML the caller built; `error` is text.
    message = (f'<p class="error">{html.escape(error)}</p>' if error else "") + notice
    return oauth.html_page(
        _TITLE,
        f"""<h2>{_TITLE}</h2>
<p>Enter the code the agent printed and choose your password. The code works
once; afterwards you sign in with the namespace name and this password.
Claiming can take up to a minute: submit once and wait for the answer.</p>{message}
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
    try:
        f = await oauth.form_body(request)
    except oauth.OAuthError:
        return _form("", "The form could not be read: submit it again.", 400)
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
    if password == code:
        # Whoever kept the code would sign in with it.
        return _form(namespace, "Choose a password other than the code.", 400)
    try:
        # A code is a secret like a password: guesses count against the name
        # and the address under the sign-in limits, which hold a check slot only
        # for the code's own check.
        proven = await throttled(
            namespace, client_address(request), lambda: _proven(namespace, code)
        )
    except LoginThrottled as e:
        page = _form(namespace, _throttled_message(e), 429)
        page.headers["Retry-After"] = str(e.retry_after_s)
        return page
    except Exception as e:  # noqa: BLE001 — a failed check answers the page, not a bare 500
        _log_unexpected(namespace, e)
        return _retry_later(namespace, "The code could not be checked: submit again in a minute.")
    if proven is None:
        return _form(namespace, "That code does not claim this namespace, or it was used.", 400)
    org, code_hash = proven
    # Hashed only once the code is proven, so a guess costs one hash.
    password_hash = await hash_password_off_loop(password)
    try:
        outcome, cutoff, others = await claim_shielded(org, namespace, code_hash, password_hash)
    except Exception:  # noqa: BLE001 — answered by the page; `_done` logs it
        return _retry_later(namespace, _EARLIER_SUBMIT)
    if cutoff is None:
        return _form(namespace, "That code does not claim this namespace, or it was used.", 400)
    # The person signs in next: past the cutoff on every replica's clock, so the
    # token they get is not one it ends.
    await revocation.wait_out(cutoff)
    if outcome == "unconfirmed":
        page = _form(namespace, _EARLIER_SUBMIT, 503, notice=_grants_notice(others))
        page.headers["Retry-After"] = "60"
        return page
    if outcome == "taken":
        return _form(
            namespace,
            "This namespace was claimed a moment ago. If that was an earlier submit of this "
            "form, sign in with the namespace name and the password you chose; otherwise ask "
            "your Loom operator.",
            400,
        )
    return oauth.html_page(
        _TITLE,
        f"<h2>Claimed</h2><p>Sign in as <b>{html.escape(namespace)}</b> with your password.</p>"
        + _grants_notice(others),
    )


def _log_unexpected(namespace: str, e: Exception) -> None:
    # A busy account or backend is expected and answered; anything else is logged.
    if not isinstance(e, _BUSY):
        log.warning("agent claim of %s: %r", namespace, e)


def _grants_notice(others: list[str] | None) -> str:
    # The claim ends no grant: one made before it, by whoever held the code,
    # is the claimer's to review.
    review = "<p>Review its grants, visibility and join policy in the dashboard.</p>"
    if others is None:
        return "<p>The other holders of grants on it could not be listed.</p>" + review
    if not others:
        return review
    names = ", ".join(f"<b>{html.escape(s)}</b>" for s in others)
    return f"<p>These subjects also hold grants on it: {names}.</p>{review}"


async def _other_grants(org: str, namespace: str) -> list[str]:
    """The subjects with an active grant on the namespace, besides its own
    subject and the agent's token while it holds the writer role sign-up gave it."""
    agent_tokens = await service_accounts.agent_signup_tokens(org, namespace, AGENT_NAME_PREFIX)
    rows = await grants.active_on(org, namespace)
    return sorted(
        {
            str(r["subject"])
            for r in rows
            if str(r["subject"]) != namespace
            and not (str(r["subject"]) in agent_tokens and r["role"] == "writer")
        }
    )


def _throttled_message(e: LoginThrottled) -> str:
    if isinstance(e, LoginBusy):
        return "Too many checks at once; try again in a second."
    # The limits are the sign-in's: wrong codes count against signing in as the
    # namespace too.
    return (
        "Too many failed attempts for this namespace or from this address; try again in "
        f"{e.retry_after_s // 60} minutes. Until then signing in as it is refused too."
    )


def _retry_later(namespace: str, error: str) -> Response:
    page = _form(namespace, error, 503)
    page.headers["Retry-After"] = "60"
    return page


async def _proven(namespace: str, code: str) -> tuple[str, str] | None:
    """(org, stored hash) of the unclaimed `namespace` the code verifies, or None."""
    rows = await namespaces.unclaimed_rows(namespace, UNCLAIMED_SQL)
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
# The longest the read-back after a claim's write may take.
_READ_BACK_S = 5.0


def _done(task: asyncio.Task[Any]) -> None:
    # Logged here, not by the request, which a disconnect may have cancelled; a
    # busy account or backend is expected and answered.
    _running.discard(task)
    if not task.cancelled() and not isinstance(task.exception(), (type(None), *_BUSY)):
        log.warning("agent claim: the claim's task failed: %r", task.exception())


async def drain() -> None:
    """Wait for the claims still running, so a shutdown does not drop a cutoff;
    the shutdown's drain budget bounds the wait."""
    if _running:
        await asyncio.wait(set(_running))


async def claim_shielded(
    org: str, namespace: str, code_hash: str, password_hash: str
) -> tuple[str, datetime | None, list[str] | None]:
    """`_replace_code` in a task of its own: a client that disconnects cancels
    the request, never the write's cutoffs."""
    task = asyncio.create_task(_replace_code(org, namespace, code_hash, password_hash))
    _running.add(task)
    task.add_done_callback(_done)
    return await asyncio.shield(task)


async def _replace_code(
    org: str, namespace: str, code_hash: str, password_hash: str
) -> tuple[str, datetime | None, list[str] | None]:
    """("claimed", the cutoff the claimer waits out, the other grant holders or
    None when they could not be listed) when this call replaced the code with
    `password_hash`; ("taken", a cutoff past the first claim's last, None) when a
    claim landed first, as an earlier submit of the same form does;
    ("unconfirmed", the cutoff after the write, the holders) when the write
    failed and did not read back, so it may still land; ("refused", None,
    None), the only outcome without a cutoff, when the row changed otherwise
    (a release)."""
    subject, sent, written = "", False, False
    # Held from the read to the write, as a release or a password link holds the
    # account; the cutoffs after the write need no lease and run after it.
    try:
        async with account_change(org, namespace):  # its reads are sequentially consistent
            row = await namespaces.fetch_row(org, namespace)
            if row is None or not row["active"]:
                return "refused", None, None
            if row["password_hash"] != code_hash:
                if CLAIMED_TAG not in (row.get("tags") or []):
                    return "refused", None, None
                return "taken", _past_a_claim_landed_now(), None
            subject = str(row["owner_email"])
            # Before the write, so a write that fails leaves the code to retry with,
            # never a spent code and live tokens. A subject cutoff also ends every
            # OAuth and device session the code started (`oauth.issue_tokens`).
            await account_admin.end_tokens(subject, by=namespace, reason=_REASON)
            sent = True
            await _write(row, password_hash)
            written = True
    except Exception as e:  # once the write is sent, the row decides what it did
        if not sent:
            raise
        # A write can land and still report a failure (a timeout after it was
        # applied, the lease's bound after it).
        if not written and not await _landed(org, namespace, password_hash):
            # It may still land: the cutoff after it runs anyway, the claim is
            # audited as unconfirmed, and the person is told to try their
            # password before submitting again.
            log.warning("agent claim of %s/%s: the write did not confirm: %r", org, namespace, e)
            cutoff = await _after_write(org, namespace, subject, password_hash)
            return "unconfirmed", cutoff, await _audit(org, namespace, confirmed=False)
        log.warning(
            "agent claim of %s/%s: the backend failed after the write: %r", org, namespace, e
        )
    cutoff = await _after_write(org, namespace, subject, password_hash)
    # After the last cutoff, so the audit's time does not delay it.
    return "claimed", cutoff, await _audit(org, namespace)


async def _landed(org: str, namespace: str, password_hash: str) -> bool:
    try:
        async with asyncio.timeout(_READ_BACK_S):
            with own_write_reads():
                row = await namespaces.fetch_row(org, namespace)
    except Exception as e:  # noqa: BLE001 — unread, the claim's own error answers
        log.warning("agent claim of %s/%s: reading the write failed: %r", org, namespace, e)
        return False
    return (row or {}).get("password_hash") == password_hash


async def _write(row: dict[str, Any], password_hash: str) -> None:
    """Replace the code with the claimer's password and mark the account claimed."""
    now = datetime.now(UTC).replace(tzinfo=None, microsecond=0)
    description = str(row["description"])
    if description == AGENT_DESCRIPTION_UNCLAIMED:
        description = AGENT_DESCRIPTION
    await get_client().insert(
        "loom.namespaces",
        [namespaces.claimed_row(row, password_hash, CLAIMED_TAG, description, now)],
    )


async def _audit(org: str, namespace: str, *, confirmed: bool = True) -> list[str] | None:
    """Audit the claim with the grants others hold, which it leaves for the
    claimer to review, and whether its write read back (`confirmed`); those
    holders, or None when they could not be listed."""
    others: list[str] | None = None
    try:
        others = await _other_grants(org, namespace)
    except Exception as e:  # noqa: BLE001 — the claim is written; it is audited without them
        log.warning("agent claim of %s/%s: listing its grants failed: %r", org, namespace, e)
    try:
        await audit.emit(
            "auth.agent_claimed",
            org=org,
            namespace=namespace,
            actor=namespace,
            subject={"other_grants": others, "confirmed": confirmed},
        )
    except Exception as e:  # noqa: BLE001 — the claim is written; the page must still answer
        log.warning("agent claim of %s/%s: the audit row failed: %r", org, namespace, e)
    return others


async def _after_write(org: str, namespace: str, subject: str, password_hash: str) -> datetime:
    """Read the write back, wait out the clock skew and cut off again; the cutoff
    the claimer waits out. The claim stands whatever fails here."""
    # A replica of the build before signs in with a plain-hash code, reading
    # without sequential consistency, until its ClickHouse replica has the
    # write, and the first cutoff predates that.
    try:
        async with asyncio.timeout(_READ_BACK_S):
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


def _past_a_claim_landed_now() -> datetime:
    """A cutoff no earlier than the last one of a claim that released the account
    by now: `_READ_BACK_S` for the release returning and once more for a check
    that its write landed, its read-back (`_READ_BACK_S`), its skew wait, its
    cutoff's own margin (`CLOCK_SKEW_S` + 1), and `CLOCK_SKEW_S` more for its
    replica's clock running ahead of this one's."""
    skew = revocation.CLOCK_SKEW_S
    return _now() + timedelta(seconds=3 * _READ_BACK_S + skew + (skew + 1) + skew)


def _now() -> datetime:
    return datetime.now(UTC)


async def _skew_wait() -> None:
    await asyncio.sleep(revocation.CLOCK_SKEW_S)


def attach(app: Any) -> None:
    """Mounted wherever sign-in is: the page is for a person signing in."""
    if settings.auth_actually_required() or settings.oauth_enabled:
        app.include_router(router)
