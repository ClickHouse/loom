"""Bounded rate changes refuse before storage and recheck before local adoption."""

from __future__ import annotations

from unittest.mock import AsyncMock, Mock

import pytest

from server.core import ratelimit
from server.core.ch_bounded import BoundedReadError
from server.core.ratelimit import Limit, Limiter, TokenBucket
from server.insights.app import limit_changes
from server.settings import settings


@pytest.fixture
def limiter(monkeypatch: pytest.MonkeyPatch) -> Limiter:
    monkeypatch.setattr(settings, "ratelimit_shared", "off")
    monkeypatch.setattr(settings, "ratelimit_shared_path", "")
    monkeypatch.setattr(settings, "ratelimit_shared_slots", 128)
    monkeypatch.setattr(settings, "web_workers", 1)
    return Limiter(clock=lambda: 10.0)


@pytest.mark.parametrize("failure", ["unknown-class", "class-count"])
def test_unknown_rate_classes_refuse_without_opening_a_backend(
    limiter: Limiter, monkeypatch: pytest.MonkeyPatch, failure: str
) -> None:
    if failure == "class-count":
        monkeypatch.setattr(
            ratelimit, "DEFAULT_LIMITS", {f"class-{i}": Limit(1, 1) for i in range(65)}
        )
    backend = Mock(side_effect=AssertionError("preflight must not resolve a backend"))
    monkeypatch.setattr(limiter, "_store", backend)
    op_class = "class-0" if failure == "class-count" else "unknown"
    with pytest.raises(BoundedReadError, match="Unsupported rate-limit class"):
        limiter.check_bounded_namespace_change("work", op_class, clear=False)
    backend.assert_not_called()
    assert limiter._ns_overrides == {}


@pytest.mark.parametrize("tier", ["_ns_buckets", "_org_buckets", "_actor_buckets"])
def test_every_bucket_tier_refuses_overflow_without_evicting_existing_work(
    limiter: Limiter, monkeypatch: pytest.MonkeyPatch, tier: str
) -> None:
    monkeypatch.setattr(ratelimit, "MAX_BUCKETS", 2)
    bucket = TokenBucket(Limit(1, 0), clock=lambda: 10.0)
    assert bucket.try_acquire()
    entries = {(str(i),): bucket for i in range(3)}
    monkeypatch.setattr(limiter, tier, entries)
    with pytest.raises(BoundedReadError, match="buckets exceed"):
        limiter.check_bounded_namespace_change("work", "memory", clear=False)
    assert getattr(limiter, tier) == entries and len(entries) == 3
    assert bucket.tokens == 0
    assert limiter._ns_overrides == {}


@pytest.mark.parametrize(
    ("count", "existing", "clear", "allowed"),
    [
        (1023, False, False, True),
        (1024, False, False, False),
        (1024, True, False, True),
        (1024, False, True, True),
        (1025, True, True, False),
    ],
)
def test_override_capacity_keeps_replacement_and_removal_available(
    limiter: Limiter, count: int, existing: bool, clear: bool, allowed: bool
) -> None:
    limiter._ns_overrides = {(f"ns-{i}", "memory"): Limit(1, 0) for i in range(count)}
    target = "ns-0" if existing else "new"
    before = dict(limiter._ns_overrides)
    if allowed:
        limiter.check_bounded_namespace_change(target, "memory", clear=clear)
    else:
        with pytest.raises(BoundedReadError, match="overrides exceed"):
            limiter.check_bounded_namespace_change(target, "memory", clear=clear)
    assert limiter._ns_overrides == before


@pytest.mark.parametrize(
    ("mode", "workers", "shared", "changed", "allowed"),
    [
        ("off", 1, False, "", True),
        ("auto", 1, False, "", True),
        ("on", 1, False, "", False),
        ("auto", 2, False, "", False),
        ("on", 1, True, "", True),
        ("auto", 2, True, "", True),
        ("on", 1, True, "mode", False),
        ("on", 1, True, "path", False),
        ("on", 1, True, "slots", False),
        ("on", 1, True, "workers", False),
    ],
)
def test_preflight_uses_only_the_initialized_backend_and_captured_configuration(
    limiter: Limiter,
    monkeypatch: pytest.MonkeyPatch,
    mode: str,
    workers: int,
    shared: bool,
    changed: str,
    allowed: bool,
) -> None:
    monkeypatch.setattr(settings, "ratelimit_shared", mode)
    monkeypatch.setattr(settings, "web_workers", workers)
    store = Mock()
    if shared:
        monkeypatch.setattr(limiter, "_shared", store)
        monkeypatch.setattr(limiter, "_shared_key", (mode, "", 128, workers))
    if changed:
        field, value = {
            "mode": ("ratelimit_shared", "off"),
            "path": ("ratelimit_shared_path", "/unused/rate-buckets"),
            "slots": ("ratelimit_shared_slots", 256),
            "workers": ("web_workers", 2),
        }[changed]
        monkeypatch.setattr(settings, field, value)
    old_store, old_key = limiter._shared, limiter._shared_key
    resolve = Mock(side_effect=AssertionError("preflight must not open or replace a backend"))
    monkeypatch.setattr(limiter, "_store", resolve)
    if allowed:
        limiter.check_bounded_namespace_change("work", "memory", clear=False)
    else:
        with pytest.raises(BoundedReadError, match="backend is not initialized"):
            limiter.check_bounded_namespace_change("work", "memory", clear=False)
    resolve.assert_not_called()
    assert store.mock_calls == []
    assert limiter._shared is old_store and limiter._shared_key == old_key


@pytest.mark.parametrize("clear", [False, True])
@pytest.mark.parametrize("failure", ["before", "storage", "after", "none"])
async def test_stored_rate_change_rechecks_before_adoption(
    limiter: Limiter, monkeypatch: pytest.MonkeyPatch, clear: bool, failure: str
) -> None:
    old = Limit(7, 3)
    limiter._ns_overrides[("work", "memory")] = old
    monkeypatch.setattr(limit_changes, "get_limiter", lambda: limiter)
    connection = object()
    stored = []

    async def persist(*args, **kwargs):
        assert limiter._ns_overrides[("work", "memory")] == old
        assert kwargs == {"deadline": 123.0, "ch": connection}
        if failure == "storage":
            raise ConnectionError("storage unavailable")
        stored.append(args)
        if failure == "after":
            monkeypatch.setattr(settings, "ratelimit_shared", "on")

    put, remove = AsyncMock(side_effect=persist), AsyncMock(side_effect=persist)
    monkeypatch.setattr(limit_changes.ro, "put", put)
    monkeypatch.setattr(limit_changes.ro, "clear", remove)
    if failure == "before":
        monkeypatch.setattr(settings, "ratelimit_shared", "on")
    arguments = dict(capacity=2, rate=1, clear=clear, deadline=123.0, ch=connection)
    if failure != "none":
        with pytest.raises(ConnectionError if failure == "storage" else BoundedReadError):
            await limit_changes.change_namespace_rate("work", "memory", **arguments)
        assert limiter._ns_overrides[("work", "memory")] == old
        assert len(stored) == (1 if failure == "after" else 0)
    else:
        result = await limit_changes.change_namespace_rate("work", "memory", **arguments)
        assert result["ok"] is True
        if clear:
            assert ("work", "memory") not in limiter._ns_overrides
        else:
            assert limiter._ns_overrides[("work", "memory")] == Limit(2, 1)
        assert len(stored) == 1
    assert (remove.call_count, put.call_count) == (
        (0, 0) if failure == "before" else ((1, 0) if clear else (0, 1))
    )
    if stored:
        assert stored[0][:2] == (limit_changes.ro.RATELIMIT_NAMESPACE, ("work", "memory"))
        if not clear:
            assert stored[0][2] == {"capacity": 2, "rate": 1}
