"""Bi-temporal memory: the commit journal, its publisher and its projections."""

from __future__ import annotations

from typing import Annotated, Literal

from pydantic import BaseModel

from server.settings._env import CaseFolded


class MemoryTemporalSettings(BaseModel):
    # Who serializes a namespace's commits. keeper = a KeeperMap lease in
    # loom._migration_lock (every process and host takes it); process = the
    # operator's statement that this server is the only process writing memory
    # (web_workers = 1, no separate workers container), serialized by an in-process
    # lock; auto = keeper when the server has one, else memory writes are refused.
    memory_temporal_publisher: Annotated[Literal["auto", "keeper", "process"], CaseFolded] = "auto"
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
