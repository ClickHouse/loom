"""SCIP staging: uploaded index parts and the batches that group them,
until the apply worker picks a complete batch up.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import TypedDict, cast

from server.code.store.index.base import CodeRepoBase
from server.code.store.index.rows import version_now
from server.core.datetimes import naive_utc_now as _now


class ScipBatch(TypedDict):
    """One loom.code_scip_batches row. The FixedString shas may come back as bytes."""

    org: str
    namespace: str
    batch_id: uuid.UUID
    commit_sha: str | bytes
    base_sha: str | bytes
    total_parts: int
    status: str
    error: str
    producer: str
    applied_at: datetime | None
    created_at: datetime
    version: int


class PendingScipBatch(ScipBatch):
    """A pending batch with how many distinct parts have arrived."""

    received_parts: int


class ScipPart(TypedDict):
    """The newest upload of one part of a batch."""

    seq: int
    payload: str
    payload_hash: str | bytes


class ScipStagingMixin(CodeRepoBase):
    """Staged SCIP uploads."""

    async def insert_scip_part(
        self,
        org: str,
        ns: str,
        batch_id: str,
        seq: int,
        payload: str,
        payload_hash: str,
    ) -> None:
        await self._ch.insert(
            "loom.code_scip_parts",
            [
                {
                    "org": org,
                    "namespace": ns,
                    "batch_id": batch_id,
                    "seq": seq,
                    "payload": payload,
                    "payload_hash": payload_hash,
                    "created_at": _now(),
                }
            ],
        )

    async def upsert_scip_batch(
        self,
        org: str,
        ns: str,
        batch_id: str,
        *,
        commit_sha: str = "",
        base_sha: str = "",
        total_parts: int = 0,
        status: str = "pending",
        error: str = "",
        producer: str = "",
        applied_at: datetime | None = None,
        created_at: datetime | None = None,
    ) -> None:
        await self._ch.insert(
            "loom.code_scip_batches",
            [
                {
                    "org": org,
                    "namespace": ns,
                    "batch_id": batch_id,
                    "commit_sha": commit_sha,
                    "base_sha": base_sha,
                    "total_parts": total_parts,
                    "status": status,
                    "error": error,
                    "producer": producer,
                    "applied_at": applied_at,
                    "created_at": created_at or _now(),
                    "version": version_now(),
                }
            ],
        )

    async def get_scip_batch(self, org: str, ns: str, batch_id: str) -> ScipBatch | None:
        row = await self._ch.query_one(
            """
            SELECT * FROM loom.code_scip_batches FINAL
            WHERE org = {org:String} AND namespace = {ns:String}
              AND batch_id = {bid:UUID}
            """,
            {"org": org, "ns": ns, "bid": batch_id},
        )
        return cast(ScipBatch, row) if row is not None else None

    async def pending_scip_batches(
        self, *, per_namespace: int = 3, namespaces: int = 50
    ) -> list[PendingScipBatch]:
        """Pending batches whose every part has arrived, oldest first, at most
        `per_namespace` per (org, namespace) and `namespaces` namespaces' worth
        in one statement — every namespace that holds one, whether or not the
        registry lists it (a namespace indexed by the CLI alone, an evaluation
        namespace, a repository registered after its first upload)."""
        rows = await self._ch.query_all(
            """
            SELECT b.*, parts.received AS received_parts
            FROM loom.code_scip_batches AS b FINAL
            INNER JOIN (
                SELECT org, namespace, batch_id, toUInt32(countDistinct(seq)) AS received
                FROM loom.code_scip_parts
                GROUP BY org, namespace, batch_id
            ) AS parts ON parts.batch_id = b.batch_id
                AND parts.org = b.org AND parts.namespace = b.namespace
            WHERE b.status = 'pending' AND parts.received >= b.total_parts
            ORDER BY b.created_at
            LIMIT {per_ns:UInt32} BY b.org, b.namespace
            LIMIT {lim:UInt32}
            """,
            {"per_ns": int(per_namespace), "lim": int(per_namespace) * int(namespaces)},
        )
        return cast(list[PendingScipBatch], rows)

    async def scip_parts(self, org: str, ns: str, batch_id: str) -> list[ScipPart]:
        """Newest payload per seq (re-uploads supersede by created_at)."""
        rows = await self._ch.query_all(
            """
            SELECT seq, argMax(payload, created_at) AS payload,
                   argMax(payload_hash, created_at) AS payload_hash
            FROM loom.code_scip_parts
            WHERE org = {org:String} AND namespace = {ns:String}
              AND batch_id = {bid:UUID}
            GROUP BY seq ORDER BY seq
            """,
            {"org": org, "ns": ns, "bid": batch_id},
        )
        return cast(list[ScipPart], rows)

    async def scip_parts_received(self, org: str, ns: str, batch_id: str) -> int:
        row = await self._ch.query_one(
            """
            SELECT toUInt32(countDistinct(seq)) AS n FROM loom.code_scip_parts
            WHERE org = {org:String} AND namespace = {ns:String}
              AND batch_id = {bid:UUID}
            """,
            {"org": org, "ns": ns, "bid": batch_id},
        )
        return int(row["n"]) if row else 0
