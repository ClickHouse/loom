"""Device authorization grant (RFC 8628): how `loom login` signs in with no
browser on the machine it runs on. The CLI asks for a code, the user enters
it on any browser, signs in and approves on a confirm page; the CLI polls the
token endpoint until the grant is decided. Why the confirm page, the ticket
and the limits look as they do: docs/architecture/security.md ("Device grant").
"""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import html
import secrets
import time
from typing import Any

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse, Response

from server.auth import claims, oauth, oidc
from server.auth.attempts import Attempts, address_key, client_address
from server.auth.jwt import new_jti
from server.auth.oauth_store import CodeReplayed, Device, token_id
from server.observability import audit
from server.settings import settings

router = APIRouter()

DEVICE_GRANT = "urn:ietf:params:oauth:grant-type:device_code"
# The CLI and the SDK are one public client; there is nothing to register.
DEVICE_CLIENT_ID = "loom-cli"

_DEVICE_TTL = 600
_INTERVAL = 5
# Consonants only: no vowels to spell words, no 0/O or 1/I to misread.
_USER_CODE_ALPHABET = "BCDFGHJKLMNPQRSTVWXZ"
_USER_CODE_LENGTH = 8  # 20**8 ≈ 2**34.6, against 10 guesses per address per 15 min

_requests_by_address = Attempts(30, 900)
_wrong_codes_by_address = Attempts(10, 900)
# One poll per interval (with a second of slack for timer jitter); a faster
# poll gets `slow_down`.
_polls = Attempts(1, _INTERVAL - 1)


def _new_user_code() -> str:
    return "".join(secrets.choice(_USER_CODE_ALPHABET) for _ in range(_USER_CODE_LENGTH))


def normalize_user_code(raw: str) -> str:
    """What the user typed, as stored: upper case, the dash and spaces gone."""
    return "".join(c for c in raw.upper() if c in _USER_CODE_ALPHABET)


def _user_code_id(user_code: str) -> str:
    """How a user code is stored: keyed with the server's secret, since a plain
    hash of a space this small (2**34.6) is reversed in seconds."""
    key = hashlib.sha256(b"loom.device user code:" + settings.jwt_secret.encode()).digest()
    return hmac.new(key, user_code.encode(), hashlib.sha256).hexdigest()


def _display(user_code: str) -> str:
    return f"{user_code[:4]}-{user_code[4:]}"


@router.post("/oauth/device_authorization")
async def device_authorization(request: Request) -> JSONResponse:
    f = await oauth.form_body(request)
    if f.get("client_id") != DEVICE_CLIENT_ID:
        raise oauth.OAuthError("invalid_client")
    address = client_address(request)
    if not _requests_by_address.allow(address_key(address)):
        oauth.log_refused("device start", address)
        raise oauth.OAuthError("slow_down", 429, headers={"Retry-After": "900"})
    device_code = secrets.token_urlsafe(32)
    user_code = _new_user_code()
    await oauth.store.save_device(
        {
            "id": token_id(device_code),
            "client_id": DEVICE_CLIENT_ID,
            "subject": "",
            "org": "",
            "namespaces": [],
            "roles": [],
            "exp": int(time.time()) + _DEVICE_TTL,
            "family": "",
            "upstream_refresh": "",
            "user_code": _user_code_id(user_code),
            "status": "pending",
            "address": address,
            "ticket": "",
        }
    )
    page = f"{oauth.issuer(request)}/oauth/device"
    return JSONResponse(
        {
            "device_code": device_code,
            "user_code": _display(user_code),
            "verification_uri": page,
            "verification_uri_complete": f"{page}?user_code={_display(user_code)}",
            "expires_in": _DEVICE_TTL,
            "interval": _INTERVAL,
        },
        headers={"Cache-Control": "no-store"},
    )


def _code_form(user_code: str, error: str = "", status_code: int = 200) -> Response:
    sso = settings.oauth_login_provider == "oidc"
    sign_in = (
        '<button type="submit">Continue with single sign-on</button>'
        if sso
        else """<input name="identifier" placeholder="email or namespace"
       autocomplete="username" required>
<input name="password" type="password" placeholder="password"
       autocomplete="current-password" required>
<button type="submit">Sign in</button>"""
    )
    message = f'<p class="error">{html.escape(error)}</p>' if error else ""
    return oauth.html_page(
        "Sign in to Loom",
        f"""<h2>Sign in to Loom</h2>
<p>Enter the code your terminal shows, then sign in.</p>{message}
<form method="post" action="/oauth/device">
<input name="user_code" value="{html.escape(user_code)}" placeholder="XXXX-XXXX"
       autocomplete="off" autocapitalize="characters" required>
{sign_in}
</form>""",
        status_code,
    )


@router.get("/oauth/device")
async def device_page(user_code: str = "") -> Response:
    return _code_form(user_code)


@router.post("/oauth/device")
async def device_sign_in(request: Request) -> Response:
    from server.auth.login import LoginThrottled, resolve_user_credentials

    f = await oauth.form_body(request)
    typed = f.get("user_code", "")
    address = client_address(request)
    if _wrong_codes_by_address.blocked(address_key(address)):
        return _code_form(typed, "Too many wrong codes. Try again later.", 429)
    user_code = normalize_user_code(typed)
    device = await oauth.store.device_by_user_code(_user_code_id(user_code))
    if device is None:
        _wrong_codes_by_address.record(address_key(address))
        return _code_form(typed, "That code is wrong or has expired.", 400)
    if device["status"] != "pending":
        return _decided_page(device)
    if settings.oauth_login_provider == "oidc":
        return await oauth.start_oidc_login(
            "device",
            address,
            user_code=user_code,  # the cookie is sealed whole
        )
    try:
        identity = await resolve_user_credentials(
            f.get("identifier", ""), f.get("password", ""), address=address
        )
    except LoginThrottled:
        return _code_form(typed, "Too many failed sign-ins. Try again later.", 429)
    if identity is None:
        return _code_form(typed, "Invalid email or password.", 401)
    return await signed_in(user_code, identity, "")


def _decided_page(device: Device) -> Response:
    # Approved by whoever entered the code first: if that was not this person,
    # the terminal is signed in as someone else.
    if device["status"] == "approved":
        error = "That code was already used. If you did not approve it, run loom logout there."
    else:
        error = "That sign-in was ended. Run loom login again."
    return _code_form("", error, 400)


def _expired_page() -> Response:
    return oauth.html_page(
        "Sign in to Loom",
        "<h2>This sign-in has expired</h2><p>Run <code>loom login</code> again.</p>",
        400,
    )


async def _undecided(user_code: str) -> Device | None:
    """The undecided grant for `user_code` as it stands now, or None when it is
    gone, decided or expired. Saving a change to it raises claims.Changed if
    another request changed it first."""
    d = await oauth.store.device_by_user_code(_user_code_id(user_code))
    return d if d is not None and d["status"] == "pending" else None


def _busy_page() -> Response:
    return oauth.html_page(
        "Sign in to Loom",
        "<h2>This sign-in is busy</h2><p>Something else is changing it. Try again.</p>",
        409,
    )


async def _revoke_all(*tokens: str) -> None:
    for token in tokens:
        await oidc.revoke(token)  # a no-op for ''


async def signed_in(
    user_code: str,
    identity: tuple[str, str, list[str], list[str]],
    upstream_refresh: str,
) -> Response:
    """The user has signed in: remember who, and ask them to confirm. Two
    different people signing in with one code means one of them got it from
    the other's screen; neither can tell which, so the grant ends and the
    terminal has to start again."""
    subject, org, *_ = identity
    ticket = secrets.token_urlsafe(32)
    try:
        for attempt in range(3):
            device = await oauth.store.device_by_user_code(_user_code_id(user_code))
            if device is None or device["status"] != "pending":
                outcome, dropped = "decided", [upstream_refresh]
                break
            try:
                outcome, dropped = await _sign_in(device, identity, upstream_refresh, ticket)
                break
            except claims.Changed:
                if attempt == 2:
                    raise
                # Someone signed in on it at the same moment: decide again on what they wrote.
    except claims.Busy:
        await _revoke_all(upstream_refresh)
        return _busy_page()
    await _revoke_all(*dropped)
    if device is None:
        return _expired_page()
    if outcome == "decided":
        return _decided_page(device)
    if outcome == "conflict":
        return oauth.html_page(
            "Sign-in stopped",
            "<h2>Someone else already used this code</h2>"
            "<p>Nobody is signed in with it. Run <code>loom login</code> again, "
            "and share the code with nobody.</p>",
            409,
        )
    asked = time.strftime("%H:%M UTC", time.gmtime(device["exp"] - _DEVICE_TTL))
    return oauth.html_page(
        "Approve sign-in",
        f"""<h2>Approve sign-in?</h2>
<p>Signed in as <b>{html.escape(subject)}</b> ({html.escape(org)}).</p>
<p>The Loom command line asked to sign in as you at {asked}
from <b>{html.escape(device["address"] or "an unknown address")}</b>.
Check that your terminal shows this code:</p>
<p><code>{_display(user_code)}</code></p>
<p>Approve only if you ran <code>loom login</code> yourself just now.
If someone sent you this code, deny it.</p>
<form method="post" action="/oauth/device/approve">
<input type="hidden" name="user_code" value="{user_code}">
<input type="hidden" name="ticket" value="{ticket}">
<button type="submit" name="decision" value="approve">Approve</button>
<button type="submit" name="decision" value="deny" class="secondary">Deny</button>
</form>""",
    )


async def _sign_in(
    device: Device,
    identity: tuple[str, str, list[str], list[str]],
    upstream_refresh: str,
    ticket: str,
) -> tuple[str, list[str]]:
    """Record who signed in on `device`: the outcome, and the IdP tokens no
    longer needed."""
    subject, org, namespaces, roles = identity
    # One principal is its (org, subject): an `alice` of another org is someone else.
    if device["subject"] and (device["org"], device["subject"]) != (org, subject):
        await oauth.store.save_device(
            device | {"status": "denied", "ticket": "", "upstream_refresh": ""}
        )
        return "conflict", [upstream_refresh, device["upstream_refresh"]]
    # The same person signing in again replaces their IdP token.
    previous = device["upstream_refresh"]
    await oauth.store.save_device(
        device
        | {
            "subject": subject,
            "org": org,
            "namespaces": namespaces,
            "roles": roles,
            "upstream_refresh": upstream_refresh,
            "ticket": token_id(ticket),
        }
    )
    return "confirm", [previous] if previous != upstream_refresh else []


async def deny(user_code: str) -> None:
    """Loom has no account for the person who signed in at the IdP: end the
    grant so `loom login` stops waiting, unless someone else already signed
    in on it (a stranger's failed sign-in must not end theirs)."""
    try:
        device = await _undecided(user_code)
        if device is not None and not device["subject"]:
            await oauth.store.save_device(device | {"status": "denied", "ticket": ""})
    except claims.Busy:
        pass  # someone else changed it (their change decides), or Keeper is down


@router.post("/oauth/device/approve")
async def device_approve(request: Request) -> Response:
    f = await oauth.form_body(request)
    ticket = f.get("ticket", "")
    approved = f.get("decision") == "approve"
    try:
        device = await _undecided(normalize_user_code(f.get("user_code", "")))
        if (
            device is None
            or not device["ticket"]
            or not ticket
            or not secrets.compare_digest(device["ticket"], token_id(ticket))
        ):
            return _expired_page()
        dropped = "" if approved else device["upstream_refresh"]
        device |= {
            "status": "approved" if approved else "denied",
            "ticket": "",  # one use
            "family": new_jti() if approved else "",
            "upstream_refresh": device["upstream_refresh"] if approved else "",
        }
        await oauth.store.save_device(device)
    except claims.Busy:
        return _busy_page()  # a double submit (the other one decided), or Keeper is down
    await _revoke_all(dropped)
    await audit.emit(
        "auth.device_approved" if approved else "auth.device_denied",
        org=device["org"],
        namespace="-",
        actor=device["subject"],
        subject={
            "client_id": DEVICE_CLIENT_ID,
            "family": device["family"],
            "requested_from": device["address"],
            "approved_from": client_address(request),
        },
    )
    if not approved:
        return oauth.html_page(
            "Sign-in denied", "<h2>Denied</h2><p>The terminal will not be signed in.</p>"
        )
    return oauth.html_page(
        "Signed in",
        "<h2>You're signed in</h2><p>You can close this tab and return to the terminal.</p>",
    )


async def device_code_grant(f: dict[str, str]) -> dict[str, Any]:
    """The token endpoint's answer to a polling CLI (RFC 8628 §3.5)."""
    if f.get("client_id") != DEVICE_CLIENT_ID:
        raise oauth.OAuthError("invalid_client")
    device_code = f.get("device_code", "")
    try:
        device = await oauth.store.take_device(device_code)
    except CodeReplayed as e:
        raise await oauth.end_replayed_session(e, "device code") from None
    if device is None:
        raise oauth.OAuthError("invalid_grant")
    if device["exp"] < int(time.time()):
        raise oauth.OAuthError("expired_token")
    if device["status"] == "pending":
        # Counted only for a real grant, so made-up codes take no slot.
        if not _polls.allow(device["id"]):
            raise oauth.OAuthError("slow_down")
        raise oauth.OAuthError("authorization_pending")
    if device["status"] != "approved":
        raise oauth.OAuthError("access_denied")
    try:
        return await oauth.issue_tokens(device, DEVICE_CLIENT_ID, since=device["exp"] - _DEVICE_TTL)
    except oauth.OAuthError:
        raise  # a refusal stands
    except asyncio.CancelledError:  # cancelled at shutdown: the device code is the CLI's again
        await oauth.put_back(oauth.store.save_device(device))
        raise
    except Exception as e:  # Loom's own failure: unspend the code so the next poll is no replay
        oauth.log.warning("oauth: issuing tokens for a device grant failed: %s", e)
        await oauth.put_back(oauth.store.save_device(device))
        raise oauth.OAuthError("temporarily_unavailable", 503, headers={"Retry-After": "5"}) from e
