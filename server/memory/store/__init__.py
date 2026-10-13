"""Repository for the loom.memory table family.

Typed I/O: inputs are explicit (identity tuple + key, etc.); outputs are
Pydantic models (MemoryRow). The CH client is injected (default =
`server.core.ch.get_client()`) so unit tests can supply a mock.

The package re-exports the API its callers use. Modules, leaf first — each
imports only the ones above it:

- `rows`        — `Identity`, `MemoryRow`, the column registries and the
                  version-row builders (single source of truth for every
                  column a ReplacingMergeTree write carries forward)
- `sql`         — statement fragments shared by reads and writes, and the
                  read-your-writes switch
- `inserts`     — the committed insert funnels for loom.memory and
                  loom.memory_embeddings
- `reads`       — heads of loom.memory by key, id, key list and namespace
- `history`     — loom.memory_versions: history, rollback
- `redaction`   — memory.redact: a version and its derived text
- `embeddings`  — the memory_embeddings mirror: live rows, re-emits, status
                  reconciliation
- `lifecycle`   — status and tombstone writes that cascade onto the mirror
- `dedup`       — duplicate and supersession reads
- `lookups`     — memory.ask's identifier, facet, prefix and term finders
- `entities`    — the entity extractor's reads and writes
- `sweeps`      — expiry and embedding-queue scans for the workers

A test that replaces a function replaces it where it is looked up: on this
package for a caller outside it, on the submodule for a call from inside.
"""

from __future__ import annotations

from server.memory.store.dedup import (
    canonical_rows_by_ids,
    clear_duplicate_tag,
    find_duplicate_clusters,
    find_duplicate_pairs,
    find_orphan_duplicates,
    find_row_by_key_namespace,
    get_canonical_row,
    live_ids_in_scope,
    status_by_keys,
    superseded_successors,
)
from server.memory.store.embeddings import (
    EmbeddingKey,
    divergent_namespaces,
    embedding_mirror_for_keys,
    live_embedding_rows_for_key,
    live_shared_embedding_rows_for_key,
    mirror_status_to_embeddings,
    reconcile_embedding_status,
    reemit_embedding_rows_for_key,
    reemit_embedding_rows_for_keys,
    reemit_shared_embedding_rows,
    restamp_embedding_rows,
    retire_vectors_for_keys,
    status_divergence_sql,
    tombstone_embeddings_for_key,
    tombstone_entity_vectors_for_keys,
    tombstone_vectors_of,
    unsupersede_embeddings,
    version_second,
)
from server.memory.store.entities import (
    ENTITY_MIRROR_RETRY_DELAY_S,
    apply_entities_batch,
    find_unentitied_keys,
    find_unentitied_memories,
    heal_divergent_entities,
    read_unentitied_rows,
)
from server.memory.store.history import (
    earliest_version_in_namespace,
    fetch_version_rows,
    find_history_target,
    list_history,
    rollback_change_rows,
)
from server.memory.store.inserts import (
    check_memory_rows,
    insert_embedding_rows,
    insert_memory_versions,
)
from server.memory.store.lifecycle import (
    bulk_tombstone_scope,
    consolidated_dependents_of,
    delete_procedure_runs_by_key,
    downgrade_consolidated_dependents,
    live_source_count,
    supersede_duplicates,
    tombstone_by_id,
    tombstone_cascade_by_ids,
    write_versions_by_ids,
)
from server.memory.store.lookups import (
    FACET_SUFFIX_SYNONYMS,
    find_by_content_terms,
    find_by_identifier,
    find_by_key_facet,
    find_by_key_prefix,
    find_chunks_by_identifier,
    find_chunks_by_session_date,
    normalize_key_prefix,
)
from server.memory.store.reads import (
    CONTAINS_PREDICATE,
    actors_by_key,
    count_by_namespace,
    find_related_by_entities,
    get_by_id,
    get_by_key,
    get_many_by_keys,
    key_holds_established_fact,
    list_by_namespace,
    memory_rows_by_identity_keys,
    namespace_version,
)
from server.memory.store.redaction import (
    delete_ask_precedents_by_key,
    delete_llm_response_cache_by_key,
    key_derivative_scrubs,
    redact_row,
    run_scrubs,
)
from server.memory.store.rows import (
    EMBEDDING_PERSISTED_COLS,
    ENTITY_SUMMARY_PERSISTED_COLS,
    MEMORY_PERSISTED_COLS,
    SESSION_SUMMARY_PERSISTED_COLS,
    ZERO_UUID,
    Identity,
    MemoryRow,
    _hydrate,
    build_embedding_row,
    build_entity_summary_row,
    build_first_version_row,
    build_memory_version_row,
    build_revision_row,
    build_session_summary_row,
    next_version,
    parse_memory_value,
    reemit_version,
    serialize_value,
    stored_value_text,
    uuid_or_none,
    written_now,
)
from server.memory.store.sql import (
    _MEMORY_FINAL_PROJECTION,
    _SEQUENTIAL,
    read_your_writes_enabled,
)
from server.memory.store.sweeps import (
    find_embedding_stubs,
    find_expired_candidates,
    find_memories_to_reindex,
    find_memories_without_embedding_rows,
)
from server.memory.store.usage import record_usage_event

__all__ = [
    "_hydrate",
    "_MEMORY_FINAL_PROJECTION",
    "next_version",
    "reemit_version",
    "_SEQUENTIAL",
    "apply_entities_batch",
    "build_embedding_row",
    "build_first_version_row",
    "build_entity_summary_row",
    "build_memory_version_row",
    "build_revision_row",
    "build_session_summary_row",
    "bulk_tombstone_scope",
    "canonical_rows_by_ids",
    "check_memory_rows",
    "clear_duplicate_tag",
    "consolidated_dependents_of",
    "CONTAINS_PREDICATE",
    "count_by_namespace",
    "delete_ask_precedents_by_key",
    "delete_llm_response_cache_by_key",
    "delete_procedure_runs_by_key",
    "divergent_namespaces",
    "downgrade_consolidated_dependents",
    "earliest_version_in_namespace",
    "embedding_mirror_for_keys",
    "EMBEDDING_PERSISTED_COLS",
    "EmbeddingKey",
    "ENTITY_MIRROR_RETRY_DELAY_S",
    "ENTITY_SUMMARY_PERSISTED_COLS",
    "FACET_SUFFIX_SYNONYMS",
    "fetch_version_rows",
    "find_by_content_terms",
    "find_by_identifier",
    "find_by_key_facet",
    "find_by_key_prefix",
    "find_chunks_by_identifier",
    "find_chunks_by_session_date",
    "find_duplicate_clusters",
    "find_duplicate_pairs",
    "find_embedding_stubs",
    "find_expired_candidates",
    "find_history_target",
    "find_memories_to_reindex",
    "find_memories_without_embedding_rows",
    "find_orphan_duplicates",
    "find_related_by_entities",
    "find_row_by_key_namespace",
    "find_unentitied_keys",
    "find_unentitied_memories",
    "get_by_id",
    "get_by_key",
    "get_canonical_row",
    "actors_by_key",
    "get_many_by_keys",
    "heal_divergent_entities",
    "Identity",
    "insert_embedding_rows",
    "insert_memory_versions",
    "key_holds_established_fact",
    "list_by_namespace",
    "list_history",
    "live_embedding_rows_for_key",
    "live_ids_in_scope",
    "live_shared_embedding_rows_for_key",
    "live_source_count",
    "MEMORY_PERSISTED_COLS",
    "memory_rows_by_identity_keys",
    "MemoryRow",
    "mirror_status_to_embeddings",
    "namespace_version",
    "normalize_key_prefix",
    "parse_memory_value",
    "read_unentitied_rows",
    "read_your_writes_enabled",
    "reconcile_embedding_status",
    "record_usage_event",
    "key_derivative_scrubs",
    "redact_row",
    "run_scrubs",
    "reemit_embedding_rows_for_key",
    "reemit_embedding_rows_for_keys",
    "retire_vectors_for_keys",
    "reemit_shared_embedding_rows",
    "restamp_embedding_rows",
    "rollback_change_rows",
    "serialize_value",
    "SESSION_SUMMARY_PERSISTED_COLS",
    "status_by_keys",
    "status_divergence_sql",
    "stored_value_text",
    "supersede_duplicates",
    "superseded_successors",
    "tombstone_by_id",
    "tombstone_cascade_by_ids",
    "tombstone_embeddings_for_key",
    "tombstone_entity_vectors_for_keys",
    "version_second",
    "tombstone_vectors_of",
    "unsupersede_embeddings",
    "uuid_or_none",
    "write_versions_by_ids",
    "written_now",
    "ZERO_UUID",
]
