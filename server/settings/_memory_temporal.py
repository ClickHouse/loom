"""Bi-temporal memory: the commit journal, its publisher and its projections."""

from __future__ import annotations

from pydantic import BaseModel


class MemoryTemporalSettings(BaseModel):
    # The bi-temporal journal, on or off. Off (the default): no migration of the
    # feature is applied, nothing journals, and every path behaves as it did before
    # the journal (current-state memory, `as_of` replay, the version-row rollback).
    # On: migration 039 applies at the next migrate, every memory write is journaled
    # first, and valid_as_of / known_as_of reads, memory.amend and the journal's
    # namespace.rollback serve. Keys written while it was off are seeded on their
    # next write: their history before that is not known.
    memory_temporal_enabled: bool = False
    # Seconds a write waits for its namespace's publisher lock before failing.
    memory_temporal_lock_wait_s: float = 10.0
    # Times a journal insert with an uncertain result is reconciled and retried
    # (same commit id, sequence and payload) before the write fails.
    memory_temporal_insert_retries: int = 3
    # Milliseconds a read holding a `read_token` waits for the projection to cover
    # its sequence before answering `projection_not_ready`.
    memory_temporal_read_wait_ms: int = 2000
    # Journal tail a reader reduces itself when the projection lags its cutoff;
    # above it the read answers `projection_not_ready` instead.
    memory_temporal_max_tail: int = 5000
    # Replays journal commits the projection missed and fills historical-search vectors.
    memory_temporal_projection_worker_enabled: bool = True
    # Seconds between passes of the temporal projection worker.
    memory_temporal_projection_worker_interval_seconds: int = 30
