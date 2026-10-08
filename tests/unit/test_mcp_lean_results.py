"""An MCP tool result carries the answer, not the ranking."""

from __future__ import annotations

import copy
import inspect
import json
import re

from server.transport import mcp_call, mcp_outcome, mcp_tools
from server.transport.lean_results import DIAGNOSTIC_KEYS, lean, lean_result

# A representative loom_search payload, cut down: two fact hits, one episodic chunk, the reader paragraph, the
# retrieval accounting. The answer is one word.
PAYLOAD = {
    "memory": {
        "results": [
            {
                "memory_key": "fav_place",
                "memory_type": "semantic",
                "content_excerpt": '"mountain" (#fav_place)',
                "distance": 0.4227,
                "similarity": 0.577,
                "rank_score": 0.552,
                "temporal_anchor": "",
                "temporal_confidence": 0,
                "valid_at": "",
                "via_path": [],
                "entities": [],
                "user_id": "",
                "source": "fact",
                "status": "active",
                "version": 1789756953983417900,
                "created_at": "2026-09-18 18:42:33",
                "namespace": "alice",
                "body_truncated": False,
                "next": "",
                "source_resource_id": "",
                "source_class": "asserted",
                "verified": False,
                "explanation": "fact hit from namespace alice; similarity=0.58; rank_score=0.55; mode=semantic",
                "session_id": "-",
                "speaker": "",
                "about_other": False,
                "derived_from": [],
                "score_components": {
                    "semantic": 0.577,
                    "rank_score": 0.552,
                    "reinforcement": -0.0256,
                    "rank_score_raw": 0.552,
                    "entity_ann_boost": 0,
                    "foreign": 1,
                },
            },
            {
                "memory_key": "chunk:c955:0",
                "memory_type": "episodic",
                "content_excerpt": "[user @ 2026-09-18 18:42] [tool_use] tool=mcp__plugin_loom_loom__loom_search ...",
                "distance": 0.594,
                "similarity": 0.406,
                "rank_score": 0.446,
                "source": "chunk",
                "status": "",
                "version": 0,
                "created_at": "",
                "namespace": "alice",
                "explanation": "chunk hit",
                "score_components": {"semantic": 0.406, "token_density": 0.04},
            },
        ],
        "reader_block": '[2026-09-18 18:42] [fav_place] (semantic) "mountain" (#fav_place)',
        "reader_instructions": "You answer a question using ONLY the stored memories provided below. "
        * 40,
        "bodies_returned": 0,
        "bodies_elided": 0,
        "tokens_returned": 0,
        "namespaces_searched": ["alice"],
        "candidates_total": 38,
        "next_page": "page/e533e035bf78456a6d5d@0",
        "page_offset": 0,
        "chunk_share": 0.6,
        "embedding_model": "text-embedding-3-large",
        "next": "",
        "scratchpad": "",
        "scratchpad_blocks": 0,
        "reinforcement": "not_credited (measurement_label)",
        "graph_seeds": 0,
        "graph_neighbors_added": 0,
        "triples_extracted_from_query": 0,
        "triples_matched": 0,
        "set_expansion_phrases": 0,
        "top_k_ceiling": 0,
        "abstain": False,
        "abstain_reason": "",
        "abstain_code": "",
        "degraded": [],
        "recipe": "",
        "score_explanation": "mode=semantic; plan=...",
        "retrieval_confidence": 0.597,
        "retrieval_risk": "medium",
        "route_agreement": 1,
        "calibration_explanation": "top_similarity=0.58, margin=0.07",
        "fallback_rounds_used": 0,
        "fallback_budget_exhausted": False,
        "fallback_elapsed_ms": 2107,
        "retrieval_plan": "mode=semantic, planes=fact,chunk, hyde=off, budget=standard",
        "retrieval_cost_estimate": {
            "budget_class": "standard",
            "llm_calls": 1,
            "vector_queries": 24,
        },
        "retrieval_work": {
            "stage_ms": {"plan": 0.3, "embed": 0.2, "vector_legs": 536, "total": 990.3},
            "vector_queries": 23,
            "namespace_count": 24,
        },
        "pending_extraction": 0,
        "derived_aggregate": None,
    },
    "knowledge": {
        "results": [],
        "embedding_model": "text-embedding-3-large",
    },
    "profile": {"results": []},
    "conversations": {
        "hits": [
            {
                "thread_id": "4ab6",
                "msg_id": "8a46",
                "role": "user",
                "content_excerpt": "which location u r on?",
                "ts": "2026-07-14T11:07:12",
                "namespace": "alice",
            }
        ],
        "results": [],
    },
}


def _tokens(obj: object) -> int:
    return len(json.dumps(obj, default=str)) // 4


def test_lean_keeps_the_answer_and_drops_the_ranking() -> None:
    out = lean(PAYLOAD)
    hits = out["memory"]["results"]
    assert [h["memory_key"] for h in hits] == ["fav_place", "chunk:c955:0"]
    first = hits[0]
    assert first["content_excerpt"].startswith('"mountain"')
    assert first["similarity"] == 0.577 and first["created_at"] == "2026-09-18 18:42:33"
    assert first["source_class"] == "asserted" and first["namespace"] == "alice"
    assert first["version"] == 1789756953983417900, "a write receipt clients read back"
    # ranking, accounting and the reader paragraph are gone at every depth
    for k in DIAGNOSTIC_KEYS:
        assert k not in out["memory"], k
        assert k not in first, k
    # empties are gone; meaningful empties (result lists) stay
    for k in ("temporal_anchor", "valid_at", "via_path", "entities", "user_id", "next", "speaker"):
        assert k not in first, k
    assert out["knowledge"]["results"] == [] and out["profile"]["results"] == []
    # what a reader still needs to trust the answer
    assert out["memory"]["retrieval_confidence"] == 0.597
    assert out["memory"]["next_page"].startswith("page/")
    assert out["memory"]["abstain"] is False
    assert out["conversations"]["hits"][0]["content_excerpt"] == "which location u r on?"


def test_lean_cuts_the_payload_by_more_than_half() -> None:
    before, after = _tokens(PAYLOAD), _tokens(lean(PAYLOAD))
    assert after < before / 2, (before, after)


def test_explain_keeps_everything() -> None:
    assert lean_result(PAYLOAD, {"query": "x", "explain": True}) is PAYLOAD
    assert lean_result(PAYLOAD, None) == lean(PAYLOAD)
    assert lean_result(["not", "a", "dict"], {}) == ["not", "a", "dict"]


def test_lean_never_changes_a_code_answer_the_hooks_read() -> None:
    # The plugin's capture hook reads results[].path / definitions[].path
    # (codeResponsePaths); a code answer keeps those and its bodies.
    code = {
        "definitions": [
            {
                "unit_id": "84ad",
                "path": "src/a.cpp",
                "start_line": 2635,
                "end_line": 2720,
                "symbol": "DB::X::f",
                "kind": "symbol",
                "signature": "",
                "body": "void f()",
                "elided": False,
                "body_hash": "661556b2",
                "description": "",
                "tier": "scip",
                "score": 0,
            }
        ],
        "ambiguous": False,
        "candidates": [],
        "tokens_returned": 126,
        "trace_run_id": "7d8d080d",
        "uses": [],
        "uses_truncated": False,
    }
    out = lean(code)
    d = out["definitions"][0]
    assert d["path"] == "src/a.cpp" and d["body"] == "void f()" and d["start_line"] == 2635
    assert "body_hash" not in d
    assert out["trace_run_id"] == "7d8d080d", "the capture hook stashes it with the paths"
    assert out["candidates"] == [] and out["ambiguous"] is False


def test_both_return_sites_go_through_the_trim() -> None:
    body = inspect.getsource(mcp_call).split("async def _call_tool(", 1)[1]
    body += inspect.getsource(mcp_outcome)
    assert "mcp_op_result(" in body, "success return (lean_result inside)"
    assert "lean_result(" in body, "idempotent replay"
    assert re.search(
        r"lean_search_result\(\s*payload,\s*arguments,\s*namespace,\s*why_note,?\s*\)",
        inspect.getsource(mcp_tools),
    )


def test_a_definition_never_carries_the_compiler_tiers_placeholder_signature() -> None:
    from server.code.domain.units import readable_signature, unit_from_symbol

    assert readable_signature("(scip:49f6e7a06ebc5aa8)") == ""
    assert readable_signature("()") == "()"
    assert readable_signature("(const String & query_id, size_t max_queries) const") == (
        "(const String & query_id, size_t max_queries) const"
    )
    assert readable_signature(None) == ""
    row = {
        "unit_id": "84ad",
        "path": "src/Storages/StorageSharedMergeTree.cpp",
        "start_line": 2635,
        "end_line": 2720,
        "qualified_name": "DB::StorageSharedMergeTree::mergeSelectingTask",
        "kind": "function",
        "signature": "(scip:49f6e7a06ebc5aa8)",
        "tier": "scip",
    }
    assert unit_from_symbol(row).signature == ""


def test_loom_search_hits_drop_the_metadata_the_model_never_acts_on() -> None:
    """On a three-row answer the key, excerpt, type, rank and dates stay; the
    scores' twin, the write receipt and the always-active status go; the home
    namespace is implied; zero counters vanish."""
    from server.transport.lean_results import lean_search_result

    out = lean_search_result(copy.deepcopy(PAYLOAD), {"query": "favorite place"}, "alice")
    hit = out["memory"]["results"][0]
    assert (
        hit["memory_key"] == "fav_place"
        and hit["content_excerpt"]
        and hit["memory_type"] == "semantic"
    )
    assert "rank_score" in hit and "created_at" in hit, (
        "no valid_at on this row: created_at is the only date"
    )
    for gone in ("similarity", "status", "source_class", "version", "namespace"):
        assert gone not in hit, gone
    for k in ("bodies_returned", "bodies_elided", "tokens_returned"):
        assert k not in out["memory"], k
    # A foreign row keeps its namespace; a dated row drops created_at.
    p = copy.deepcopy(PAYLOAD)
    p["memory"]["results"][0]["namespace"] = "core-tracker"
    p["memory"]["results"][0]["valid_at"] = "2026-09-18 18:43:00"
    h2 = lean_search_result(p, {}, "alice")["memory"]["results"][0]
    assert h2["namespace"] == "core-tracker" and "created_at" not in h2 and h2["valid_at"]
    assert lean_search_result(PAYLOAD, {"explain": True}, "alice") is PAYLOAD
