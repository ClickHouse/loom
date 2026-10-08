"""The live tracker source as other packages reach it, and the gate its reads
pass through.

`tracker_source(org, namespace)` opens a namespace's source; `nearest_items`
is the vector read over loom's own index of the record. `_AppGate` is what the
store's admission and bounded read need from outside the store: the
self-service registry's refresh, the QA client (credential and read), the
record-contract check and the directory's classification of a restricted
namespace. It is installed when this package is imported.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from server.tracker.domain.errors import TrackerUnavailable
from server.tracker.store import source as store_source
from server.tracker.store.embed import EmbedHit, TrackerEmbedRepo
from server.tracker.store.source.base import install_gate


class _AppGate:
    async def refresh_sources(self) -> None:
        from server.tracker.app import registry_sources

        await registry_sources.refresh_db_sources()

    def credential_configured(self, credential: str) -> bool:
        from server.tracker.domain import sources

        return bool(sources.credential_configured(credential))

    async def ensure_contract(self, cfg: Any) -> None:
        from server.tracker.app import contract

        await contract.ensure(cfg)

    async def is_restricted(self, org: str, namespace: str) -> bool:
        from server.identity.app import namespace_classification

        return bool(await namespace_classification.is_restricted(org, namespace))

    async def query_rows(
        self,
        sql: str,
        params: dict[str, Any],
        *,
        credential: str,
        max_execution_s: float | None,
        query_settings: dict[str, Any] | None,
    ) -> list[dict[str, Any]]:
        from server.tracker.store.source import qa_intel

        try:
            rows: list[dict[str, Any]] = await qa_intel.query_rows(
                sql,
                params,
                credential=credential,
                max_execution_s=max_execution_s,
                query_settings=query_settings,
            )
        except qa_intel.QaIntelUnavailable as e:
            raise TrackerUnavailable(str(e)) from e
        return rows


install_gate(_AppGate())


def tracker_source(org: str, namespace: str) -> store_source.TrackerSource:
    """The live source of one tracker namespace (the caller has authorized it)."""
    return store_source.TrackerSource(org, namespace)


async def nearest_items(
    org: str,
    namespace: str,
    *,
    vector: list[float],
    dim: int,
    limit: int = 50,
    kinds: list[str] | None = None,
    before: datetime | None = None,
    exclude: tuple[str, int] | None = None,
    numbers: list[int] | None = None,
) -> list[EmbedHit]:
    """The nearest live items of a tracker namespace to `vector` in loom's own
    index of the record (`TrackerEmbedRepo.nearest`)."""
    return await TrackerEmbedRepo().nearest(
        org,
        namespace,
        vector=vector,
        dim=dim,
        limit=limit,
        kinds=kinds,
        before=before,
        exclude=exclude,
        numbers=numbers,
    )


__all__ = ["nearest_items", "tracker_source"]
