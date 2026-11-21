"""Source-id retirement queue under each mirror, in the erased sync-state table."""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any
from uuid import UUID

from server.core.naming import is_mirror_name, mirror_name_sql
from server.core.repo_base import _CHClient, _default_client
from server.knowledge.domain.sources import MIRRORED_ACCESS, KnowledgeSource, valid_name

TABLE = "loom.code_index_state"
PREFIX = "knowledge_source_retire:"
PAGE_SIZE = 40  # not a setting: bounded queue discovery
_ID_FORM = "[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}"


@dataclass(frozen=True)
class Retirement:
    org: str
    namespace: str
    name: str
    source_id: str
    kind: str
    mirror: str

    @property
    def key(self) -> str:
        return PREFIX + self.source_id

    def source(self) -> KnowledgeSource:
        """Only identity and ownership: cleanup never opens a credential."""
        return KnowledgeSource(
            org=self.org,
            namespace=self.namespace,
            name=self.name,
            source_id=self.source_id,
            kind=self.kind,
            mirror=self.mirror,
            access=MIRRORED_ACCESS,
            active=False,
            pending_delete=True,
        )


def from_row(row: dict[str, Any]) -> Retirement:
    detail = json.loads(row["detail"])
    source_id = str(detail["source_id"])
    if (
        not is_mirror_name(row["namespace"])
        or str(UUID(source_id)) != source_id
        or row["state_key"] != PREFIX + source_id
        or not valid_name(detail["name"])
        or not detail["namespace"]
        or is_mirror_name(detail["namespace"])
    ):
        raise ValueError("SYNC_FAILED")
    return Retirement(
        org=row["org"],
        namespace=detail["namespace"],
        name=detail["name"],
        source_id=source_id,
        kind=detail["kind"],
        mirror=row["namespace"],
    )


async def write(item: Retirement, *, pending: bool, ch: _CHClient | None = None) -> None:
    """A full version row; the source lease serializes enqueue and completion."""
    if str(UUID(item.source_id)) != item.source_id or not is_mirror_name(item.mirror):
        raise ValueError("SYNC_FAILED")
    await (ch or _default_client()).insert(
        TABLE,
        [
            {
                "org": item.org,
                "namespace": item.mirror,
                "state_key": item.key,
                "state_value": "pending" if pending else "",
                "detail": json.dumps(
                    {
                        "namespace": item.namespace,
                        "name": item.name,
                        "source_id": item.source_id,
                        "kind": item.kind,
                    },
                    sort_keys=True,
                )
                if pending
                else "",
            }
        ],
    )


async def page(after: tuple[str, str], *, ch: _CHClient | None = None) -> list[dict[str, Any]]:
    """Global worker discovery, ordered by registration id and org, never file data."""
    rows = await (ch or _default_client()).query_all(
        f"SELECT org, namespace, state_key, detail, hex(SHA256(org)) AS org_cursor "
        f"FROM {TABLE} FINAL "
        "WHERE startsWith(state_key, {prefix:String}) AND state_value = 'pending' "
        "AND match(state_key, {key_form:String}) "
        f"AND {mirror_name_sql()} "
        "AND (state_key, org_cursor) > ({key:String}, {org_hash:String}) "
        f"ORDER BY state_key, org_cursor LIMIT {PAGE_SIZE}",
        params={
            "prefix": PREFIX,
            "key_form": f"^{PREFIX}{_ID_FORM}$",
            "key": after[0],
            "org_hash": after[1],
        },
    )
    return [dict(r) for r in rows]


async def pending(item: Retirement, *, ch: _CHClient | None = None) -> bool:
    row = await (ch or _default_client()).query_one(
        f"SELECT state_value FROM {TABLE} FINAL WHERE org = {{org:String}} "
        "AND namespace = {ns:String} AND state_key = {key:String} LIMIT 1",
        params={"org": item.org, "ns": item.mirror, "key": item.key},
    )
    return bool(row and row["state_value"] == "pending")
