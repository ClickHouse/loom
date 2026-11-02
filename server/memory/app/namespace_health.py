"""The cheap health summary of a namespace, for the moments the agent listens.

An `advisories` field on the `memory.set` response and `namespace.audit` (an op
the agent has to choose to call) sit where a misbehaving agent never looks; a
namespace can collect thousands of exact-key gets and double-encoded values
without the agent hearing of it. This module is the store telling it: the four
audit checks that are cheap and decisive — `exact_key_store`,
`double_encoded_values`, `unembedded_rows`, `coordination_as_memory` — run over
the two summary reads they need, reduced to counts, shares and remedies (no samples), cached per
(org, namespace) for `settings.namespace_health_ttl_seconds`, and rendered
as one compact block that `context.pack` places after the profile section
when the namespace is unhealthy. A healthy namespace renders nothing.

The audit's own SQL runs; nothing is duplicated here. A lookup failure
returns None and is cached briefly so a pack never pays for it twice in a
row. Nothing raises.
"""

from __future__ import annotations

import logging
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime

from server.core.datetimes import naive_utc_now
from server.settings import settings

log = logging.getLogger(__name__)

# The four checks: each one is one read (the ledger, or a FINAL-scoped read
# of loom.memory / loom.memory_embeddings), and each one critical or warning
# says the namespace is being used as something other than a memory. The
# index-row scan, the versions read and the shape checks that only cost
# storage stay in the full op. `hash_shaped_queries` is not here: the
# ledger's `args['query']` is a redaction (`sha256:<16 hex>/<length>`,
# server/observability/event_args.py), never the query text, so that check
# reads 100 % on every namespace.
HEALTH_CHECKS: tuple[str, ...] = (
    "exact_key_store",
    "double_encoded_values",
    "unembedded_rows",
    "coordination_as_memory",
)
# The summary reads the four checks take their populations from (live and
# searchable rows; gets and searches). The embeddings summary is not needed.
HEALTH_SUMMARY_STEPS: tuple[str, ...] = ("summary_live", "summary_ledger")

# The pack block: one line per finding, hard-capped in characters.
RENDER_CHARS = 400
# Process cache size cap: (org, namespace) pairs held.
CACHE_MAX_ENTRIES = 2048
_TAIL = "Run `namespace.audit` for samples; `loom-optimize` reworks it with redirects."


@dataclass(frozen=True)
class HealthFinding:
    code: str
    severity: str  # critical | warning
    count: int
    share: float  # 0..1 of the population the check looked at
    remedy: str


@dataclass(frozen=True)
class NamespaceHealth:
    criticals: int
    warnings: int
    findings: tuple[HealthFinding, ...] = field(default_factory=tuple)
    computed_at: datetime = field(default_factory=lambda: naive_utc_now())

    @property
    def unhealthy(self) -> bool:
        """Worth a line in the pack: one critical, or two warnings."""
        return self.criticals >= 1 or self.warnings >= 2


# (org, namespace) -> (expires_at on the monotonic clock, health or None for a failure)
_cache: dict[tuple[str, str], tuple[float, NamespaceHealth | None]] = {}


def reset_cache() -> None:
    _cache.clear()


def _cache_put(key: tuple[str, str], expires_at: float, value: NamespaceHealth | None) -> None:
    if key not in _cache and len(_cache) >= CACHE_MAX_ENTRIES:
        # Drop the entry closest to expiry; a bounded dict, not an LRU.
        oldest = min(_cache, key=lambda k: _cache[k][0])
        _cache.pop(oldest, None)
    _cache[key] = (expires_at, value)


async def _compute(org: str, namespace: str, window_seconds: int) -> NamespaceHealth | None:
    from server.identity.api.models_namespace_catalog import NamespaceAuditInput
    from server.identity.app import namespace_audit as na

    inp = NamespaceAuditInput(
        org=org, namespace=namespace, window_seconds=int(window_seconds), sample_limit=0
    )
    audit = na.new_audit(inp)
    await na.run_checks(audit, HEALTH_CHECKS, summary_steps=HEALTH_SUMMARY_STEPS)
    if audit.rules.skipped:
        # A check that could not run leaves the answer unknown; a "healthy"
        # built from zero counts would be cached for an hour and be wrong.
        log.warning("namespace_health %s/%s: %s", org, namespace, "; ".join(audit.rules.skipped))
        return None
    findings = tuple(
        HealthFinding(
            code=f.code,
            severity=f.severity,
            count=int(f.count),
            share=float(f.share),
            remedy=f.remedy,
        )
        for f in audit.findings
        if f.severity in ("critical", "warning")
    )
    return NamespaceHealth(
        criticals=sum(1 for f in findings if f.severity == "critical"),
        warnings=sum(1 for f in findings if f.severity == "warning"),
        findings=findings,
    )


async def namespace_health(
    org: str,
    namespace: str,
    *,
    window_seconds: int = 7 * 86400,
    clock: Callable[[], float] = time.monotonic,
) -> NamespaceHealth | None:
    """The cached health of (org, namespace); None when the TTL is 0 (the
    section is off), when the lookup failed, or when a failure is still
    remembered. Never raises."""
    ttl = int(settings.namespace_health_ttl_seconds)
    if ttl <= 0:
        return None
    key = (str(org), str(namespace))
    now = clock()
    hit = _cache.get(key)
    if hit is not None and hit[0] > now:
        return hit[1]
    try:
        health = await _compute(org, namespace, window_seconds)
    except Exception as exc:  # noqa: BLE001 — advisory; the pack must not fail on it
        log.warning("namespace_health %s/%s failed: %s", org, namespace, exc)
        health = None
    _cache_put(
        key,
        now
        + (ttl if health is not None else min(ttl, settings.memory_namespace_health_failure_ttl_s)),
        health,
    )
    return health


def _first_clause(remedy: str) -> str:
    """The first clause of a remedy: up to its first `;` or sentence end,
    parenthetical dropped."""
    text = remedy.strip()
    cut = len(text)
    for sep in (";", ". ", " — "):
        i = text.find(sep)
        if 0 < i < cut:
            cut = i
    clause = text[:cut].rstrip(". ")
    open_ = clause.find(" (")
    if open_ > 0 and clause.endswith(")"):
        clause = clause[:open_]
    return clause


def render_health(namespace: str, health: NamespaceHealth, *, cap: int = RENDER_CHARS) -> str:
    """The pack block, or "" when the namespace is healthy. Criticals first,
    then by share; findings are dropped from the end until the block fits
    `cap`, and the tail naming the op and the skill is always kept."""
    if not health.unhealthy:
        return ""
    head = f"Namespace {namespace}: {health.criticals} critical, {health.warnings} warning"
    ordered = sorted(
        health.findings, key=lambda f: (f.severity != "critical", -f.share, -f.count, f.code)
    )
    items = [f"{f.code}: {f.count} ({f.share:.0%}) → {_first_clause(f.remedy)}" for f in ordered]
    while True:
        body = f"{head} — {'; '.join(items)}. {_TAIL}" if items else f"{head}. {_TAIL}"
        if len(body) <= cap or not items:
            break
        items.pop()
    return body[:cap]
