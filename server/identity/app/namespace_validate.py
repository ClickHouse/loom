"""namespace.validate — one read-only conformance report for a namespace.

The integrity gauges in `server/observability/otel_probes.py` tell an OPERATOR that
something is wrong somewhere in the cluster. Nothing told the AGENT that
just finished writing whether it left its own namespace consistent. This
handler is that call: the same class of invariants, scoped to one
(org, namespace), returned with samples the caller can act on.

Fourteen checks, each an independent query over existing tables. No mutation,
no new storage. A check that fails to run is reported in `skipped` and the
rest still return — the report closes a task, it must never fail one. Under
Postgres the checks start once ClickHouse holds what the namespace committed
(`pg_validation.published_reads`); a wait that runs out is reported as `projection`
in `skipped` and `coverage`.

Severity is fixed per check, not per row:

  error   — a storage invariant is violated (two tables disagree about one
            fact; a vector with no memory; a memory with no vector past the
            worker's grace). Readers see different truths depending on which
            table they hit.
  warning — a lifecycle state that nothing will resolve on its own (a
            conflict nobody reviewed, a duplicate whose canonical is gone, a
            fact whose stated validity has ended but is still active, an
            attestation the content has outrun).
  info    — a quality signal (an unsourced, low-confidence row: the agent's
            own inference stored as if it were observed).
"""

from __future__ import annotations

import functools
import logging
from collections.abc import Awaitable, Callable
from typing import Any

from server.core.ch import get_client
from server.core.datetimes import naive_utc_now_seconds
from server.identity.api.models_namespace_catalog import (
    NamespaceValidateInput,
    NamespaceValidateOutput,
    _NamespaceValidateFinding,
)
from server.identity.app.report_rules import (
    PROJECTION,
    RuleContract,
    RuleLog,
    attach_evidence,
)
from server.memory.app.search import pg_validation

log = logging.getLogger(__name__)


# Full tenancy identity. Joining loom.memory to loom.memory_embeddings on
# anything coarser produces a cross product (the gauge measured 16x
# inflation on (org, namespace, memory_key) alone).
_IDENT = "org, namespace, agent, user_id, session_id, memory_key"
_IDENT_JOIN = (
    "a.org = b.org AND a.namespace = b.namespace AND a.agent = b.agent "
    "AND a.user_id = b.user_id AND a.session_id = b.session_id "
    "AND a.memory_key = b.memory_key"
)

# Every sample query returns `total` (count() OVER (), computed before LIMIT)
# alongside up to `lim` sample rows. One round-trip per check.
# A report scoped to the keys one session wrote. With no keys the predicate is a constant
# `empty([])` ClickHouse folds away, so the unscoped report costs nothing extra; with keys,
# `memory_key IN` is a secondary-key filter inside the namespace. Every check that reads
# loom.memory or loom.memory_embeddings carries it.
_KEYS = "(empty({keys:Array(String)}) OR memory_key IN {keys:Array(String)})"
_LIVE = (
    "FROM loom.memory FINAL WHERE org = {org:String} AND namespace = {ns:String} "
    f"AND tombstoned_at IS NULL AND {_KEYS}"
)

# Time cues a fact's text can carry without the extractor having resolved an
# event date for it. Deliberately narrow (relative phrases, month names,
# four-digit years): a broad list would report every "then" as a finding.
_TIME_CUE_RE = (
    r"\b(yesterday|tomorrow|tonight|recently|last (week|month|year|weekend|night|"
    r"monday|tuesday|wednesday|thursday|friday|saturday|sunday)|next (week|month|year)|"
    r"\d+ (days?|weeks?|months?|years?) ago|(in|since|by|on) (january|february|march|"
    r"april|may|june|july|august|september|october|november|december)\b|"
    r"\b(19|20)\d\d)\b"
)

_CHECKS: dict[str, tuple[str, str, str]] = {
    # name: (severity, detail, sql)
    "divergent_entities": (
        "error",
        "loom.memory and loom.memory_embeddings disagree about a row's entities; "
        "the graph leg and the vector leg see different facts. Re-emit the row "
        "(memory.update_by_id) so both tables carry the same entity list.",
        f"""
        SELECT count() OVER () AS total, a.memory_key AS memory_key,
               toString(a.memory_id) AS memory_id,
               a.ents AS memory_entities, b.ents AS embedding_entities
        FROM (SELECT {_IDENT}, memory_id, arraySort(entities) AS ents {_LIVE}) AS a
        INNER JOIN (SELECT {_IDENT}, arraySort(entities) AS ents
                    FROM loom.memory_embeddings FINAL
                    WHERE org = {{org:String}} AND namespace = {{ns:String}}
                      AND tombstoned_at IS NULL AND {_KEYS}) AS b
          ON {_IDENT_JOIN}
        WHERE a.ents != b.ents
        ORDER BY memory_key
        LIMIT {{lim:UInt32}}
        """,
    ),
    "divergent_status": (
        "error",
        "loom.memory and loom.memory_embeddings disagree about a row's status "
        "under the SAME identity (a fact superseded or conflicted in memory "
        "but still active in the pool memory.search reads, or the reverse): "
        "the reader is handed a retired fact as current. Run "
        "`loom reconcile-status --org … --namespace …` to re-emit the mirror rows, "
        "or memory.reindex to rebuild the embedding table from loom.memory.",
        f"""
        WITH m AS (
            SELECT {_IDENT}, argMax(status, version) AS ms,
                   argMax(memory_id, version) AS mid
            FROM loom.memory
            WHERE org = {{org:String}} AND namespace = {{ns:String}} AND tombstoned_at IS NULL
              AND {_KEYS}
            GROUP BY {_IDENT}),
        e AS (
            SELECT {_IDENT}, argMax(status, version) AS es
            FROM loom.memory_embeddings
            WHERE org = {{org:String}} AND namespace = {{ns:String}} AND tombstoned_at IS NULL
              AND {_KEYS}
            GROUP BY {_IDENT})
        SELECT count() OVER () AS total, m.memory_key AS memory_key,
               toString(m.mid) AS memory_id, m.session_id AS session_id, m.ms AS memory_status,
               e.es AS embedding_status
        FROM m INNER JOIN e USING ({_IDENT})
        WHERE m.ms != e.es
        ORDER BY memory_key, session_id
        LIMIT {{lim:UInt32}}
        """,
    ),
    "orphan_vectors": (
        "error",
        "A vector whose memory row is gone: search can return a hit that "
        "resolves to nothing. memory.reindex rebuilds the embedding table "
        "from loom.memory.",
        f"""
        SELECT count() OVER () AS total, a.memory_key AS memory_key,
               a.created_at AS created_at
        FROM (SELECT {_IDENT}, created_at FROM loom.memory_embeddings FINAL
              WHERE org = {{org:String}} AND namespace = {{ns:String}}
                AND tombstoned_at IS NULL AND embedding_dim > 0 AND {_KEYS}) AS a
        LEFT ANTI JOIN (SELECT {_IDENT} {_LIVE}) AS b
          ON {_IDENT_JOIN}
        ORDER BY memory_key
        LIMIT {{lim:UInt32}}
        """,
    ),
    "unembedded": (
        "error",
        "A live memory with no vector past the embedding worker's grace "
        "window: it is invisible to semantic search. Check the worker "
        "(embed_last_error on the embedding row) or call memory.reindex.",
        f"""
        SELECT count() OVER () AS total, a.memory_key AS memory_key,
               toString(a.memory_id) AS memory_id,
               dateDiff('second', a.created_at, now()) AS age_seconds
        FROM (SELECT {_IDENT}, memory_id, created_at {_LIVE}
              -- a high row is never embedded, by design
              AND sensitivity != 'high'
              AND created_at < now() - INTERVAL {{grace:UInt32}} SECOND) AS a
        LEFT ANTI JOIN (SELECT {_IDENT} FROM loom.memory_embeddings FINAL
                        WHERE org = {{org:String}} AND namespace = {{ns:String}}
                          AND tombstoned_at IS NULL AND embedding_dim > 0 AND {_KEYS}) AS b
          ON {_IDENT_JOIN}
        ORDER BY age_seconds DESC
        LIMIT {{lim:UInt32}}
        """,
    ),
    "conflicted_aging": (
        "warning",
        "A write flagged as semantically colliding with an existing fact, "
        "still unresolved past conflict_age_seconds. Resolve it with "
        "conflicts.resolve (keep / replace / allow) or in the dashboard's Review queue.",
        f"""
        SELECT count() OVER () AS total, memory_key,
               toString(memory_id) AS memory_id,
               dateDiff('day', updated_at, now()) AS age_days,
               length(conflict_with) AS peers
        {_LIVE} AND status = 'conflicted'
          AND updated_at < now() - INTERVAL {{age:UInt32}} SECOND
        ORDER BY updated_at ASC
        LIMIT {{lim:UInt32}}
        """,
    ),
    "orphan_duplicates": (
        "warning",
        "A row marked duplicate whose canonical is no longer a live active "
        "memory: it defers to nothing and never surfaces. "
        "memory.resolve_orphan_duplicates re-activates these.",
        f"""
        SELECT count() OVER () AS total, a.memory_key AS memory_key,
               toString(a.memory_id) AS memory_id, a.duplicate_of AS duplicate_of
        FROM (SELECT memory_key, memory_id, duplicate_of {_LIVE} AND status = 'duplicate') AS a
        LEFT ANTI JOIN (SELECT toString(memory_id) AS mid {_LIVE}
                        AND status = 'active') AS b
          ON a.duplicate_of = b.mid
        ORDER BY memory_key
        LIMIT {{lim:UInt32}}
        """,
    ),
    "superseded_dangling": (
        "warning",
        "A superseded row whose successor is empty or no longer live: the "
        "supersession chain is broken and knowledge_time replay cannot follow "
        "it. memory.restore reactivates the row if the successor is gone for good.",
        f"""
        SELECT count() OVER () AS total, a.memory_key AS memory_key,
               toString(a.memory_id) AS memory_id, a.superseded_by AS superseded_by
        FROM (SELECT memory_key, memory_id, superseded_by {_LIVE}
              AND status = 'superseded') AS a
        LEFT ANTI JOIN (SELECT toString(memory_id) AS mid {_LIVE}
                        AND status = 'active') AS b
          ON a.superseded_by = b.mid
        ORDER BY memory_key
        LIMIT {{lim:UInt32}}
        """,
    ),
    "stale_valid_to": (
        "warning",
        "An active fact whose own stated validity (valid_to) has ended. It "
        "is still ranked as current. Write the successor fact, or tombstone "
        "it, so as-of reads and live reads agree.",
        f"""
        SELECT count() OVER () AS total, memory_key,
               toString(memory_id) AS memory_id, valid_to,
               dateDiff('day', valid_to, now()) AS days_past
        {_LIVE} AND status = 'active' AND valid_to IS NOT NULL AND valid_to < now()
        ORDER BY valid_to ASC
        LIMIT {{lim:UInt32}}
        """,
    ),
    "expired_live": (
        "warning",
        "A row past its expires_at that has not been tombstoned. The "
        "retention sweep has not run for this namespace, or it is off.",
        f"""
        SELECT count() OVER () AS total, memory_key,
               toString(memory_id) AS memory_id, expires_at
        {_LIVE} AND expires_at IS NOT NULL AND expires_at < now()
        ORDER BY expires_at ASC
        LIMIT {{lim:UInt32}}
        """,
    ),
    "stale_verification": (
        "warning",
        "A row someone attested to (verified_by) whose content has changed since "
        "— or whose attested hash cannot be matched to the row's stored hash. The "
        "badge no longer holds. Re-verify the current content with memory.verify, "
        "or revoke it, so readers stop seeing an attestation that is not live.",
        f"""
        SELECT count() OVER () AS total, memory_key,
               toString(memory_id) AS memory_id, verified_by, verified_at,
               dateDiff('day', verified_at, now()) AS attested_days_ago
        {_LIVE} AND verified_by != ''
          AND (verified_sha256 = '' OR content_sha256 IN ('', '0')
               OR verified_sha256 != content_sha256)
        ORDER BY verified_at ASC
        LIMIT {{lim:UInt32}}
        """,
    ),
    "unsourced_low_confidence": (
        "info",
        "A live row below confidence_floor with neither source_trace nor "
        "source_resource_id: an inference stored with nothing behind it. "
        "Either attach the source it came from or let it expire.",
        f"""
        SELECT count() OVER () AS total, memory_key,
               toString(memory_id) AS memory_id, confidence, agent
        {_LIVE} AND status = 'active' AND confidence < {{floor:Float32}}
          AND source_trace = '' AND source_resource_id = ''
        ORDER BY confidence ASC, memory_key
        LIMIT {{lim:UInt32}}
        """,
    ),
    "unanchored_time_cue": (
        "info",
        "A live fact whose text carries a time cue ('last week', 'recently', "
        "'in 2019', a month name) but no resolved event date (temporal_anchor): "
        "every date read against it — timelines, 'how long' intervals — uses the "
        "session it was said in, and date reads can only report a bound. "
        "Re-anchor it: memory.update_by_id with valid_at set to the event date, "
        "or re-ingest with observation_date so the extractor resolves the cue.",
        f"""
        SELECT count() OVER () AS total, memory_key,
               toString(memory_id) AS memory_id, toString(valid_at) AS said_on,
               substring(JSONExtractString(value), 1, 160) AS text
        {_LIVE} AND status = 'active' AND temporal_anchor IS NULL
          -- a high row's text is readable by key only: never quoted or matched here
          AND sensitivity != 'high'
          AND memory_type IN ('semantic', 'episodic', 'preference')
          AND match(lower(JSONExtractString(value)), {{cue:String}})
        ORDER BY valid_at DESC, memory_key
        LIMIT {{lim:UInt32}}
        """,
    ),
    "unresolved_evidence": (
        "warning",
        "Active facts whose extractor-supplied evidence quote does not occur "
        "in the turns it cites (grounding = 'unresolved'): the "
        "quote is a paraphrase, so the fact's tie to its source is the model's "
        "word alone. A rising share means the extraction prompt is producing "
        "paraphrase where it promises verbatim. Review the sample in the dashboard; "
        "memory.update_by_id the fact with the real quote, or tombstone it "
        "via memory.delete when the source does not support it.",
        f"""
        SELECT count() OVER () AS total, memory_key,
               toString(memory_id) AS memory_id, agent, evidence
        {_LIVE} AND status = 'active' AND grounding = 'unresolved'
          -- the evidence quote is the high row's own text
          AND sensitivity != 'high'
        ORDER BY created_at DESC, memory_key
        LIMIT {{lim:UInt32}}
        """,
    ),
}


# Each check's contract (`RuleContract`). The version goes up when the
# check's predicate or built-in threshold changes; a request input it reads is not.
_CONTRACT: dict[str, RuleContract] = {
    "divergent_entities": RuleContract("contract_violation", 1),
    "divergent_status": RuleContract("contract_violation", 1),
    "orphan_vectors": RuleContract("contract_violation", 1),
    "unembedded": RuleContract(
        "contract_violation",
        1,
        exceptions=(
            "High-sensitivity rows are never embedded and are not checked; a row younger than "
            "embed_grace_seconds is the worker's normal lag and is not counted."
        ),
        uncertainty=(
            "With the embedding worker stopped or backlogged, every new row ages into this check."
        ),
    ),
    "conflicted_aging": RuleContract(
        "usage_heuristic",
        1,
        exceptions="A conflict younger than conflict_age_seconds is not counted.",
        uncertainty="The age threshold is a review expectation, not a stored contract.",
    ),
    "orphan_duplicates": RuleContract("contract_violation", 1),
    "superseded_dangling": RuleContract("contract_violation", 1),
    "stale_valid_to": RuleContract("contract_violation", 1),
    "expired_live": RuleContract(
        "contract_violation",
        1,
        uncertainty=(
            "A row the retention sweep has not reached yet is counted until the sweep runs."
        ),
    ),
    "stale_verification": RuleContract("contract_violation", 1),
    "unsourced_low_confidence": RuleContract(
        "usage_heuristic",
        1,
        exceptions="Only active rows below confidence_floor are checked.",
        uncertainty=(
            "A low-confidence inference kept on purpose, with its reasoning in the text, "
            "matches too."
        ),
    ),
    "unanchored_time_cue": RuleContract(
        "usage_heuristic",
        1,
        exceptions=(
            "High-sensitivity rows and types other than semantic, episodic and preference are "
            "not checked."
        ),
        uncertainty="The cue list is a pattern match: 'in May' can name a person, not a date.",
    ),
    "unresolved_evidence": RuleContract(
        "usage_heuristic",
        1,
        exceptions="High-sensitivity rows are not checked.",
        uncertainty="A faithful paraphrase of the cited turns is still counted.",
    ),
    # Its predicate is the code-link store's (`stale_of_served`): a change there raises this
    # version too.
    "code_links_stale": RuleContract(
        "usage_heuristic",
        1,
        exceptions=(
            "Only repositories the caller may read, and links made from the current revision."
        ),
        uncertainty=(
            "A memory that still holds after its code changed is counted until someone checks it."
        ),
    ),
}


def _sample_rows(rows: list[dict[str, Any]], limit: int) -> tuple[int, list[dict[str, Any]]]:
    """Split the `count() OVER ()` column off the sample rows."""
    if not rows:
        return 0, []
    total = int(rows[0].get("total") or 0)
    out: list[dict[str, Any]] = []
    for r in rows[:limit]:
        clean = {k: v for k, v in r.items() if k != "total"}
        # Datetimes and arrays serialise as-is through Pydantic; UUID-ish
        # strings already came back as str. Keep the sample flat.
        out.append(clean)
    return total, out


# Read from the code links rather than with SQL of its own: the links decide what is
# stale and which repositories the caller may read.
_CODE_LINKS_STALE = (
    "warning",
    "A memory whose linked code changed after it was written, was renamed or removed "
    "without a clear successor, or is gone (search "
    "leaves out one whose code is all gone, unless the namespace's code_change_policy "
    "is `flag` or a person holds it). Check it against the code and memory.update it, "
    "or memory.delete it when it no longer holds. Only repositories you may read "
    "are counted, and only links made from the memory as it is now.",
)


async def _code_links_stale(params: dict[str, Any]) -> list[dict[str, Any]]:
    """`code_links_stale`'s rows, newest check first, in the shape the SQL checks give."""
    from server.memory.app.code_link_reads import stale_links

    links = await stale_links(
        params["org"], params["ns"], params["keys"] or None, limit=params["lim"]
    )
    return [
        {
            "total": a["total"],
            "memory_key": a["memory_key"],
            "memory_id": a["memory_id"],
            "symbol": a["qualified_name"],
            "path": a["path"],
            "state": a["state"],
            "since": a["changed_since"],
        }
        for a in links
    ]


async def handle_namespace_validate(input: NamespaceValidateInput) -> NamespaceValidateOutput:
    rules = RuleLog()
    published = pg_validation.published_reads(
        input.org, [input.namespace], lambda reason: rules.skip(PROJECTION, reason)
    )
    async with published:
        return await _validate(input, rules)


async def _validate(input: NamespaceValidateInput, rules: RuleLog) -> NamespaceValidateOutput:
    # Taken before the reads, to the second as updated_at is: a cited row updated at or
    # after it may have been revised mid-report.
    checked_at = naive_utc_now_seconds()
    ch = get_client()
    params: dict[str, Any] = {
        "org": input.org,
        "ns": input.namespace,
        # LIMIT 0 would return no rows, and each check's total rides its sample rows: read
        # at least one and trim the samples to what was asked for.
        "lim": max(1, int(input.sample_limit)),
        "age": int(input.conflict_age_seconds),
        "grace": int(input.embed_grace_seconds),
        "floor": float(input.confidence_floor),
        "cue": _TIME_CUE_RE,
        "keys": [str(k) for k in input.keys],
    }

    findings: list[_NamespaceValidateFinding] = []

    async def run(
        name: str,
        severity: str,
        detail: str,
        query: Callable[[], Awaitable[list[dict[str, Any]]]],
    ) -> None:
        try:
            rows = await query()
        except Exception as exc:  # noqa: BLE001 — advisory report; one failed check must not sink the rest
            log.warning("namespace.validate %s skipped: %s", name, exc)
            rules.skip(name, type(exc).__name__)
            return
        rules.ran(name)
        total, samples = _sample_rows(rows, input.sample_limit)
        rule = _CONTRACT[name]
        findings.append(
            _NamespaceValidateFinding(
                check=name,
                severity=severity,  # type: ignore[arg-type]
                count=total,
                samples=samples,
                detail=detail,
                kind=rule.kind,
                rule_version=rule.version,
                source_op="namespace.validate",
                sample_limit=int(input.sample_limit),
                samples_complete=total <= len(samples),
                exceptions=rule.exceptions,
                uncertainty=rule.uncertainty,
            )
        )

    # CH client is per-call; checks run sequentially like namespace.summary.
    for name, (severity, detail, sql) in _CHECKS.items():
        await run(name, severity, detail, functools.partial(ch.query_all, sql, params=params))
    await run("code_links_stale", *_CODE_LINKS_STALE, functools.partial(_code_links_stale, params))
    await attach_evidence(input.org, input.namespace, findings, rules, ch=ch)

    errors = sum(f.count for f in findings if f.severity == "error")
    warnings = sum(f.count for f in findings if f.severity == "warning")
    infos = sum(f.count for f in findings if f.severity == "info")
    healthy = errors == 0 and (not input.strict or warnings == 0)

    return NamespaceValidateOutput(
        org=input.org,
        namespace=input.namespace,
        healthy=healthy,
        errors=errors,
        warnings=warnings,
        infos=infos,
        findings=findings,
        skipped=rules.skipped,
        keys_scoped=len(input.keys),
        coverage=rules.coverage,
        checked_at=checked_at,
    )
