"""bodies="unit" reads each body under the session the row was WRITTEN in,
not the caller's session,
and says when a body could not be fetched."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

import server.memory.store as repo
from server.memory.api.models_search import _MemorySearchHit
from server.memory.app.search.payload import _expand_unit_bodies

CLIP = "x" * 1200


def _hit(key: str, session_id: str = "", excerpt: str = CLIP) -> _MemorySearchHit:
    return _MemorySearchHit(
        memory_key=key,
        memory_type="semantic",
        content_excerpt=excerpt,
        distance=0.2,
        similarity=0.8,
        session_id=session_id,
    )


def _input(**kw):
    base = dict(
        org="o",
        namespace="n",
        agent="*",
        user_id="-",
        session_id="-",
        bodies="unit",
        body_budget_tokens=4000,
    )
    base.update(kw)
    return SimpleNamespace(**base)


@pytest.mark.asyncio
async def test_body_is_read_under_the_hit_session_and_missing_bodies_are_flagged(monkeypatch):
    calls: list[tuple[str, list[str]]] = []
    store = {
        ("chunk-1", "k1"): "the whole first body",
        ("-", "k3"): "a body under the caller's session",
    }

    async def fake_get_many(identity, keys, **_):
        calls.append((identity.session_id, list(keys)))
        return [
            SimpleNamespace(memory_key=k, value=store[(identity.session_id, k)], sensitivity="")
            for k in keys
            if (identity.session_id, k) in store
        ]

    monkeypatch.setattr(repo, "get_many_by_keys", fake_get_many)
    hits = [_hit("k1", session_id="chunk-1"), _hit("k2", session_id="chunk-2"), _hit("k3")]
    returned, elided, tokens = await _expand_unit_bodies(hits, _input())

    assert sorted(calls) == [("-", ["k3"]), ("chunk-1", ["k1"]), ("chunk-2", ["k2"])]
    assert hits[0].content_excerpt == "the whole first body" and hits[0].body_truncated is False
    assert hits[2].content_excerpt == "a body under the caller's session"
    # k2 was written under a session no row answered for: the clip stays, and
    # the hit SAYS so instead of passing the clip off as the body.
    assert hits[1].content_excerpt == CLIP and hits[1].body_truncated is True
    assert hits[1].next == 'memory.get(memory_key="k2")'
    assert (returned, elided) == (2, 1) and tokens > 0


@pytest.mark.asyncio
async def test_short_excerpt_without_a_body_is_not_flagged(monkeypatch):
    async def fake_get_many(identity, keys, **_):
        return []

    monkeypatch.setattr(repo, "get_many_by_keys", fake_get_many)
    h = _hit("k9", excerpt="short value, already whole")
    returned, elided, _ = await _expand_unit_bodies([h], _input())
    assert (returned, elided) == (0, 0) and h.body_truncated is False


@pytest.mark.asyncio
async def test_a_high_row_is_never_a_search_body(monkeypatch):
    # The body read leaves 'high' rows out before it picks one per key: the newest row of
    # the key may be another identity's 'high' one.
    # Without the flag the fake answers the high row, so the test fails if the read stops
    # asking (a TypeError would be swallowed by the body read's own except).
    async def fake_get_many(identity, keys, *, scope, exclude_high_sensitivity=False):
        high = SimpleNamespace(memory_key="k1", value="vault token", sensitivity="high")
        return [] if exclude_high_sensitivity else [high]

    monkeypatch.setattr(repo, "get_many_by_keys", fake_get_many)
    h = _hit("k1")
    await _expand_unit_bodies([h], _input())
    assert h.content_excerpt == CLIP and "vault" not in h.content_excerpt


@pytest.mark.asyncio
async def test_two_writers_of_one_key_in_one_session_each_get_their_own_body(monkeypatch):
    # Each hit is read under the row it names, so neither writer's body hides the other's.
    bodies = {("a", "u1", "s1"): "the first user's body", ("a", "u2", "s1"): "the second's"}
    reads = []

    async def fake_get_many(identity, keys, **_):
        who = (identity.agent, identity.user_id, identity.session_id)
        reads.append(who)
        return [SimpleNamespace(memory_key="k", value=bodies[who], sensitivity="")]

    monkeypatch.setattr(repo, "get_many_by_keys", fake_get_many)
    hits = [_hit("k", session_id="s1"), _hit("k", session_id="s1")]
    hits[0]._row_identity, hits[1]._row_identity = ("a", "u1", "s1"), ("a", "u2", "s1")
    returned, elided, _ = await _expand_unit_bodies(hits, _input())
    assert sorted(reads) == sorted(bodies)
    assert [h.content_excerpt for h in hits] == ["the first user's body", "the second's"]
    assert (returned, elided) == (2, 0)


@pytest.mark.asyncio
async def test_an_own_user_read_never_serves_another_users_body(monkeypatch):
    from server.identity.domain.read_scope import ReadFilters
    from server.memory.app.search import payload

    # The store answers a read only for the user its scope allows.
    async def fake_get_many(identity, keys, *, scope, **_):
        if scope.user_id != identity.user_id:
            return []
        return [SimpleNamespace(memory_key=k, value="u2's body", sensitivity="") for k in keys]

    monkeypatch.setattr(repo, "get_many_by_keys", fake_get_many)
    only_u1 = ReadFilters(scope="own_user", user_id="u1", session_id=None)
    monkeypatch.setattr(payload, "request_scope", lambda: only_u1)
    h = _hit("k", session_id="s1")
    h._row_identity = ("a", "u2", "s1")
    await _expand_unit_bodies([h], _input())
    assert h.content_excerpt == CLIP and h.body_truncated is True


@pytest.mark.asyncio
async def test_a_shared_row_is_read_under_the_callers_identity(monkeypatch):
    # a promoted row's embedding names the `_shared` agent, which no loom.memory row has
    reads = []

    async def fake_get_many(identity, keys, **_):
        reads.append(identity.agent)
        return [SimpleNamespace(memory_key=k, value="the body", sensitivity="") for k in keys]

    monkeypatch.setattr(repo, "get_many_by_keys", fake_get_many)
    h = _hit("k", session_id="-")
    h._row_identity = ("_shared", "-", "-")
    await _expand_unit_bodies([h], _input())
    assert reads == ["*"] and h.content_excerpt == "the body"
