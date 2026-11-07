"""The residue census: every substrate the read paths touch, the saga step
that owns it in each scope, and the count that proves a saga left nothing.

`_CENSUS` is the one registry the census, the sagas' step lists and a
resumed saga's re-opened steps all read; `STEP_ONLY_TABLES` and
`NO_MEMORY_CONTENT` classify the rest of the schema. One count is not a
table's: `OBJECT_RESIDUE`, the scope's objects in the document bucket, which
re-opens `documents.tombstone`.
"""

from __future__ import annotations

import json
import logging
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from server.core import object_store, pg
from server.core.repo_base import _CHClient, _default_client
from server.erasure.store.mutations import namespaces_of, ns_list_predicate, purge_mode
from server.erasure.store.predicates import (
    LIVE,
    MEMORY_NS_EQ,
    NS_EQ,
    ORPHAN_LEARNINGS,
    PAYLOAD_UID,
    UID,
    USER_CHUNKS,
    USER_COMMUNITY_ROWS,
    USER_CONFLICT_RECORD_IDS,
    USER_MEMORY_KEYS,
    USER_PARENT_KEYS,
    USER_PRECEDENT_KEYS,
    USER_RECORD_IDS,
    USER_WINDOW_KEYS,
)
from server.erasure.store.provider_copies import copy_residue
from server.erasure.store.steps_memory import SESSION_OUTCOME_TABLES, TERM_AGGREGATE_TABLES
from server.erasure.store.steps_pg import PG_CENSUS, pg_residue
from server.knowledge.domain.live_chunks import LIVE_CHUNK_SQL
from server.settings import settings

log = logging.getLogger(__name__)

# The census key of the scope's objects in the document bucket, and the step that owns them.
OBJECT_RESIDUE = "objects.document_originals"
_OBJECT_OWNER = "documents.tombstone"


# One census row per substrate: the live predicate the census counts ("1" =
# every row), the user-scope predicate (None → namespace-only), and the saga
# step that owns it in each scope. Zero live rows in scope is the only pass.
@dataclass(frozen=True)
class Substrate:
    table: str
    live: str
    user_pred: str | None
    ns_step: str
    user_step: str | None
    # False for a plain MergeTree that carries a live predicate: FINAL is
    # illegal there (loom.conversations). Tables whose live predicate is "1"
    # never read FINAL whatever this says.
    final: bool = True
    # The namespace-scope predicate for a table whose `namespace` column is not the
    # scope's: "1" counts org-wide (no namespace column), MEMORY_NS_EQ the memory a
    # code answer quoted; None → the namespace predicate.
    ns_pred: str | None = None

    def counted_in(self, scope_type: str) -> bool:
        return scope_type == "namespace" or self.user_pred is not None

    def owner(self, scope_type: str) -> str | None:
        return self.ns_step if scope_type == "namespace" else self.user_step


_HOLDS_TEXT = "(answer != '' OR citations != '')"

_CENSUS: tuple[Substrate, ...] = (
    # In the user scope `memory.tombstone` only stamps these rows, which the later steps
    # read the user's keys from; `memory.retire` owns them and, in a purge, deletes them.
    Substrate("loom.memory", LIVE, UID, "memory.tombstone", "memory.retire"),
    Substrate(
        "loom.memory_embeddings",
        LIVE,
        UID,
        "memory_embeddings.tombstone",
        "memory_embeddings.tombstone",
    ),
    Substrate(
        "loom.memory_versions",
        "1",
        UID,
        "memory_versions.delete",
        "memory_versions.delete",
    ),
    Substrate(
        "loom.memory_code_links",
        LIVE,
        UID,
        "code_links.delete",
        "code_links.delete",
    ),
    Substrate("loom.memory_code_link_queue", "1", UID, "code_links.delete", "code_links.delete"),
    Substrate(
        "loom.working_memory",
        LIVE,
        UID,
        "working_memory.tombstone",
        "working_memory.tombstone",
    ),
    Substrate("loom.memory_evidence", "1", UID, "raw_evidence.delete", "raw_evidence.delete"),
    Substrate(
        "loom.memory_extraction_queue",
        "1",
        UID,
        "raw_evidence.delete",
        "raw_evidence.delete",
    ),
    Substrate(
        "loom.memory_chunks",
        LIVE,
        USER_CHUNKS,
        "memory_chunks.tombstone",
        "memory_chunks.tombstone",
    ),
    Substrate(
        "loom.memory_belief_sources",
        LIVE,
        USER_PARENT_KEYS,
        "source_links.tombstone",
        "source_links.tombstone",
    ),
    Substrate(
        "loom.memory_chunk_sources",
        LIVE,
        USER_PARENT_KEYS,
        "source_links.tombstone",
        "source_links.tombstone",
    ),
    Substrate(
        "loom.entity_triple_memories",
        "1",
        USER_PARENT_KEYS,
        "graph_links.delete",
        "graph_links.delete",
    ),
    Substrate(
        "loom.entity_triple_rows",
        "1",
        UID,
        "graph_links.delete",
        "graph_links.delete",
    ),
    Substrate(
        "loom.entity_edge_memories",
        "1",
        USER_PARENT_KEYS,
        "graph_links.delete",
        "graph_links.delete",
    ),
    Substrate(
        "loom.entity_edge_rows",
        "1",
        UID,
        "graph_links.delete",
        "graph_links.delete",
    ),
    Substrate("loom.session_summaries", LIVE, UID, "summaries.tombstone", "summaries.tombstone"),
    Substrate("loom.entity_summaries", LIVE, UID, "summaries.tombstone", "summaries.tombstone"),
    Substrate("loom.memory_records", LIVE, UID, "records.tombstone", "records.tombstone"),
    Substrate("loom.memory_record_versions", "1", UID, "records.tombstone", "records.tombstone"),
    Substrate(
        "loom.memory_record_fields",
        LIVE,
        USER_RECORD_IDS,
        "records.tombstone",
        "records.tombstone",
    ),
    Substrate(
        "loom.memory_record_conflicts",
        LIVE,
        USER_CONFLICT_RECORD_IDS,
        "records.tombstone",
        "records.tombstone",
    ),
    Substrate("loom.derived_facts", "1", UID, "derived_facts.delete", "derived_facts.delete"),
    # The usage fold's side table (keyed by identity + memory_key).
    Substrate(
        "loom.memory_reinforcement",
        "1",
        UID,
        "memory_reinforcement.delete",
        "memory_reinforcement.delete",
    ),
    # The three derived tables reach the user scope through the keys of the
    # window they quote (cache, precedents) or the entities only the user's
    # memories linked (communities) — parent-key predicates, as for the graph
    # links. Read at census time from loom.memory's tombstoned rows and the
    # edge links; under a purge the memory rows are gone and the predicates
    # read 0, as `USER_CHUNKS` does.
    Substrate(
        "loom.entity_communities",
        "1",
        USER_COMMUNITY_ROWS,
        "entity_communities.delete",
        "entity_communities.delete",
    ),
    Substrate(
        "loom.memory_ask_precedents",
        "1",
        USER_PRECEDENT_KEYS,
        "ask_precedents.delete",
        "ask_precedents.delete",
    ),
    Substrate(
        "loom.llm_response_cache",
        "1",
        USER_WINDOW_KEYS,
        "llm_response_cache.delete",
        "llm_response_cache.delete",
    ),
    # code.ask's answer cache: an answer that quoted memory excerpts names their
    # namespace and keys. Every version holding text counts, a tombstone an older build
    # wrote among them; this build's tombstones hold none.
    Substrate(
        "loom.code_answers",
        "1",
        f"{USER_MEMORY_KEYS} AND {_HOLDS_TEXT}",
        "code_answers.delete",
        "code_answers.delete",
        ns_pred=f"{MEMORY_NS_EQ} AND {_HOLDS_TEXT}",
    ),
    # User scope deletes the triples/edges only the user's
    # memories carried, through the link tables — but the census cannot count
    # them by user (no user axis), so they are namespace-only rows here.
    Substrate("loom.entity_edges", "1", None, "entity_graph.delete", None),
    Substrate("loom.entity_triples", "1", None, "entity_graph.delete", None),
    # The entity boost's table, deleted by `entity_vectors.delete`.
    Substrate(
        "loom.entity_vectors",
        LIVE,
        f"(user_id = {{uid:String}} OR {USER_PARENT_KEYS})",
        "entity_vectors.delete",
        "entity_vectors.delete",
    ),
    # The readable tables the review found outside the census.
    Substrate(
        "loom.conversations",
        LIVE,
        UID,
        "conversations.tombstone",
        "conversations.tombstone",
        final=False,
    ),
    Substrate(
        "loom.conversation_threads",
        LIVE,
        UID,
        "conversations.tombstone",
        "conversations.tombstone",
    ),
    Substrate(
        "loom.conversation_embeddings",
        LIVE,
        UID,
        "conversations.tombstone",
        "conversations.tombstone",
    ),
    Substrate(
        "loom.context_blobs",
        LIVE,
        UID,
        "context_blobs.tombstone",
        "context_blobs.tombstone",
    ),
    Substrate(
        "loom.session_briefs",
        "1",
        UID,
        "session_briefs.delete",
        "session_briefs.delete",
    ),
    Substrate(
        "loom.session_brief_revisions",
        "1",
        UID,
        "session_briefs.delete",
        "session_briefs.delete",
    ),
    Substrate(
        "loom.memory_exposures",
        "1",
        UID,
        "memory_exposures.delete",
        "memory_exposures.delete",
    ),
    Substrate("loom.search_trace_log", "1", None, "search_trace_log.delete", None),
    # A replayed response body quotes whatever the op returned. The rows name
    # the caller's org and subject but no namespace and no end user, so both
    # scopes clear and count the org's rows (a cache — the cost is one miss
    # per retried key); see delete_idempotency_by_user.
    Substrate(
        "loom.idempotency",
        "1",
        "1",
        "idempotency.delete",
        "idempotency.delete",
        ns_pred="1",
    ),
    Substrate(
        "loom.code_search_trace",
        "1",
        UID,
        "code_search_trace.delete",
        "code_search_trace.delete",
    ),
    Substrate("loom.code_search_outcome", "1", None, "code_search_trace.delete", None),
    Substrate("loom.events", "1", UID, "events.delete", "events.delete"),
    Substrate("loom.memory_drops", "1", None, "memory_drops.delete", None),
    # memory.delete_version's owed checks: a row's identity and key, 30-day TTL.
    Substrate(
        "loom.delete_version_checks",
        "1",
        UID,
        "delete_version_checks.delete",
        "delete_version_checks.delete",
    ),
    # Memory webhook events: a row's identity, key and id, never its text; read for 3
    # days, a day's part dropped within about 4 (the TTL drops whole parts). The
    # saga's own memory.erased announcements (no key, user '-') are not residue.
    Substrate(
        "loom.memory_webhook_outbox",
        "event_type != 'memory.erased'",
        UID,
        "memory_webhook_outbox.delete",
        "memory_webhook_outbox.delete",
        final=False,
    ),
    Substrate("loom.entity_degree", "1", None, "entity_graph.delete", None),
    Substrate(
        "loom.learnings_queue",
        "1",
        ORPHAN_LEARNINGS,
        "learnings_queue.delete",
        "learnings_queue.delete",
    ),
    Substrate(
        "loom.learnings_supporters",
        "1",
        UID,
        "learnings_queue.delete",
        "learnings_queue.delete",
    ),
    # A namespace's guide: free text its writers wrote about the namespace.
    Substrate("loom.namespace_guides", "1", None, "namespace_guides.delete", None),
    # Task inbox plane: the brief and the notes quote source
    # and issue text; keyed on the inbox's owner namespace, no user axis.
    Substrate("loom.tasks", "is_deleted = 0", None, "tasks.delete", None),
    Substrate("loom.task_events", "1", None, "tasks.delete", None, final=False),
    Substrate("loom.agent_runs", "1", UID, "agent_runs.delete", "agent_runs.delete"),
    Substrate("loom.operator_edges", LIVE, None, "curation.tombstone", None),
    Substrate("loom.conflict_allowlist", LIVE, None, "curation.tombstone", None),
    Substrate("loom.memory_categories", LIVE, None, "curation.tombstone", None),
    Substrate(
        "loom.consolidation_runs",
        "1",
        UID,
        "consolidation_runs.delete",
        "consolidation_runs.delete",
    ),
    # A failure's payload names its writer (a bulk write's identity).
    Substrate(
        "loom.memory_projection_failures",
        "1",
        PAYLOAD_UID,
        "projection_failures.delete",
        "projection_failures.delete",
    ),
    # The repository index's two content tables — chunked
    # source text and file/directory summaries — are counted for a namespace
    # scope and owned by `code_index.purge`; no user axis (the rows are the
    # repository's, not a subject's).
    Substrate("loom.code_chunks", "1", None, "code_index.purge", None),
    Substrate("loom.code_summaries", "1", None, "code_index.purge", None),
    # The knowledge plane: chunk text, and the whole documents with their
    # uploaded originals. A chunk is live by the predicate every reader and the
    # step's count use; a document by its own `superseded_at`, or while its
    # original is not erased yet; an original by its row. A user's are the ones
    # they wrote (`user_id`); an original shared with another writer's document
    # stays, so originals are counted by namespace only.
    Substrate(
        "loom.chunks", LIVE_CHUNK_SQL, UID, "chunks.supersede", "chunks.supersede", final=False
    ),
    Substrate(
        "loom.documents",
        "(superseded_at IS NULL OR (blob_path != '' AND blob_erased_at IS NULL))",
        UID,
        "documents.tombstone",
        "documents.tombstone",
    ),
    Substrate("loom.document_blobs", "1", None, "documents.tombstone", None, final=False),
    # Who uploaded each original: a user's rows go with their originals.
    Substrate(
        "loom.blob_uploads", "1", UID, "documents.tombstone", "documents.tombstone", final=False
    ),
    # Server-side ingestion: an upload names its file and its uploader; a job holds
    # the document's title, description, metadata and file name. Both go with the
    # documents.
    Substrate(
        "loom.knowledge_uploads",
        "1",
        UID,
        "documents.tombstone",
        "documents.tombstone",
        final=False,
    ),
    Substrate("loom.knowledge_ingestions", "1", UID, "documents.tombstone", "documents.tombstone"),
    # A cancel names its job and who asked for it.
    Substrate(
        "loom.knowledge_ingest_cancels",
        "1",
        UID,
        "documents.tombstone",
        "documents.tombstone",
        final=False,
    ),
    # The datasets typed from an ingested file's tables: their catalog (sheet and
    # column names) and their rows (the cell values) go with the documents.
    Substrate("loom.datasets", "1", UID, "documents.tombstone", "documents.tombstone"),
    Substrate(
        "loom.dataset_rows", "1", UID, "documents.tombstone", "documents.tombstone", final=False
    ),
)

# Steps that mutate a table the census does not count, with the reason. The
# registry test holds each saga's step list to `_CENSUS` owners ∪ these, so a
# step cannot appear without saying whether the census sees its table.
#   entity_canonical_map.tombstone   — alias map, no memory content.
#   agent_events.scrub               — identity scrubbed, rows kept for cost.
#   subject_last_seen.delete         — an end user's newest-call row: an
#                                       identifier, no memory content.
#   shared*.tombstone                — org-wide promoted rows, matched by
#                                       provenance not by scope.
#   consolidated.tombstone           — loom.memory rows under another identity,
#                                       and their code links; counted by the
#                                       memory row for the namespace scope only.
#                                       `memory.retire` (the user-scope owner of
#                                       loom.memory) runs the same pass again.
#   procedure_runs / query_nodes / session_outcomes / term_aggregates .delete
#                                    — derived ledgers without a read path
#                                       into memory content; not censused.
#   code_index.purge                 — the repository index; its two
#                                       content tables are census rows, the
#                                       rest are derived from them.
# Every other `loom.code_*` table keyed by (org, namespace) —
# symbols, edges, files, forge mirrors, watermarks, caches. `code_index.purge`
# reads the set from `system.columns` at run time; this tuple is the static
# twin the registry classifies, and a unit test holds the two to the schema
# so a new code table is classified the day it lands. Not counted
# by the census: they hold names, ids, counters and cursors derived from the
# two content tables above, and every one of them is a plain mutation.
CODE_INDEX_STEP_ONLY_TABLES: tuple[str, ...] = (
    "loom.code_call_sites_unresolved",
    "loom.code_caller_guards",
    "loom.code_chunks_prefix",
    "loom.code_context_packs",
    "loom.code_coverage_files",
    "loom.code_coverage_watches",
    "loom.code_doc_stats",
    "loom.code_edges",
    "loom.code_files",
    "loom.code_gh_items",
    "loom.code_index_state",
    "loom.code_pr_history",
    "loom.code_pr_reviews",
    "loom.code_review_briefs",
    "loom.code_scip_batches",
    "loom.code_scip_parts",
    "loom.code_search_memo",
    "loom.code_symbol_ids",
    "loom.code_symbol_names",
    "loom.code_symbol_stats",
    "loom.code_symbols",
    "loom.code_term_df",
    "loom.code_term_freq",
    "loom.code_test_facets",
    "loom.code_test_map",
    "loom.code_unit_risk",
    "loom.code_unit_signal",
)

STEP_ONLY_TABLES: dict[str, tuple[str, ...]] = {
    "entity_canonical_map.tombstone": ("loom.entity_canonical_map",),
    "agent_events.scrub": ("loom.agent_events",),
    "subject_last_seen.delete": ("loom.subject_last_seen",),
    "shared.tombstone": ("loom.shared",),
    "shared_embeddings.tombstone": ("loom.memory_embeddings",),
    "consolidated.tombstone": ("loom.memory", "loom.memory_embeddings", "loom.memory_code_links"),
    "procedure_runs.delete": ("loom.procedure_runs",),
    "query_nodes.delete": ("loom.query_nodes",),
    "session_outcomes.delete": SESSION_OUTCOME_TABLES,
    "term_aggregates.delete": TERM_AGGREGATE_TABLES,
    "code_index.purge": CODE_INDEX_STEP_ONLY_TABLES,
    # Who began each still-open run (KeeperMap: ids and user ids, no run text);
    # dropped beside the runs, not counted.
    "agent_runs.delete": ("loom.agent_run_claims",),
    # What a grant, a group and the collection tree say about a user as a
    # principal: identifiers, no memory content; rewritten, not counted.
    "grants.revoke": ("loom.namespace_grants",),
    "groups.remove_member": ("loom.principal_groups",),
    "personal.retire": ("loom.namespaces",),
    "collections.forget_owner": ("loom.collections", "loom.namespaces"),
    # The org's other cache: its cached query vectors (never their text), deleted
    # org-wide beside the replay bodies at both scopes but not counted, since any
    # search in the org refills them between the step and the census.
    "idempotency.delete": ("loom.embedding_cache",),
}

# Every other `loom.*` table in schemas/schema.sql, with the reason it holds
# no memory content in scope (or cannot be scoped). The registry test fails on
# a CREATE TABLE that is in none of the three lists, so a new table is
# classified the day it lands.
NO_MEMORY_CONTENT: dict[str, str] = {
    "loom.chunk_supersessions": (
        "One row per retired knowledge ingestion — (org, namespace, "
        "embedding_model, source_uri, doc_version, superseded_at), each a copy of a "
        "loom.chunks key; no chunk text. It narrows what loom.chunks serves and goes where "
        "that table's purge goes: a purge deletes the chunks it names, and a row left "
        "behind names no chunk."
    ),
    # -- telemetry derived from the ledger -----------------------------------
    "loom_telemetry.sli_minute": (
        "Request counts and latency quantiles per minute, operation and org, rolled up "
        "from loom.agent_events by a materialized view — counters keyed by operation and "
        "org name, no memory, conversation, document or code content; expires after 400 days."
    ),
    # -- configuration, not content ------------------------------------------
    "loom.deployment_config": (
        "The deployment's published settings every replica checks itself against (one "
        "JSON row, no secrets) — configuration, keyed by name, holding no memory, "
        "conversation, document or code content."
    ),
    "loom.provider_configuration": (
        "Deployment-wide model settings and encrypted API credentials with rebuild "
        "checkpoints; no memory, conversation, document or code content. "
        "Operator configuration is not owned by a data-plane principal."
    ),
    "loom.qa_sources": (
        "A tracker source a person registered (host, database, read-only user, "
        "an encrypted password, the tracker namespaces reading it) — deployment configuration "
        "keyed by (org, name), holding no memory or record content; retired by active=false."
    ),
    "loom.knowledge_sources": (
        "A document source a repository's maintainer registered (kind, folder ids, an "
        "encrypted credential) — deployment configuration keyed by (org, namespace, name), "
        "holding no memory or document content; retired by active=false."
    ),
    "loom.task_inboxes": (
        "An inbox a namespace admin registered (owner agent, posters, "
        "accepted disclosure, lease defaults) — configuration keyed by (org, inbox), "
        "holding no task text."
    ),
    "loom.task_poster_policies": (
        "Trust per poster and kind (deny/review/auto, caps, evidence "
        "field names, streak) — policy keyed by (org, inbox, poster, kind), no task text."
    ),
    "loom.task_leases": (
        "The live lease per task in ClickHouse Keeper (task id, owner "
        "label, fence, lease_until) — identifiers only, deleted on the terminal transition "
        "and by the lease sweep's reconcile for a task that is gone or ended."
    ),
    "loom.task_claims": (
        "A task claim arbitration log that no code path writes (task id, fence, "
        "claimant label, timestamp) — identifiers only, no task text."
    ),
    "loom.memory_webhook_attempts": (
        "The webhook worker's record per event (event id, status, attempt count, next "
        "attempt, last HTTP status and error): it holds no key or identity, so an event's "
        "erasure leaves nothing here to send or to read back; read for 3 days, a day's part "
        "is dropped within about 4."
    ),
    "loom.memory_webhooks": (
        "A webhook a namespace admin registered (URL, the name of the environment "
        "variable holding its signing secret, event types) — configuration keyed by "
        "(org, namespace, webhook_id), holding no memory content and no principal id: "
        "who registered it is in the audited webhook.register row."
    ),
    "loom.task_webhook_cursor": (
        "The signed-webhook delivery cursor per inbox (last acknowledged "
        "event seq, attempts, next try, last HTTP status) — identifiers only, no task text."
    ),
    "loom.code_repos": (
        "A repository entry a person added (GitHub owner/name, branch, "
        "join rule, an encrypted fetch token) — deployment configuration keyed by "
        "(org, namespace), holding no memory or source content; retired by active=false."
    ),
    # -- no tenant axis: cannot be scoped by namespace or user -----------------
    "loom.audit_checkpoints": "hash-chain checkpoints over the audit log; digests only.",
    "loom.blacklist_version": "singleton version counter for the blacklist cache.",
    "loom.acl_versions": (
        "change counts per org and kind for the grant cache; an org name, a kind and a number."
    ),
    "loom.worker_status": (
        "worker health per pass: worker name, launcher label, status, "
        "counters, a bounded error text and a resume cursor; `namespace` is the label of "
        "the namespace a pass reached, not a tenant axis over memory rows; the sensitivity "
        "sweep's cursor names an org, a namespace and an agent, never a user or a memory "
        "key; the document_blob_gc cursor is a bucket key: an org, a namespace and an "
        "original's sha256, no bytes; no user axis, no memory row; 30-day TTL."
    ),
    "loom.qa_link_candidates": (
        "issue and PR numbers of the public tracker record with scores and titles, keyed by "
        "org + tracker namespace; no memory row, no user axis."
    ),
    "loom.qa_item_embeddings": (
        "QA reference data, no memory content, namespace-scoped: the "
        "public tracker record's issue and PR numbers, titles and the vectors loom's tracker_embed "
        "pass computed over them, keyed by org + tracker namespace; no memory row, no user axis."
    ),
    "loom.code_backport_gaps": (
        "PR numbers, titles and backport verdicts of the public code record per release "
        "branch, keyed by org + code namespace; no memory row, no user axis."
    ),
    "loom.revoked_tokens": "auth plane: revoked JWT ids.",
    "loom.machine_identities": "auth plane: registered bots and their public keys; no memory.",
    "loom.org_model_calls": "whether an org's data may go to a model, and who said so; no memory.",
    "loom.extraction_policies": "the operator's rules for what extraction stores; no memory.",
    "loom.sso_links": "auth plane: which password accounts SSO sign-ins reach; no memory.",
    "loom.scim_users": (
        "auth plane: the people the identity provider provisions (email, the emails "
        "list it sent, userName, active) and the subjects a deactivation revoked; no memory."
    ),
    "loom.oauth_signin_state": "auth plane: sign-in state in Keeper (codes, sessions); no memory.",
    "loom.oauth_state": "auth plane: OAuth client registrations and codes; no memory rows.",
    "loom.oauth_claims": "auth plane: no code path writes it; never holds memory content.",
    "loom.grant_claims": "auth plane: no code path writes it; never holds memory content.",
    "loom.service_accounts": "auth plane: service-account definitions.",
    "loom.user_state": "per-subject UI state (default namespace).",
    # -- ledgers of the erasure itself, kept by design ----------
    "loom.erasure_requests": "the erasure ledger; scope_value names the scope, 90-day TTL.",
    "loom.erasure_reversals": "reversal ledger: memory ids/keys restored after an erasure.",
    "loom.audit_log": (
        "the audit trail, including of the deletion itself; `subject` names keys, ids, "
        "principals and vocabulary values, and holds other text only as a SHA-256 and "
        "length (`audit.input_subject`)."
    ),
    # -- counters and aggregates: ids or counts, no text -----------------------
    "loom.memory_usage_events": "usage ledger keyed by memory_id; counts only.",
    "loom.memory_usage_totals": "SummingMergeTree of the usage ledger; counts by memory_id.",
    "loom.memory_served_daily": "agent reads that returned a memory, per day; counts by memory_id.",
    "loom.memory_exposure_breadth_hourly": "hourly exposure breadth per actor/agent; no keys.",
    "loom.agent_cost_rollup_daily": "cost aggregates per (org, namespace, agent, day, operation).",
    "loom.agent_turn_usage": "token counts and cost per turn; no text.",
    "loom.namespace_write_marks": "last-write marks per plane (AggregatingMergeTree).",
    "loom.namespace_write_keys": "each namespace's last-write time as a Keeper key.",
    "loom.graph_projections": (
        "ids and start times of graph projections in flight and each namespace's last "
        "graph-write time, as Keeper keys."
    ),
    "loom.auth_attempts": "keyed hashes of sign-in identifiers and client addresses; 1-day TTL.",
    "loom.keyring_readers": "each process's host, pid and keyring refresh interval; 1-day TTL.",
    "loom.carry_acks": "each process's host, pid and column-list fingerprints; 1-day TTL.",
    "loom.storage_authority": "the deployment's memory authority, epoch and id; no tenant data.",
    "loom.instances": "one heartbeat row per Loom process: build, fingerprints, host; no content.",
    "loom.frontend_assets": "the dashboard's built JS, CSS and fonts; no tenant data; 30-day TTL.",
    "loom.embedder_calibration": "similarity thresholds per embedder model; no content.",
    "loom.usage_acted_credits": "hashes of credited write/read pairs; no text; 3-day TTL.",
    "loom.policy_decisions": (
        "request-level policy verdicts: rule ids, error codes, latency; no memory text; "
        "90-day TTL ages the identity columns out."
    ),
    # -- namespace registry and configuration ---------------------------------
    "loom.signing_keys": "token signing keys, sealed with the root secret; no subject data.",
    "loom.runtime_overrides": (
        "operator rate-limit, latency-budget and setting overrides: numbers and "
        "operator-written names and patterns (namespaces, orgs, agents, tool globs), "
        "no memory content."
    ),
    "loom.namespace_types": "per-type retention/dedup configuration.",
    "loom.namespace_visits": "last-seen per (subject, namespace) for the namespace picker.",
    "loom.namespace_policies": "admin-authored access policies (effect, actions, condition).",
    "loom.namespace_ontology": "admin-registered kinds/names for the ontology.",
    "loom.namespace_snapshots": (
        "counts and the source namespace of each snapshot; the payload is returned "
        "to the caller and never stored."
    ),
    "loom.namespace_calibration_result": "calibrated thresholds per namespace; numbers only.",
    "loom.categories": "namespace taxonomy (name, description, summary) authored by the operator",
    "loom.memory_schemas": "admin-authored record schemas.",
    "loom.memory_schema_fields": "fields of the admin-authored record schemas.",
    "loom.profile_schemas": (
        "admin-authored profile schemas: property names and descriptions; each user's "
        "profile built from one is a loom.memory row."
    ),
    "loom.blacklist": "consent patterns authored by admins; policy, not subject data.",
    # -- tracker plane: org-shared reference data mirrored from CI, not anyone's
    # memory; rebuilt from the source, never read into a memory answer.
    "loom.qa_test_daily": (
        "tracker plane: per-test daily roll-up of the public CI history (counts, hours, "
        "PR numbers); re-rolled from the source by the tracker_test_daily worker, "
        "no memory content."
    ),
    "loom.qa_fail_hours": ("tracker plane: failing tests and PR heads per CI hour; counts only."),
    "loom.qa_rollup_days": ("tracker plane: the roll-up worker's ledger per day; counts only."),
}


def classified_tables() -> dict[str, str]:
    """Every table the registry knows → which list holds it
    (`census` | `step_only` | `no_memory_content`)."""
    out: dict[str, str] = {}
    for sub in _CENSUS:
        out[sub.table] = "census"
    for tables in STEP_ONLY_TABLES.values():
        for t in tables:
            out.setdefault(t, "step_only")
    for t in NO_MEMORY_CONTENT:
        out.setdefault(t, "no_memory_content")
    return out


def _pg_census() -> dict[str, str]:
    """The Postgres authority's substrates, counted only under that authority."""
    return PG_CENSUS if pg.enabled() else {}


def census_tables(scope_type: str) -> list[str]:
    """The substrates the census covers for a scope (exposed for tests/docs): the
    ClickHouse tables, then the Postgres authority's (`pg:`), counted in both scopes."""
    return [*(s.table for s in _CENSUS if s.counted_in(scope_type)), *_pg_census()]


def step_owners(scope_type: str) -> dict[str, tuple[str, ...]]:
    """Saga step → the census tables it owns in `scope_type` (namespace|user_id)."""
    out: dict[str, list[str]] = {}
    for s in _CENSUS:
        if not s.counted_in(scope_type):
            continue
        step = s.owner(scope_type)
        if step is not None:
            out.setdefault(step, []).append(s.table)
    for table, step in _pg_census().items():
        out.setdefault(step, []).append(table)
    return {k: tuple(v) for k, v in out.items()}


def steps_to_reopen(residue: Mapping[str, Any] | str | None, scope_type: str) -> set[str]:
    """The steps a resumed saga must run again: the owners of every table the
    last census found rows in. A checkpoint says a step ran;
    the census says what it left. When they disagree the census wins — the
    steps are count-first and idempotent, so re-running one over a clean
    table costs a single count(). `residue` is the ledger's per-table map
    (JSON or dict); `OBJECT_RESIDUE` re-opens `documents.tombstone`, and other
    keys the registry does not know are ignored."""
    if not residue:
        return set()
    if isinstance(residue, str):
        raw = residue
        try:
            residue = json.loads(raw) if raw.strip() else {}
        except ValueError:
            log.warning("erasure: unreadable residue %r; nothing re-opened", raw[:120])
            return set()
    if not isinstance(residue, Mapping):
        return set()
    hot = {str(t) for t, n in residue.items() if int(n or 0) > 0}
    if not hot:
        return set()
    reopen = {_OBJECT_OWNER} if OBJECT_RESIDUE in hot else set()
    hot |= {"loom." + t for t in hot if not t.startswith(("loom.", "pg:"))}
    reopen |= {
        step for step, tables in step_owners(scope_type).items() if any(t in hot for t in tables)
    }
    return reopen


async def residue_census(
    org: str,
    scope_type: str,
    scope_value: str | Sequence[str],
    *,
    ch: _CHClient | None = None,
) -> dict[str, int]:
    """Live rows still in scope, per substrate: ClickHouse's tables (`table_residue`)
    and the Postgres authority's. Bucket objects are not counted here: see
    `object_residue`, which a namespace scope's verify and `evidence_census` add."""
    out = await table_residue(org, scope_type, scope_value, ch=ch)
    out.update(await pg_residue(org, scope_type, scope_value))
    return out


async def table_residue(
    org: str,
    scope_type: str,
    scope_value: str | Sequence[str],
    *,
    ch: _CHClient | None = None,
) -> dict[str, int]:
    """Live rows still in scope per ClickHouse table. One count() per table; FINAL
    on the ReplacingMergeTree tables so an un-merged tombstone row does not
    read as live."""
    ch = ch or _default_client()
    params: dict[str, Any] = {"org": org}
    ns_list = isinstance(scope_value, (list, tuple))
    if scope_type == "namespace":
        params["ns"] = [str(x) for x in scope_value] if ns_list else scope_value
    else:
        params["uid"] = scope_value
    out: dict[str, int] = {}
    # A purge leaves nothing behind, so its census counts every row, not the
    # live ones.
    purge = purge_mode()
    for sub in _CENSUS:
        table, live = sub.table, sub.live
        if scope_type != "namespace":
            pred = sub.user_pred
        else:
            pred = sub.ns_pred or NS_EQ
            if ns_list:  # the rewrite the steps' count_then_mutate applies
                pred = ns_list_predicate(pred)
        if pred is None:
            continue
        if purge:
            live = "1"
        final = " FINAL" if live != "1" and sub.final else ""
        row = await ch.query_one(
            f"SELECT count() AS n FROM {table}{final} "
            f"WHERE org = {{org:String}} AND {pred} AND {live}",
            params=params,
        )
        out[table] = int((row or {}).get("n") or 0)
    out.update(await copy_residue(org, scope_type, scope_value, ch=ch))
    return out


async def evidence_census(
    org: str,
    scope_type: str,
    scope_value: str | Sequence[str],
    *,
    ch: _CHClient | None = None,
) -> tuple[dict[str, int], dict[str, Exception]]:
    """`residue_census` for evidence, not a verdict: ClickHouse's tables, then Postgres
    and the bucket each on its own, so a store that cannot be read (often why a step
    failed) costs only its own counts. Returns the counts and each such store's error."""
    out = await table_residue(org, scope_type, scope_value, ch=ch)
    errors: dict[str, Exception] = {}
    try:
        out.update(await pg_residue(org, scope_type, scope_value))
    except pg.AuthorityMismatch:
        raise  # another deployment's database: a refusal, not a store that failed
    except Exception as e:  # noqa: BLE001 — the counts above stand; the caller reports the error
        errors["postgres"] = e
    if scope_type == "namespace":
        try:
            out[OBJECT_RESIDUE] = await object_residue(org, scope_value)
        except Exception as e:  # noqa: BLE001 — as above
            errors["bucket"] = e
    return out, errors


async def object_residue(org: str, scope_value: str | Sequence[str]) -> int:
    """Objects left under the erased namespaces' prefixes: after the step, any
    is residue. Each prefix counts up to one listing page of the scope's own keys
    (`_owned_on_first_page`), so a census that runs before the step reports "at
    least" that many rather than listing the whole namespace. With no bucket
    configured there are none to count (a row naming one fails the step itself)."""
    bucket = settings.document_s3_bucket
    if not bucket:
        return 0
    left = 0
    for n in namespaces_of(scope_value):
        left += await _owned_on_first_page(bucket, org, n)
    return left


async def _owned_on_first_page(bucket: str, org: str, namespace: str) -> int:
    """The scope's own keys on the first listing page under its prefix that holds
    any: after a prefix change another scope's keys can fill the pages before them."""
    after = ""
    while True:
        page = await object_store.list_page(
            bucket, object_store.namespace_prefix(org, namespace), start_after=after
        )
        owned = sum(1 for o in page.objects if object_store.in_namespaces(o.key, org, {namespace}))
        if owned or not page.more or not page.objects:
            return owned
        after = page.objects[-1].key
