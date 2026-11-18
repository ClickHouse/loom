"""`MemorySearchHit` carries exactly the fields the server's search hit
serves, so a field added on the server cannot be dropped by the SDK unnoticed."""

from __future__ import annotations

from dataclasses import fields

from loom.memory import MemorySearchHit, _parse_search_hit

from server.memory.api.models_search import _MemorySearchHit

_POSITIONAL_HIT_FIELDS = (
    "memory_key memory_type content_excerpt distance similarity namespace code_state "
    "rank_score below_threshold temporal_anchor temporal_confidence valid_at source "
    "parent_memory_key via_path tags score_components explanation status superseded_by "
    "superseded_at resolved_from version created_at source_class source_resource_id "
    "verified user_id session_id speaker about_other entities derived_from body_truncated "
    "next reinforcement_count used_count used_cited_count harm_count evidence_count acted_count pinned"
).split()


def test_hit_positional_constructor_preserves_user_and_session() -> None:
    defaults = MemorySearchHit("key", "semantic", "content")
    values = {name: getattr(defaults, name) for name in _POSITIONAL_HIT_FIELDS}
    values.update(user_id="case-user", session_id="case-session", speaker="assistant")
    hit = MemorySearchHit(*(values[name] for name in _POSITIONAL_HIT_FIELDS))
    assert (hit.user_id, hit.session_id, hit.speaker, hit.agent) == (
        "case-user",
        "case-session",
        "assistant",
        "",
    )


def test_hit_positional_pattern_preserves_existing_fields() -> None:
    assert MemorySearchHit.__match_args__ == tuple(_POSITIONAL_HIT_FIELDS)
    hit = MemorySearchHit("key", "semantic", "content", agent="writer")
    assert hit.agent == "writer"


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
