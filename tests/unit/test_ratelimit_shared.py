"""Host-shared rate buckets.

The store is a memory-mapped file every worker of one server opens; these
tests pin the invariants that make it a limiter: one cap across processes,
no cross-key leakage, refusal decrements nothing, the per-replica division
stays while the per-worker division goes, and the fallback to per-process
buckets is reported rather than silent.
"""

from __future__ import annotations

import multiprocessing as mp
import os
from pathlib import Path

import pytest

from server.core import ratelimit
from server.core.ratelimit import DEFAULT_LIMITS, Limit, Limiter
from server.core.ratelimit_shared import (
    HEADER_SIZE,
    MIN_SLOTS,
    PROBE_WINDOW,
    SLOT_STRIDE,
    SharedBucketStore,
    key_hash,
)


class FakeClock:
    def __init__(self) -> None:
        self._t = 0.0

    def __call__(self) -> float:
        return self._t

    def advance(self, dt: float) -> None:
        self._t += dt


@pytest.fixture
def shared_on(settings_override, tmp_path: Path):
    """A Limiter whose buckets live in a fresh shared table at `tmp_path`."""
    path = str(tmp_path / "rl.buckets")
    settings_override(ratelimit_shared="on", ratelimit_shared_path=path, ratelimit_shared_slots=256)
    return path


# --- store: single process --------------------------------------------------


def test_key_hash_is_deterministic_and_never_zero() -> None:
    assert key_hash("ns\0a\0memory") == key_hash("ns\0a\0memory")
    assert key_hash("ns\0a\0memory") != key_hash("ns\0b\0memory")
    assert key_hash("") != 0


def test_store_burst_then_refusal_then_refill(tmp_path: Path) -> None:
    clk = FakeClock()
    st = SharedBucketStore(str(tmp_path / "t"), MIN_SLOTS, clock=clk)
    key = "ns\0a\0memory"
    for i in range(3):
        r = st.check([(key, 3, 1)])
        assert r.allowed and r.remaining[0] == 2 - i
    r = st.check([(key, 3, 1)])
    assert not r.allowed and r.retry_after_s == pytest.approx(1.0)
    clk.advance(1.0)
    assert st.check([(key, 3, 1)]).allowed
    st.close()


def test_store_refusal_takes_nothing_from_the_other_tiers(tmp_path: Path) -> None:
    st = SharedBucketStore(str(tmp_path / "t"), MIN_SLOTS, clock=FakeClock())
    ns, org = "ns\0a\0memory", "org\0o\0memory"
    assert st.check([(ns, 1, 0), (org, 10, 0)]).allowed
    r = st.check([(ns, 1, 0), (org, 10, 0)])
    assert not r.allowed and r.binding == 0
    assert st.peek(org) == (9.0, 0.0)  # untouched by the refused check
    st.close()


def test_store_distinct_keys_do_not_share_a_slot(tmp_path: Path) -> None:
    st = SharedBucketStore(str(tmp_path / "t"), MIN_SLOTS, clock=FakeClock())
    keys = [f"ns\0ns-{i}\0memory" for i in range(40)]
    for k in keys:
        for _ in range(2):
            assert st.check([(k, 2, 0)]).allowed
    for k in keys:
        assert not st.check([(k, 2, 0)]).allowed
    st.close()


def test_store_evicts_the_stalest_when_the_probe_window_is_full(tmp_path: Path) -> None:
    clk = FakeClock()
    st = SharedBucketStore(str(tmp_path / "t"), MIN_SLOTS, clock=clk)
    # MIN_SLOTS == PROBE_WINDOW here, so the window is the whole table.
    assert PROBE_WINDOW == MIN_SLOTS
    keys = [f"k{i}" for i in range(MIN_SLOTS)]
    for k in keys:
        st.check([(k, 5, 0)])
        clk.advance(1.0)
    # Every slot taken; the next key evicts the least recently refilled (k0).
    assert st.peek("k0") is not None
    assert st.check([("fresh", 5, 0)]).allowed
    assert st.peek("fresh") is not None
    assert st.peek("k0") is None
    st.close()


def test_store_header_mismatch_refuses_to_share(tmp_path: Path) -> None:
    p = str(tmp_path / "t")
    a = SharedBucketStore(p, 128)
    with pytest.raises(ValueError, match="slots=128"):
        SharedBucketStore(p, 256)
    a.close()
    with pytest.raises(ValueError):
        SharedBucketStore(p, MIN_SLOTS - 1)


def test_store_second_opener_sees_the_first_ones_tokens(tmp_path: Path) -> None:
    p = str(tmp_path / "t")
    clk = FakeClock()
    a = SharedBucketStore(p, MIN_SLOTS, clock=clk)
    b = SharedBucketStore(p, MIN_SLOTS, clock=clk)
    key = "ns\0a\0memory"
    assert a.check([(key, 2, 0)]).allowed
    assert b.check([(key, 2, 0)]).allowed
    assert not a.check([(key, 2, 0)]).allowed
    assert not b.check([(key, 2, 0)]).allowed
    assert os.path.getsize(p) == HEADER_SIZE + MIN_SLOTS * SLOT_STRIDE
    a.close()
    b.close()


def test_store_reset_zeroes_every_bucket(tmp_path: Path) -> None:
    st = SharedBucketStore(str(tmp_path / "t"), MIN_SLOTS, clock=FakeClock())
    assert st.check([("k", 1, 0)]).allowed
    assert not st.check([("k", 1, 0)]).allowed
    st.reset()
    assert st.peek("k") is None
    assert st.check([("k", 1, 0)]).allowed
    st.close()


# --- store: several processes -----------------------------------------------


def _hammer(
    path: str, keys: list[str], attempts: int, q, start
) -> None:  # pragma: no cover — child
    st = SharedBucketStore(path, 256)
    start.wait(timeout=60)  # every process races from the same instant
    granted = dict.fromkeys(keys, 0)
    for _ in range(attempts):
        for k in keys:
            if st.check([(k, 200, 0.0)]).allowed:
                granted[k] += 1
    st.close()
    q.put(granted)


def test_four_processes_share_exactly_one_cap_per_key(tmp_path: Path) -> None:
    """Capacity 200, no refill, four processes each trying 200 times per key:
    exactly 200 succeed per key across all of them, and a second key gets its
    own 200 — the property per-process buckets cannot give."""
    path = str(tmp_path / "mp.buckets")
    SharedBucketStore(path, 256).close()  # header written before the race
    keys = ["ns\0alpha\0memory", "ns\0beta\0memory"]
    ctx = mp.get_context("spawn")
    q = ctx.Queue()
    start = ctx.Barrier(4)
    procs = [ctx.Process(target=_hammer, args=(path, keys, 200, q, start)) for _ in range(4)]
    for p in procs:
        p.start()
    results = [q.get(timeout=60) for _ in procs]
    for p in procs:
        p.join(timeout=60)
        assert p.exitcode == 0
    for k in keys:
        per_proc = [r[k] for r in results]
        # The property is the shared cap — exactly 200 across all processes.
        # How the 200 split between them is scheduling (a loaded CI runner
        # once handed one process the whole cap), not a correctness claim.
        assert sum(per_proc) == 200, per_proc


# --- Limiter integration ------------------------------------------------------


def test_limiter_defaults_to_per_process_with_one_worker(settings_override) -> None:
    settings_override(ratelimit_shared="auto", web_workers=1)
    lim = Limiter(clock=FakeClock())
    assert lim.shared_active is False
    assert lim.backend_status()["backend"] == "per-process"
    assert lim.backend_status()["reason"] == "single web worker"


def test_limiter_auto_shares_when_workers_exceed_one(settings_override, tmp_path: Path) -> None:
    settings_override(
        ratelimit_shared="auto",
        web_workers=4,
        ratelimit_shared_path=str(tmp_path / "rl"),
        ratelimit_shared_slots=256,
    )
    lim = Limiter(clock=FakeClock())
    assert lim.shared_active is True
    status = lim.backend_status()
    assert status["backend"] == "shared" and status["divisor"] == 1
    assert status["slots"] == 256 and status["reason"] == "shared table"
    # `/v1/health` answers without a token: no host path in it.
    assert str(tmp_path) not in str(status)


def test_shared_cap_is_divided_by_replicas_not_workers(settings_override, tmp_path: Path) -> None:
    settings_override(
        ratelimit_shared="on",
        web_workers=4,
        replica_count=2,
        ratelimit_shared_path=str(tmp_path / "rl"),
        ratelimit_shared_slots=256,
    )
    lim = Limiter(clock=FakeClock())
    r = lim.check("ns", "memory.search")
    # 100 / 2 replicas, not 100 / (2 × 4).
    assert r.allowed and r.limit == DEFAULT_LIMITS["memory"].capacity / 2
    assert ratelimit.effective_replicas() == 8  # the replica count's meaning is unchanged
    assert ratelimit.effective_divisor(shared=True) == 2
    assert ratelimit.effective_divisor(shared=False) == 8


def test_a_limit_is_sized_for_shared_buckets_before_any_check(
    settings_override, tmp_path: Path
) -> None:
    """Sizing a limit resolves the store itself, so no call order matters."""
    settings_override(
        ratelimit_shared="on",
        web_workers=4,
        replica_count=1,
        ratelimit_shared_path=str(tmp_path / "rl"),
        ratelimit_shared_slots=256,
    )
    lim = Limiter(clock=FakeClock())
    assert lim._ns_limit_for("ns", "memory") == DEFAULT_LIMITS["memory"]  # not divided by 4


def test_per_process_fallback_keeps_the_worker_division(settings_override) -> None:
    settings_override(ratelimit_shared="off", web_workers=4, replica_count=1)
    lim = Limiter(clock=FakeClock())
    r = lim.check("ns", "memory.search")
    assert r.allowed and r.limit == DEFAULT_LIMITS["memory"].capacity / 4
    assert lim.backend_status()["reason"] == "off by LOOM_RATELIMIT_SHARED"


def test_unavailable_path_falls_back_and_says_so(settings_override, tmp_path: Path) -> None:
    settings_override(
        ratelimit_shared="on",
        web_workers=4,
        ratelimit_shared_path=str(tmp_path / "no-such-dir" / "rl"),
    )
    lim = Limiter(clock=FakeClock())
    r = lim.check("ns", "memory.search")
    assert r.allowed and r.limit == DEFAULT_LIMITS["memory"].capacity / 4
    status = lim.backend_status()
    assert status["backend"] == "per-process"
    assert "unavailable" in str(status["reason"]) and "FileNotFoundError" in str(status["reason"])


def test_shared_three_tiers_headers_name_the_binding_bucket(shared_on) -> None:
    clk = FakeClock()
    lim = Limiter(clock=clk)
    lim.set_namespace_limit("ns", "memory", Limit(capacity=10, rate=1))
    lim.set_org_limit("o", "memory", Limit(capacity=2, rate=1))
    lim.set_actor_limit("ns", "sa:one", "memory", Limit(capacity=5, rate=1))
    r1 = lim.check("ns", "memory.search", org="o", actor="sa:one")
    r2 = lim.check("ns", "memory.search", org="o", actor="sa:one")
    assert r1.allowed and r2.allowed and r2.limit == 2 and r2.remaining == 0
    r3 = lim.check("ns", "memory.search", org="o", actor="sa:one")
    assert not r3.allowed and r3.limit == 2 and r3.retry_after_s == pytest.approx(1.0)
    # The refusal took nothing from the namespace or actor buckets.
    assert lim._shared is not None
    assert lim._shared.peek("ns\0o\0ns\0memory")[0] == 8
    assert lim._shared.peek("actor\0o\0ns\0sa:one\0memory")[0] == 3
    # Another org is not throttled by this one's empty bucket.
    assert lim.check("ns", "memory.search", org="p", actor="sa:one").allowed


def test_shared_namespace_and_org_with_the_same_spelling_are_separate(shared_on) -> None:
    lim = Limiter(clock=FakeClock())
    lim.set_namespace_limit("x", "memory", Limit(capacity=1, rate=0))
    assert lim.check("x", "memory.search", org="x").allowed
    r = lim.check("x", "memory.search", org="x")
    assert not r.allowed and r.limit == 1  # the namespace tier, not the org's 300


def test_shared_reset_clears_state_and_keeps_overrides(shared_on) -> None:
    lim = Limiter(clock=FakeClock())
    lim.set_namespace_limit("ns", "memory", Limit(capacity=1, rate=0))
    assert lim.check("ns", "memory.search").allowed
    assert not lim.check("ns", "memory.search").allowed
    lim.reset()
    r = lim.check("ns", "memory.search")
    assert r.allowed and r.limit == 1


def test_backend_re_resolves_when_settings_change(settings_override, tmp_path: Path) -> None:
    settings_override(ratelimit_shared="off", web_workers=4)
    lim = Limiter(clock=FakeClock())
    assert lim.check("ns", "memory.search").limit == 25
    settings_override(ratelimit_shared="on", ratelimit_shared_path=str(tmp_path / "rl"))
    assert lim.check("ns", "memory.search").limit == 100
    assert lim.backend_status()["backend"] == "shared"


def test_the_slot_memo_is_bounded_and_a_forgotten_key_keeps_its_bucket(tmp_path: Path) -> None:
    """Keys carry actors and namespaces; the per-process memo of their slots
    stays within the table size, and a key it forgot still finds its own
    bucket (the memo is a shortcut, never the bucket)."""
    clk = FakeClock()
    st = SharedBucketStore(str(tmp_path / "t"), MIN_SLOTS, clock=clk)
    first = "actor\0o\0ns\0subject-0\0memory"
    assert st.check([(first, 5, 0)]).allowed
    for i in range(1, 10 * MIN_SLOTS):
        st.check([(f"actor\0o\0ns\0subject-{i}\0memory", 5, 0)])
        assert len(st._memo) <= MIN_SLOTS
    st._memo.clear()
    again = SharedBucketStore(str(tmp_path / "t"), MIN_SLOTS, clock=clk)
    key = "ns\0a\0memory"
    for _ in range(2):
        again.check([(key, 2, 0)])
    again._memo.clear()
    assert not again.check([(key, 2, 0)]).allowed  # same bucket after the memo went
    st.close()
    again.close()
