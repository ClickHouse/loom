"""Blacklist sweep: a pattern reaches the rows written before it.

`blacklist.add` refuses future writes (422 on `memory.set`, dropped entries on the bulk
path) and filters search. A row stored before the pattern existed stays in the table,
and readers that do not go through search still see it. The sweep finds the live rows a
pattern matches — inside the caller's org, and inside the namespace for a
namespace-scoped pattern — and tombstones them through the one by-id funnel that also
mirrors the tombstone onto the embeddings table, leaving a `memory_drops` row per
pattern so the ledger says what a Refuse removed, and a `memory.tombstoned` event per
row for the namespace's webhooks. A token narrowed to one user never
reaches a sweep (no blacklist op is in NARROW_SAFE_OPS).

Matching is the SQL twin of `server.memory.app.blacklist_guard.find_match`: `position(value,
pattern) > 0` for a substring pattern (case-sensitive, like the in-process
check) and `match(value, pattern)` for a regex. Both run RE2, and the write path
compiles with `.` matching a newline as ClickHouse does, so a sweep tombstones the
rows a write of the same value would be refused for.

Under the Postgres authority ClickHouse may trail it. The sweep first waits until
ClickHouse holds the memory rows committed in the pattern's scope (503
PROJECTION_BEHIND past the bound), matches with a sequential read, then reaches the live
heads ClickHouse keeps refusing (a parked delivery) a page at a time, and judges every
head the authority holds with the same match, so it tombstones only what the authority
still serves matching.
"""

from __future__ import annotations

import itertools
import logging
from collections.abc import Callable
from contextlib import nullcontext
from dataclasses import dataclass, field
from datetime import datetime
from operator import itemgetter
from typing import Any

from server.core import pg
from server.core.caller_regex import compile_caller_regex
from server.core.ch import sequential_reads
from server.core.datetimes import naive_utc_now
from server.core.error_summary import error_summary
from server.core.errors import LoomServerError, ValidationError, caller_text
from server.memory.app import webhooks
from server.memory.domain.expire_reasons import SWEEP_REASON_PREFIX
from server.memory.store.rows import MemoryRow, stored_value_text
from server.settings import settings

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class SweepResult:
    pattern_id: str
    matched: int
    tombstoned: int
    truncated: bool
    error: str = ""


def sweep_where(pattern_type: str, scope: str, namespace: str) -> tuple[str, dict[str, Any]]:
    """The predicate's variable part and its params (org/pattern added by caller)."""
    parts = ["org = {org:String}", "tombstoned_at IS NULL", "status != 'tombstoned'"]
    params: dict[str, Any] = {}
    if scope == "namespace":
        parts.append("namespace = {ns:String}")
        params["ns"] = namespace
    if pattern_type == "regex":
        parts.append("match(value, {pat:String})")
    else:
        parts.append("position(value, {pat:String}) > 0")
    return " AND ".join(parts), params


def _matcher(pattern: str, pattern_type: str) -> Callable[[str], bool]:
    """The sweep's ClickHouse predicate on a value's stored text: a substring, or the RE2
    pattern the write path runs (`.` matching a newline, as ClickHouse's `match` has it)."""
    if pattern_type == "regex":
        from server.memory.app.blacklist_guard import compile_regex

        compiled = compile_regex(pattern)
        return lambda text: compiled.search(text) is not None
    return lambda text: pattern in text


def sweep_error_text(e: Exception) -> str:
    """A failure as the sweep answers it, "<code>: <message>": a Loom error's own code and
    message, SWEEP_FAILED for any other (the server log has its summary)."""
    if isinstance(e, LoomServerError):
        return f"{e.code}: {e}"[:200]
    return "SWEEP_FAILED: the sweep did not finish; the server log has the cause"


@dataclass
class _Tombstoner:
    """A sweep's tombstones, a namespace at a time (one restored from another's snapshot
    keeps the memory ids), each group announced and recorded as it lands."""

    org: str
    pattern_id: str
    ch: Any
    skip: Callable[[MemoryRow], bool]
    when: datetime = field(default_factory=naive_utc_now)
    landed: list[tuple[str, str]] = field(default_factory=list)  # (namespace, memory_id)

    @property
    def reason(self) -> str:
        return f"{SWEEP_REASON_PREFIX}{self.pattern_id}"[:512]

    async def tombstone(self, ids: set[tuple[str, str]]) -> None:
        import server.memory.store as memory_repo
        from server.memory.app.drops import record_drop

        for ns, group in itertools.groupby(sorted(ids), key=itemgetter(0)):
            done = await memory_repo.tombstone_cascade_by_ids(
                self.org,
                [mid for _, mid in group],
                when=self.when,
                update=lambda _e: {"expire_reason": self.reason},
                ch=self.ch,
                namespace=ns,
                skip=self.skip,
            )
            await webhooks.announce([("memory.tombstoned", change) for change in done])
            record_drop(self.org, ns, "blacklist.sweep", f"pattern:{self.pattern_id}", n=len(done))
            self.landed.extend((ns, str(change["memory_id"])) for change in done)


async def sweep_pattern(
    *,
    org: str,
    namespace: str,
    pattern_id: str,
    pattern: str,
    pattern_type: str,
    scope: str,
    limit: int | None = None,
    ch: Any = None,
) -> SweepResult:
    """Tombstone the live rows the pattern matches: up to `limit` that ClickHouse matched,
    and under Postgres every live head whose row ClickHouse keeps refusing, a page at a
    time (so a sweep takes longer with the parked backlog `GET /v1/health?include=postgres`
    reports). A failed match query is answered in `error`, as is a failure after a row
    may have landed, with what is known to have; one while tombstoning before that
    raises."""
    if limit is None:
        limit = settings.memory_blacklist_sweep_limit
    from server.core.repo_base import _default_client
    from server.memory.app.search import pg_validation
    from server.memory.store import pg_memory

    if pattern_type == "regex":
        # ClickHouse's match() is RE2: a pattern RE2 refuses is the caller's to fix, and
        # RE2's reason names only the pattern.
        try:
            compile_caller_regex(pattern)
        except ValidationError as e:
            return SweepResult(pattern_id, 0, 0, False, error=e.message[:200])
    ch = ch or _default_client()
    authority = pg.enabled()
    where, params = sweep_where(pattern_type, scope, namespace)
    params.update({"org": org, "pat": pattern, "lim": int(limit) + 1})
    scoped = namespace if scope == "namespace" else None
    await pg_validation.admit(org, None if scoped is None else [scoped], owed=pg_memory.MEMORY_ROW)
    try:
        # Under Postgres every head the authority holds is judged by ClickHouse's match.
        matches = _matcher(pattern, pattern_type) if authority else None
        # A write acts on what it reads: a row published moments ago is matched too.
        with sequential_reads() if authority else nullcontext():
            rows = await ch.query_all(
                f"""
                SELECT memory_id, namespace FROM loom.memory FINAL
                WHERE {where}
                LIMIT {{lim:UInt32}}
                """,
                params=params,
            )
    except Exception as e:  # noqa: BLE001 — a failed sweep must not fail the add
        log.warning("blacklist sweep %s: match query failed: %s", pattern_id, error_summary(e))
        # The driver's text can carry the statement and the server's address.
        return SweepResult(
            pattern_id, 0, 0, False, error=f"the match query failed ({caller_text(e)})"
        )

    def no_match(head: Any) -> bool:
        return matches is not None and not matches(stored_value_text(head))

    truncated = len(rows) > limit
    rows = rows[:limit]
    found = {(str(r["namespace"]), str(r["memory_id"])) for r in rows}
    tombstoner = _Tombstoner(org, pattern_id, ch, no_match)

    def result(error: str = "") -> SweepResult:
        # What ClickHouse matched, and the parked heads it tombstoned.
        landed = tombstoner.landed
        unseen = sum(row not in found for row in landed)
        return SweepResult(pattern_id, len(rows) + unseen, len(landed), truncated, error)

    try:
        await tombstoner.tombstone(found)
        after: tuple[str, int] | None = None
        while matches is not None:  # under Postgres
            page = await pg_memory.parked_heads(org, scoped, after=after, limit=limit)
            # Judged here, from the text the page read; the cascade judges its own read too.
            await tombstoner.tombstone(
                {(h.namespace, h.memory_id) for h in page if matches(h.value_text)} - found
            )
            if len(page) < limit:
                break
            after = (page[-1].namespace, page[-1].seq)
    except Exception as e:  # once a row may have landed, the answer names what did
        if not tombstoner.landed and not pg.may_have_committed(e):
            raise
        # A summary only: the rows this path writes match the pattern, often a secret.
        log.warning(
            "blacklist sweep %s: stopped after %d: %s",
            pattern_id,
            len(tombstoner.landed),
            error_summary(e),
        )
        return result(sweep_error_text(e))
    return result()
