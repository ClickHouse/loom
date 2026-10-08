"""Read-only ownership reconciliation before creating a snapshot manifest."""

from typing import Any
from uuid import UUID

from server.core.repo_base import _CHClient, _default_client
from server.insights.domain.provider_rebuild import REBUILD_TABLES
from server.insights.store.provider_rebuild import _inventory_excluding
from server.insights.store.provider_staging import (
    SourceSchema,
    StagingNames,
    completion_marker,
    source_schema,
)
from server.insights.store.provider_target import _identity, target_marker


def _snapshot_receipt(names: StagingNames, schema: SourceSchema, row: dict[str, Any]) -> UUID:
    if row["engine"] != "MergeTree" or row["comment"] not in (
        names.marker(schema.table_uuid),
        completion_marker(names, schema),
    ):
        raise ValueError("Snapshot recovery found a foreign or changed source copy")
    identity = UUID(str(row["uuid"]))
    if identity.int == 0:
        raise ValueError("Snapshot recovery requires stable table identities")
    return identity


def _recovery_copies(
    job_id: UUID, fingerprint: str, schemas: dict[str, SourceSchema], rows: list[dict[str, Any]]
) -> dict[str, UUID]:
    present = {row["name"]: row for row in rows}
    if len(present) != len(rows):
        raise ValueError("Ambiguous snapshot recovery table identities")
    owned = {}
    for table, schema in schemas.items():
        names = StagingNames(job_id, table)
        snapshot = present.get(names.snapshot)
        target = present.get(names.target)
        if snapshot is None:
            if target is not None:
                raise ValueError("Recovery target has no source snapshot")
            continue
        identity = _snapshot_receipt(names, schema, snapshot)
        owned[names.snapshot] = identity
        if target is not None:
            if snapshot["comment"] != completion_marker(names, schema):
                raise ValueError("Recovery target refers to an unfinished source snapshot")
            owned[names.target] = _identity(target, target_marker(names, identity, fingerprint))
    if set(present) != set(owned):
        raise ValueError("Snapshot recovery found unexpected private tables")
    return owned


async def preflight_snapshot_recovery(
    job_id: UUID, fingerprint: str, *, ch: _CHClient | None = None
) -> dict[str, SourceSchema]:
    """Validate all live schemas and partial-copy ownership before the first write.

    Incomplete sources may be recognized for inventory accounting but are not
    accepted as completed snapshots. ensure_snapshot still waits for idle work
    and rebuilds them. Requires a pinned node and exclusive drained-writer lease.
    """
    client = ch or _default_client()
    schemas = {}
    for table in REBUILD_TABLES:
        names = StagingNames(job_id, table)
        schemas[table] = await source_schema(names, ch=client)
        target_marker(names, schemas[table].table_uuid, fingerprint)
    rows = await client.query_all(
        "SELECT name, uuid, engine, comment FROM system.tables WHERE database = 'loom' "
        "AND startsWith(name, {prefix:String})",
        {"prefix": f"_provider_rebuild_{job_id.hex}_"},
    )
    owned = _recovery_copies(job_id, fingerprint, schemas, rows)
    await _inventory_excluding(owned, client)
    return schemas
