"""Resumable table publication under closed admission and an exclusive lease.

Do not use EXCHANGE here: the shared client can replay a DDL after a lost response,
which would swap the old table back. One-way renames either advance the plan or
fail because their source moved. UUID-based recovery then resumes the next step.
There is a temporary name gap, so every reader and writer must remain drained.
"""

from uuid import UUID

from server.core.ch_names import SCHEMA, physical
from server.core.repo_base import _CHClient, _default_client
from server.insights.store.provider_staging import StagingNames, _identifier


async def _state(
    names: StagingNames, original: UUID, prepared: UUID, database: str, client: _CHClient
) -> str:
    rows = await client.query_all(
        "SELECT name, uuid FROM system.tables WHERE database = {database:String} "
        "AND name IN {tables:Array(String)}",
        {"database": database, "tables": [names.table, names.prepared, names.backup]},
    )
    present = {str(row["name"]): UUID(str(row["uuid"])) for row in rows}
    if len(present) != len(rows):
        raise ValueError("Ambiguous publication table identities")
    if present == {names.table: original, names.prepared: prepared}:
        return "before"
    if present == {names.backup: original, names.prepared: prepared}:
        return "between"
    if present == {names.table: prepared, names.backup: original}:
        return "after"
    raise ValueError("Publication table identities do not match the durable plan")


async def publish_prepared_table(
    names: StagingNames,
    original_uuid: UUID,
    prepared_uuid: UUID,
    *,
    database: str = SCHEMA,
    ch: _CHClient | None = None,
) -> None:
    """Publish an already verified, live-schema candidate; retain its old table.

    The coordinator must persist both UUIDs before calling, fence other writers,
    and use one pinned ClickHouse node throughout. The candidate is not the
    ordinal-keyed target: its live schema, indexes and constraints must already
    have been prepared and verified. Activation and backup cleanup are separate.
    """
    if (
        not isinstance(original_uuid, UUID)
        or not isinstance(prepared_uuid, UUID)
        or original_uuid.int == 0
        or prepared_uuid.int == 0
        or original_uuid == prepared_uuid
    ):
        raise ValueError("Invalid publication identities")
    database = physical(database)
    db = _identifier(database)
    client = ch or _default_client()
    state = await _state(names, original_uuid, prepared_uuid, database, client)
    if state == "after":
        return
    if state == "before":
        await client.execute(
            f"RENAME TABLE {db}.{_identifier(names.table)} TO {db}.{_identifier(names.backup)}"
        )
        state = await _state(names, original_uuid, prepared_uuid, database, client)
    if state != "between":
        raise ValueError("Publication changed unexpectedly after preserving the old table")
    await client.execute(
        f"RENAME TABLE {db}.{_identifier(names.prepared)} TO {db}.{_identifier(names.table)}"
    )
    if await _state(names, original_uuid, prepared_uuid, database, client) != "after":
        raise ValueError("Publication did not reach its expected final identities")
