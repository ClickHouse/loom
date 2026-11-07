"""Period digests: one derived row over K consecutive session digests.

Loom compresses a conversation at exactly one scale above the turn: the
session digest (`loom.session_summaries`). A question whose
answer spans several sessions has to assemble several digests or several
facts inside one top-k, and the more sessions the answer spans, the less
often all of them reach it.

A period digest is the next scale: for one identity (org, namespace, agent, user_id) the
digests are ordered by event time and cut into non-overlapping windows of
`sessions_per_period`; each complete window is compressed by one LLM call into a dated
paragraph and written as a `loom.memory` row of type `semantic` (the type the search
legs read; `episodic` is the non-searchable type reserved for raw turn rows), key
`period_digest_<first>_<last>`, source_class `derived`, `valid_at` = the first session's
date, tagged `period_digest` plus a `src:summary:<session>` tag per digest. Lineage: the
row links `derived_from` to the memories its digests compressed, so the derived-row
audit (`memory.evidence` scope `derived`, `derived_fmr`) judges a period row against the
facts under it; a row whose publication was pending, or whose lineage write failed, goes
without it until a `rewrite_existing` pass. A trailing window shorter than K is left for
a later pass; a window whose key has a version, a deleted one included, is skipped, so
the pass is idempotent and additive and never writes back a period taken out.
Under Postgres the authority's heads decide which periods are written, so one committed
but not yet in ClickHouse is not written again. Nothing is tombstoned.

The rows are plain memories: they compete in the vector legs like any fact,
keep out of the session-digest leg (whose keys start with `summary:`) and
out of the digest chain, and read back through `memory.list`
and `memory.evidence` like every other derived row.
"""

from __future__ import annotations

import json
import re
import uuid
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from server.core import pg
from server.core.datetimes import naive_utc_now
from server.core.error_summary import error_summary
from server.llm import customer
from server.llm.identity import reads_writer
from server.memory.app.provenance import writer_actor
from server.memory.domain.trust import subject_of
from server.memory.store import pg_memory
from server.memory.store.lifecycle import insert_documented_versions, served_rows
from server.memory.store.reads import session_writers
from server.memory.store.rows import next_version
from server.observability.genai import llm_stage
from server.settings.budgets import llm_max_tokens

PERIOD_KEY_PREFIX = "period_digest_"
PERIOD_TAG = "period_digest"
SOURCE_TRACE = "digest.period"
PERIOD_DIGEST_PROMPT_VERSION = "pd-1"

_SLUG_RE = re.compile(r"[^a-z0-9]+")

_PERIOD_DIGEST_SYSTEM = (
    "You compress several consecutive session digests of one ongoing conversation "
    "into ONE period digest for later retrieval. Write 3-6 sentences of concrete facts, "
    "events, decisions and stated plans, in chronological order, naming the speakers. "
    "Open with the span: 'Between <first date> and <last date>, ...'. Keep every date "
    "exactly as the digests state it and attach it to the event it dates; never add, "
    "shift or sharpen a date, and an event the digests leave undated stays undated. "
    "Prefer what recurs or connects across sessions (a plan and its outcome, a state "
    "and its change, a habit) over one-off detail. No greetings, no meta-commentary. "
    "Output the period digest only."
)


@dataclass(frozen=True)
class PeriodDigestConfig:
    sessions_per_period: int = 5
    max_identities_per_pass: int = 10
    max_periods_per_identity: int = 20
    org_filter: str | None = None
    namespace_filter: str | None = None
    # With an explicit namespace the operator has said which namespace; the
    # namespaces.consolidation_enabled opt-in gates the unscoped worker pass.
    require_opt_in: bool = True
    # Rewrite live windows whose key already exists (a new version replaces the old row
    # under FINAL), for a prompt or shape change; one taken out stays out.
    rewrite_existing: bool = False


@dataclass
class PeriodDigestResult:
    identities_scanned: int = 0
    periods_planned: int = 0
    periods_written: int = 0
    periods_skipped: int = 0
    digests_covered: int = 0
    tokens_in: int = 0
    tokens_out: int = 0
    failures: list[str] = field(default_factory=list)


@dataclass(frozen=True)
class Period:
    """One window of consecutive digests of one identity, first → last."""

    org: str
    namespace: str
    agent: str
    user_id: str
    digests: tuple[dict[str, Any], ...]

    @property
    def first(self) -> dict[str, Any]:
        return self.digests[0]

    @property
    def last(self) -> dict[str, Any]:
        return self.digests[-1]

    @property
    def memory_key(self) -> str:
        return period_key(
            str(self.first.get("session_id") or ""), str(self.last.get("session_id") or "")
        )


def _session_tail(session_id: str) -> str:
    """`<namespace>:session_7` -> `session_7`; a bare id stays itself."""
    tail = session_id.rsplit(":", 1)[-1] if session_id else ""
    slug = _SLUG_RE.sub("_", tail.lower()).strip("_")
    return slug[:40] or "s"


def period_key(first_session: str, last_session: str) -> str:
    return f"{PERIOD_KEY_PREFIX}{_session_tail(first_session)}_{_session_tail(last_session)}"


def _at(d: dict[str, Any]) -> datetime | None:
    at = d.get("at") or d.get("valid_at") or d.get("created_at")
    return at if isinstance(at, datetime) else None


def plan_periods(
    digests: list[dict[str, Any]],
    *,
    sessions_per_period: int,
    skip_keys: set[str] | None = None,
) -> tuple[list[Period], int]:
    """Cut one namespace's digests into complete windows of K per identity,
    ordered by event time then session_id; skip windows whose key is in
    `skip_keys`. Returns (periods to write, periods skipped). Pure."""
    k = max(2, int(sessions_per_period))
    skip = skip_keys or set()
    by_identity: dict[tuple[str, str, str, str], list[dict[str, Any]]] = defaultdict(list)
    for d in digests:
        if not str(d.get("session_id") or ""):
            continue
        ident = (
            str(d.get("org") or ""),
            str(d.get("namespace") or ""),
            str(d.get("agent") or "-"),
            str(d.get("user_id") or "-"),
        )
        by_identity[ident].append(d)
    out: list[Period] = []
    skipped = 0
    for ident, items in sorted(by_identity.items()):
        ordered = sorted(
            items,
            key=lambda d: (_at(d) is None, _at(d) or datetime.min, str(d.get("session_id"))),
        )
        for start in range(0, len(ordered) - k + 1, k):
            period = Period(*ident, digests=tuple(ordered[start : start + k]))
            if period.memory_key in skip:
                skipped += 1
            else:
                out.append(period)
    return out, skipped


def build_period_prompt(period: Period) -> tuple[str, str]:
    lines = []
    for d in period.digests:
        at = _at(d)
        stamp = at.strftime("%Y-%m-%d") if at else "undated"
        lines.append(f"[{stamp}] {str(d.get('summary_text') or '').strip()}")
    first, last = _at(period.first), _at(period.last)
    span = (
        f"{first.strftime('%Y-%m-%d')} → {last.strftime('%Y-%m-%d')}"
        if first and last
        else "undated"
    )
    user = f"Period: {span}\nSessions: {len(period.digests)}\n\nDigests:\n" + "\n".join(lines)
    return _PERIOD_DIGEST_SYSTEM, user + "\n\nPeriod digest:"


def period_memory_id(period: Period) -> str:
    """The digest's memory_id: stable for the row's identity, so a rewrite of
    the same period keeps its id and its derived_from links, and two periods
    never share one."""
    ident = "/".join(
        [period.org, period.namespace, period.agent, period.user_id, "-", period.memory_key]
    )
    return str(uuid.uuid5(uuid.NAMESPACE_URL, f"loom://period-digest/{ident}"))


def build_period_memory_rows(
    period: Period,
    *,
    text: str,
    embedding: list[float],
    embedding_model: str,
    embedding_dim: int,
    version: int,
    entities: list[str] | None = None,
) -> tuple[dict[str, Any], dict[str, Any]]:
    """(loom.memory row, loom.memory_embeddings row) for one period digest,
    through the canonical repo builders so every persisted column is set.
    `entities` is the union of the compressed facts' entities; the row carries
    them so the entity-ANN leg and the chunk leg's overlap can reach it."""
    from server.core.hashing import sha256_value_json
    from server.memory.store import (
        MemoryRow,
        build_embedding_row,
        build_memory_version_row,
    )

    now = naive_utc_now()
    serialized = json.dumps(text)
    sha = sha256_value_json(serialized)
    first_at = _at(period.first)
    valid_at = first_at.replace(tzinfo=None) if first_at and first_at.tzinfo else first_at
    src_tags = [f"src:summary:{d.get('session_id')}" for d in period.digests[:32]]
    first_tail = _session_tail(str(period.first.get("session_id")))
    last_tail = _session_tail(str(period.last.get("session_id")))
    span_tag = f"period:{first_tail}..{last_tail}"
    tags = [PERIOD_TAG, span_tag, *src_tags]
    ents = sorted({str(e).strip().lower() for e in (entities or []) if str(e).strip()})[:32]
    key = period.memory_key
    seed = MemoryRow(
        memory_id=period_memory_id(period),
        org=period.org,
        namespace=period.namespace,
        agent=period.agent,
        user_id=period.user_id,
        session_id="-",
        memory_key=key,
        memory_type="semantic",
        value=text,
        confidence=1.0,
        version=version,
        pinned=False,
        created_at=now,
        expires_at=None,
    )
    memory_row = build_memory_version_row(
        seed,
        version=version,
        value=serialized,
        value_json="{}",
        source_trace=f"{SOURCE_TRACE}|prompt={PERIOD_DIGEST_PROMPT_VERSION}",
        actor=writer_actor(fallback="process:period_digest"),
        source_class="derived",
        tags=tags,
        entities=ents,
        content_sha256=sha,
        valid_at=valid_at,
        created_at=now,
        updated_at=now,
        last_reinforced_at=now,
        reinforcement_count=1,
    )
    embedding_row = build_embedding_row(
        org=period.org,
        namespace=period.namespace,
        agent=period.agent,
        user_id=period.user_id,
        session_id="-",
        memory_key=key,
        memory_type="semantic",
        scope="private",
        content_excerpt=text[:256],
        source_class="derived",
        embedding_model=embedding_model,
        embedding_dim=embedding_dim,
        embedding=list(embedding),
        confidence=1.0,
        created_at=now,
        version=version,
        tags=tags,
        entities=ents,
        valid_at=valid_at,
        status="active",
    )
    return memory_row, embedding_row


async def _fetch_namespaces(ch: Any, cfg: PeriodDigestConfig) -> list[dict[str, Any]]:
    """Namespaces with at least K digests: opted-in ones for the worker, the
    named one for the CLI."""
    scope = ""
    params: dict[str, Any] = {
        "k": int(max(2, cfg.sessions_per_period)),
        "lim": int(max(1, cfg.max_identities_per_pass)),
    }
    if cfg.org_filter is not None:
        scope += " AND s.org = {org_f:String}"
        params["org_f"] = cfg.org_filter
    if cfg.namespace_filter is not None:
        scope += " AND s.namespace = {ns_f:String}"
        params["ns_f"] = cfg.namespace_filter
    # A switched-off org's sessions wait unpicked, leaving the pass's slots to others.
    off = await customer.switched_off_orgs()
    if off:
        scope += " AND NOT has({off:Array(String)}, s.org)"
        params["off"] = sorted(off)
    opt_in = (
        """
        INNER JOIN loom.namespaces AS n FINAL
                ON s.org = n.org AND s.namespace = n.namespace
        WHERE n.active = true AND n.consolidation_enabled = true
        """
        if cfg.require_opt_in
        else "WHERE 1 = 1"
    )
    rows = await ch.query_all(
        f"""
        SELECT s.org AS org, s.namespace AS namespace, s.agent AS agent,
               s.user_id AS user_id, uniqExact(s.session_id) AS sessions
        FROM loom.session_summaries AS s FINAL
        {opt_in}
          AND s.tombstoned_at IS NULL
          {scope}
        GROUP BY s.org, s.namespace, s.agent, s.user_id
        HAVING sessions >= {{k:UInt32}}
        ORDER BY sessions DESC
        LIMIT {{lim:UInt32}}
        """,
        params=params,
    )
    return list(rows or [])


async def _fetch_digests(ch: Any, ident: dict[str, Any]) -> list[dict[str, Any]]:
    rows = await ch.query_all(
        """
        SELECT org, namespace, agent, user_id, session_id, summary_text, derived_from,
               coalesce(valid_at, created_at) AS at
        FROM loom.session_summaries FINAL
        WHERE org = {org:String} AND namespace = {ns:String}
          AND agent = {agent:String} AND user_id = {uid:String}
          AND tombstoned_at IS NULL
        ORDER BY at, session_id
        """,
        params={
            "org": ident["org"],
            "ns": ident["namespace"],
            "agent": ident["agent"],
            "uid": ident["user_id"],
        },
    )
    return list(rows or [])


async def _skipped_period_keys(ch: Any, ident: dict[str, Any], *, rewrite: bool) -> set[str]:
    """On ClickHouse, the identity's period keys a pass skips: every key with a version, or
    under a rewrite only those taken out (deleted, redacted or no longer active: an Undo
    of a retirement brings a redacted version back as the live head)."""
    rows = await ch.query_all(
        """
        SELECT memory_key,
               (status != 'active' OR tombstoned_at IS NOT NULL OR redacted_at IS NOT NULL)
                   AS taken_out
        FROM loom.memory FINAL
        WHERE org = {org:String} AND namespace = {ns:String}
          AND agent = {agent:String} AND user_id = {uid:String} AND session_id = '-'
          AND startsWith(memory_key, {pfx:String})
        """,
        params={
            "org": ident["org"],
            "ns": ident["namespace"],
            "agent": ident["agent"],
            "uid": ident["user_id"],
            "pfx": PERIOD_KEY_PREFIX,
        },
    )
    return {str(r["memory_key"]) for r in rows if not rewrite or r["taken_out"]}


async def _writable_in_authority(periods: list[Period], *, rewrite: bool) -> list[Period]:
    """Under Postgres, the periods a pass may write: those the authority holds no head for,
    or under a rewrite a live one (active, neither deleted nor redacted, as
    `_skipped_period_keys` has it). A period committed but not yet in ClickHouse counts as
    written."""
    keys = [(p.org, p.namespace, p.agent, p.user_id, "-", p.memory_key) for p in periods]
    heads = await pg_memory.heads_by_keys(keys, include_tombstoned=True)
    return [
        p
        for p, k in zip(periods, keys, strict=True)
        if (h := heads.get(k)) is None
        or (
            rewrite
            and h.get("status") == "active"
            and h.get("tombstoned_at") is None
            and h.get("redacted_at") is None
        )
    ]


async def _source_memories(ch: Any, period: Period) -> tuple[list[str], list[str]]:
    """(memory_ids, entities) of the facts the period's digests compressed
    (their `derived_from` keys), bounded: the ids are the lineage the derived-row
    audit reads, the entities ride on the period row so the entity-ANN leg
    and the chunk leg's entity overlap can reach it. A failed read raises."""
    keys: list[str] = []
    for d in period.digests:
        raw = d.get("derived_from") or []
        if isinstance(raw, str):
            try:
                raw = json.loads(raw)
            except Exception:  # noqa: BLE001 — not JSON: the string is one key
                raw = [raw]
        keys.extend(str(k) for k in raw if k)
    keys = list(dict.fromkeys(keys))[:64]
    if not keys:
        return [], []
    # A 'high' row's entities were mined from its value; the period row is plain and
    # embedded, so it never carries them.
    rows = await ch.query_all(
        """
        SELECT toString(memory_id) AS memory_id, org, namespace, agent, user_id, session_id,
               memory_key, version, redacted_at, toString(content_sha256) AS content_sha256,
               if(sensitivity = 'high', [], entities) AS entities
        FROM loom.memory FINAL
        WHERE org = {org:String} AND namespace = {ns:String}
          AND agent = {agent:String} AND user_id = {uid:String}
          AND memory_key IN {keys:Array(String)}
          AND status = 'active' AND tombstoned_at IS NULL
        """,
        params={
            "org": period.org,
            "ns": period.namespace,
            "agent": period.agent,
            "uid": period.user_id,
            "keys": keys,
        },
    )
    ids = [str(r.get("memory_id")) for r in rows if r.get("memory_id")]
    # Only a source the authority still serves as read, or as a copy of it, lends its
    # entities: a change ClickHouse has not published still reads as the old row here.
    # Every source stays in the lineage; one left out lends none until a rewrite.
    served = await served_rows(rows, _is_copy)
    ents = sorted(
        {
            str(e).strip().lower()
            for r in served
            for e in (r.get("entities") or [])
            if str(e).strip()
        }
    )[:32]
    return ids, ents


def _is_copy(row: dict[str, Any], head: dict[str, Any]) -> bool:
    """Whether a newer head is a copy of the source read (a pin, a touch, a lock): the
    same memory and text, still active and not labelled high. The row lends the entities
    ClickHouse read, so a source whose newer extraction ClickHouse has not published lends
    the ones it had before."""
    return (
        str(head.get("memory_id")) == row["memory_id"]
        and str(head.get("content_sha256")) == row["content_sha256"]
        and head.get("status") == "active"
        and head.get("sensitivity") != "high"
    )


async def _period_writer(ch: Any, period: Period) -> str:
    """Whom a period's digest call is for: the newest of its sessions' principal
    writers ('' when none recorded one, and the period's namespace decides). Read
    only while identity headers name it."""
    if not reads_writer():
        return ""
    keys = [
        (period.org, period.namespace, period.agent, period.user_id, str(d.get("session_id") or ""))
        for d in period.digests
    ]
    writers = await session_writers(keys, ch=ch)
    for key in reversed(keys):
        if subject := subject_of(writers.get(key, "")):
            return subject
    return ""


async def _write_period(
    ch: Any, llm: Any, embedder: Any, period: Period, result: PeriodDigestResult
) -> bool:
    """Write one period. A step that fails before the row is written fails the period
    alone, recorded in `result.failures`, and a later pass writes it whole. The lineage
    follows the row once it is in ClickHouse, where a user erasure reaches the lineage
    through the row's key; a row whose publication is pending, or whose lineage write
    failed, is recorded and goes without its lineage until a rewrite."""
    try:
        source_ids, source_entities = await _source_memories(ch, period)
    except Exception as e:  # noqa: BLE001 — recorded in failures; the pass continues
        result.failures.append(f"sources {period.memory_key}: {error_summary(e)}"[:256])
        return False
    try:
        writer = await _period_writer(ch, period)
    except Exception as e:  # noqa: BLE001 — recorded in failures; the pass continues
        result.failures.append(f"writer {period.memory_key}: {error_summary(e)}"[:256])
        return False
    system, user = build_period_prompt(period)
    whose = {"namespace": period.namespace, "subject": writer, "user_id": period.user_id}
    try:
        with llm_stage("period_digest"), customer.for_org(period.org, **whose):
            out = await llm.complete(
                system=system, user=user, max_tokens=llm_max_tokens("period_digest")
            )
    except customer.HOLDS:
        raise  # the identity waits for the org, its other periods included
    except Exception as e:  # noqa: BLE001 — recorded in failures; the pass continues
        result.failures.append(f"llm {period.memory_key}: {error_summary(e)}"[:256])
        return False
    result.tokens_in += int(getattr(out, "tokens_in", 0) or 0)
    result.tokens_out += int(getattr(out, "tokens_out", 0) or 0)
    text = (getattr(out, "text", "") or "").strip()
    if not text:
        result.failures.append(f"empty digest {period.memory_key}")
        return False
    try:
        with customer.for_org(period.org, **whose):
            embedding = await embedder.embed(text)
    except customer.HOLDS:
        raise  # as for the model call: the identity waits for the org
    except Exception as e:  # noqa: BLE001 — recorded in failures; the pass continues
        result.failures.append(f"embed {period.memory_key}: {error_summary(e)}"[:256])
        return False
    memory_row, embedding_row = build_period_memory_rows(
        period,
        text=text,
        embedding=embedding,
        embedding_model=getattr(embedder, "model_name", ""),
        embedding_dim=int(getattr(embedder, "dim", 0) or 0),
        version=next_version(),
        entities=source_entities,
    )
    try:
        published = await insert_documented_versions([memory_row], [embedding_row], ch=ch)
    except Exception as e:  # noqa: BLE001 — recorded in failures; the pass continues
        result.failures.append(f"write {period.memory_key}: {error_summary(e)}"[:256])
        return False
    if source_ids and not published:
        result.failures.append(f"links {period.memory_key}: publication pending")
    elif source_ids:
        from server.memory.app.evidence import link_belief_sources

        try:
            await link_belief_sources(
                org=period.org,
                namespace=period.namespace,
                memory_links=[(str(memory_row["memory_key"]), str(memory_row["memory_id"]))],
                evidence_ids=source_ids,
                support_type="derived_from",
                ch=ch,
            )
        except Exception as e:  # noqa: BLE001 — recorded in failures; the row stands
            result.failures.append(f"links {period.memory_key}: {error_summary(e)}"[:256])
    return True


async def _plan_identity(
    ch: Any, digests: list[dict[str, Any]], ident: dict[str, Any], cfg: PeriodDigestConfig
) -> tuple[list[Period], int]:
    """(the identity's periods to write, how many were skipped as written or taken out).
    Under Postgres the authority's heads decide which are written, ClickHouse's
    otherwise; the cap applies to what is left, so a period skipped takes no place in it."""
    k, rewrite = cfg.sessions_per_period, cfg.rewrite_existing
    if pg.enabled():
        windows, _ = plan_periods(digests, sessions_per_period=k)
        writable = await _writable_in_authority(windows, rewrite=rewrite)
        skipped = len(windows) - len(writable)
    else:
        skip = await _skipped_period_keys(ch, ident, rewrite=rewrite)
        writable, skipped = plan_periods(digests, sessions_per_period=k, skip_keys=skip)
    return writable[: max(0, cfg.max_periods_per_identity)], skipped


async def run_period_digest(
    cfg: PeriodDigestConfig | None = None,
    *,
    ch: Any | None = None,
    llm: Any | None = None,
    embedder: Any | None = None,
    dry_run: bool = False,
) -> PeriodDigestResult:
    """One scan → plan → compress → write pass. CH / LLM / embedder are
    injectable for tests and default to the configured singletons."""
    cfg = cfg or PeriodDigestConfig()
    result = PeriodDigestResult()
    if ch is None:
        from server.core.ch import get_client

        ch = get_client()
    if llm is None and not dry_run:
        from server.llm.client import get_llm_client

        llm = get_llm_client()
    if embedder is None and not dry_run:
        from server.embedding import get_embedder

        embedder = get_embedder()

    identities = await _fetch_namespaces(ch, cfg)
    result.identities_scanned = len(identities)
    for ident in identities:
        try:
            digests = await _fetch_digests(ch, ident)
            periods, skipped = await _plan_identity(ch, digests, ident, cfg)
            result.periods_planned += len(periods)
            result.periods_skipped += skipped
            if dry_run:
                continue
            for period in periods:
                if await _write_period(ch, llm, embedder, period, result):
                    result.periods_written += 1
                    result.digests_covered += len(period.digests)
        except customer.HOLDS:
            continue  # a held call is no failure: its periods are written once the org is back
        except Exception as e:  # noqa: BLE001 — one identity never fails the pass
            result.failures.append(
                f"{ident.get('org')}/{ident.get('namespace')}: {error_summary(e)}"[:256]
            )
    return result


__all__ = [
    "PERIOD_DIGEST_PROMPT_VERSION",
    "PERIOD_KEY_PREFIX",
    "PERIOD_TAG",
    "Period",
    "PeriodDigestConfig",
    "PeriodDigestResult",
    "build_period_memory_rows",
    "build_period_prompt",
    "period_key",
    "plan_periods",
    "run_period_digest",
]
