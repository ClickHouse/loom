"""Which ops the dashboard leaves out, and why.

Every op is served over HTTP, is in both generated SDKs and is reachable over
MCP unless `toolsets._MCP_HIDDEN_OPS` keeps it for people and operators. The
dashboard is the people's surface: an op reaches it when a quoted `"/v1/<op>"`
call site sits in `frontend/src` (the `dashboard` reach kind,
`server.operations.reach`). An op a person needs (to administer, review,
inspect, configure, or to delete with a confirmation) has a control there; an op
only an agent or a client program calls, or one the dashboard serves through
another op, is named in `NOT_IN_DASHBOARD` with the reason. The census's
`--check` (`tests/unit/test_op_reach_census.py`, through `dashboard_violations`)
fails on an op with neither, and on an entry for an op the dashboard calls or
that no longer exists.
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


def _named_once(*groups: dict[str, str]) -> dict[str, str]:
    """The groups as one table; an op named in two is a mistake, refused here."""
    table: dict[str, str] = {}
    for group in groups:
        for name, reason in group.items():
            if name in table:
                raise ValueError(f"{name} is named twice in NOT_IN_DASHBOARD")
            table[name] = reason
    return table


NOT_IN_DASHBOARD: dict[str, str] = _named_once(
    # ---- code questions
    dict.fromkeys(
        (
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
        ),
        CODE_QUESTION,
    ),
    # ---- tracker questions
    dict.fromkeys(
        (
            "tracker.component",
            "tracker.duplicate_check",
            "tracker.flaky",
            "tracker.links",
            "tracker.release",
            "tracker.similar",
            "tracker.test_signal",
        ),
        TRACKER_QUESTION,
    ),
    # ---- an agent's working steps
    dict.fromkeys(
        (
            "context.fetch",
            "context.offload",
            "context.pack",
            "conversation.append",
            "conversation.compact",
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
        ),
        AGENT_STEP,
    ),
    # ---- client programs
    dict.fromkeys(
        (
            "code.calibrate",
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
            "observe.crystallize",
            "observe.record",
            "ops.capabilities",
        ),
        CLIENT_CALL,
    ),
    # ---- a chat client's own feature
    dict.fromkeys(
        (
            "conversation.list_saved_messages",
            "conversation.save_message",
            "conversation.saved_status",
            "conversation.search_saved_messages",
            "conversation.unsave_message",
        ),
        "a chat client calls it for the message its user picks, and the user finds saved "
        "messages there",
    ),
    # ---- the dashboard does this through another op
    {
        "grants.share_agent": (
            "an agent shares its own namespace and the handler refuses any other caller; "
            "a person shares a namespace from the Sharing page with grants.create"
        ),
        "grants.unshare_agent": (
            "an agent withdraws its own share and the handler refuses any other caller; "
            "a person revokes a grant from the Sharing page with grants.revoke"
        ),
        "memory.import": (
            "run by the program that exports from another memory system; the dashboard imports "
            "files with memory.import_curated"
        ),
        "memory.apply": (
            "the Memories page's bulk actions call the per-row ops (memory.pin, memory.delete)"
        ),
        "memory.cross_promote": (
            "the memory drawer's Copy to… (memory.copy_to) makes the same copy with its lineage "
            "link; this by-id form needs admin on both namespaces"
        ),
        "memory.search_trace": "Recall Lab explains each ranking with memory.search explain=true",
        "memory.touch": (
            "an agent's keep-alive on memories it relies on; the memory drawer changes an "
            "expiry with memory.set, which audits the change"
        ),
        "memory.update": "the memory drawer saves an edit with memory.set",
        "memory.update_by_id": "the memory drawer saves an edit with memory.set",
        "namespace.list": (
            "the namespace picker lists namespace.list_mine and finds others with namespace.browse"
        ),
        "namespace.summary": (
            "an agent's one-call orientation on a namespace; the Assistant page's overview reads "
            "namespace.inventory"
        ),
        "procedure.list": (
            "memory.list under the procedure_ key prefix; the Memories page lists the same rows"
        ),
        "search.query": (
            "each page searches its own plane: Memories memory.search, the Library "
            "knowledge.search, Conversations conversation.search"
        ),
    },
)
