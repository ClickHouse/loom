"""Exact-key memory reads draw from their own rate bucket.

An agent that uses loom as a key-value store issues `memory.get` in bursts
(hundreds a minute) beside a handful of writes. If every `memory.*` op shared
the `memory` bucket, the reads would spend the budget the writes need. Here:
the read ops resolve to `memory.read`, the
write / list / search ops stay in `memory`, the registry stays totally
covered, and a read burst leaves the write bucket full — pinned with the
injected clock of `test_ratelimit_clock.py` so no wall time is involved.
"""

from __future__ import annotations

import pytest

from server.core.ratelimit import (
    DEFAULT_LIMITS,
    DEFAULT_ORG_LIMITS,
    Limit,
    Limiter,
    op_class_for,
)
from server.operations.registry import ALL_OPERATIONS


class FakeClock:
    def __init__(self, start: float = 0.0) -> None:
        self._t = float(start)

    def __call__(self) -> float:
        return self._t

    def advance(self, dt: float) -> None:
        assert dt >= 0
        self._t += dt


READ_OPS = (
    "memory.get",
    "memory.bulk_get",
    "memory.history",
    "memory.lineage",
    "memory.provenance",
    "memory.related",
    "memory.backlinks",
    "working_memory.get",
    "working_memory.render",
)

STAY_IN_MEMORY = (
    "memory.set",
    "memory.search",
    "memory.list",
    "memory.bulk_set",
    "memory.update",
    "memory.delete",
    "conversation.append",
)


@pytest.mark.parametrize("op", READ_OPS)
def test_exact_key_reads_resolve_to_memory_read(op: str) -> None:
    assert op_class_for(op) == "memory.read"


@pytest.mark.parametrize("op", READ_OPS)
def test_every_routed_read_op_is_registered(op: str) -> None:
    """The frozenset names real ops; a rename in the registry fails here,
    not silently in production as a read that pays the write bucket."""
    assert op in {o.name for o in ALL_OPERATIONS}, f"{op} is not a registered operation"


@pytest.mark.parametrize("op", STAY_IN_MEMORY)
def test_writes_list_and_search_stay_in_memory(op: str) -> None:
    assert op_class_for(op) == "memory"


def test_registry_is_totally_covered() -> None:
    """Every registered op resolves to a class with a configured default, in
    both tiers — the prefix rule keeps coverage total; the read set must not
    break that."""
    for op in ALL_OPERATIONS:
        cls = op_class_for(op.name)
        assert cls in DEFAULT_LIMITS, f"{op.name} -> {cls!r} has no namespace default"
        assert cls in DEFAULT_ORG_LIMITS, f"{op.name} -> {cls!r} has no org default"


def test_default_limits_for_the_read_class() -> None:
    assert DEFAULT_LIMITS["memory.read"] == Limit(capacity=300, rate=50)
    # Org tier derives as 3x, like every other class.
    assert DEFAULT_ORG_LIMITS["memory.read"] == Limit(capacity=900, rate=150)
    # The write bucket is untouched.
    assert DEFAULT_LIMITS["memory"] == Limit(capacity=100, rate=17)


def test_read_burst_leaves_the_write_bucket_full() -> None:
    """150 gets in one instant are all admitted (burst 300) and memory.set
    still has its whole 100 burst afterwards — a get storm of hundreds a
    minute refuses none of the namespace's writes."""
    clk = FakeClock()
    lim = Limiter(clock=clk)
    for i in range(150):
        res = lim.check("ops-alerts", "memory.get")
        assert res.allowed, f"get #{i + 1} refused"
        assert res.limit == 300  # headers name the read bucket's cap
    assert res.remaining == 150
    admitted = 0
    for _ in range(101):
        if lim.check("ops-alerts", "memory.set").allowed:
            admitted += 1
    assert admitted == 100  # exactly the write burst, untouched by the reads
    refused = lim.check("ops-alerts", "memory.set")
    assert refused.allowed is False
    assert refused.limit == 100


def test_write_storm_leaves_the_read_bucket_full() -> None:
    """The converse: draining `memory` never refuses a get."""
    clk = FakeClock()
    lim = Limiter(clock=clk)
    for _ in range(100):
        assert lim.check("ns", "memory.set").allowed
    assert lim.check("ns", "memory.set").allowed is False
    for _ in range(300):
        assert lim.check("ns", "memory.get").allowed
    assert lim.check("ns", "memory.get").allowed is False


def test_read_bucket_refills_at_fifty_per_second() -> None:
    clk = FakeClock()
    lim = Limiter(clock=clk)
    for _ in range(300):
        assert lim.check("ns", "memory.get").allowed
    res = lim.check("ns", "memory.get")
    assert res.allowed is False
    assert res.retry_after_s == pytest.approx(1 / 50)
    clk.advance(1.0)
    admitted = sum(1 for _ in range(60) if lim.check("ns", "memory.get").allowed)
    assert admitted == 50


def test_org_tier_is_three_times_the_namespace_cap() -> None:
    """Three namespaces of one org each burst 300 gets; the org bucket
    (900) binds on the 901st and the headers name it."""
    clk = FakeClock()
    lim = Limiter(clock=clk)
    for ns in ("a", "b", "c"):
        for _ in range(300):
            assert lim.check(ns, "memory.get", org="acme").allowed
    res = lim.check("d", "memory.get", org="acme")
    assert res.allowed is False
    assert res.limit == 900


def test_admin_override_reaches_the_read_class() -> None:
    """`admin/ratelimit.set` takes any op_class string; an override on
    `memory.read` resizes that bucket and nothing else."""
    clk = FakeClock()
    lim = Limiter(clock=clk)
    lim.set_namespace_limit("ns", "memory.read", Limit(capacity=5, rate=1))
    admitted = sum(1 for _ in range(10) if lim.check("ns", "memory.get").allowed)
    assert admitted == 5
    assert lim.check("ns", "memory.get").limit == 5
    # memory.set still sees the class default.
    assert lim.check("ns", "memory.set").limit == 100


def test_knowledge_query_draws_from_its_own_read_bucket() -> None:
    """By the prefix rule it would draw on knowledge.add's write-shaped bucket,
    so a few queries would refuse the namespace's next write."""
    assert op_class_for("knowledge.query") == "knowledge.query"
    assert DEFAULT_LIMITS["knowledge.query"] == DEFAULT_LIMITS["knowledge.search"]


def test_knowledge_reads_draw_from_their_own_bucket() -> None:
    """Every read-only knowledge op but search and query (their own buckets) draws on
    knowledge.read, and every other knowledge op stays in knowledge.add: a new read the set
    does not name fails here, not in production as a read that spends the write bucket."""
    for op in ALL_OPERATIONS:
        if not op.name.startswith("knowledge.") or op.name in (
            "knowledge.search",
            "knowledge.query",
        ):
            continue
        want = "knowledge.read" if op.read_only else "knowledge.add"
        assert op_class_for(op.name) == want, op.name
    assert DEFAULT_LIMITS["knowledge.read"] == DEFAULT_LIMITS["default"]


def test_a_knowledge_read_burst_leaves_the_add_bucket_full() -> None:
    """Twenty list and status reads in one instant are all admitted, and knowledge.add
    still has its whole burst of 10 afterwards."""
    clk = FakeClock()
    lim = Limiter(clock=clk)
    reads = ("knowledge.list", "knowledge.ingest_status", "knowledge.source.list")
    assert all(lim.check("ns", reads[i % 3]).allowed for i in range(20))
    assert sum(1 for _ in range(12) if lim.check("ns", "knowledge.add").allowed) == 10
