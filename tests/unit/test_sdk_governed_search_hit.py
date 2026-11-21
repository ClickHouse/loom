"""Additive governance metadata preserves positional search-hit consumers."""

from typing import Any

from loom.memory import MemorySearchHit

_POSITIONAL_FIELDS = (
    "memory_key",
    "memory_type",
    "content_excerpt",
    "distance",
    "similarity",
    "namespace",
    "code_state",
    "rank_score",
    "below_threshold",
    "temporal_anchor",
    "temporal_confidence",
    "valid_at",
    "source",
    "parent_memory_key",
    "via_path",
    "tags",
    "score_components",
    "explanation",
    "status",
    "superseded_by",
    "superseded_at",
    "resolved_from",
    "version",
    "created_at",
    "source_class",
    "source_resource_id",
    "verified",
    "user_id",
    "session_id",
    "speaker",
    "about_other",
    "entities",
    "derived_from",
    "body_truncated",
    "next",
    "reinforcement_count",
    "used_count",
    "used_cited_count",
    "harm_count",
    "evidence_count",
    "acted_count",
    "pinned",
)
_VALUES: tuple[Any, ...] = (
    "key",
    "semantic",
    "excerpt",
    0.1,
    0.9,
    "namespace",
    "fresh",
    1.0,
    False,
    "anchor",
    0.8,
    "valid",
    "fact",
    "parent",
    ("path",),
    ("tag",),
    {"sim": 0.9},
    "explanation",
    "active",
    "successor",
    "superseded",
    "resolved",
    7,
    "created",
    "asserted",
    "resource",
    True,
    "user",
    "session",
    "speaker",
    False,
    ("entity",),
    ("derived",),
    False,
    "read",
    5,
    6,
    7,
    8,
    9,
    10,
    True,
)


def test_governance_does_not_shift_positional_hit_fields() -> None:
    hit = MemorySearchHit(*_VALUES)
    assert {name: getattr(hit, name) for name in _POSITIONAL_FIELDS} == dict(
        zip(_POSITIONAL_FIELDS, _VALUES, strict=True)
    )
    assert hit.governed is False
    assert MemorySearchHit(*_VALUES, governed=True).governed is True


def test_governance_does_not_shift_positional_hit_patterns() -> None:
    assert MemorySearchHit.__match_args__ == _POSITIONAL_FIELDS
    hit = MemorySearchHit(*_VALUES)
    match hit:
        case MemorySearchHit(_, _, _, _, _, _, _, _, _, anchor, confidence, valid_at, source):
            assert (anchor, confidence, valid_at, source) == ("anchor", 0.8, "valid", "fact")
        case _:
            raise AssertionError("The public search-hit pattern did not match")
