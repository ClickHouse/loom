"""SCIM 2.0 Groups (RFC 7643 §4.2, RFC 7644) under /scim/v2/Groups: the groups a
service in front of Loom pushes for its org, which grants name as `group:<id>`.

The credential is a token of an org admin of exactly one org: the group lands
in that org and nowhere else. A member is one of the service's end users,
`{"value": "<user id>"}`, held as the principal `user:<id>`; a nested group is
refused. The deployment's own SCIM token (`LOOM_SCIM_TOKEN`)
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
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from fastapi import APIRouter, Depends, Request, Response
from fastapi.responses import JSONResponse

from server.auth import scim
from server.auth.bearer import bearer_token
from server.auth.gateway import is_end_user
from server.auth.grant_claims import group_change
from server.auth.jwt import AuthInfo, AuthUnavailable, TokenError, verify_token
from server.auth.scim_routes import (
    LAST_INDEX,
    LIST_SCHEMA,
    MAX_RESULTS,
    SCIM_JSON,
    ScimError,
    ScimRoute,
    int_param,
    lowered,
    patch_operations,
    read_body,
)
from server.core import grant_cache
from server.core.ch import get_client, own_write_reads, set_query_tag
from server.core.principals import USER, is_principal_id
from server.observability import audit
from server.observability.metrics import record_auth_refusal
from server.operations.base import Role
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


@dataclass(frozen=True)
class _Caller:
    """Who pushes: the org admin token's org and subject ('' for the
    deployment's SCIM token, which names no org)."""

    org: str
    actor: str


async def _caller(request: Request) -> _Caller:
    """The SCIM client of this request."""
    set_query_tag(f"scim.groups.{request.method.lower()}")
    message = "a token of one org's admin, or the SCIM bearer token, is required"
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
        return _Caller(org="", actor="scim")
    try:
        auth: AuthInfo = verify_token(token)
    except AuthUnavailable as e:  # the revocation list or keyring is not loaded yet
        raise ScimError(503, "tokens cannot be checked yet; retry") from e
    except TokenError as e:
        record_auth_refusal(e, transport="scim")
        raise ScimError(401, message) from None
    if not (auth.has_role(Role.ORG_ADMIN) and auth.org not in ("", "*")) or is_end_user(auth):
        refused = PermissionError("groups are pushed with a token of one org's admin")
        record_auth_refusal(refused, transport="scim")
        raise ScimError(403, str(refused))
    return _Caller(org=auth.org, actor=auth.subject)


router = APIRouter(prefix="/scim/v2", route_class=ScimRoute)
# The SCIM client of each request, as a route dependency.
_CALLER = Depends(_caller)


def group_row(
    org: str,
    group_id: str,
    *,
    display_name: str,
    external_id: str,
    members: list[str],
    created_at: datetime | None = None,
    deleted: bool = False,
    after: int = 0,
) -> dict[str, Any]:
    """One whole loom.principal_groups row, versioned above `after` (the
    version of the row it replaces) whatever this host's clock says."""
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
        "version": max(time.time_ns(), after + 1),
    }


async def _stored_version(org: str, group_id: str) -> int:
    """The version of the group's newest row, a deleted one included; 0 for none."""
    with own_write_reads():
        row = await get_client().query_one(
            "SELECT max(version) AS v FROM loom.principal_groups"
            " WHERE org = {org:String} AND group_id = {gid:String}",
            params={"org": org, "gid": group_id},
        )
    return int((row or {}).get("v") or 0)


async def fetch_group(org: str, group_id: str) -> dict[str, Any] | None:
    """The live group `group_id` of `org`, or None."""
    with own_write_reads():
        return await get_client().query_one(
            f"""
            SELECT {", ".join(COLUMNS)} FROM loom.principal_groups FINAL
            WHERE org = {{org:String}} AND group_id = {{gid:String}} AND NOT deleted
            """,
            params={"org": org, "gid": group_id},
        )


async def _write(row: dict[str, Any], *, event: str, actor: str) -> None:
    """The group's row, announced and audited even when the insert's reply is
    lost: it may have landed, and a client that does not retry never asks again."""
    try:
        async with grant_cache.announced(row["org"], kind=grant_cache.GROUPS):
            await get_client().insert(
                "loom.principal_groups",
                [row],
                settings={"async_insert": 0, "wait_for_async_insert": 1},
            )
    finally:
        await audit.emit(
            event,
            org=row["org"],
            namespace="-",
            actor=actor,
            subject={"group": row["group_id"], "members": len(row["members"])},
        )


def _members(value: Any) -> list[str]:
    """`members` as principals; a member that is not a user id is refused."""
    out = []
    for m in value or []:
        ident = str(lowered(m).get("value") or "") if isinstance(m, dict) else ""
        if not is_principal_id(ident):
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


def _caller_org(caller: _Caller) -> str:
    """The org a group request reads or writes; the deployment's SCIM token names none."""
    if not caller.org:
        raise ScimError(403, "groups are pushed with a token of one org's admin")
    return caller.org


async def _existing(org: str, group_id: str) -> dict[str, Any]:
    row = await fetch_group(org, group_id)
    if row is None:
        raise ScimError(404, f"no group {group_id!r}")
    return row


# a blank run is read from its first blank only
_EQ = re.compile(r"(?<!\s)\s+eq\s+", re.IGNORECASE)


@router.get("/Groups")
async def list_groups(request: Request, caller: _Caller = _CALLER) -> JSONResponse:
    start = min(LAST_INDEX, max(1, int_param(request, "startIndex", 1)))
    count = min(MAX_RESULTS, max(0, int_param(request, "count", MAX_RESULTS)))
    rows: list[dict[str, Any]] = []
    total = 0
    org = caller.org
    if org:
        where = "org = {org:String} AND NOT deleted"
        params: dict[str, Any] = {"org": org}
        text = request.query_params.get("filter") or ""
        if text:
            # Operators are case-insensitive (RFC 7644 §3.4.2.2).
            attribute, _, value = _EQ.sub(" eq ", text, count=1).partition(" eq ")
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
            counted = await get_client().query_one(
                f"SELECT count() AS n FROM loom.principal_groups FINAL WHERE {where}",
                params=params,
            )
            rows = await get_client().query_all(
                f"SELECT {', '.join(COLUMNS)} FROM loom.principal_groups FINAL WHERE {where} "
                "ORDER BY group_id LIMIT {lim:UInt32} OFFSET {off:UInt64}",
                params={**params, "lim": count, "off": start - 1},
            )
        total = int((counted or {}).get("n") or 0)
    return _answer(
        {
            "schemas": [LIST_SCHEMA],
            "totalResults": total,
            "startIndex": start,
            "itemsPerPage": len(rows),
            "Resources": [_resource(request, r) for r in rows],
        }
    )


@router.get("/Groups/{group_id}")
async def get_group(group_id: str, request: Request, caller: _Caller = _CALLER) -> JSONResponse:
    return _answer(_resource(request, await _existing(_caller_org(caller), group_id)))


@router.post("/Groups")
async def create_group(request: Request, caller: _Caller = _CALLER) -> JSONResponse:
    """The group's id is its externalId when that is a well-formed id, so a
    client can address and grant it by its own id; else a new one."""
    org = _caller_org(caller)
    body = lowered(await read_body(request))  # names match in any case (RFC 7643 §2.1)
    external = str(body.get("externalid") or "")
    group_id = external if is_principal_id(external) else uuid.uuid4().hex
    members = _members(body.get("members"))
    async with group_change(org, group_id):
        if await fetch_group(org, group_id) is not None:
            raise ScimError(409, f"a group {group_id!r} exists", "uniqueness")
        row = group_row(
            org,
            group_id,
            display_name=str(body.get("displayname") or ""),
            external_id=external,
            members=members,
            after=await _stored_version(org, group_id),  # above a deleted group of this id
        )
        await _write(row, event="scim.group_created", actor=caller.actor)
    resource = _resource(request, row)
    return _answer(resource, 201, Location=resource["meta"]["location"])


@router.put("/Groups/{group_id}")
async def replace_group(group_id: str, request: Request, caller: _Caller = _CALLER) -> JSONResponse:
    org = _caller_org(caller)
    body = lowered(await read_body(request))  # names match in any case (RFC 7643 §2.1)
    members = _members(body.get("members"))
    async with group_change(org, group_id):
        current = await _existing(org, group_id)
        row = group_row(
            org,
            group_id,
            display_name=str(body.get("displayname") or current["display_name"]),
            external_id=str(body.get("externalid") or current["external_id"]),
            members=members,
            created_at=current["created_at"],
            after=current["version"],
        )
        await _write(row, event="scim.group_replaced", actor=caller.actor)
    return _answer(_resource(request, row))


def _patched(
    name: str, members: set[str], operations: list[dict[str, Any]]
) -> tuple[str, set[str]]:
    """The group's name and members once `operations` (RFC 7644 §3.5.2, names
    lower-cased by `patch_operations`) apply."""
    for op in operations:
        kind = str(op.get("op") or "").lower()
        path = str(op.get("path") or "")
        value = op.get("value")
        if path.lower() == "displayname" and kind == "replace":
            name = str(value or "")
        elif path.lower() == "members" and kind in ("add", "replace"):
            members = (set() if kind == "replace" else members) | set(_members(value))
        elif path.lower() == "members" and kind == "remove":
            # No value removes every member (§3.5.2.2).
            members = members - set(_members(value)) if value else set()
        elif (filtered := _MEMBER_FILTER.fullmatch(path)) and kind == "remove":
            members = members - {USER + filtered.group(1)}
        elif not path and kind == "replace" and isinstance(value, dict):
            value = lowered(value)
            name = str(value.get("displayname") or name)
            if "members" in value:
                members = set(_members(value["members"]))
        else:
            raise ScimError(400, f"{kind} {path or '(no path)'} is not served", "invalidPath")
    return name, members


@router.patch("/Groups/{group_id}")
async def patch_group(group_id: str, request: Request, caller: _Caller = _CALLER) -> JSONResponse:
    """`add` / `remove` / `replace` on `members`, and `replace` on `displayName`."""
    org = _caller_org(caller)
    body = await read_body(request)
    async with group_change(org, group_id):
        current = await _existing(org, group_id)
        name, members = _patched(
            current["display_name"], set(current["members"]), patch_operations(body)
        )
        row = group_row(
            org,
            group_id,
            display_name=name,
            external_id=current["external_id"],
            members=sorted(members),
            created_at=current["created_at"],
            after=current["version"],
        )
        await _write(row, event="scim.group_changed", actor=caller.actor)
    return _answer(_resource(request, row))


@router.delete("/Groups/{group_id}")
async def delete_group(group_id: str, caller: _Caller = _CALLER) -> Response:
    """The group goes, and so do the grants that named it; `grants.create`
    refuses a group that is gone."""
    from server.identity.app.grants import revoke_subject

    org = _caller_org(caller)
    async with group_change(org, group_id):
        current = await _existing(org, group_id)
        row = group_row(
            org,
            group_id,
            display_name=current["display_name"],
            external_id=current["external_id"],
            members=[],
            created_at=current["created_at"],
            deleted=True,
            after=current["version"],
        )
        # The grants first: a delete cut short is retried, and finds the group still there.
        await revoke_subject(org, f"group:{group_id}", actor=caller.actor)
        await _write(row, event="scim.group_deleted", actor=caller.actor)
    return Response(status_code=204)


def attach(app: Any) -> None:
    """Mounted always, before the Users routes, so its paths answer first."""
    app.include_router(router)
