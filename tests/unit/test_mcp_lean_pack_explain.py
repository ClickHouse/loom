"""A context.pack over MCP ships its bundle once, and
loom_search explain=true is the lean answer plus one `why` per hit."""

from __future__ import annotations

import copy
import json
from types import SimpleNamespace

from server.memory.domain.context_layout import _procedure_line
from server.transport.lean_results import lean_pack, lean_search_result, mcp_op_result
from server.transport.mcp_tools import why_note
from tests.unit.test_mcp_lean_results import PAYLOAD as SEARCH

_MEM = '- [memory:alice/fav_place source=fact] "mountain"'
_MENTOR = "Loom mentor — facts_graph_unused: walk the graph."
# The shape context.pack returns for a task description, cut down: every section's text rides three times.
PACK = {
    "rendered": f"<mentor>\n{_MENTOR}\n</mentor>\n\n<memories>\n{_MEM}\n</memories>",
    "sections": {"mentor": _MENTOR, "memories": _MEM},
    "blocks": [
        {
            "label": "mentor",
            "section": "mentor",
            "rendered": _MENTOR,
            "tokens_estimate": 20,
            "priority": 3,
            "truncated": False,
            "source_count": 1,
        },
        {
            "label": "recall.memories",
            "section": "memories",
            "rendered": _MEM,
            "tokens_estimate": 30,
            "citations": ["memory:alice/fav_place", "memory:alice/fav_place"],
            "priority": 5,
        },
    ],
    "section_meta": {
        "memories": {
            "source_count": 1,
            "abstain": False,
            "retrieval_risk": "high",
            "retrieval_confidence": 0.47,
            "fill_ratio": 0.7,
        }
    },
    "section_count": 2,
    "truncated": False,
    "token_budget": 2000,
    "mode": "task",
    "tokens_estimate": 50,
    "fill_ratio": 0.03,
    "stable_sections": ["mentor"],
    "stable_prefix_chars": 321,
    "stable_hash": "d93a" * 16,
    "procedure_keys": [],
}


def _tokens(obj: object) -> int:
    return len(json.dumps(obj, default=str)) // 4


def test_a_pack_ships_its_bundle_once() -> None:
    out = lean_pack(copy.deepcopy(PACK))
    assert out == {
        "rendered": PACK["rendered"],
        "citations": ["memory:alice/fav_place"],
        "retrieval_risk": "high",
    }
    assert _tokens(out) * 3 < _tokens(PACK), "the three copies collapse to one"


def test_a_truncated_or_abstaining_pack_says_so() -> None:
    p = copy.deepcopy(PACK)
    p["truncated"] = True
    p["section_meta"]["memories"]["abstain"] = True
    out = lean_pack(p)
    assert out["truncated"] is True and out["abstain"] is True


def test_the_mentor_hint_is_not_repeated_when_the_pack_renders_it() -> None:
    hint = ("facts_graph_unused", _MENTOR)
    out = mcp_op_result("context.pack", copy.deepcopy(PACK), {}, hint)
    assert "hint" not in out and "sections" not in out and "blocks" not in out
    other = mcp_op_result("knowledge.get", {"found": True, "content": "x"}, {}, hint)
    assert other["hint"] == _MENTOR and other["hint_code"] == "facts_graph_unused"


def test_another_ops_result_is_only_leaned() -> None:
    out = mcp_op_result("memory.get", {"found": True, "value": "v", "session_id": "-"}, {}, None)
    assert out == {"found": True, "value": "v"}


def test_explain_is_the_lean_answer_plus_a_why_per_hit() -> None:
    lean_out = lean_search_result(copy.deepcopy(SEARCH), {"query": "q"}, "alice", why_note)
    out = lean_search_result(
        copy.deepcopy(SEARCH), {"query": "q", "explain": True}, "alice", why_note
    )
    hits = out["memory"]["results"]
    assert hits[0]["why"].startswith("fact hit from namespace alice")
    assert "semantic+0.58" in hits[0]["why"] and "rank_score_raw" not in hits[0]["why"]
    assert hits[1]["why"].startswith("chunk hit")
    for gone in (
        "reader_instructions",
        "reader_block",
        "retrieval_work",
        "retrieval_cost_estimate",
    ):
        assert gone not in out["memory"], gone
    assert "score_components" not in hits[0] and "explanation" not in hits[0]
    stripped = copy.deepcopy(out)
    for h in stripped["memory"]["results"]:
        h.pop("why")
    assert stripped == lean_out, "explain adds the notes and nothing else"
    assert _tokens(out) * 4 < _tokens(SEARCH)


def _proc(situation: str, steps: list[str]) -> SimpleNamespace:
    return SimpleNamespace(
        namespace="clickgap-public", name="fr_9aac", situation=situation, steps=steps
    )


def test_a_one_sentence_procedure_prints_its_sentence_once() -> None:
    recipe = "[FIX RECIPE] Check: Style check | Fix: renumber the test"
    line = _procedure_line(_proc(recipe, [recipe]), "", recipe, "")
    assert line == f"- [clickgap-public/fr_9aac] {recipe}"
    two = _procedure_line(
        _proc("deploy fails", ["drain", "swap"]), " (unproven)", "drain; swap", ""
    )
    assert two == "- [clickgap-public/fr_9aac] (unproven) when deploy fails: drain; swap"
