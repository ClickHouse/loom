"""Period digests: one derived row over K consecutive session digests.

Loom compresses a conversation at exactly one scale above the turn: the
session digest (`loom.session_summaries`). A question whose
answer spans several sessions has to assemble several digests or several
facts inside one top-k, and the more sessions the answer spans, the less
often all of them reach it.

A period digest is the next scale: for one identity (org, namespace, agent, user_id) the
digests are ordered by event time and cut into non-overlapping windows of
`sessions_per_period`; each complete window is compressed by one LLM call into a dated
paragraph and written as a `loom.memory` row of type `semantic` (the type the search legs
read; `episodic` is the non-searchable type reserved for raw turn rows), key
`period_digest_<first>_<last>`, source_class `derived`, `valid_at` = the first session's
date, tagged `period_digest` plus a `src:summary:<session>` tag per digest. Lineage: the row
links `derived_from` to the memories its digests compressed, so the derived-row audit
(`memory.evidence` scope `derived`, `derived_fmr`) judges a period row against the facts
under it. A trailing window shorter than K is left for a later pass; a window whose key
already exists is skipped, so the pass is idempotent and additive. Nothing is tombstoned.

The rows are plain memories: they compete in the vector legs like any fact,
keep out of the session-digest leg (whose keys start with `summary:`) and
out of the digest chain, and read back through `memory.list`
and `memory.evidence` like every other derived row.
"""

from __future__ import annotations

import json
import logging
import re
import uuid
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from server.core.datetimes import naive_utc_now
from server.llm import customer
from server.memory.app.provenance import writer_actor
from server.memory.store.inserts import insert_memory_versions
from server.memory.store.rows import next_version
from server.observability.genai import llm_stage

log = logging.getLogger(__name__)

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
    # Rewrite windows whose key already exists (a new version replaces the
    # old row under FINAL); for a prompt or shape change on rows already written.
    rewrite_existing: bool = False


@dataclass
class PeriodDigestResult:
    identities_scanned: int = 0
    periods_planned: int = 0
    periods_written: int = 0
    periods_existing: int = 0
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
    existing_keys: set[str] | None = None,
    max_periods_per_identity: int = 20,
) -> tuple[list[Period], int]:
    """Cut one namespace's digests into complete windows of K per identity,
    ordered by event time then session_id; skip windows whose key already
    exists. Returns (periods to write, periods already present). Pure."""
    k = max(2, int(sessions_per_period))
    existing = existing_keys or set()
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
    present = 0
    for ident, items in sorted(by_identity.items()):
        ordered = sorted(
            items,
            key=lambda d: (_at(d) is None, _at(d) or datetime.min, str(d.get("session_id"))),
        )
        made = 0
        for start in range(0, len(ordered) - k + 1, k):
            window = tuple(ordered[start : start + k])
            period = Period(*ident, digests=window)
            if period.memory_key in existing:
                present += 1
                continue
            if made >= max_periods_per_identity:
                break
            out.append(period)
            made += 1
    return out, present


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


async def _existing_period_keys(ch: Any, ident: dict[str, Any]) -> set[str]:
    rows = await ch.query_all(
        """
        SELECT DISTINCT memory_key FROM loom.memory FINAL
        WHERE org = {org:String} AND namespace = {ns:String}
          AND agent = {agent:String} AND user_id = {uid:String}
          AND startsWith(memory_key, {pfx:String})
          AND status = 'active' AND tombstoned_at IS NULL
        """,
        params={
            "org": ident["org"],
            "ns": ident["namespace"],
            "agent": ident["agent"],
            "uid": ident["user_id"],
            "pfx": PERIOD_KEY_PREFIX,
        },
    )
    return {str(r.get("memory_key") or "") for r in (rows or [])}


async def _source_memories(ch: Any, period: Period) -> tuple[list[str], list[str]]:
    """(memory_ids, entities) of the facts the period's digests compressed
    (their `derived_from` keys), bounded: the ids are the lineage the derived-row
    audit reads, the entities ride on the period row so the entity-ANN leg
    and the chunk leg's entity overlap can reach it."""
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
    rows = await ch.query_all(
        """
        SELECT toString(memory_id) AS memory_id,
               -- a 'high' row's entities were mined from its value; the period row is
               -- plain and embedded, so it never carries them (the id stays, for lineage)
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
    ids = [str(r.get("memory_id")) for r in (rows or []) if r.get("memory_id")]
    ents = sorted(
        {
            str(e).strip().lower()
            for r in (rows or [])
            for e in (r.get("entities") or [])
            if str(e).strip()
        }
    )[:32]
    return ids, ents


async def _write_period(
    ch: Any, llm: Any, embedder: Any, period: Period, result: PeriodDigestResult
) -> bool:
    system, user = build_period_prompt(period)
    try:
        with llm_stage("period_digest"), customer.for_org(period.org):
            out = await llm.complete(system=system, user=user, max_tokens=420)
    except customer.HOLDS:
        raise  # the identity waits for the org, its other periods included
    except Exception as e:  # noqa: BLE001 — recorded in failures; the pass continues
        result.failures.append(f"llm {period.memory_key}: {type(e).__name__}: {e}"[:256])
        return False
    result.tokens_in += int(getattr(out, "tokens_in", 0) or 0)
    result.tokens_out += int(getattr(out, "tokens_out", 0) or 0)
    text = (getattr(out, "text", "") or "").strip()
    if not text:
        result.failures.append(f"empty digest {period.memory_key}")
        return False
    with customer.for_org(period.org):
        embedding = await embedder.embed(text)
    try:
        source_ids, source_entities = await _source_memories(ch, period)
    except Exception as e:  # noqa: BLE001 — lineage and entities are additive
        log.warning("period_digest: source lookup failed (non-fatal): %s", e)
        source_ids, source_entities = [], []
    memory_row, embedding_row = build_period_memory_rows(
        period,
        text=text,
        embedding=embedding,
        embedding_model=getattr(embedder, "model_name", ""),
        embedding_dim=int(getattr(embedder, "dim", 0) or 0),
        version=next_version(),
        entities=source_entities,
    )
    await insert_memory_versions([memory_row], ch=ch)
    try:
        if source_ids:
            from server.memory.app.evidence import link_belief_sources

            await link_belief_sources(
                org=period.org,
                namespace=period.namespace,
                memory_links=[(str(memory_row["memory_key"]), str(memory_row["memory_id"]))],
                evidence_ids=source_ids,
                support_type="derived_from",
                ch=ch,
            )
    except Exception as e:  # noqa: BLE001 — lineage is additive
        log.warning("period_digest: derived_from links failed (non-fatal): %s", e)
    await ch.insert(
        "loom.memory_embeddings",
        [embedding_row],
        settings={"async_insert": 0, "wait_for_async_insert": 1},
    )
    return True


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
            existing = set() if cfg.rewrite_existing else await _existing_period_keys(ch, ident)
            periods, present = plan_periods(
                digests,
                sessions_per_period=cfg.sessions_per_period,
                existing_keys=existing,
                max_periods_per_identity=cfg.max_periods_per_identity,
            )
            result.periods_planned += len(periods)
            result.periods_existing += present
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
                f"{ident.get('org')}/{ident.get('namespace')}: {type(e).__name__}: {e}"[:256]
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
