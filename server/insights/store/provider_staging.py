"""Owned, immutable source snapshots for resumable provider rebuilds.

These helpers require drained writers and an exclusive coordinator lease. They
never modify a live table. Creation is deliberately not IF NOT EXISTS: an
interrupted CREATE AS SELECT must not be mistaken for a complete snapshot.
"""

import hashlib
import json
import re
from dataclasses import dataclass
from typing import Any
from uuid import UUID

from server.core.repo_base import _CHClient, _default_client
from server.insights.domain.provider_rebuild import EMBEDDING_SURFACES, REBUILD_TABLES
from server.insights.domain.provider_source_policy import MODEL_KEYED_SOURCES, SOURCE_POLICY_VERSION

_ORDINAL = "_loom_rebuild_ordinal"
_IDENTIFIER = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


def _owned_marker(comment: str, marker: str) -> bool:
    return comment == marker or bool(
        re.fullmatch(re.escape(marker) + r":complete:[a-f0-9]{64}", comment)
    )


def _identifier(value: str) -> str:
    if not _IDENTIFIER.fullmatch(value):
        raise ValueError("Unsupported rebuild column identifier")
    return f"`{value}`"


@dataclass(frozen=True)
class StagingNames:
    job_id: UUID
    table: str

    def __post_init__(self) -> None:
        if not isinstance(self.job_id, UUID) or self.table not in REBUILD_TABLES:
            raise ValueError("Unsupported rebuild table")

    @property
    def snapshot(self) -> str:
        return f"_provider_rebuild_{self.job_id.hex}_{self.table}_source"

    @property
    def target(self) -> str:
        return f"_provider_rebuild_{self.job_id.hex}_{self.table}_target"

    @property
    def prepared(self) -> str:
        return f"_provider_rebuild_{self.job_id.hex}_{self.table}_publish"

    @property
    def backup(self) -> str:
        return f"_provider_rebuild_{self.job_id.hex}_{self.table}_backup"

    def marker(self, source_uuid: UUID) -> str:
        if not isinstance(source_uuid, UUID):
            raise ValueError("Invalid source table identity")
        return f"provider-rebuild:{self.job_id}:{source_uuid}:snapshot"


@dataclass(frozen=True)
class SourceSchema:
    table_uuid: UUID
    engine: str
    columns: tuple[str, ...]
    fingerprint: str

    @property
    def final(self) -> str:
        return (
            " FINAL" if self.engine.endswith(("ReplacingMergeTree", "AggregatingMergeTree")) else ""
        )


async def source_schema(names: StagingNames, *, ch: _CHClient | None = None) -> SourceSchema:
    client = ch or _default_client()
    table = await client.query_one(
        "SELECT uuid, engine, create_table_query FROM system.tables "
        "WHERE database = currentDatabase() AND name = {table:String}",
        {"table": names.table},
    )
    if table is None or not str(table["engine"]).endswith("MergeTree"):
        raise ValueError("Rebuild source is missing or has an unsupported engine")
    table_uuid = UUID(str(table["uuid"]))
    if table_uuid.int == 0:
        raise ValueError("Rebuild requires stable Atomic table identities")
    columns = await client.query_all(
        "SELECT name, type, default_kind, default_expression FROM system.columns "
        "WHERE database = currentDatabase() AND table = {table:String} ORDER BY position",
        {"table": names.table},
    )
    writable = tuple(
        str(row["name"]) for row in columns if row["default_kind"] not in ("MATERIALIZED", "ALIAS")
    )
    if not writable or _ORDINAL in writable:
        raise ValueError("Rebuild source has invalid columns")
    for column in writable:
        _identifier(column)
    fingerprint = hashlib.sha256(
        json.dumps(
            {
                "ddl": table["create_table_query"],
                "columns": columns,
                "source_policy_version": SOURCE_POLICY_VERSION,
            },
            sort_keys=True,
        ).encode()
    ).hexdigest()
    schema = SourceSchema(table_uuid, str(table["engine"]), writable, fingerprint)
    _source_selection(names, schema)
    return schema


def _source_selection(names: StagingNames, schema: SourceSchema) -> str:
    """Choose a whole latest row across old labels; equal revisions prefer deletion.

    A later active revision can represent an explicit restore. At an equal
    revision a tombstone wins, preventing a historical active model row from
    reviving erased content. The old label breaks any remaining tie consistently.
    """
    columns = ", ".join(_identifier(name) for name in schema.columns)
    source = f"SELECT {columns} FROM loom.{names.table}{schema.final}"
    policy = MODEL_KEYED_SOURCES.get(names.table)
    if policy is None:
        return source
    required = {*policy.keys, policy.revision, "embedding_model", "tombstoned_at"}
    if not required.issubset(schema.columns) or not schema.engine.endswith("ReplacingMergeTree"):
        raise ValueError("Unsupported model-keyed source schema")
    keys = ", ".join(_identifier(key) for key in policy.keys)
    return (
        f"{source} ORDER BY {_identifier(policy.revision)} DESC, "
        f"isNull(`tombstoned_at`) ASC, `embedding_model` ASC LIMIT 1 BY {keys}"
    )


async def create_snapshot(
    names: StagingNames, schema: SourceSchema, *, ch: _CHClient | None = None
) -> UUID:
    """Create a full-row source copy with a stable, one-based paging ordinal.

    The coordinator journals the returned UUID only after this returns. If the
    query fails, the owned partial copy must be explicitly discarded and rebuilt.
    """
    client = ch or _default_client()
    current = await source_schema(names, ch=client)
    if current != schema:
        raise ValueError("Rebuild source schema changed before snapshot")
    vectors = {surface.column for surface in EMBEDDING_SURFACES if surface.table == names.table}
    # A copy made by CREATE ... AS SELECT keeps a Quantized column's fixed width in
    # memory though its DDL shows no codec; the cast drops it, so the copies hold
    # vectors of any width (the new model's included).
    columns = ", ".join(
        f"CAST({_identifier(name)}, toTypeName({_identifier(name)})) AS {_identifier(name)}"
        if name in vectors
        else _identifier(name)
        for name in schema.columns
    )
    # ClickHouse's COMMENT grammar requires a literal, not a query parameter.
    # Both identities are validated UUIDs; the marker contains no user text.
    marker = names.marker(schema.table_uuid)
    selected = _source_selection(names, schema)
    await client.execute(
        f"CREATE TABLE loom.{names.snapshot} ENGINE = MergeTree ORDER BY {_ORDINAL} "
        f"COMMENT '{marker}' AS SELECT row_number() OVER () AS "
        f"{_ORDINAL}, {columns} FROM ({selected})",
    )
    created = await _owned_snapshot(names, schema.table_uuid, client)
    return UUID(str(created["uuid"]))


async def _owned_snapshot(
    names: StagingNames, source_uuid: UUID, client: _CHClient
) -> dict[str, Any]:
    row = await client.query_one(
        "SELECT uuid, comment FROM system.tables "
        "WHERE database = currentDatabase() AND name = {table:String}",
        {"table": names.snapshot},
    )
    if row is None or not _owned_marker(str(row["comment"]), names.marker(source_uuid)):
        raise ValueError("Snapshot is missing or not owned by this rebuild")
    return row


async def snapshot_identity(
    names: StagingNames, source_uuid: UUID, *, ch: _CHClient | None = None
) -> UUID | None:
    """Inspect an interrupted copy without treating its existence as completion."""
    client = ch or _default_client()
    marker = names.marker(source_uuid)
    row = await client.query_one(
        "SELECT uuid, comment FROM system.tables "
        "WHERE database = currentDatabase() AND name = {table:String}",
        {"table": names.snapshot},
    )
    if row is None:
        return None
    if not _owned_marker(str(row["comment"]), marker):
        raise ValueError("Snapshot is not owned by this rebuild")
    identity = UUID(str(row["uuid"]))
    if identity.int == 0:
        raise ValueError("Snapshot requires a stable Atomic table identity")
    return identity


def completion_marker(names: StagingNames, schema: SourceSchema) -> str:
    if not re.fullmatch(r"[a-f0-9]{64}", schema.fingerprint):
        raise ValueError("Invalid snapshot schema fingerprint")
    return f"{names.marker(schema.table_uuid)}:complete:{schema.fingerprint}"


async def complete_snapshot(
    names: StagingNames, schema: SourceSchema, identity: UUID, *, ch: _CHClient | None = None
) -> None:
    """Persist a receipt only after CREATE AS SELECT has returned successfully.

    The caller must have observed CREATE success, not merely found a table. The
    completion receipt survives a lost application checkpoint or response.
    """
    client = ch or _default_client()
    marker = completion_marker(names, schema)
    if await snapshot_query_running(names, ch=client):
        raise ValueError("Snapshot creation is still running")
    if await snapshot_identity(names, schema.table_uuid, ch=client) != identity:
        raise ValueError("Rebuild snapshot identity changed")
    if await source_schema(names, ch=client) != schema:
        raise ValueError("Rebuild source schema changed during snapshot")
    await client.execute(f"ALTER TABLE loom.{names.snapshot} MODIFY COMMENT '{marker}'")


async def ensure_snapshot(
    names: StagingNames, schema: SourceSchema, *, ch: _CHClient | None = None
) -> UUID:
    """Resume under the coordinator lease, reusing only a completed matching copy.

    A copy with no completion receipt is discarded and rebuilt even when all
    its rows may have arrived. Never infer completion from row counts alone.
    """
    client = ch or _default_client()
    marker = completion_marker(names, schema)
    if await source_schema(names, ch=client) != schema:
        raise ValueError("Rebuild source schema changed before snapshot")
    if await snapshot_query_running(names, ch=client):
        raise ValueError("Snapshot creation is still running")
    identity = await snapshot_identity(names, schema.table_uuid, ch=client)
    if identity is not None:
        existing = await _owned_snapshot(names, schema.table_uuid, client)
        if UUID(str(existing["uuid"])) != identity:
            raise ValueError("Rebuild snapshot identity changed")
        if existing["comment"] == marker:
            return identity
        if existing["comment"] != names.marker(schema.table_uuid):
            raise ValueError("Completed snapshot has a different source schema")
        await discard_snapshot(names, schema.table_uuid, identity, ch=client)
    identity = await create_snapshot(names, schema, ch=client)
    await complete_snapshot(names, schema, identity, ch=client)
    return identity


async def discard_snapshot(
    names: StagingNames,
    source_uuid: UUID,
    expected_uuid: UUID,
    *,
    ch: _CHClient | None = None,
) -> bool:
    """Discard an owned copy; callers must hold the exclusive coordinator lease.

    Missing is an idempotent success (False means nothing needed removal). A
    same-name replacement is never silently adopted. The lease must exclude DDL
    on these private tables between inspection and DROP, as well as creation.
    This does not establish that an interrupted CREATE query has stopped: the
    coordinator must confirm that separately before calling this function.
    """
    if not isinstance(expected_uuid, UUID) or expected_uuid.int == 0:
        raise ValueError("Invalid expected snapshot identity")
    client = ch or _default_client()
    if await snapshot_query_running(names, ch=client):
        raise ValueError("Snapshot creation is still running")
    current = await snapshot_identity(names, source_uuid, ch=client)
    if current is None:
        return False
    if current != expected_uuid:
        raise ValueError("Rebuild snapshot identity changed")
    await client.execute(f"DROP TABLE loom.{names.snapshot} SYNC")
    return True


async def snapshot_query_running(names: StagingNames, *, ch: _CHClient | None = None) -> bool:
    """Check this ClickHouse node for our exact CREATE statement prefix.

    This is a local guard, not a cluster-wide drain acknowledgement. Recovery
    must use the same pinned node as creation and fence old coordinators before
    inspection. A missing query result is unknown, never proof of quiescence.
    """
    client = ch or _default_client()
    result = await client.query_one(
        "SELECT count() AS running FROM system.processes WHERE startsWith(query, {prefix:String})",
        {"prefix": f"CREATE TABLE loom.{names.snapshot} "},
    )
    if result is None or type(result.get("running")) is not int or result["running"] < 0:
        raise ValueError("Cannot establish snapshot query status")
    return bool(result["running"] > 0)


async def snapshot_page(
    names: StagingNames,
    schema: SourceSchema,
    snapshot_uuid: UUID,
    *,
    after: int,
    limit: int = 64,
    ch: _CHClient | None = None,
) -> list[dict[str, Any]]:
    """Bounded keyset paging; require the journaled copy, not a same-name replacement."""
    if type(after) is not int or after < 0 or type(limit) is not int or not 1 <= limit <= 512:
        raise ValueError("Invalid rebuild page bounds")
    client = ch or _default_client()
    owned = await _owned_snapshot(names, schema.table_uuid, client)
    if UUID(str(owned["uuid"])) != snapshot_uuid:
        raise ValueError("Rebuild snapshot identity changed")
    columns = ", ".join(_identifier(name) for name in schema.columns)
    return await client.query_all(
        f"SELECT {_ORDINAL}, {columns} FROM loom.{names.snapshot} "
        f"WHERE {_ORDINAL} > {{after:UInt64}} ORDER BY {_ORDINAL} LIMIT {{limit:UInt32}}",
        {"after": after, "limit": limit},
    )
