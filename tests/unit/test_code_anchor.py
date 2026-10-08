"""code_anchor: proximity extraction, write-time disambiguation (the homonym
skip), and read-time staleness classification keyed on symbol_id."""

from __future__ import annotations

from server.code.domain.anchor import (
    CodeClaim,
    extract_claims,
    resolve_anchor,
)


def test_extract_claims_pairs_by_proximity_not_cartesian() -> None:
    text = (
        "the guard moved in src/Planner/PlannerJoinTree.cpp near collectSets; "
        "separately, unrelatedHelper lives far away in prose ... "
        + "x" * 200
        + " and getExtremes is over here in src/Columns/ColumnArray.cpp"
    )
    claims = extract_claims(text)
    pairs = {(c.symbol, c.claimed_path.rsplit("/", 1)[-1]) for c in claims}
    assert ("collectSets", "PlannerJoinTree.cpp") in pairs
    assert ("getExtremes", "ColumnArray.cpp") in pairs
    # proximity gate: the far-away path must NOT capture ColumnArray's symbol
    assert ("getExtremes", "PlannerJoinTree.cpp") not in pairs


def test_resolve_anchor_claimed_file_disambiguates_a_homonym() -> None:
    # getValue exists in many files; the claim names one of them -> anchor THAT
    claim = CodeClaim(symbol="getValue", claimed_path="src/Interpreters/SetUtils.cpp")
    defs = [
        {"symbol_id": "id_a", "path": "src/Interpreters/SetUtils.cpp", "start_line": 40},
        {"symbol_id": "id_b", "path": "src/Columns/ColumnConst.h", "start_line": 10},
        {"symbol_id": "id_c", "path": "src/Formats/CapnProtoSerializer.cpp", "start_line": 99},
    ]
    a = resolve_anchor(claim, defs)
    assert a is not None
    assert a.symbol_id == "id_a" and a.claim_matched is True


def test_resolve_anchor_skips_ambiguous_homonym_rather_than_guess() -> None:
    # bare name resolves to many, claim names NONE of them -> SKIP (no false anchor)
    claim = CodeClaim(symbol="getValue", claimed_path="src/Interpreters/resolveFunction.cpp")
    defs = [
        {"symbol_id": "id_b", "path": "src/Columns/ColumnConst.h", "start_line": 10},
        {"symbol_id": "id_c", "path": "src/Formats/CapnProtoSerializer.cpp", "start_line": 99},
    ]
    assert resolve_anchor(claim, defs) is None


def test_resolve_anchor_unique_symbol_anchors_even_without_claim_match() -> None:
    claim = CodeClaim(
        symbol="computeMaxTableNameLength", claimed_path="src/Storages/DistributedSink.cpp"
    )
    defs = [
        {"symbol_id": "id_x", "path": "src/Storages/computeMaxTableNameLength.cpp", "start_line": 7}
    ]
    a = resolve_anchor(claim, defs)
    assert a is not None
    assert a.symbol_id == "id_x" and a.claim_matched is False  # already elsewhere


def test_resolve_anchor_unresolved_symbol_is_skipped() -> None:
    assert resolve_anchor(CodeClaim("nope", "src/Foo.cpp"), []) is None


def test_resolve_anchor_carries_the_defining_chunk_hash() -> None:
    claim = CodeClaim(symbol="collectSets", claimed_path="src/Planner/CollectSets.cpp")
    defs = [
        {
            "symbol_id": "sid1",
            "path": "src/Planner/CollectSets.cpp",
            "start_line": 10,
            "body_hash": "a" * 64,
        }
    ]
    a = resolve_anchor(claim, defs)
    assert a is not None and a.content_hash == "a" * 64


def test_fixedstring_hashes_decode_to_hex_text() -> None:
    from server.code.store.index.sql import _hex

    raw = b"341d76b7c4" + b"0" * 54
    assert _hex(raw) == raw.decode()
    assert _hex(raw + b"\x00\x00") == raw.decode(), "FixedString NUL padding stripped"
    assert _hex("abc") == "abc" and _hex(None) == "" and _hex(b"") == ""
    assert not _hex(raw).startswith("b'"), "never the bytes repr"
