"""SCIM 2.0 Groups (RFC 7643 §4.2, RFC 7644) under /scim/v2/Groups: the groups a
gateway pushes for its org, which grants name as `group:<id>`.

The credential is the org's gateway token (`loom mint-token --gateway`): the
group lands in that token's org and nowhere else. A member is one of the
gateway's users, `{"value": "<user id>"}`, held as the principal `user:<id>`;
a nested group is refused. The deployment's own SCIM token (`LOOM_SCIM_TOKEN`)
names no org: it reads an empty list, as an identity provider's connection test
expects, and writes nothing.

`loom.principal_groups` holds one whole row per group, so every change rewrites
it and moves the org's grant watermark: a member removed is refused everywhere
at the next poll.
"""

from __future__ import annotations

import hmac
import re
import time
import uuid
from datetime import UTC, datetime
from typing import Any

from fastapi import APIRouter, Depends, Request, Response
from fastapi.responses import JSONResponse

from server.auth import scim
from server.auth.bearer import bearer_token
from server.auth.gateway import USER, principal_id
from server.auth.jwt import AuthInfo, TokenError, verify_token
from server.auth.scim_routes import (
    _LIST_SCHEMA,
    _MAX_RESULTS,
    SCIM_JSON,
    ScimError,
    _body,
    _int_param,
    _ScimRoute,
)
from server.core import grant_cache
from server.core.ch import get_client, own_write_reads, set_query_tag
from server.observability import audit
from server.observability.metrics import record_auth_refusal
from server.settings import settings

GROUP_SCHEMA = "urn:ietf:params:scim:schemas:core:2.0:Group"
# The path Entra and Okta send to remove one member: members[value eq "<id>"].
_MEMBER_FILTER = re.compile(r'members\[\s*value\s+eq\s+"([^"]+)"\s*\]', re.IGNORECASE)
_PATCH_SCHEMA = "urn:ietf:params:scim:api:messages:2.0:PatchOp"
COLUMNS = (
    "org",
    "group_id",
    "display_name",
    "external_id",
    "members",
    "deleted",
    "created_at",
    "updated_at",
    "version",
)


async def _org(request: Request) -> str:
    """The org a request's gateway token belongs to; '' for the deployment's
    SCIM token, which names none."""
    set_query_tag(f"scim.groups.{request.method.lower()}")
    message = "a gateway token, or the SCIM bearer token, is required"
    try:
        token = bearer_token(request.headers.get("authorization"))
    except TokenError as e:
        record_auth_refusal(e, transport="scim")
        raise ScimError(401, message) from None
    if token is None:
        record_auth_refusal(None, transport="scim")
        raise ScimError(401, message)
    presented = token.encode()
    if any([hmac.compare_digest(presented, t.encode()) for t in scim.tokens(settings.scim_token)]):
        return ""
    try:
        auth: AuthInfo = verify_token(token)
    except TokenError as e:
        record_auth_refusal(e, transport="scim")
        raise ScimError(401, message) from None
    if not auth.gateway:
        raise ScimError(403, "groups are pushed with the org's gateway token")
    return auth.org


router = APIRouter(prefix="/scim/v2", route_class=_ScimRoute)


def group_row(
    org: str,
    group_id: str,
    *,
    display_name: str,
    external_id: str,
    members: list[str],
    created_at: datetime | None = None,
    deleted: bool = False,
) -> dict[str, Any]:
    """One whole loom.principal_groups row."""
    now = datetime.now(UTC).replace(tzinfo=None)
    return {
        "org": org,
        "group_id": group_id,
        "display_name": display_name,
        "external_id": external_id,
        "members": sorted(set(members)),
        "deleted": deleted,
        "created_at": created_at or now,
        "updated_at": now,
        "version": time.time_ns(),
    }


async def _fetch(org: str, group_id: str) -> dict[str, Any] | None:
    with own_write_reads():
        return await get_client().query_one(
            f"""
            SELECT {", ".join(COLUMNS)} FROM loom.principal_groups FINAL
            WHERE org = {{org:String}} AND group_id = {{gid:String}} AND NOT deleted
            """,
            params={"org": org, "gid": group_id},
        )


async def _write(row: dict[str, Any], *, event: str) -> None:
    await get_client().insert(
        "loom.principal_groups", [row], settings={"async_insert": 0, "wait_for_async_insert": 1}
    )
    await grant_cache.changed(row["org"])
    await audit.emit(
        event,
        org=row["org"],
        namespace="-",
        actor="scim",
        subject={"group": row["group_id"], "members": len(row["members"])},
    )


def _members(value: Any) -> list[str]:
    """`members` as principals; a member that is not a user id is refused."""
    out = []
    for m in value or []:
        ident = str((m or {}).get("value") or "") if isinstance(m, dict) else ""
        if not principal_id(USER + ident, USER):
            raise ScimError(400, f"a member is a user id, not {ident!r}", "invalidValue")
        out.append(USER + ident)
    return out


def _resource(request: Request, row: dict[str, Any]) -> dict[str, Any]:
    base = str(request.base_url).rstrip("/")
    return {
        "schemas": [GROUP_SCHEMA],
        "id": row["group_id"],
        "externalId": row["external_id"] or None,
        "displayName": row["display_name"],
        "members": [{"value": m.removeprefix(USER)} for m in row["members"]],
        "meta": {
            "resourceType": "Group",
            "created": row["created_at"].replace(tzinfo=UTC).isoformat(),
            "lastModified": row["updated_at"].replace(tzinfo=UTC).isoformat(),
            "location": f"{base}/scim/v2/Groups/{row['group_id']}",
        },
    }


def _answer(body: dict[str, Any], status: int = 200, **headers: str) -> JSONResponse:
    return JSONResponse(body, status_code=status, media_type=SCIM_JSON, headers=headers or None)


def _writable(org: str) -> str:
    if not org:
        raise ScimError(403, "groups are pushed with the org's gateway token")
    return org


async def _existing(org: str, group_id: str) -> dict[str, Any]:
    row = await _fetch(org, group_id)
    if row is None:
        raise ScimError(404, f"no group {group_id!r}")
    return row


@router.get("/Groups")
async def list_groups(request: Request, org: str = Depends(_org)) -> JSONResponse:
    start = max(1, _int_param(request, "startIndex", 1))
    count = min(_MAX_RESULTS, max(0, _int_param(request, "count", _MAX_RESULTS)))
    rows: list[dict[str, Any]] = []
    total = 0
    if org:
        where, params = "org = {org:String} AND NOT deleted", {"org": org}
        text = request.query_params.get("filter") or ""
        if text:
            attribute, _, value = text.partition(" eq ")
            column = {"displayname": "display_name", "externalid": "external_id"}.get(
                attribute.strip().lower()
            )
            if column is None or not value.strip().startswith('"'):
                raise ScimError(
                    400,
                    'the filters served are displayName eq "x" and externalId eq "x"',
                    "invalidFilter",
                )
            where += f" AND {column} = {{v:String}}"
            params["v"] = value.strip().strip('"')
        with own_write_reads():
            found = await get_client().query_all(
                f"SELECT {', '.join(COLUMNS)} FROM loom.principal_groups FINAL WHERE {where} "
                "ORDER BY group_id",
                params=params,
            )
        total = len(found)
        rows = found[start - 1 : start - 1 + count]
    return _answer(
        {
            "schemas": [_LIST_SCHEMA],
            "totalResults": total,
            "startIndex": start,
            "itemsPerPage": len(rows),
            "Resources": [_resource(request, r) for r in rows],
        }
    )


@router.get("/Groups/{group_id}")
async def get_group(group_id: str, request: Request, org: str = Depends(_org)) -> JSONResponse:
    return _answer(_resource(request, await _existing(_writable(org), group_id)))


@router.post("/Groups")
async def create_group(request: Request, org: str = Depends(_org)) -> JSONResponse:
    """The group's id is its externalId when that is a well-formed id, so a
    client can address and grant it by its own id; else a new one."""
    org = _writable(org)
    body = await _body(request)
    external = str(body.get("externalId") or "")
    group_id = external if principal_id(USER + external, USER) else uuid.uuid4().hex
    if await _fetch(org, group_id) is not None:
        raise ScimError(409, f"a group {group_id!r} exists", "uniqueness")
    row = group_row(
        org,
        group_id,
        display_name=str(body.get("displayName") or ""),
        external_id=external,
        members=_members(body.get("members")),
    )
    await _write(row, event="scim.group_created")
    resource = _resource(request, row)
    return _answer(resource, 201, Location=resource["meta"]["location"])


@router.put("/Groups/{group_id}")
async def replace_group(group_id: str, request: Request, org: str = Depends(_org)) -> JSONResponse:
    org = _writable(org)
    current = await _existing(org, group_id)
    body = await _body(request)
    row = group_row(
        org,
        group_id,
        display_name=str(body.get("displayName") or current["display_name"]),
        external_id=str(body.get("externalId") or current["external_id"]),
        members=_members(body.get("members")),
        created_at=current["created_at"],
    )
    await _write(row, event="scim.group_replaced")
    return _answer(_resource(request, row))


@router.patch("/Groups/{group_id}")
async def patch_group(group_id: str, request: Request, org: str = Depends(_org)) -> JSONResponse:
    """`add` / `remove` / `replace` on `members`, and `replace` on `displayName`."""
    org = _writable(org)
    current = await _existing(org, group_id)
    body = await _body(request)
    members = set(current["members"])
    name = current["display_name"]
    for op in body.get("Operations") or []:
        kind = str(op.get("op") or "").lower()
        path = str(op.get("path") or "")
        value = op.get("value")
        if path.lower() == "displayname" and kind == "replace":
            name = str(value or "")
        elif path.lower() == "members" and kind in ("add", "replace"):
            members = (set() if kind == "replace" else members) | set(_members(value))
        elif path.lower() == "members" and kind == "remove":
            # No value removes every member (RFC 7644 §3.5.2.2).
            members = members - set(_members(value)) if value else set()
        elif (filtered := _MEMBER_FILTER.fullmatch(path)) and kind == "remove":
            members.discard(USER + filtered.group(1))
        elif not path and kind == "replace" and isinstance(value, dict):
            name = str(value.get("displayName") or name)
            if "members" in value:
                members = set(_members(value["members"]))
        else:
            raise ScimError(400, f"{kind} {path or '(no path)'} is not served", "invalidPath")
    row = group_row(
        org,
        group_id,
        display_name=name,
        external_id=current["external_id"],
        members=sorted(members),
        created_at=current["created_at"],
    )
    await _write(row, event="scim.group_changed")
    return _answer(_resource(request, row))


@router.delete("/Groups/{group_id}")
async def delete_group(group_id: str, org: str = Depends(_org)) -> Response:
    org = _writable(org)
    current = await _existing(org, group_id)
    row = group_row(
        org,
        group_id,
        display_name=current["display_name"],
        external_id=current["external_id"],
        members=[],
        created_at=current["created_at"],
        deleted=True,
    )
    await _write(row, event="scim.group_deleted")
    return Response(status_code=204)


def attach(app: Any) -> None:
    """Mounted always, before the Users routes, so its paths answer first."""
    app.include_router(router)
