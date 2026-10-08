"""Read-only schema preflight for the embedding rebuild coordinator."""

from uuid import UUID

from server.core.repo_base import _CHClient, _default_client
from server.insights.domain.provider_rebuild import EmbeddingSurface, rebuild_surfaces
from server.insights.domain.provider_rebuild_manifest import RebuildManifest
from server.insights.store.provider_staging import StagingNames


def _owned_tables(manifest: RebuildManifest | None) -> dict[str, UUID]:
    if manifest is None:
        return {}
    owned = {}
    for table in manifest.tables:
        names = StagingNames(manifest.job_id, table.table)
        owned[names.snapshot] = table.snapshot_uuid
        owned[names.target] = table.target_uuid
        if table.prepared_uuid is not None:
            owned[names.prepared] = table.prepared_uuid
            owned[names.backup] = table.source_uuid
    return owned


async def rebuild_inventory(
    *, manifest: RebuildManifest | None = None, ch: _CHClient | None = None
) -> tuple[EmbeddingSurface, ...]:
    """Check physical vector columns before any model transition writes data.

    Materialized views repeat source columns; their destination tables, not the
    views themselves, are the persisted surfaces that need migration.
    On recovery, only exact name/UUID pairs in the durable manifest are excluded
    from live inventory. A matching prefix, another job's copy or a same-name
    replacement is never sufficient. Unjournaled copies must first be reconciled
    through their completion receipts, not silently ignored here.
    """
    client = ch or _default_client()
    return await _inventory_excluding(_owned_tables(manifest), client)


async def _inventory_excluding(
    owned: dict[str, UUID], client: _CHClient
) -> tuple[EmbeddingSurface, ...]:
    """Internal store helper; exclusions must already have ownership/UUID proof."""
    columns = await client.query_all(
        "SELECT c.table AS table, c.name AS name, c.type AS type, t.uuid AS table_uuid "
        "FROM system.columns AS c "
        "INNER JOIN system.tables AS t ON c.database = t.database AND c.table = t.name "
        "WHERE c.database = 'loom' "
        "AND (position(c.type, 'Array(') > 0 OR startsWith(c.type, 'QBit(')) "
        "AND t.engine NOT IN ('View', 'MaterializedView')"
    )
    live = []
    for column in columns:
        expected = owned.get(column["table"])
        if expected is None:
            live.append(column)
        elif str(column.get("table_uuid")) != str(expected):
            raise ValueError("Rebuild inventory found a replaced staging table")
    return rebuild_surfaces(live)
