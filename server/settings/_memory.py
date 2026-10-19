"""The memory plane outside search/ask/extraction: writes, conflicts, lifecycle, workers."""

from __future__ import annotations

from typing import Annotated, Literal

from pydantic import BaseModel, Field

from server.settings._env import CaseFolded


class MemorySettings(BaseModel):
    # ---- prompt safety and search priors ----
    # Cap on memory.search's metadata priors (confidence, soft tag/category) so
    # they break ties instead of re-ranking. 0.0 = uncapped; a request's
    # `prior_cap` overrides it.
    memory_prior_cap: float = 0.08
    # Memory bodies in every synthesis and extraction prompt sit between
    # nonce-carrying data markers (server/memory/domain/prompt_fence.py).
    # Key for the fence nonce, HMAC(secret, prompt): an identical prompt renders
    # identically (the response cache keys on it) while a memory author cannot
    # predict the closing marker. "" = derive from jwt_secret.
    prompt_fence_secret: str = ""

    # Characters each injected working-memory block renders before it is cut with "(truncated)".
    memory_scratchpad_max_chars: int = Field(default=2000, ge=1)

    # ---- deployment-specific names the fact filters recognise ----
    # Comma-separated names of the people whose sessions this deployment records
    # ("jane,jane doe"). The junk-fact filters read "Jane ran a command …" like
    # "The user ran a command …". Empty = only the generic actors (user, assistant,
    # Claude, the agent).
    memory_quality_actor_names: str = ""
    # Regex (case-insensitive) matching the host name of another agent session on
    # this deployment ("build-[0-9]+"); a fact whose subject is such a peer is
    # coordination chatter, not a durable fact. Empty = no host pattern.
    memory_quality_peer_host_pattern: str = ""

    # ---- write-path contracts and reinforcement ----
    # Negative memory.outcome signals after which a memory is quarantined
    # (status 'rejected', excluded from search). 0 = off: negatives stay
    # audit-only.
    memory_negative_demote_threshold: int = 0
    # Recall-equivalents one `used` event (run.complete memories_used,
    # memory.outcome "used") adds to search-side reinforcement; the boost
    # follows use, not exposure. 0 = recorded, not reinforcing.
    memory_used_reinforcement_weight: int = 2
    # Recall-equivalents one `harm` event (a relied-on row in a run that ended
    # badly) subtracts. Floored at the write-time base: harm cancels earned
    # reinforcement, never ranks below cosine and never deletes. None = the
    # `used` weight; 0 = counted in memory.health, not charged.
    memory_harm_reinforcement_weight: int | None = None
    # Recall-equivalents one `used_cited` event (memory.ask crediting the keys
    # its answer cited) adds; `used` stays for labels an agent wrote. None =
    # the `used` weight; 0 = recorded, not ranking.
    memory_used_cited_reinforcement_weight: int | None = None
    # Days a consumer's closer keeps its run-less reads credited.
    memory_reinforcement_closer_window_days: int = 14
    # Seconds one (org, consumer) closer answer is cached per worker.
    memory_reinforcement_closer_cache_seconds: int = 300
    # memory.list rows per call for a token below writer on the namespace; it
    # pages with `offset` (wire cap stays 1000). 0 = no viewer cap.
    memory_list_viewer_limit: int = 100
    # Transcript characters the session-digest call reads (~4 chars a token);
    # 0 = the built-in 3000.
    memory_session_digest_max_input_chars: int = 3000
    # content_excerpt ceiling for answer-bearing facts (question_topic set)
    # when memory_capture_structured_answers is on, so an enumerated answer
    # is not clipped mid-list; every other fact keeps EXCERPT_LIMIT (1,200 chars).
    memory_structured_answer_excerpt_limit: int = 4096
    # A searchable memory.set value serializing longer than this many chars
    # gets one advisory line (the write still lands): only the first
    # EXCERPT_LIMIT chars are embedded, and past the embedder's 8,191-token cap
    # nothing is. 30,000 chars ≈ 8k tokens. 0 disables.
    memory_value_advisory_chars: int = 30_000

    # ---- conflict detection ----
    # memory.set flags a write `conflicted` when an active peer in the same
    # (org, namespace, memory_type) sits at cosine ≥ this. Tuned
    # for text-embedding-3-*; 1.0 disables. Per-namespace override:
    # loom.namespaces.conflict_threshold.
    conflict_threshold: float = 0.85
    # When the cosine check would flag a write, one cheap LLM call labels the
    # closest peers (contradiction / elaboration / restatement / unrelated) and
    # the verdict follows: conflict / refines (supersedes with a `refines`
    # edge) / merge / clear. One call per flagged write.
    memory_conflict_judge: bool = False
    # Closest peers the conflict judge labels in its one call per flagged write.
    memory_conflict_judge_max_peers: int = Field(default=3, ge=1)
    # With the judge on, peers down to this cosine are judged too (never
    # flagged on cosine alone), catching knowledge updates filed under a
    # different key.
    memory_conflict_judge_min_similarity: float = Field(default=0.70, gt=0.0, le=1.0)
    # Entries per bulk call that get the conflict check; later entries are written unchecked
    # and counted. 0 = none are checked.
    bulk_conflict_check_max_entries: int = 200
    # Bulk-path conflict checks run at once (at least 1).
    bulk_conflict_check_concurrency: int = Field(default=8, ge=1)
    # Facts per batched candidate lookup (conflict and value-update peers) on
    # the write path; matches the per-fact concurrency so wall time stays flat.
    # 0 = one statement per fact.
    memory_write_candidate_batch: int = 6

    # Cosine ≥ this with a canonical-form mismatch marks a write 'duplicate'
    # with a duplicate_of pointer instead of 'conflicted'. Per-namespace
    # override: loom.namespaces.duplicate_threshold.
    duplicate_threshold: float = 0.97

    # Blacklist cache invalidation: in-process invalidate_cache(), a one-row
    # CH version watermark (loom.blacklist_version) polled at most every check
    # interval (bounds cross-replica lag), and a hard TTL backstop. No Redis.
    blacklist_version_check_interval_seconds: float = 2.0
    # Seconds after which the blacklist cache reloads regardless of the version watermark.
    blacklist_cache_ttl_seconds: int = 300

    # ---- workers ----
    # Periodically re-run the embedder calibration probe so thresholds follow a
    # silently revised embedder. Fake providers skip.
    recalibration_enabled: bool = True
    # Seconds between embedder calibration probes.
    recalibration_interval_seconds: int = 7 * 24 * 3600

    # Seconds between the entity extractor's mirror divergence heal, a
    # FINAL-to-FINAL join of loom.memory and loom.memory_embeddings (343 GB/day
    # if run every pass). Divergence arrives a few rows a day.
    entity_heal_interval_seconds: int = 900

    # Retry best-effort projections (chunks, evidence links, graph side tables)
    # that failed after the primary write. Bounded and idempotent; unsupported
    # ones stay in ops.projection_failures.list.
    projection_repair_enabled: bool = True
    # Seconds between projection repair passes.
    projection_repair_interval_seconds: int = 300
    # Recorded projection failures one pass retries (at least 1).
    projection_repair_max_failures_per_pass: int = Field(default=25, ge=1)

    # Fold loom.memory_usage_totals onto the ranking columns (reinforcement
    # boost, memory.list sort_by=score, auto-promote gate); off, they freeze at
    # write-time values. Cadence is fixed in server/memory/workers/usage_fold.py.
    usage_fold_enabled: bool = True

    # Re-emit, with a tombstone, vector rows still live after their loom.memory
    # row was tombstoned or erased, so search stops offering them. Bounded per
    # pass; loom.integrity.orphan_vectors should read 0 between passes.
    orphan_vector_reconcile_enabled: bool = True
    # Seconds between orphan vector reconcile passes.
    orphan_vector_reconcile_interval_seconds: int = 600
    # Orphaned keys one pass re-emits tombstones for (at least 1).
    orphan_vector_reconcile_max_keys_per_pass: int = Field(default=500, ge=1)
    # Seconds before an unmatched embedding counts as orphaned, so a write whose
    # two rows land apart is not taken for a deletion.
    orphan_vector_reconcile_grace_seconds: int = 600

    # ---- procedures ----
    # `procedure.evolve` quarantines a procedure whose trace log ends in this
    # many consecutive failed runs (out of procedure.match and the pack until a
    # run succeeds). 0 = off.
    procedure_strike_limit: int = 3

    # ---- consolidation ----
    # Clusters merged concurrently by memory.consolidate; 1 = sequential.
    consolidation_merge_concurrency: int = 4

    # Fold a user's durable preference/decision memories into a versioned
    # profile section (memory_type='profile') for profile.render /
    # context.pack; skipped when refreshed within the cooldown. LLM calls;
    # namespace opt-in via consolidation_enabled.
    profile_consolidation_enabled: bool = False
    # Seconds between profile fold passes.
    profile_consolidation_interval_seconds: int = 6 * 3600
    # Users (org, namespace, user_id) one pass folds, most preference/decision rows first.
    profile_consolidation_max_identities_per_pass: int = 10
    # Live preference/decision memories a user needs before a profile is folded.
    profile_consolidation_min_memories: int = 5
    # Memories read into one fold, highest confidence then newest first.
    profile_consolidation_memory_limit: int = 100
    # Seconds after a profile section is written before it is folded again.
    profile_consolidation_cooldown_seconds: int = 24 * 3600
    # `memory_key` of the folded profile row.
    profile_consolidation_label: str = "consolidated_profile"

    # Reflection distillation: label recent sessions that carry no outcome
    # (positive / negative / unclear into loom.session_outcomes_by_user), pair each
    # confidently failed session with its nearest successful one and distil
    # the difference into one hidden `reflection` memory. LLM calls; sessions
    # labelled through run.complete are not re-judged.
    reflection_distill_enabled: bool = False
    # Seconds between reflection distillation passes.
    reflection_distill_interval_seconds: int = 3600
    # Days back a pass looks for unlabelled sessions and for failed sessions to contrast.
    reflection_distill_lookback_days: int = 14
    # Sessions one pass sends to the outcome labeller.
    reflection_distill_max_judged_per_pass: int = 40
    # Failed sessions one pass tries to pair and distil.
    reflection_distill_max_contrasts_per_pass: int = 10
    # A judged label below this confidence neither pairs nor is paired with.
    reflection_distill_min_confidence: float = 0.7
    # Cosine floor between the failed and the successful summary; below it the
    # sessions were not about the same thing.
    reflection_distill_min_pair_similarity: float = 0.55

    # Async LLM entity tagging of memory rows (fills `entities` for search's
    # entity match); off the memory.set hot path.
    entity_extract_enabled: bool = True
    # Seconds between entity extraction passes that found work.
    entity_extract_interval_seconds: int = 60
    # Sleep after a pass that found no unentitied row.
    entity_extract_idle_interval_seconds: int = 300
    # Each pass scans only memories of the last recent_seconds; the full
    # candidate scan (without the value column) runs once per
    # full_scan_seconds. full_scan_seconds 0 = full scan every pass.
    entity_extract_full_scan_seconds: int = 1800
    # Window, seconds, of recent memories each pass scans for untagged rows.
    entity_extract_recent_seconds: int = 7200
    # Keys the full scan queues for the passes in between (refilled early when
    # the queue runs dry).
    entity_extract_full_scan_keys: int = 5000
    # Backlog keys per namespace in one refill, oldest first, round-robin
    # across namespaces so one cannot starve the rest. 0 = org-wide newest
    # first.
    entity_extract_backlog_per_namespace: int = 500

    # ---- dedup and expiry ----
    # Bulk-path exact dedup. "session" = a byte-identical value is skipped only
    # under the same key in the same session. "identity" = a live row anywhere
    # in (org, namespace, agent, user_id) with the same content_sha256 is the
    # fact; the entry becomes an `evidence` reinforcement of it, named in
    # `identity_duplicates`.
    memory_bulk_dedup_scope: Literal["session", "identity"] = "identity"

    # Apply each expired row's `on_expire` (tombstone | demote | anonymize) as a
    # version row. Reads already hide expired rows; the sweep records it.
    memory_expiry_sweep_enabled: bool = True
    # Seconds between expiry sweep passes.
    memory_expiry_sweep_interval_seconds: int = 300
    # Expired rows one sweep pass applies.
    memory_expiry_sweep_batch_size: int = 500

    # `demote` multiplies confidence by this factor, never below the floor.
    memory_expiry_demote_factor: float = 0.5
    # Lowest confidence a `demote` (expiry or run.complete) leaves on a row.
    memory_expiry_demote_floor: float = 0.05

    # Finish the checks memory.delete_version could not run after its tombstone (a failed
    # read), so a write that landed between its version check and its tombstone is
    # written back above it rather than left deleted.
    delete_version_check_enabled: bool = True
    # Seconds between passes while checks are owed.
    delete_version_check_interval_seconds: int = 60
    # Seconds between passes that found none.
    delete_version_check_idle_interval_seconds: int = 300
    # Seconds a check is left to the delete that owes it before a pass runs it.
    delete_version_check_grace_seconds: int = 60
    # Checks one pass runs.
    delete_version_check_batch_size: int = 200

    # ---- namespace health, knowledge graph, communities ----
    # Seconds a namespace's health summary is cached per process (a failed
    # lookup at most 300 s). 0 disables the summary and its pack section.
    namespace_health_ttl_seconds: int = 3600
    # Label-propagation communities over the entity co-occurrence graph, per
    # namespace, into loom.entity_communities (read by namespace.taxonomy and
    # graph-mode expansion). A CPU pass over every namespace's edges.
    entity_communities_enabled: bool = False
    # Seconds between community partition passes.
    entity_communities_interval_seconds: int = 6 * 3600
    # Entity pairs read per namespace, strongest first; past it the rest are dropped and
    # the namespace is reported truncated.
    entity_communities_max_edges: int = 200_000
    # A pass covers the namespaces with consolidation_enabled plus those whose
    # graph was walked (facts.traverse / related / path, namespace.taxonomy)
    # within this many days; 0 = the opted-in set only.
    entity_communities_walked_days: int = 14
    # Leave the top fraction of entities by degree out of the partition, so
    # generic tokens cannot fuse the graph into one community. A graph under 500
    # entities drops none.
    # 0 = keep every entity.
    entity_communities_hub_drop_fraction: float = 0.002
    # When one community holds more than this share of a namespace's entities,
    # double hub_drop_fraction and re-partition, up to hub_drop_max_fraction
    # (a code corpus parted at 0.016, 2.9%). 0 = fixed fraction.
    entity_communities_giant_share: float = 0.5
    # Four doublings from the default; a graph still fused here is written as
    # is and counted in the pass outcome.
    entity_communities_hub_drop_max_fraction: float = 0.032
    # Co-occurrence count an entity pair needs to count as an edge.
    entity_communities_min_co_occur: int = 1
    # ---- bulk contextualization ----
    # memory.bulk_set honours contextualize=true (embed a chunk with its
    # context) only when allowed; concurrency bounds the per-entry model calls,
    # max_entries caps one call's spend.
    memory_bulk_contextualize_allowed: bool = True
    # Context model calls one bulk_set runs at once.
    memory_bulk_contextualize_concurrency: int = Field(default=8, ge=1)
    # A bulk_set with more pending entries than this is not contextualized at all.
    memory_bulk_contextualize_max_entries: int = Field(default=200, ge=1)
    # ---- graph walks (facts.traverse) ----
    # Relative hub rule: hold a reached node when the edge that reached it is at
    # most this share of the node's degree (via_co_occur / degree). Scale-free.
    # 0 = off; a request's `hub_share` overrides it.
    facts_traverse_hub_share: float = 0.01
    # What facts.traverse's per-hop trim ranks by: `co_occur` (most
    # co-mentioned pairs), `specificity` (via_co_occur / degree; request option
    # only, it fills hop 1 with singletons) or `cosine` (via_co_occur /
    # sqrt(degree)).
    facts_traverse_rank_by: Annotated[Literal["co_occur", "specificity", "cosine"], CaseFolded] = (
        "cosine"
    )

    # ---- sensitivity and provenance ----
    # With no explicit `sensitivity`, detect it from the value with regexes:
    # credential shapes -> 'high' (never embedded, key-only reads, audited);
    # email/phone -> 'medium' (embedded, labelled). Off = every row stays ''.
    memory_sensitivity_autodetect: bool = True
    # `medium`/`high` rows get expires_at = now + N days on the set and bulk
    # paths (a shorter caller expiry wins) and, if the caller set none, this
    # on_expire. 0 = off.
    memory_sensitive_retention_days: int = 0
    # `on_expire` given to a sensitive row whose caller set none.
    memory_sensitive_on_expire: Literal["tombstone", "demote", "anonymize"] = "anonymize"
    # Daily sweep: re-label live rows written `sensitivity = ''` by a path that
    # skipped the detector, as a new version per row; `high` rows' embeddings
    # are tombstoned, never ALTER UPDATE. Covers the trailing window; the
    # backlog is `loom sensitivity-sweep --apply`.
    sensitivity_sweep_enabled: bool = True
    # Seconds between sensitivity sweep passes.
    sensitivity_sweep_interval_seconds: int = 86400
    # Days back the sweep re-labels rows created in.
    sensitivity_sweep_window_days: int = 2
    # Rows one sweep pass re-labels (at least 1).
    sensitivity_sweep_max_rows_per_pass: int = Field(default=500, ge=1)
    # Shapes the sweep applies (comma-separated: credential, email, phone,
    # other). `phone` (a ten-digit run) mostly matches ids, and a `medium` label
    # demotes the row in ranking, so it is left to the operator's
    # `--only-shape phone`.
    sensitivity_sweep_shapes: str = "credential,email"
    # ---- code-linked memories ----
    # Link memories to the code they name (symbols and files of the repository
    # their `repo:` tag or code namespace names) and keep the links current as the
    # code changes; the namespace's `code_change_policy` decides how reads treat a
    # memory whose code is removed or changed. Off stops linking and rechecks; reads
    # still show the stored states but no longer hide a memory, as nothing keeps them
    # current.
    memory_code_links_enabled: bool = True
    # Seconds between link passes.
    memory_code_links_interval_seconds: int = 60
    # Memory writes one pass links (at least 1).
    memory_code_links_batch: int = Field(default=500, ge=1)
    # Seconds the pass stays behind the newest queued memory write, so inserts running at
    # the same moment are visible before the cursor passes them (at least 10). A write
    # visible later than this is passed and stays unlinked until a backfill reads it; a
    # longer wait makes that rarer and links every write that much later.
    memory_code_links_settle_seconds: int = Field(default=60, ge=10)
    # Days a code-shaped name that names nothing on trunk yet (unmerged code) is
    # retried before it is dropped.
    memory_code_links_pending_days: int = Field(default=14, ge=1)
    # Changed claims one pass re-checks with the LLM under the `revise` policy.
    memory_code_links_revise_per_pass: int = Field(default=20, ge=0)
    # Tag rows mentioning a protected attribute or common proxy with
    # `attribute:<class>` at write time (gender, religion, health, age, family,
    # income, ethnicity, sexuality, politics). A label, never a gate; the
    # profile fold leaves such rows out.
    memory_attribute_tagging: bool = True

    # ---- memory chunks ----
    # Max ~800-char verbatim chunks one set_from_messages call writes (512 ≈
    # 400K chars; agent transcripts run ~190K chars a session at the median).
    # Text past the cap is recorded in loom.memory_drops as
    # chunk.split/truncated.
    memory_chunks_max_per_call: int = 512
    # Max chunks per memory.set value (48 ≈ 34 KB); the tail is recorded in
    # loom.memory_drops as chunk.split/truncated.
    memory_set_chunk_max_chunks: int = 48
    # ---- fact-leg reads of memory_embeddings ----
    # Seconds a process caches its count of patch parts on
    # loom.memory_embeddings (server/core/vectors/patch_parts.py); the fact leg
    # reads quantized codes only while the count is zero.
    memory_embeddings_patch_parts_probe_seconds: int = 60
