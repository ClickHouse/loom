"""Remove journaled rebuild copies only after every live replacement is installed.

Caller holds a live exclusive fence, with readers, writers and deferred work
drained, on one pinned ClickHouse node. Local process inspection supplements that
barrier; it is not a substitute for a distributed acknowledgement.
"""

from uuid import UUID

from server.core.repo_base import _CHClient, _default_client
from server.insights.domain.provider_rebuild_manifest import RebuildManifest
from server.insights.store.provider_staging import StagingNames, _identifier


def _identities(manifest: RebuildManifest) -> tuple[dict[str, UUID], dict[str, UUID]]:
    manifest.require_prepared()
    live = {}
    copies = {}
    for table in manifest.tables:
        names = StagingNames(manifest.job_id, table.table)
        assert table.prepared_uuid is not None  # require_prepared establishes this for every row.
        live[table.table] = table.prepared_uuid
        copies[names.snapshot] = table.snapshot_uuid
        copies[names.target] = table.target_uuid
        copies[names.backup] = table.source_uuid
    return live, copies


async def _inspect(manifest: RebuildManifest, database: str, client: _CHClient) -> dict[str, UUID]:
    live, copies = _identities(manifest)
    rows = await client.query_all(
        "SELECT name, uuid FROM system.tables WHERE database = {database:String} "
        "AND (name IN {live:Array(String)} OR startsWith(name, {prefix:String}))",
        {
            "database": database,
            "live": list(live),
            "prefix": f"_provider_rebuild_{manifest.job_id.hex}_",
        },
    )
    present = {str(row["name"]): UUID(str(row["uuid"])) for row in rows}
    if len(present) != len(rows) or any(
        present.get(name) != identity for name, identity in live.items()
    ):
        raise ValueError("Cleanup requires all published live table identities")
    private = {name: identity for name, identity in present.items() if name not in live}
    if any(copies.get(name) != identity for name, identity in private.items()):
        raise ValueError("Cleanup found an unjournaled or replaced rebuild copy")
    return private


async def _assert_idle(tables: list[str], database: str, client: _CHClient) -> None:
    queries = await client.query_one(
        "SELECT count() AS running FROM system.processes WHERE query_id != currentQueryID() "
        "AND arrayExists(name -> position(query, name) > 0, {tables:Array(String)})",
        {"tables": tables},
    )
    mutations = await client.query_one(
        "SELECT count() AS running FROM system.mutations WHERE database = {database:String} "
        "AND table IN {tables:Array(String)} AND NOT is_done",
        {"database": database, "tables": tables},
    )
    for result in (queries, mutations):
        if result is None or type(result.get("running")) is not int or result["running"] != 0:
            raise ValueError("Rebuild cleanup work is running or its status is unknown")


async def cleanup_rebuild_copies(
    manifest: RebuildManifest,
    *,
    database: str = "loom",
    ch: _CHClient | None = None,
) -> int:
    """Resume cleanup by UUID; never delete live tables or infer ownership by name.

    Missing private copies are an idempotent success. Any unexpected identity
    fails the whole preflight before the first DROP. Keep the fence and admission
    closed through the final census. This does not activate a provider.
    """
    db = _identifier(database)
    client = ch or _default_client()
    private = await _inspect(manifest, database, client)
    live, copies = _identities(manifest)
    await _assert_idle([*live, *copies], database, client)
    for name in private:
        # IF EXISTS tolerates the shared client's replay after a lost DROP reply.
        # The exclusive fence excludes same-name replacement throughout cleanup.
        await client.execute(f"DROP TABLE IF EXISTS {db}.{_identifier(name)} SYNC")
    if await _inspect(manifest, database, client):
        raise ValueError("Rebuild cleanup left private copies behind")
    return len(private)
