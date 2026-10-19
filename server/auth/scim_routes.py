"""SCIM 2.0 (RFC 7643/7644) under /scim/v2: what an identity provider needs to
deactivate and reactivate people (`server/auth/scim.py`).

Mounted only when `LOOM_SCIM_TOKEN` is set, and that bearer token is the only
credential these routes take. A user resource carries `userName`, its email
(the `emails` entry marked primary, else the work one, else the first, else
`userName`) and `active`; other attributes a provider sends are accepted and
not kept.
"""

from __future__ import annotations

import hmac
import json
import logging
import re
from collections.abc import AsyncIterator, Callable, Coroutine
from contextlib import asynccontextmanager
from typing import Any

from clickhouse_connect.driver.exceptions import ClickHouseError
from fastapi import APIRouter, Depends, Request, Response
from fastapi.responses import JSONResponse
from fastapi.routing import APIRoute

from server.auth import scim
from server.auth.public_url import public_base_url
from server.core.ch import set_query_tag
from server.core.errors import LoomServerError
from server.settings import settings

SCIM_JSON = "application/scim+json"
USER_SCHEMA = "urn:ietf:params:scim:schemas:core:2.0:User"
_LIST_SCHEMA = "urn:ietf:params:scim:api:messages:2.0:ListResponse"
_ERROR_SCHEMA = "urn:ietf:params:scim:api:messages:2.0:Error"
_CONFIG_SCHEMA = "urn:ietf:params:scim:schemas:core:2.0:ServiceProviderConfig"
_MAX_RESULTS = 200
_LAST_INDEX = 2**32 - 1
# A shorter bearer token is guessable: SCIM refuses to start with one.
MIN_TOKEN_LENGTH = 32

log = logging.getLogger(__name__)


class ScimError(Exception):
    """An RFC 7644 §3.12 error response."""

    def __init__(
        self, status: int, detail: str, scim_type: str = "", headers: dict[str, str] | None = None
    ) -> None:
        super().__init__(detail)
        self.status, self.detail, self.scim_type = status, detail, scim_type
        self.headers = headers or {}


async def _authorized(request: Request) -> None:
    set_query_tag(f"scim.{request.method.lower()}")  # these routes are not under the transports
    scheme, _, token = request.headers.get("authorization", "").partition(" ")
    presented = token.strip().encode()
    # Every token is compared, so the time taken doesn't say which one matched.
    matches = [hmac.compare_digest(presented, t.encode()) for t in scim.tokens(settings.scim_token)]
    if scheme.lower() != "bearer" or not any(matches):
        raise ScimError(401, "a valid SCIM bearer token is required")


def _answer(body: dict[str, Any], status: int = 200, **headers: str) -> JSONResponse:
    return JSONResponse(body, status_code=status, media_type=SCIM_JSON, headers=headers or None)


def _error_response(e: ScimError) -> JSONResponse:
    body: dict[str, Any] = {"schemas": [_ERROR_SCHEMA], "status": str(e.status)}
    if e.scim_type:
        body["scimType"] = e.scim_type
    body["detail"] = e.detail
    headers = {**e.headers, **({"WWW-Authenticate": "Bearer"} if e.status == 401 else {})}
    if e.status == 503:
        headers["Retry-After"] = "5"
    return _answer(body, e.status, **headers)


class _ScimRoute(APIRoute):
    """Answers, as RFC 7644 errors, what these routes and their bearer check
    raise: a SCIM refusal, a busy user, any of Loom's own errors (a 5xx as a
    retryable 503) and anything else (a store error) as a 503. Unknown paths
    and methods under /scim/v2 reach `unknown` below. The rest of the server's
    error format is left alone."""

    def get_route_handler(self) -> Callable[[Request], Coroutine[Any, Any, Response]]:
        handler = super().get_route_handler()

        async def run(request: Request) -> Response:
            try:
                return await handler(request)
            except ScimError as e:
                return _error_response(e)
            except scim.Busy:
                return _error_response(ScimError(503, "this user is being changed; retry"))
            except LoomServerError as e:
                if e.status_code >= 500:  # the store, overloaded or timed out: retryable
                    return _error_response(ScimError(503, f"{e.code.lower()}; retry"))
                return _error_response(ScimError(e.status_code, str(e) or e.code.lower()))
            except Exception:  # logged: a provider needs a SCIM error to retry on
                log.exception("scim: %s %s failed", request.method, request.url.path)
                return _error_response(ScimError(503, "the request failed; retry"))

        return run


router = APIRouter(prefix="/scim/v2", dependencies=[Depends(_authorized)], route_class=_ScimRoute)


# ── parsing ───────────────────────────────────────────────────────────────

# The filters served: `userName eq "x"`, and on the email, what Entra sends when
# it matches users on `mail`: `emails[type eq "work"].value eq "x"` or `emails.value eq "x"`.
_FILTER = re.compile(
    r'\s*(userName|emails(?:\[\s*type\s+eq\s+"work"\s*\])?\.value)\s+eq\s+"((?:[^"\\]|\\.)*)"\s*',
    re.IGNORECASE,
)


def parse_filter(text: str) -> tuple[str, str]:
    """What a served filter compares and its value: ("userName", v) or ("email", v)."""
    m = _FILTER.fullmatch(text)
    if m:
        try:
            value = json.loads(f'"{m.group(2)}"')
        except ValueError:
            value = None
        if isinstance(value, str):
            return ("userName" if m.group(1).lower() == "username" else "email"), value
    raise ScimError(
        400,
        'the filters served are userName eq "<v>", and emails[type eq "work"].value eq '
        '"<email>" or emails.value eq "<email>"',
        "invalidFilter",
    )


def parse_active(value: Any) -> bool:
    """A boolean, or the strings "true"/"false" some providers send."""
    if isinstance(value, bool):
        return value
    if isinstance(value, str) and value.strip().lower() in ("true", "false"):
        return value.strip().lower() == "true"
    raise ScimError(400, "active must be a boolean", "invalidValue")


# A path may name its schema: `urn:…:core:2.0:User:active` is `active`.
_USER_URN = USER_SCHEMA.lower() + ":"


def _attributes(op: Any) -> list[tuple[str, Any]]:
    """The (attribute, value) pairs an operation sets, lower-cased and without
    the User schema prefix: its path's, or each key of a path-less value's."""
    if not isinstance(op, dict):
        raise ScimError(400, "each operation must be an object", "invalidSyntax")
    path, value = str(op.get("path") or ""), op.get("value")
    pairs = list(value.items()) if not path and isinstance(value, dict) else [(path, value)]
    return [(str(k).lower().removeprefix(_USER_URN), v) for k, v in pairs]


def _replaces(op: dict[str, Any]) -> bool:
    return str(op.get("op") or "").lower() in ("add", "replace")


def patched_active(body: dict[str, Any]) -> bool | None:
    """What a PatchOp sets `active` to, or None when it leaves it alone.
    Operations on other attributes are accepted and dropped."""
    ops = body.get("Operations")
    if not isinstance(ops, list):
        raise ScimError(400, "a PatchOp needs Operations", "invalidSyntax")
    active = None
    for op in ops:
        if not isinstance(op, dict):
            continue  # refused by `patched_names`, once an `active` beside it is known
        for attr, value in _attributes(op):
            if attr != "active":
                continue
            if not _replaces(op):
                raise ScimError(400, "active can only be replaced", "invalidSyntax")
            active = parse_active(value)
    return active


def _sets_inactive(body: dict[str, Any]) -> bool:
    """Whether any operation of a PatchOp sets `active` to false, whatever else fails."""
    ops = body.get("Operations")
    if not isinstance(ops, list):
        return False
    for op in ops:
        if not isinstance(op, dict) or not _replaces(op):
            continue
        for attr, value in _attributes(op):
            try:
                if attr == "active" and parse_active(value) is False:
                    return True
            except ScimError:
                continue
    return False


def patched_names(
    body: dict[str, Any], row: dict[str, Any]
) -> tuple[str | None, dict[str, Any] | None]:
    """The userName a PatchOp sets, and the emails entry it leaves `row` keyed
    on when it touches that one; each None when it does not. Other addresses
    may be added, changed or removed. Call after `patched_active`, which
    checks the shape."""
    user_name, entry = None, None
    for op in body["Operations"]:
        pairs = _attributes(op)  # refuses an operation that is not an object
        kind = str(op.get("op") or "").lower()
        if kind not in ("add", "remove", "replace"):
            raise ScimError(400, "op must be add, remove or replace", "invalidSyntax")
        if not op.get("path"):  # RFC 7644 §3.5.2: a remove needs a target, a write an object
            if kind == "remove":
                raise ScimError(400, "a remove needs a path", "noTarget")
            if not isinstance(op.get("value"), dict):
                raise ScimError(400, "a write with no path needs an object value", "invalidSyntax")
        for attr, value in pairs:
            if attr == "username":
                if kind == "remove":
                    raise ScimError(400, "the userName cannot be removed", "mutability")
                if not isinstance(value, str) or not value.strip():
                    raise ScimError(400, "userName must be a non-empty string", "invalidValue")
                user_name = value
            elif attr.startswith("emails"):
                entry = _keyed_written(kind, attr, value, row) or entry
    return user_name, entry


def _keyed_written(kind: str, attr: str, value: Any, row: dict[str, Any]) -> dict[str, Any] | None:
    """The entry an `emails…` operation leaves the user keyed on, when it
    writes the keyed one or one that takes its place; None when it leaves it
    alone. Removing the keyed entry, or not saying what replaces it, is refused."""
    stored = {"value": row["email"], "type": row["email_type"], "primary": row["email_primary"]}
    # An email the userName gave is outranked by any entry (`user_name_and_email`).
    here = -1 if row["email_from_user_name"] else _rank(stored)
    if attr == "emails":
        if kind == "remove":
            raise _no_change("the email cannot be removed")
        best = keyed_entry(value if isinstance(value, list) else [value])
        if kind == "add":  # appends (RFC 7644 §3.5.2.1): an entry that outranks counts,
            # and a primary one always, as only one entry may be primary (RFC 7643 §2.4)
            if best and (_rank(best) > here or _rank(best) == 2):
                return best
            return None
        if best is None:
            raise _no_change("the email cannot be removed")
        return best
    filt: str | None = None  # no filter: `emails.value` writes every entry (RFC 7644 §3.5.2.3)
    if "[" not in attr and "]" not in attr:
        sub = attr.removeprefix("emails")
    else:
        opening, closing = attr.find("["), attr.find("]")
        if attr.count("[") != 1 or attr.count("]") != 1 or closing < opening:
            raise ScimError(400, "an emails path is malformed", "invalidPath")
        filt, sub = attr[opening + 1 : closing], attr[closing + 1 :]
    if sub not in ("", ".value", ".type", ".primary"):
        return None  # `emails.display` and the like
    if sub == "":
        if kind != "remove" and not isinstance(value, dict):
            raise ScimError(400, "an emails entry must be an object", "invalidValue")
        written = dict(value) if isinstance(value, dict) else {}
    else:
        written = {sub[1:]: value}
    # Every entry (no filter), the keyed one, or a filter we cannot read.
    if filt is None or _selected(filt, stored) is not False:
        if kind == "remove":
            raise _no_change("the email cannot be removed")
        entry = {**stored, **written}
        if _rank(entry) < here:
            raise _no_change("the email's entry cannot lose its place")
        return entry
    if kind == "remove":
        return None
    entry = {**_criteria(filt), **written}
    if _rank(entry) <= here and _rank(entry) < 2:  # only one entry may be primary
        return None  # another address: not the one sign-in carries
    if not isinstance(entry.get("value"), str):
        raise _no_change("an entry would take the email's place")
    return entry


# One `attr eq value` comparison: what a provider puts in an `emails[…]` filter.
_COMPARISON = re.compile(r'\s*(type|value|primary)\s+eq\s+(?:"([^"]*)"|(true|false))\s*')


def _comparison(filt: str) -> tuple[str, str | bool] | None:
    """An `emails[…]` filter as (attribute, value), `primary` as a bool; None
    when it is not one comparison."""
    m = _COMPARISON.fullmatch(filt)
    if not m:
        return None
    attr, text, flag = m.groups()
    want = text if text is not None else flag
    return attr, scim.is_primary(want) if attr == "primary" else want


def _selected(filt: str, stored: dict[str, Any]) -> bool | None:
    """Whether an `emails[…]` filter selects the keyed entry; None when it is
    not one comparison."""
    comparison = _comparison(filt)
    if comparison is None:
        return None
    attr, want = comparison
    if attr == "primary":
        return want == scim.is_primary(stored["primary"])
    return scim.normalized(str(want)) == scim.normalized(str(stored[attr]))


def _criteria(filt: str) -> dict[str, Any]:
    """What a filter says of the entry it selects (`{"type": "other"}`)."""
    comparison = _comparison(filt)
    return {} if comparison is None else {comparison[0]: comparison[1]}


def _no_change(detail: str) -> ScimError:
    return ScimError(400, detail, "mutability")


def _int_param(request: Request, name: str, default: int) -> int:
    raw = request.query_params.get(name)
    if raw is None:
        return default
    try:
        return int(raw)
    except ValueError:
        raise ScimError(400, f"{name} must be an integer", "invalidValue") from None


async def _body(request: Request) -> dict[str, Any]:
    try:
        body = json.loads(await request.body())
    except (ValueError, RecursionError):  # not JSON, or nested too deep to parse
        raise ScimError(400, "the body is not JSON", "invalidSyntax") from None
    if not isinstance(body, dict):
        raise ScimError(400, "the body must be a JSON object", "invalidSyntax")
    return body


def _rank(entry: dict[str, Any]) -> int:
    """Which emails entry Loom keys on: the primary, else the work one, else the first."""
    if scim.is_primary(entry.get("primary")):
        return 2
    return 1 if str(entry.get("type") or "").lower() == "work" else 0


def _nonempty(value: Any) -> bool:
    return isinstance(value, str) and bool(value.strip())


def keyed_entry(emails: Any) -> dict[str, Any] | None:
    """The entry of a SCIM `emails` list Loom keys on (`_rank`), or None."""
    entries = (
        [e for e in emails if isinstance(e, dict) and _nonempty(e.get("value"))]
        if isinstance(emails, list)
        else []
    )
    return max(entries, key=_rank, default=None)  # the first of the highest rank


def user_name_and_email(body: dict[str, Any]) -> tuple[str, str, dict[str, Any]]:
    """The userName, the email SSO sign-in will carry (the keyed `emails`
    entry's, else the userName) and that entry ({} for the userName)."""
    name = body.get("userName")
    if not isinstance(name, str) or not name.strip():
        raise ScimError(400, "userName is required", "invalidValue")
    entry = keyed_entry(body.get("emails")) or {}
    email = scim.normalized(entry.get("value") or name)
    if "@" not in email:
        raise ScimError(400, "userName or an email must be the person's email", "invalidValue")
    return name.strip(), email, entry


# ── resources ─────────────────────────────────────────────────────────────


def _resource(request: Request, row: dict[str, Any]) -> dict[str, Any]:
    return {
        "schemas": [USER_SCHEMA],
        "id": row["id"],
        "userName": row["user_name"],
        "active": bool(row["active"]),
        "emails": [_email(row)],
        "meta": {
            "resourceType": "User",
            "created": row["created_at"].isoformat(),
            "lastModified": row["updated_at"].isoformat(),
            "location": f"{public_base_url(request)}/scim/v2/Users/{row['id']}",
        },
    }


def _email(row: dict[str, Any]) -> dict[str, Any]:
    """The keyed entry as stored (address and type lower-cased), with its type
    and primary flag, so a provider comparing it finds no change."""
    entry: dict[str, Any] = {"value": row["email"], "primary": bool(row["email_primary"])}
    if row["email_type"]:
        entry["type"] = row["email_type"]
    return entry


def _not_found() -> ScimError:
    return ScimError(404, "no such user")


@router.get("/ServiceProviderConfig")
async def service_provider_config() -> JSONResponse:
    return _answer(
        {
            "schemas": [_CONFIG_SCHEMA],
            "patch": {"supported": True},
            "bulk": {"supported": False, "maxOperations": 0, "maxPayloadSize": 0},
            "filter": {"supported": True, "maxResults": _MAX_RESULTS},
            "changePassword": {"supported": False},
            "sort": {"supported": False},
            "etag": {"supported": False},
            "authenticationSchemes": [
                {
                    "type": "oauthbearertoken",
                    "name": "Bearer token",
                    "description": "The deployment's LOOM_SCIM_TOKEN",
                    "primary": True,
                }
            ],
        }
    )


@router.get("/Users")
async def list_users(request: Request) -> JSONResponse:
    # Past the end is an empty page, however far: the offset must fit its UInt32.
    start = min(_LAST_INDEX, max(1, _int_param(request, "startIndex", 1)))
    count = min(_MAX_RESULTS, max(0, _int_param(request, "count", 100)))
    text = request.query_params.get("filter")
    if text is not None:
        attribute, value = parse_filter(text)
        row = await (scim.find(value) if attribute == "userName" else scim.get(scim.user_id(value)))
        total, rows = (1, [row]) if row else (0, [])
        rows = rows[start - 1 : start - 1 + count]
    else:
        total, rows = await scim.page(start, count)
    return _answer(
        {
            "schemas": [_LIST_SCHEMA],
            "totalResults": total,
            "startIndex": start,
            "itemsPerPage": len(rows),
            "Resources": [_resource(request, r) for r in rows],
        }
    )


@router.get("/Groups")
async def list_groups(request: Request) -> JSONResponse:
    """No groups: a provider's connection test and group sync read an empty
    list here, where a 404 would count as a broken endpoint."""
    start = min(_LAST_INDEX, max(1, _int_param(request, "startIndex", 1)))
    return _answer(
        {
            "schemas": [_LIST_SCHEMA],
            "totalResults": 0,
            "startIndex": start,
            "itemsPerPage": 0,
            "Resources": [],
        }
    )


@router.get("/Users/{uid}")
async def get_user(uid: str, request: Request) -> JSONResponse:
    row = await scim.get(uid)
    if row is None:
        raise _not_found()
    return _answer(_resource(request, row))


@router.post("/Users")
async def create_user(request: Request) -> JSONResponse:
    """Records the email; the Loom account comes from the person's first sign-in."""
    body = await _body(request)
    name, email, entry = user_name_and_email(body)
    # A null is unassigned (RFC 7643 §2.5): a new user is active.
    active = parse_active(True if body.get("active") is None else body["active"])
    try:
        row = await scim.create(name, email, active=active, email_entry=entry)
    except scim.Exists:
        raise ScimError(409, "a user with this email or userName exists", "uniqueness") from None
    resource = _resource(request, row)
    return _answer(resource, 201, Location=resource["meta"]["location"])


@router.put("/Users/{uid}")
async def replace_user(uid: str, request: Request) -> JSONResponse:
    body = await _body(request)
    if "id" in body and body["id"] != uid:
        raise ScimError(400, "the body names another user", "invalidValue")
    active = None if body.get("active") is None else parse_active(body["active"])
    row = await scim.get(uid)
    if row is None:
        raise _not_found()
    async with _deactivating_anyway(uid, active):
        name, email, entry = user_name_and_email(body)
        _refuse_another_email(row, email)
        new = await _update(uid, active, name, entry)
        if new is None:  # deleted since it was read
            raise _not_found()
        row = new
    return _answer(_resource(request, row))


@router.patch("/Users/{uid}")
async def patch_user(uid: str, request: Request) -> JSONResponse:
    body = await _body(request)
    try:
        active = patched_active(body)
    except ScimError:
        # Refused for another `active` op: one that does set false still deactivates.
        if _sets_inactive(body):
            await scim.update(uid, active=False)  # None for a user that is not there
        raise
    row = await scim.get(uid)
    if row is None:
        raise _not_found()
    async with _deactivating_anyway(uid, active):
        name, entry = patched_names(body, row)
        if entry is not None:
            _refuse_another_email(row, str(entry.get("value") or row["email"]))
        elif name is not None and row["email_from_user_name"]:
            _refuse_another_email(row, name)  # the email is the userName
        # Every PATCH converges, one of only dropped attributes too: an active
        # user lets go what no deactivated email reaches any more.
        new = await _update(uid, active, name, entry)
        if new is None:  # deleted since it was read
            raise _not_found()
        row = new
    return _answer(_resource(request, row))


@asynccontextmanager
async def _deactivating_anyway(uid: str, active: bool | None) -> AsyncIterator[None]:
    """Fail closed: a request refused for another attribute still deactivates.
    A provider batches a leaver's changes into one request and does not retry
    a 400, so dropping its `active: false` would leave them active."""
    try:
        yield
    except (scim.Busy, LoomServerError, ClickHouseError):
        raise  # busy, or the store failing (wrapped or raw): a retry here would fail again
    except Exception:  # re-raised: a refused or failed request must still deactivate
        if active is False:
            await scim.update(uid, active=False)
        raise


async def _update(
    uid: str, active: bool | None, name: str | None, entry: dict[str, Any] | None
) -> dict[str, Any] | None:
    try:
        return await scim.update(uid, active=active, user_name=name, email_entry=entry)
    except scim.Exists:
        raise ScimError(409, "another user has this userName", "uniqueness") from None


def _refuse_another_email(row: dict[str, Any], email: str) -> None:
    if scim.normalized(email) != row["email"]:
        # The id is the email's: another email is another person, and the old
        # one's deactivation would miss the account the new one signs in to.
        raise ScimError(400, "the email cannot change", "mutability")


@router.delete("/Users/{uid}")
async def delete_user(uid: str) -> Response:
    if not await scim.delete(uid):
        raise _not_found()
    return Response(status_code=204)


# Registered last, so it only answers what no route above matches: what a
# provider probes (`/Groups`, `/Schemas`), and a known path's other methods.
@router.api_route(
    "/{rest:path}",
    methods=["GET", "POST", "PUT", "PATCH", "DELETE"],
    include_in_schema=False,
)
async def unknown(rest: str, request: Request) -> Response:
    path = request.url.path
    allowed = sorted(
        {
            method
            for r in router.routes
            if isinstance(r, APIRoute) and r.endpoint is not unknown and r.path_regex.match(path)
            for method in r.methods or ()
        }
    )
    if allowed:
        raise ScimError(
            405, f"{request.method} is not served here", headers={"Allow": ", ".join(allowed)}
        )
    raise ScimError(
        404, "this server serves only Users, an empty Groups list and ServiceProviderConfig"
    )


def scim_settings_problem(raw: str, login_provider: str, auth_required: bool) -> str:
    """Why SCIM, with the `LOOM_SCIM_TOKEN` value `raw`, cannot start, or ''
    (SCIM off is fine): a guessable token, or sign-in that is not SSO or not
    required, where a deactivation would answer 200 and refuse nobody."""
    tokens = scim.tokens(raw)
    for token in tokens:
        if len(token) < MIN_TOKEN_LENGTH:
            return (
                f"a LOOM_SCIM_TOKEN token is {len(token)} characters; "
                f"each must be at least {MIN_TOKEN_LENGTH}"
            )
    if tokens and login_provider != "oidc":
        return (
            "LOOM_SCIM_TOKEN is set but LOOM_OAUTH_LOGIN_PROVIDER is not oidc: SCIM "
            "deactivates SSO sign-ins, and there are none"
        )
    if tokens and not auth_required:
        return "LOOM_SCIM_TOKEN is set but auth is off: no sign-in is checked, so none is refused"
    return ""


def require_scim_settings(raw: str, login_provider: str, auth_required: bool) -> None:
    """Refuse to start with SCIM that could not deactivate anyone (run in the lifespan)."""
    if problem := scim_settings_problem(raw, login_provider, auth_required):
        raise RuntimeError(f"refusing to start: {problem}")


def attach(app: Any) -> None:
    if not scim.tokens(settings.scim_token):
        return
    app.include_router(router)
