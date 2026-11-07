"""Entity extraction worker.

Scans memories with empty `entities` and calls an LLM to extract named
entities. Persists the result on both `loom.memory` and
`loom.memory_embeddings` so the search-side WHERE on `entities` doesn't
need a join back to the canonical row. Out of the hot path because
entities are an additive recall lift, never load-bearing for correctness.

On by default; `LOOM_ENTITY_EXTRACT_ENABLED=false` turns it off.
"""

from __future__ import annotations

import json
import logging
import re
import time
from collections import deque
from dataclasses import dataclass, field
from typing import Any

from server.llm import customer
from server.memory.app.drops import record_drop
from server.memory.domain.entity_sentinels import NONE_SENTINEL, PARSE_FAILED_SENTINEL
from server.memory.domain.graph_quality import canonical_entity_text
from server.memory.domain.sensitivity import value_reads_high
from server.memory.domain.trust import subject_of
from server.memory.store.rows import IDENTITY_COLUMNS, identity_key
from server.observability.budget import namespaces_over_cap
from server.observability.genai import staged
from server.settings import settings
from server.settings.budgets import llm_max_tokens
from server.workers import pass_context
from server.workers.cost_event import emit_worker_cost_event_for
from server.workers.status import record_pass

_HEAL_LAST_RUN: dict[str, float] = {"at": 0.0}


def _heal_due(now: float | None = None) -> bool:
    """True once per entity_heal_interval_seconds (0 = every pass); the first
    call after boot is due. Marks the run when it says yes."""
    from server.settings import settings

    interval = float(settings.entity_heal_interval_seconds)
    t = time.monotonic() if now is None else now
    if interval > 0 and _HEAL_LAST_RUN["at"] > 0.0 and t - _HEAL_LAST_RUN["at"] < interval:
        return False
    _HEAL_LAST_RUN["at"] = t
    return True


log = logging.getLogger(__name__)


_EXTRACT_PROMPT = """\
Extract the named entities from this memory value as a JSON array of
short lowercased strings.

Prioritize by salience:
  1. people (names), places (cities, regions), organizations / companies
  2. projects, products, tools, technologies
  3. file paths, identifiers (URLs, ticket IDs, commit shas)

Exclude generic English nouns, adjectives, verbs, pronouns, stopwords.
Deduplicate. At most 8 entities — keeps downstream embedding cost
bounded; if more than 8 candidates exist, keep the highest-salience ones.

Return [] if the value has no entities meeting these criteria.

Return EXACTLY a JSON array, no surrounding prose, no code fence:
  ["alice", "postgres", "web-01"]
"""


@dataclass(frozen=True)
class EntityExtractorConfig:
    batch_size: int = 25
    org_filter: str | None = None
    namespace_filter: str | None = None


@dataclass
class EntityExtractorResult:
    # The model's answers; `rows_written` says what landed (a head moved since is left).
    extracted: list[dict[str, Any]] = field(default_factory=list)
    skipped_no_value: int = 0
    healed_divergent: int = 0  # mirror rows re-emitted with memory's entities
    # Rows dropped because the namespace was over its 24h LLM cost cap.
    skipped_over_budget: int = 0
    # Rows of an org whose model calls are switched off, left for later.
    skipped_switched_off: int = 0
    # LLM replies that were not a parseable JSON array. The row is left
    # untouched for the next pass (see `memory_entity_extract_parse_fail_max`) — NOT written as
    # `_none_`, which would look like "genuinely no entities" forever.
    parse_failures: int = 0
    # Rows that exhausted `memory_entity_extract_parse_fail_max` and were stamped `_parse_failed_`.
    parse_poisoned: int = 0
    # Rows whose value reads as high, whatever their label: never sent to the model, stamped
    # `_none_` so the scan stops picking them up.
    skipped_sensitive: int = 0
    # Rows the batch write put on each table.
    rows_written: dict[str, int] = field(default_factory=dict)
    failures: list[str] = field(default_factory=list)


# Sentinel written when the LLM returned a well-formed empty array: the row
# genuinely has no entities and must leave the `empty(entities)` rescan.
_NONE_SENTINEL = NONE_SENTINEL  # "_none_"
# Sentinel written after `memory_entity_extract_parse_fail_max` consecutive unparseable replies for
# the SAME row. Distinct from `_none_` so an operator can tell "nothing to
# extract" from "the model never answered in the requested shape", and so the
# row stops burning LLM budget every pass. Reset by re-running extraction
# (any write that empties `entities`).
_PARSE_FAILED_SENTINEL = PARSE_FAILED_SENTINEL  # "_parse_failed_"

# In-process per-row parse-failure streaks. Keyed by the full identity tuple +
# memory_key. Process-local on purpose: a restart grants the row another
# `memory_entity_extract_parse_fail_max` attempts, which is the cheap and correct default (the
# alternative is a column on loom.memory for a worker-internal counter).
# Bounded so a pathological corpus can't grow it without limit.
_parse_fail_streaks: dict[tuple[str, ...], int] = {}
_PARSE_FAIL_STREAKS_CAP = 10_000


def _note_parse_failure(row_key: tuple[str, ...]) -> int:
    """Bump and return the row's consecutive parse-failure count."""
    if len(_parse_fail_streaks) >= _PARSE_FAIL_STREAKS_CAP and row_key not in _parse_fail_streaks:
        _parse_fail_streaks.clear()
    n = _parse_fail_streaks.get(row_key, 0) + 1
    _parse_fail_streaks[row_key] = n
    return n


def _parse_entities(text: str) -> list[str] | None:
    """First JSON array in the response, canonical (`canonical_entity_text`) + deduped.

    Returns None when the reply contains no parseable JSON array — a
    PARSE FAILURE, distinct from `[]` ("the model said there are none").
    The caller must not write the `_none_` sentinel for None: doing so
    would remove the row from the `empty(entities)` rescan permanently on
    the strength of a garbled reply.
    """
    match = re.search(r"\[\s*(?:\"[^\"]*\"(?:\s*,\s*\"[^\"]*\")*)?\s*\]", text)
    if not match:
        return None
    try:
        raw = json.loads(match.group(0))
    except (ValueError, TypeError):
        return None
    out: list[str] = []
    seen: set[str] = set()
    for v in raw:
        if not isinstance(v, str):
            continue
        norm = canonical_entity_text(v)
        if not norm or norm in seen:
            continue
        seen.add(norm)
        out.append(norm)
        if len(out) >= 8:
            break
    return out


@staged("entity_extract")
async def _extract(value_str: str) -> list[str] | None:
    from server.llm.client import get_llm_client
    from server.memory.app.extract import with_strict_examples_preamble

    client = get_llm_client()
    result = await client.complete(
        with_strict_examples_preamble(_EXTRACT_PROMPT),
        value_str[:2000],
        max_tokens=llm_max_tokens("entity_extract"),
    )
    return _parse_entities(result.text)


# The candidate read is a queue. Every pass reads the keys
# of memories created in the last `recent_seconds` (a fresh row written
# without entities is minutes old; on a quiet store this is a 0.1 MB read)
# and then draws the rest of its batch from a queue of backlog keys that one
# whole-table key scan (no value column) refills every `full_scan_seconds`
# or when it runs dry. Phase two reads type and value for exactly the keys
# the pass will work on. Module state, like the entity heal's clock
# and the embedding worker's orphan scan.
_backlog_keys: deque[dict[str, Any]] = deque()
_backlog_filled_at: float = 0.0


def _scan_settings() -> tuple[int, int, int]:
    from server.settings import settings

    return (
        int(settings.entity_extract_full_scan_seconds),
        int(settings.entity_extract_recent_seconds),
        int(settings.entity_extract_full_scan_keys),
    )


def _backlog_per_namespace() -> int:
    from server.settings import settings

    return int(settings.entity_extract_backlog_per_namespace)


def interleave_by_namespace(keys: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """The backlog queue takes namespaces in turn, each one's
    keys in the order they came (oldest first), so one large namespace cannot
    hold the head of the queue."""
    groups: dict[tuple[str, str], list[dict[str, Any]]] = {}
    for k in keys:
        groups.setdefault((str(k.get("org")), str(k.get("namespace"))), []).append(k)
    out: list[dict[str, Any]] = []
    queues = list(groups.values())
    i = 0
    while queues:
        q = queues[i % len(queues)]
        out.append(q.pop(0))
        if not q:
            queues.remove(q)
        else:
            i += 1
    return out


def _backlog_due(*, now: float | None = None) -> bool:
    """Whether the whole-table key scan runs this pass: the queue is empty,
    or the slow clock has elapsed since it was last filled."""
    full_every, recent, _ = _scan_settings()
    if full_every <= 0 or recent <= 0:
        return True  # the clock is off: every pass is a full scan
    t = time.monotonic() if now is None else now
    return not _backlog_keys or _backlog_filled_at <= 0.0 or t - _backlog_filled_at >= full_every


def _entity_update(r: dict[str, Any], entities: list[str]) -> dict[str, Any]:
    """One row of the pass's batch write: the row's identity, the version and content hash
    read, and its entities."""
    read = {c: r[c] for c in (*IDENTITY_COLUMNS, "version", "content_sha256")}
    return read | {"entities": entities}


async def _next_candidates(cfg: EntityExtractorConfig) -> list[dict[str, Any]]:
    """The rows this pass works on: fresh rows first (the windowed one-shot
    read), then backlog keys from the queue, read in one phase-two call. Under Postgres
    only the rows the authority still serves at the version read (`served_rows`), so a
    value deleted, revised or redacted there but not yet in ClickHouse never goes to the
    model. Fresh rows it no longer serves free their slots for backlog keys; a backlog
    key it no longer serves is dropped and its slot goes unused this pass."""
    global _backlog_filled_at
    import server.memory.store as memory_repo

    full_every, recent, full_keys = _scan_settings()
    # An org whose model calls are switched off keeps its rows for later.
    off = await customer.switched_off_orgs()
    if full_every <= 0 or recent <= 0:
        return await memory_repo.served_rows(
            await memory_repo.find_unentitied_memories(
                batch_size=cfg.batch_size,
                org_filter=cfg.org_filter,
                namespace_filter=cfg.namespace_filter,
                exclude_orgs=off,
            )
        )
    rows = await memory_repo.find_unentitied_memories(
        batch_size=cfg.batch_size,
        org_filter=cfg.org_filter,
        namespace_filter=cfg.namespace_filter,
        since_seconds=recent,
        exclude_orgs=off,
    )
    rows = await memory_repo.served_rows(rows)
    if len(rows) >= cfg.batch_size:
        return rows
    taken = {identity_key(r) for r in rows}
    try:
        if _backlog_due():
            scanned = await memory_repo.find_unentitied_keys(
                batch_size=max(full_keys, cfg.batch_size),
                org_filter=cfg.org_filter,
                namespace_filter=cfg.namespace_filter,
                per_namespace=_backlog_per_namespace(),
                exclude_orgs=off,
            )
            _backlog_keys.clear()
            _backlog_keys.extend(interleave_by_namespace(scanned))
            _backlog_filled_at = time.monotonic()
        keys: list[dict[str, Any]] = []
        while len(rows) + len(keys) < cfg.batch_size and _backlog_keys:
            k = _backlog_keys.popleft()
            key = identity_key(k)
            if key in taken:
                continue
            taken.add(key)
            keys.append(k)
        if keys:
            rows += await memory_repo.served_rows(await memory_repo.read_unentitied_rows(keys))
    except Exception as e:  # noqa: BLE001 — the backlog never costs the fresh rows their pass
        log.warning("entity_extractor: backlog scan skipped this pass: %s", e)
        pass_context.note_failure(e, "backlog_scan")
    return rows


async def run_pass(
    cfg: EntityExtractorConfig | None = None,
) -> EntityExtractorResult:
    """One batch: find memories with empty entities, extract, update."""
    cfg = cfg or EntityExtractorConfig()
    result = EntityExtractorResult()

    import server.memory.store as memory_repo
    from server.observability import cost_accumulator

    with record_pass("entity_extractor") as rec:
        rows = await _next_candidates(cfg)
        if not rows:
            # Nothing to extract — the supervisor sleeps the
            # idle interval; the first pass with a row brings the interval back.
            pass_context.mark_idle()

        # The 24 h cost-cap verdict for every namespace of
        # this pass's rows in ONE statement, before the LLM loop — not two
        # point reads per namespace per pass (a `sum(upstream_cost_usd)` scan
        # of agent_events each).
        with_value = [r for r in rows if (r.get("value") or "").strip()]
        result.skipped_no_value += len(rows) - len(with_value)
        over_cap = (
            await namespaces_over_cap({(r["org"], r["namespace"]) for r in with_value})
            if with_value
            else {}
        )
        # The rows this pass extracted; written once, after the loop, in
        # five statements for the batch instead of five per row.
        updates: list[dict[str, Any]] = []

        for r in with_value:
            value_str = r.get("value") or ""
            if value_reads_high(value_str):
                # A row whose value reads as high, whatever its label, never goes to the
                # model; the sentinel keeps the next pass from picking it up.
                updates.append(_entity_update(r, [_NONE_SENTINEL]))
                result.skipped_sensitive += 1
                continue
            if over_cap.get((r["org"], r["namespace"]), False):
                result.skipped_over_budget += 1
                continue
            if await customer.switched_off(str(r["org"])):
                # A backlog key queued before the switch: left for later, no sentinel.
                result.skipped_switched_off += 1
                continue
            row_started = time.monotonic()
            cost_accumulator.begin_op()
            try:
                with customer.for_org(
                    str(r["org"]),
                    namespace=str(r["namespace"]),
                    subject=subject_of(str(r.get("actor") or "")),
                    user_id=str(r["user_id"]),
                ):
                    entities = await _extract(value_str)
            except customer.HOLDS:
                # Switched off since the scan, or the switch unread: left for later, no sentinel.
                result.skipped_switched_off += 1
                continue
            except Exception as e:  # noqa: BLE001 — one row's failure must not stop the pass
                result.failures.append(
                    f"{r['org']}/{r['namespace']}/{r['memory_key']}: {type(e).__name__}: {e}"
                )
                emit_worker_cost_event_for(
                    org=r["org"],
                    namespace=r["namespace"],
                    operation="worker.entity_extractor",
                    started=row_started,
                )
                continue
            if entities is None:
                # Parse failure — NOT "no entities". Leave the row for the
                # next pass; after `memory_entity_extract_parse_fail_max` consecutive failures stamp
                # the distinct `_parse_failed_` sentinel so it stops costing
                # an LLM call per pass.
                result.parse_failures += 1
                row_key = identity_key(r)
                streak = _note_parse_failure(row_key)
                emit_worker_cost_event_for(
                    org=r["org"],
                    namespace=r["namespace"],
                    operation="worker.entity_extractor",
                    started=row_started,
                )
                record_drop(
                    r["org"],
                    r["namespace"],
                    "entity.extract",
                    "parse_failed",
                    item=r["memory_key"],
                )
                if streak < settings.memory_entity_extract_parse_fail_max:
                    log.warning(
                        "entity_extractor: unparseable LLM reply for %s/%s/%s "
                        "(attempt %d/%d); leaving row for next pass",
                        r["org"],
                        r["namespace"],
                        r["memory_key"],
                        streak,
                        settings.memory_entity_extract_parse_fail_max,
                    )
                    continue
                record_drop(
                    r["org"],
                    r["namespace"],
                    "entity.extract",
                    "parse_poisoned",
                    item=r["memory_key"],
                )
                log.warning(
                    "entity_extractor: %d consecutive unparseable replies for "
                    "%s/%s/%s; writing %r sentinel",
                    streak,
                    r["org"],
                    r["namespace"],
                    r["memory_key"],
                    _PARSE_FAILED_SENTINEL,
                )
                _parse_fail_streaks.pop(row_key, None)
                result.parse_poisoned += 1
                entities = [_PARSE_FAILED_SENTINEL]
            elif not entities:
                # Sentinel so the empty() filter stops re-scanning a row
                # that genuinely has no entities.
                entities = [_NONE_SENTINEL]
            updates.append(_entity_update(r, entities))
            result.extracted.append(
                {
                    "org": r["org"],
                    "namespace": r["namespace"],
                    "memory_key": r["memory_key"],
                    "entities": entities,
                }
            )
            emit_worker_cost_event_for(
                org=r["org"],
                namespace=r["namespace"],
                operation="worker.entity_extractor",
                started=row_started,
            )
        if updates:
            result.rows_written = await memory_repo.apply_entities_batch(updates)
        # Self-heal the mirror: a pass that finds nothing new to extract is
        # still the right moment to close the divergence the gauge reports —
        # but the join behind it costs 268 MB of reads, so on its own clock
        # (entity_heal_interval_seconds), not every pass.
        if _heal_due():
            try:
                result.healed_divergent = await memory_repo.heal_divergent_entities(limit=200)
                if result.healed_divergent:
                    log.info(
                        "entity_extractor: re-emitted %d divergent mirror rows",
                        result.healed_divergent,
                    )
            except Exception as e:  # noqa: BLE001 — heal is additive, never fatal
                log.warning("entity_extractor: divergence heal failed: %s", e)
                pass_context.note_failure(e, "heal")
        rec.outcome = {
            "extracted": len(result.extracted),
            "skipped_no_value": result.skipped_no_value,
            "skipped_over_budget": result.skipped_over_budget,
            "parse_failures": result.parse_failures,
            "parse_poisoned": result.parse_poisoned,
            "skipped_sensitive": result.skipped_sensitive,
            "failed": len(result.failures),
            "healed_divergent": result.healed_divergent,
            "memory_rows_written": int(result.rows_written.get("memory", 0)),
            "mirror_rows_written": int(result.rows_written.get("memory_embeddings", 0)),
            "idle": not rows,
        }

    if result.extracted or result.failures or result.parse_failures:
        log.info(
            "entity_extractor: extracted=%d skipped=%d parse_failed=%d parse_poisoned=%d failed=%d",
            len(result.extracted),
            result.skipped_no_value,
            result.parse_failures,
            result.parse_poisoned,
            len(result.failures),
        )
    return result


__all__ = ["EntityExtractorConfig", "EntityExtractorResult", "run_pass"]
