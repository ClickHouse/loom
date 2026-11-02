"""namespace.audit — how a namespace is being used, with the anti-patterns named.

`namespace.validate` reports the integrity of what was written (two tables disagreeing, a
vector with no memory); this op reports HOW the namespace is being used. An agent that treats
`memory.set` as an object store (hash keys, double-encoded JSON, one lease key read a hundred
times, hand-rolled index rows, 1 MB blobs whose claim falls past the embed clip) leaves every
table consistent and recall useless. Each of those facts is derivable from `loom.memory`,
`loom.memory_embeddings`, `loom.memory_versions`, `loom.search_trace_log` and
`loom.agent_events` with SQL; this op says it, so the namespace owner can run it and the
optimize skill can act on it.

Ten checks, each one read over existing tables scoped to (org, namespace).
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
# exact_key_store: gets ≥ RATIO × searches AND gets ≥ MIN_GETS is critical
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

# Over loom.memory_embeddings: a searchable row the worker gave up on (summary and check).
_UNEMBEDDED_ROW = f"embedding_dim = 0 AND embed_last_error != '' AND {_SEARCHABLE}"
# Remedies point at the recipe: docs/guides/investigation-agent-memory.md.
_RECIPE = "docs/guides/investigation-agent-memory.md"

# code: (detail, remedy). One sentence each — what the check found means,
# and what to do about it.
_TEXT: dict[str, tuple[str, str]] = {
    "exact_key_store": (
        "Exact-key reads (memory.get, memory.bulk_get) counted against searches "
        "(memory.search, memory.ask) in the window; critical once reads reach "
        f"{EXACT_KEY_STORE_MIN_GETS} and {EXACT_KEY_STORE_RATIO:g}× the searches — the "
        "namespace is then an object store, and nothing in it is recalled by meaning.",
        "Write readable keys and recall with `memory.search` (tags carry the facets); "
        "keep leases and pointers in `working_memory`, not `memory.set`. "
        f"See {_RECIPE}.",
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
        "Searchable rows the embedding worker gave up on (no vector, a recorded error): "
        "invisible to memory.search however well the key is chosen.",
        "Shorten or split the value (`knowledge.upsert` for documents), then memory.set "
        "the row again; `namespace.validate` lists the same rows under `unembedded`.",
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
    "exact_key_store": RuleContract(
        "usage_heuristic",
        1,
        windowed=True,
        uncertainty=(
            "A workflow that reads known keys on purpose shows the same ratio, and the ledger "
            "counts calls, not whether a result reached the model."
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
        1,
        exceptions=(
            "Non-searchable types are not checked, and a row with no recorded embed error is "
            "not counted."
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
        1,
        windowed=True,
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
        1,
        uncertainty="A fact that really changes often leaves a long history that is not waste.",
    ),
}


# Sample rows from a `count() OVER ()` query: the aggregate columns ride every row and are split
# off. A value-bearing column is dropped even if a query ever selected one: samples carry keys
# and counts, never content.
_AGG_COLS = frozenset({"total", "total_bytes"})
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
        }
        self.summary = NamespaceAuditSummary()
        self.searchable_rows = 0
        self.episodic_rows = 0
        self.findings: list[NamespaceAuditFinding] = []
        # The summary reads and checks that ran or were skipped; the summary reads are rules
        # of the report too, since most findings are measured against one.
        self.rules = RuleLog()

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
                   countIf(memory_type = 'episodic') AS episodic_rows
            {_LIVE}
            """
        )
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
            SELECT countIf(operation IN {_LEDGER_GETS} AND {_AGENT_CALL}) AS gets,
                   countIf(operation IN {_LEDGER_SEARCHES} AND {_AGENT_CALL}) AS searches,
                   -- a write from the dashboard is a person using the namespace
                   countIf(operation IN {_LEDGER_SETS}) AS sets,
                   countIf(NOT ({_AGENT_CALL}) AND (operation IN {_LEDGER_GETS}
                           OR operation IN {_LEDGER_SEARCHES})) AS inspector_calls
            {_LEDGER_ALL}
            """
        )
        s = self.summary
        s.gets = int(r.get("gets") or 0)
        s.searches = int(r.get("searches") or 0)
        s.sets = int(r.get("sets") or 0)
        s.inspector_calls = int(r.get("inspector_calls") or 0)
        s.get_search_ratio = round(s.gets / max(s.searches, 1), 2)

    # -- checks -------------------------------------------------------------

    async def exact_key_store(self) -> None:
        s = self.summary
        # Complete only when the hot keys account for every get, both counted in one scan.
        rows = await self.rows(
            f"""
            SELECT memory_key, reads, window_gets, sum(reads) OVER () AS hot_reads
            FROM (SELECT memory_key, count() AS reads, sum(count()) OVER () AS window_gets
                  {_LEDGER} AND operation IN {_LEDGER_GETS}
                  GROUP BY memory_key)
            WHERE {_HOT_KEY}
            ORDER BY reads DESC, memory_key LIMIT {{lim_past:UInt32}}
            """
        )
        samples = [{"memory_key": r["memory_key"], "reads": int(r["reads"])} for r in rows]
        # A failed ledger summary leaves `gets` at 0, which no listing can be complete for.
        if not self.rules.has_run("summary_ledger"):
            covered = False
        elif rows:
            covered = int(rows[0]["hot_reads"]) >= int(rows[0]["window_gets"])
        else:
            covered = s.gets == 0
        critical = (
            s.gets >= EXACT_KEY_STORE_MIN_GETS and s.gets >= EXACT_KEY_STORE_RATIO * s.searches
        )
        severity = "critical" if critical else _grade(s.gets, 0.0)
        self.add(
            "exact_key_store",
            severity,
            s.gets,
            _share(s.gets, s.gets + s.searches),
            samples,
            population=self.measured("summary_ledger", s.gets + s.searches),
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
        rows = await self.rows(
            f"""
            SELECT count() OVER () AS total, sum(length(m.value)) OVER () AS total_bytes,
                   m.memory_key AS memory_key, m.memory_id AS memory_id,
                   length(m.value) AS value_chars, l.reads AS reads
            FROM (SELECT memory_key, toString(memory_id) AS memory_id, value {_LIVE}) AS m
            LEFT JOIN ({_HOT_KEYS}) AS l ON m.memory_key = l.memory_key
            WHERE (length(m.value) < {{coord_chars:UInt32}}
                    AND length(splitByWhitespace(m.value)) < {{coord_words:UInt32}}
                    AND l.reads >= {{hot:UInt32}})
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
            FROM (SELECT memory_key, count() AS versions, sum(length(value)) AS bytes
                  FROM loom.memory_versions FINAL WHERE {_SCOPE}
                  GROUP BY memory_key)
            ORDER BY bytes DESC, memory_key
            LIMIT {{lim_past:UInt32}}
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


_KIND_LABEL = {"contract_violation": "contract", "usage_heuristic": "heuristic"}


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
        f"  {'code':<24} {'severity':<9} {'kind':<9} {'count':>8} {'share':>6}  first sample",
    ]
    for f in out.findings:
        first = "-"
        if f.samples:
            # memory_id only joins a sample to its evidence entry
            first = ", ".join(f"{k}={v}" for k, v in f.samples[0].items() if k != "memory_id")
        kind = _KIND_LABEL[f.kind]
        lines.append(
            f"  {f.code:<24} {f.severity:<9} {kind:<9} {f.count:>8} {f.share:>6.0%}  {first}"
        )
    for sk in out.skipped:
        lines.append(f"  skipped: {sk}")
    return "\n".join(lines)
