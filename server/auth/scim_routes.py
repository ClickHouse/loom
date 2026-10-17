"""SCIM 2.0 (RFC 7643/7644) under /scim/v2: what an identity provider needs to
deactivate and reactivate people (`server/auth/scim.py`).

Mounted only when `LOOM_SCIM_TOKEN` is set, and that bearer token is the only
credential these routes take. A user resource carries `userName` (the email)
and `active`; other attributes a provider sends are accepted and not kept.
"""

from __future__ import annotations

import hmac
import json
import re
from typing import Any

from fastapi import APIRouter, Depends, Request, Response
from fastapi.responses import JSONResponse

from server.auth import scim
from server.core.ch import set_query_tag
from server.settings import settings

SCIM_JSON = "application/scim+json"
USER_SCHEMA = "urn:ietf:params:scim:schemas:core:2.0:User"
_LIST_SCHEMA = "urn:ietf:params:scim:api:messages:2.0:ListResponse"
_ERROR_SCHEMA = "urn:ietf:params:scim:api:messages:2.0:Error"
_CONFIG_SCHEMA = "urn:ietf:params:scim:schemas:core:2.0:ServiceProviderConfig"
_MAX_RESULTS = 200


class ScimError(Exception):
    """An RFC 7644 §3.12 error response."""

    def __init__(self, status: int, detail: str, scim_type: str = "") -> None:
        super().__init__(detail)
        self.status, self.detail, self.scim_type = status, detail, scim_type


async def _authorized(request: Request) -> None:
    set_query_tag(f"scim.{request.method.lower()}")  # these routes are not under the transports
    scheme, _, token = request.headers.get("authorization", "").partition(" ")
    expected = settings.scim_token.encode()
    if (
        not expected
        or scheme.lower() != "bearer"
        or not hmac.compare_digest(token.strip().encode(), expected)
    ):
        raise ScimError(401, "a valid SCIM bearer token is required")


router = APIRouter(prefix="/scim/v2", dependencies=[Depends(_authorized)])


def _answer(body: dict[str, Any], status: int = 200, **headers: str) -> JSONResponse:
    return JSONResponse(body, status_code=status, media_type=SCIM_JSON, headers=headers or None)


def _error_response(_request: Request, exc: Exception) -> JSONResponse:
    e = exc if isinstance(exc, ScimError) else ScimError(503, "this user is being changed; retry")
    body: dict[str, Any] = {"schemas": [_ERROR_SCHEMA], "status": str(e.status)}
    if e.scim_type:
        body["scimType"] = e.scim_type
    body["detail"] = e.detail
    headers = {"WWW-Authenticate": "Bearer"} if e.status == 401 else {}
    if e.status == 503:
        headers["Retry-After"] = "5"
    return _answer(body, e.status, **headers)


# ── parsing ───────────────────────────────────────────────────────────────

_FILTER = re.compile(r'\s*userName\s+eq\s+"((?:[^"\\]|\\.)*)"\s*', re.IGNORECASE)


def parse_filter(text: str) -> str:
    """The value of `userName eq "<value>"`, the one filter served."""
    m = _FILTER.fullmatch(text)
    if m:
        try:
            value = json.loads(f'"{m.group(1)}"')
        except ValueError:
            value = None
        if isinstance(value, str):
            return value
    raise ScimError(400, 'the only filter served is userName eq "<email>"', "invalidFilter")


def parse_active(value: Any) -> bool:
    """A boolean, or the strings "true"/"false" some providers send."""
    if isinstance(value, bool):
        return value
    if isinstance(value, str) and value.strip().lower() in ("true", "false"):
        return value.strip().lower() == "true"
    raise ScimError(400, "active must be a boolean", "invalidValue")


def patched_active(body: dict[str, Any]) -> bool | None:
    """What a PatchOp sets `active` to, or None when it leaves it alone.
    Operations on other attributes are accepted and dropped."""
    ops = body.get("Operations")
    if not isinstance(ops, list):
        raise ScimError(400, "a PatchOp needs Operations", "invalidSyntax")
    active = None
    for op in ops:
        if not isinstance(op, dict):
            raise ScimError(400, "each operation must be an object", "invalidSyntax")
        path, value = str(op.get("path") or ""), op.get("value")
        if path.lower() == "active":
            touched = value
        elif not path and isinstance(value, dict):
            touched = next((v for k, v in value.items() if k.lower() == "active"), None)
            if touched is None:
                continue
        else:
            continue
        if str(op.get("op") or "").lower() not in ("add", "replace"):
            raise ScimError(400, "active can only be replaced", "invalidSyntax")
        active = parse_active(touched)
    return active


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
    except ValueError:
        raise ScimError(400, "the body is not JSON", "invalidSyntax") from None
    if not isinstance(body, dict):
        raise ScimError(400, "the body must be a JSON object", "invalidSyntax")
    return body


def _user_name(body: dict[str, Any]) -> str:
    name = body.get("userName")
    if not isinstance(name, str) or "@" not in name:
        raise ScimError(400, "userName must be the person's email", "invalidValue")
    return scim.normalized(name)


# ── resources ─────────────────────────────────────────────────────────────


def _resource(request: Request, row: dict[str, Any]) -> dict[str, Any]:
    from server.auth.public_url import public_base_url

    return {
        "schemas": [USER_SCHEMA],
        "id": row["id"],
        "userName": row["user_name"],
        "active": bool(row["active"]),
        "emails": [{"value": row["user_name"], "primary": True}],
        "meta": {
            "resourceType": "User",
            "created": _iso(row["created_at"]),
            "lastModified": _iso(row.get("updated_at") or row["created_at"]),
            "location": f"{public_base_url(request)}/scim/v2/Users/{row['id']}",
        },
    }


def _iso(ts: Any) -> str:
    return ts.isoformat() if hasattr(ts, "isoformat") else str(ts)


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
    start = max(1, _int_param(request, "startIndex", 1))
    count = min(_MAX_RESULTS, max(0, _int_param(request, "count", 100)))
    text = request.query_params.get("filter")
    if text is not None:
        row = await scim.find(parse_filter(text))
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
    name = _user_name(body)
    try:
        row = await scim.create(name, active=parse_active(body.get("active", True)))
    except scim.Exists:
        raise ScimError(409, "a user with this userName exists", "uniqueness") from None
    resource = _resource(request, row)
    return _answer(resource, 201, Location=resource["meta"]["location"])


@router.put("/Users/{uid}")
async def replace_user(uid: str, request: Request) -> JSONResponse:
    body = await _body(request)
    row = await scim.get(uid)
    if row is None:
        raise _not_found()
    if _user_name(body) != row["user_name"]:
        # The id is the email's: another email is another person.
        raise ScimError(400, "userName cannot change", "mutability")
    if "active" in body:
        row = await scim.set_active(uid, parse_active(body["active"])) or row
    return _answer(_resource(request, row))


@router.patch("/Users/{uid}")
async def patch_user(uid: str, request: Request) -> JSONResponse:
    active = patched_active(await _body(request))
    row = await scim.get(uid) if active is None else await scim.set_active(uid, active)
    if row is None:
        raise _not_found()
    return _answer(_resource(request, row))


@router.delete("/Users/{uid}")
async def delete_user(uid: str) -> Response:
    if not await scim.delete(uid):
        raise _not_found()
    return Response(status_code=204)


def attach(app: Any) -> None:
    if not settings.scim_token:
        return
    app.add_exception_handler(ScimError, _error_response)
    app.add_exception_handler(scim.Busy, _error_response)  # answered as a 503
    app.include_router(router)
