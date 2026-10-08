"""Per-actor rate-limit tier + memory.list viewer cap."""

from __future__ import annotations

import pytest

from server.auth.context import set_current_auth
from server.auth.jwt import AuthInfo
from server.core.ratelimit import DEFAULT_LIMITS, Limit, Limiter, actor_fraction
from server.operations.base import Role


class FakeClock:
    def __init__(self) -> None:
        self._t = 0.0

    def __call__(self) -> float:
        return self._t

    def advance(self, dt: float) -> None:
        self._t += dt


@pytest.fixture
def fraction(monkeypatch):
    from server.settings import settings

    def _set(v: float) -> None:
        monkeypatch.setattr(settings, "ratelimit_actor_fraction", v)

    _set(1.0)
    return _set


def test_default_fraction_is_neutral_no_actor_bucket(fraction):
    assert actor_fraction() == 1.0
    lim = Limiter(clock=FakeClock())
    lim.set_namespace_limit("ns", "memory", Limit(capacity=3, rate=0))
    for _ in range(3):
        r = lim.check("ns", "memory.search", actor="sa:one")
        assert r.allowed and r.limit == 3
    assert not lim.check("ns", "memory.search", actor="sa:one").allowed
    assert lim._actor_buckets == {}


def test_fraction_sizes_the_actor_bucket_from_the_namespace_limit(fraction):
    fraction(0.5)
    lim = Limiter(clock=FakeClock())
    lim.set_namespace_limit("ns", "memory", Limit(capacity=10, rate=2))
    r = lim.check("ns", "memory.search", actor="sa:one")
    assert r.allowed and r.limit == 5  # the actor bucket is the binding one
    assert lim._actor_buckets[("", "ns", "sa:one", "memory")].rate == 1.0
    # Another namespace: its own budget for the same actor.
    assert (
        lim.check("other", "memory.search", actor="sa:one").limit
        == DEFAULT_LIMITS["memory"].capacity * 0.5
    )


def test_one_actor_is_throttled_before_the_namespace_runs_dry():
    clk = FakeClock()
    lim = Limiter(clock=clk)
    lim.set_namespace_limit("ns", "memory", Limit(capacity=10, rate=0))
    lim.set_actor_limit("ns", "sa:one", "memory", Limit(capacity=4, rate=0))
    for _ in range(4):
        assert lim.check("ns", "memory.search", actor="sa:one").allowed
    refused = lim.check("ns", "memory.search", actor="sa:one")
    assert not refused.allowed and refused.limit == 4
    # The namespace still has budget for a different actor — the first one
    # did not spend the namespace's tokens while it was being refused.
    assert lim.check("ns", "memory.search", actor="sa:two").allowed
    assert lim.check("ns", "memory.search").allowed  # anonymous pays two tiers


def test_refusal_takes_no_token_from_the_other_tiers():
    clk = FakeClock()
    lim = Limiter(clock=clk)
    lim.set_namespace_limit("ns", "memory", Limit(capacity=3, rate=0))
    lim.set_actor_limit("ns", "sa:one", "memory", Limit(capacity=1, rate=0))
    assert lim.check("ns", "memory.search", actor="sa:one").allowed
    assert not lim.check("ns", "memory.search", actor="sa:one").allowed
    # Namespace bucket: 3 - 1 = 2 left, untouched by the refusal.
    assert lim.check("ns", "memory.search").remaining == 1


def test_headers_name_the_binding_bucket():
    clk = FakeClock()
    lim = Limiter(clock=clk)
    lim.set_namespace_limit("ns", "memory", Limit(capacity=100, rate=1))
    lim.set_actor_limit("ns", "sa:one", "memory", Limit(capacity=2, rate=1))
    r = lim.check("ns", "memory.search", actor="sa:one")
    assert r.allowed and r.limit == 2 and r.remaining == 1
    r2 = lim.check("ns", "memory.search", actor="sa:one")
    assert r2.allowed and r2.remaining == 0
    r3 = lim.check("ns", "memory.search", actor="sa:one")
    assert not r3.allowed and r3.limit == 2 and r3.retry_after_s > 0
    clk.advance(1.0)
    assert lim.check("ns", "memory.search", actor="sa:one").allowed


def test_reset_clears_actor_buckets_but_keeps_overrides():
    lim = Limiter(clock=FakeClock())
    lim.set_actor_limit("ns", "sa:one", "memory", Limit(capacity=1, rate=0))
    assert lim.check("ns", "memory.search", actor="sa:one").allowed
    assert not lim.check("ns", "memory.search", actor="sa:one").allowed
    lim.reset()
    assert lim.check("ns", "memory.search", actor="sa:one").allowed
    assert not lim.check("ns", "memory.search", actor="sa:one").allowed


# --- memory.list viewer cap --------------------------------------------------


def _auth(**kw) -> AuthInfo:
    base = dict(
        subject="sa:bot",
        org="acme",
        namespaces=("shop",),
        roles=(Role.VIEWER,),
        is_service_account=True,
    )
    base.update(kw)
    return AuthInfo(**base)


def test_viewer_list_limit_is_capped_and_writer_is_not(monkeypatch):
    from server.memory.api.models_crud import MemoryListInput
    from server.memory.app.query import _cap_viewer_list_limit
    from server.settings import settings

    monkeypatch.setattr(settings, "memory_list_viewer_limit", 100)
    inp = MemoryListInput(org="acme", namespace="shop", agent="*", limit=1000)
    try:
        set_current_auth(_auth())
        assert _cap_viewer_list_limit(inp).limit == 100
        set_current_auth(_auth(roles=(Role.WRITER,)))
        assert _cap_viewer_list_limit(inp).limit == 1000
        # Scoped token: the per-namespace role decides.
        set_current_auth(_auth(roles=(), ns_roles=(("shop", "viewer"),)))
        assert _cap_viewer_list_limit(inp).limit == 100
        set_current_auth(_auth(roles=(), ns_roles=(("shop", "writer"),)))
        assert _cap_viewer_list_limit(inp).limit == 1000
        # Under the cap: untouched object.
        small = MemoryListInput(org="acme", namespace="shop", agent="*", limit=50)
        set_current_auth(_auth())
        assert _cap_viewer_list_limit(small) is small
        # Cap disabled.
        monkeypatch.setattr(settings, "memory_list_viewer_limit", 0)
        assert _cap_viewer_list_limit(inp).limit == 1000
    finally:
        set_current_auth(None)
    # No token (auth off): no cap.
    monkeypatch.setattr(settings, "memory_list_viewer_limit", 100)
    assert _cap_viewer_list_limit(inp).limit == 1000
