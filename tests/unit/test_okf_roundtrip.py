"""bench/okf_roundtrip.py — drift and overlap arithmetic (network-free)."""

from __future__ import annotations

from bench.okf_roundtrip import (
    compare_rows,
    edge_drift,
    evidence_drift,
    outside_memory_pct,
    retrieval_overlap,
)
from server.memory.domain import okf_evidence as ev


def test_compare_rows_ignores_bundle_tags_and_counts_missing() -> None:
    src = [
        {
            "memory_key": "a",
            "value": "x ",
            "tags": ["t1"],
            "valid_at": "2023-05-01 00:00:00",
            "memory_type": "semantic",
            "verified_by": "alice",
        },
        {
            "memory_key": "b",
            "value": {"k": 1},
            "tags": [],
            "valid_at": None,
            "memory_type": "semantic",
        },
        {
            "memory_key": "c",
            "value": "gone",
            "tags": [],
            "valid_at": None,
            "memory_type": "semantic",
        },
    ]
    imp = [
        {
            "memory_key": "a",
            "value": "x",
            "tags": ["t1", "okf:type:fact"],
            "valid_at": "2023-05-01",
            "memory_type": "semantic",
            "source_class": "imported",
        },
        {
            "memory_key": "b",
            "value": {"k": 1},
            "tags": ["okf:type:fact"],
            "valid_at": "",
            "memory_type": "procedural",
            "source_class": "imported",
        },
    ]
    d = compare_rows(src, imp)
    assert d["source_rows"] == 3 and d["imported_rows"] == 2 and d["missing_after_import"] == ["c"]
    assert d["source_rows_total"] == 3 and d["imported_rows_total"] == 2
    assert (
        d["value_equal"] == 2
        and d["tags_equal"] == 2
        and d["valid_at_equal"] == 2
        and d["type_equal"] == 1
    )
    assert (
        d["source_attested"] == 1 and d["imported_attested"] == 0
    )  # the attestation did not travel
    assert d["imported_source_class"] == {"imported": 2}


def test_retrieval_overlap_jaccard_and_top1() -> None:
    o = retrieval_overlap([(["a", "b", "c"], ["a", "b", "c"]), (["a", "b"], ["b", "x"]), ([], [])])
    assert o["questions"] == 3 and o["identical_sets_pct"] == 66.7
    assert o["top1_agreement_pct"] == 33.3 and o["jaccard_at_k_min"] == round(1 / 3, 3)
    assert retrieval_overlap([]) == {"questions": 0}


def test_outside_memory_pct_counts_only_sibling_tables() -> None:
    assert outside_memory_pct({"fact": 3, "chunk": 5, "summary": 2}) == 70.0
    assert outside_memory_pct({"fact": 4, "": 1}) == 0.0
    assert outside_memory_pct({}) == 0.0


def test_evidence_drift_counts_bodies_links_and_digests() -> None:
    a = ev.ChunkRecord(session_id="s1", chunk_index=0, content="x", memory_keys=["k1", "k2"])
    b = ev.ChunkRecord(session_id="s1", chunk_index=1, content="y", memory_keys=["k1"])
    b2 = ev.ChunkRecord(session_id="s1", chunk_index=1, content="y-edited", memory_keys=["k1"])
    d = ev.DigestRecord(session_id="s1", summary_text="digest")
    out = evidence_drift([a, b], [a, b2], [d], [d])
    assert out["source_chunks"] == 2 and out["imported_chunks"] == 2
    assert (
        out["chunk_bodies_equal"] == 1 and out["source_links"] == 3 and out["imported_links"] == 3
    )
    assert out["source_digests"] == 1 and out["digest_texts_equal"] == 1


def test_edge_drift_counts_travelled_edges_by_kind_and_leaves_the_chain_home() -> None:
    def e(a: str, b: str, r: str) -> dict:
        return {"from_key": a, "to_key": b, "relation": r}

    src = [
        e("a", "b", "supports"),
        e("b", "c", "supersedes"),
        e("a", "c", "precedes"),
        e("c", "a", "refines"),
    ]
    imp = [e("a", "b", "supports"), e("b", "c", "supersedes")]
    d = edge_drift(src, imp)
    assert d["source_edges"] == 4 and d["source_edges_stay_home"] == 1
    assert d["source_edges_expected"] == 3 and d["edges_travelled"] == 2
    assert d["edges_travelled_pct"] == 66.7
    assert d["by_kind"] == {
        "refines": {"source": 1, "imported": 0},
        "supersedes": {"source": 1, "imported": 1},
        "supports": {"source": 1, "imported": 1},
    }
    assert edge_drift([], [])["edges_travelled_pct"] == 100.0
