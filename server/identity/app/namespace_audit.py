"""namespace.audit — how a namespace is being used, with the anti-patterns named.

`namespace.validate` reports the integrity of what was written (two tables disagreeing, a
vector with no memory); this op reports HOW the namespace is being used. An agent that treats
`memory.set` as an object store (hash keys, double-encoded JSON, one lease key read a hundred
times, hand-rolled index rows, 1 MB blobs whose claim falls past the embed clip) leaves every
table consistent and recall useless. Each of those facts is derivable from `loom.memory`,
`loom.memory_embeddings`, `loom.memory_versions`, `loom.search_trace_log` and
`loom.agent_events` with SQL; this op says it, so the namespace owner can run it and the
optimize skill can act on it.

Thirteen checks, each one read over existing tables scoped to (org, namespace).
No mutation, no new storage. A check that fails to run is reported in
`skipped` and the rest still return. Samples carry keys and counts only,
never a value.

There is no query-shape check: the ledger keeps no query text
(`server/observability/event_args.py` replaces every free-text argument with
`sha256:<16 hex>/<length>` by design) and `search_trace_log.query` is written only under
`search_trace_log_enabled` with `langfuse_capture_io_level != 'off'`.

Severity is graded per finding from the thresholds below: `critical` when
the namespace is being used as something other than a memory (recall cannot
work), `warning` when a pattern costs recall or storage on part of it,
`info` when the check ran and found little or nothing. `healthy` is "no
critical".
"""

from __future__ import annotations

import logging
import re
from collections.abc import Awaitable, Callable, Iterable, Sequence
from typing import Any

from server.core.ch import get_client
from server.core.datetimes import naive_utc_now_seconds
from server.identity.api.models_namespace_catalog import (
    NamespaceAuditFinding,
    NamespaceAuditInput,
    NamespaceAuditOutput,
    NamespaceAuditSummary,
)
from server.identity.app.namespace_summary import state_change_ops
from server.identity.app.report_rules import RuleContract, RuleLog, attach_evidence
from server.memory.domain.excerpt import EXCERPT_LIMIT
from server.memory.domain.memory_types import NON_SEARCHABLE_TYPES
from server.memory.domain.namespace_shapes import (
    COORDINATION_MAX_WORDS,
    COORDINATION_SEGMENTS,
    COORDINATION_VALUE_CHARS,
    EPISODIC_BLOB_CHARS,
    STRUCTURED_TYPES,
)
from server.observability.consumer import INSPECTOR_CONSUMERS
from server.settings import settings

log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Thresholds. Each critical threshold sits well under what a namespace used as an object store
# shows (gets two orders of magnitude above searches, nearly every value double-encoded,
# hundreds of rows past the embedder cap), each warning threshold where the pattern starts
# costing recall on a meaningful part of the namespace.
# ---------------------------------------------------------------------------
# exact_key_store: gets of non-structured rows ≥ RATIO × searches AND ≥ MIN_GETS is critical
# (a small namespace read by key a few times is not an object store).
EXACT_KEY_STORE_RATIO = 20.0
EXACT_KEY_STORE_MIN_GETS = 200
# A key read this many times in the window is "hot" (samples; coordination check).
HOT_KEY_READS = 20
# hand_rolled_index_rows: a value naming this many other live keys is an index.
INDEX_ROW_MIN_KEYS = 8
INDEX_ROW_VALUE_CLIP = 65_536
# double_encoded_values: share of live rows that are a JSON string holding JSON.
DOUBLE_ENCODED_CRITICAL_SHARE = 0.25
# json_prefix_excerpts: share of searchable rows whose embedded text is a JSON prefix.
JSON_PREFIX_WARNING_SHARE = 0.10
# hash_keys: share of live keys whose LAST path segment is ≥ 24 hex chars, or
# any segment is ≥ 40 hex chars (a sha). A readable layout may carry a hex
# scope id mid-path (`sre/case/<24-hex case>/evidence/E-rule`) — that is an
# id, not a hash key; `…/<sha256>` as the identity of the row is.
HASH_KEY_WARNING_SHARE = 0.25
# coordination_as_memory / episodic_blobs bounds and STRUCTURED_TYPES are shared
# with the mentor: server/memory/domain/namespace_shapes.py.
# telemetry_unavailable: a write younger than this may not have reached the ledger yet (it
# is flushed in batches), so only older writes are judged.
TELEMETRY_LAG_SECONDS = 60
# index_backlog: a searchable row with no vector and no recorded embed error younger than
# this is the embedding worker's normal lag (namespace.validate's default grace).
INDEX_GRACE_SECONDS = 600
# version_churn: version rows ≥ ROWS × live rows, or version bytes ≥ BYTES × live bytes.
VERSION_CHURN_ROWS_RATIO = 3.0
VERSION_CHURN_BYTES_RATIO = 2.0

# A key path segment that is a hash (24+ hex chars between separators).
_HASH_KEY_SEGMENT_RE = r"(^|[/:])[0-9a-fA-F]{24,}$|(^|[/:])[0-9a-fA-F]{40,}([/:]|$)"
_TOKEN_SPLIT_RE = re.compile(r"""[\s"',\[\]{}()<>\\]+""")

_LEDGER_GETS = "('memory.get', 'memory.bulk_get')"
_LEDGER_SEARCHES = "('memory.search', 'memory.ask')"
_LEDGER_SETS = (
    "('memory.set', 'memory.bulk_set', 'memory.update', 'memory.update_by_id', "
    "'memory.set_from_messages')"
)

_NOT_STRUCTURED = "memory_type NOT IN {structured:Array(String)}"

_SCOPE = "org = {org:String} AND namespace = {ns:String}"
_LIVE = f"FROM loom.memory FINAL WHERE {_SCOPE} AND tombstoned_at IS NULL"
_SEARCHABLE = "memory_type NOT IN {ns_types:Array(String)}"
_LEDGER_ALL = (
    f"FROM loom.agent_events WHERE {_SCOPE} AND ts >= now() - INTERVAL {{win:UInt32}} SECOND"
)
# The dashboard's and the assistant's calls inspect the namespace on a person's behalf;
# counting them would grade the namespace by who looked at it.
_AGENT_CALL = "consumer NOT IN {inspectors:Array(String)}"
_LEDGER = f"{_LEDGER_ALL} AND {_AGENT_CALL}"
# A hot key: a named key read HOT_KEY_READS times or more in the window. One fragment for
# the exact-key samples and the hot-key list the coordination check joins.
_HOT_KEY = "memory_key != '' AND reads >= {hot:UInt32}"
# The reports on a namespace: their own ledger rows say nothing about whether the window's
# calls were recorded.
_REPORT_OPS = (
    "('namespace.audit', 'namespace.validate', 'namespace.summary', 'namespace.inventory', "
    "'mentor.brief')"
)
# The live keys of a type that is structured by design: read back whole by key on purpose.
_TYPED_KEYS = f"SELECT memory_key {_LIVE} AND memory_type IN {{structured:Array(String)}}"
_HOT_KEYS = (
    f"SELECT memory_key, count() AS reads {_LEDGER} "
    f"AND operation IN {_LEDGER_GETS} "
    f"GROUP BY memory_key HAVING {_HOT_KEY}"
)
# JSON value shapes. JSONType returns '' / 'Null' for non-JSON text.
_IS_JSON_STRING_OF_JSON = (
    "JSONType(value) = 'String' AND JSONType(JSONExtractString(value)) IN ('Object', 'Array')"
)
_IS_JSON_CONTAINER = "JSONType(value) IN ('Object', 'Array')"

# A model call the deployment held or could not bill: the row's error is not its own.
# The embedding worker's markers (`HELD`, `customer.NO_CUSTOMER`); a unit test pins them.
EMBED_HOLDS: tuple[str, ...] = (
    "model call held",
    "MODEL_CALL_UNATTRIBUTED: the row names no org to bill",
)
# Over loom.memory_embeddings: a searchable row the worker gave up on (summary and check).
_UNEMBEDDED_ROW = (
    f"embedding_dim = 0 AND embed_last_error != '' "
    f"AND embed_last_error NOT IN {{holds:Array(String)}} AND {_SEARCHABLE}"
)
# Remedies point at the recipe: docs/guides/investigation-agent-memory.md.
_RECIPE = "docs/guides/investigation-agent-memory.md"

# code: (detail, remedy). One sentence each — what the check found means,
# and what to do about it.
_TEXT: dict[str, tuple[str, str]] = {
    # Runs first: the ledger checks read whether it found the ledger empty.
    "telemetry_unavailable": (
        "Live rows were created in the window but the ledger holds no call for the "
        "namespace apart from reports on it, the dashboard's or the assistant's reads and "
        "page views, and a worker's own rows: the read and search counts were not recorded, "
        "so the checks built on them are skipped rather than reported clean.",
        "Check that the server writes its event ledger (loom.agent_events) and keeps it "
        "for the window (LOOM_RETENTION_AGENT_EVENTS_DAYS), or whether the rows were written "
        "in-process (`loom import-recall` or `import-brain` without `--base-url`), then "
        "rerun with a window the ledger covers; `memory.list` still shows the rows.",
    ),
    "exact_key_store": (
        "Exact-key reads (memory.get, memory.bulk_get) counted against searches "
        "(memory.search, memory.ask) in the window; critical once reads reach "
        f"{EXACT_KEY_STORE_MIN_GETS} and {EXACT_KEY_STORE_RATIO:g}× the searches — the "
        "namespace is then an object store, and nothing in it is recalled by meaning.",
        "Write readable keys and recall with `memory.search` (tags carry the facets); "
        "keep leases and pointers in `working_memory`, not `memory.set`. "
        f"See {_RECIPE}.",
    ),
    "intentional_exact_key_reads": (
        "Exact-key reads of rows whose type is structured by design (procedures are read "
        "back whole by key): a workflow, left out of the object-store gauge.",
        "Nothing to change: `memory.get` reads of these rows are not counted against "
        "the namespace.",
    ),
    "index_backlog": (
        "Searchable rows with no vector and no error of their own: rows the embedding "
        "worker has not reached, or whose model call the deployment held or could not "
        f"bill. Within {INDEX_GRACE_SECONDS // 60} minutes of a write that is normal lag; "
        "older, the worker is stopped or behind, or the calls are held. Either way the rows "
        "are not found by meaning yet, through no fault of the writer.",
        "Check that the embedding worker runs and that the org's model calls are allowed: "
        "a held row is retried on the worker's next pass. A row whose call named no org to "
        "bill is given up on; once the deployment attributes its calls, a new revision of "
        "the row (`memory.update_by_id`) queues it again. `memory.get` still reads them by "
        "key meanwhile.",
    ),
    "hand_rolled_index_rows": (
        f"Live rows whose value names {INDEX_ROW_MIN_KEYS}+ other live keys of the "
        "namespace: a hand-rolled index the agent maintains because it cannot find its "
        "own rows — it goes stale on every write and is never recalled by meaning.",
        "Replace the index with `tags` + `memory.list prefix`, revise a fact under the "
        f"SAME key, and chain revisions with `memory.link supersedes`. See {_RECIPE}.",
    ),
    "double_encoded_values": (
        "Values that are a JSON string containing serialized JSON (json.dumps twice): "
        "the embedded text starts with an escaped quote and the claim is buried in "
        "escape sequences.",
        "Send the text, or the object itself, as `value` — memory.set advises on a "
        "double-encoded value.",
    ),
    "json_prefix_excerpts": (
        f"Searchable rows holding a JSON object/array longer than {EXCERPT_LIMIT} chars "
        "(the embed clip): the vector is built from a JSON prefix, and the claim the "
        "agent will ask about may sit past it — recall abstains on a near-verbatim query.",
        "Write the claim first as plain text (one sentence, then the source line); "
        f"documents go to `knowledge.upsert`. See {_RECIPE}.",
    ),
    "oversized_values": (
        "Searchable rows longer than the memory value advisory "
        f"({settings.memory_value_advisory_chars} chars): a document, "
        "not a memory, and past the embedder's cap it is never embedded at all.",
        "Store the document with `knowledge.upsert` and write a one-paragraph memory "
        f"row under the same key that points at it. See {_RECIPE}.",
    ),
    "unembedded_rows": (
        "Searchable rows the embedding worker gave up on (no vector, an embed error of the "
        "row's own): "
        "invisible to memory.search however well the key is chosen.",
        "Shorten or split the value (`knowledge.upsert` for documents), then memory.set "
        "the row again; `namespace.validate` lists them among its `unembedded` rows.",
    ),
    "hash_keys": (
        "Live keys whose last segment is 24+ hex chars, or any segment a 40+ hex sha: "
        "the row's identity is a hash, so a person cannot read it, `memory.list "
        "prefix` cannot group it, and a revision becomes a new key "
        "instead of a new version.",
        "Use readable keys shaped `<scope>/<kind>/<id>` (e.g. `sre/case/<case>/evidence/"
        f"<id>`); revise under the same key. See {_RECIPE}.",
    ),
    "coordination_as_memory": (
        f"Small values (under {COORDINATION_VALUE_CHARS} chars and {COORDINATION_MAX_WORDS} "
        f"words) read {HOT_KEY_READS}+ times in the window, or keys with a lease/lock/freeze "
        "segment: "
        "coordination state, paying for a version row, an embedding and a conflict "
        "check on every write.",
        "Keep leases, locks and pointers in `working_memory.set/get`; they are read by "
        f"key, never embedded or ranked. See {_RECIPE}.",
    ),
    "episodic_blobs": (
        f"Episodic rows longer than {EPISODIC_BLOB_CHARS} chars: a checkpoint or a run "
        "dump stored as a memory row, versioned on every rewrite.",
        "Park it with `context.offload` (a summary plus a TTL) and keep the run ledger "
        f"as a short episodic row keyed by run id. See {_RECIPE}.",
    ),
    "version_churn": (
        "Version history far larger than the live namespace: large values rewritten "
        "over and over, every rewrite a full copy in loom.memory_versions.",
        "Rewrite less — revise the claim, not the blob; documents that change belong in "
        "`knowledge.upsert`, checkpoints in `context.offload`.",
    ),
}

FINDING_CODES: tuple[str, ...] = tuple(_TEXT)

# Each code's contract (`RuleContract`). The version goes up when the
# code's predicate or built-in threshold changes; a setting it reads is not versioned.
_STRUCTURED_EXCLUDED = (
    "Procedural rows (structured by design) and non-searchable types (episodic, profile) "
    "are not checked."
)
_CONTRACT: dict[str, RuleContract] = {
    "telemetry_unavailable": RuleContract(
        "system_state",
        1,
        windowed=True,
        exceptions=(
            "Only rows created in the window are judged: a quiet namespace has nothing to "
            "count, and a revision (a worker's relabel among them) is not judged. A row "
            f"created in the last {TELEMETRY_LAG_SECONDS} seconds is not judged; its "
            "ledger row may not have landed yet. Reports on the namespace (the audit's own "
            "call among them) and the dashboard's or the assistant's reads and page views do "
            "not close a gap, nor do a worker's own ledger rows; an inspector's calls that "
            "change stored state (a memory write, a setting, a grant) do."
        ),
        uncertainty=(
            "Rows written in-process, by an importer run without `--base-url` or a worker "
            "that creates keys, leave no request row (a worker's own row is not counted) and "
            "read as a gap in a quiet namespace, "
            "and so do rows a recent snapshot restored (`namespace.restore` is ledgered "
            "under no namespace). Copied rows are not judged. A ledger that keeps less than "
            "the window (a retention shorter than it) holds some calls and is not a gap, so "
            "the ledger checks then count only the part it kept."
        ),
    ),
    "exact_key_store": RuleContract(
        "usage_heuristic",
        # 2: reads of structured-by-design rows are left out of the gauge.
        2,
        windowed=True,
        exceptions=(
            "Reads of rows whose type is structured by design are not counted "
            "(intentional_exact_key_reads)."
        ),
        uncertainty=(
            "A workflow that reads known keys on purpose shows the same ratio, and the ledger "
            "counts calls, not whether a result reached the model."
        ),
    ),
    "intentional_exact_key_reads": RuleContract(
        "usage_heuristic",
        1,
        windowed=True,
        uncertainty=(
            "Only the structured-by-design types are recognised; other workflows that read by "
            "key on purpose still count in exact_key_store. A read is matched by key, so a "
            "read of another writer's row under a procedure's key counts as one."
        ),
    ),
    "index_backlog": RuleContract(
        "system_state",
        1,
        exceptions=(
            "Non-searchable, high-sensitivity and empty rows are never embedded and are not "
            "counted."
        ),
        uncertainty=(
            "A row whose embedding row was written but not yet merged can read as pending for "
            "a moment, and a row with a vector from another embedder counts as reached."
        ),
    ),
    "hand_rolled_index_rows": RuleContract(
        "usage_heuristic",
        1,
        exceptions=(
            f"Skipped above the index-row scan cap; values are read up to {INDEX_ROW_VALUE_CLIP} "
            "bytes."
        ),
        uncertainty="A row that lists related keys for a person to read matches too.",
    ),
    "double_encoded_values": RuleContract(
        "usage_heuristic",
        1,
        uncertainty="A value meant to hold a JSON document as a string matches too.",
    ),
    "json_prefix_excerpts": RuleContract(
        "usage_heuristic",
        1,
        exceptions=_STRUCTURED_EXCLUDED,
        uncertainty=(
            "The rule checks length and shape, not whether the claim sits inside the embedded "
            "prefix."
        ),
    ),
    "oversized_values": RuleContract(
        "usage_heuristic",
        1,
        exceptions=_STRUCTURED_EXCLUDED,
        uncertainty=(
            "A long value written on purpose matches too; length alone does not make it a document."
        ),
    ),
    "unembedded_rows": RuleContract(
        "contract_violation",
        # 2: a model call the deployment held or could not bill is not the row's error.
        2,
        exceptions=(
            "Non-searchable types are not checked, a row with no recorded embed error is not "
            "counted, and nor is one whose model call was held or unbilled (index_backlog)."
        ),
    ),
    "hash_keys": RuleContract(
        "usage_heuristic",
        1,
        exceptions="A hex id under 40 chars in the middle of a key is not counted.",
        uncertainty="A key that is a content hash by design (a cache keyed by digest) matches too.",
    ),
    "coordination_as_memory": RuleContract(
        "usage_heuristic",
        # 2: a structured-by-design row read often by key is not coordination state.
        2,
        windowed=True,
        exceptions=(
            "A row whose type is structured by design is not counted on its reads; while "
            "telemetry_unavailable warns, only the key-segment arm runs."
        ),
        uncertainty="A short fact read often for a good reason matches the read-count arm.",
    ),
    "episodic_blobs": RuleContract(
        "usage_heuristic",
        1,
        exceptions="Only episodic rows are checked.",
        uncertainty="A long episodic note written once and kept on purpose matches too.",
    ),
    "version_churn": RuleContract(
        "usage_heuristic",
        # 2: a version that keeps the value and its reinforcement count is not counted.
        2,
        exceptions=(
            "A version of the same row (agent, user, session, key) that keeps the value and "
            "the reinforcement count of the one before it is not counted: a status change "
            "(conflict marking, supersession), a tags or confidence edit through "
            "`memory.update_by_id`, a lock or an unpin, and the server's own copies of a "
            "write. A new value counts, as does a restatement of the same value (a "
            "`memory.set` or `memory.pin` of it), a delete and a set of the same value after "
            "it, a value that flips back and forth and a version with no recorded hash."
        ),
        uncertainty=(
            "A fact that really changes often leaves a long history that is not waste, and a "
            "row whose tags are edited over and over stores a copy each time that is not "
            "counted."
        ),
    ),
}


# Sample rows from a `count() OVER ()` query: the aggregate columns ride every row and are split
# off. A value-bearing column is dropped even if a query ever selected one: samples carry keys
# and counts, never content.
_AGG_COLS = frozenset({"total", "total_bytes", "stalled"})
_NEVER_SAMPLED = frozenset({"value", "value_json", "value_preview", "content_excerpt", "query"})


def _grade(
    count: int,
    share: float,
    *,
    critical_share: float | None = None,
    warning_share: float | None = None,
    warning_any: bool = False,
) -> str:
    """Severity from a count and its share of the population checked."""
    if count <= 0:
        return "info"
    if critical_share is not None and share >= critical_share:
        return "critical"
    if warning_share is not None and share >= warning_share:
        return "warning"
    if warning_any:
        return "warning"
    return "info"


def _share(count: int, population: int) -> float:
    if population <= 0 or count <= 0:
        return 0.0
    return min(1.0, count / population)


def _split(rows: list[dict[str, Any]], limit: int) -> tuple[int, int, list[dict[str, Any]]]:
    """(total, total_bytes, samples) from rows carrying the window aggregates."""
    if not rows:
        return 0, 0, []
    total = int(rows[0].get("total") or 0)
    total_bytes = int(rows[0].get("total_bytes") or 0)
    samples = [
        {k: v for k, v in r.items() if k not in _AGG_COLS and k not in _NEVER_SAMPLED}
        for r in rows[:limit]
    ]
    return total, total_bytes, samples


def index_rows(rows: Sequence[dict[str, Any]]) -> list[dict[str, Any]]:
    """Sample rows {memory_key, memory_id, keys_named}, never the value, for each input row
    (memory_key, memory_id, value) whose value names INDEX_ROW_MIN_KEYS or more other keys
    of the set, most keys first. Judged row by row: a key held under several
    identities is an index under one of them only when that row's own value is."""
    keys = {str(r["memory_key"]) for r in rows}
    hits = [
        (n, str(r["memory_key"]), str(r["memory_id"]))
        for r in rows
        if (n := keys_named(str(r["memory_key"]), str(r["value"]), keys)) >= INDEX_ROW_MIN_KEYS
    ]
    hits.sort(key=lambda h: (-h[0], h[1], h[2]))
    return [{"memory_key": k, "memory_id": mid, "keys_named": n} for n, k, mid in hits]


def keys_named(key: str, value: str, keys: set[str]) -> int:
    """How many keys of `keys` other than `key` itself `value` names as a delimited token."""
    return len({tok for tok in _TOKEN_SPLIT_RE.split(value) if tok and tok != key and tok in keys})


class _Audit:
    """One report in progress: the CH client, the bound parameters, the
    summary being filled, the findings and the rules that ran or were skipped."""

    def __init__(self, ch: Any, inp: NamespaceAuditInput) -> None:
        self.ch = ch
        self.inp = inp
        self.params: dict[str, Any] = {
            "org": inp.org,
            "ns": inp.namespace,
            # LIMIT 0 would return no rows, and the totals of a `count() OVER ()` read ride the
            # sample rows: at least one row is read and `add` trims the samples to what was
            # asked for (the health summary asks for none).
            "lim": max(1, int(inp.sample_limit)),
            "lim_past": int(inp.sample_limit) + 1,
            "win": int(inp.window_seconds),
            "hot": int(HOT_KEY_READS),
            "inspectors": list(INSPECTOR_CONSUMERS),
            "ns_types": sorted(NON_SEARCHABLE_TYPES),
            "structured": sorted(STRUCTURED_TYPES),
            "clip": int(EXCERPT_LIMIT),
            "advisory": int(settings.memory_value_advisory_chars),
            "coord_chars": int(COORDINATION_VALUE_CHARS),
            "coord_words": int(COORDINATION_MAX_WORDS),
            "coord_segs": list(COORDINATION_SEGMENTS),
            "episodic_chars": int(EPISODIC_BLOB_CHARS),
            "hash_key_re": _HASH_KEY_SEGMENT_RE,
            "index_clip": int(INDEX_ROW_VALUE_CLIP),
            "grace": int(INDEX_GRACE_SECONDS),
            "lag": int(TELEMETRY_LAG_SECONDS),
            "holds": list(EMBED_HOLDS),
            "state_changes": list(state_change_ops()),
        }
        self.summary = NamespaceAuditSummary()
        self.searchable_rows = 0
        self.episodic_rows = 0
        self.findings: list[NamespaceAuditFinding] = []
        # The summary reads and checks that ran or were skipped; the summary reads are rules
        # of the report too, since most findings are measured against one.
        self.rules = RuleLog()
        # Set by `telemetry_unavailable`: the window's writes left no ledger row.
        self.telemetry_gap = False

    def measured(self, step: str, population: int) -> int | None:
        """`population` when the summary read it comes from ran; None when it was never
        measured."""
        return population if self.rules.has_run(step) else None

    async def rows(self, sql: str) -> list[dict[str, Any]]:
        return list(await self.ch.query_all(sql, params=self.params))

    async def one(self, sql: str) -> dict[str, Any]:
        row = await self.ch.query_one(sql, params=self.params)
        return dict(row) if row else {}

    def add(
        self,
        code: str,
        severity: str,
        count: int,
        share: float,
        samples: list[dict[str, Any]],
        *,
        bytes_: int = 0,
        population: int | None = None,
        samples_complete: bool | None = None,
    ) -> None:
        detail, remedy = _TEXT[code]
        rule = _CONTRACT[code]
        kept = samples[: self.inp.sample_limit]
        self.findings.append(
            NamespaceAuditFinding(
                code=code,
                severity=severity,  # type: ignore[arg-type]
                count=int(count),
                share=float(min(1.0, max(0.0, share))),
                bytes=int(bytes_),
                samples=kept,
                detail=detail,
                remedy=remedy,
                kind=rule.kind,
                rule_version=rule.version,
                source_op="namespace.audit",
                window_seconds=int(self.inp.window_seconds) if rule.windowed else None,
                population=population,
                sample_limit=int(self.inp.sample_limit),
                samples_complete=(
                    int(count) <= len(kept) if samples_complete is None else samples_complete
                ),
                exceptions=rule.exceptions,
                uncertainty=rule.uncertainty,
            )
        )

    def listed_all(self, rows: list[dict[str, Any]]) -> bool:
        """Whether a read that asked for one row past the cap (`lim_past`) kept every row."""
        return len(rows) <= self.inp.sample_limit

    async def guarded(self, name: str, step: Callable[[], Awaitable[None]]) -> None:
        """Run one step; a failure lands in `skipped`, never raises."""
        try:
            await step()
        except Exception as exc:  # noqa: BLE001 — advisory report; one failed check must not sink the rest
            log.warning("namespace.audit %s skipped: %s", name, exc)
            self.rules.skip(name, type(exc).__name__)
            return
        if not self.rules.was_skipped(name):  # a step may skip itself (a scan cap)
            self.rules.ran(name)

    # -- summary ------------------------------------------------------------

    async def summary_live(self) -> None:
        r = await self.one(
            f"""
            SELECT count() AS live_rows, sum(length(value)) AS live_bytes,
                   countIf({_SEARCHABLE}) AS searchable_rows,
                   countIf(memory_type = 'episodic') AS episodic_rows,
                   -- created in the window: not revised (a worker's relabel keeps created_at)
                   -- and not copied in (a copy's call is ledgered under no namespace)
                   countIf(created_at >= now() - INTERVAL {{win:UInt32}} SECOND
                           AND created_at < now() - INTERVAL {{lag:UInt32}} SECOND
                           AND cross_promoted_from_namespace = '')
                       AS written_in_window
            {_LIVE}
            """
        )
        self.summary.written_in_window = int(r.get("written_in_window") or 0)
        self.summary.live_rows = int(r.get("live_rows") or 0)
        self.summary.live_bytes = int(r.get("live_bytes") or 0)
        self.searchable_rows = int(r.get("searchable_rows") or 0)
        self.episodic_rows = int(r.get("episodic_rows") or 0)

    async def summary_embeddings(self) -> None:
        r = await self.one(
            f"""
            SELECT sum(length(content_excerpt)) AS excerpt_bytes,
                   countIf({_UNEMBEDDED_ROW}) AS unembedded_rows
            FROM loom.memory_embeddings FINAL
            WHERE {_SCOPE} AND tombstoned_at IS NULL
            """
        )
        self.summary.excerpt_bytes = int(r.get("excerpt_bytes") or 0)
        self.summary.unembedded_rows = int(r.get("unembedded_rows") or 0)

    async def summary_ledger(self) -> None:
        r = await self.one(
            f"""
            -- a request row: a worker's own row (an extractor's spend) says nothing about
            -- the callers; an inspector's write counts, its reads and page views do not
            SELECT countIf(operation NOT IN {_REPORT_OPS} AND kind != 'worker'
                           AND ({_AGENT_CALL}
                                OR operation IN {{state_changes:Array(String)}})) AS ledger_calls,
                   countIf(operation IN {_LEDGER_GETS} AND {_AGENT_CALL}) AS gets,
                   countIf(operation IN {_LEDGER_GETS} AND {_AGENT_CALL}
                           AND memory_key IN ({_TYPED_KEYS})) AS typed_gets,
                   countIf(operation IN {_LEDGER_SEARCHES} AND {_AGENT_CALL}) AS searches,
                   -- a write from the dashboard is a person using the namespace
                   countIf(operation IN {_LEDGER_SETS}) AS sets,
                   countIf(NOT ({_AGENT_CALL}) AND (operation IN {_LEDGER_GETS}
                           OR operation IN {_LEDGER_SEARCHES})) AS inspector_calls
            {_LEDGER_ALL}
            """
        )
        s = self.summary
        s.ledger_calls = int(r.get("ledger_calls") or 0)
        s.typed_gets = int(r.get("typed_gets") or 0)
        s.gets = int(r.get("gets") or 0)
        s.searches = int(r.get("searches") or 0)
        s.sets = int(r.get("sets") or 0)
        s.inspector_calls = int(r.get("inspector_calls") or 0)
        # The object-store gauge: reads of procedures are a workflow, not key-value use.
        s.get_search_ratio = round((s.gets - s.typed_gets) / max(s.searches, 1), 2)

    # -- checks -------------------------------------------------------------

    def ledger_missing(self, code: str) -> bool:
        """Skip a ledger check, saying why, when the ledger holds no call for the window's
        writes (`telemetry_unavailable`): its counts would read as a clean namespace."""
        if not self.telemetry_gap:
            return False
        self.rules.skip(code, "telemetry_unavailable")
        return True

    async def telemetry_unavailable(self) -> None:
        s = self.summary
        # A window no longer than the ledger lag holds no row old enough to judge.
        if int(self.inp.window_seconds) <= TELEMETRY_LAG_SECONDS:
            self.rules.skip("telemetry_unavailable", "window within the ledger lag")
            return
        # Judged only on both summary reads: a failed one is not a missing ledger.
        if not (self.rules.has_run("summary_live") and self.rules.has_run("summary_ledger")):
            self.rules.skip("telemetry_unavailable", "summary read failed")
            return
        # Any call but a report on the namespace or an inspector's read: the audit's own row,
        # or the dashboard looking, would otherwise hide that the window went unrecorded.
        self.telemetry_gap = s.written_in_window > 0 and s.ledger_calls == 0
        count = s.written_in_window if self.telemetry_gap else 0
        self.add(
            "telemetry_unavailable",
            "warning" if self.telemetry_gap else "info",
            count,
            _share(count, s.live_rows),
            [],
            population=s.live_rows,
        )

    async def intentional_exact_key_reads(self) -> None:
        s = self.summary
        if self.ledger_missing("intentional_exact_key_reads"):
            return
        self.add(
            "intentional_exact_key_reads",
            "info",
            s.typed_gets,
            _share(s.typed_gets, s.gets),
            [],
            population=self.measured("summary_ledger", s.gets),
        )

    async def index_backlog(self) -> None:
        rows = await self.rows(
            f"""
            SELECT count() OVER () AS total,
                   countIf(age_seconds >= {{grace:UInt32}}) OVER () AS stalled,
                   memory_key, memory_id, age_seconds
            FROM (SELECT a.memory_key AS memory_key, toString(a.memory_id) AS memory_id,
                         dateDiff('second', a.updated_at, now()) AS age_seconds
                  FROM (SELECT agent, user_id, session_id, memory_key, memory_id, updated_at
                        {_LIVE} AND {_SEARCHABLE} AND sensitivity != 'high'
                          AND value_len > 0) AS a
                  -- reached: a vector, or an error of the row's own (a held or unbilled
                  -- model call is the deployment's, and still waiting)
                  LEFT ANTI JOIN (SELECT agent, user_id, session_id, memory_key
                                  FROM loom.memory_embeddings FINAL
                                  WHERE {_SCOPE} AND tombstoned_at IS NULL
                                    AND (embedding_dim > 0 OR (embed_last_error != ''
                                         AND embed_last_error NOT IN {{holds:Array(String)}}))
                                 ) AS b
                    ON a.agent = b.agent AND a.user_id = b.user_id
                   AND a.session_id = b.session_id AND a.memory_key = b.memory_key)
            ORDER BY age_seconds DESC, memory_key
            LIMIT {{lim:UInt32}}
            """
        )
        total, _bytes, samples = _split(rows, self.inp.sample_limit)
        stalled = int(rows[0].get("stalled") or 0) if rows else 0
        self.add(
            "index_backlog",
            "warning" if stalled else "info",
            total,
            _share(total, self.searchable_rows),
            samples,
            population=self.measured("summary_live", self.searchable_rows),
        )

    async def exact_key_store(self) -> None:
        s = self.summary
        if self.ledger_missing("exact_key_store"):
            return
        # Complete only when the hot keys account for every get, both counted in one scan.
        # Reads of structured-by-design rows are a workflow (intentional_exact_key_reads).
        rows = await self.rows(
            f"""
            SELECT memory_key, reads, window_gets, sum(reads) OVER () AS hot_reads
            FROM (SELECT memory_key, count() AS reads, sum(count()) OVER () AS window_gets
                  {_LEDGER} AND operation IN {_LEDGER_GETS}
                    AND memory_key NOT IN ({_TYPED_KEYS})
                  GROUP BY memory_key)
            WHERE {_HOT_KEY}
            ORDER BY reads DESC, memory_key LIMIT {{lim_past:UInt32}}
            """
        )
        samples = [{"memory_key": r["memory_key"], "reads": int(r["reads"])} for r in rows]
        gets = s.gets - s.typed_gets
        # A failed ledger summary leaves `gets` at 0, which no listing can be complete for.
        if not self.rules.has_run("summary_ledger"):
            covered = False
        elif rows:
            covered = int(rows[0]["hot_reads"]) >= int(rows[0]["window_gets"])
        else:
            covered = gets == 0
        critical = gets >= EXACT_KEY_STORE_MIN_GETS and gets >= EXACT_KEY_STORE_RATIO * s.searches
        severity = "critical" if critical else _grade(gets, 0.0)
        self.add(
            "exact_key_store",
            severity,
            gets,
            _share(gets, gets + s.searches),
            samples,
            population=self.measured("summary_ledger", gets + s.searches),
            samples_complete=self.listed_all(rows) and covered,
        )

    async def hand_rolled_index_rows(self) -> None:
        live = self.summary.live_rows
        if live > settings.namespace_audit_index_row_scan_cap:
            self.rules.skip(
                "hand_rolled_index_rows",
                f"{live} live rows exceed the "
                f"{settings.namespace_audit_index_row_scan_cap}-row scan cap",
            )
            return
        rows = await self.rows(
            "SELECT memory_key, toString(memory_id) AS memory_id, "
            f"substring(value, 1, {{index_clip:UInt32}}) AS value {_LIVE}"
        )
        samples = index_rows(rows)
        share = _share(len(samples), live)
        self.add(
            "hand_rolled_index_rows",
            _grade(len(samples), share, warning_any=True),
            len(samples),
            share,
            samples,
            population=self.measured("summary_live", live),
        )

    async def _value_check(
        self,
        code: str,
        predicate: str,
        population: int,
        *,
        critical_share: float | None = None,
        warning_share: float | None = None,
        warning_any: bool = False,
    ) -> None:
        """One FINAL-scoped read of loom.memory: count, bytes and the largest
        matching keys, with `count() OVER ()` riding the sample rows."""
        rows = await self.rows(
            f"""
            SELECT count() OVER () AS total, sum(length(value)) OVER () AS total_bytes,
                   memory_key, toString(memory_id) AS memory_id, length(value) AS value_chars
            {_LIVE} AND {predicate}
            ORDER BY value_chars DESC, memory_key
            LIMIT {{lim:UInt32}}
            """
        )
        total, total_bytes, samples = _split(rows, self.inp.sample_limit)
        share = _share(total, population)
        severity = _grade(
            total,
            share,
            critical_share=critical_share,
            warning_share=warning_share,
            warning_any=warning_any,
        )
        measured = self.measured("summary_live", population)
        self.add(code, severity, total, share, samples, bytes_=total_bytes, population=measured)

    async def double_encoded_values(self) -> None:
        await self._value_check(
            "double_encoded_values",
            _IS_JSON_STRING_OF_JSON,
            self.summary.live_rows,
            critical_share=DOUBLE_ENCODED_CRITICAL_SHARE,
            warning_any=True,
        )

    async def json_prefix_excerpts(self) -> None:
        await self._value_check(
            "json_prefix_excerpts",
            f"{_SEARCHABLE} AND {_NOT_STRUCTURED} AND length(value) > {{clip:UInt32}} "
            f"AND ({_IS_JSON_CONTAINER} OR ({_IS_JSON_STRING_OF_JSON}))",
            self.searchable_rows,
            warning_share=JSON_PREFIX_WARNING_SHARE,
        )

    async def oversized_values(self) -> None:
        await self._value_check(
            "oversized_values",
            f"{_SEARCHABLE} AND {_NOT_STRUCTURED} AND length(value) > {{advisory:UInt32}}",
            self.searchable_rows,
            warning_any=True,
        )

    async def hash_keys(self) -> None:
        await self._value_check(
            "hash_keys",
            "match(memory_key, {hash_key_re:String})",
            self.summary.live_rows,
            warning_share=HASH_KEY_WARNING_SHARE,
        )

    async def episodic_blobs(self) -> None:
        await self._value_check(
            "episodic_blobs",
            "memory_type = 'episodic' AND length(value) > {episodic_chars:UInt32}",
            self.episodic_rows,
            warning_any=True,
        )

    async def unembedded_rows(self) -> None:
        rows = await self.rows(
            f"""
            SELECT count() OVER () AS total, memory_key, agent, user_id, session_id,
                   substring(splitByChar(':', embed_last_error)[1], 1, 80) AS error_class
            FROM loom.memory_embeddings FINAL
            WHERE {_SCOPE} AND tombstoned_at IS NULL AND {_UNEMBEDDED_ROW}
            ORDER BY memory_key
            LIMIT {{lim:UInt32}}
            """
        )
        total, _bytes, samples = _split(rows, self.inp.sample_limit)
        share = _share(total, self.searchable_rows)
        self.add(
            "unembedded_rows",
            _grade(total, share, warning_any=True),
            total,
            share,
            samples,
            population=self.measured("summary_live", self.searchable_rows),
        )

    async def coordination_as_memory(self) -> None:
        # With no ledger for the window the read-count arm has nothing to count: the
        # statement reads only loom.memory and runs the key-segment arm alone.
        if self.telemetry_gap:
            reads, hot_join, read_arm = "0", "", "0"
        else:
            reads = "l.reads"
            hot_join = f"LEFT JOIN ({_HOT_KEYS}) AS l ON m.memory_key = l.memory_key"
            read_arm = """(length(m.value) < {coord_chars:UInt32}
                    AND length(splitByWhitespace(m.value)) < {coord_words:UInt32}
                    AND l.reads >= {hot:UInt32}
                    -- a structured row is read back whole by key by design
                    AND m.memory_type NOT IN {structured:Array(String)})"""
        rows = await self.rows(
            f"""
            SELECT count() OVER () AS total, sum(length(m.value)) OVER () AS total_bytes,
                   m.memory_key AS memory_key, m.memory_id AS memory_id,
                   length(m.value) AS value_chars, {reads} AS reads
            FROM (SELECT memory_key, toString(memory_id) AS memory_id, value, memory_type
                  {_LIVE}) AS m
            {hot_join}
            WHERE {read_arm}
               OR hasAny(splitByChar('/', lower(m.memory_key)), {{coord_segs:Array(String)}})
            ORDER BY reads DESC, memory_key
            LIMIT {{lim:UInt32}}
            """
        )
        total, total_bytes, samples = _split(rows, self.inp.sample_limit)
        share = _share(total, self.summary.live_rows)
        self.add(
            "coordination_as_memory",
            _grade(total, share, warning_any=True),
            total,
            share,
            samples,
            bytes_=total_bytes,
            population=self.measured("summary_live", self.summary.live_rows),
        )

    async def version_churn(self) -> None:
        rows = await self.rows(
            f"""
            SELECT sum(versions) OVER () AS total, sum(bytes) OVER () AS total_bytes,
                   memory_key, versions, bytes
            FROM (SELECT memory_key, count() AS versions, sum(chars) AS bytes
                  -- a version that keeps the value and its reinforcement count of the one
                  -- before it (a status change, a tags edit by id, a lock, the server's
                  -- own copies of a write) rewrites nothing; a restatement (a set or pin
                  -- of the same value) bumps the count
                  FROM (SELECT memory_key, value_len AS chars,
                               -- a version with no recorded hash (the column's '0'
                               -- default) is not known to repeat, so it counts
                               content_sha256 = toFixedString('0', 64)
                               OR content_sha256 != lagInFrame(content_sha256) OVER w
                               OR reinforcement_count != lagInFrame(reinforcement_count) OVER w
                               -- a delete, and a set after one, are the writer's
                               OR isNull(tombstoned_at)
                                   != lagInFrame(isNull(tombstoned_at)) OVER w AS rewrote
                        FROM loom.memory_versions FINAL WHERE {_SCOPE}
                        -- the table's whole sort key, so the window reads in its order
                        WINDOW w AS (PARTITION BY org, namespace, agent, user_id, session_id,
                                                  memory_key
                                     ORDER BY version
                                     ROWS BETWEEN 1 PRECEDING AND CURRENT ROW))
                  WHERE rewrote
                  GROUP BY memory_key)
            ORDER BY bytes DESC, memory_key
            LIMIT {{lim_past:UInt32}}
            -- reading in window order halves the memory of the version scan
            SETTINGS optimize_read_in_window_order = 1
            """
        )
        total, total_bytes, samples = _split(rows, self.inp.sample_limit)
        s = self.summary
        s.version_rows, s.version_bytes = total, total_bytes
        churn = s.live_rows > 0 and (
            total >= VERSION_CHURN_ROWS_RATIO * s.live_rows
            or total_bytes >= VERSION_CHURN_BYTES_RATIO * s.live_bytes
        )
        # Share: the part of the version history that is not the live head.
        share = _share(total - s.live_rows, total)
        severity = "warning" if churn else _grade(total, share)
        self.add(
            "version_churn",
            severity,
            total,
            share,
            samples,
            bytes_=total_bytes,
            population=total,
            samples_complete=self.listed_all(rows),
        )


# Rules of the report too: most findings' share and population come from one of these
# reads; a skipped one leaves the population null.
_SUMMARY_STEPS: tuple[str, ...] = ("summary_live", "summary_embeddings", "summary_ledger")


def new_audit(inp: NamespaceAuditInput, ch: Any = None) -> _Audit:
    """One report in progress over `ch` (the per-call client when omitted);
    `run_checks` fills it."""
    return _Audit(ch if ch is not None else get_client(), inp)


async def run_checks(
    audit: _Audit,
    codes: Iterable[str],
    *,
    summary_steps: Iterable[str] = _SUMMARY_STEPS,
) -> None:
    """Fill `audit` with the summary steps then the named checks, each one
    guarded. Summary first: the checks' populations come from it. Checks
    run sequentially on the per-call client, like namespace.validate. The
    full op runs every code; the health summary runs the five cheap, decisive
    ones over the two summary reads they need."""
    for name in summary_steps:
        await audit.guarded(name, getattr(audit, name))
    for code in codes:
        await audit.guarded(code, getattr(audit, code))


async def handle_namespace_audit(input: NamespaceAuditInput) -> NamespaceAuditOutput:
    # Taken before the reads, to the second as updated_at is: a cited row updated at or
    # after it may have been revised mid-report.
    computed_at = naive_utc_now_seconds()
    audit = new_audit(input)
    await run_checks(audit, FINDING_CODES)
    await attach_evidence(input.org, input.namespace, audit.findings, audit.rules, ch=audit.ch)

    criticals = sum(1 for f in audit.findings if f.severity == "critical")
    warnings = sum(1 for f in audit.findings if f.severity == "warning")
    infos = sum(1 for f in audit.findings if f.severity == "info")
    return NamespaceAuditOutput(
        org=input.org,
        namespace=input.namespace,
        computed_at=computed_at,
        window_seconds=int(input.window_seconds),
        summary=audit.summary,
        findings=audit.findings,
        skipped=audit.rules.skipped,
        coverage=audit.rules.coverage,
        healthy=criticals == 0,
        criticals=criticals,
        warnings=warnings,
        infos=infos,
    )


_KIND_LABEL = {
    "contract_violation": "contract",
    "usage_heuristic": "heuristic",
    "system_state": "system",
}


def render_table(out: NamespaceAuditOutput) -> str:
    """The CLI table: summary line, one row per finding (code, severity, kind,
    count, share, first sample), then the skipped rules."""
    s = out.summary
    lines = [
        f"namespace.audit {out.org}/{out.namespace} — window {out.window_seconds // 86400}d, "
        f"{'healthy' if out.healthy else 'UNHEALTHY'} "
        f"({out.criticals} critical, {out.warnings} warning, {out.infos} info)",
        f"  live {s.live_rows} rows / {s.live_bytes:,} B; versions {s.version_rows} rows / "
        f"{s.version_bytes:,} B; excerpts {s.excerpt_bytes:,} B; unembedded {s.unembedded_rows}; "
        f"gets {s.gets} / searches {s.searches} / sets {s.sets} (ratio {s.get_search_ratio})"
        + (
            f"; {s.inspector_calls} dashboard and assistant calls not counted"
            if s.inspector_calls
            else ""
        ),
        "",
        f"  {'code':<28} {'severity':<9} {'kind':<9} {'count':>8} {'share':>6}  first sample",
    ]
    for f in out.findings:
        first = "-"
        if f.samples:
            # memory_id only joins a sample to its evidence entry
            first = ", ".join(f"{k}={v}" for k, v in f.samples[0].items() if k != "memory_id")
        kind = _KIND_LABEL[f.kind]
        lines.append(
            f"  {f.code:<28} {f.severity:<9} {kind:<9} {f.count:>8} {f.share:>6.0%}  {first}"
        )
    for sk in out.skipped:
        lines.append(f"  skipped: {sk}")
    return "\n".join(lines)
