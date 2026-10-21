"""Full-row, retry-safe batch staging; no operation here writes a live table.

Targets use the snapshot ordinal as their replacing key. Repeating an insert
after a lost response cannot add a second logical row under FINAL. Readers and
publication must use FINAL; physical duplicates may exist until merges run.
All calls require the rebuild coordinator's exclusive lease and drained writers.
"""

import json
import re
from typing import Any
from uuid import UUID

from clickhouse_connect.driver.external import ExternalData

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

_UPDATED_FIELDS = {
    "embedding_model": "String",
    "embedding_dim": "UInt32",
    "memory_key_embedding_dim": "UInt32",
    "embed_attempts": "UInt32",
    "embed_last_error": "String",
    "embed_failed_at": "Nullable(DateTime)",
}


def updated_fields(table: str) -> dict[str, str]:
    """Tracker source reconciliation changes only its exact-text digest."""
    return {
        **_UPDATED_FIELDS,
        **({"text_sha256": "String"} if table == "qa_item_embeddings" else {}),
    }


def target_marker(names: StagingNames, snapshot_uuid: UUID, embedding_fingerprint: str) -> str:
    if not isinstance(snapshot_uuid, UUID) or snapshot_uuid.int == 0:
        raise ValueError("Invalid snapshot identity")
    if not re.fullmatch(r"[a-f0-9]{64}", embedding_fingerprint):
        raise ValueError("Invalid embedding-space fingerprint")
    return f"provider-rebuild:{names.job_id}:{snapshot_uuid}:target:{embedding_fingerprint}"


async def _target(names: StagingNames, client: _CHClient) -> dict[str, Any] | None:
    return await client.query_one(
        "SELECT uuid, comment, engine FROM system.tables "
        "WHERE database = currentDatabase() AND name = {table:String}",
        {"table": names.target},
    )


def _identity(row: dict[str, Any] | None, marker: str) -> UUID:
    if row is None or row["comment"] != marker or row["engine"] != "ReplacingMergeTree":
        raise ValueError("Rebuild target is missing or not owned by this plan")
    identity = UUID(str(row["uuid"]))
    if identity.int == 0:
        raise ValueError("Rebuild target requires a stable Atomic identity")
    return identity


async def ensure_target(
    names: StagingNames,
    schema: SourceSchema,
    snapshot_uuid: UUID,
    embedding_fingerprint: str,
    *,
    ch: _CHClient | None = None,
) -> UUID:
    """Create an empty target or resume the same source/model-specific target."""
    client = ch or _default_client()
    marker = target_marker(names, snapshot_uuid, embedding_fingerprint)
    source = await _owned_snapshot(names, schema.table_uuid, client)
    if UUID(str(source["uuid"])) != snapshot_uuid or source["comment"] != completion_marker(
        names, schema
    ):
        raise ValueError("Target requires the completed source snapshot")
    existing = await _target(names, client)
    if existing is not None:
        return _identity(existing, marker)
    # AS <table> copies columns but no rows; there is no partial copy to accept.
    await client.execute(
        f"CREATE TABLE loom.{names.target} AS loom.{names.snapshot} "
        f"ENGINE = ReplacingMergeTree ORDER BY {_ORDINAL} COMMENT '{marker}'"
    )
    return _identity(await _target(names, client), marker)


async def write_target_page(
    names: StagingNames,
    schema: SourceSchema,
    snapshot_uuid: UUID,
    target_uuid: UUID,
    embedding_fingerprint: str,
    rows: list[dict[str, Any]],
    *,
    after: int,
    ch: _CHClient | None = None,
) -> None:
    """Insert a bounded contiguous page, preserving the complete column set.

    The caller validates regenerated vectors and checkpoints only after this
    synchronous insert returns. It may retry the same page at the same cursor.
    """
    if type(after) is not int or after < 0 or not 1 <= len(rows) <= 512:
        raise ValueError("Invalid target page bounds")
    columns = {*schema.columns, _ORDINAL}
    for index, row in enumerate(rows, start=after + 1):
        if set(row) != columns:
            raise ValueError("Rebuild target page must carry every source column")
        if type(row[_ORDINAL]) is not int or row[_ORDINAL] != index:
            raise ValueError("Rebuild target page has invalid ordinals")
    client = ch or _default_client()
    marker = target_marker(names, snapshot_uuid, embedding_fingerprint)
    if _identity(await _target(names, client), marker) != target_uuid:
        raise ValueError("Rebuild target identity changed")
    source = await _owned_snapshot(names, schema.table_uuid, client)
    if UUID(str(source["uuid"])) != snapshot_uuid or source["comment"] != completion_marker(
        names, schema
    ):
        raise ValueError("Target requires the completed source snapshot")
    await _insert_patches(names, schema, rows, client)


async def _insert_patches(
    names: StagingNames, schema: SourceSchema, rows: list[dict[str, Any]], client: _CHClient
) -> None:
    # Keep unchanged metadata inside ClickHouse. A Python round-trip of naive
    # DateTime values can reinterpret them in the host timezone; DateTime64(9)
    # would also lose precision. Only embedding-related fields cross the wire.
    types = {
        surface.column: "Array(Float64)"
        for surface in EMBEDDING_SURFACES
        if surface.table == names.table
    }
    types.update(
        {name: kind for name, kind in updated_fields(names.table).items() if name in schema.columns}
    )
    fields = [name for name in schema.columns if name in types]
    if not fields:
        raise ValueError("Target page has no embedding fields")
    # Multipart file data has no HTML-form field-size ceiling. A 64-row page
    # of 3072-dimensional vectors already exceeds that ceiling; shrinking
    # pages cannot accommodate an arbitrarily large individual vector either.
    payload = "\n".join(
        json.dumps(
            {_ORDINAL: row[_ORDINAL], **{name: row[name] for name in fields}}, allow_nan=False
        )
        for row in rows
    ).encode()
    updates = ExternalData(
        file_name="provider_updates",
        data=payload,
        fmt="JSONEachRow",
        structure=[f"{_ORDINAL} UInt64", *(f"{name} {types[name]}" for name in fields)],
    )
    replacements = {name: f"updates.{_identifier(name)}" for name in fields}
    columns = [_ORDINAL, *schema.columns]
    selected = ", ".join(
        f"{replacements.get(name, 'source.' + _identifier(name))} AS {_identifier(name)}"
        for name in columns
    )
    await client.execute(
        f"INSERT INTO loom.{names.target} ({', '.join(_identifier(name) for name in columns)}) "
        f"SELECT {selected} FROM loom.{names.snapshot} AS source "
        "INNER JOIN provider_updates AS updates "
        f"ON source.{_ORDINAL} = updates.{_ORDINAL} SETTINGS async_insert = 0",
        external_data=updates,
    )
