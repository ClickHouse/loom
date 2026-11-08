"""Per-(namespace, op-class) token bucket rate limiter.

Configured caps target the deployment-wide rate; each replica's cap is
`configured / replicas`, where replicas is the hosts with a live serving
`loom.instances` row (`replica_count()`), floored at LOOM_REPLICA_COUNT. That
correction is approximate — a runaway tenant can hit each replica fresh, so
aggregate burst can be up to N× the per-replica burst.

Within one replica the web workers share one table: a
memory-mapped file under /tmp holds the buckets for every process of a
`uvicorn --workers N` server, so a keep-alive client pinned to one worker
sees the whole per-replica cap, not 1/N of it. When the table cannot be
opened, or the server runs one worker, buckets live in this process and
the cap is divided by the worker count;
`/v1/health` reports which (`ratelimit.backend`).

Bucket fill is per host on purpose: it refills in seconds, and a ClickHouse
round trip per request is the wrong trade. The admin overrides are the
deployment's and live in loom.runtime_overrides (`adopt`).
"""

from __future__ import annotations

import functools
import json
import logging
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from server.core.errors import error_kind
from server.settings import settings

if TYPE_CHECKING:
    from server.core.ratelimit_shared import SharedBucketStore

log = logging.getLogger("loom.ratelimit")

# Op-class resolution: ops sharing a class share a token bucket. Derived from
# the dotted prefix (total coverage by construction); overrides catch ops the
# prefix rule would misfile.
_OP_CLASS_OVERRIDES: dict[str, str] = {
    # A read; the prefix rule would file it under the write-shaped add bucket.
    "knowledge.search": "knowledge.search",
    # A read too, and a scan of up to four datasets: its own bucket, so searches and
    # queries never spend each other's budget.
    "knowledge.query": "knowledge.query",
    # Cheap high-volume telemetry — rides the big memory bucket, not the
    # read-shaped code bucket.
    "code.outcome": "memory",
    # One call embeds and conflict-checks up to 200 items: its own bucket, sized like
    # knowledge.add, so an import never spends the namespace's memory budget.
    "memory.import": "memory.import",
    # Up to a statement's time cap of ClickHouse each: its own bucket, so analysis
    # never spends the budget of the ops filed under default.
    "insights.query": "insights.query",
    # One model-backed answer each, its tool calls a handful of reads: its own bucket,
    # so answers never spend the budget of the ops filed under default.
    "assistant.ask": "assistant.ask",
}

# Exact-key memory reads: one primary-key lookup each, and the shape an agent that uses loom
# as a key-value store issues in bursts (hundreds of `memory.get` a minute beside a couple
# of `memory.set`). They draw from their own bucket so a read storm never spends the budget
# the namespace's writes and searches need, and a write burst never refuses a read.
# `memory.list` (pages a store) and `memory.search` (an embedding call plus two legs) stay
# in `memory`: neither is one lookup.
_EXACT_KEY_READS: frozenset[str] = frozenset(
    {
        "memory.get",
        "memory.bulk_get",
        "memory.history",
        "memory.lineage",
        "memory.provenance",
        "memory.related",
        "memory.backlinks",
        "working_memory.get",
        "working_memory.render",
    }
)

# Knowledge reads other than search and query: a document, a listing, a job's status. A
# dashboard page or an agent polling an ingest issues several in a row, which the
# write-shaped knowledge.add bucket (burst 10) would refuse, and a read burst would then
# refuse the namespace's next write. knowledge.inspect stays in knowledge.add: it reports
# on the whole namespace, so the tight bucket bounds it.
_KNOWLEDGE_READS: frozenset[str] = frozenset(
    {
        "knowledge.dataset_get",
        "knowledge.formats",
        "knowledge.get",
        "knowledge.ingest_status",
        "knowledge.list",
        "knowledge.outline",
        "knowledge.reads",
        "knowledge.source.inspect",
        "knowledge.source.list",
    }
)


def op_class_for(op_name: str) -> str:
    """Resolve an operation to its rate-limit bucket class (total coverage)."""
    override = _OP_CLASS_OVERRIDES.get(op_name)
    if override is not None:
        return override
    if op_name in _EXACT_KEY_READS:
        return "memory.read"
    if op_name in _KNOWLEDGE_READS:
        return "knowledge.read"
    prefix = op_name.split(".", 1)[0]
    if prefix in ("memory", "conversation"):
        return "memory"
    if prefix == "knowledge":
        return "knowledge.add"
    if prefix == "code":
        return "code"
    return "default"


@dataclass
class Limit:
    """A token-bucket configuration: `capacity` burst, refilled at `rate` tokens/sec."""

    capacity: float
    rate: float  # tokens per second


# Per-namespace defaults: burst capacity, sustained rate (tokens per second).
DEFAULT_LIMITS: dict[str, Limit] = {
    "memory": Limit(capacity=100, rate=17),  # 1000/min sustained
    # Exact-key reads: ~3000/min sustained, burst 300. The
    # observed peak was 761 gets in one minute from one agent; a get is one
    # primary-key read, so the cap is bounded by fairness, not by cost.
    "memory.read": Limit(capacity=300, rate=50),
    "knowledge.search": Limit(capacity=20, rate=3),  # ~200/min
    "knowledge.query": Limit(capacity=20, rate=3),  # ~200/min
    "knowledge.add": Limit(capacity=10, rate=2),  # ~100/min (embedding is downstream-limited)
    "knowledge.read": Limit(capacity=50, rate=10),  # ~600/min, as default: no embedding call
    "memory.import": Limit(capacity=10, rate=2),  # each call up to 200 embedded items
    # Read-shaped code-intelligence ops; agents burst on session start.
    "code": Limit(capacity=60, rate=10),  # ~600/min
    # One SQL statement per call, each up to the analysis time cap.
    "insights.query": Limit(capacity=20, rate=3),  # ~180/min
    # One answer per call, each held to the assistant's budget share and its
    # per-replica limit; the bucket bounds a burst of asks before those apply.
    "assistant.ask": Limit(capacity=10, rate=1),  # ~60/min
    "default": Limit(capacity=50, rate=10),
}

# Per-org defaults: 3× the per-namespace caps — bounds total fan-out so a
# tenant with many namespaces can't sustain N× a single namespace's baseline.
DEFAULT_ORG_LIMITS: dict[str, Limit] = {
    op_class: Limit(capacity=lim.capacity * 3, rate=lim.rate * 3)
    for op_class, lim in DEFAULT_LIMITS.items()
}

# Per-actor tier: one verified token subject cannot spend a
# namespace's whole budget on its own. Keyed per (namespace, actor, op-class)
# — a service token that legitimately spans many namespaces keeps a budget in
# each — and sized as a FRACTION of that namespace's limit
# (LOOM_RATELIMIT_ACTOR_FRACTION). The default fraction is 1.0, which means
# the tier is neutral: no actor bucket is created, headers and 429s are
# byte-identical to the two-tier limiter. An operator who wants a guarantee
# that a second credential always has budget sets it to 0.6 (or lower). A
# memory-extraction attack needs only a few dozen prompts, so a rate limit is
# not the control — the exposure ledger
# and the read scope are — but the tier is what makes one credential's sweep
# legible next to its peers' traffic. Explicit set_actor_limit overrides
# always create a bucket, whatever the fraction.


def actor_fraction() -> float:
    try:
        frac = float(settings.ratelimit_actor_fraction)
    except Exception:  # noqa: BLE001 — an unusable fraction falls back to the neutral tier
        return 1.0
    return frac if frac > 0 else 1.0


# Cap to prevent unbounded growth from misbehaving tenants
MAX_BUCKETS = 10_000


# Traffic classes. `production` and `interactive` draw from
# the base bucket of every tier — the key the two-tier limiter always used and
# the one the operator's caps (`admin/ratelimit.set`, the defaults) bound — so
# a request that carries no class (a direct caller, a test) and a request
# from an unregistered namespace (interactive) pay the base cap; a class is
# never a way around a configured cap. Only `bench` is
# carved out: its own bucket, keyed with the class, sized as
# LOOM_RATELIMIT_CLASS_FRACTIONS["bench"] of the base cap (absent = 1.0). A
# bench sweep that empties its bucket has touched nothing production draws
# from; production's share is reserved by construction, not by arithmetic.
CARVED_CLASSES: frozenset[str] = frozenset({"bench"})
_CLASS_FRACTIONS_RAW: str | None = None
_CLASS_FRACTIONS: dict[str, float] = {}


def class_fractions() -> dict[str, float]:
    """`ratelimit_class_fractions` parsed; re-parsed only when the raw string
    changes. A malformed map is logged once and read as empty (every class
    at the base cap) — a bad override must not take the limiter down."""
    global _CLASS_FRACTIONS_RAW, _CLASS_FRACTIONS
    raw = str(settings.ratelimit_class_fractions)
    if raw == _CLASS_FRACTIONS_RAW:
        return _CLASS_FRACTIONS
    parsed: dict[str, float] = {}
    if raw.strip():
        try:
            obj = json.loads(raw)
            if not isinstance(obj, dict):
                raise ValueError("not a JSON object")
            for cls, value in obj.items():
                frac = float(value)
                if not frac > 0:
                    raise ValueError(f"{cls!r}: fraction must be > 0, got {value!r}")
                parsed[str(cls).strip().lower()] = frac
        except (ValueError, TypeError) as exc:
            log.warning("LOOM_RATELIMIT_CLASS_FRACTIONS ignored: %s", exc)
            parsed = {}
    _CLASS_FRACTIONS_RAW = raw
    _CLASS_FRACTIONS = parsed
    return parsed


def bucket_class(traffic_class: str | None) -> str:
    """The class component of a bucket key: the class for a carved-out class
    (`bench`), '' for every other — production, interactive and an
    unclassified check all draw from the base bucket."""
    cls = str(traffic_class or "").strip().lower()
    return cls if cls in CARVED_CLASSES else ""


def _scale_for_class(limit: Limit, traffic_class: str) -> Limit:
    """A carved-out class's bucket: the base limit × its fraction, the
    capacity floored at one token so a tiny fraction still admits a request."""
    cls = bucket_class(traffic_class)
    if not cls:
        return limit
    frac = class_fractions().get(cls, 1.0)
    if frac == 1.0:
        return limit
    return Limit(capacity=max(1.0, limit.capacity * frac), rate=limit.rate * frac)


def _scale_for_replicas(base: Limit, *, shared: bool = False) -> Limit:
    """Divide a deployment-wide cap by the processes that hold separate copies
    of it: replicas × web workers with per-process buckets, replicas alone
    when the workers share one table.

    Floored at the configured cap (n>=1) so a misconfig doesn't open the throttle,
    and the burst at one request.
    """
    n = effective_divisor(shared=shared)
    if n == 1:
        return base
    # A request costs one token: a share below one would refuse every request.
    return Limit(capacity=max(1.0, base.capacity / n), rate=base.rate / n)


class TokenBucket:
    __slots__ = ("_clock", "capacity", "last_refill", "limit_of", "rate", "tokens")

    def __init__(
        self,
        limit: Limit,
        *,
        clock: Callable[[], float] = time.monotonic,
        limit_of: Callable[[], Limit | None] | None = None,
    ) -> None:
        # `clock` is injectable for tests. MUST be monotonic: refill math
        # assumes elapsed >= 0; a backward jump leaks tokens.
        self._clock = clock
        # What the bucket's limit is now: asked again when the replica count moves.
        self.limit_of = limit_of
        self.capacity = limit.capacity
        self.rate = limit.rate
        self.tokens = limit.capacity  # start full
        self.last_refill = clock()

    def _refill(self) -> None:
        now = self._clock()
        elapsed = now - self.last_refill
        self.tokens = min(self.capacity, self.tokens + elapsed * self.rate)
        self.last_refill = now

    def try_acquire(self, cost: float = 1.0) -> bool:
        self._refill()
        if self.tokens >= cost:
            self.tokens -= cost
            return True
        return False

    def seconds_until_available(self, cost: float = 1.0) -> float:
        self._refill()
        if self.tokens >= cost:
            return 0.0
        return (cost - self.tokens) / max(self.rate, 1e-9)


@dataclass(frozen=True)
class CheckResult:
    """Outcome of a rate-limit check, rich enough to populate X-RateLimit-* headers."""

    allowed: bool
    retry_after_s: float  # 0.0 if allowed
    limit: float  # bucket capacity
    remaining: float  # tokens left after acquire (or before refusal)
    reset_s: float  # seconds until bucket is full again


def replica_count() -> int:
    """Replicas sharing the deployment-wide cap: the hosts with a live serving
    instance row (`server.core.instances`), never fewer than LOOM_REPLICA_COUNT, and
    that setting alone while the rows are unread or stale."""
    from server.core.instances import live_hosts
    from server.settings import settings as _s

    floor = max(1, int(_s.replica_count))
    live = live_hosts()
    return floor if live is None else max(floor, live)


def effective_replicas() -> int:
    """Processes sharing the deployment-wide cap: replicas × uvicorn workers
    per replica. Each process with its own buckets caps at
    configured / this."""
    from server.settings import settings as _s

    return replica_count() * max(1, int(_s.web_workers))


def effective_divisor(*, shared: bool) -> int:
    """What one bucket's cap is divided by: the replica count when the
    replica's workers share a table, every process otherwise."""
    return replica_count() if shared else effective_replicas()


def _wants_table(mode: str, workers: int) -> bool:
    """Whether LOOM_RATELIMIT_SHARED puts this server's buckets in a shared table."""
    return mode == "on" or (mode == "auto" and workers > 1)


class Limiter:
    """Two-tier in-process registry: per-namespace AND per-org buckets.

    A request must pass BOTH buckets:
      - the namespace bucket bounds one tenant's namespace
      - the org bucket bounds the tenant's collective fan-out across all
        their namespaces

    The more restrictive of the two determines `allowed` and `retry_after_s`.
    Headers reflect the bottleneck bucket so the client sees what's
    actually limiting them.

    Lookup precedence for either tier:
      1. Override (per-namespace or per-org).
      2. Class default in DEFAULT_LIMITS / DEFAULT_ORG_LIMITS.
      3. `*_LIMITS["default"]`.

    Overrides survive `reset()`; admin policy outlives test hygiene.
    """

    def __init__(self, *, clock: Callable[[], float] = time.monotonic) -> None:
        # Forwarded to every TokenBucket so a test can drive check() off one clock.
        self._clock = clock
        # Bucket keys are (org, namespace, op_class) for the namespace tier,
        # (org, namespace, actor, op_class) for the actor tier and
        # (org, op_class) for the org tier — plus the traffic class as a
        # trailing element for the non-production classes, so a bench sweep
        # never touches production's bucket.
        self._ns_buckets: dict[tuple[str, ...], TokenBucket] = {}
        self._org_buckets: dict[tuple[str, ...], TokenBucket] = {}
        self._ns_overrides: dict[tuple[str, str], Limit] = {}
        self._org_overrides: dict[tuple[str, str], Limit] = {}
        self._actor_buckets: dict[tuple[str, ...], TokenBucket] = {}
        self._actor_overrides: dict[tuple[str, str, str], Limit] = {}
        # Unusable override rows already warned about, said once per row.
        self._unusable_warned: set[tuple[str, tuple[str, ...], str]] = set()
        # Host-shared table. Resolved lazily from settings on
        # first use and re-resolved when the governing settings change, so a
        # test that flips them gets the backend it asked for.
        self._shared: SharedBucketStore | None = None
        self._shared_key: tuple[object, ...] | None = None
        self._shared_reason: str = "not resolved yet"
        # The divisor this process's buckets were sized with; a change in the
        # live replica count resizes them (`_follow_divisor`).
        self._divisor = 0

    # -- backend selection ---------------------------------

    def _store(self) -> SharedBucketStore | None:
        """The host-shared table, chosen once per setting: a table that cannot be
        opened leaves this process on its own buckets until a restart (the
        overrides, unlike the buckets, look for their directory again)."""
        mode = str(settings.ratelimit_shared)
        path = str(settings.ratelimit_shared_path)
        slots = int(settings.ratelimit_shared_slots)
        workers = max(1, int(settings.web_workers))
        key = (mode, path, slots, workers)
        if key == self._shared_key:
            return self._shared
        if self._shared is not None:
            try:
                self._shared.close()
            except Exception:  # noqa: BLE001 — closing the old table never blocks reopening
                pass
            self._shared = None
        self._shared_key = key
        if not _wants_table(mode, workers):
            self._shared_reason = (
                "off by LOOM_RATELIMIT_SHARED" if mode == "off" else "single web worker"
            )
            return None
        from server.core.ratelimit_shared import SharedBucketStore as _Store
        from server.core.ratelimit_shared import default_path

        try:
            target = path or default_path()
            self._shared = _Store(target, slots, clock=self._clock)
        except Exception as e:  # noqa: BLE001 — fall back, say so in health
            # `/v1/health` shows the reason without a token: the error's kind.
            log.warning(
                "rate limiter: the shared table is unavailable: %s: %s", type(e).__name__, e
            )
            self._shared_reason = f"shared table unavailable ({error_kind(e)}); per-process buckets"
            return None
        self._shared_reason = "shared table"  # `/v1/health` shows it: no host path
        return self._shared

    # -- overrides kept in loom.runtime_overrides --------------------------

    def adopt(self, snapshot: dict[str, dict[tuple[str, ...], Any]]) -> None:
        """Take the deployment's namespace and org overrides (a
        `runtime_overrides.load()` snapshot) and resize the live buckets whose
        limit changed."""
        from server.core import runtime_overrides as ro

        def limits(kind: str) -> dict[tuple[str, str], Limit]:
            out: dict[tuple[str, str], Limit] = {}
            for k, v in snapshot.get(kind, {}).items():
                try:
                    lim = Limit(float(v["capacity"]), float(v["rate"]))
                    if len(k) == 2 and lim.capacity > 0 and lim.rate >= 0:
                        out[(k[0], k[1])] = lim
                        continue
                except (KeyError, TypeError, ValueError):
                    pass
                row = (kind, k, repr(v))
                if row not in self._unusable_warned:
                    self._unusable_warned.add(row)
                    log.warning("rate limiter: override %s %s unusable (%r); skipped", kind, k, v)
            return out

        ns, org = limits(ro.RATELIMIT_NAMESPACE), limits(ro.RATELIMIT_ORG)
        for key, lim in ns.items():
            if self._ns_overrides.get(key) != lim:
                self._apply_override(self._ns_buckets, key, lim, skip=1)
        for key, lim in org.items():
            if self._org_overrides.get(key) != lim:
                self._apply_override(self._org_buckets, key, lim)
        for key in self._ns_overrides.keys() - ns.keys():
            self._restore_default(self._ns_buckets, key, DEFAULT_LIMITS, skip=1)
        for key in self._org_overrides.keys() - org.keys():
            self._restore_default(self._org_buckets, key, DEFAULT_ORG_LIMITS)
        self._ns_overrides, self._org_overrides = ns, org

    @property
    def shared_active(self) -> bool:
        """True when this process's buckets live in the host-shared table."""
        return self._store() is not None

    def backend_status(self) -> dict[str, object]:
        """For /v1/health: which backend holds the buckets and what one cap is divided by."""
        store = self._store()
        out: dict[str, object] = {
            "backend": "shared" if store is not None else "per-process",
            "reason": self._shared_reason,
            "divisor": effective_divisor(shared=store is not None),
            "replica_count": replica_count(),
            "replica_count_floor": max(1, int(settings.replica_count)),
            "web_workers": max(1, int(settings.web_workers)),
            # The non-production classes' share of the base cap.
            "class_fractions": dict(class_fractions()),
        }
        if store is not None:
            # `/v1/health` answers without a token: the table's host path stays out.
            out.update({k: v for k, v in store.status().items() if k != "path"})
        return out

    def _ns_limit_for(self, namespace: str, op_class: str, traffic_class: str = "") -> Limit:
        ov = self._ns_overrides.get((namespace, op_class))
        base = (
            ov
            if ov is not None
            else DEFAULT_LIMITS.get(
                op_class,
                DEFAULT_LIMITS["default"],
            )
        )
        return _scale_for_class(_scale_for_replicas(base, shared=self.shared_active), traffic_class)

    def _org_limit_for(self, org: str, op_class: str, traffic_class: str = "") -> Limit:
        ov = self._org_overrides.get((org, op_class))
        base = (
            ov
            if ov is not None
            else DEFAULT_ORG_LIMITS.get(
                op_class,
                DEFAULT_ORG_LIMITS["default"],
            )
        )
        return _scale_for_class(_scale_for_replicas(base, shared=self.shared_active), traffic_class)

    def _actor_limit_for(
        self, namespace: str, actor: str, op_class: str, traffic_class: str = ""
    ) -> Limit | None:
        """The actor's limit inside `namespace`: an explicit override, else the
        namespace's own limit scaled by the actor fraction; None when the
        fraction is 1.0 and nothing is overridden (the tier stays out of the
        way — no bucket, no header change)."""
        ov = self._actor_overrides.get((namespace, actor, op_class))
        if ov is not None:
            return _scale_for_class(
                _scale_for_replicas(ov, shared=self.shared_active), traffic_class
            )
        frac = actor_fraction()
        if frac >= 1.0:
            return None
        # already replica- and class-scaled
        ns = self._ns_limit_for(namespace, op_class, traffic_class)
        return Limit(capacity=max(1.0, ns.capacity * frac), rate=ns.rate * frac)

    @staticmethod
    def _key(*parts: str, traffic_class: str) -> tuple[str, ...]:
        cls = bucket_class(traffic_class)
        return (*parts, cls) if cls else parts

    def _actor_bucket(
        self, org: str, namespace: str, actor: str, op_class: str, traffic_class: str = ""
    ) -> TokenBucket | None:
        key = self._key(org, namespace, actor, op_class, traffic_class=traffic_class)
        bucket = self._actor_buckets.get(key)
        if bucket is None:
            limit = self._actor_limit_for(namespace, actor, op_class, traffic_class)
            if limit is None:
                return None
            if len(self._actor_buckets) >= MAX_BUCKETS:
                self._actor_buckets.pop(next(iter(self._actor_buckets)))
            bucket = TokenBucket(
                limit,
                clock=self._clock,
                limit_of=functools.partial(
                    self._actor_limit_for, namespace, actor, op_class, traffic_class
                ),
            )
            self._actor_buckets[key] = bucket
        return bucket

    def _ns_bucket(
        self, org: str, namespace: str, op_class: str, traffic_class: str = ""
    ) -> TokenBucket:
        # Keyed on (org, namespace): two orgs' same-named namespaces are two
        # tenants and never share a budget. Overrides stay keyed on the name.
        key = self._key(org, namespace, op_class, traffic_class=traffic_class)
        bucket = self._ns_buckets.get(key)
        if bucket is None:
            if len(self._ns_buckets) >= MAX_BUCKETS:
                self._ns_buckets.pop(next(iter(self._ns_buckets)))
            limit_of = functools.partial(self._ns_limit_for, namespace, op_class, traffic_class)
            bucket = TokenBucket(limit_of(), clock=self._clock, limit_of=limit_of)
            self._ns_buckets[key] = bucket
        return bucket

    def _org_bucket(self, org: str, op_class: str, traffic_class: str = "") -> TokenBucket:
        key = self._key(org, op_class, traffic_class=traffic_class)
        bucket = self._org_buckets.get(key)
        if bucket is None:
            if len(self._org_buckets) >= MAX_BUCKETS:
                self._org_buckets.pop(next(iter(self._org_buckets)))
            limit_of = functools.partial(self._org_limit_for, org, op_class, traffic_class)
            bucket = TokenBucket(limit_of(), clock=self._clock, limit_of=limit_of)
            self._org_buckets[key] = bucket
        return bucket

    def check(
        self,
        namespace: str,
        op_name: str,
        *,
        org: str | None = None,
        actor: str | None = None,
        traffic_class: str = "",
    ) -> CheckResult:
        """Try to acquire one token from the namespace bucket AND (if provided)
        the org and actor buckets. If any bucket is empty, none is decremented —
        prevents leaking a token from a looser bucket while being throttled.
        Headers in the result reflect the binding bucket. When all are
        consumed (success), they reflect whichever has the smallest remaining
        fraction (the next request will likely hit that one first).

        `actor` is the verified token subject: a third tier,
        per (namespace, actor), so one credential cannot consume a namespace's
        whole budget. Sized by LOOM_RATELIMIT_ACTOR_FRACTION of the namespace
        limit; at the default 1.0 no actor bucket exists and behaviour is
        identical to the two-tier limiter. Anonymous requests (auth off) carry
        no actor and pay only the first two tiers.

        `traffic_class` selects the bucket of every tier:
        `bench` draws from its own, sized by LOOM_RATELIMIT_CLASS_FRACTIONS;
        production, interactive and an unclassified check draw from the base
        bucket the configured caps bound. A refusal in one class's bucket
        spends nothing in another's.
        """
        op_class = op_class_for(op_name)
        cls = bucket_class(traffic_class)
        store = self._store()
        if store is not None:
            return self._check_shared(
                store, namespace, op_class, org=org, actor=actor, traffic_class=cls
            )
        self._follow_divisor()
        buckets: list[TokenBucket] = [self._ns_bucket(org or "", namespace, op_class, cls)]
        if org:
            buckets.append(self._org_bucket(org, op_class, cls))
        if actor:
            ab = self._actor_bucket(org or "", namespace, actor, op_class, cls)
            if ab is not None:
                buckets.append(ab)

        def _result(b: TokenBucket, *, allowed: bool, wait: float) -> CheckResult:
            return CheckResult(
                allowed=allowed,
                retry_after_s=wait,
                limit=b.capacity,
                remaining=b.tokens,
                reset_s=(b.capacity - b.tokens) / max(b.rate, 1e-9),
            )

        waits = [b.seconds_until_available() for b in buckets]
        if any(w > 0 for w in waits):
            # The bottleneck is the longest wait; earlier tiers win ties so a
            # namespace-wide throttle reads as such in the headers.
            i = max(range(len(buckets)), key=lambda j: (waits[j], -j))
            return _result(buckets[i], allowed=False, wait=waits[i])
        for b in buckets:
            b.try_acquire()
        i = min(
            range(len(buckets)),
            key=lambda j: (buckets[j].tokens / max(buckets[j].capacity, 1), j),
        )
        return _result(buckets[i], allowed=True, wait=0.0)

    def _follow_divisor(self) -> None:
        """Resize the live per-process buckets when the replica count moved
        (a replica joined or left) to the limit their key has now: each keeps its
        fill, capped at the new size.
        The shared table is sized on every check and needs none of this."""
        n = effective_divisor(shared=False)
        if n == self._divisor:
            return
        old, self._divisor = self._divisor, n
        if not old:
            return
        for buckets in (self._ns_buckets, self._org_buckets, self._actor_buckets):
            for b in buckets.values():
                limit = b.limit_of() if b.limit_of is not None else None
                if limit is not None:
                    b.capacity, b.rate = limit.capacity, limit.rate
                    b.tokens = min(b.tokens, b.capacity)

    def _check_shared(
        self,
        store: SharedBucketStore,
        namespace: str,
        op_class: str,
        *,
        org: str | None,
        actor: str | None,
        traffic_class: str = "",
    ) -> CheckResult:
        """The same three tiers, acquired together in the host-shared table.
        Keys carry the tier name so a namespace and an org with the same
        spelling never share a bucket, the org so two orgs' same-named
        namespaces never share one, and the traffic class as a trailing
        component for the carved-out class (`bench`)."""
        cls = bucket_class(traffic_class)
        suffix = f"\0{cls}" if cls else ""
        o = org or ""
        entries: list[tuple[str, Limit]] = [
            (
                f"ns\0{o}\0{namespace}\0{op_class}{suffix}",
                self._ns_limit_for(namespace, op_class, cls),
            )
        ]
        if org:
            entries.append(
                (f"org\0{org}\0{op_class}{suffix}", self._org_limit_for(org, op_class, cls))
            )
        if actor:
            al = self._actor_limit_for(namespace, actor, op_class, cls)
            if al is not None:
                entries.append((f"actor\0{o}\0{namespace}\0{actor}\0{op_class}{suffix}", al))
        res = store.check([(k, lim.capacity, lim.rate) for k, lim in entries])
        lim = entries[res.binding][1]
        remaining = res.remaining[res.binding]
        return CheckResult(
            allowed=res.allowed,
            retry_after_s=res.retry_after_s,
            limit=lim.capacity,
            remaining=remaining,
            reset_s=(lim.capacity - remaining) / max(lim.rate, 1e-9),
        )

    def set_namespace_limit(self, namespace: str, op_class: str, limit: Limit) -> None:
        """Install a per-namespace override for one op_class."""
        self._ns_overrides[(namespace, op_class)] = limit
        # The namespace's buckets in every org (key element 0 is the org).
        self._apply_override(self._ns_buckets, (namespace, op_class), limit, skip=1)

    def clear_namespace_limit(self, namespace: str, op_class: str) -> None:
        self._ns_overrides.pop((namespace, op_class), None)
        self._restore_default(self._ns_buckets, (namespace, op_class), DEFAULT_LIMITS, skip=1)

    def set_org_limit(self, org: str, op_class: str, limit: Limit) -> None:
        """Install a per-org override for one op_class. Bounds the total
        fan-out across all of the org's namespaces for this op class."""
        self._org_overrides[(org, op_class)] = limit
        self._apply_override(self._org_buckets, (org, op_class), limit)

    def clear_org_limit(self, org: str, op_class: str) -> None:
        self._org_overrides.pop((org, op_class), None)
        self._restore_default(self._org_buckets, (org, op_class), DEFAULT_ORG_LIMITS)

    def set_actor_limit(self, namespace: str, actor: str, op_class: str, limit: Limit) -> None:
        """Install a per-(namespace, actor) override for one op_class. Creates
        the actor bucket even when the deployment fraction is 1.0."""
        self._actor_overrides[(namespace, actor, op_class)] = limit
        self._apply_override(self._actor_buckets, (namespace, actor, op_class), limit, skip=1)

    def _apply_override(
        self,
        buckets: dict[tuple[str, ...], TokenBucket],
        base: tuple[str, ...],
        limit: Limit,
        *,
        skip: int = 0,
    ) -> None:
        """Resize the live bucket(s) an override reaches: the base key and every
        class-suffixed key on it, sized as a fresh bucket would be: the
        replica share of the override, times a carved-out class's fraction.
        `skip` leading key elements (the org) are not part of what the
        override names."""
        self._follow_divisor()
        for full_key, existing in buckets.items():
            key = full_key[skip:]
            if key[: len(base)] != base or len(key) > len(base) + 1:
                continue
            cls = key[len(base)] if len(key) > len(base) else ""
            # As a fresh bucket would be: the replica share, then the class's.
            scaled = _scale_for_class(_scale_for_replicas(limit, shared=self.shared_active), cls)
            existing.capacity = scaled.capacity
            existing.rate = scaled.rate
            existing.tokens = min(existing.tokens, scaled.capacity)

    def _restore_default(
        self,
        buckets: dict[tuple[str, ...], TokenBucket],
        base: tuple[str, ...],
        defaults: dict[str, Limit],
        *,
        skip: int = 0,
    ) -> None:
        """Resize the live bucket(s) a cleared override reached to the default
        limit for its op class (`base[-1]`), keeping the tokens left: clearing
        an override does not refill a throttled tenant's bucket."""
        limit = defaults.get(base[-1], defaults["default"])
        self._apply_override(buckets, base, limit, skip=skip)

    def reset(self) -> None:
        """Test hook. Clears bucket state but keeps overrides — admin policy
        is not test data."""
        self._ns_buckets.clear()
        self._org_buckets.clear()
        self._actor_buckets.clear()
        if self._shared is not None:
            self._shared.reset()

    def reset_all(self) -> None:
        """Test hook. Clears both buckets and overrides."""
        self._ns_buckets.clear()
        self._org_buckets.clear()
        self._actor_buckets.clear()
        self._ns_overrides.clear()
        self._org_overrides.clear()
        self._actor_overrides.clear()
        if self._shared is not None:
            self._shared.reset()


_limiter = Limiter()


def get_limiter() -> Limiter:
    return _limiter


def admit_route(
    op_name: str,
    namespace: str,
    *,
    org: str,
    actor: str | None,
    label: str = "",
    traffic_class: str = "",
) -> None:
    """The rate-limit buckets of `op_name`, drawn for a route that serves it outside
    the op dispatcher, as the transports draw them for the op in `traffic_class` (''
    for a route that does not classify its calls): RATE_LIMITED when one is empty; a
    limiter that fails admits the request, as it admits an op, and its log names `label`
    (the route) or the op."""
    from server.core import fail_open
    from server.core.errors import RateLimitedError

    try:
        rl = get_limiter().check(
            namespace, op_name, org=org, actor=actor or None, traffic_class=traffic_class
        )
    except Exception as e:  # noqa: BLE001 — the limiter fails open: the request is admitted
        fail_open.note("rate_limit", label or op_name, e)
        return
    if not rl.allowed:
        wait = max(1, int(rl.retry_after_s + 0.99))
        raise RateLimitedError("rate limit exceeded; retry later", retry_after_s=wait)
