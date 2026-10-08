"""memory.ask follows the identifiers the reached memories name."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from typing import Any

from server.memory.app.ask import identifiers

K0 = "cd613e30-d8f1-4adf-91b7-584a2265b1f5"
K1 = "0b5f2a1c-7e3d-4f60-9a2b-1c2d3e4f5a6b"
K2 = "9f8e7d6c-5b4a-4321-8765-4321fedcba98"
FINAL = "11112222-3333-4444-5555-666677778888"


def _hit(key: str, text: str) -> SimpleNamespace:
    return SimpleNamespace(
        memory_key=key, content_excerpt=text, rank_score=1.0, similarity=1.0, explanation=""
    )


def test_next_identifiers_skips_seen_tokens_and_uuid_segments() -> None:
    hits = [
        _hit("kv_a", f"Key: {K0}, Value: {K1}"),
        _hit("x", "PR 109741 fixed by 3650f447a1b2c3d4e5f6"),
    ]
    out = identifiers.next_identifiers(hits, seen=[f"Find the value for key {K0}", K0])
    assert out[0] == K1 and set(out) == {K1, "109741", "3650f447a1b2c3d4e5f6"}
    # Already chased identifiers do not come back; a UUID's own segment is not new.
    assert identifiers.next_identifiers(hits, seen=[K0, K1, "109741", "3650f447a1b2c3d4e5f6"]) == []
    assert identifiers.next_identifiers([_hit("k", "584a2265b1f5 alone")], seen=[K0]) == []


def test_chase_walks_hops_until_the_value_is_not_a_key(monkeypatch: Any) -> None:
    store = {
        K0: _hit("kv_0", f"Key: {K0}, Value: {K1}"),
        K1: _hit("kv_1", f"Key: {K1}, Value: {K2}"),
        K2: _hit("kv_2", f"Key: {K2}, Value: {FINAL}"),
    }
    calls: list[list[str]] = []

    async def fake_inject(search_input: Any, idents: list[str], ranked: list[Any]) -> list[Any]:
        calls.append(list(idents))
        have = {h.memory_key for h in ranked}
        return [store[i] for i in idents if i in store and store[i].memory_key not in have]

    monkeypatch.setattr(identifiers, "_inject_identifier_memories", fake_inject)
    injected = [store[K0]]
    added, hops = asyncio.run(
        identifiers._chase_identifiers(
            SimpleNamespace(query=f"value for key {K0}"),
            seen=[f"value for key {K0}", K0],
            frontier=injected,
            ranked=injected,
            hops=4,
            cap=8,
        )
    )
    assert [h.memory_key for h in added] == ["kv_1", "kv_2"]
    # Hop 1 chased K1, hop 2 chased K2, hop 3 chased FINAL and found nothing → stop.
    assert calls == [[K1], [K2], [FINAL]] and hops == 3
    assert all(
        h.rank_score == 0.9 and h.explanation.startswith("identifier chase hop") for h in added
    )


def test_chase_respects_the_hop_and_row_caps(monkeypatch: Any) -> None:
    async def fake_inject(search_input: Any, idents: list[str], ranked: list[Any]) -> list[Any]:
        # Every identifier reaches a memory naming yet another identifier.
        return [_hit(f"m_{i}", f"Key: {i}, Value: {i[:8]}aaaa-{i[9:]}") for i in idents]

    monkeypatch.setattr(identifiers, "_inject_identifier_memories", fake_inject)
    frontier = [_hit("kv_0", f"Key: {K0}, Value: {K1}")]
    added, hops = asyncio.run(
        identifiers._chase_identifiers(
            SimpleNamespace(query=K0),
            seen=[K0],
            frontier=frontier,
            ranked=frontier,
            hops=2,
            cap=8,
        )
    )
    assert hops == 2 and 2 <= len(added) <= 8  # two hops walked, rows bounded by the cap
    added, hops = asyncio.run(
        identifiers._chase_identifiers(
            SimpleNamespace(query=K0),
            seen=[K0],
            frontier=frontier,
            ranked=frontier,
            hops=10,
            cap=1,
        )
    )
    assert hops == 1 and len(added) == 1


def test_chase_fails_open_on_a_read_error(monkeypatch: Any) -> None:
    async def boom(*a: Any, **k: Any) -> list[Any]:
        raise RuntimeError("ch down")

    monkeypatch.setattr(identifiers, "_inject_identifier_memories", boom)
    frontier = [_hit("kv_0", f"Key: {K0}, Value: {K1}")]
    added, hops = asyncio.run(
        identifiers._chase_identifiers(
            SimpleNamespace(query=K0),
            seen=[K0],
            frontier=frontier,
            ranked=frontier,
            hops=3,
            cap=8,
        )
    )
    assert added == [] and hops == 0


def test_injectable_identifiers_include_uuids_and_hashes() -> None:
    idents = identifiers._injectable_identifiers(f"Find the value for key {K0}")
    assert K0 in idents
    idents = identifiers._injectable_identifiers(
        "what changed in commit 3650f447a1b2c3d4e5f6 for PR 109741?"
    )
    assert "3650f447a1b2c3d4e5f6" in idents and "109741" in idents
    # Prose still injects nothing.
    assert identifiers._injectable_identifiers("what hobbies do they enjoy") == []


def test_cited_hits_start_the_chase_from_ranked_rows_too() -> None:
    hits = [
        _hit("kv_0", f"Key: {K0}, Value: {K1}"),
        _hit("other", "unrelated fact"),
        _hit("h", "@rdev1 reviewed"),
    ]
    assert [h.memory_key for h in identifiers._cited_hits(hits, [K0])] == ["kv_0"]
    assert [h.memory_key for h in identifiers._cited_hits(hits, ["@rdev1"])] == ["h"]
    assert identifiers._cited_hits(hits, []) == []
