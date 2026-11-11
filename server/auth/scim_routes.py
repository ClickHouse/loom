"""SCIM 2.0 (RFC 7643/7644) under /scim/v2: what an identity provider needs to
deactivate and reactivate people (`server/auth/scim.py`).

Mounted only when `LOOM_SCIM_TOKEN` is set, and that bearer token is the only
credential these routes take. A user resource carries `userName`, the `emails`
list (Loom keys the user on one of the entries with an address: the one
marked primary, else the first work one, else the first; with none, on
`userName`) and `active`; other attributes a provider sends are accepted and
not kept.
"""

from __future__ import annotations

import hmac
import json
import logging
import re
from collections.abc import AsyncIterator, Callable, Coroutine, Iterable
from contextlib import asynccontextmanager
from datetime import datetime
from typing import Any

from clickhouse_connect.driver.exceptions import ClickHouseError
from fastapi import APIRouter, Depends, Request, Response
from fastapi.responses import JSONResponse
from fastapi.routing import APIRoute

from server.auth import scim
from server.auth.bearer import bearer_token
from server.auth.jwt import TokenError
from server.auth.public_url import public_base_url
from server.core.ch import set_query_tag
from server.core.datetimes import as_aware_utc
from server.core.errors import LoomServerError
from server.observability.metrics import record_auth_refusal
from server.settings import settings

SCIM_JSON = "application/scim+json"
USER_SCHEMA = "urn:ietf:params:scim:schemas:core:2.0:User"
LIST_SCHEMA = "urn:ietf:params:scim:api:messages:2.0:ListResponse"
_ERROR_SCHEMA = "urn:ietf:params:scim:api:messages:2.0:Error"
_CONFIG_SCHEMA = "urn:ietf:params:scim:schemas:core:2.0:ServiceProviderConfig"
MAX_RESULTS = 200
LAST_INDEX = 2**32 - 1
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
    message = "a valid SCIM bearer token is required"
    # Counted apart from Loom's own bearers: a provider holding a rotated token
    # needs a different fix from a storm of bad user tokens.
    try:
        token = bearer_token(request.headers.get("authorization"))
    except TokenError as e:
        record_auth_refusal(e, transport="scim")
        raise ScimError(401, message) from None
    if token is None:
        record_auth_refusal(None, transport="scim")
        raise ScimError(401, message)
    # Every token is compared, so the time taken doesn't say which one matched.
    presented = token.encode()
    matches = [hmac.compare_digest(presented, t.encode()) for t in scim.tokens(settings.scim_token)]
    if not any(matches):
        record_auth_refusal(TokenError(reason="invalid"), transport="scim")
        raise ScimError(401, message)


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


def scim_error(status: int, detail: str) -> JSONResponse:
    """An RFC 7644 error for a refusal made outside these routes (the cell's tenancy
    middleware), in the shape an identity provider parses."""
    return _error_response(ScimError(status, detail))


class ScimRoute(APIRoute):
    """Every error these routes and their bearer check raise answers as an
    RFC 7644 error, a server-side one as a retryable 503."""

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


router = APIRouter(prefix="/scim/v2", dependencies=[Depends(_authorized)], route_class=ScimRoute)


# ── parsing ───────────────────────────────────────────────────────────────

# The filters served: `userName eq "x"`, and on the email, what Entra sends when
# it matches users on `mail`: `emails[type eq "work"].value eq "x"` or `emails.value eq "x"`.
_FILTER = re.compile(
    r'\s*(userName|emails(?:\[\s*type\s+eq\s+"work"\s*\])?\.value)\s+eq\s+"((?:[^"\\]|\\.)*)"\s*',
    re.IGNORECASE,
)


def parse_filter(text: str) -> tuple[str, str]:
    """What a served filter compares and its value: ("userName", v), ("email", v)
    or ("work email", v)."""
    refused = ScimError(
        400,
        'the filters served are userName eq "<v>", and emails[type eq "work"].value eq '
        '"<email>" or emails.value eq "<email>"',
        "invalidFilter",
    )
    m = _FILTER.fullmatch(text)
    if not m:
        raise refused
    try:
        value: str = json.loads(f'"{m.group(2)}"')  # a quoted capture decodes to a string
    except ValueError:  # a bad escape or a control character
        raise refused from None
    attribute = m.group(1).lower()
    if attribute == "username":
        return "userName", value
    return ("email" if attribute == "emails.value" else "work email"), value


def parse_active(value: Any) -> bool:
    """A boolean, or the strings "true"/"false" some providers send."""
    if isinstance(value, bool):
        return value
    if isinstance(value, str) and value.strip().lower() in ("true", "false"):
        return value.strip().lower() == "true"
    raise ScimError(400, "active must be a boolean", "invalidValue")


# A path may name its schema: `urn:…:core:2.0:User:active` is `active`.
_USER_URN = USER_SCHEMA.lower() + ":"


def lowered(attrs: dict[str, Any]) -> dict[str, Any]:
    """`attrs` with its names lower-cased (RFC 7643 §2.1: they match in any
    case). Two names that differ only in case are one attribute sent twice."""
    out = {str(k).lower(): v for k, v in attrs.items()}
    if len(out) < len(attrs):
        raise ScimError(400, "an attribute is named twice", "invalidSyntax")
    return out


def _every(attrs: dict[str, Any], name: str) -> list[Any]:
    """The values of `attrs` named `name` in any case, each spelling's."""
    return [v for k, v in attrs.items() if str(k).lower() == name]


def patch_operations(body: dict[str, Any]) -> list[dict[str, Any]]:
    """A PatchOp's operations, each with its names lower-cased (RFC 7644: they
    match in any case); 400 unless `Operations` is a list of objects."""
    ops = _operations(body)
    if not isinstance(ops, list):
        raise ScimError(400, "a PatchOp needs Operations", "invalidSyntax")
    if not all(isinstance(op, dict) for op in ops):
        raise ScimError(400, "each operation must be an object", "invalidSyntax")
    return ops


def _operations(body: dict[str, Any]) -> Any:
    ops = lowered(body).get("operations")
    return (
        [lowered(op) if isinstance(op, dict) else op for op in ops]
        if isinstance(ops, list)
        else ops
    )


def _attributes(op: Any) -> list[tuple[str, Any]]:
    """The (attribute, value) pairs an operation sets, lower-cased and without
    the User schema prefix: its path's, or each key of a path-less value's."""
    if not isinstance(op, dict):
        raise ScimError(400, "each operation must be an object", "invalidSyntax")
    path, value = str(op.get("path") or ""), op.get("value")
    pairs = _pairs(path, value)
    if len({attr for attr, _ in pairs}) < len(pairs):  # in two cases, or with the schema prefix
        raise ScimError(400, "an attribute is named twice", "invalidSyntax")
    return pairs


def _pairs(path: str, value: Any) -> list[tuple[str, Any]]:
    pairs = list(value.items()) if not path and isinstance(value, dict) else [(path, value)]
    return [(_lower_names(str(k)).removeprefix(_USER_URN), v) for k, v in pairs]


def _lower_names(path: str) -> str:
    """`path` with its attribute names lower-cased, and a filter's values as sent."""
    opening, closing = path.find("["), path.rfind("]")
    if opening < 0 or closing < opening:
        return path.lower()
    return path[:opening].lower() + path[opening : closing + 1] + path[closing + 1 :].lower()


def _replaces(op: dict[str, Any]) -> bool:
    return str(op.get("op") or "").lower() in ("add", "replace")


def patched_active(body: dict[str, Any]) -> bool | None:
    """What a PatchOp sets `active` to, or None when it leaves it alone.
    Operations on other attributes are accepted and dropped."""
    ops = _operations(body)
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
    """Whether any operation of a PatchOp sets `active` to false, whatever else
    fails: an attribute named twice too, in any of its spellings."""
    for ops in _every(body, "operations"):
        for op in ops if isinstance(ops, list) else []:
            if not isinstance(op, dict):
                continue
            if not any(str(kind).lower() in ("add", "replace") for kind in _every(op, "op")):
                continue
            for path in [str(p or "") for p in _every(op, "path")] or [""]:
                for value in _every(op, "value"):
                    if any(a == "active" and _is_false(v) for a, v in _pairs(path, value)):
                        return True
    return False


def _is_false(value: Any) -> bool:
    try:
        return parse_active(value) is False
    except ScimError:
        return False


def patched_names(
    body: dict[str, Any], row: dict[str, Any]
) -> tuple[str | None, list[dict[str, Any]] | None]:
    """The userName a PatchOp sets and the `emails` list it leaves, each None
    when it does not touch it: its operations applied in order, each to what
    the ones before it left (RFC 7644 §3.5.2). Call after `patched_active`,
    which checks the shape."""
    user_name, emails = None, None
    for op in _operations(body):
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
                before = scim.emails_of(row) if emails is None else emails
                emails = _patched_emails(kind, attr, value, before)
    return user_name, emails


def _patched_emails(
    kind: str, attr: str, value: Any, emails: list[dict[str, Any]]
) -> list[dict[str, Any]]:
    """`emails` after one operation: on the list (`emails`), on a sub-attribute
    of every entry (`emails.type`), or on the entries a filter selects
    (`emails[type eq "work"]`, `emails[type eq "work"].value`)."""
    filt, sub = _emails_path(attr)
    comparison = None if filt is None else _comparison(filt)
    if sub not in ("", "value", "type", "primary"):
        return emails  # `emails.display` and the like
    if comparison is None and not sub:
        if kind == "remove":  # a value names the entries to drop (as Entra removes members)
            if value is None:
                return []
            named = [_naming(n) for n in _listed(value)]
            return [e for e in emails if not any(_names(n, e) for n in named)]
        written = _entries(_listed(value or None))  # a null (or falsy) one is none
        if kind == "replace":
            return _one_primary(written, range(len(written)))
        return _added(emails, written)
    picked = [i for i, e in enumerate(emails) if comparison is None or _matches(comparison, e)]
    if kind == "remove":
        if not sub:
            return [e for i, e in enumerate(emails) if i not in picked]
        blank = {"value": "", "type": "", "primary": False}[sub]
        return [{**e, sub: blank} if i in picked else e for i, e in enumerate(emails)]
    if not sub and not isinstance(value, dict):
        raise ScimError(400, "an emails entry must be an object", "invalidValue")
    changes = lowered(value) if not sub else {sub: value}
    if not picked and comparison is not None:
        # Entra writes `emails[type eq "work"].value` for an entry not there yet,
        # by add or replace; the list it leaves is checked as any other.
        return _added(emails, _entries([{comparison[0]: comparison[1], **changes}]))
    out = [_entries([{**e, **changes}])[0] if i in picked else e for i, e in enumerate(emails)]
    return _one_primary(out, picked)


def _added(emails: list[dict[str, Any]], written: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """`emails` with `written` appended, an entry already there not again
    (RFC 7644 §3.5.2.1), though its primary flag is taken."""
    out, touched = list(emails), []
    for entry in written:
        same = next((i for i, e in enumerate(out) if _same(e, entry)), None)
        if same is None:
            out.append(entry)
            touched.append(len(out) - 1)
        elif entry["primary"]:
            out[same] = {**out[same], "primary": True}
            touched.append(same)
    return _one_primary(out, touched)


def _same(a: dict[str, Any], b: dict[str, Any]) -> bool:
    return scim.normalized(a["value"]) == scim.normalized(b["value"]) and (_type(a) == _type(b))


def _naming(named: Any) -> dict[str, str]:
    """What a remove's value says of an entry to drop: its address and/or type."""
    if not isinstance(named, dict):
        raise ScimError(400, "an emails entry must be an object", "invalidValue")
    named = lowered(named)
    if any(not isinstance(named.get(k), str | None) for k in ("value", "type")):
        raise ScimError(400, "an email's value and type must be strings", "invalidValue")
    said = {k: named[k] for k in ("value", "type") if isinstance(named.get(k), str)}
    if not said:
        raise ScimError(400, "a remove names an entry by its value or type", "invalidValue")
    return said


def _names(said: dict[str, str], entry: dict[str, Any]) -> bool:
    if "value" in said and scim.normalized(said["value"]) != scim.normalized(entry["value"]):
        return False
    return "type" not in said or scim.normalized(said["type"]) == _type(entry)


def _type(entry: dict[str, Any]) -> str:
    """An entry's type as filters compare it: trimmed, in any case."""
    return scim.normalized(str(entry.get("type") or ""))


def _listed(value: Any) -> list[Any]:
    return [] if value is None else value if isinstance(value, list) else [value]


# `emails[<filter>]<rest>`: a bracket inside the filter's quoted value is the value's.
_EMAILS_FILTER = re.compile(r'emails\[((?:[^"\[\]]|"(?:[^"\\]|\\.)*")*)\](.*)', re.DOTALL)


def _emails_path(attr: str) -> tuple[str | None, str]:
    """An `emails…` path as its filter (None for none) and sub-attribute ('' for none)."""
    malformed = ScimError(400, "an emails path is malformed", "invalidPath")
    filt, rest = None, attr.removeprefix("emails")
    if "[" in attr or "]" in attr:
        m = _EMAILS_FILTER.fullmatch(attr)
        if m is None:
            raise malformed
        filt, rest = m.groups()
    if (rest and not rest.startswith(".")) or "[" in rest or "]" in rest:
        raise malformed
    return filt, rest[1:]


def _one_primary(entries: list[dict[str, Any]], written: Iterable[int]) -> list[dict[str, Any]]:
    """Only one entry may be primary (RFC 7643 §2.4): of those just written
    primary, the first in the list with an address, else the first, takes the
    flag from every other, as `keyed_entry` would pick among them."""
    primaries = sorted(i for i in written if entries[i]["primary"])
    if not primaries:
        return entries
    first = next((i for i in primaries if _nonempty(entries[i]["value"])), primaries[0])
    return [{**e, "primary": i == first} for i, e in enumerate(entries)]


def _entries(items: list[Any]) -> list[dict[str, Any]]:
    """`emails` entries as Loom keeps them: a value, a type and a primary flag."""
    out = []
    for e in items:
        if not isinstance(e, dict):
            raise ScimError(400, "an emails entry must be an object", "invalidValue")
        e = lowered(e)
        value, kind = e.get("value"), e.get("type")
        if not isinstance(value, str | None) or not isinstance(kind, str | None):
            raise ScimError(400, "an email's value and type must be strings", "invalidValue")
        out.append(
            {"value": value or "", "type": kind or "", "primary": scim.is_primary(e.get("primary"))}
        )
    return out


# One `attr eq value` comparison: what a provider puts in an `emails[…]` filter.
_COMPARISON = re.compile(
    r'\s*(type|value|primary)\s+eq\s+(?:"((?:[^"\\]|\\.)*)"|(true|false))\s*', re.IGNORECASE
)


def _comparison(filt: str) -> tuple[str, str | bool]:
    """An `emails[…]` filter as (attribute, value), `primary` as a bool. A
    quoted value is a JSON string (RFC 7644 §3.4.2.2), escapes and all."""
    refused = ScimError(
        400, "an emails filter must be one comparison on type, value or primary", "invalidFilter"
    )
    m = _COMPARISON.fullmatch(filt)
    if not m:
        raise refused
    attr, text, flag = m.groups()
    if text is not None:
        try:
            text = json.loads(f'"{text}"')
        except ValueError:  # a bad escape or a control character
            raise refused from None
    attr, want = attr.lower(), text if text is not None else flag
    return attr, scim.is_primary(want) if attr == "primary" else want


def _matches(comparison: tuple[str, str | bool], entry: dict[str, Any]) -> bool:
    attr, want = comparison
    if attr == "primary":
        return want is entry["primary"]
    return scim.normalized(str(want)) == scim.normalized(entry[attr])


def int_param(request: Request, name: str, default: int) -> int:
    raw = request.query_params.get(name)
    if raw is None:
        return default
    try:
        return int(raw)
    except ValueError:
        raise ScimError(400, f"{name} must be an integer", "invalidValue") from None


async def read_body(request: Request) -> dict[str, Any]:
    try:
        body = json.loads(await request.body())
    except (ValueError, RecursionError):  # not JSON, or nested too deep to parse
        raise ScimError(400, "the body is not JSON", "invalidSyntax") from None
    if not isinstance(body, dict):
        raise ScimError(400, "the body must be a JSON object", "invalidSyntax")
    return body


def _rank(entry: dict[str, Any]) -> int:
    """Which emails entry Loom keys on: the primary, else the first work one, else the first."""
    if scim.is_primary(entry.get("primary")):
        return 2
    return 1 if _type(entry) == "work" else 0


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


def user_name_and_email(body: dict[str, Any]) -> tuple[str, str, list[dict[str, Any]]]:
    """The userName, the email SSO sign-in will carry (the keyed `emails`
    entry's, else the userName) and the `emails` list as Loom keeps it."""
    body = lowered(body)
    name = body.get("username")
    if not isinstance(name, str) or not name.strip():
        raise ScimError(400, "userName is required", "invalidValue")
    emails = _entries(_listed(body.get("emails") or None))  # a null (or falsy) one is none
    emails = _one_primary(emails, range(len(emails)))  # the first primary with an address keys it
    entry = keyed_entry(emails) or {}
    email = scim.normalized(entry.get("value") or name)
    if "@" not in email:
        raise ScimError(400, "userName or an email must be the person's email", "invalidValue")
    return name.strip(), email, emails


# ── resources ─────────────────────────────────────────────────────────────


def _resource(request: Request, row: dict[str, Any]) -> dict[str, Any]:
    return {
        "schemas": [USER_SCHEMA],
        "id": row["id"],
        "userName": row["user_name"],
        "active": bool(row["active"]),
        "emails": [_email(e) for e in scim.emails_of(row)],
        "meta": {
            "resourceType": "User",
            "created": _utc(row["created_at"]),
            "lastModified": _utc(row["updated_at"]),
            "location": f"{public_base_url(request)}/scim/v2/Users/{row['id']}",
        },
    }


def _utc(ts: datetime) -> str:
    # ClickHouse hands DateTime64(3, 'UTC') back without its zone; a fresh row has one.
    return as_aware_utc(ts).isoformat()


def _email(entry: dict[str, Any]) -> dict[str, Any]:
    """An `emails` entry as Loom keeps it: the value and type as written, when
    set, and the primary flag (false when not sent, RFC 7643 §2.4)."""
    shown: dict[str, Any] = {k: entry[k] for k in ("value", "type") if entry[k]}
    return {**shown, "primary": entry["primary"]}


def _not_found() -> ScimError:
    return ScimError(404, "no such user")


@router.get("/ServiceProviderConfig")
async def service_provider_config() -> JSONResponse:
    return _answer(
        {
            "schemas": [_CONFIG_SCHEMA],
            "patch": {"supported": True},
            "bulk": {"supported": False, "maxOperations": 0, "maxPayloadSize": 0},
            "filter": {"supported": True, "maxResults": MAX_RESULTS},
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
    start = min(LAST_INDEX, max(1, int_param(request, "startIndex", 1)))
    count = min(MAX_RESULTS, max(0, int_param(request, "count", 100)))
    text = request.query_params.get("filter")
    if text is not None:
        attribute, value = parse_filter(text)
        if attribute == "userName":
            row = await scim.find(value)
            total, rows = (1, [row]) if row else (0, [])
            rows = rows[start - 1 : start - 1 + count]
        else:  # any entry with the address matches (RFC 7644 §3.4.2.2)
            work = attribute == "work email"
            total, rows = await scim.page(start, count, email=value, work=work)
    else:
        total, rows = await scim.page(start, count)
    return _answer(
        {
            "schemas": [LIST_SCHEMA],
            "totalResults": total,
            "startIndex": start,
            "itemsPerPage": len(rows),
            "Resources": [_resource(request, r) for r in rows],
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
    body = lowered(await read_body(request))
    name, email, emails = user_name_and_email(body)
    # A null is unassigned (RFC 7643 §2.5): a new user is active.
    active = parse_active(True if body.get("active") is None else body["active"])
    try:
        row = await scim.create(name, email, active=active, emails=emails)
    except scim.Exists:
        raise ScimError(409, "a user with this email or userName exists", "uniqueness") from None
    resource = _resource(request, row)
    return _answer(resource, 201, Location=resource["meta"]["location"])


@router.put("/Users/{uid}")
async def replace_user(uid: str, request: Request) -> JSONResponse:
    body = await read_body(request)
    if any(named != uid for named in _every(body, "id")):
        raise ScimError(400, "the body names another user", "invalidValue")
    # Read in every spelling: a body refused for an attribute named twice
    # still deactivates when one of them says false.
    spellings = [a for a in _every(body, "active") if a is not None]
    if any(_is_false(a) for a in spellings):
        active: bool | None = False
    else:
        active = parse_active(spellings[0]) if spellings else None
    row = await scim.get(uid)
    if row is None:
        raise _not_found()
    async with _deactivating_anyway(uid, active):
        name, email, emails = user_name_and_email(body)
        _refuse_another_email(row, email)
        new = await _update(uid, active=active, user_name=name, emails=emails)
        if new is None:  # deleted since it was read
            raise _not_found()
        row = new
    return _answer(_resource(request, row))


@router.patch("/Users/{uid}")
async def patch_user(uid: str, request: Request) -> JSONResponse:
    body = await read_body(request)
    try:
        active = patched_active(body)
    except ScimError:
        # Refused for another `active` op: one that does set false still deactivates.
        if _sets_inactive(body):
            await scim.update(uid, active=False)  # None for a user that is not there
        raise

    def patched(row: dict[str, Any]) -> tuple[str | None, list[dict[str, Any]] | None]:
        name, emails = patched_names(body, row)
        if name is not None or emails is not None:
            after = {
                "userName": row["user_name"] if name is None else name,
                "emails": scim.emails_of(row) if emails is None else emails,
            }
            _refuse_another_email(row, user_name_and_email(after)[1])
        return name, emails

    async with _deactivating_anyway(uid, active):
        # Every PATCH converges, one of only dropped attributes too: an active
        # user lets go what no deactivated email reaches any more.
        row = await _update(uid, active=active, patched=patched)
        if row is None:
            raise _not_found()
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
    uid: str,
    *,
    active: bool | None,
    user_name: str | None = None,
    emails: list[dict[str, Any]] | None = None,
    patched: scim.Patch | None = None,
) -> dict[str, Any] | None:
    try:
        return await scim.update(
            uid, active=active, user_name=user_name, emails=emails, patched=patched
        )
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
# provider probes (`/Schemas`), and a known path's other methods.
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
    raise ScimError(404, "this server serves only Users, Groups and ServiceProviderConfig")


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
