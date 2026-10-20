"""Fail-closed verification of staged re-embedding before publication."""

from uuid import UUID

from server.core.repo_base import _CHClient, _default_client
from server.insights.domain.provider_rebuild import EMBEDDING_SURFACES
from server.insights.store.provider_staging import (
    _ORDINAL,
    SourceSchema,
    StagingNames,
    _identifier,
    _owned_snapshot,
    completion_marker,
)
from server.insights.store.provider_target import _identity, _target, target_marker, updated_fields
from server.memory.domain.sensitivity import reads_high


def _vector_checks(schema: SourceSchema, columns: list[str]) -> str:
    checks = ["t.embedding_model != {model:String}"]
    active = "isNull(s.tombstoned_at)" if "tombstoned_at" in schema.columns else "1"
    for column in columns:
        vector = f"t.{_identifier(column)}"
        norm = f"arraySum(arrayMap(x -> toFloat64(x) * toFloat64(x), {vector}))"
        checks.extend(
            [
                f"length({vector}) NOT IN (0, {{dim:UInt32}})",
                f"NOT arrayAll(x -> isFinite(toFloat64(x)), {vector})",
                f"(notEmpty({vector}) AND ({norm} <= 0 OR NOT isFinite({norm})))",
            ]
        )
        if "tombstoned_at" in schema.columns:
            checks.append(f"(notEmpty({vector}) AND NOT {active})")
        dimension = (
            "memory_key_embedding_dim" if column == "memory_key_embedding" else "embedding_dim"
        )
        if dimension in schema.columns:
            checks.append(f"t.{_identifier(dimension)} != length({vector})")
    return " OR ".join(checks)


def _emptied(schema: SourceSchema, columns: list[str]) -> str:
    """A live row staged without a vector whose source holds a real one. An all-zero
    source is a placeholder (vectors/placeholders.py) and rebuilds empty, as in
    provider_rebuild_batch._inputs."""
    active = "isNull(s.tombstoned_at)" if "tombstoned_at" in schema.columns else "1"
    return " OR ".join(
        f"(empty(t.{_identifier(c)}) AND arrayExists(x -> x != 0, s.{_identifier(c)}) AND {active})"
        for c in columns
    )


async def verify_reembedded_target(
    names: StagingNames,
    schema: SourceSchema,
    snapshot_uuid: UUID,
    target_uuid: UUID,
    embedding_fingerprint: str,
    *,
    model: str,
    dimensions: int,
    passage_text: str | None = None,
    ch: _CHClient | None = None,
) -> int:
    """Verify exact row coverage, preserved metadata and the new vector space.

    `passage_text` names the text column of a raw-passage table: a row there that the
    rebuild staged without a vector is accepted when that text reads as a credential,
    as its write path leaves it (`provider_rebuild_batch.PASSAGE_TABLES`).

    Returns the logical row count. Prefix derivation and cache invalidation have
    different completeness rules and must not use this verifier. The coordinator
    must keep all writers drained through verification and publication.
    """
    vectors = [
        s.column for s in EMBEDDING_SURFACES if s.table == names.table and s.action == "reembed"
    ]
    if not vectors or not model or type(dimensions) is not int or not 1 <= dimensions <= 65535:
        raise ValueError("Invalid re-embedding verification plan")
    client = ch or _default_client()
    source = await _owned_snapshot(names, schema.table_uuid, client)
    if UUID(str(source["uuid"])) != snapshot_uuid or source["comment"] != completion_marker(
        names, schema
    ):
        raise ValueError("Verification requires the completed source snapshot")
    marker = target_marker(names, snapshot_uuid, embedding_fingerprint)
    if _identity(await _target(names, client), marker) != target_uuid:
        raise ValueError("Rebuild target identity changed")
    immutable = [
        name
        for name in schema.columns
        if name not in vectors and name not in updated_fields(names.table)
    ]
    metadata = (
        " OR ".join(
            f"NOT isNotDistinctFrom(s.{_identifier(name)}, t.{_identifier(name)})"
            for name in immutable
        )
        or "0"
    )
    vector_checks = _vector_checks(schema, vectors)
    emptied = _emptied(schema, vectors)
    joined = (
        f"loom.{names.snapshot} AS s FULL OUTER JOIN "
        f"(SELECT * FROM loom.{names.target} FINAL) AS t ON s.{_ORDINAL} = t.{_ORDINAL}"
    )
    if names.table == "qa_item_embeddings":
        vector_checks += " OR NOT match(toString(t.text_sha256), '^[0-9a-f]{64}$')"
    result = await client.query_one(
        f"SELECT countIf(s.{_ORDINAL} > 0) AS source_rows, "
        f"countIf(t.{_ORDINAL} > 0) AS target_rows, "
        f"countIf(s.{_ORDINAL} = 0 OR t.{_ORDINAL} = 0) AS missing, "
        f"countIf({metadata}) AS metadata_errors, "
        f"countIf({vector_checks}) AS vector_errors, "
        f"countIf({emptied}) AS emptied "
        f"FROM {joined} SETTINGS join_use_nulls = 0",
        {"model": model, "dim": dimensions},
    )
    fields = (
        "source_rows",
        "target_rows",
        "missing",
        "metadata_errors",
        "vector_errors",
        "emptied",
    )
    if result is None or any(
        type(result.get(name)) is not int or result[name] < 0 for name in fields
    ):
        raise ValueError("Cannot establish rebuild verification result")
    if result["emptied"] and passage_text:
        # Few rows: only those the rebuild left empty. Their text is read whole and checked
        # here, as the write path checks it.
        texts = await client.query_all(
            f"SELECT s.{_identifier(passage_text)} AS text FROM {joined} "
            f"WHERE {emptied} SETTINGS join_use_nulls = 0"
        )
        result["emptied"] = sum(1 for r in texts if not reads_high(str(r["text"])))
    if result["source_rows"] != result["target_rows"] or any(result[name] for name in fields[2:]):
        raise ValueError("Staged rebuild failed verification")
    await verify_live_key_uniqueness(names, schema, client)
    return int(result["source_rows"])


async def verify_live_key_uniqueness(
    names: StagingNames, schema: SourceSchema, client: _CHClient
) -> None:
    """Reject rows that would silently collapse under the live table's engine.

    Ordinal-keyed staging can retain two old-model rows that map to the same
    new-model sorting key. Publication needs an explicit canonical-row policy
    for that case, not ReplacingMergeTree's incidental merge ordering.
    This internal check follows target ownership and row verification.
    """
    live = await client.query_one(
        "SELECT uuid, engine, sorting_key FROM system.tables "
        "WHERE database = 'loom' AND name = {table:String}",
        {"table": names.table},
    )
    if (
        live is None
        or UUID(str(live["uuid"])) != schema.table_uuid
        or live["engine"] != schema.engine
    ):
        raise ValueError("Live rebuild table identity changed")
    if not schema.engine.endswith(("ReplacingMergeTree", "AggregatingMergeTree")):
        return
    keys = [key.strip().strip("`") for key in str(live["sorting_key"]).split(",")]
    if not keys or any(key not in schema.columns for key in keys):
        raise ValueError("Unsupported live rebuild sorting key")
    grouped = ", ".join(_identifier(key) for key in keys)
    result = await client.query_one(
        f"SELECT count() AS collisions FROM (SELECT {grouped} FROM loom.{names.target} FINAL "
        f"GROUP BY {grouped} HAVING count() > 1)",
    )
    if result is None or type(result.get("collisions")) is not int or result["collisions"] < 0:
        raise ValueError("Cannot establish live-key uniqueness")
    if result["collisions"]:
        raise ValueError("Rebuilt rows collide under the live sorting key")
