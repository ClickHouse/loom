"""Concrete storage effects for the durable runner, under an external live fence."""

from collections.abc import Awaitable, Callable
from dataclasses import replace
from uuid import UUID

from server.core.repo_base import _CHClient
from server.embedding.embedder import Embedder
from server.insights.app.provider_rebuild_page import rebuild_snapshot_page
from server.insights.app.provider_rebuild_runner import BatchCheckpoint
from server.insights.app.provider_rebuild_snapshot import snapshot_rebuild_manifest
from server.insights.domain.provider_rebuild_manifest import RebuildManifest, RebuildTablePlan
from server.insights.domain.provider_views import RebuildView
from server.insights.store.provider_cache import verify_staged_cache
from server.insights.store.provider_cleanup import cleanup_rebuild_copies
from server.insights.store.provider_prefix import verify_staged_prefixes
from server.insights.store.provider_prepared import prepare_live_table
from server.insights.store.provider_publish import publish_prepared_table
from server.insights.store.provider_staging import SourceSchema, StagingNames
from server.insights.store.provider_verify import verify_reembedded_target
from server.insights.store.provider_views import rebind_rebuild_views


def _schema(table: RebuildTablePlan) -> SourceSchema:
    return SourceSchema(table.source_uuid, table.engine, table.columns, table.schema_fingerprint)


class ClickHouseRebuildBackend:
    """One job's explicit provider and pinned client; no process-wide activation.

    The owner must hold the exclusive coordinator fence for each runner step.
    `barrier` must positively establish distributed/deferred-write quiescence;
    this class neither substitutes a local gate nor acquires a global lease.
    """

    def __init__(
        self,
        job_id: UUID,
        fingerprint: str,
        target: Embedder,
        *,
        prefix_dimensions: tuple[int, ...],
        barrier: Callable[[UUID], Awaitable[bool]],
        ch: _CHClient | None = None,
        views: tuple[RebuildView, ...] = (),
    ) -> None:
        self.job_id = job_id
        self.fingerprint = fingerprint
        self.target = target
        self.prefix_dimensions = prefix_dimensions
        self.barrier = barrier
        self.ch = ch
        self.views = views

    def _require_job(self, config_id: UUID, manifest: RebuildManifest | None = None) -> None:
        if config_id != self.job_id or (
            manifest is not None
            and (
                manifest.job_id != config_id
                or manifest.embedding_fingerprint != self.fingerprint
                or manifest.prefix_dimensions != self.prefix_dimensions
            )
        ):
            raise ValueError("Rebuild backend does not own this configuration and recipe")

    async def drain(self, config_id: UUID) -> bool:
        self._require_job(config_id)
        acknowledged = await self.barrier(config_id)
        if type(acknowledged) is not bool:
            raise ValueError("Rebuild barrier acknowledgement is unknown")
        return acknowledged

    async def snapshot(self, config_id: UUID) -> RebuildManifest:
        self._require_job(config_id)
        return await snapshot_rebuild_manifest(
            config_id, self.fingerprint, prefix_dimensions=self.prefix_dimensions, ch=self.ch
        )

    async def rebuild(
        self, config_id: UUID, table: str, cursor: int, manifest: RebuildManifest
    ) -> BatchCheckpoint:
        self._require_job(config_id, manifest)
        plan = next(row for row in manifest.tables if row.table == table)
        if plan.prepared_uuid is not None:
            raise ValueError("Prepared rebuild targets cannot be regenerated")
        return await rebuild_snapshot_page(
            StagingNames(config_id, table),
            _schema(plan),
            plan.snapshot_uuid,
            plan.target_uuid,
            self.fingerprint,
            self.target,
            after=cursor,
            manifest=manifest,
            ch=self.ch,
        )

    async def _verify_table(
        self, manifest: RebuildManifest, table: RebuildTablePlan, held: list[int]
    ) -> int:
        names = StagingNames(manifest.job_id, table.table)
        args = (names, _schema(table), table.snapshot_uuid, table.target_uuid, self.fingerprint)
        if table.table == "embedding_cache":
            return await verify_staged_cache(*args, ch=self.ch)
        if table.table == "code_chunks_prefix":
            return await verify_staged_prefixes(
                manifest,
                model=self.target.model_name,
                dimensions=self.target.dim,
                prefix_dimensions=manifest.prefix_dimensions,
                ch=self.ch,
            )
        return await verify_reembedded_target(
            *args,
            model=self.target.model_name,
            dimensions=self.target.dim,
            held=held,
            ch=self.ch,
        )

    async def verify(
        self, config_id: UUID, manifest: RebuildManifest, held: dict[str, list[int]]
    ) -> RebuildManifest:
        self._require_job(config_id, manifest)
        prepared = []
        for table in manifest.tables:
            count = await self._verify_table(manifest, table, held.get(table.table, []))
            identity = await prepare_live_table(
                StagingNames(config_id, table.table),
                _schema(table),
                table.snapshot_uuid,
                table.target_uuid,
                self.fingerprint,
                expected_rows=count,
                dimensions=self.target.dim,
                ch=self.ch,
            )
            prepared.append(replace(table, prepared_uuid=identity, verified_rows=count))
        return replace(manifest, tables=tuple(prepared))

    async def publish(self, config_id: UUID, manifest: RebuildManifest) -> None:
        self._require_job(config_id, manifest)
        manifest.require_prepared()
        for table in manifest.tables:
            assert table.prepared_uuid is not None
            await publish_prepared_table(
                StagingNames(config_id, table.table),
                table.source_uuid,
                table.prepared_uuid,
                ch=self.ch,
            )
        await rebind_rebuild_views(self.views, ch=self.ch)

    async def cleanup(self, config_id: UUID, manifest: RebuildManifest) -> None:
        self._require_job(config_id, manifest)
        await cleanup_rebuild_copies(manifest, ch=self.ch)
