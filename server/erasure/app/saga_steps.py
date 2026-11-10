"""The saga's steps: the ordered (step name, store function) tables of the
user and namespace sagas, the few steps with logic of their own (the code
index purge, the documents and their blobs), and the residue census that ends
every saga. `server.erasure.app.saga` runs them in order and checkpoints
each."""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable, Sequence
from contextlib import AsyncExitStack
from datetime import UTC, datetime
from typing import Any

from server.auth.grant_claims import account_change, collections_change, group_change
from server.code.app.index.retire import code_tables_keyed_by_namespace, refresh_code_views
from server.core import grant_cache
from server.core.errors import caller_text
from server.core.principals import personal_name
from server.erasure.store import census as erasure_census
from server.erasure.store import steps_graph as graph_steps
from server.erasure.store import steps_ledgers as ledger_steps
from server.erasure.store import steps_memory as memory_steps
from server.erasure.store import steps_pg as pg_steps
from server.erasure.store import steps_principals as principal_steps
from server.memory.app.code_links import queue_lease
from server.workers.registry import DEADLINE_CAP_SECONDS

log = logging.getLogger(__name__)

_NS = str | Sequence[str]


class ErasureResidueError(RuntimeError):
    """The post-erasure census found live rows in scope. Raised inside the
    verify step so the step is NOT recorded as completed: the recovery
    worker re-runs the incomplete steps and verifies again."""

    def __init__(self, per_table: dict[str, int]) -> None:
        self.per_table = per_table
        self.count = sum(per_table.values())
        residue = {t: n for t, n in per_table.items() if n}
        super().__init__(f"residue after erasure: {residue}")


class ErasureStepError(RuntimeError):
    """A saga step failed. Names the step and `caller_text` of the cause, which the
    ledger's `failure_reason` and the `erasure.failed` audit event carry to the
    requester. A backend's text (a statement, an address, a remote's response) stays
    in the log; a residue failure keeps its census counts, since they name what is left."""

    def __init__(self, step: str, cause: BaseException) -> None:
        self.step = step
        self.cause = cause
        detail = caller_text(cause)
        if isinstance(cause, ErasureResidueError) and (counts := str(cause).strip()):
            detail = f"{type(cause).__name__}: {counts}"
        super().__init__(f"{step}: {detail}")


async def _step_verify_residue(
    org: str, scope_type: str, scope_value: str | Sequence[str], holder: dict[str, Any]
) -> int:
    """Verified forgetting: count what is still live in scope across every
    substrate the read paths touch. Fills `holder` for the terminal
    checkpoint; zero is the only pass."""
    per_table = await erasure_census.residue_census(org, scope_type, scope_value)
    if scope_type == "namespace":  # and the scope's objects left in the document bucket
        per_table[erasure_census.OBJECT_RESIDUE] = await erasure_census.object_residue(
            org, scope_value
        )
    holder["per_table"] = per_table
    holder["count"] = sum(per_table.values())
    holder["verified_at"] = datetime.now(UTC).replace(tzinfo=None)
    if holder["count"]:
        raise ErasureResidueError(per_table)
    return 0


async def _step_purge_code_index_by_ns(org: str, namespace: _NS) -> int:
    """A code namespace's repository index — every
    loom.code_* table keyed by (org, namespace) — leaves with the namespace."""
    return await memory_steps.purge_code_index_by_namespace(
        org,
        namespace,
        code_tables=code_tables_keyed_by_namespace,
        refresh_views=refresh_code_views,
    )


# how long a code-link erasure waits for a linking pass to give up the queue lease: longer
# than the link worker's pass deadline (a test holds it so)
_LINK_PASS_WAIT_S = DEADLINE_CAP_SECONDS


async def _step_delete_code_links_by_user(org: str, user_id: str) -> int:
    """The user's code links, erased under the link worker's queue lease, which every
    cursor write is under: no pass then publishes a cursor it read before the erasure."""
    async with queue_lease(wait_s=_LINK_PASS_WAIT_S):
        return await memory_steps.delete_code_links_by_user(org, user_id)


async def _step_delete_code_links_by_ns(org: str, namespace: _NS) -> int:
    """The namespace's code links, as `_step_delete_code_links_by_user`."""
    async with queue_lease(wait_s=_LINK_PASS_WAIT_S):
        return await memory_steps.delete_code_links_by_namespace(org, namespace)


async def _step_tombstone_documents_by_ns(org: str, namespace: _NS) -> int:
    """Erase the namespace's uploaded originals, then supersede its
    whole-document rows (a purge deletes them). The originals go first, while
    the rows their erasure is stamped on still exist. One step owns both, so
    the census rows for loom.documents and loom.document_blobs re-open it."""
    await _step_erase_blobs_by_ns(org, namespace)
    await memory_steps.delete_ingestions_by_namespace(org, namespace)
    return await memory_steps.tombstone_documents_by_namespace(org, namespace)


async def _step_revoke_grants_by_user(org: str, user_id: str) -> int:
    """Every grant naming the user as a principal, `user:<user_id>`, revoked
    (which moves the watermark) and then deleted; then the user's id off the
    grants they gave others. Returns the grants revoked."""
    from server.identity.app.grants import forget_granter, revoke_subject
    from server.memory.app.provenance import writer_actor

    principal = "user:" + user_id
    revoked = await revoke_subject(org, principal, actor=writer_actor("-"))
    await forget_granter(org, principal)
    await principal_steps.delete_grant_rows_of_user(org, user_id)
    return revoked


async def _step_remove_from_groups_by_user(org: str, user_id: str) -> int:
    """Each group under the claim a SCIM change of it holds, so a PATCH that
    read the group first cannot put the user back."""
    removed = 0
    groups = await principal_steps.groups_of_user(org, user_id)
    if not groups:
        return 0
    async with grant_cache.announced(org, kind=grant_cache.GROUPS):
        for group_id in groups:
            async with group_change(org, group_id):
                removed += await principal_steps.remove_from_group(org, group_id, user_id)
    return removed


async def _step_retire_personal_collection_by_user(org: str, user_id: str) -> int:
    """Under its directory row's claim, which `namespace.delete` holds too."""
    async with account_change(org, personal_name(org, "user:" + user_id)):
        return await principal_steps.retire_personal_collection_by_user(org, user_id)


async def _step_forget_collection_owner_by_user(org: str, user_id: str) -> int:
    """Under each directory row's claim, then the tree's, so neither a
    `namespace.delete` nor a move that lands meanwhile writes back what it read."""
    async with AsyncExitStack() as held:
        for namespace in await principal_steps.collections_created_by(org, user_id):
            await held.enter_async_context(account_change(org, namespace))
        await held.enter_async_context(collections_change(org))
        # No watermark: the cached tree holds parents, not owners.
        return await principal_steps.forget_collection_owner_by_user(org, user_id)


async def _step_tombstone_documents_by_user(org: str, user_id: str) -> int:
    """The user's originals first, as `_step_tombstone_documents_by_ns`, then
    the documents they wrote."""
    await memory_steps.erase_blobs_by_user(org, user_id)
    await memory_steps.delete_ingestions_by_user(org, user_id)
    return await memory_steps.tombstone_documents_by_user(org, user_id)


async def _step_erase_blobs_by_ns(org: str, namespace: _NS) -> int:
    """Delete the uploaded originals of the namespace(s), stamp
    `blob_erased_at` on their documents, then delete the record of who
    uploaded them. Returns the originals deleted."""
    n = await memory_steps.delete_document_blobs_by_namespace(org, namespace)
    await memory_steps.stamp_blobs_erased(org, namespace)
    await memory_steps.delete_blob_uploads_by_namespace(org, namespace)
    return n


UserStep = tuple[str, Callable[[str, str], Awaitable[int]]]
NamespaceStep = tuple[str, Callable[[str, _NS], Awaitable[int]]]

# The steps that run after the memory steps; each deletes or tombstones its own tables.
_LATE_STEPS_NS: tuple[NamespaceStep, ...] = (
    ("conversations.tombstone", ledger_steps.tombstone_conversations_by_namespace),
    ("context_blobs.tombstone", ledger_steps.tombstone_context_blobs_by_namespace),
    ("session_briefs.delete", ledger_steps.delete_session_briefs_by_namespace),
    ("memory_exposures.delete", ledger_steps.delete_memory_exposures_by_namespace),
    ("search_trace_log.delete", ledger_steps.delete_search_trace_log_by_namespace),
    ("code_search_trace.delete", ledger_steps.delete_code_search_trace_by_namespace),
    ("events.delete", ledger_steps.delete_events_by_namespace),
    ("memory_drops.delete", ledger_steps.delete_memory_drops_by_namespace),
    ("delete_version_checks.delete", ledger_steps.delete_delete_version_checks_by_namespace),
    ("memory_webhook_outbox.delete", ledger_steps.delete_memory_webhook_outbox_by_namespace),
    ("learnings_queue.delete", ledger_steps.delete_learnings_queue_by_namespace),
    ("namespace_guides.delete", ledger_steps.delete_namespace_guides_by_namespace),
    ("tasks.delete", ledger_steps.delete_tasks_by_namespace),
    ("agent_runs.delete", ledger_steps.delete_agent_runs_by_namespace),
    ("curation.tombstone", ledger_steps.tombstone_curation_by_namespace),
    ("consolidation_runs.delete", ledger_steps.delete_consolidation_runs_by_namespace),
    ("projection_failures.delete", ledger_steps.delete_projection_failures_by_namespace),
    ("idempotency.delete", ledger_steps.delete_idempotency_by_namespace),
    ("code_index.purge", _step_purge_code_index_by_ns),
)
_LATE_STEPS_USER: tuple[UserStep, ...] = (
    ("conversations.tombstone", ledger_steps.tombstone_conversations_by_user),
    ("context_blobs.tombstone", ledger_steps.tombstone_context_blobs_by_user),
    ("session_briefs.delete", ledger_steps.delete_session_briefs_by_user),
    ("memory_exposures.delete", ledger_steps.delete_memory_exposures_by_user),
    ("code_search_trace.delete", ledger_steps.delete_code_search_trace_by_user),
    ("events.delete", ledger_steps.delete_events_by_user),
    ("delete_version_checks.delete", ledger_steps.delete_delete_version_checks_by_user),
    ("memory_webhook_outbox.delete", ledger_steps.delete_memory_webhook_outbox_by_user),
    ("agent_runs.delete", ledger_steps.delete_agent_runs_by_user),
    ("learnings_queue.delete", ledger_steps.delete_learnings_queue_by_user),
    ("consolidation_runs.delete", ledger_steps.delete_consolidation_runs_by_user),
    ("projection_failures.delete", ledger_steps.delete_projection_failures_by_user),
    # Org-wide, not by the stamped subject: a replay a service account stored
    # while acting for the user carries the user's text under the account's
    # subject. A cache, so deleting more costs only misses.
    ("idempotency.delete", ledger_steps.delete_idempotency_by_user),
)


# Steps a ClickHouse-authority saga never runs.
PG_ONLY_STEPS = frozenset({"pg_memory.delete", "pg_knowledge.delete"})


def authority_steps[StepT: tuple[str, Any]](steps: tuple[StepT, ...]) -> list[StepT]:
    """The steps a saga runs: the Postgres authority's step only under that authority."""
    from server.core import pg

    return [s for s in steps if pg.enabled() or s[0] not in PG_ONLY_STEPS]


# The loom-side steps of a user erasure over (org, user_id), in saga order.
# The step names are stored in the ledger's `steps_completed`: renaming one
# makes a resumed saga run it again.
USER_STEPS: tuple[UserStep, ...] = (
    # First: the authority's copy, then the wait for its publishers, so every
    # ClickHouse copy the steps below look for has landed.
    ("pg_memory.delete", pg_steps.delete_pg_memory_by_user),
    ("pg_knowledge.delete", pg_steps.delete_pg_knowledge_by_user),
    ("memory.tombstone", memory_steps.tombstone_memory_by_user),
    ("memory_embeddings.tombstone", memory_steps.tombstone_embeddings_by_user),
    # The summaries built from the user's memories, under another identity: retired
    # with the user's rows, before the steps that remove what was derived from either.
    ("consolidated.tombstone", graph_steps.tombstone_consolidated_dependents_by_user),
    ("code_links.delete", _step_delete_code_links_by_user),
    ("working_memory.tombstone", memory_steps.tombstone_working_memory_by_user),
    # The search rows first: they are found through the shared rows, which a purge deletes.
    ("shared_embeddings.tombstone", memory_steps.tombstone_shared_embeddings_by_user),
    ("shared.tombstone", memory_steps.tombstone_shared_by_user),
    ("agent_events.scrub", memory_steps.scrub_events_by_user),
    ("derived_facts.delete", memory_steps.delete_derived_facts_by_user),
    ("memory_reinforcement.delete", memory_steps.delete_memory_reinforcement_by_user),
    ("raw_evidence.delete", memory_steps.delete_raw_evidence_by_user),
    ("memory_chunks.tombstone", memory_steps.tombstone_memory_chunks_by_user),
    ("source_links.tombstone", graph_steps.tombstone_source_links_by_user),
    ("entity_vectors.delete", graph_steps.delete_entity_vectors_by_user),
    # With the links, the triples, edges and community rows only this user's memories (or
    # summaries of them) carried, read from the links first.
    ("graph_links.delete", graph_steps.delete_graph_links_by_user),
    # The answer text that quoted the user's memories or a summary of them: the
    # exact-prompt cache, the precedents and code.ask's answer cache, by the keys each
    # row was shown. After the summaries' tombstone, so no new answer can quote one.
    ("llm_response_cache.delete", graph_steps.delete_llm_response_cache_by_user),
    ("ask_precedents.delete", graph_steps.delete_ask_precedents_by_user),
    ("code_answers.delete", graph_steps.delete_code_answers_by_user),
    # After the cache steps, which read a summary's history.
    ("memory_versions.delete", memory_steps.delete_memory_versions_by_user),
    ("summaries.tombstone", graph_steps.tombstone_summaries_by_user),
    ("records.tombstone", graph_steps.tombstone_records_by_user),
    ("session_outcomes.delete", memory_steps.delete_session_outcomes_by_user),
    # Last of the steps that find rows by the user's memory keys: a purge deletes
    # the memory rows here, not in `memory.tombstone`, so those steps can read them.
    ("memory.retire", graph_steps.retire_memory_by_user),
    # The knowledge the user wrote, in any namespace of the org; chunks first, as for a
    # namespace.
    ("chunks.supersede", memory_steps.supersede_chunks_by_user),
    ("documents.tombstone", _step_tombstone_documents_by_user),
    # The user as a principal (`user:<user_id>`): what a grant, a group or the
    # tree still says about them.
    ("grants.revoke", _step_revoke_grants_by_user),
    ("groups.remove_member", _step_remove_from_groups_by_user),
    ("personal.retire", _step_retire_personal_collection_by_user),
    ("collections.forget_owner", _step_forget_collection_owner_by_user),
    # Last of the counted steps, after the scrub: a refresh of the table that
    # read the ledger before the scrub landed has most likely finished by now.
    # That is a bound in time, not an order: one still running could write the
    # row back, and the user's later calls write a new one.
    ("subject_last_seen.delete", memory_steps.delete_last_seen_by_user),
    *_LATE_STEPS_USER,
)

# The loom-side steps of a namespace erasure over (org, namespace or list), in
# saga order. Names as for USER_STEPS.
NAMESPACE_STEPS: tuple[NamespaceStep, ...] = (
    ("pg_memory.delete", pg_steps.delete_pg_memory_by_namespace),
    ("pg_knowledge.delete", pg_steps.delete_pg_knowledge_by_namespace),
    ("memory.tombstone", memory_steps.tombstone_memory_by_namespace),
    ("memory_embeddings.tombstone", memory_steps.tombstone_embeddings_by_namespace),
    ("memory_versions.delete", memory_steps.delete_memory_versions_by_namespace),
    ("code_links.delete", _step_delete_code_links_by_ns),
    ("working_memory.tombstone", memory_steps.tombstone_working_memory_by_namespace),
    (
        "entity_canonical_map.tombstone",
        memory_steps.tombstone_entity_canonical_map_by_namespace,
    ),
    ("derived_facts.delete", memory_steps.delete_derived_facts_by_namespace),
    ("memory_reinforcement.delete", memory_steps.delete_memory_reinforcement_by_namespace),
    ("raw_evidence.delete", memory_steps.delete_raw_evidence_by_namespace),
    ("memory_chunks.tombstone", memory_steps.tombstone_memory_chunks_by_namespace),
    ("source_links.tombstone", graph_steps.tombstone_source_links_by_namespace),
    ("graph_links.delete", graph_steps.delete_graph_links_by_namespace),
    ("entity_graph.delete", graph_steps.delete_entity_graph_by_namespace),
    ("entity_vectors.delete", graph_steps.delete_entity_vectors_by_namespace),
    ("summaries.tombstone", graph_steps.tombstone_summaries_by_namespace),
    ("records.tombstone", graph_steps.tombstone_records_by_namespace),
    ("entity_communities.delete", graph_steps.delete_entity_communities_by_namespace),
    ("ask_precedents.delete", graph_steps.delete_ask_precedents_by_namespace),
    ("llm_response_cache.delete", graph_steps.delete_llm_response_cache_by_namespace),
    ("code_answers.delete", graph_steps.delete_code_answers_by_namespace),
    ("procedure_runs.delete", memory_steps.delete_procedure_runs_by_namespace),
    ("query_nodes.delete", memory_steps.delete_query_nodes_by_namespace),
    ("session_outcomes.delete", memory_steps.delete_session_outcomes_by_namespace),
    ("term_aggregates.delete", memory_steps.delete_term_aggregates_by_namespace),
    # The chunk supersede hides the RAG-projected text; documents.tombstone then
    # deletes the original blob bytes and tombstones the whole-document rows.
    ("chunks.supersede", memory_steps.supersede_chunks_by_namespace),
    ("documents.tombstone", _step_tombstone_documents_by_ns),
    *_LATE_STEPS_NS,
)
