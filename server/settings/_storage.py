"""Storage: document blobs, per-table retention and warm-tier moves."""

from __future__ import annotations

from pydantic import BaseModel


class StorageSettings(BaseModel):
    # ---- document blobs ----
    # Directory that holds uploaded document originals, content-addressed as
    # {org}/{namespace}/{sha256[:2]}/{sha256}; knowledge.download serves from
    # it and erasure deletes from it. A relative path resolves from the
    # server's working directory.
    blob_dir: str = "var/blobs"

    # ---- retention and tiering ----
    # Opt-in retention/tiering, applied as idempotent ALTER TABLE MODIFY TTL by
    # server/storage/retention_overrides.py:
    #   - retention_*_days: delete rows older than N days. 0 = keep forever.
    #   - tiering_*_warm_days: move rows older than W days to
    #     tiering_warm_volume_name. 0 = no tiering.
    # With both set, W must be < N or the move is a no-op (logged WARN).
    retention_audit_log_days: int = 0
    # Days `loom.agent_events` rows are kept; 0 = forever.
    retention_agent_events_days: int = 0
    # Days `loom.memory` rows are kept; 0 = forever.
    retention_memory_days: int = 0
    # Days `loom.memory_embeddings` rows are kept; 0 = forever.
    retention_memory_embeddings_days: int = 0
    # Days `loom.conversations` rows are kept; 0 = forever.
    retention_conversations_days: int = 0
    # Days `loom.chunks` rows are kept; 0 = forever.
    retention_chunks_days: int = 0
    # Days `loom.documents` rows are kept; 0 = forever.
    retention_documents_days: int = 0
    # The supervisor re-applies table TTLs from settings + namespace rows on
    # this timer as metadata changes (materialize_ttl_after_modify = 0): rows
    # already past a new TTL leave at their part's next merge or a manual
    # MATERIALIZE TTL.
    retention_reconcile_enabled: bool = True
    # Seconds between TTL re-apply passes.
    retention_reconcile_interval_seconds: int = 300

    # ---- storage tiering ----
    # Per-table warm-tier move age, days (see retention and tiering above).
    tiering_audit_log_warm_days: int = 0
    # Days before `loom.agent_events` rows move to the warm volume; 0 = never.
    tiering_agent_events_warm_days: int = 0
    # Days before `loom.memory` rows move to the warm volume; 0 = never.
    tiering_memory_warm_days: int = 0
    # Days before `loom.memory_embeddings` rows move to the warm volume;
    # 0 = never.
    tiering_memory_embeddings_warm_days: int = 0
    # Days before `loom.conversations` rows move to the warm volume; 0 = never.
    tiering_conversations_warm_days: int = 0
    # Days before `loom.chunks` rows move to the warm volume; 0 = never.
    tiering_chunks_warm_days: int = 0
    # Days before `loom.documents` rows move to the warm volume; 0 = never.
    tiering_documents_warm_days: int = 0

    # Volume named in tiering TTL clauses; must match a volume in the
    # cluster's storage_policies.
    tiering_warm_volume_name: str = "warm"
