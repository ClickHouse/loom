"""Erasure: saga recovery, reclaim, purge and the external backends it cascades to."""

from __future__ import annotations

from pydantic import BaseModel


class ErasureSettings(BaseModel):
    # ---- saga recovery and reclaim ----
    # Replay erasure sagas stuck `in_progress` past a heartbeat from
    # `steps_completed`.
    erasure_recovery_enabled: bool = True
    # Seconds between passes of the erasure recovery worker.
    erasure_recovery_interval_seconds: int = 60
    # Physically remove purged rows: a purge of a wide table only masks its
    # rows (lightweight DELETE), and large settled parts rarely merge, so the
    # bytes stay. The erasure reclaim worker gives the purge table with the
    # most dead bytes one `APPLY DELETED MASK` per pass. False = the masked
    # rows wait for merges.
    erasure_reclaim_enabled: bool = True
    # Seconds between passes of the erasure reclaim worker.
    erasure_reclaim_interval_seconds: int = 3600
    # A table is reclaimed once this share of its rows on disk are purged rows.
    erasure_reclaim_min_dead_fraction: float = 0.3
    # ...and those rows hold at least this many bytes (default 1 GiB).
    erasure_reclaim_min_dead_bytes: int = 1073741824
    # Seconds one erasure step waits for its mutation (submitted with
    # mutations_sync=0, polled in system.mutations); past it the saga fails
    # naming the mutation.
    erasure_mutation_wait_s: int = 3600

    # ---- external erasure backends ----
    # ClickStack erasure: direct ALTER TABLE DELETE on the OTel tables in the
    # same CH service. `clickstack_database` = HyperDX's DB ("" skips the leg);
    # `clickstack_user_id_keys` = comma-separated Map attribute keys that carry
    # user_id (ORed; depends on the emitting SDK).
    clickstack_database: str = ""  # empty → skip cascade
    # Comma-separated OTel Map attribute keys holding user_id, ORed in the delete.
    clickstack_user_id_keys: str = "user_id,user.id,enduser.id"
    # Per-request seconds for external trace/chat backend lookups and deletes
    # during erasure.
    external_erasure_timeout_s: float = 30.0
