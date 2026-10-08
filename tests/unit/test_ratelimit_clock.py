"""Deterministic token-bucket math via an injected fake clock.

The integration suite (tests/integration/test_ratelimit*.py) exercises the
limiter over an HTTP process boundary, where token-refill timing races real
wall-clock under cluster load — hence their `flaky(reruns=2)` markers. The
*math* the buckets implement (burst, drain, refill, refusal, retry-after,
two-tier bottleneck selection) is fully deterministic, though, and that's what
these in-process unit tests pin down with a clock we control tick-by-tick.

`TokenBucket(limit, clock=...)` and `Limiter(clock=...)` take a
monotonic-seconds callable defaulting to `time.monotonic`; production passes
nothing and behaves exactly as before. Here we hand them a `FakeClock` so no
real time elapses and there is zero jitter.
"""

from __future__ import annotations

import pytest

from server.core.ratelimit import Limit, Limiter, TokenBucket, op_class_for


class FakeClock:
    """Controllable monotonic-seconds source. `advance(dt)` moves it forward;
    it never goes backward (mirrors time.monotonic's contract)."""

    def __init__(self, start: float = 0.0) -> None:
        self._t = float(start)

    def __call__(self) -> float:
        return self._t

    def advance(self, dt: float) -> None:
        assert dt >= 0, "monotonic clock cannot move backward"
        self._t += dt


# --- TokenBucket: burst, refusal, refill --------------------------------


def test_burst_up_to_capacity_then_refusal() -> None:
    clk = FakeClock()
    b = TokenBucket(Limit(capacity=3, rate=1), clock=clk)
    # Starts full: exactly `capacity` immediate acquires succeed.
    assert b.try_acquire()
    assert b.try_acquire()
    assert b.try_acquire()
    # No time has passed (fake clock frozen) → the 4th is refused.
    assert not b.try_acquire()
    assert b.tokens == 0


def test_no_refill_without_elapsed_time() -> None:
    """The flaky integration tests assume bursts don't refill mid-flood.
    With a frozen clock that's guaranteed: zero elapsed → zero refill."""
    clk = FakeClock()
    b = TokenBucket(Limit(capacity=2, rate=100), clock=clk)
    assert b.try_acquire()
    assert b.try_acquire()
    # Even with a fast rate, no wall time elapsed → still empty.
    assert not b.try_acquire()


def test_refill_is_linear_in_elapsed_time() -> None:
    clk = FakeClock()
    b = TokenBucket(Limit(capacity=10, rate=2), clock=clk)
    for _ in range(10):
        assert b.try_acquire()
    assert not b.try_acquire()
    # rate=2/s → 0.5s yields exactly 1 token back.
    clk.advance(0.5)
    assert b.try_acquire()
    assert not b.try_acquire()
    # 2.0s → 4 tokens.
    clk.advance(2.0)
    assert b.try_acquire()
    assert b.try_acquire()
    assert b.try_acquire()
    assert b.try_acquire()
    assert not b.try_acquire()


def test_refill_caps_at_capacity() -> None:
    """A long idle period cannot over-fill past capacity (no token hoarding)."""
    clk = FakeClock()
    b = TokenBucket(Limit(capacity=5, rate=10), clock=clk)
    for _ in range(5):
        assert b.try_acquire()
    clk.advance(3600)  # an hour idle
    # Only `capacity` tokens, not 3600*10.
    assert b.try_acquire()
    assert b.try_acquire()
    assert b.try_acquire()
    assert b.try_acquire()
    assert b.try_acquire()
    assert not b.try_acquire()


def test_seconds_until_available_is_exact() -> None:
    clk = FakeClock()
    b = TokenBucket(Limit(capacity=4, rate=2), clock=clk)
    for _ in range(4):
        assert b.try_acquire()
    # Empty: need 1 token at 2/s → 0.5s.
    assert b.seconds_until_available() == 0.5
    # Cost of 3 tokens → 1.5s.
    assert b.seconds_until_available(cost=3) == 1.5
    # After exactly the wait, it's acquirable and reports 0.
    clk.advance(0.5)
    assert b.seconds_until_available() == 0.0
    assert b.try_acquire()


def test_retry_after_then_recovery_is_deterministic() -> None:
    """Mirrors the integration "wait Retry-After, regain access" assertion,
    but with no real sleep: advance the fake clock by the reported wait."""
    clk = FakeClock()
    b = TokenBucket(Limit(capacity=1, rate=4), clock=clk)
    assert b.try_acquire()
    wait = b.seconds_until_available()
    assert wait == 0.25  # 1 token / 4 per s
    clk.advance(wait)
    assert b.try_acquire()


def test_fractional_cost_acquire() -> None:
    clk = FakeClock()
    b = TokenBucket(Limit(capacity=1, rate=1), clock=clk)
    assert b.try_acquire(cost=0.5)
    assert b.try_acquire(cost=0.5)
    assert not b.try_acquire(cost=0.5)
    clk.advance(0.5)  # +0.5 token
    assert b.try_acquire(cost=0.5)


# --- Limiter: two-tier (namespace + org) bottleneck selection -----------


def test_limiter_namespace_only_burst_and_refusal() -> None:
    clk = FakeClock()
    lim = Limiter(clock=clk)
    # knowledge.add default ns capacity is 10.
    assert op_class_for("knowledge.add") == "knowledge.add"
    allowed = 0
    for _ in range(15):
        if lim.check("ns1", "knowledge.add").allowed:
            allowed += 1
    assert allowed == 10  # exactly capacity, deterministically
    res = lim.check("ns1", "knowledge.add")
    assert res.allowed is False
    assert res.retry_after_s > 0
    assert res.limit == 10
    assert res.remaining < 1


def test_limiter_distinct_namespaces_independent() -> None:
    clk = FakeClock()
    lim = Limiter(clock=clk)
    for _ in range(10):
        assert lim.check("nsA", "knowledge.add").allowed
    assert lim.check("nsA", "knowledge.add").allowed is False
    # nsB untouched → full budget.
    assert lim.check("nsB", "knowledge.add").allowed


def test_limiter_org_bucket_binds_before_namespace() -> None:
    """Tight org override (5) + default ns (10): the org bucket is the
    bottleneck, so exactly 5 succeed and the 429 reports the org cap."""
    clk = FakeClock()
    lim = Limiter(clock=clk)
    org = "noisy"
    lim.set_org_limit(org, "knowledge.add", Limit(capacity=5, rate=1))
    allowed = 0
    for i in range(10):
        ns = "a" if i % 2 == 0 else "b"
        if lim.check(ns, "knowledge.add", org=org).allowed:
            allowed += 1
    assert allowed == 5  # org bucket cap, not 2x ns cap
    res = lim.check("a", "knowledge.add", org=org)
    assert res.allowed is False
    # Header-bearing fields reflect the binding (org) bucket.
    assert res.limit == 5


def test_limiter_namespace_binds_under_loose_org() -> None:
    clk = FakeClock()
    lim = Limiter(clock=clk)
    org = "big"
    ns = "tight"
    lim.set_org_limit(org, "knowledge.add", Limit(capacity=1000, rate=100))
    lim.set_namespace_limit(ns, "knowledge.add", Limit(capacity=3, rate=0.5))
    allowed = sum(lim.check(ns, "knowledge.add", org=org).allowed for _ in range(8))
    assert allowed == 3  # ns cap binds
    assert lim.check(ns, "knowledge.add", org=org).allowed is False


def test_limiter_no_token_leak_when_one_bucket_full() -> None:
    """Refusal must not decrement the non-binding bucket: drain the org
    bucket, then confirm the ns bucket still has its full budget for a
    different op path that shares only the ns tier."""
    clk = FakeClock()
    lim = Limiter(clock=clk)
    org = "leaky"
    lim.set_org_limit(org, "knowledge.add", Limit(capacity=2, rate=0.0001))
    # Two succeed (org cap), the rest refused.
    assert lim.check("nsX", "knowledge.add", org=org).allowed
    assert lim.check("nsX", "knowledge.add", org=org).allowed
    for _ in range(5):
        assert lim.check("nsX", "knowledge.add", org=org).allowed is False
    # The ns bucket only paid for the 2 successes; default cap is 10, so
    # 8 tokens should remain (the namespace bucket is keyed per org, so it is
    # read directly — another org's check would draw from its own bucket).
    assert lim._ns_buckets[(org, "nsX", "knowledge.add")].tokens == pytest.approx(8, abs=0.01)


def test_limiter_two_tier_recovers_after_clock_advance() -> None:
    clk = FakeClock()
    lim = Limiter(clock=clk)
    org = "rec"
    lim.set_org_limit(org, "knowledge.add", Limit(capacity=2, rate=2))
    assert lim.check("ns", "knowledge.add", org=org).allowed
    assert lim.check("ns", "knowledge.add", org=org).allowed
    refused = lim.check("ns", "knowledge.add", org=org)
    assert refused.allowed is False
    # Advance past the reported retry → access restored, deterministically.
    clk.advance(refused.retry_after_s)
    assert lim.check("ns", "knowledge.add", org=org).allowed


def test_overrides_survive_reset_but_clock_state_resets() -> None:
    clk = FakeClock()
    lim = Limiter(clock=clk)
    ns = "policy"
    lim.set_namespace_limit(ns, "memory", Limit(capacity=999, rate=999))
    # Drain a bit.
    assert lim.check(ns, "memory.set").allowed
    lim.reset()  # clears buckets, keeps overrides
    res = lim.check(ns, "memory.set")
    assert res.allowed
    assert res.limit == 999  # override still in force
    lim.reset_all()  # clears overrides too
    res = lim.check(ns, "memory.set")
    assert res.allowed
    assert res.limit == 100  # back to the default memory capacity


def test_default_clock_is_monotonic_in_production_path() -> None:
    """Belt-and-braces: constructing without a clock must use a real,
    advancing source so production behavior is unchanged."""
    import time

    b = TokenBucket(Limit(capacity=1, rate=1))
    assert b.try_acquire()
    assert not b.try_acquire()
    before = b.seconds_until_available()
    time.sleep(0.01)  # not an async test; plain unit, real clock
    after = b.seconds_until_available()
    assert 0 < after < before
