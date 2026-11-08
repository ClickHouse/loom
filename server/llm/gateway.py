"""Loom's own model calls through an inference gateway, with no provider key.

With `LOOM_INFERENCE_TOKEN_URL` set, Loom proves itself to the gateway with an
identity token and exchanges it there for an access token (RFC 8693), which
every chat and embedding call then carries as its bearer. The identity token is
either the workload's, a file its platform rotates (a projected
service-account token on Kubernetes), or one Loom signs itself
(`LOOM_INFERENCE_TOKEN_AUDIENCE`): a few minutes long, from issuer
`<LOOM_PUBLIC_BASE_URL>/v1` with the newest RS256 key of the signing keyring,
which `/v1/.well-known/jwks.json` publishes. Either way the gateway must trust
the issuer. Loom's own verifier never accepts that audience, so the token opens
nothing here. With `LOOM_INFERENCE_WORKLOAD_TOKEN_MODE=bearer` the workload's
token file is itself the bearer, for a gateway that verifies such tokens: read
at most once a minute, and again at once after the gateway answers 401. Unset,
the provider's key is the bearer, as always; `LOOM_OPENAI_BASE_URL` /
`LOOM_ANTHROPIC_BASE_URL` point either at a gateway, and
`LOOM_INFERENCE_GATEWAY_REQUIRED` (as a workload token does) refuses every call
to a host `LOOM_INFERENCE_GATEWAY_HOSTS` does not list.

With identity headers set (server/llm/identity.py) the gateway can refuse one
identity. An answer from a gateway host that `LOOM_INFERENCE_IDENTITY_REFUSALS_JSON`
lists, matched exactly by its status and its error body's code, type or message,
is that identity's, never the provider's failure: one under "refused" raises
`ModelCallRefused`, one under "wait" `ModelCallDeferred`. Neither is retried or
marks a provider failing. A background job's calls for that identity, to the same
path and model, are held for `LOOM_INFERENCE_REFUSAL_COOLDOWN_SECONDS` (a refusal)
or the answer's `Retry-After` (a wait), and refused before anything is sent
(`sending`); other identities' calls, and its own to other models, go on. A
success there ends the hold. Any other answer is the provider's, as always.
"""

from __future__ import annotations

import asyncio
import json
import logging
import math
import time
from collections.abc import Mapping
from dataclasses import dataclass
from email.utils import parsedate_to_datetime
from pathlib import Path
from typing import Any

import httpx

from server.clients._http import refuse_off_gateway
from server.core.errors import LoomServerError, ModelCallDeferred, ModelCallRefused
from server.settings import settings
from server.settings.catalog import IDENTITY_REFUSAL_FIELDS, url_host

log = logging.getLogger(__name__)

TOKEN_EXCHANGE = "urn:ietf:params:oauth:grant-type:token-exchange"
# How long an identity token Loom signs itself lives: it is exchanged at once.
IDENTITY_TOKEN_LIFE_S = 300  # not a setting: a longer life only widens a stolen token's use
# Its subject: the deployment, which the gateway's trust entry attributes spend to.
IDENTITY_SUBJECT = "loom"
_MAX_LIFE_S = 86400
# Renewed this long before it runs out (halfway, for a token living under ten
# minutes); on a failed renewal the token in hand serves until it runs out,
# and the next try waits this long (with no token in hand too).
_RENEW_BEFORE_S = 300
_RETRY_S = 30

_token = ""
_expires_at = 0.0
_renew_at = 0.0
_lock = asyncio.Lock()

# The bearer mode's token, as last read from its file (time.monotonic), and whether
# the gateway answered 401 since.
_REREAD_S = 60.0  # not a setting: a platform rotates the file well before the token ends
_file_token = ""
_file_read_at = 0.0
_file_refused = False

# A call's identity, as the gateway judges it: each identity header's value.
Identity = tuple[tuple[str, str], ...]
# Where a call goes: its path and the model its body names.
Route = tuple[str, str, str]


@dataclass(frozen=True)
class _Hold:
    """What the gateway answered for one identity at one route, and until when
    (time.monotonic) a job's calls there are held."""

    until: float
    status: int
    code: str
    wait: bool


_held: dict[Identity, dict[Route, _Hold]] = {}
_HELD_MAX = 10_000  # not a setting: bounds memory; an identity dropped is asked once more
# A "wait" answer's hold without a Retry-After, and the most of one that is kept.
_WAIT_S = 5.0  # not a setting: the common gateway default
_WAIT_MAX_S = 300.0  # not a setting: a longer wait is asked about again after it
_LOG_EVERY_S = 60.0  # not a setting: how often a refusal's log line repeats
_logged_at: dict[tuple[int, str], float] = {}
# A request's call (never a job's) asked to wait this long or less waits it out once in
# process and lets its sender ask again, as Loom retried a busy provider before; a
# second such answer for the same call defers it. Calls that waited, by identity,
# route and body, when (time.monotonic).
_WAIT_IN_PROCESS_S = 2.0  # not a setting: a wait a person's request can absorb


class RetryIdentityCall(Exception):
    """A replayable model request may retry one short identity wait."""

    def __init__(self, response: httpx.Response, seconds: float) -> None:
        self.response = response
        self.seconds = seconds


_sleep = asyncio.sleep


class GatewayUnavailable(httpx.TransportError):
    """No gateway token could be had and none is in hand: an outage, retried
    and fallen back from as any transport failure is."""


def exchanging() -> bool:
    return bool(settings.inference_token_url)


def bearing() -> bool:
    """Whether the workload token file is itself the bearer (the bearer mode)."""
    return settings.inference_workload_token_mode == "bearer"


def keyless() -> bool:
    """Whether a workload token stands in for the provider key: exchanged at the
    gateway, or sent to it as it is."""
    return exchanging() or bearing()


async def bearer(provider_key: str, url: str) -> str:
    """What a model call to `url` presents: the workload token in the bearer
    mode, the gateway's token when Loom exchanges an identity token there, else
    the provider key. A host off the gateway is refused here, before a token is
    fetched for it, and on every hop by the model client
    (`server.clients._http._on_request`)."""
    refuse_off_gateway(url)
    if bearing():
        return await _workload_bearer()
    if not exchanging():
        return provider_key
    now = time.time()
    if now < _renew_at and _token and now < _expires_at:  # a retry pause never outlasts it
        return _token
    async with _lock:
        if time.time() >= _renew_at:  # 0 at first: the first call renews
            await _renew()
    if not _token or time.time() >= _expires_at:
        raise GatewayUnavailable("the inference gateway's token could not be renewed")
    return _token


async def _renew() -> None:
    global _token, _expires_at, _renew_at
    from server.clients._http import get_model_client as http

    try:
        form = {
            "grant_type": TOKEN_EXCHANGE,
            "subject_token": await _identity_token(),
            "subject_token_type": "urn:ietf:params:oauth:token-type:jwt",
        }
        if settings.inference_token_scope:
            form["scope"] = settings.inference_token_scope
        # Never followed: a redirect would carry the identity token elsewhere.
        r = await http().post(
            settings.inference_token_url,
            data=form,
            timeout=settings.inference_token_exchange_timeout_s,
            follow_redirects=False,
        )
        if r.status_code != 200:
            raise ValueError(f"{r.status_code} {_error_code(r)}".strip())
        body = r.json()
        token, life = body["access_token"], float(body.get("expires_in", 900))
        if not isinstance(token, str) or not token:
            raise ValueError("no access_token")
        if not math.isfinite(life) or life <= 0:
            raise ValueError(f"expires_in {life}")
        life = min(life, _MAX_LIFE_S)  # kept no longer than a day, whatever it says
    except (
        OSError,
        httpx.HTTPError,
        ValueError,
        KeyError,
        TypeError,
        AttributeError,
        LoomServerError,
    ) as e:
        # The gateway's status and error code, never a token.
        log.warning("inference gateway token exchange failed: %s", e)
        _renew_at = time.time() + _RETRY_S  # the token in hand serves until it runs out
        return
    now = time.time()
    _token, _expires_at = token, now + life
    _renew_at = _expires_at - min(_RENEW_BEFORE_S, life / 2)


async def _workload_bearer() -> str:
    """The bearer mode's token: the file's content, read again once a minute has
    passed or the gateway answered 401 to it. A failed read serves the token in
    hand, tried again a minute later; with none in hand the call is an outage,
    and the next call reads again."""
    global _file_token, _file_read_at, _file_refused
    if _file_token and not _file_refused and time.monotonic() - _file_read_at < _REREAD_S:
        return _file_token
    async with _lock:
        if not _file_token or _file_refused or time.monotonic() - _file_read_at >= _REREAD_S:
            try:
                text = await asyncio.to_thread(
                    Path(settings.inference_workload_token_file).read_text
                )
                if not text.strip():
                    raise ValueError("the file is empty")
                _file_token = text.strip()
            except (OSError, ValueError) as e:
                # Its error, never the token.
                log.warning("inference workload token file could not be read: %s", e)
            _file_read_at, _file_refused = time.monotonic(), False
    if not _file_token:
        raise GatewayUnavailable("the inference workload token file could not be read")
    return _file_token


async def sending(request: httpx.Request) -> None:
    """Before a model call goes out: a job's call (`customer.for_org`) for an
    identity, path and model the gateway refused or asked to wait, still held,
    raises that answer's error and sends nothing, so the job goes on with other
    work. A request's call always asks the gateway. Runs before every request of
    the model client (`server.clients._http`)."""
    if not _held or not _judges_identities(request):
        return
    from server.llm import customer

    if not customer.job_call():
        return
    identity = identity_of(request.headers)
    routes = _held.get(identity)
    if not routes:
        return
    route = _route(request)
    hold = routes.get(route)
    if hold is None:
        return
    left = hold.until - time.monotonic()
    if left <= 0:
        _release(identity, route)
        return
    what = "asked to wait" if hold.wait else "refused"
    raise _held_error(
        hold.status,
        hold.code,
        hold.wait,
        left,
        f"the inference gateway {what} this identity's calls here: {math.ceil(left)} s left",
    )


async def answered(response: httpx.Response) -> None:
    """What the gateway's answer to a model call says. In the bearer mode a 401
    makes the next call read the token file again at once. An answer
    LOOM_INFERENCE_IDENTITY_REFUSALS_JSON lists is the call's identity's: one
    under "refused" raises `ModelCallRefused` and holds that identity's job calls
    to the same path and model for LOOM_INFERENCE_REFUSAL_COOLDOWN_SECONDS, one
    under "wait" `ModelCallDeferred`, held for its Retry-After; a request's call asked
    to wait two seconds or less waits once here and its sender asks again
    in the model client. A success ends the hold there. Runs on every response
    of the model client."""
    global _file_refused
    status = response.status_code
    if status == 401 and bearing():
        _file_refused = True
    if not _judges_identities(response.request):
        return
    if status < 300:
        identity = identity_of(response.request.headers) if _held else ()
        if identity in _held:
            _release(identity, _route(response.request))
        return
    table = settings.identity_refusals()
    if not any(e["status"] == status for entries in table.values() for e in entries):
        return  # the provider's answer, as always: read by the sender
    await response.aread()
    fields = _error_fields(response)
    for kind, entries in table.items():
        for entry in entries:
            if entry["status"] == status and all(
                fields.get(f) == entry[f] for f in IDENTITY_REFUSAL_FIELDS if f in entry
            ):
                if kind == "wait":
                    from server.llm import customer

                    seconds = _retry_after(response)
                    if (
                        not customer.job_call()
                        and seconds <= _WAIT_IN_PROCESS_S
                        and not response.request.extensions.get("loom_identity_retried")
                    ):
                        try:
                            _ = response.request.content
                        except httpx.RequestNotRead:
                            pass
                        else:
                            raise RetryIdentityCall(response, seconds)
                raise _hold(response, entry, wait=kind == "wait")


def _judges_identities(request: httpx.Request) -> bool:
    """Whether `request` is a model call to the gateway naming an identity: the
    identity headers set, a host it lists, and not the token exchange (Loom's own)."""
    if (
        not settings.identity_headers()
        or url_host(str(request.url)) not in settings.gateway_hosts()
    ):
        return False
    return not (exchanging() and request.url == httpx.URL(settings.inference_token_url))


def _error_fields(response: httpx.Response) -> dict[str, str]:
    """The error body's code, type and message, as the gateway wrote them."""
    try:
        data = response.json()
    except ValueError:  # not JSON: nothing to match
        return {}
    err = data.get("error") if isinstance(data, dict) else None
    if isinstance(err, str):
        return {"message": err}
    if not isinstance(err, dict):
        return {}
    out: dict[str, str] = {}
    for name in IDENTITY_REFUSAL_FIELDS:
        value = err.get(name)
        if isinstance(value, str) or (isinstance(value, int) and not isinstance(value, bool)):
            out[name] = str(value)
    return out


def _route(request: httpx.Request) -> Route:
    """The path `request` goes to and the model its JSON body names ('' for none)."""
    try:
        body = json.loads(request.content or b"null")
    except (ValueError, httpx.RequestNotRead):  # not JSON, or streamed: no model to read
        body = None
    model = body.get("model") if isinstance(body, dict) else None
    origin = str(request.url.copy_with(path="", query=None, fragment=None))
    return origin, request.url.path, model if isinstance(model, str) else ""


def _hold(response: httpx.Response, entry: Mapping[str, Any], *, wait: bool) -> ModelCallRefused:
    """The answer's error, and the hold of its identity at its route started;
    logged once a minute per answer."""
    request, status = response.request, response.status_code
    code = str(entry.get("code") or entry.get("type") or "")
    seconds = _retry_after(response) if wait else settings.inference_refusal_cooldown_seconds
    if seconds > 0:
        if len(_held) >= _HELD_MAX:
            _held.clear()
        hold = _Hold(time.monotonic() + seconds, status, code, wait)
        _held.setdefault(identity_of(request.headers), {})[_route(request)] = hold
    now = time.monotonic()
    if now - _logged_at.get((status, code), -_LOG_EVERY_S) >= _LOG_EVERY_S:
        _logged_at[(status, code)] = now
        # The status and code, never the identity or the gateway's text: either can
        # name a person.
        log.warning(
            "inference gateway %s a model call for its identity (%d %s); "
            "jobs hold that identity's calls to the same model for %.0f s",
            "asked to wait" if wait else "refused",
            status,
            code or "no code",
            seconds,
        )
    what = "asked this call to wait" if wait else "refused this call"
    # The status and code, never the gateway's text: job rows store this message.
    said = " ".join(filter(None, (str(status), code)))
    return _held_error(status, code, wait, seconds, f"the inference gateway {what} ({said})")


def _held_error(
    status: int, code: str, wait: bool, seconds: float, message: str
) -> ModelCallRefused:
    if wait:
        return ModelCallDeferred(
            message,
            gateway_status=status,
            gateway_code=code,
            retry_after_s=max(1, math.ceil(seconds)),
            hold_s=seconds,
        )
    return ModelCallRefused(message, gateway_status=status, gateway_code=code, hold_s=seconds)


def _retry_after(response: httpx.Response) -> float:
    """The answer's Retry-After in seconds (delta-seconds or an HTTP date), at most
    `_WAIT_MAX_S`; `_WAIT_S` without a usable one."""
    value = (response.headers.get("retry-after") or "").strip()
    seconds = _WAIT_S
    if value.isdigit():
        seconds = float(value)
    elif value:
        try:
            seconds = parsedate_to_datetime(value).timestamp() - time.time()
        except (TypeError, ValueError):
            seconds = _WAIT_S
    return min(max(seconds, 0.0), _WAIT_MAX_S)


def _release(identity: Identity, route: Route) -> None:
    routes = _held.get(identity)
    if routes is not None:
        routes.pop(route, None)
        if not routes:
            _held.pop(identity, None)


def identity_of(headers: Mapping[str, str]) -> Identity:
    """The identity a call names, as the gateway judges it: each identity header's value."""
    names = [name for name, _ in settings.identity_headers()]
    if settings.inference_customer_header:
        names.append(settings.inference_customer_header)
    return tuple((name, headers.get(name) or "") for name in names)


async def _identity_token() -> str:
    """What Loom exchanges: the workload's token from its file, else one it
    signs itself."""
    from server.auth import keyring

    if settings.inference_workload_token_file:
        workload = await asyncio.to_thread(Path(settings.inference_workload_token_file).read_text)
        return workload.strip()
    if not keyring.loaded() or keyring.stale():
        # A CLI command calling models (`loom reembed`) runs no refresh loop, and
        # a server's first model call can come before the loop's first read.
        await keyring.refresh()
    return signed_identity_token()


def signed_identity_token() -> str:
    """A short identity token for the exchange, signed with the newest RS256
    key. Raises ValueError when there is none (an HS256 secret is this
    deployment's alone, and the gateway could not verify it), and
    BackendUnavailableError while the keyring is not loaded or is stale."""
    import jwt as pyjwt

    from server.auth import keyring
    from server.auth.jwt import new_jti

    key = keyring.rs256_signing_key()
    if key is None:
        raise ValueError(
            "no RS256 signing key to sign the identity token: run "
            "`loom signing-key rotate --algorithm RS256` (or make LOOM_JWT_SECRET an RSA key)"
        )
    now = int(time.time())
    claims = {
        "iss": settings.inference_issuer(),
        "sub": IDENTITY_SUBJECT,
        "aud": settings.inference_token_audience,
        "iat": now,
        "nbf": now,
        "exp": now + IDENTITY_TOKEN_LIFE_S,
        "jti": new_jti(),
    }
    return pyjwt.encode(claims, key.secret, algorithm="RS256", headers={"kid": key.kid})


def _error_code(r: httpx.Response) -> str:
    """The OAuth error code of a refusal; '' for a page that is not one."""
    try:
        code = r.json().get("error", "")
    except (ValueError, AttributeError):
        return ""
    return code if isinstance(code, str) else ""


def _reset_for_tests() -> None:
    global _token, _expires_at, _renew_at, _file_token, _file_read_at, _file_refused
    _token, _expires_at, _renew_at = "", 0.0, 0.0
    _file_token, _file_read_at, _file_refused = "", 0.0, False
    _held.clear()
    _logged_at.clear()
