"""The MCP toolsets: which registry ops each listing carries.

A registry fact, not transport code: the MCP transport lists and hides by
these rosters, and the op catalogue (`ops.capabilities`, the reach census),
the mentor and the installer read them to say where an op is reachable. A
roster names op names only; the transport owns how a listing is rendered.
"""

from __future__ import annotations

import re
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from server.operations.base import Operation

# Ops hidden from MCP entirely (both tools/list AND tools/call). They stay
# callable via /v1/* HTTP and the SDKs; the tools/call block is the
# `is not available` path in _call_tool. Use sparingly — every entry is a
# divergence between the registry and what clients see. Decision rule:
# would a typical end user (not an operator) reach for this in chat?
_MCP_HIDDEN_OPS: frozenset[str] = frozenset(
    {
        # Org-wide list grows unbounded (162 KB at 245 namespaces) and the
        # LLM sometimes picks it over list_mine. list_mine is always correct
        # for chat; namespace.list stays on /v1/* + SDK for operators.
        "namespace.list",
        # The dashboard's own paint telemetry — one row per page view,
        # posted by the dashboard shell; nothing an agent calls.
        "ui.vitals",
        # Dashboard-only paging over precomputed PR packs; an agent wants
        # ONE pack (code.context_pack), never the catalogue.
        "code.context_packs",
        # The repository card's "opened by agents" counts — a dashboard view
        # over the event ledger, nothing an agent asks.
        "knowledge.reads",
        # ---- Operator / admin surface ----
        # Memory housekeeping.
        "memory.dedup_list",
        "memory.dedup_resolve",
        "memory.dedup_collapse",
        "memory.outcome",
        "memory.touch",
        # Admin-only lifecycle ops: loosening a lock and forcing an expiry
        # pass are operator actions, not agent ones.
        "memory.unlock",
        "memory.sweep_expired",
        "memory.search_trace",
        "memory.bulk_set",
        "memory.bulk_get",
        # A sync job's reads and deletes: one answer of every key in the namespace, and a
        # delete of one exact version (import-okf --prune).
        "memory.keys",
        "memory.delete_version",
        # Deferred-batch lifecycle across the namespace (every agent's and user's
        # batches, up to 500 a call) is an SDK/dashboard view. An agent polls the
        # batches it queued with memory.extraction_status_mine, and sees
        # `pending_extraction` on search/ask.
        "memory.extraction_status",
        # Run ledger paging is a dashboard/SDK view; an agent wants ONE run
        # (run.get), never the catalogue.
        "run.list",
        # Learnings / curation triage queue.
        "learnings.list",
        "learnings.review",
        # Conflict review queue (conversational conflict already surfaces in
        # search results).
        "conflicts.list",
        "conflicts.propose_revision",
        "conflicts.resolve",
        "conflicts.allowlist_list",
        "conflicts.allowlist_remove",
        # Privacy filter config. blacklist.add stays visible (the in-band
        # Refuse verb — a mid-conversation consent request must be honorable
        # without sending the user to the dashboard).
        "blacklist.list",
        # History policy: rules are an operator surface; the dry run
        # `policy.check` stays callable so an agent can ask first.
        "policy.set",
        "policy.delete",
        "policy.decisions",
        "blacklist.remove",
        # Reaches rows written before a pattern; an operator or the add's own
        # `sweep=true`, never a chat-time tool.
        "blacklist.sweep",
        # Namespace backup/restore — operator-only; exposing tempts mis-fire.
        "namespace.snapshot",
        "namespace.restore",
        # Erasure status polling (request + export stay visible — users ask).
        "erasure.status",
        # Audit + cost dashboards.
        "audit.list",
        "cost.summary",
        "ops.health",
        # Individual search collapsed behind loom_search — one parallel
        # round-trip across all stores. Both surfaces visible caused agent
        # indecision. SDK + HTTP keep the per-store endpoints reachable.
        "memory.search",
        "knowledge.search",
        "conversation.search",
        # ---- Code plane ----
        # Feedback telemetry (plugin hooks/bots call it; chat never should).
        "code.outcome",
        # Ingest + admin surface: CI/operator only.
        "code.refresh",
        # Coverage watches: a consumer's bot subscribes its inbox.
        "code.coverage.watch",
        "code.coverage.unwatch",
        "code.scip_ingest",
        "code.scip_status",
        "code.reindex",
        "code.calibrate",
    }
)


# Code-intelligence tools joining the core listing when the deployment
# enables them (LOOM_MCP_CODE_TOOLS=true). callees/impact/examples/history/map
# stay callable by name so the taught arbitration in the tool descriptions
# can reference them without spending listing budget.
_MCP_CODE_CORE_OPS: frozenset[str] = frozenset(
    {
        "code.search",
        "code.grep",
        "code.enclosing",
        "code.ask",
        "code.symbol",
        # The read shunt's redirect target ("what is in this file?"): an op
        # only the plugin can name is dark to every other agent, so it
        # spends one listing slot.
        "code.outline",
        "code.callers",
        "code.context_pack",
    }
)


# The "core" MCP toolset (settings.mcp_toolset == "core", the default): the
# focused chat surface visible in tools/list. Everything else stays callable
# by name over MCP and via /v1/* + SDKs — hidden only to keep the tool budget
# under the count where selection accuracy degrades. Synthetic tools are
# always shown. LOOM_MCP_TOOLSET=all exposes the full surface.
_MCP_CORE_OPS: frozenset[str] = frozenset(
    {
        # Save + recall + manage memory — the core loop.
        "memory.set",
        # The standing-memory verb: a pinned fact is never evicted by a later
        # conflicting write. Its description names memory.set as the default
        # save and pin as the exception, or a model routes every 'remember X'
        # to it.
        "memory.pin",
        "memory.set_from_messages",
        "memory.update",
        "memory.update_by_id",
        "memory.get",
        # memory.get_by_id: conflict-resolution flow — callable by name;
        # the chat loop reaches memories via search/list/get.
        "memory.list",
        "memory.related",
        "memory.history",
        "memory.delete",
        # The two write shapes an agent reaches for after set/delete: a wide,
        # previewed write over a scope and a per-row lock.
        "memory.apply",
        "memory.lock",
        # Knowledge documents.
        "knowledge.add",
        "knowledge.get",
        "knowledge.list",
        "knowledge.delete",
        # Profile — the always-loaded persona/context. render stays
        # callable-by-name: context.pack already loads the rendered profile,
        # so the chat loop writes (set/append) but never re-renders.
        "profile.set",
        "profile.append",
        # Conversation history — the agent opens a thread once per session
        # and logs each turn, so the transcript shows in the dashboard. MCP is
        # pull-based (the server
        # only sees what the model calls), so these must be visible for any
        # transcript to land at all; recall of past threads stays folded
        # into loom_search.
        "conversation.create",
        "conversation.append",
        # Working memory — scratchpad included by context.pack.
        "working_memory.set",
        "working_memory.append",
        "working_memory.replace",
        "working_memory.delete",
        # Self-service namespaces: orientation stays listed; the
        # join/leave/browse membership ops are onboarding-time actions,
        # callable by name when an agent is told to use them. Every listing
        # slot is a per-turn tax on every session — the roster carries the
        # chat loop, not the whole product surface.
        "namespace.list_mine",
        "namespace.summary",
        # The end-of-task integrity call: did my writes leave the namespace
        # consistent? Read-only, returns counts + samples per check.
        "namespace.validate",
        # Data rights.
        "erasure.request",
        "erasure.export",
        # Refuse — the in-band "never remember X" consent verb.
        "blacklist.add",
        # One-call task-context loader.
        "context.pack",
    }
)


# The "coding" MCP toolset: the coding-agent archetype's listing. A coding
# client saves, revises, reads, deletes memory, keeps a scratchpad, loads task
# context once, and reaches the code plane; it does not open conversation
# threads, edit a profile, manage knowledge documents or namespaces, or file
# erasures. The listing is the memory-side ops below plus _MCP_CODE_CORE_OPS
# (the code tools ride the coding tier regardless of LOOM_MCP_CODE_TOOLS —
# they are its point) plus the two synthetic tools. It carries what coding
# clients actually call; everything on `core` (memory.pin, blacklist.add,
# working_memory.append, …) stays callable by name, since the transport reads
# the registry, not the roster. memory.update stays: it is the revision verb
# the descriptions and the planning bench's revision tasks name.
_MCP_CODING_OPS: frozenset[str] = frozenset(
    {
        "memory.set",
        "memory.update",
        "memory.get",
        "memory.delete",
        "working_memory.set",
        "context.pack",
        # "Is this already reported?" is a coding session's question — the
        # calibrated answer is listed where the session works, not behind a
        # second MCP entry.
        "tracker.duplicate_check",
        # So are "what introduced this crash", "is someone already doing this
        # PR" and "what does my diff break" (~1.5k listing tokens); the rest
        # of the tracker plane stays on `tracker`.
        "code.bisect_candidates",
        "code.pr_check",
        "code.impact",
    }
)


# The "tasks" toolset (wake-up layer): the task inbox plane
# for a session acting as an agent — pull and claim, post for another agent,
# finish / fail / decline / defer what it claimed, renew the lease, watch
# what it posted, read the board, find the inbox. Its own roster rather than
# a core addition: the default core listing is paid by every Claude Code
# session and its tool count sits under the tool-selection knee; a session
# that acts as an agent mounts `?toolset=tasks` (or is told to). The ops stay
# experimental until the ledger shows production callers.
_MCP_TASK_OPS: frozenset[str] = frozenset(
    {
        "task.pull",
        "task.create",
        "task.complete",
        "task.fail",
        "task.wont_do",
        "task.defer",
        "task.heartbeat",
        "task.updates",
        "task.list",
        "task.get",
        "inbox.list",
    }
)


# The "tracker" toolset: tracker record, bisect, PR duplicate
# check and blast radius for a session that works a failing test, a crash or
# a pull request. Its own roster, not a core addition: the coding listing is
# paid by every coding session, and a session that wants these mounts a
# second MCP entry on `?toolset=tracker`. Read-only on the tracker record
# (the ops read the mirrored tables with the namespace's credential).
_MCP_TRACKER_OPS: frozenset[str] = frozenset(
    {
        "tracker.answer",
        "tracker.binding",
        "tracker.component",
        "tracker.duplicate_check",
        "tracker.flaky",
        "tracker.item",
        "tracker.links",
        "tracker.release",
        "tracker.similar",
        "tracker.test_signal",
        "code.bisect_candidates",
        "code.pr_check",
        "code.impact",
    }
)


# The atomic surface: four memory verbs (create, read, update, delete) plus
# the run scratchpad and the run ledger. Create and update are one tool —
# memory.set under a key a read returned IS the update — and read is
# loom_search (the synthetic search tool, always listed) plus memory.get. The
# ops below plus the two synthetic tools; a small verb set plans better than
# the core surface.
_MCP_ATOMIC_OPS: frozenset[str] = frozenset(
    {
        "memory.set",
        "memory.get",
        "memory.delete",
        "memory.ask",
        "working_memory.set",
        "working_memory.append",
        "run.begin",
        "run.complete",
    }
)


# Every toolset name `settings.mcp_toolset` and the per-request `?toolset=`
# override accept. An unknown value is the configured default (the same rule
# `?schema_profile=` follows — a bad value never widens or narrows silently).
MCP_TOOLSETS: tuple[str, ...] = (
    "core",
    "coding",
    "atomic",
    "tasks",
    "tracker",
    "readonly",
    "assistant",
    "all",
)
# `readonly` is a projection, not a curated roster: it lists the
# `all` surface minus every write / higher-than-viewer tool, computed in
# `_tools_descriptor` from each op's role. It is deliberately NOT in
# CURATED_TOOLSETS and NOT a value of the `op_toolsets` catalogue facet — it is
# a URL-selected view for a read-only registration, not a membership tier.
# Ops MCP serves through a hand-written synthetic descriptor listed on every
# toolset instead of the registry-derived one: the transport's listing skips
# their registry entry (the synthetic descriptor carries the tool name, text
# and schema clients already hold). The curated rosters above name only
# registry-listed ops; `op_toolsets` counts these on every listing.
SYNTHETIC_TOOL_OPS: frozenset[str] = frozenset({"search.query"})

# `assistant` is the connection a chat client investigates Loom through on a person's
# behalf: a small fixed set of reads, and nothing else is callable on it (`loom_op` and
# `loom_search` included), whatever the token would allow. It is not a curated roster:
# it never joins a `+` union, since a union would add another roster's writes. It carries
# `memory.search` and `knowledge.search`, which the other listings hide in favour of
# `loom_search`: an investigation needs one plane's results at a time. On it every call
# names consumer `loom-assistant` (a token whose `consumer` claim names another keeps its
# claim), so usage reports leave the assistant's own reads out, and the reads that record
# usage as a side effect are told not to (`ASSISTANT_FORCED_ARGS`).
ASSISTANT_TOOLSET = "assistant"
MCP_ASSISTANT_OPS: frozenset[str] = frozenset(
    {
        # Inspect: what the namespace holds and how it is used.
        "namespace.get",
        "namespace.summary",
        "namespace.inventory",
        "memory.inspect",
        "knowledge.inspect",
        "namespace.tune",
        "namespace.taxonomy",
        "namespace.audit",
        "namespace.validate",
        "namespace.guide_get",
        "namespace.guide_history",
        "namespace.list_categories",
        "mentor.brief",
        # Loom's own guide to writing memory, which recommendations cite.
        "mentor.guide",
        "grants.list",
        # Cited questions over memory, documents and code.
        "memory.search",
        "memory.ask",
        "memory.get",
        "memory.list",
        "memory.history",
        "memory.provenance",
        "memory.aggregate",
        "facts.traverse",
        "facts.path",
        "knowledge.search",
        "knowledge.get",
        "knowledge.list",
        "code.search",
        "code.symbol",
        # Read-only SQL over the analysis allowlist, held to the namespace by ClickHouse.
        "insights.catalog",
        "insights.query",
    }
)
# Per op, the arguments an assistant call always carries, whatever the model sent: a
# search records no recall usage and an ask feeds no reinforcement, so inspecting a
# namespace does not change what it ranks. An assistant call's result is the op's full
# output, as HTTP returns it; a search leaves out its per-hit score breakdown.
ASSISTANT_FORCED_ARGS: dict[str, dict[str, object]] = {
    "memory.search": {"auto_touch": False, "explain": False},
    # `precedents: false` also keeps an answer from being stored for reuse.
    "memory.ask": {"reinforce": False, "precedents": False},
}


# Arguments no assistant call keeps: a call's own `run_id` would file its ledger and
# exposure rows under a run of the model's choosing, not the answer's (`X-Loom-Run-Id`),
# and the answer's citations are checked by the answer's run. On memory.search it is
# also a filter (rows written under one run): the assistant gives that recall up, since
# an answer is about a namespace, not about one agent's run.
ASSISTANT_DROPPED_ARGS: frozenset[str] = frozenset({"run_id"})


# The assistant reads of one writer's rows: they match the writer exactly, so a call that
# names no `agent` is refused, and the listing marks it required.
ASSISTANT_NAMED_AGENT_OPS: frozenset[str] = frozenset({"memory.aggregate", "memory.provenance"})


def is_assistant(toolset: str | None) -> bool:
    """Whether a connection's ``?toolset=`` is the assistant toolset."""
    return toolset_parts(toolset) == (ASSISTANT_TOOLSET,)


# How an assistant answer's tool calls bear on its label (server/assistant/domain/support.py):
# a call to a CITABLE op returns rows the answer can cite and the citation check verifies; an
# UNCITABLE op's output (counts, findings, history) has no citation form; a NO_NAMESPACE_DATA
# op returns Loom's own guide or catalog, never the namespace's data. Every assistant op is in
# exactly one (tests/unit/test_assistant_support.py).
ASSISTANT_CITABLE_OPS: frozenset[str] = frozenset(
    {
        "memory.search",
        "memory.ask",
        "memory.get",
        "memory.list",
        "knowledge.search",
        "knowledge.get",
        "knowledge.list",
        "code.search",
        "code.symbol",
        "insights.query",
    }
)
ASSISTANT_NO_NAMESPACE_DATA_OPS: frozenset[str] = frozenset({"mentor.guide", "insights.catalog"})
ASSISTANT_UNCITABLE_OPS: frozenset[str] = (
    MCP_ASSISTANT_OPS - ASSISTANT_CITABLE_OPS - ASSISTANT_NO_NAMESPACE_DATA_OPS
)

# Whether an assistant op's call found anything, as its ledger row records it (event_args):
# the output lists whose summed length is `out_hits`; an op whose miss is `found: false`
# (`out_found`); or an op that always returns data, with why.
ASSISTANT_FOUND_FIELD: dict[str, tuple[str, ...]] = {
    "memory.search": ("results",),
    "memory.ask": ("results",),
    "memory.list": ("entries",),
    "memory.history": ("rows",),
    "memory.aggregate": ("rows",),
    "facts.traverse": ("nodes",),
    "knowledge.search": ("results",),
    "knowledge.list": ("rows",),
    "knowledge.inspect": ("listed",),
    "code.search": ("results",),
    # A fuzzy candidate is no find; a presence answer is.
    "code.symbol": ("definitions", "presence"),
    "insights.query": ("rows",),
    "grants.list": ("rows",),
    "namespace.audit": ("findings",),
    "namespace.validate": ("findings",),
    "namespace.tune": ("proposals",),
    "namespace.guide_history": ("revisions",),
    "namespace.list_categories": ("categories",),
    "namespace.taxonomy": ("tags",),
    "namespace.summary": ("type_counts",),
}
ASSISTANT_FOUND_BOOL_OPS: frozenset[str] = frozenset(
    {
        "memory.get",
        "memory.provenance",
        "knowledge.get",
        "namespace.get",
        "namespace.guide_get",
        "facts.path",
    }
)
ASSISTANT_ALWAYS_RETURNS: dict[str, str] = {
    "namespace.inventory": "one section per plane, its counts zero or not",
    "memory.inspect": "the subject's counts and settings, zero or not",
    "mentor.brief": "the namespace's usage counts and hints, zero or not",
    "mentor.guide": "Loom's own writing guide",
    "insights.catalog": "the tables an analysis may read",
}


def assistant_ops() -> list[Operation]:
    """The ops the assistant toolset lists, in registry order: its fixed set of reads,
    whatever their maturity or the code tools flag. The agent definition names these."""
    from server.operations.registry import ALL_OPERATIONS

    return [op for op in ALL_OPERATIONS if op.name in MCP_ASSISTANT_OPS and op.read_only]


# The tiers whose roster is a fixed frozenset (everything else is `all`).
CURATED_TOOLSETS: frozenset[str] = frozenset({"core", "coding", "atomic", "tasks", "tracker"})


# One MCP entry may list the union of curated rosters — `?toolset=coding+tracker`
# is the coding listing plus the tracker plane, paid for on purpose by the
# session that mounts it. Only curated rosters join (`all`,
# `readonly` and `assistant` are whole surfaces); an unknown part makes the whole value
# unknown, so a typo never widens a listing silently.
def toolset_parts(toolset: str | None) -> tuple[str, ...]:
    """The lower-cased, de-duplicated parts of a toolset value, in the order
    given (the first part drives tier-specific descriptions)."""
    out: list[str] = []
    # `+` is the documented separator; a query string decodes an unescaped `+`
    # to a space, and a comma is what a hand-typed URL tends to carry, so all
    # three split.
    for part in re.split(r"[+ ,]+", str(toolset or "").lower()):
        part = part.strip()
        if part and part not in out:
            out.append(part)
    return tuple(out)


def is_toolset(toolset: str | None) -> bool:
    """Whether a value names a listing: one of MCP_TOOLSETS, or a `+` union
    of two or more curated rosters."""
    parts = toolset_parts(toolset)
    if len(parts) == 1:
        return parts[0] in MCP_TOOLSETS
    return len(parts) > 1 and all(p in CURATED_TOOLSETS for p in parts)


def canonical_toolset(toolset: str | None) -> str:
    """The value as the listing names it: parts lower-cased, de-duplicated,
    joined with `+` (`Coding+Tracker+coding` → `coding+tracker`)."""
    return "+".join(toolset_parts(toolset))


def is_curated(toolset: str) -> bool:
    """Whether every part of the value is a curated roster (a fixed set)."""
    parts = toolset_parts(toolset)
    return bool(parts) and all(p in CURATED_TOOLSETS for p in parts)


def curated_roster(toolset: str, *, code_tools: bool) -> frozenset[str] | None:
    """The op names a curated toolset lists, or ``None`` for ``all``.

    ``core`` gains the code tools with the deployment flag; ``coding`` always
    carries them; ``atomic`` never does. A `+` union lists the union of its
    parts' rosters.
    """
    parts = toolset_parts(toolset)
    if len(parts) > 1:
        rosters = [curated_roster(p, code_tools=code_tools) for p in parts]
        if any(r is None for r in rosters):
            return None
        return frozenset().union(*[r for r in rosters if r is not None])
    toolset = parts[0] if parts else toolset  # the normalised spelling
    if toolset == "atomic":
        return _MCP_ATOMIC_OPS
    if toolset == "tasks":
        return _MCP_TASK_OPS
    if toolset == "tracker":
        return _MCP_TRACKER_OPS
    if toolset == "coding":
        return _MCP_CODING_OPS | _MCP_CODE_CORE_OPS
    if toolset == "core":
        return _MCP_CORE_OPS | _MCP_CODE_CORE_OPS if code_tools else _MCP_CORE_OPS
    return None


def listed_under_all(op: Operation) -> bool:
    """Whether a `tools/list` on ``all`` carries ``op``: never a hidden op,
    always a synthetic tool, else the stable surface plus every op the core
    (code tools included) or atomic roster names regardless of maturity, so
    core ⊆ all holds. The experimental remainder stays callable by name.
    The `code.*` ops also need the code tools flag; the listing applies it."""
    if op.name in _MCP_HIDDEN_OPS:
        return False
    if op.name in SYNTHETIC_TOOL_OPS or op.maturity == "stable":
        return True
    return op.name in _MCP_CORE_OPS or op.name in _MCP_CODE_CORE_OPS or op.name in _MCP_ATOMIC_OPS


def op_toolsets(name: str) -> list[str]:
    """Every toolset whose listing carries the op named ``name`` (the
    catalogue's ``toolsets`` facet): ``all`` when `listed_under_all`,
    ``core`` (with the code tools flag for a code op), ``coding``,
    ``atomic``, ``tasks``, ``tracker``, ``assistant``. A hidden op is on the
    assistant roster or nowhere; an unknown op is nowhere. Sorted for a stable
    wire shape."""
    from server.operations.registry import OPERATIONS_BY_NAME

    op = OPERATIONS_BY_NAME.get(name)
    if op is None:
        return []
    if name in _MCP_HIDDEN_OPS:
        return [ASSISTANT_TOOLSET] if name in MCP_ASSISTANT_OPS else []
    if name in SYNTHETIC_TOOL_OPS:
        return sorted({"all", *CURATED_TOOLSETS})
    out: set[str] = set()
    if listed_under_all(op):
        out.add("all")
    if name in _MCP_CORE_OPS or name in _MCP_CODE_CORE_OPS:
        out.add("core")
    if name in _MCP_CODING_OPS or name in _MCP_CODE_CORE_OPS:
        out.add("coding")
    if name in _MCP_ATOMIC_OPS:
        out.add("atomic")
    if name in _MCP_TASK_OPS:
        out.add("tasks")
    if name in _MCP_TRACKER_OPS:
        out.add("tracker")
    if name in MCP_ASSISTANT_OPS:
        out.add(ASSISTANT_TOOLSET)
    return sorted(out)
