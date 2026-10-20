"""One bounded re-embedding page under the coordinator's live drain lease."""

from collections.abc import Mapping
from uuid import UUID

from server.core.repo_base import _CHClient
from server.embedding.embedder import Embedder
from server.insights.app.provider_rebuild_batch import rebuild_batch
from server.insights.app.provider_rebuild_runner import BatchCheckpoint
from server.insights.app.provider_tracker_text import reconcile_tracker_page
from server.insights.domain.provider_rebuild_manifest import RebuildManifest
from server.insights.store.provider_cache import invalidate_staged_cache
from server.insights.store.provider_prefix import derive_staged_prefixes
from server.insights.store.provider_staging import (
    _ORDINAL,
    SourceSchema,
    StagingNames,
    snapshot_page,
)
from server.insights.store.provider_target import write_target_page
from server.settings import settings

PAGE_SIZE = 64


async def rebuild_snapshot_page(
    names: StagingNames,
    schema: SourceSchema,
    snapshot_uuid: UUID,
    target_uuid: UUID,
    embedding_fingerprint: str,
    embedder: Embedder,
    *,
    after: int,
    source_texts: Mapping[int, str] | None = None,
    manifest: RebuildManifest | None = None,
    prefix_dimensions: tuple[int, ...] | None = None,
    ch: _CHClient | None = None,
) -> BatchCheckpoint:
    """Return a checkpoint only after validated vectors reach the staging table.

    Both table identities come from the coordinator's durable plan. Tracker
    hydration uses the namespace's source gate unless supplied by the caller;
    derived prefixes require the durable manifest and frozen projection policy.
    Cache invalidation clears only its owned staging target. No global provider
    is changed and no live table is written.
    """
    if names.table == "code_chunks_prefix":
        if manifest is None or type(after) is not int or after != 0:
            raise ValueError(
                "Prefix rebuild requires a manifest, projection policy and zero cursor"
            )
        if prefix_dimensions is None:
            prefix_dimensions = manifest.prefix_dimensions
        twin = next(table for table in manifest.tables if table.table == names.table)
        if (
            manifest.job_id != names.job_id
            or manifest.embedding_fingerprint != embedding_fingerprint
            or (twin.source_uuid, twin.snapshot_uuid, twin.target_uuid)
            != (schema.table_uuid, snapshot_uuid, target_uuid)
            or (twin.engine, twin.columns, twin.schema_fingerprint)
            != (schema.engine, schema.columns, schema.fingerprint)
        ):
            raise ValueError("Prefix page differs from its durable manifest")
        count = await derive_staged_prefixes(
            manifest,
            model=embedder.model_name,
            dimensions=embedder.dim,
            prefix_dimensions=prefix_dimensions,
            ch=ch,
        )
        return BatchCheckpoint(cursor=0, rows_written=count, done=True)
    if names.table == "embedding_cache":
        if type(after) is not int or after != 0:
            raise ValueError("Cache invalidation has no row cursor")
        await invalidate_staged_cache(
            names, schema, snapshot_uuid, target_uuid, embedding_fingerprint, ch=ch
        )
        return BatchCheckpoint(cursor=0, rows_written=0, done=True)
    rows = await snapshot_page(names, schema, snapshot_uuid, after=after, limit=PAGE_SIZE, ch=ch)
    if not rows:
        return BatchCheckpoint(cursor=after, rows_written=0, done=True)
    if names.table == "qa_item_embeddings" and source_texts is None:
        rows, source_texts = await reconcile_tracker_page(
            rows, body_chars=settings.tracker_embed_body_chars
        )
    waiting: list[int] = []
    held: list[int] = []
    rebuilt = await rebuild_batch(
        names.table, rows, embedder, source_texts=source_texts, waiting=waiting, held=held
    )
    await write_target_page(
        names,
        schema,
        snapshot_uuid,
        target_uuid,
        embedding_fingerprint,
        rebuilt,
        after=after,
        ch=ch,
    )
    return BatchCheckpoint(
        cursor=after + len(rows),
        rows_written=len(rows),
        done=len(rows) < PAGE_SIZE,
        rows_waiting=len(waiting),
        held=tuple(int(rows[i][_ORDINAL]) for i in held),
    )
