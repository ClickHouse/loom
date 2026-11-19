"""Source copies contribute complete hits and never borrow another copy's rank credit."""

from __future__ import annotations

import json

import pytest

from server.knowledge.app import source_ranking as app
from server.knowledge.app.search import ChunkHit
from server.knowledge.app.source_access import Admitted
from server.knowledge.app.source_search import Hydrated


def copy(label, *, uri="drive:file", distance=0.1, lexical=0.0, matched=()):
    sid = ("1" if label == "A" else "2") * 8 + "-1111-4111-8111-111111111111"
    version, revision = (10, 0) if label == "A" else (20, 1)
    candidate = {
        "namespace": "acl-" + "1" * 24,
        "source_uri": uri + "#" + sid,
        "chunk_idx": 0,
        "doc_version": version,
        "revision": revision,
        "distance": distance,
        "lexical": lexical,
        "matched": list(matched),
    }
    return Hydrated(
        Admitted("docs", candidate, {"version": version, "title": label}),
        {"content": label, "chunk_metadata": json.dumps({"revision": revision})},
    )


def ordinary(uri="drive:ordinary", *, score=0.8):
    return ChunkHit("docs", uri, 0, "ordinary content", ["ordinary"], 0.2, score=score)


def test_public_mirror_hits_omit_internal_ownership_tags():
    item = copy("A")
    item.row["tags"] = ["source-id:private", "kind:notes"]
    [result] = app.rank({}, {"semantic": [item]}, legs=("semantic",), fallback=[], top_k=1)
    assert result.chunk.tags == ["kind:notes"]


def test_public_ordinary_hits_keep_their_existing_tags():
    hit = ordinary()
    hit.tags[:] = ["source-id:ordinary", "kind:notes"]
    [result] = app.rank({"semantic": [hit]}, {}, legs=("semantic",), fallback=[hit], top_k=1)
    assert result.chunk is hit and result.chunk.tags == ["source-id:ordinary", "kind:notes"]


def test_different_semantic_and_lexical_copies_never_mix_text_distance_or_credit():
    a, b = copy("A"), copy("B", distance=0.5, lexical=4.0)
    [result] = app.rank(
        {},
        {"semantic": [a], "lexical": [b]},
        legs=("semantic", "lexical"),
        fallback=[],
        top_k=1,
    )
    hit = result.chunk
    assert (hit.content, hit.distance, hit.lexical_score, hit.revision, hit.doc_version) == (
        "A",
        0.1,
        0.0,
        0,
        10,
    )
    assert hit.score == pytest.approx(1 / 61)
    assert result.admitted is a.admitted and result.mirrored
    assert hit.namespace == "docs" and hit.source_uri == "drive:file"


def test_lexical_and_key_credit_can_choose_one_copy_without_borrowing_semantic_distance():
    a, b = copy("A"), copy("B", distance=0.5, lexical=4.0, matched=("key",))
    [result] = app.rank(
        {},
        {"semantic": [a], "lexical": [b], "keys": [b]},
        legs=("semantic", "lexical", "keys"),
        fallback=[],
        top_k=1,
    )
    hit = result.chunk
    assert (hit.content, hit.distance, hit.lexical_score, hit.revision, hit.doc_version) == (
        "B",
        0.5,
        4.0,
        1,
        20,
    )
    assert hit.score == pytest.approx(2 / 61)
    assert result.admitted is b.admitted and hit.keys_matched == ("key",)


def test_same_storage_copy_combines_its_own_leg_credit():
    a = copy("A", lexical=4.0)
    [result] = app.rank(
        {},
        {"semantic": [a], "lexical": [a]},
        legs=("semantic", "lexical"),
        fallback=[],
        top_k=1,
    )
    assert result.chunk.content == "A" and result.chunk.score == pytest.approx(2 / 61)


def test_different_versions_at_one_storage_address_keep_separate_payload_and_credit():
    a, b = copy("A"), copy("A", distance=0.5, lexical=4)
    b.admitted.candidate["doc_version"] = 20
    b.row["content"] = "different version"
    [result] = app.rank(
        {},
        {"semantic": [a], "lexical": [b]},
        legs=("semantic", "lexical"),
        fallback=[],
        top_k=1,
    )
    assert result.chunk.content == "A" and result.chunk.doc_version == 10
    assert result.chunk.distance == 0.1 and result.chunk.lexical_score == 0
    assert result.chunk.score == pytest.approx(1 / 61)


def test_copy_collapse_happens_before_final_page_limit():
    out = app.rank(
        {},
        {
            "semantic": [copy("A"), copy("A", uri="drive:second", distance=0.8)],
            "lexical": [
                copy("B", distance=0.5, lexical=4),
                copy("B", uri="drive:third", lexical=1),
            ],
        },
        legs=("semantic", "lexical"),
        fallback=[],
        top_k=2,
    )
    assert len(out) == 2
    assert out[0].chunk.source_uri == "drive:file"
    assert out[1].chunk.source_uri in {"drive:second", "drive:third"}
    assert out[1].chunk.score == pytest.approx(1 / 62)


def test_serving_ordinary_family_wins_across_legs_without_changing_its_fields():
    hit = ordinary(uri="drive:file")
    [result] = app.rank(
        {"semantic": [hit]},
        {"lexical": [copy("A", lexical=99)]},
        legs=("semantic", "lexical"),
        fallback=[hit],
        top_k=1,
    )
    assert result.chunk is hit and result.admitted is None and not result.mirrored


def test_ordinary_pool_from_an_unselected_leg_cannot_shadow_a_mirror():
    hit = ordinary(uri="drive:file")
    [result] = app.rank(
        {"keys": [hit]},
        {"lexical": [copy("A", lexical=3)]},
        legs=("lexical",),
        fallback=[],
        top_k=1,
    )
    assert result.mirrored and result.chunk.lexical_score == result.chunk.score == 3


def test_no_admitted_mirrors_preserves_fallback_order_identity_and_scores():
    a, b = ordinary("drive:z", score=0.7), ordinary("drive:a", score=0.7)
    out = app.rank(
        {"semantic": [a, b]},
        {},
        legs=("semantic",),
        fallback=[a, b],
        top_k=2,
    )
    assert [r.chunk for r in out] == [a, b]
    assert out[0].chunk is a and out[1].chunk is b
    assert all(not r.mirrored for r in out)


def test_semantic_scores_and_native_uri_fragments_survive_projection():
    [result] = app.rank(
        {},
        {"semantic": [copy("A", uri="drive:file#native", distance=0.25)]},
        legs=("semantic",),
        fallback=[],
        top_k=1,
    )
    assert result.chunk.source_uri == "drive:file#native" and result.chunk.score == 0.75


def test_ordinary_semantic_hit_keeps_its_payload_and_similarity_when_mirrors_match():
    hit = ordinary()
    out = app.rank(
        {"semantic": [hit]},
        {"semantic": [copy("A")]},
        legs=("semantic",),
        fallback=[hit],
        top_k=2,
    )
    assert out[1].chunk is hit and out[1].chunk.score == hit.score
    assert not out[1].mirrored
