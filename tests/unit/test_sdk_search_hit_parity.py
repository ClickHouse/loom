"""`MemorySearchHit` carries exactly the fields the server's search hit
serves, so a field added on the server cannot be dropped by the SDK unnoticed."""

from __future__ import annotations

from dataclasses import fields

from loom.memory import MemorySearchHit, _parse_search_hit

from server.memory.api.models_search import _MemorySearchHit


def test_hit_fields_match_the_server_hit() -> None:
    assert {f.name for f in fields(MemorySearchHit)} == set(_MemorySearchHit.model_fields)


def test_superseded_and_chunk_fields_round_trip() -> None:
    base = {"memory_key": "k", "memory_type": "semantic", "content_excerpt": "x"}
    base |= {"distance": 0.1, "similarity": 0.9}
    hit = _parse_search_hit(
        base
        | {
            "parent_memory_key": "fact/1",
            "status": "superseded",
            "superseded_by": "k2",
            "superseded_at": "2026-09-01T00:00:00Z",
        }
    )
    assert (hit.parent_memory_key, hit.superseded_by, hit.superseded_at) == (
        "fact/1",
        "k2",
        "2026-09-01T00:00:00Z",
    )
    bare = _parse_search_hit(base)
    assert (bare.parent_memory_key, bare.superseded_by, bare.superseded_at) == ("", None, None)
