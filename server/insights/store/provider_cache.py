"""Invalidate a private cache replacement, never the currently served cache."""

from uuid import UUID

from server.core.repo_base import _CHClient, _default_client
from server.insights.store.provider_staging import (
    SourceSchema,
    StagingNames,
    _owned_snapshot,
    completion_marker,
)
from server.insights.store.provider_target import _identity, _target, target_marker


async def _owned_cache(
    names: StagingNames,
    schema: SourceSchema,
    snapshot_uuid: UUID,
    target_uuid: UUID,
    fingerprint: str,
    client: _CHClient,
) -> None:
    if names.table != "embedding_cache":
        raise ValueError("Cache invalidation cannot target a content table")
    source = await _owned_snapshot(names, schema.table_uuid, client)
    if UUID(str(source["uuid"])) != snapshot_uuid or source["comment"] != completion_marker(
        names, schema
    ):
        raise ValueError("Cache invalidation requires its completed snapshot")
    marker = target_marker(names, snapshot_uuid, fingerprint)
    if _identity(await _target(names, client), marker) != target_uuid:
        raise ValueError("Cache rebuild target identity changed")


async def verify_staged_cache(
    names: StagingNames,
    schema: SourceSchema,
    snapshot_uuid: UUID,
    target_uuid: UUID,
    fingerprint: str,
    *,
    ch: _CHClient | None = None,
) -> int:
    """An empty owned target is the complete invalidation result, regardless of source size."""
    client = ch or _default_client()
    await _owned_cache(names, schema, snapshot_uuid, target_uuid, fingerprint, client)
    count = await client.query_one(f"SELECT count() AS rows FROM loom.{names.target} FINAL")
    if count is None or type(count.get("rows")) is not int or count["rows"] != 0:
        raise ValueError("Rebuild cache invalidation is incomplete or unknown")
    return 0


async def invalidate_staged_cache(
    names: StagingNames,
    schema: SourceSchema,
    snapshot_uuid: UUID,
    target_uuid: UUID,
    fingerprint: str,
    *,
    ch: _CHClient | None = None,
) -> None:
    """Idempotent under the coordinator's fence; live cache changes only on publication.

    Old model/cache keys must not survive a provider or endpoint change, even
    when the model's human-readable label remains the same. No text is embedded.
    """
    client = ch or _default_client()
    await _owned_cache(names, schema, snapshot_uuid, target_uuid, fingerprint, client)
    await client.execute(f"TRUNCATE TABLE loom.{names.target} SYNC")
    await verify_staged_cache(names, schema, snapshot_uuid, target_uuid, fingerprint, ch=client)
