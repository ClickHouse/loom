"""Repository for loom.profile_schemas: the shape of each user's structured
profile in a namespace.

One row per change, ReplacingMergeTree(version) keyed on (org, namespace), so
the newest wins; a row with `tombstoned_at` set means the schema was cleared.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any

from server.core.repo_base import _CHClient, _default_client
from server.memory.domain.profile_schema import ProfileProperty, parse


@dataclass(frozen=True)
class ActiveSchema:
    """A namespace's schema as a pass uses it, parsed once."""

    org: str
    namespace: str
    props: list[ProfileProperty]
    sha256: str


async def insert_row(row: dict[str, Any], *, ch: _CHClient | None = None) -> None:
    """Write one change, committed before it returns (the next read must see it)."""
    ch = ch or _default_client()
    await ch.insert(
        "loom.profile_schemas",
        [row],
        settings={"async_insert": 0, "wait_for_async_insert": 1},
    )


async def get(org: str, namespace: str, *, ch: _CHClient | None = None) -> dict[str, Any] | None:
    """The namespace's current schema; None when none is set or it was cleared."""
    ch = ch or _default_client()
    row: dict[str, Any] | None = await ch.query_one(
        "SELECT schema_json, schema_sha256, updated_by, updated_at "
        "FROM loom.profile_schemas FINAL "
        "WHERE org = {org:String} AND namespace = {ns:String} AND tombstoned_at IS NULL "
        "LIMIT 1",
        params={"org": org, "ns": namespace},
    )
    return row


async def list_active(
    *,
    exclude_orgs: list[str],
    after: tuple[str, str] | None,
    limit: int,
    only: tuple[str, str] | None = None,
    ch: _CHClient | None = None,
) -> list[ActiveSchema]:
    """At most `limit` active namespaces with a schema in (org, namespace) order,
    starting just after `after` and wrapping around to the first; `only` keeps
    one namespace. Those of `exclude_orgs` are left out."""
    ch = ch or _default_client()
    rows = await ch.query_all(
        "SELECT s.org AS org, s.namespace AS namespace, s.schema_json AS schema_json, "
        "s.schema_sha256 AS schema_sha256 "
        "FROM loom.profile_schemas AS s FINAL "
        "WHERE s.tombstoned_at IS NULL AND NOT has({off:Array(String)}, s.org) "
        # A deactivated namespace waits; one never registered has no row and is served.
        "AND (s.org, s.namespace) NOT IN "
        "(SELECT org, namespace FROM loom.namespaces FINAL WHERE active = false) "
        "AND ({o_org:String} = '' OR (s.org, s.namespace) = ({o_org:String}, {o_ns:String})) "
        # Those after the cursor first (false sorts first), then from the start.
        "ORDER BY (s.org, s.namespace) <= ({a_org:String}, {a_ns:String}), s.org, s.namespace "
        "LIMIT {lim:UInt32}",
        params={
            "off": exclude_orgs,
            "a_org": after[0] if after else "",
            "a_ns": after[1] if after else "",
            "o_org": only[0] if only else "",
            "o_ns": only[1] if only else "",
            "lim": int(limit),
        },
    )
    return [
        ActiveSchema(
            org=r["org"],
            namespace=r["namespace"],
            props=parse(json.loads(r["schema_json"])),
            sha256=r["schema_sha256"],
        )
        for r in rows
    ]
