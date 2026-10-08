"""Derive private code-prefix rows from the verified new full-vector staging table."""

from typing import Any

from server.core.repo_base import _CHClient, _default_client
from server.insights.domain.provider_rebuild_manifest import RebuildManifest, RebuildTablePlan
from server.insights.store.provider_staging import (
    _ORDINAL,
    SourceSchema,
    StagingNames,
    _identifier,
    _owned_snapshot,
    completion_marker,
)
from server.insights.store.provider_target import _identity, _target, target_marker
from server.insights.store.provider_verify import verify_reembedded_target


def _schema(table: RebuildTablePlan) -> SourceSchema:
    return SourceSchema(table.source_uuid, table.engine, table.columns, table.schema_fingerprint)


def _plan(
    manifest: RebuildManifest, model: str, dimensions: int, prefixes: tuple[int, ...]
) -> tuple[RebuildTablePlan, RebuildTablePlan]:
    if (
        not isinstance(model, str)
        or not model
        or type(dimensions) is not int
        or not 1 <= dimensions <= 65535
        or not isinstance(prefixes, tuple)
        or len(prefixes) > 64
        or any(type(dim) is not int or not 1 <= dim <= dimensions for dim in prefixes)
        or len(set(prefixes)) != len(prefixes)
    ):
        raise ValueError("Invalid rebuild prefix projection plan")
    if prefixes != manifest.prefix_dimensions:
        raise ValueError("Prefix projection differs from the durable policy")
    tables = {table.table: table for table in manifest.tables}
    base, twin = tables["code_chunks"], tables["code_chunks_prefix"]
    if base.prepared_uuid is not None or twin.prepared_uuid is not None:
        raise ValueError("Prepared rebuild targets cannot be regenerated")
    if not set(twin.columns).issubset(base.columns):
        raise ValueError("Prefix schema is not a projection of full-vector rows")
    return base, twin


def _projection(
    names: StagingNames, columns: tuple[str, ...], count: int, *, as_tuple: bool = False
) -> str:
    replacements = {
        _ORDINAL: f"toUInt64(({_ORDINAL} - 1) * {count} + {{slot:UInt64}})",
        "embedding_model": "{label:String}",
        "embedding": (
            "CAST(if(L2Norm(head) = 0, head, arrayMap(x -> x / L2Norm(head), head)) "
            "AS Array(BFloat16))"
        ),
        "embedding_dim": "toUInt16(if(empty(head), 0, {dim:UInt16}))",
    }
    selected = ", ".join(replacements.get(column, _identifier(column)) for column in columns)
    if as_tuple:
        selected = f"tuple({selected}) AS row"
    return (
        f"SELECT {selected} FROM (SELECT *, "
        "arraySlice(CAST(embedding AS Array(Float32)), 1, {dim:UInt16}) AS head "
        f"FROM loom.{names.target} FINAL)"
    )


async def _owned_prefix(
    manifest: RebuildManifest, twin: RebuildTablePlan, client: _CHClient
) -> StagingNames:
    names = StagingNames(manifest.job_id, twin.table)
    source = await _owned_snapshot(names, twin.source_uuid, client)
    if str(source["uuid"]) != str(twin.snapshot_uuid) or source["comment"] != completion_marker(
        names, _schema(twin)
    ):
        raise ValueError("Prefix rebuild requires its completed snapshot")
    marker = target_marker(names, twin.snapshot_uuid, manifest.embedding_fingerprint)
    if _identity(await _target(names, client), marker) != twin.target_uuid:
        raise ValueError("Prefix rebuild target identity changed")
    return names


async def verify_staged_prefixes(
    manifest: RebuildManifest,
    *,
    model: str,
    dimensions: int,
    prefix_dimensions: tuple[int, ...],
    ch: _CHClient | None = None,
) -> int:
    """Prove exact projected rows, metadata and multiplicity for every configured twin."""
    base, twin = _plan(manifest, model, dimensions, prefix_dimensions)
    client = ch or _default_client()
    base_names = StagingNames(manifest.job_id, base.table)
    count = await verify_reembedded_target(
        base_names,
        _schema(base),
        base.snapshot_uuid,
        base.target_uuid,
        manifest.embedding_fingerprint,
        model=model,
        dimensions=dimensions,
        ch=client,
    )
    names = await _owned_prefix(manifest, twin, client)
    result = await client.query_one(f"SELECT count() AS rows FROM loom.{names.target} FINAL")
    expected = count * len(prefix_dimensions)
    if result is None or type(result.get("rows")) is not int or result["rows"] != expected:
        raise ValueError("Prefix rebuild row coverage is incomplete or unknown")
    columns = (_ORDINAL, *twin.columns)
    selected = ", ".join(
        "toString(embedding_model)" if column == "embedding_model" else _identifier(column)
        for column in columns
    )
    projection = _projection(base_names, columns, len(prefix_dimensions), as_tuple=True)
    for slot, dim in enumerate(prefix_dimensions, start=1):
        result = await client.query_one(
            "SELECT count() AS mismatches FROM (SELECT row FROM ("
            f"SELECT tuple({selected}) AS row, toInt64(1) AS delta FROM loom.{names.target} FINAL "
            "WHERE embedding_model = {label:String} UNION ALL "
            f"SELECT row, toInt64(-1) AS delta FROM ({projection})"
            ") GROUP BY row HAVING sum(delta) != 0) "
            "SETTINGS enable_named_columns_in_function_tuple = 0",
            {"slot": slot, "dim": dim, "label": f"prefix:{dim}:{model}"},
        )
        if result is None or type(result.get("mismatches")) is not int or result["mismatches"] != 0:
            raise ValueError("Staged prefix projection differs or cannot be verified")
    return expected


async def derive_staged_prefixes(
    manifest: RebuildManifest,
    *,
    model: str,
    dimensions: int,
    prefix_dimensions: tuple[int, ...],
    ch: _CHClient | None = None,
) -> int:
    """Rebuild only private derived rows under the live exclusive drain lease.

    The caller freezes configured prefix dimensions for the transition. Empty
    configuration clears stale twins. A retry reconstructs the complete private
    result; neither live table nor the process-wide vector map is changed.
    """
    base, twin = _plan(manifest, model, dimensions, prefix_dimensions)
    client = ch or _default_client()
    base_names = StagingNames(manifest.job_id, base.table)
    await verify_reembedded_target(
        base_names,
        _schema(base),
        base.snapshot_uuid,
        base.target_uuid,
        manifest.embedding_fingerprint,
        model=model,
        dimensions=dimensions,
        ch=client,
    )
    names = await _owned_prefix(manifest, twin, client)
    await client.execute(f"TRUNCATE TABLE loom.{names.target} SYNC")
    columns = (_ORDINAL, *twin.columns)
    select = _projection(base_names, columns, len(prefix_dimensions))
    for slot, dim in enumerate(prefix_dimensions, start=1):
        params: dict[str, Any] = {"slot": slot, "dim": dim, "label": f"prefix:{dim}:{model}"}
        await client.execute(
            f"INSERT INTO loom.{names.target} ({', '.join(_identifier(c) for c in columns)}) "
            f"{select} SETTINGS async_insert = 0",
            params,
        )
    return await verify_staged_prefixes(
        manifest, model=model, dimensions=dimensions, prefix_dimensions=prefix_dimensions, ch=client
    )
