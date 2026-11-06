"""Which ops the dashboard leaves out, and why.

Every op is served over HTTP, is in both generated SDKs and is reachable over
MCP unless `toolsets._MCP_HIDDEN_OPS` keeps it for people and operators. The
dashboard is the people's surface: an op reaches it when a quoted
`"/v1/<op>"` call site sits in `frontend/src` (the `dashboard` reach kind,
`server.operations.reach`). An op a person needs (to administer, review,
inspect, configure, or to delete with a confirmation) has a control there; an
op only an agent or a client program calls is named in `NOT_IN_DASHBOARD` with
the reason. `tests/unit/test_op_surfaces.py` fails on an op with neither, and
on an entry for an op the dashboard calls.

`DASHBOARD_PENDING` holds the ops a person needs whose control is not built
yet. It only shrinks (`test_op_surfaces.py` pins it): an op leaves it when its
control lands, and a new op ships with its control instead.
"""

from __future__ import annotations

AGENT_STEP = (
    "an agent's own step inside its task; a person reaches it through the agent, "
    "and the dashboard shows what the step left behind"
)
CODE_QUESTION = (
    "a question a coding agent asks about the change it is making; the Code page answers a "
    "person's browsing questions (code.search, code.symbol, code.callers, code.map) and "
    "manages repositories"
)
TRACKER_QUESTION = (
    "a question an agent asks of the tracker record while it works; the Trackers page "
    "answers a person's questions (tracker.answer, tracker.item) and manages the sources"
)
CLIENT_CALL = (
    "called by a client program (a plugin hook, a bot, a CI job, an import or sync script), "
    "not by a person"
)

NOT_IN_DASHBOARD: dict[str, str] = {
    # ---- code questions
    **{
        name: CODE_QUESTION
        for name in (
            "code.ask",
            "code.backport_gap",
            "code.backport_gaps",
            "code.bisect_candidates",
            "code.blame_range",
            "code.conventions",
            "code.coverage_claim",
            "code.enclosing",
            "code.examples",
            "code.files",
            "code.first_build",
            "code.grep",
            "code.history",
            "code.impact",
            "code.outline",
            "code.path_guards",
            "code.pr_check",
            "code.review_brief",
            "code.setting_facts",
            "code.symbol_history",
            "code.test_gate",
            "code.test_home",
            "code.tests_for",
            "code.trace",
            "code.verify",
            "code.verify_citations",
        )
    },
    # ---- tracker questions
    **{
        name: TRACKER_QUESTION
        for name in (
            "tracker.component",
            "tracker.duplicate_check",
            "tracker.flaky",
            "tracker.links",
            "tracker.release",
            "tracker.similar",
            "tracker.test_signal",
        )
    },
    # ---- an agent's working steps
    **{
        name: AGENT_STEP
        for name in (
            "context.fetch",
            "context.offload",
            "context.pack",
            "conversation.append",
            "conversation.compact",
            "conversation.create",
            "decision.chain",
            "decision.outcome",
            "decision.precedents",
            "decision.record",
            "knowledge.outline",
            "knowledge.query",
            "knowledge.synthesize",
            "memory.aggregate",
            "memory.ask",
            "memory.assign_category",
            "memory.brief",
            "memory.demonstrate",
            "memory.digests",
            "memory.extraction_retry_mine",
            "memory.extraction_status_mine",
            "memory.for_file",
            "memory.link",
            "memory.outcome_run",
            "memory.outline",
            "memory.recall",
            "memory.record.write",
            "memory.set_from_messages",
            "memory.stack",
            "memory.timeline",
            "memory_schema.propose",
            "mentor.guide",
            "namespace.search_categories",
            "namespace.validate",
            "procedure.add",
            "procedure.evolve",
            "procedure.fork",
            "procedure.locate",
            "procedure.match",
            "procedure.record_run",
            "procedure.revise",
            "profile.append",
            "profile.render",
            "profile.replace",
            "profile.structured",
            "run.begin",
            "run.complete",
            "session.brief_save",
            "task.heartbeat",
            "task.progress",
            "task.pull",
            "task.updates",
            "working_memory.append",
            "working_memory.delete",
            "working_memory.get",
            "working_memory.render",
            "working_memory.replace",
            "working_memory.set",
        )
    },
    # ---- client programs
    **{
        name: CLIENT_CALL
        for name in (
            "code.coverage.unwatch",
            "code.coverage.watch",
            "code.outcome",
            "code.scip_ingest",
            "code.scip_status",
            "cost.record_turns",
            "knowledge.bulk_add",
            "memory.bulk_set",
            "memory.delete_version",
            "memory.export_evidence",
            "memory.import_evidence",
            "memory.keys",
            "memory.outcome",
            "observe.crystallize",
            "observe.record",
            "ops.capabilities",
        )
    },
    # ---- a chat client's own feature
    **{
        name: (
            "a chat client calls it for the message its user picks, and the user finds saved "
            "messages there"
        )
        for name in (
            "conversation.list_saved_messages",
            "conversation.save_message",
            "conversation.saved_status",
            "conversation.search_saved_messages",
            "conversation.unsave_message",
        )
    },
    # ---- the dashboard does this through another op
    "memory.import": (
        "run by the program that exports from another memory system; the dashboard imports "
        "files with memory.import_curated"
    ),
    "memory.apply": (
        "the Memories page's bulk actions call the per-row ops (memory.pin, memory.delete)"
    ),
    "memory.cross_promote": (
        "an operator's copy by memory id from a script; the memory drawer copies with "
        "memory.copy_to"
    ),
    "memory.search_trace": "the Recall lab explains each ranking with memory.search explain=true",
    "memory.update": "the memory drawer saves an edit with memory.set",
    "memory.update_by_id": "the memory drawer saves an edit with memory.set",
    "namespace.list": (
        "the namespace picker lists namespace.list_mine and finds others with namespace.browse"
    ),
    "namespace.summary": (
        "an agent's one-call orientation on a namespace; the Assistant page's overview reads "
        "namespace.inventory"
    ),
    "search.query": (
        "each page searches its own plane: Memories memory.search, the Library knowledge.search, "
        "Conversations conversation.search"
    ),
}

DASHBOARD_PENDING: frozenset[str] = frozenset(
    {
        # erasure, forgetting and exposure
        "erasure.export",
        "erasure.request",
        "erasure.status",
        "memory.clear",
        "memory.exposure_summary",
        "memory.owner_report",
        # the memory drawer
        "memory.backlinks",
        "memory.demote",
        "memory.evidence",
        "memory.lock",
        "memory.promote",
        "memory.redact",
        "memory.reject",
        "memory.touch",
        "memory.unlock",
        # review
        "conflicts.propose_revision",
        "knowledge.dedup_list",
        "knowledge.dedup_resolve",
        "memory.agents_list",
        "memory.consolidate",
        "memory.dedup_collapse",
        "memory.dedup_list",
        "memory.dedup_resolve",
        "memory.proposal_decide",
        "memory.proposals",
        "memory.resolve_orphan_duplicates",
        # settings
        "extraction_policy.clear",
        "extraction_policy.get",
        "extraction_policy.set",
        "org.model_calls",
        "org.set_model_calls",
        "webhook.deliveries",
        "webhook.list",
        "webhook.register",
        "webhook.remove",
        # health and maintenance
        "blacklist.sweep",
        "calibration.get",
        "code.calibrate",
        "code.reindex",
        "facts.expire_by_adapter",
        "memory.extraction_status",
        "memory.health",
        "memory.reindex",
        "memory.sweep_expired",
        "namespace.restore",
        "namespace.rollback",
        "namespace.snapshot",
        "ops.projection_failures.list",
        "ops.projection_failures.resolve",
        "ops.projection_failures.retry",
        # schemas, ontology and typed records
        "memory.record.conflicts",
        "memory.record.history",
        "memory.record.query",
        "memory_schema.approve",
        "memory_schema.list",
        "memory_schema.register",
        "namespace.list_categories",
        "namespace.list_ontology",
        "namespace.register_category",
        "namespace.register_ontology",
        "profile_schema.clear",
        "profile_schema.get",
        "profile_schema.set",
        # documents and sources
        "collection.move",
        "knowledge.dataset_approve",
        "knowledge.dataset_get",
        "knowledge.ingest_cancel",
        "knowledge.source.inspect",
        # pages of their own
        "conversation.delete",
        "conversation.update",
        "cost.agent_summary",
        "grants.share_agent",
        "grants.unshare_agent",
        "namespace.guide_history",
        "namespace.mark_seen",
        "procedure.list",
        "run.grade",
        "session.brief_delete",
    }
)
