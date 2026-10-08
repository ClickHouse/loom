"""Create/resume all source and target identities for one durable checkpoint."""

from uuid import UUID

from server.core.repo_base import _CHClient
from server.insights.domain.provider_rebuild_manifest import RebuildManifest, RebuildTablePlan
from server.insights.store.provider_snapshot_plan import preflight_snapshot_recovery
from server.insights.store.provider_staging import StagingNames, ensure_snapshot
from server.insights.store.provider_target import ensure_target


async def snapshot_rebuild_manifest(
    job_id: UUID,
    fingerprint: str,
    *,
    prefix_dimensions: tuple[int, ...],
    ch: _CHClient | None = None,
) -> RebuildManifest:
    """Return only after every input copy is complete and every target is owned.

    Caller holds the coordinator fence and writer barrier for the entire step,
    then persists this manifest before generating vectors. A lost checkpoint can
    retry this operation: receipts, not existence or row counts, decide reuse.
    No existing source row or global provider is changed.
    """
    if (
        not isinstance(prefix_dimensions, tuple)
        or len(prefix_dimensions) > 64
        or any(type(dim) is not int or not 1 <= dim <= 65535 for dim in prefix_dimensions)
        or len(set(prefix_dimensions)) != len(prefix_dimensions)
    ):
        raise ValueError("Invalid snapshot prefix policy")
    schemas = await preflight_snapshot_recovery(job_id, fingerprint, ch=ch)
    tables = []
    for table, schema in schemas.items():
        names = StagingNames(job_id, table)
        snapshot = await ensure_snapshot(names, schema, ch=ch)
        target = await ensure_target(names, schema, snapshot, fingerprint, ch=ch)
        tables.append(
            RebuildTablePlan(
                table,
                schema.table_uuid,
                schema.engine,
                schema.columns,
                schema.fingerprint,
                snapshot,
                target,
            )
        )
    return RebuildManifest(job_id, fingerprint, tuple(tables), prefix_dimensions)
