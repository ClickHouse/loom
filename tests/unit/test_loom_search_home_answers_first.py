"""When a fact from the caller's home namespace leads the
memory section, loom_search withholds foreign-namespace memory/knowledge
rows and the conversation section."""

from __future__ import annotations

import copy
import inspect
from typing import Any

import pytest

from server.memory.app.loom_search.answer_first import (
    answer_first,
    leading_home_fact,
    withheld_note,
)
from server.transport import mcp_tools


@pytest.fixture(autouse=True)
def _without_precision_guards(monkeypatch: pytest.MonkeyPatch) -> None:
    """These fixtures isolate the home-first withholding: the precision floor
    and chunk cap are off (tests/unit/test_fact_quality.py covers them)."""
    from server.memory.app.loom_search import answer_first as af
    from server.settings import settings

    monkeypatch.setattr(settings, "mcp_loom_search_precision_floor", 0.0)
    monkeypatch.setattr(af, "MAX_CHUNKS_BEHIND_FACT", 10**6)


def _laptop_payload() -> dict[str, Any]:
    """A 'what i test?' answer: one home fact ahead of foreign chunks and documents."""
    return {
        "memory": {
            "results": [
                {
                    "memory_key": "what_i_test",
                    "source": "fact",
                    "namespace": "jdoe",
                    "similarity": 0.401,
                    "content_excerpt": '"random stuff" (#what_i_test)',
                },
                {
                    "memory_key": "chunk:memory.set:sre/a:1",
                    "source": "chunk",
                    "namespace": "team-alerts",
                    "similarity": 0.3095,
                },
                {
                    "memory_key": "chunk:memory.set:sre/b:16",
                    "source": "chunk",
                    "namespace": "team-alerts",
                    "similarity": 0.3037,
                },
                {
                    "memory_key": "chunk:memory.set:sre/c:13",
                    "source": "chunk",
                    "namespace": "team-alerts",
                    "similarity": 0.2965,
                },
                {
                    "memory_key": "chunk:memory.set:sre/d:1",
                    "source": "chunk",
                    "namespace": "team-alerts",
                    "similarity": 0.2727,
                },
            ],
            "retrieval_confidence": 0.507,
        },
        "knowledge": {
            "results": [
                {
                    "source_uri": "loom://clickhouse/team-alerts/x",
                    "namespace": "team-alerts",
                    "similarity": 0.4018,
                },
                {
                    "source_uri": "loom://clickhouse/team-alerts/y",
                    "namespace": "team-alerts",
                    "similarity": 0.3527,
                },
                {
                    "source_uri": "loom://clickhouse/team-alerts/z",
                    "namespace": "team-alerts",
                    "similarity": 0.3472,
                },
            ],
            "embedding_model": "m",
        },
        "profile": {"results": []},
        "conversations": {
            "results": [
                {
                    "thread_id": "81f4",
                    "msg_id": "1f0f",
                    "role": "user",
                    "content_excerpt": "can you test ...",
                    "ts": "2026-08-08T15:33:19",
                    "namespace": "jdoe",
                },
                {
                    "thread_id": "d68f",
                    "msg_id": "998a",
                    "role": "user",
                    "content_excerpt": "role_arn e2e test ...",
                    "ts": "2026-08-04T17:49:37",
                    "namespace": "jdoe",
                },
            ]
        },
    }


def test_the_home_fact_answers_and_the_widening_is_withheld() -> None:
    out = answer_first(_laptop_payload(), "jdoe")
    assert [h["memory_key"] for h in out["memory"]["results"]] == ["what_i_test"]
    assert out["knowledge"]["results"] == [] and out["knowledge"]["embedding_model"] == "m"
    assert out["conversations"]["results"] == []
    assert out["withheld"] == {
        "memory": 4,
        "knowledge": 3,
        "conversations": 2,
        "reason": "home_fact_leads",
        "lead": "what_i_test",
    }
    note = withheld_note(out)
    assert (
        "Withheld 4 memory, 3 knowledge, 2 conversations hit(s)" in note and "[what_i_test]" in note
    )


def test_a_foreign_row_within_the_margin_and_home_rows_stay() -> None:
    p = _laptop_payload()
    p["memory"]["results"][1]["similarity"] = 0.39  # within 0.03 of the 0.401 lead
    p["memory"]["results"].append(
        {
            "memory_key": "user_test_focus",
            "source": "fact",
            "namespace": "jdoe",
            "similarity": 0.30,
        }
    )  # within HOME_MARGIN
    p["knowledge"]["results"].append(
        {"source_uri": "loom://clickhouse/jdoe/doc", "namespace": "jdoe", "similarity": 0.31}
    )
    out = answer_first(p, "jdoe")
    assert [h["memory_key"] for h in out["memory"]["results"]] == [
        "what_i_test",
        "chunk:memory.set:sre/a:1",
        "user_test_focus",
    ]
    assert [k["source_uri"] for k in out["knowledge"]["results"]] == ["loom://clickhouse/jdoe/doc"]
    assert out["withheld"]["memory"] == 3 and out["withheld"]["knowledge"] == 3


def test_no_home_fact_leading_means_nothing_is_withheld() -> None:
    chunk_leads = _laptop_payload()
    chunk_leads["memory"]["results"].insert(
        0, {"memory_key": "chunk:z:0", "source": "chunk", "namespace": "jdoe", "similarity": 0.5}
    )
    assert "withheld" not in answer_first(copy.deepcopy(chunk_leads), "jdoe")
    foreign_fact_leads = _laptop_payload()
    foreign_fact_leads["memory"]["results"][0]["namespace"] = "core-tracker"
    assert "withheld" not in answer_first(copy.deepcopy(foreign_fact_leads), "jdoe")
    assert (
        leading_home_fact([], "jdoe") is None
        and leading_home_fact(_laptop_payload()["memory"]["results"], "") is None
    )
    empty = {
        "memory": {"results": []},
        "knowledge": {"results": []},
        "profile": {"results": []},
        "conversations": {"results": []},
    }
    assert answer_first(copy.deepcopy(empty), "jdoe") == empty
    assert withheld_note(empty) == ""


def test_explain_keeps_everything() -> None:
    full = _laptop_payload()
    assert answer_first(copy.deepcopy(full), "jdoe", explain=True) == full


def test_the_handler_applies_the_rule_before_it_counts_the_sections() -> None:
    from server.memory.app.loom_search import fanout

    fan = inspect.getsource(fanout.search_sections)
    assert fan.rstrip().endswith("return payload")
    assert "payload = answer_first(payload, namespace, explain=explain, moved=moved)" in fan
    # The MCP text counts the sections of the payload the handler returned.
    src = inspect.getsource(mcp_tools.search_answer)
    assert src.index('mem_hits = payload["memory"]') < src.index("withheld_note(payload)")


def test_home_rows_far_below_the_lead_are_withheld_too() -> None:
    """A coffee query: two favourite-place echoes
    and a code-answer chunk from the home namespace, all ≥0.18 below the lead."""
    p = {
        "memory": {
            "results": [
                {
                    "memory_key": "coffee_type",
                    "source": "fact",
                    "namespace": "jdoe",
                    "similarity": 0.566,
                },
                {
                    "memory_key": "chunk:df59:0",
                    "source": "chunk",
                    "namespace": "jdoe",
                    "similarity": 0.577,
                },
                {
                    "memory_key": "chunk:0b11:0",
                    "source": "chunk",
                    "namespace": "jdoe",
                    "similarity": 0.374,
                },
                {
                    "memory_key": "user_favorite_place",
                    "source": "fact",
                    "namespace": "jdoe",
                    "similarity": 0.381,
                },
                {
                    "memory_key": "user_favorite_place",
                    "source": "fact",
                    "namespace": "jdoe",
                    "similarity": 0.341,
                },
                {
                    "memory_key": "coffee_notes",
                    "source": "fact",
                    "namespace": "jdoe",
                    "similarity": 0.43,
                },
            ]
        },
        "knowledge": {"results": []},
        "profile": {"results": []},
        "conversations": {"results": []},
    }
    out = answer_first(p, "jdoe")
    assert [h["memory_key"] for h in out["memory"]["results"]] == [
        "coffee_type",
        "chunk:df59:0",
        "coffee_notes",
    ]
    assert out["withheld"] == {"memory": 3, "reason": "home_fact_leads", "lead": "coffee_type"}
    # The lead itself is never withheld, whatever its similarity field says.
    lead_only = {
        "memory": {"results": [{"memory_key": "k", "source": "fact", "namespace": "jdoe"}]},
        "knowledge": {"results": []},
        "profile": {"results": []},
        "conversations": {"results": []},
    }
    assert [h["memory_key"] for h in answer_first(lead_only, "jdoe")["memory"]["results"]] == ["k"]


def _where_do_i_live_pool() -> dict[str, Any]:
    """A served pool, in rank order as served."""
    return {
        "memory": {
            "results": [
                {
                    "memory_key": "chunk:65187490:0",
                    "source": "chunk",
                    "namespace": "jdoe",
                    "similarity": 0.452,
                    "rank_score": 0.532,
                    "content_excerpt": "[assistant @ 08:53] Your favorite place is the mountain, saved in Loom as `fav_place`. [user @ 08:53] and where do i live? [assistant @ 08:53] I'll check Loom",
                },
                {
                    "memory_key": "where_i_live",
                    "source": "fact",
                    "namespace": "jdoe",
                    "similarity": 0.470,
                    "rank_score": 0.486,
                    "content_excerpt": '"xyz" (#where_i_live)',
                },
                {
                    "memory_key": "chunk:a3f0:0",
                    "source": "chunk",
                    "namespace": "jdoe",
                    "similarity": 0.499,
                    "rank_score": 0.485,
                    "content_excerpt": "[user @ 10:34] and where do i live?\n[assistant @ 10:34] I'll check Loom for your saved home location.\n[assistant @ 10:34] You live in Ujjain, saved in Loom as `where_i_live`",
                },
                {
                    "memory_key": "user_favorite_place",
                    "source": "fact",
                    "namespace": "jdoe",
                    "similarity": 0.432,
                    "rank_score": 0.418,
                    "content_excerpt": "User's favorite place is the mountain.",
                },
                {
                    "memory_key": "user_favorite_place",
                    "source": "fact",
                    "namespace": "jdoe",
                    "similarity": 0.406,
                    "rank_score": 0.392,
                    "content_excerpt": "User's favorite place is the mountain, saved in Loom as `fav_place`.",
                },
            ]
        },
        "knowledge": {"results": []},
        "profile": {"results": []},
        "conversations": {"results": []},
    }


def test_a_home_fact_within_the_lead_margin_takes_the_lead_and_recall_chunks_are_withheld() -> None:
    """On the served pool: the fact leads, both lookup chunks go,
    the two favourite-place facts stay (within the home margin)."""
    out = answer_first(_where_do_i_live_pool(), "jdoe")
    keys = [h["memory_key"] for h in out["memory"]["results"]]
    assert keys == ["where_i_live", "user_favorite_place", "user_favorite_place"], keys
    assert out["withheld"] == {"memory": 2, "reason": "home_fact_leads", "lead": "where_i_live"}
    # The coffee pool of the same morning: the fact already led; the two
    # lookup chunks (one from Codex) are withheld outright.
    coffee = {
        "memory": {
            "results": [
                {
                    "memory_key": "coffee_type",
                    "source": "fact",
                    "namespace": "jdoe",
                    "similarity": 0.566,
                    "rank_score": 0.577,
                    "content_excerpt": '"espresso" (#coffee_type)',
                },
                {
                    "memory_key": "chunk:df59:0",
                    "source": "chunk",
                    "namespace": "jdoe",
                    "similarity": 0.577,
                    "rank_score": 0.576,
                    "content_excerpt": "[user @ 13:24] whats my coffee type?\n[assistant @ 13:24] I'll check Loom for a saved coffee preference.",
                },
                {
                    "memory_key": "chunk:bfc0:1",
                    "source": "chunk",
                    "namespace": "jdoe",
                    "similarity": 0.482,
                    "rank_score": 0.562,
                    "content_excerpt": "[assistant @ 17:12] Espresso. It is saved in Loom under the key coffee_type.",
                },
            ]
        },
        "knowledge": {"results": []},
        "profile": {"results": []},
        "conversations": {"results": []},
    }
    out2 = answer_first(coffee, "jdoe")
    assert [h["memory_key"] for h in out2["memory"]["results"]] == ["coffee_type"]
    assert out2["withheld"]["memory"] == 2


def test_the_lead_margin_does_not_promote_a_fact_far_below_a_leading_chunk() -> None:
    p = _where_do_i_live_pool()
    p["memory"]["results"][0]["similarity"] = 0.60  # the chunk really is the closer text
    p["memory"]["results"][0]["content_excerpt"] = (
        "[user] tell me about the mountain trip [assistant] you went in June"
    )
    assert "withheld" not in answer_first(p, "jdoe"), (
        "no home fact within 0.03 of the lead: nothing withheld"
    )
    assert leading_home_fact(p["memory"]["results"], "jdoe") is None


def test_a_fact_the_search_moved_for_stale_code_is_never_lifted_to_lead() -> None:
    p = _where_do_i_live_pool()
    assert leading_home_fact(p["memory"]["results"], "jdoe")["memory_key"] == "where_i_live"
    hits = p["memory"]["results"]
    # the rule reads the search's moves, not `code_state`: one its search did not move
    # (held, or the move off) still leads
    hits[1]["code_state"] = "changed"
    assert leading_home_fact(hits, "jdoe")["memory_key"] == "where_i_live"
    # the search moved it down: it stays where the search put it, and the next one leads
    lead = leading_home_fact(hits, "jdoe", moved=frozenset({id(hits[1])}))
    assert lead is not None and lead["memory_key"] == "user_favorite_place"
