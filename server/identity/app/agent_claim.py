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
)
from server.core.background import TaskSet
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


def _form(
    namespace: str,
    error: str = "",
    status_code: int = 200,
    notice: str = "",
    retry_after_s: int | None = None,
) -> Response:
    # `notice` is HTML the caller built; `error` is text.
    message = (f'<p class="error">{html.escape(error)}</p>' if error else "") + notice
    return oauth.html_page(
        _TITLE,
        f"""<h2>{_TITLE}</h2>
<p>Enter the code the agent printed and choose your password. The code works
once; afterwards you sign in with the namespace name and this password.
Claiming can take a minute or more: submit once and wait for the answer.</p>{message}
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
        headers=None if retry_after_s is None else {"Retry-After": str(retry_after_s)},
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
    if len(code) > PASSWORD_MAX_LENGTH or len(namespace) > 64:
        # Longer than any code or namespace: wrong for every account, and not hashed.
        return _form("", "That code does not claim this namespace, or it was used.", 400)
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
        return _form(namespace, _throttled_message(e), 429, retry_after_s=e.retry_after_s)
    except Exception as e:  # noqa: BLE001 — a failed check answers the page, not a bare 500
        _log_unexpected(namespace, e)
        return _retry_later(namespace, "The code could not be checked: submit again in a minute.")
    if proven is None:
        return _form(namespace, "That code does not claim this namespace, or it was used.", 400)
    org, code_hash = proven
    # The namespace's rows are this cell's, so its org is held; checked anyway, so
    # a claim never writes for an org the cell does not hold (no principal: no
    # placement).
    from server.auth import tenancy
    from server.core.errors import WrongCellError

    try:
        await tenancy.ensure_held(org, None)
    except WrongCellError:
        return _form(namespace, "This namespace cannot be claimed on this server.", 409)
    except Exception as e:  # noqa: BLE001 — an unread tenancy answers the page, not a bare 500
        _log_unexpected(namespace, e)
        return _retry_later(namespace, "The code could not be checked: submit again in a minute.")
    # Hashed only once the code is proven, so a guess costs one hash.
    password_hash = await hash_password_off_loop(password)
    try:
        outcome, cutoff = await claim_shielded(org, namespace, code_hash, password_hash)
    except GrantBusyError:
        return _retry_later(namespace, _EARLIER_SUBMIT)
    except Exception:  # noqa: BLE001 — answered by the page; `_done` logs it
        # Nothing was written: the code still claims.
        return _retry_later(namespace, "The claim could not run: submit again in a minute.")
    if cutoff is None:
        return _form(namespace, "That code does not claim this namespace, or it was used.", 400)
    # The person signs in next: past the cutoff on every replica's clock, so the
    # token they get is not one it ends.
    if outcome == "taken":
        await revocation.wait_out(cutoff)
        return _form(
            namespace,
            "This namespace was claimed a moment ago. If that was an earlier submit of this "
            "form, sign in with the namespace name and the password you chose; otherwise ask "
            "your Loom operator.",
            400,
        )
    others, _ = await asyncio.gather(
        _listed_once_replicas_cut_off(org, namespace), revocation.wait_out(cutoff)
    )
    if outcome == "unconfirmed":
        return _form(
            namespace,
            "This claim may still be landing: wait a minute and sign in with the password you "
            "chose; if that fails, submit again.",
            503,
            notice=_grants_notice(others),
            retry_after_s=60,
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
    review = (
        "<p>Review its grants and all of its settings in the dashboard: whoever held "
        "the code could change any of them.</p>"
    )
    if others is None:
        return "<p>The other holders of grants on it could not be listed.</p>" + review
    if not others:
        return review
    names = ", ".join(f"<b>{html.escape(s)}</b>" for s in others)
    return f"<p>These subjects also hold grants on it: {names}.</p>{review}"


async def _other_grants(org: str, namespace: str) -> list[str]:
    """The subjects with an active grant on the namespace, besides its own
    subject and the agent's token while it holds the writer role sign-up gave it."""
    agent_tokens = await service_accounts.agent_signup_tokens(org, namespace)
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
    return _form(namespace, error, 503, retry_after_s=60)


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
_running = TaskSet()
# The longest the read-back after a claim's write may take.
# not a setting: a second submit's wait (`_past_a_claim_landed_now`) is derived from it
_READ_BACK_S = 5.0


def _done(task: asyncio.Task[Any]) -> None:
    # Logged here, not by the request, which a disconnect may have cancelled; a
    # busy account or backend is expected and answered.
    if not task.cancelled() and not isinstance(task.exception(), (type(None), *_BUSY)):
        log.warning("agent claim: the claim's task failed: %r", task.exception())


# The most a shutdown waits for the claims still running.
# not a setting: one share of the shutdown's fixed drain budget
SHUTDOWN_WAIT_S = 20.0


async def drain() -> None:
    """Wait for the claims still running, so a shutdown does not drop a cutoff;
    the shutdown's drain budget bounds the wait."""
    live = [t for t in _running if not t.done()]
    if live:
        await asyncio.wait(live)


async def claim_shielded(
    org: str, namespace: str, code_hash: str, password_hash: str
) -> tuple[str, datetime | None]:
    """`_replace_code` in a task of its own: a client that disconnects cancels
    the request, never the write's cutoffs."""
    task = _running.spawn(
        _replace_code(org, namespace, code_hash, password_hash), name="agent-claim"
    )
    task.add_done_callback(_done)
    return await asyncio.shield(task)


async def _replace_code(
    org: str, namespace: str, code_hash: str, password_hash: str
) -> tuple[str, datetime | None]:
    """("claimed", the cutoff the claimer waits out) when this call replaced the
    code with `password_hash`; ("taken", a cutoff past the first claim's last)
    when a claim landed first, as an earlier submit of the same form does;
    ("unconfirmed", the cutoff after the write) when the write failed and did
    not read back, so it may still land; ("refused", None), the only outcome
    without a cutoff, when the row changed otherwise (a release)."""
    subject, sent, written = "", False, False
    # Held from the read to the write, as a release or a password link holds the
    # account; the cutoffs after the write need no lease and run after it.
    try:
        async with account_change(org, namespace):  # its reads are sequentially consistent
            row = await namespaces.fetch_row(org, namespace)
            if row is None or not row["active"]:
                return "refused", None
            if row["password_hash"] != code_hash:
                if CLAIMED_TAG not in (row.get("tags") or []):
                    return "refused", None
                return "taken", _past_a_claim_landed_now()
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
            # It may still land: the cutoff after it runs anyway, and the claim
            # is unconfirmed unless that step's read finds it.
            log.warning("agent claim of %s/%s: the write did not confirm: %r", org, namespace, e)
            cutoff, landed = await _after_write(org, namespace, subject, password_hash)
            await _audit(org, namespace, confirmed=landed)
            return ("claimed" if landed else "unconfirmed"), cutoff
        log.warning(
            "agent claim of %s/%s: the backend failed after the write: %r", org, namespace, e
        )
    cutoff, _ = await _after_write(org, namespace, subject, password_hash)
    # After the last cutoff, so the audit's time does not delay it.
    await _audit(org, namespace)
    return "claimed", cutoff


async def _listed_once_replicas_cut_off(org: str, namespace: str) -> list[str] | None:
    """The other grant holders, listed once every replica has refreshed its
    revocations past the claim's cutoffs: until then a code session can still
    act on another replica. None when they could not be listed."""
    await asyncio.sleep(settings.jwt_revocation_cache_ttl_seconds)
    return await _listed(org, namespace)


async def _listed(org: str, namespace: str) -> list[str] | None:
    """`_other_grants`, or None when they could not be listed."""
    try:
        return await _other_grants(org, namespace)
    except Exception as e:  # noqa: BLE001 — the claim is done; it answers without them
        log.warning("agent claim of %s/%s: listing its grants failed: %r", org, namespace, e)
        return None


async def _landed(org: str, namespace: str, password_hash: str) -> bool:
    try:
        async with asyncio.timeout(_READ_BACK_S):
            with own_write_reads():
                row = await namespaces.fetch_row(org, namespace)
    except Exception as e:  # noqa: BLE001 — an unread row counts as not landed
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


async def _audit(org: str, namespace: str, *, confirmed: bool = True) -> None:
    """Audit the claim with the grants others hold (None when they could not
    be listed), which it leaves for the claimer to review, and whether its
    write read back (`confirmed`)."""
    others = await _listed(org, namespace)
    await audit.emit(  # never raises
        "auth.agent_claimed",
        org=org,
        namespace=namespace,
        actor=namespace,
        subject={"other_grants": others, "confirmed": confirmed},
    )


async def _after_write(
    org: str, namespace: str, subject: str, password_hash: str
) -> tuple[datetime, bool]:
    """Read the write back, wait out the clock skew and cut off again; the cutoff
    the claimer waits out, and whether the write read back. The claim stands
    whatever fails here."""
    # A replica of the build before signs in with a plain-hash code, reading
    # without sequential consistency, until its ClickHouse replica has the
    # write, and the first cutoff predates that.
    # The read only times the cutoff below, which runs whatever it finds.
    landed = await _landed(org, namespace, password_hash)
    if not landed:
        log.warning("agent claim of %s/%s: the write does not read back yet", org, namespace)
    # A replica whose clock runs up to CLOCK_SKEW_S ahead stamps later `iat`s:
    # after this wait the cutoff still ends what any replica issues for
    # CLOCK_SKEW_S + 1 seconds after the read back.
    await _skew_wait()
    try:
        return await account_admin.end_tokens(subject, by=namespace, reason=_REASON), landed
    except Exception as e:  # noqa: BLE001 — the claim is written; the earlier cutoff holds
        log.warning(
            "agent claim of %s/%s: the cutoff after the write failed: %r", org, namespace, e
        )
        # Later than any cutoff the failed call could have stored, so the claimer's
        # next sign-in is not ended by it either.
        return revocation.from_now_on(), landed


def _past_a_claim_landed_now() -> datetime:
    """A cutoff no earlier than the last one of a claim that released the account
    by now: 3 * `_READ_BACK_S` (its release returning, which is assumed, a
    landed check and the read-back), its skew wait, its cutoff's margin
    (`CLOCK_SKEW_S` + 1) and its replica's clock running up to `CLOCK_SKEW_S` ahead."""
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
