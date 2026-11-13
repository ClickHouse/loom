"""The mentor's rule tables: what fires a hint, and what it says.

`evaluate_rules` runs the profile rules over one agent's operation counts
and its namespace's facts; `CALL_RULES` are the per-call rules over one
call's own arguments and result. Both are pure over what they are handed:
`server/insights/app/mentor.py` reads the counts, caches the profile and
keeps the per-session state. The planes an agent can be pointed at, and the
thresholds each rule compares against, live here with the rules.
"""

from __future__ import annotations

import re
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from server.memory.domain.namespace_shapes import (
    COORDINATION_VALUE_CHARS,
)

# A hint is one sentence: the situation, the op, the reason.
HINT_CHARS = 220
# The profile keeps this many hints, criticals first.
PROFILE_MAX_HINTS = 8

# Per-call rule thresholds.
LARGE_VALUE_CHARS = 4000
HUGE_VALUE_CHARS = 20000
SET_CHURN_PER_SESSION = 5
# Profile rule thresholds.
KEY_ONLY_MIN_GETS = 50
KEY_ONLY_RATIO = 5.0
WRITES_NEVER_READ_MIN_SETS = 20
WRITES_NEVER_READ_SHARE = 0.02
NO_PACK_MIN_SESSIONS = 3
CODE_UNUSED_MIN_SEARCHES = 10
CODE_UNUSED_MIN_SETS = 20
BISECT_MIN_HISTORY_CALLS = 3
LARGE_SETS_MIN = 5
HUGE_SETS_MIN = 3
DUP_MIN = 5
DUP_SHARE = 0.10
RECORDS_MIN_SETS = 30
RECORDS_MIN_SAME_PREFIX = 10
HOT_SET_PER_SESSION = 10
HOT_GET_PER_SESSION = 20
FACTS_MIN_ENTITY_ROWS = 50
RUNS_MIN_SESSIONS = 5
ABSTAIN_MIN_SEARCHES = 10
ABSTAIN_SHARE = 0.40
# Reliance: keys an agent credits in run.complete(memories_used) that are chunk
# ids, or that no live memory row answers; and keys its reads asked for that
# were not there. An agent can credit hundreds of chunk ids a day and miss a
# quarter of its bulk_get keys with no hint from anyone, although the ledger and
# the run table both hold the evidence.
RELIED_MIN_KEYS = 20
RELIED_CHUNK_SHARE = 0.10
RELIED_UNRESOLVABLE_SHARE = 0.05
READ_MISS_MIN_ASKED = 50
READ_MISS_SHARE = 0.20


GETS = ("memory.get", "memory.bulk_get")
SEARCHES = ("memory.search", "memory.ask")
# Every recall by meaning, the unified search (loom_search's op) included: what the
# reading rules count, since they advise search.query.
RECALLS = (*SEARCHES, "search.query")
SETS = (
    "memory.set",
    "memory.bulk_set",
    "memory.import",
    "memory.update",
    "memory.update_by_id",
    "memory.set_from_messages",
)
PACKS = ("context.pack", "loom_activate", "memory.stack", "memory.brief")
HISTORY_OPS = ("code.blame_range", "code.history", "code.symbol_history")

# Who acts on a conflict: an agent cannot settle one over MCP.
_CONFLICTS_ACTOR = "the user resolves conflicts in the dashboard's Review queue"

# The plane an op belongs to, for `mentor.brief`'s planes table and the
# "when" a hint points at. Prefix → (plane, one-line when).
PLANES: tuple[tuple[str, str, str, bool], ...] = (
    (
        "memory.record",
        "records",
        "a table of facts with identity: register a schema, write rows, aggregate",
        True,
    ),
    (
        "memory_schema",
        "records",
        "a table of facts with identity: register a schema, write rows, aggregate",
        True,
    ),
    (
        "memory",
        "claims",
        "one plain-text claim per readable key; recall by meaning with search.query",
        True,
    ),
    (
        "working_memory",
        "scratchpad",
        "the plan, the lease, 'where was I' — mutable blocks read by key, never ranked",
        True,
    ),
    (
        "knowledge",
        "documents",
        "anything over ~2 KB: specs, runbooks, reports, review bodies — chunked and searched",
        True,
    ),
    (
        "context",
        "checkpoints",
        "context.pack at session start; context.offload parks a large tool result with a TTL",
        True,
    ),
    (
        "facts",
        "graph",
        "walk the entity graph: facts.traverse from a seed, facts.path between two",
        True,
    ),
    (
        "code",
        "code index",
        "identifier questions: code.search, code.symbol, code.callers, code.grep, code.impact",
        True,
    ),
    (
        "tracker",
        "tracker",
        "test failures and reports: tracker.similar, tracker.duplicate_check, "
        "tracker.flaky, tracker.test_signal",
        True,
    ),
    (
        "run",
        "runs",
        "run.begin before the work, run.complete with the memories used — outcome-graded memory",
        True,
    ),
    (
        "decision",
        "decisions",
        "record a decision with its scenario; decision.precedents before a similar one",
        True,
    ),
    (
        "procedure",
        "procedures",
        "a way of doing something that worked: procedure.add, procedure.match on a new task",
        True,
    ),
    (
        "conflicts",
        "conflicts",
        "two facts that disagree: " + _CONFLICTS_ACTOR,
        True,
    ),
    (
        "conversation",
        "conversations",
        "the transcript itself, where the owner turned recording on",
        False,
    ),
    (
        "profile",
        "profile",
        "the always-loaded read contract; profile.set replaces a section",
        False,
    ),
    (
        "namespace",
        "namespace",
        "namespace.validate before done; namespace.audit when recall abstains on what you wrote",
        False,
    ),
    (
        "erasure",
        "erasure",
        "a person's right to be forgotten or to a copy: erasure.request, erasure.export",
        False,
    ),
)


def plane_of(op: str) -> str:
    return known_plane(op) or op.split(".", 1)[0]


def known_plane(op: str) -> str | None:
    """The plane of `op` in the table above, or None for an op outside it (no fallback):
    what a hint names as the place its advice applies."""
    for prefix, plane, _, _ in PLANES:
        if op == prefix or op.startswith(prefix + "."):
            return plane
    return None


def hint_plane(ops: list[str] | tuple[str, ...]) -> str | None:
    """The plane a hint names: that of the first of its ops that has one, or None (a hint
    may lead with an op of no plane, `search.query`, before the ops it is about)."""
    return next((p for op in ops if (p := known_plane(op))), None)


def plane_mentored(plane: str) -> bool:
    """Planes the mentor has a rule for. The transcript, the profile, the
    namespace ops and erasure are not things an agent is nudged toward."""
    for _, name, _, mentored in PLANES:
        if name == plane:
            return mentored
    return False


@dataclass(frozen=True)
class Hint:
    code: str
    severity: str  # critical | warning | info
    text: str
    ops: tuple[str, ...]
    evidence: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class MentorProfile:
    org: str
    namespace: str
    agent: str
    window_seconds: int
    usage: dict[str, int]
    sessions: int
    facts: dict[str, Any]
    hints: tuple[Hint, ...]
    skipped: tuple[str, ...] = ()
    computed_at: datetime = field(default_factory=lambda: datetime.now(UTC).replace(tzinfo=None))

    def hint(self, code: str) -> Hint | None:
        for h in self.hints:
            if h.code == code:
                return h
        return None


def _count(usage: dict[str, int], ops: tuple[str, ...]) -> int:
    return sum(int(usage.get(o, 0)) for o in ops)


def _prefix_count(usage: dict[str, int], prefix: str) -> int:
    return sum(int(n) for o, n in usage.items() if o == prefix or o.startswith(prefix + "."))


# Ops an MCP agent reaches through a listed synthetic tool or a core alias:
# no listing note for these.
_LISTED_VIA_ALIAS = frozenset({"memory.search", "knowledge.search", "conversation.search"})


# The note _unlisted adds for an op the core listing leaves out, and its pattern.
_LOOM_OP_NOTE = " (call {} via loom_op)"
# The note ends a hint: anchored, so an echoed value that spells one is not taken for it.
_LOOM_OP_NOTE_RE = re.compile(r" \(call ([a-z_.]+) via loom_op\)\Z")


def without_listing_note(text: str) -> str:
    """``text`` without _unlisted's core-listing note, for a caller that names
    ops for its own listing instead."""
    return _LOOM_OP_NOTE_RE.sub("", text)


def _unlisted(ops: tuple[str, ...]) -> str:
    """A trailing note for the first recommended op that is not on the core
    listing, for a reader of the stored text (HTTP, the SDK, the dashboard). Over
    MCP the note is stripped and the hint is named for the caller's own listing."""
    try:
        from server.operations.toolsets import _MCP_HIDDEN_OPS, op_toolsets
    except Exception:  # noqa: BLE001 — no MCP config in this process: no note
        return ""
    for op in ops:
        if op in _LISTED_VIA_ALIAS:
            continue
        try:
            sets = op_toolsets(op)
        except Exception:  # noqa: BLE001 — an op with no toolset entry is skipped
            continue
        if op in _MCP_HIDDEN_OPS:
            # Name who acts, never a route an agent could chase.
            if op.startswith("conflicts."):
                return f" ({_CONFLICTS_ACTOR})"
            return f" ({op} is not an agent tool; skip it over MCP)"
        if "core" not in sets:
            return _LOOM_OP_NOTE.format(op)
    return ""


def _usd(amount: float) -> str:
    """Dollars to the 6 places cost and budgets are kept to: "$2.50", "$1.000004"."""
    return "$" + re.sub("0{1,4}$", "", f"{amount:,.6f}")


def _duration(seconds: int) -> str:
    """Seconds as the Runs page shows them: "42s", "30m 0s", "2h 5m"."""
    if seconds < 90:
        return f"{seconds}s"
    minutes = seconds // 60
    if minutes < 90:
        return f"{minutes}m {seconds % 60}s"
    return f"{minutes // 60}h {minutes % 60}m"


# Room the values a hint echoes take, together: short enough that the clip never
# cuts a closing quote, and that a hint with typical values keeps all its advice.
ECHO_CHARS = 40


def _quoted(value: object, limit: int = ECHO_CHARS) -> str:
    """A caller's value as a hint echoes it: in single quotes, which the MCP renderer
    leaves as written, with none inside and no newline; cut to ``limit`` with an ASCII
    "..." that the X-Loom-Hint header keeps."""
    text = str(value).replace("'", '"').replace("\n", " ")
    if len(text) > limit:
        text = text[: limit - 3] + "..."
    return f"'{text}'"


# The most echoed values a hint lists by name; the rest are counted.
ECHO_VALUES = 3


def _quoted_list(values: list[Any], total: int | None = None) -> str:
    """Up to ECHO_VALUES echoed values sharing one ECHO_CHARS budget, then "+K" for the
    rest of ``total`` (default: the values given)."""
    shown = values[:ECHO_VALUES]
    each = ECHO_CHARS // max(1, len(shown))
    more = (len(values) if total is None else total) - len(shown)
    parts = [_quoted(v, each) for v in shown] + ([f"+{more}"] if more > 0 else [])
    return ", ".join(parts)


def clip(text: str, limit: int = HINT_CHARS) -> str:
    return text if len(text) <= limit else text[: limit - 1].rstrip() + "…"


def clip_keeping_note(text: str) -> str:
    """``text`` clipped to HINT_CHARS with a "(call X via loom_op)" note at its end kept
    whole, as _Ctx.add keeps a profile hint's: that note says how to reach the op. The
    other notes _unlisted writes are part of the body and clip with it."""
    m = _LOOM_OP_NOTE_RE.search(text)
    if m is None:
        return clip(text)
    note = m.group(0)
    return clip(text[: m.start()], HINT_CHARS - len(note)) + note


@dataclass
class _Ctx:
    """One agent's counts, derived once for every rule."""

    usage: dict[str, int]
    sessions: int
    facts: dict[str, Any]
    agent: str = ""
    hints: list[Hint] = field(default_factory=list)

    @property
    def gets(self) -> int:
        return _count(self.usage, GETS)

    @property
    def searches(self) -> int:
        return _count(self.usage, SEARCHES)

    @property
    def recalls(self) -> int:
        return _count(self.usage, RECALLS)

    @property
    def sets(self) -> int:
        return _count(self.usage, SETS)

    @property
    def packs(self) -> int:
        return _count(self.usage, PACKS)

    def fact(self, key: str) -> int:
        return int(self.facts.get(key, 0) or 0)

    def add(
        self, code: str, severity: str, text: str, ops: tuple[str, ...], **evidence: Any
    ) -> None:
        note = _unlisted(ops)
        # The body is clipped, never the note: it says how to reach the op.
        body = clip(text.rstrip(".") + ".", HINT_CHARS - len(note))
        self.hints.append(
            Hint(
                code=code,
                severity=severity,
                text=body + note,
                ops=ops,
                evidence=evidence,
            )
        )


UNNAMED_AGENTS = frozenset({"", "-", "*"})
UNNAMED_MIN_SETS = 20


def _rules_identity(c: _Ctx) -> None:
    """An agent the ledger cannot tell apart from every other unnamed caller."""
    if c.agent in UNNAMED_AGENTS and c.sets >= UNNAMED_MIN_SETS:
        c.add(
            "agent_unnamed",
            "info",
            f"{c.sets} writes under the anonymous agent name {_quoted(c.agent or '-')}: name "
            "the agent (SDK agent=, MCP clientInfo.name, the X-Loom-Agent of a bot) so the "
            "mentor, the cost views and provenance can tell this caller from every other "
            "unnamed one.",
            ("memory.set",),
            sets=c.sets,
        )


def _rules_reading(c: _Ctx) -> None:
    """How the agent reads: by key, never, without a pack."""
    gets, recalls, sets, packs = c.gets, c.recalls, c.sets, c.packs
    if gets >= KEY_ONLY_MIN_GETS and gets >= KEY_ONLY_RATIO * max(recalls, 1):
        c.add(
            "recall_by_key_only",
            "warning",
            f"{gets} exact-key reads to {recalls} searches this week: search.query recalls by "
            "meaning; keep a value read in a loop with working_memory.set.",
            ("search.query", "memory.search", "working_memory.set"),
            gets=gets,
            recalls=recalls,
        )
    reads = recalls + gets + packs
    if (
        sets >= WRITES_NEVER_READ_MIN_SETS
        and c.sessions >= 2
        and reads < WRITES_NEVER_READ_SHARE * sets
    ):
        c.add(
            "writes_never_read",
            "warning",
            f"{sets} writes and {reads} reads over {c.sessions} sessions: "
            "nothing written here is read back. Open a session with context.pack; "
            "recall with search.query before deciding.",
            ("context.pack", "search.query", "memory.search"),
            sets=sets,
            reads=reads,
            sessions=c.sessions,
        )
    sw = c.fact("sessions_with_id")
    if sw >= NO_PACK_MIN_SESSIONS and c.fact("pack_first_sessions") == 0 and packs == 0:
        c.add(
            "no_session_pack",
            "info",
            f"{sw} sessions, none opened with context.pack: one call loads working memory, "
            "profile, the relevant memories and procedures — the cheapest read of the session.",
            ("context.pack",),
            sessions=sw,
        )
    # abstains are counted over the memory searches, whose rows carry out_abstain
    abst, searches = c.fact("abstains"), c.searches
    if searches >= ABSTAIN_MIN_SEARCHES and abst >= ABSTAIN_SHARE * searches:
        c.add(
            "search_abstains",
            "warning",
            f"{abst} of {searches} searches abstained: write the claim first in the value, use "
            "search.query to fan out to documents and conversations, and namespace.audit when "
            "recall misses what you wrote.",
            ("search.query", "namespace.audit", "memory.set"),
            abstains=abst,
            searches=searches,
        )


def _rules_reliance(c: _Ctx) -> None:
    """Does the credit the agent writes land on a fact, and do its reads answer?"""
    credited = c.fact("credited_keys")
    chunk_ids = c.fact("credited_chunk_keys")
    if credited >= RELIED_MIN_KEYS and chunk_ids >= RELIED_CHUNK_SHARE * credited:
        c.add(
            "relied_on_chunk_ids",
            "warning",
            f"{chunk_ids} of {credited} keys named in run.complete's memories_used this week are "
            "chunk ids (chunk:<session>:<n>): every chunk hit carries parent_memory_key; read "
            "and credit that key (memory.get).",
            ("run.complete", "memory.get"),
            credited=credited,
            chunk_ids=chunk_ids,
        )
    unresolvable = c.fact("credited_unresolvable")
    if credited >= RELIED_MIN_KEYS and unresolvable >= RELIED_UNRESOLVABLE_SHARE * credited:
        c.add(
            "relied_on_missing_keys",
            "warning",
            f"{unresolvable} of {credited} keys named in run.complete's memories_used this week "
            "answer no live memory row: the credit lands nowhere. Name the memory_key the "
            "search returned (or its parent_memory_key), and re-read it with memory.get "
            "before relying on it.",
            ("run.complete", "memory.get"),
            credited=credited,
            unresolvable=unresolvable,
        )
    asked, missed = c.fact("keys_asked"), c.fact("keys_missed")
    if asked >= READ_MISS_MIN_ASKED and missed >= READ_MISS_SHARE * asked:
        c.add(
            "reads_miss",
            "warning",
            f"{missed} of {asked} keys asked for by key this week were not there: "
            "a key from a search result that misses is a chunk or a moved key — follow "
            "canonical_key, or recall by meaning with search.query.",
            ("memory.get", "search.query", "memory.search"),
            asked=asked,
            missed=missed,
        )


def _rules_planes(c: _Ctx) -> None:
    """Planes the namespace offers that the agent never called."""
    repos = c.facts.get("repos")
    if (
        repos
        and _prefix_count(c.usage, "code") == 0
        and (c.searches >= CODE_UNUSED_MIN_SEARCHES or c.sets >= CODE_UNUSED_MIN_SETS)
    ):
        shown = list(c.facts.get("repo_names") or [])
        names = _quoted_list(shown, total=repos)
        c.add(
            "code_index_unused",
            "warning",
            f"{repos} indexed {'repo' if repos == 1 else 'repos'} ({names}) and 0 code.* calls: "
            "code.search for a concept, code.symbol for a definition, code.callers for "
            "callers, code.grep for a literal.",
            ("code.search", "code.symbol", "code.callers", "code.grep"),
            repos=repos,
        )
    tracker_sources = c.facts.get("tracker_sources")
    if (
        tracker_sources
        and _prefix_count(c.usage, "tracker") == 0
        and c.fact("tracker_tagged_sets") > 0
    ):
        sources = list(tracker_sources)
        echo = _quoted_list(sources[:1], total=len(sources))
        answers = (
            f"the tracker source ({echo}) answers"
            if len(sources) == 1
            else f"tracker sources ({echo}) answer"
        )
        c.add(
            "tracker_unused",
            "warning",
            f"{c.fact('tracker_tagged_sets')} test/flaky/ci writes, 0 tracker.* calls: {answers} "
            "tracker.similar, tracker.duplicate_check, tracker.flaky, tracker.test_signal.",
            ("tracker.similar", "tracker.duplicate_check", "tracker.flaky", "tracker.test_signal"),
            tracker_tagged_sets=c.fact("tracker_tagged_sets"),
            sources=sources,
        )
    hist = _count(c.usage, HISTORY_OPS)
    if hist >= BISECT_MIN_HISTORY_CALLS and int(c.usage.get("code.bisect_candidates", 0)) == 0:
        c.add(
            "bisect_available",
            "info",
            f"{hist} blame/history calls and no bisect: code.bisect_candidates "
            "names the PRs that first shipped in the failing release and touched the frames — "
            "start there.",
            ("code.bisect_candidates",),
            history_calls=hist,
        )
    ent = c.fact("entity_rows")
    if (
        ent >= FACTS_MIN_ENTITY_ROWS
        and int(c.usage.get("facts.traverse", 0)) == 0
        and int(c.usage.get("memory.related", 0)) == 0
    ):
        c.add(
            "facts_graph_unused",
            "info",
            f"{ent} rows carry entities and the graph was never walked: facts.traverse from a "
            f"seed entity, memory.related for a row's neighbours, facts.path "
            "between two names.",
            ("facts.traverse", "memory.related", "facts.path"),
            entity_rows=ent,
        )
    if c.sessions >= RUNS_MIN_SESSIONS and int(c.usage.get("run.begin", 0)) == 0 and c.sets > 0:
        c.add(
            "runs_unused",
            "info",
            f"{c.sessions} sessions and no run ledger: run.begin before the work "
            "and run.complete with memories_used after grade the memories by the outcomes they "
            "produced.",
            ("run.begin", "run.complete"),
            sessions=c.sessions,
        )


def _rules_shapes(c: _Ctx) -> None:
    """Rows that belong in another plane: documents, checkpoints, records."""
    over = c.fact("shape_oversized_values") + c.fact("shape_json_prefix_excerpts")
    large = c.fact("large_sets")
    if over > 0 or large >= LARGE_SETS_MIN:
        c.add(
            "documents_as_memories",
            "warning",
            f"{max(over, large)} rows this agent wrote are documents stored as memories "
            "(over ~2 KB or JSON past "
            f"the embed clip): knowledge.upsert the body and keep a one-paragraph "
            "pointer row under the same key.",
            ("knowledge.upsert", "memory.set"),
            oversized_rows=over,
            large_sets=large,
        )
    huge, blobs = c.fact("huge_sets"), c.fact("shape_episodic_blobs")
    if huge >= HUGE_SETS_MIN or blobs > 0:
        c.add(
            "checkpoints_as_memory",
            "warning",
            f"{max(huge, blobs)} checkpoint-sized writes: park a run dump with "
            f"context.offload (summary + TTL) and keep one short episodic row "
            "keyed by run id.",
            ("context.offload", "memory.set"),
            huge_sets=huge,
        )
    idk = c.fact("id_keyed_sets")
    if (
        idk >= RECORDS_MIN_SETS
        and c.fact("top_prefix_sets") >= RECORDS_MIN_SAME_PREFIX
        and int(c.usage.get("memory.record.write", 0)) == 0
    ):
        counts = "" if int(c.usage.get("memory.aggregate", 0)) else "; memory.aggregate counts them"
        top = c.facts.get("top_prefix")
        prefix = _quoted(top) if top else "one prefix"
        c.add(
            "records_for_identities",
            "info",
            f"{idk} writes keyed by a number, {c.fact('top_prefix_sets')} of them "
            f"under {prefix}: facts with identity — "
            f"memory_schema.register once, then memory.record.write{counts}.",
            ("memory_schema.register", "memory.record.write", "memory.aggregate"),
            id_keyed_sets=idk,
            top_prefix=c.facts.get("top_prefix", ""),
        )
    dup, sets = c.fact("dup_sets"), c.sets
    if dup >= DUP_MIN and sets > 0 and dup >= DUP_SHARE * sets:
        c.add(
            "duplicates_written",
            "warning",
            f"{dup} of {sets} writes landed as duplicates of an existing row: memory.related or "
            "memory.get before the write, revise under the SAME key, and memory.link supersedes "
            "for a replacement.",
            ("memory.related", "memory.get", "memory.link"),
            duplicate_sets=dup,
            sets=sets,
        )
    conflicted = c.fact("conflicted_rows")
    if conflicted > 0 and c.fact("ns_conflict_ops") == 0:
        c.add(
            "conflicts_pending",
            "info",
            f"{conflicted} facts in this namespace disagree with another and nobody has looked: "
            f"conflicts.list shows the pairs, conflicts.resolve settles one; "
            "search penalises them until then.",
            ("conflicts.list", "conflicts.resolve"),
            conflicted_rows=conflicted,
        )


def _coordination_sentence(c: _Ctx, hot_set: bool, hot_get: bool) -> tuple[str, str]:
    """What the hot-key facts say, naming each key with ITS OWN count: the
    hottest written key and the hottest read key are different rows in
    different sessions more often than not. Returns (sentence, key)."""
    ms, mg = c.fact("max_sets_one_key"), c.fact("max_gets_one_key")
    set_key = str(c.facts.get("hot_set_key") or "")
    get_key = str(c.facts.get("hot_get_key") or "")
    if hot_set and hot_get and set_key == get_key:
        return (
            f"key {_quoted(set_key)} was written {ms}× and read {mg}× in one session",
            set_key,
        )
    parts: list[str] = []
    each = ECHO_CHARS // (2 if hot_set and hot_get else 1)  # the two keys share the budget
    if hot_set:
        parts.append(f"key {_quoted(set_key, each)} was written {ms}×")
    if hot_get:
        parts.append(f"key {_quoted(get_key, each)} was read {mg}×")
    return " and ".join(parts) + " in one session", set_key if hot_set else get_key


def _rules_coordination(c: _Ctx) -> None:
    """Coordination state kept as memory rows: a key rewritten or re-read
    many times within ONE client-named session. The read side counts only
    when the value is short (or its length is unknown): a long row read in a
    loop is a memory recalled by key, `recall_by_key_only`'s ground."""
    ms, mg = c.fact("max_sets_one_key"), c.fact("max_gets_one_key")
    chars = c.fact("hot_get_key_chars")
    hot_set = ms >= HOT_SET_PER_SESSION
    hot_get = mg >= HOT_GET_PER_SESSION and (chars == 0 or chars < COORDINATION_VALUE_CHARS)
    if not (hot_set or hot_get):
        return
    what, key = _coordination_sentence(c, hot_set, hot_get)
    c.add(
        "coordination_loop",
        "warning",
        f"{what}: coordination state; use working_memory.set / working_memory.get "
        "(no version rows).",
        ("working_memory.set", "working_memory.get"),
        max_sets_one_key=ms,
        max_gets_one_key=mg,
        hot_get_key_chars=chars,
        key=key,
    )


_RULE_GROUPS: tuple[Callable[[_Ctx], None], ...] = (
    _rules_identity,
    _rules_reading,
    _rules_reliance,
    _rules_planes,
    _rules_shapes,
    _rules_coordination,
)


def evaluate_rules(
    usage: dict[str, int],
    sessions: int,
    facts: dict[str, Any],
    *,
    agent: str = "",
    max_hints: int = PROFILE_MAX_HINTS,
) -> list[Hint]:
    """The fixed rule table over one agent's counts and its namespace's facts.
    A rule whose fact the read did not supply does not fire. Criticals first,
    then warnings, then by the size of the count; at most `max_hints`."""
    c = _Ctx(usage=usage, sessions=int(sessions), facts=facts, agent=str(agent or ""))
    for group in _RULE_GROUPS:
        group(c)
    order = {"critical": 0, "warning": 1, "info": 2}
    c.hints.sort(
        key=lambda h: (
            order.get(h.severity, 3),
            -max([v for v in h.evidence.values() if isinstance(v, int)] or [0]),
            h.code,
        )
    )
    return c.hints[:max_hints]


_IDENT_TOKEN_RE = re.compile(
    r"(?:\b[a-z]+_[a-z0-9_]+\b)"  # snake_case
    r"|(?:\b[A-Z][a-z0-9]+(?:[A-Z][a-z0-9]+)+\b)"  # CamelCase
    r"|(?:\b\w+::\w+)"  # C++ scope
    r"|(?:\b\w+\(\))"  # a call
    r"|(?:\b[\w./-]+\.(?:py|cpp|h|hpp|ts|tsx|js|go|rs|java|sql|sh)\b)"  # a source path
)
# A line's own leading blanks only: `^\s*` would rescan every blank line below.
_TRACE_LINE_RES = (
    re.compile(r"^[^\S\n]*File \"[^\"]+\", line \d+", re.M),  # Python
    re.compile(r"^[^\S\n]*at [\w.$<>]+ ?\(.*:\d+(?::\d+)?\)", re.M),  # JS/Java
    re.compile(r"^[^\S\n]*#\d+\s+0x[0-9a-fA-F]+ in ", re.M),  # gdb / ClickHouse
    re.compile(r"^[^\S\n]*\d+\.\s+(?:0x[0-9a-fA-F]+\s+)?[\w:<>]++.*@ 0x", re.M),  # ClickHouse
    re.compile(r"^[^\S\n]*goroutine \d+ \[", re.M),  # Go
    re.compile(r"^[^\S\n]*[\w./-]+\.go:\d+", re.M),  # Go frames
)
_TRACE_HEAD_RE = re.compile(
    r"Traceback \(most recent call last\)|Stack trace|Exception:|Error:|panic:|DB::Exception|"
    r"Segmentation fault|SIGSEGV"
)


def _assertion_failed(text: str) -> bool:
    """An `Assertion … failed` line. Read once per line: as a pattern, each
    `Assertion ` on a line rescans the rest of it."""
    for line in text.split("\n"):
        at = line.find("Assertion ")
        if at >= 0 and line.find(" failed", at + len("Assertion ")) >= 0:
            return True
    return False


_ISSUE_REF_RE = re.compile(r"(?:^|[\s(\[])#(\d{3,7})\b|/(?:pull|issues)/(\d{2,7})\b")


def looks_like_identifier_query(query: str) -> bool:
    """A search whose text is a code identifier, a scoped name, a call or a
    source path — the shape the code index answers exactly and memory
    answers by accident."""
    q = (query or "").strip()
    if not q or len(q) > 200:
        return False
    words = q.split()
    if len(words) > 6:
        return False
    return bool(_IDENT_TOKEN_RE.search(q))


def looks_like_stack_trace(value: str) -> bool:
    """Three or more frame lines, or a frame line under an exception head."""
    v = value or ""
    if len(v) < 80:
        return False
    frames = sum(len(r.findall(v)) for r in _TRACE_LINE_RES)
    return frames >= 3 or (frames >= 1 and (bool(_TRACE_HEAD_RE.search(v)) or _assertion_failed(v)))


def issue_references(text: str) -> list[str]:
    """Issue / PR numbers the text names (`#12345`, `/pull/123`, `/issues/123`)."""
    out: list[str] = []
    for m in _ISSUE_REF_RE.finditer(text or ""):
        n = m.group(1) or m.group(2)
        if n and n not in out:
            out.append(n)
    return out


def _value_text(arguments: dict[str, Any]) -> str:
    v = arguments.get("value")
    if v is None:
        return ""
    if isinstance(v, str):
        return v
    try:
        import json

        return json.dumps(v, default=str)
    except Exception:  # noqa: BLE001 — an unserializable value reads as empty
        return ""


def _get_missed(result: dict[str, Any]) -> bool:
    if result.get("found") is False:
        return True
    return (
        bool(result.get("memory_key"))
        and result.get("value") is None
        and "found" in result
        and not result["found"]
    )


def _value_chars(arguments: dict[str, Any]) -> int:
    v = arguments.get("value")
    if v is None:
        return 0
    if isinstance(v, str):
        return len(v)
    try:
        import json

        return len(json.dumps(v, default=str))
    except Exception:  # noqa: BLE001 — an unserializable value counts as empty
        return 0


_Call = tuple[str, str] | None


def _rule_set_churn(
    op: str, args: dict[str, Any], res: dict[str, Any], facts: dict[str, Any], st: dict[str, Any]
) -> _Call:
    key = str(args.get("memory_key") or "")
    if op not in SETS or not key:
        return None
    n = st["set_keys"].get(key, 0) + 1
    st["set_keys"][key] = n
    if n < SET_CHURN_PER_SESSION:
        return None
    return (
        "set_churn",
        f"{_quoted(key)} was set {n} times this session: a value that changes every turn is "
        "working_memory.set, not a memory version each time.",
    )


def _rule_set_stack_trace(
    op: str, args: dict[str, Any], res: dict[str, Any], facts: dict[str, Any], st: dict[str, Any]
) -> _Call:
    if op != "memory.set" or not looks_like_stack_trace(_value_text(args)):
        return None
    repos, qa = int(facts.get("repos") or 0), list(facts.get("tracker_sources") or [])
    if repos and qa:
        text = (
            "this value is a stack trace: code.trace resolves each frame to its symbol and "
            "tracker.similar finds a report that names it; keep the memory row as one claim "
            "pointing at them." + _unlisted(("code.trace", "tracker.similar"))
        )
    elif repos:
        text = (
            "this value is a stack trace: code.trace resolves each frame to its symbol and "
            "code.callers / code.history follow the one that matters; keep the memory row as "
            "the one-line finding." + _unlisted(("code.trace",))
        )
    elif qa:
        text = (
            "this value is a stack trace: tracker.similar finds the reports that "
            "already name it and "
            "tracker.duplicate_check gives a verdict before you file one."
            + _unlisted(("tracker.similar",))
        )
    else:
        text = (
            "this value is a stack trace: park the dump with context.offload (summary + TTL) and "
            "keep the memory row as the one-line finding." + _unlisted(("context.offload",))
        )
    return ("set_stack_trace", text)


def _rule_set_issue_reference(
    op: str, args: dict[str, Any], res: dict[str, Any], facts: dict[str, Any], st: dict[str, Any]
) -> _Call:
    if op != "memory.set":
        return None
    refs = issue_references(str(args.get("memory_key") or "") + " " + _value_text(args)[:2000])
    if not refs:
        return None
    if facts.get("tracker_sources"):
        return (
            "set_issue_reference",
            f"this row names issue/PR #{refs[0]}: tracker.item returns its record (component, "
            "severity, bisect) — write the claim, not the ticket; memory.record.write keeps a "
            "table of them." + _unlisted(("tracker.item", "memory.record.write")),
        )
    return (
        "set_issue_reference",
        f"this row names issue/PR #{refs[0]}: ticket-keyed rows are facts with identity — "
        "memory_schema.register once, then memory.record.write per ticket; code.pr_check "
        "before a second PR." + _unlisted(("memory_schema.register",)),
    )


def _rule_get_miss(
    op: str, args: dict[str, Any], res: dict[str, Any], facts: dict[str, Any], st: dict[str, Any]
) -> _Call:
    if op != "memory.get" or not _get_missed(res):
        return None
    return (
        "get_miss",
        "no row at that key: search.query recalls by meaning, memory.list with a "
        "key prefix lists the siblings, and a moved key answers with canonical_key (follow=true).",
    )


def _rule_search_identifier(
    op: str, args: dict[str, Any], res: dict[str, Any], facts: dict[str, Any], st: dict[str, Any]
) -> _Call:
    if op not in SEARCHES or int(facts.get("repos") or 0) <= 0:
        return None
    q = str(args.get("query") or "")
    thin = res.get("abstain") is True or not res.get("hits")
    if not thin or not looks_like_identifier_query(q):
        return None
    return (
        "search_identifier_query",
        f"{_quoted(q)} is an identifier: code.symbol gives its definition, code.callers its "
        "callers, code.grep the literal, code.search the concept; memory holds only what was "
        "said about it.",
    )


def _rule_search_abstain(
    op: str, args: dict[str, Any], res: dict[str, Any], facts: dict[str, Any], st: dict[str, Any]
) -> _Call:
    if op not in SEARCHES or res.get("abstain") is not True:
        return None
    if int(facts.get("repos") or 0) > 0:
        return (
            "search_abstain_code",
            "memory abstained; for a symbol, file or call site the code index answers: "
            "code.search, code.symbol, code.callers, code.grep — code.repos lists what is indexed.",
        )
    if int(facts.get("documents") or 0) > 0:
        return (
            "search_abstain_knowledge",
            f"memory abstained and this namespace holds {facts['documents']} documents: "
            "search.query fans out to knowledge and conversations in one call.",
        )
    return None


def _rule_set_duplicate(
    op: str, args: dict[str, Any], res: dict[str, Any], facts: dict[str, Any], st: dict[str, Any]
) -> _Call:
    if op != "memory.set" or not str(res.get("duplicate_of") or ""):
        return None
    return (
        "set_duplicate",
        "this write duplicates the row named in the result's duplicate_of: revise that key instead "
        "(same key = new version), or memory.link supersedes if this one replaces it.",
    )


def _rule_set_large_value(
    op: str, args: dict[str, Any], res: dict[str, Any], facts: dict[str, Any], st: dict[str, Any]
) -> _Call:
    if op != "memory.set" or _value_chars(args) < LARGE_VALUE_CHARS:
        return None
    return (
        "set_large_value",
        f"a {_value_chars(args):,}-char value: over ~2 KB reads better as a document — "
        "knowledge.upsert the body and keep a one-paragraph pointer row under this key."
        + _unlisted(("knowledge.upsert",)),
    )


def _rule_run_over_budget(
    op: str, args: dict[str, Any], res: dict[str, Any], facts: dict[str, Any], st: dict[str, Any]
) -> _Call:
    if op != "run.get":
        return None
    over = res.get("over_budget") or []
    run = res.get("run") or {}
    if not over or run.get("status") != "active":
        return None
    spent = []
    if "cost" in over:
        budget = _usd(float(run.get("budget_usd") or 0.0))
        spent.append(f"{_usd(float(res.get('cost_usd') or 0.0))} of {budget} spent")
    if "time" in over:
        elapsed = int(res.get("elapsed_seconds") or 0)
        allowed = int(run.get("budget_seconds") or 0)
        spent.append(f"{_duration(elapsed)} of {_duration(allowed)}")
    budgets = "cost and time budgets" if len(over) > 1 else f"{over[0]} budget"
    return (
        "run_over_budget",
        f"this run is past its {budgets} ({', '.join(spent)}): wrap up and run.complete, "
        "or ask the user before going on." + _unlisted(("run.complete",)),
    )


# Per-call rules in precedence order: one fires per call.
CALL_RULES: tuple[Callable[..., _Call], ...] = (
    _rule_set_churn,
    _rule_set_stack_trace,
    _rule_set_issue_reference,
    _rule_get_miss,
    _rule_search_identifier,
    _rule_search_abstain,
    _rule_set_duplicate,
    _rule_set_large_value,
    _rule_run_over_budget,
)
# Per-call hint codes and the ops that count as following them (the profile
# codes carry their own `ops`); read by mentor.brief's adoption.
CALL_HINT_OPS: dict[str, tuple[str, ...]] = {
    "get_miss": ("memory.search", "memory.list", "search.query"),
    "search_identifier_query": ("code.symbol", "code.callers", "code.grep", "code.search"),
    "search_abstain_code": (
        "code.search",
        "code.symbol",
        "code.callers",
        "code.grep",
        "code.repos",
    ),
    "search_abstain_knowledge": ("search.query", "knowledge.search", "knowledge.get"),
    "set_duplicate": ("memory.update", "memory.link", "memory.get", "memory.related"),
    "set_large_value": ("knowledge.upsert", "knowledge.add"),
    "run_over_budget": ("run.complete",),
    "set_churn": ("working_memory.set", "working_memory.append", "working_memory.replace"),
    "set_stack_trace": (
        "code.trace",
        "tracker.similar",
        "tracker.duplicate_check",
        "context.offload",
    ),
    "set_issue_reference": (
        "tracker.item",
        "memory.record.write",
        "memory_schema.register",
        "code.pr_check",
    ),
}


__all__ = [
    "CALL_HINT_OPS",
    "CALL_RULES",
    "HINT_CHARS",
    "PLANES",
    "Hint",
    "MentorProfile",
    "clip",
    "clip_keeping_note",
    "evaluate_rules",
    "hint_plane",
    "issue_references",
    "known_plane",
    "looks_like_identifier_query",
    "looks_like_stack_trace",
    "plane_mentored",
    "plane_of",
    "without_listing_note",
]
