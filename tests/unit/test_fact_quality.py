"""Loom_search and context.pack return real facts, not junk.

The junk classes (`server/memory/domain/fact_quality.py`) on labeled rows and on
held-out real rows, the pool filter (restatements, question
echoes, facts ahead of their transcript), the pool widening, and the MCP
precision floor / chunk cap in `answer_first`.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from server.memory.app.loom_search import answer_first as af
from server.memory.domain import fact_quality as fq

# ── junk_class: one labeled row per class ─────────────────────────────────────


@pytest.mark.parametrize(
    ("key", "value", "kw", "want"),
    [
        (
            "event:5fffc9e422629734",
            "Step 2/5 · After Bash `command=sed -n '1001,2000p' MergeTreeData.cpp`",
            {"tags": ["event", "from_observation", "tool:Bash"]},
            "transcript",
        ),
        ("hint:851d543809cef5c5", "Useful Commands for Analyzing MergeTreeData.cpp", {}, "hint"),
        ("otel_fix_probe", "phase1 recheck 150930 — existing key => updated", {}, "probe"),
        (
            "otel_outcome_probe_b",
            "Second write to the same key with different content, should count as updated.",
            {},
            "probe",
        ),
        (
            "bulk_probe_new_142554",
            "A brand new key that has never been written before.",
            {},
            "probe",
        ),
        ("what_i_test", "random stuff", {}, "probe"),
        (
            "reflection:gm-r1",
            '{"run_id": "gm-r1", "goal": "review the parser change"}',
            {"memory_type": "reflection"},
            "procedure",
        ),
        (
            "procedure_read_and_analyze_mergetreedata_cpp",
            '{"graph": {"edges": []}}',
            {"memory_type": "procedural"},
            "procedure",
        ),
        (
            "user_loom_status",
            "User ran the client-side doctor for Loom and found every line passes.",
            {},
            "narration",
        ),
        (
            "user_merge_tree_data_cpp_summary",
            "User read and summarized the file src/Storages/MergeTree/MergeTreeData.cpp.",
            {},
            "narration",
        ),
        (
            "user_skill_base_directory",
            "User's base directory for the skill is /Users/l/.claude/plugins/cache/loom/skills/x.",
            {},
            "code_location",
        ),
        (
            "merge_tree_data_methods_count",
            "There are roughly 290 methods in about 14.8k lines.",
            {},
            "code_location",
        ),
        (
            "avery_loom_code_plugin",
            "Avery is using the loom-code plugin to answer questions about a loom-indexed codebase.",
            {},
            "tool_doc",
        ),
        (
            "user_memory_set_failure",
            "User's one failed memory.set this morning was due to the namespace bug.",
            {},
            "session_status",
        ),
        (
            "reporting_window",
            "The reporting window for this run is from 2026-09-14 to 2026-09-21 (UTC).",
            {},
            "session_status",
        ),
    ],
)
def test_junk_class_names_each_labeled_class(key, value, kw, want):
    assert fq.junk_class(key, value, **kw) == want


@pytest.mark.parametrize(
    ("key", "value", "kw"),
    [
        # real rows from a coding namespace
        ("coffee_type", '"espresso"', {}),
        ("fav_place", "mountain", {}),
        (
            "deploy_note_1",
            "The deployment pipeline builds the Docker image and restarts the loom server container.",
            {},
        ),
        # an ops probe row (adjudicated real): a probe key with a long value
        (
            "ops/probe/memory-set-after-incident-2026-09-20",
            "After the 09-20 memory_embeddings outage a memory.set probe landed a row in 1.2 s; "
            "merges resumed once the text skip index was excluded from materialization on merge.",
            {},
        ),
        # a mined fix recipe is prose knowledge, not a session procedure
        ("procedure_fr_3fa9c2", "Rebuild the image, then restart", {"memory_type": "procedural"}),
        # held-out rows from other namespaces: they cite path:line and start with a finding
        (
            "finding_h_pr113877_001",
            "Codec-only MODIFY COLUMN validated against pre-ALTER type; see "
            "src/Storages/AlterCommands.cpp:1402",
            {},
        ),
        (
            "lesson_a68666d234fe7f49",
            "COVERAGE_GAP: `ColumnDecimal::hasOnlyTypeDefaults` decides column omission with no test.",
            {},
        ),
        (
            "sre/pr-review/deploy-app/41788",
            "PR 41788 uses a 45-minute Hydra provisioning recommendation window and always "
            "registers provisioning-max.",
            {},
        ),
    ],
)
def test_junk_class_keeps_real_rows(key, value, kw):
    assert fq.junk_class(key, value, **kw) is None


# ── the pool filter ──────────────────────────────────────────────────────────


def _hit(key, text, *, source="fact", sim=0.5, rank=0.5):
    return SimpleNamespace(
        memory_key=key,
        content_excerpt=text,
        source=source,
        similarity=sim,
        rank_score=rank,
        score_components={},
        tags=None,
        memory_type="semantic",
    )


def test_content_words_split_digits_from_letters():
    assert fq.content_words("User's editor font size is 13pt") == fq.content_words(
        "editor font size 13 pt"
    )


def test_restatement_of_a_higher_ranked_fact_leaves():
    hits = [
        _hit("user_coffee_type", "User's coffee type is espresso", rank=0.9),
        _hit("coffee_type", "espresso", rank=0.8),
        _hit("fav_place", "mountain", rank=0.7),
    ]
    kept, dropped = fq.drop_junk_and_restatements(hits)
    assert [h.memory_key for h in kept] == ["user_coffee_type", "fav_place"]
    assert dropped["restatement"] == 1


def test_junk_facts_and_question_echo_chunks_leave():
    hits = [
        _hit("what_i_test", "random stuff", rank=0.95),
        _hit("chunk:1", "[user @ 2026-09-24] which coffee do I drink?", source="chunk", rank=0.9),
        _hit("user_coffee_type", "User's coffee type is espresso", rank=0.6),
    ]
    kept, dropped = fq.drop_junk_and_restatements(hits)
    assert [h.memory_key for h in kept] == ["user_coffee_type"]
    assert dropped["probe"] == 1 and dropped["question_echo"] == 1


def test_a_chunk_with_the_answer_is_not_a_question_echo():
    text = "[user @ 09-24] which coffee? [assistant @ 09-24] espresso, double"
    assert fq.chunk_junk_class(text) is None


def test_digest_narration_summary_leaves():
    s = _hit(
        "summary:f2c46ffb",
        "On 2026-09-20, the user edited a file located at /Users/l/.claude/projects/x.md",
        source="summary",
    )
    kept, dropped = fq.drop_junk_and_restatements([s])
    assert kept == [] and sum(dropped.values()) == 1


def test_facts_first_lifts_a_close_fact_over_the_transcript():
    chunk = _hit(
        "chunk:1", "we talked about the renameTempPart race", source="chunk", sim=0.60, rank=0.9
    )
    fact = _hit(
        "renametemppart_regression", "renameTempPart races with DROP PART", sim=0.52, rank=0.4
    )
    far = _hit("fav_place", "mountain", sim=0.30, rank=0.3)
    out = fq.facts_first([chunk, fact, far])
    assert out[0] is fact and fact.rank_score > chunk.rank_score
    assert "fact_first_lift" in fact.score_components
    assert far.rank_score == 0.3  # outside the margin: not lifted


def test_evidence_restating_a_ranked_fact_leaves():
    fact = _hit("user_coffee_type", "User's coffee type is espresso", rank=0.9)
    summ = _hit(
        "summary:1",
        "The user said their coffee type is espresso, daily",
        source="summary",
        rank=0.5,
    )
    other = _hit("chunk:2", "an unrelated build log line", source="chunk", rank=0.4)
    dropped: dict[str, int] = {}
    out = fq._drop_redundant_evidence([fact, summ, other], dropped)
    assert out == [fact, other] and dropped == {"restated_evidence": 1}


# ── pool widening ────────────────────────────────────────────────────────────


def test_quality_pool_widens_then_caps():
    from server.memory.app.search.plan import _quality_pool

    s = SimpleNamespace(memory_search_quality_pool_multiplier=3, memory_search_max_pool_top_k=150)
    on = SimpleNamespace(quality_filter=True, top_k=10)
    off = SimpleNamespace(quality_filter=False, top_k=10)
    assert _quality_pool(on, 30, s) == 90
    assert _quality_pool(off, 30, s) == 30
    assert _quality_pool(on, 90, s) == 150
    assert _quality_pool(SimpleNamespace(quality_filter=True, top_k=200), 600, s) == 200


def test_loom_search_and_context_pack_ask_for_the_filter():
    import inspect

    from server.memory.api.models_search import MemorySearchInput
    from server.memory.app import context_pack as context

    assert MemorySearchInput.model_fields["quality_filter"].default is False
    from server.memory.app.loom_search import fanout

    assert "quality_filter=True" in inspect.getsource(fanout)
    assert "quality_filter=True" in inspect.getsource(context)


# ── MCP: precision floor and chunk cap ───────────────────────────────────────


def _payload(*rows):
    return {"memory": {"results": [dict(r) for r in rows]}}


def test_precision_floor_is_the_calibrated_midpoint(monkeypatch):
    from server.memory.app import embedder_calibration as ec
    from server.settings import settings

    monkeypatch.setattr(settings, "mcp_loom_search_precision_floor", -1.0)
    monkeypatch.setattr(ec, "effective_noise_floor", lambda _d: 0.218)
    monkeypatch.setattr(ec, "effective_hyde_fallback_threshold", lambda _d: 0.517)
    assert af.precision_floor() == pytest.approx(0.3675)
    monkeypatch.setattr(settings, "mcp_loom_search_precision_floor", 0.3)
    assert af.precision_floor() == 0.3


def test_precision_floor_holds_foreign_rows_to_a_margin(monkeypatch):
    from server.settings import settings

    monkeypatch.setattr(settings, "mcp_loom_search_foreign_margin", 0.08)
    p = _payload(
        {"memory_key": "home_hi", "namespace": "z", "similarity": 0.40},
        {"memory_key": "home_lo", "namespace": "z", "similarity": 0.30},
        {"memory_key": "far_mid", "namespace": "sre", "similarity": 0.40},
        {"memory_key": "far_hi", "namespace": "sre", "similarity": 0.50},
        {"memory_key": "pinned", "namespace": "z"},
    )
    assert af.apply_precision_floor(p, "z", floor=0.37) == 2
    assert [h["memory_key"] for h in p["memory"]["results"]] == ["home_hi", "far_hi", "pinned"]


def test_precision_floor_zero_disables():
    p = _payload({"memory_key": "a", "similarity": 0.01})
    assert af.apply_precision_floor(p, "z", floor=0.0) == 0


def test_chunks_behind_a_home_fact_are_capped():
    p = _payload(
        {"memory_key": "fact", "namespace": "z", "source": "fact"},
        {"memory_key": "chunk:1", "namespace": "z", "source": "chunk"},
        {"memory_key": "chunk:2", "namespace": "z", "source": "chunk"},
        {"memory_key": "chunk:3", "namespace": "z", "source": "chunk"},
    )
    assert af.cap_chunks_behind_fact(p, "z") == 2
    assert [h["memory_key"] for h in p["memory"]["results"]] == ["fact", "chunk:1"]
    only_chunks = _payload(
        {"memory_key": "chunk:1", "source": "chunk"}, {"memory_key": "chunk:2", "source": "chunk"}
    )
    assert af.cap_chunks_behind_fact(only_chunks, "z") == 0


def test_chunks_behind_a_fact_the_search_ranked_down_are_not_capped():
    p = _payload(
        {"memory_key": "chunk:1", "namespace": "z", "source": "chunk"},
        {"memory_key": "chunk:2", "namespace": "z", "source": "chunk"},
        {"memory_key": "fact", "namespace": "z", "source": "fact"},
    )
    stale = frozenset({id(p["memory"]["results"][2])})
    assert af.cap_chunks_behind_fact(p, "z", stale) == 0


def test_answer_first_says_why_weak_hits_left(monkeypatch):
    from server.settings import settings

    monkeypatch.setattr(settings, "mcp_loom_search_precision_floor", 0.37)
    p = _payload({"memory_key": "weak", "namespace": "z", "similarity": 0.2, "source": "fact"})
    out = af.answer_first(p, "z")
    assert out["memory"]["results"] == []
    assert out["withheld"] == {"memory": 1, "reason": "below_precision_floor"}
    assert "too weak" in af.withheld_note(out)
    kept = af.answer_first(
        _payload({"memory_key": "weak", "namespace": "z", "similarity": 0.2}), "z", explain=True
    )
    assert len(kept["memory"]["results"]) == 1


# ── the cleanup's classifier (dry run by default; never deletes) ────────────


def test_cleanup_classes_junk_and_names_the_canonical_of_a_restatement():
    from server.memory.app.activity_trivia_cleanup import candidate_line, classify_junk

    rows = [
        {"memory_key": "coffee_type", "value": '"espresso"'},
        {"memory_key": "what_i_test", "value": "random stuff"},
        {"memory_key": "user_coffee_type", "value": "User's coffee type is Espresso."},
        {"memory_key": "fav_place", "value": "mountain"},
    ]
    out = {r["memory_key"]: r for r in classify_junk(rows)}
    assert set(out) == {"what_i_test", "user_coffee_type"}
    assert out["what_i_test"]["class"] == "probe"
    assert out["user_coffee_type"]["canonical"] == "coffee_type"
    assert candidate_line(out["user_coffee_type"]).startswith("[restatement of coffee_type] ")


def test_cleanup_apply_labels_with_the_class(monkeypatch):
    import asyncio

    from server.memory.app import activity_trivia_cleanup as atc
    from server.memory.app import apply_ops as memory_apply

    seen = []

    async def fake(inp):
        seen.append(inp)
        return type(
            "O", (), {"matched": len(inp.target.keys), "would_apply": len(inp.target.keys)}
        )()

    monkeypatch.setattr(memory_apply, "handle_memory_apply", fake)
    cands = [
        {"memory_key": "a", "agent": "x", "user_id": "-", "session_id": "-", "class": "probe"},
        {"memory_key": "b", "agent": "x", "user_id": "-", "session_id": "-", "class": "hint"},
    ]
    totals = asyncio.run(atc.apply("o", "ns", cands, action="label", **atc.junk_apply_kwargs(True)))
    assert totals["would_apply"] == 2 and totals["applied"] == 0
    assert all(i.dry_run for i in seen)
    assert sorted(tuple(i.label.tags) for i in seen) == [
        ("fact_quality_junk", "junk:hint"),
        ("fact_quality_junk", "junk:probe"),
    ]
    assert all(i.reason == "junk-class row, not a fact" for i in seen)


def test_actor_names_come_from_the_setting(monkeypatch: pytest.MonkeyPatch) -> None:
    from server.memory.domain import fact_quality as fq

    text = "Jane Doe read lines 1-200 of the file server/main.py."
    monkeypatch.setattr(fq.settings, "memory_quality_actor_names", "")
    assert fq.junk_class("k", text) is None
    monkeypatch.setattr(fq.settings, "memory_quality_actor_names", "jane,jane doe")
    assert fq.junk_class("k", text) == "narration"
    assert "jane" not in fq.content_words("Jane prefers dark mode")
