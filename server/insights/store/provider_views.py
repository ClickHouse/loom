"""Rebind journaled explicit-TO views after all replacement tables are live."""

from uuid import UUID, uuid4

from server.core.ch_names import database, unqualify
from server.core.repo_base import _CHClient, _default_client
from server.insights.domain.provider_rebuild import REBUILD_TABLES
from server.insights.domain.provider_views import RebuildView


async def capture_rebuild_views(*, ch: _CHClient | None = None) -> tuple[RebuildView, ...]:
    client = ch or _default_client()
    sources = await client.query_all(
        "SELECT dependencies_database, dependencies_table FROM system.tables "
        "WHERE database = currentDatabase() AND name IN {tables:Array(String)}",
        {"tables": list(REBUILD_TABLES)},
    )
    dependencies = {
        (database, table)
        for source in sources
        for database, table in zip(
            source["dependencies_database"], source["dependencies_table"], strict=True
        )
    }
    if any(name != database() for name, _ in dependencies):
        raise ValueError("Cross-database rebuild view requires an explicit publication plan")
    if not dependencies:
        return ()
    rows = await client.query_all(
        "SELECT name, uuid, engine, create_table_query FROM system.tables "
        "WHERE database = currentDatabase() AND name IN {names:Array(String)}",
        {"names": sorted(table for _, table in dependencies)},
    )
    if {str(row["name"]) for row in rows} != {table for _, table in dependencies}:
        raise ValueError("Rebuild view dependency metadata is incomplete")
    plans = []
    for row in rows:
        if row["engine"] != "MaterializedView":
            raise ValueError("Rebuild dependency is not a supported materialized view")
        plan = RebuildView(
            str(row["name"]),
            UUID(str(row["uuid"])),
            uuid4(),
            unqualify(str(row["create_table_query"])),
        )
        plan.creation_sql()
        plans.append(plan)
    return tuple(sorted(plans, key=lambda plan: plan.name))


async def rebind_rebuild_views(
    plans: tuple[RebuildView, ...], *, ch: _CHClient | None = None
) -> None:
    """Caller owns the fence and has published every live table. UUIDs journal retries."""
    if not plans:
        return
    client = ch or _default_client()
    if len({plan.name for plan in plans}) != len(plans):
        raise ValueError("Duplicate rebuild view names")
    rows = await client.query_all(
        "SELECT name, uuid FROM system.tables WHERE database=currentDatabase() "
        "AND name IN {names:Array(String)}",
        {"names": [plan.name for plan in plans]},
    )
    present = {str(row["name"]): UUID(str(row["uuid"])) for row in rows}
    for plan in plans:
        plan.creation_sql()
        if present.get(plan.name) not in (None, plan.original, plan.replacement):
            raise ValueError("Materialized view differs from durable rebuild plan")
    for plan in plans:
        if present.get(plan.name) == plan.replacement:
            continue
        if present.get(plan.name) == plan.original:
            await client.execute(f"DROP VIEW `loom`.`{plan.name}` SYNC")
        await client.execute(plan.creation_sql())
    rows = await client.query_all(
        "SELECT name, uuid FROM system.tables WHERE database=currentDatabase() "
        "AND name IN {names:Array(String)}",
        {"names": [plan.name for plan in plans]},
    )
    actual = {str(row["name"]): UUID(str(row["uuid"])) for row in rows}
    if actual != {plan.name: plan.replacement for plan in plans}:
        raise ValueError("Rebuilt materialized-view identities are unconfirmed")
