"""Build a live-schema candidate without touching the live table.

The caller owns the fenced drain lease and has verified the staged data. Only
ordinary local MergeTree engines are accepted here; replicated/shared engines
need a separate DDL plan, not blindly copied replication paths.
"""

from typing import Any
from uuid import UUID

from server.core.ch_names import qualify, unqualify
from server.core.repo_base import _CHClient, _default_client
from server.core.schema.vector_codec_ddl import statement_settings
from server.insights.domain.provider_prepared_ddl import PreparedDDL, prepared_ddl
from server.insights.domain.provider_rebuild import PUBLISHABLE_ENGINES
from server.insights.store.provider_staging import (
    SourceSchema,
    StagingNames,
    _identifier,
    completion_marker,
    source_schema,
)
from server.insights.store.provider_target import _identity, _target, target_marker


async def _prepared(names: StagingNames, client: _CHClient) -> dict[str, Any] | None:
    return await client.query_one(
        "SELECT uuid, comment FROM system.tables "
        "WHERE database = currentDatabase() AND name = {table:String}",
        {"table": names.prepared},
    )


async def _assert_idle(names: StagingNames, client: _CHClient) -> None:
    prefixes = [
        qualify(f"{op} loom.{names.prepared} ")
        for op in ("CREATE TABLE", "INSERT INTO", "ALTER TABLE")
    ]
    result = await client.query_one(
        "SELECT count() AS running FROM system.processes "
        "WHERE arrayExists(prefix -> startsWith(query, prefix), {prefixes:Array(String)})",
        {"prefixes": prefixes},
    )
    if result is None or type(result.get("running")) is not int or result["running"] != 0:
        raise ValueError("Prepared-table work is running or its status is unknown")


async def _count(names: StagingNames, schema: SourceSchema, client: _CHClient) -> int:
    result = await client.query_one(
        f"SELECT count() AS rows FROM loom.{names.prepared}{schema.final}"
    )
    if result is None or type(result.get("rows")) is not int or result["rows"] < 0:
        raise ValueError("Cannot establish prepared-table row count")
    return int(result["rows"])


async def _verify_copy(
    names: StagingNames,
    schema: SourceSchema,
    client: _CHClient,
    *,
    projection: str | None = None,
) -> None:
    """Compare full row multisets inside ClickHouse, including duplicate counts.

    Counts alone cannot detect changed values or one row replacing another.
    Group the actual tuples, not a digest, and keep metadata out of Python's
    datetime/type conversions. The staging ordinal is deliberately excluded.
    """
    columns = ", ".join(_identifier(column) for column in schema.columns)
    result = await client.query_one(
        "SELECT count() AS mismatches FROM ("
        "SELECT row FROM ("
        f"SELECT tuple({columns}) AS row, toInt64(1) AS delta "
        f"FROM loom.{names.prepared}{schema.final} UNION ALL "
        f"SELECT tuple({projection or columns}) AS row, toInt64(-1) AS delta "
        f"FROM loom.{names.target} FINAL"
        ") GROUP BY row HAVING sum(delta) != 0)"
    )
    if result is None or type(result.get("mismatches")) is not int or result["mismatches"] < 0:
        raise ValueError("Cannot establish prepared-table content equality")
    if result["mismatches"]:
        raise ValueError("Prepared-table content differs from verified staging")


async def _ddl_plan(
    names: StagingNames, marker: str, dimensions: int, client: _CHClient
) -> PreparedDDL:
    row = await client.query_one(
        "SELECT uuid, engine, create_table_query FROM system.tables "
        "WHERE database = currentDatabase() AND name = {table:String}",
        {"table": names.table},
    )
    if row is None or not isinstance(row.get("create_table_query"), str):
        raise ValueError("Cannot establish prepared-table DDL")
    ddl = unqualify(row["create_table_query"])
    return prepared_ddl(ddl, names.table, names.prepared, marker, dimensions)


async def _create_candidate(plan: PreparedDDL, client: _CHClient) -> None:
    if not plan.padded_columns:
        await client.execute(plan.sql)
        return
    available = await client.query_all(
        "SELECT name FROM system.settings WHERE name IN "
        "('enable_quantized_codec', 'allow_experimental_codecs')"
    )
    names = {row["name"] for row in available}
    setting = (
        "enable_quantized_codec"
        if "enable_quantized_codec" in names
        else "allow_experimental_codecs"
    )
    if setting not in names:
        raise ValueError("Quantized codec setting is unavailable")
    # A preserved general-codec chain needs this on self-hosted ClickHouse.
    options = {setting: 1}
    if statement_settings(plan.sql).get("allow_suspicious_codecs"):
        options["allow_suspicious_codecs"] = 1
    await client.execute(plan.sql, settings=options)


async def prepare_live_table(
    names: StagingNames,
    schema: SourceSchema,
    snapshot_uuid: UUID,
    target_uuid: UUID,
    embedding_fingerprint: str,
    *,
    expected_rows: int,
    dimensions: int,
    ch: _CHClient | None = None,
) -> UUID:
    """Clone live DDL and fill from verified staging; journal the returned UUID.

    Completion is marked only after checking row count and full row equality. A
    lost response leaves either a reusable completed candidate or an owned copy
    that is rebuilt. Call on one pinned node; local query inspection is not a
    distributed fence. No table is published or traffic reopened here.
    """
    if type(expected_rows) is not int or expected_rows < 0:
        raise ValueError("Invalid prepared-table row count")
    if schema.engine not in PUBLISHABLE_ENGINES:
        raise ValueError("Prepared-table engine requires a separate replication plan")
    client = ch or _default_client()
    if await source_schema(names, ch=client) != schema:
        raise ValueError("Live schema changed before replacement preparation")
    marker = target_marker(names, snapshot_uuid, embedding_fingerprint)
    if _identity(await _target(names, client), marker) != target_uuid:
        raise ValueError("Rebuild target identity changed")
    marker = f"{completion_marker(names, schema)}:publish:{target_uuid}:{embedding_fingerprint}"
    plan = await _ddl_plan(names, marker, dimensions, client)
    projection = plan.projection(schema.columns, dimensions)
    receipt = f"{marker}:rows:{expected_rows}"
    await _assert_idle(names, client)
    existing = await _prepared(names, client)
    if existing is not None:
        if existing["comment"] == receipt:
            if await _count(names, schema, client) != expected_rows:
                raise ValueError("Completed prepared-table row count changed")
            await _verify_copy(names, schema, client, projection=projection)
            return UUID(str(existing["uuid"]))
        if existing["comment"] != marker:
            raise ValueError("Prepared table is not owned by this rebuild plan")
        await client.execute(f"DROP TABLE loom.{names.prepared} SYNC")
    await _create_candidate(plan, client)
    created = await _prepared(names, client)
    if created is None or created["comment"] != marker:
        raise ValueError("Prepared-table creation could not be confirmed")
    identity = UUID(str(created["uuid"]))
    columns = ", ".join(_identifier(column) for column in schema.columns)
    await client.execute(
        f"INSERT INTO loom.{names.prepared} ({columns}) "
        f"SELECT {projection} FROM loom.{names.target} FINAL SETTINGS async_insert = 0"
    )
    if await _count(names, schema, client) != expected_rows:
        raise ValueError("Prepared-table copy changed logical row count")
    await _verify_copy(names, schema, client, projection=projection)
    await client.execute(f"ALTER TABLE loom.{names.prepared} MODIFY COMMENT '{receipt}'")
    return identity
