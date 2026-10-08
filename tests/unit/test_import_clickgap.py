"""Unit coverage for the ClickGap → Loom import mapping (server/cli/import_clickgap.py
pure helpers) and the consolidation polarity/kind guard (server/memory/app/ops).

These are the load-bearing, deterministic pieces of the migration: provenance
classification, the tag set that preserves what the flat schema can't (typed
entities, suppression polarity, human-attribution), idempotent ids, the
assertion+reasoning packing, and the guard that stops consolidation from
merging a suppression memory into a positive one.
"""

from __future__ import annotations

import json

import pytest

from server.cli.import_clickgap import (
    _CLICKGAP_TYPE_REMAP,
    _clickgap_classify_source,
    _clickgap_memory_id,
    _clickgap_memory_type,
    _clickgap_session_id,
    _clickgap_source_trace,
    _clickgap_tags,
    _clickgap_value_json,
)
from server.memory.app.consolidate import _consolidation_compatible
from server.memory.domain.memory_types import NON_SEARCHABLE_TYPES, is_searchable_type


def test_classify_source_pipeline_default() -> None:
    for src in ("analyst_summary", "consolidated", "issue_learning", "agent", "pattern_extractor"):
        cls, is_human = _clickgap_classify_source(src)
        assert cls == "pipeline"
        assert is_human is False


def test_classify_source_human_is_erasure_relevant() -> None:
    # Person-attributed rows must be flagged so erasure can target them by tag.
    for src in ("mention:amir", "operator", "maintainer_feedback", "maintainer_pushback_ingest"):
        cls, is_human = _clickgap_classify_source(src)
        assert is_human is True, src
        assert cls == "human"


def test_classify_source_session_reviewer_eval() -> None:
    assert _clickgap_classify_source("session_index:h_pr60669_001") == ("session", False)
    assert _clickgap_classify_source("reviewer_reject") == ("reviewer", False)
    assert _clickgap_classify_source("review:pr101307")[0] == "eval"
    assert _clickgap_classify_source("prompt_regression:ci.x:AGENT_BUG")[0] == "eval"


def test_source_trace_keeps_full_source_after_class() -> None:
    assert _clickgap_source_trace("analyst_summary") == "clickgap://pipeline/analyst_summary"
    assert _clickgap_source_trace("mention:amir") == "clickgap://human/mention:amir"
    assert _clickgap_source_trace("") == "clickgap://pipeline/unknown"


def test_session_id_only_for_session_scoped_sources() -> None:
    assert _clickgap_session_id("session_index:fix_pr93876_101708") == "fix_pr93876_101708"
    assert _clickgap_session_id("analyst_summary") == "-"
    assert _clickgap_session_id("") == "-"


def test_tags_false_positive_is_suppression() -> None:
    tags = _clickgap_tags(kind="false_positive", source="reviewer_reject", typed_entities=[])
    assert "polarity:negative" in tags
    assert "source:reviewer" in tags


def test_tags_non_fp_has_no_polarity() -> None:
    tags = _clickgap_tags(kind="observation", source="agent", typed_entities=[])
    assert "polarity:negative" not in tags


def test_tags_carry_kind() -> None:
    # The kind tag is what makes multi-kind recall (kill-list filtering on
    # false_positive OR maintainer_preference) possible — memory.search takes
    # a single memory_type — and is the only surviving copy of the kind once
    # `summary` is remapped to episodic.
    assert "kind:false_positive" in _clickgap_tags(
        kind="false_positive", source="reviewer_reject", typed_entities=[]
    )
    assert "kind:summary" in _clickgap_tags(
        kind="summary", source="analyst_summary", typed_entities=[]
    )


def test_tags_human_source_marks_pii() -> None:
    tags = _clickgap_tags(kind="maintainer_preference", source="mention:amir", typed_entities=[])
    assert "pii:human-attributed" in tags


def test_tags_preserve_typed_entities_and_dedupe() -> None:
    tags = _clickgap_tags(
        kind="principle",
        source="agent",
        typed_entities=[
            ("src/Foo.cpp", "file_path"),
            ("BUG-9", "error_code"),
            ("src/Foo.cpp", "file_path"),
        ],  # duplicate
    )
    assert "entity:file_path:src/Foo.cpp" in tags
    assert "entity:error_code:BUG-9" in tags
    # Untyped entity falls back to a generic prefix, never dropped.
    fallback = _clickgap_tags(kind="observation", source="agent", typed_entities=[("x", "")])
    assert "entity:entity:x" in fallback
    # No duplicate entity tags.
    assert len(tags) == len(set(tags))


def test_memory_id_deterministic_and_unique() -> None:
    a = _clickgap_memory_id("org", "clickgap", 7)
    assert a == _clickgap_memory_id("org", "clickgap", 7)  # idempotent
    assert a != _clickgap_memory_id("org", "clickgap", 8)  # per-lesson
    assert a != _clickgap_memory_id("org", "other", 7)  # per-namespace
    # Valid UUID string.
    import uuid

    assert str(uuid.UUID(a)) == a


def test_value_json_packs_assertion_and_reasoning() -> None:
    packed = _clickgap_value_json("the assertion", "the justification")
    obj = json.loads(packed)
    # get_by_key returns value_json AS value when populated, so the assertion
    # must be recoverable from value_json.text — never lost.
    assert obj["text"] == "the assertion"
    assert obj["reasoning"] == "the justification"


def test_value_json_handles_missing_reasoning() -> None:
    # Most ClickGap lessons have NO reasoning (only ~344/1851). Empty must be
    # valid JSON and keep the assertion intact.
    obj = json.loads(_clickgap_value_json("claim", None))
    assert obj["text"] == "claim"
    assert obj["reasoning"] == ""


# ---- kind → memory_type remap + non-searchable guard --------------------


def test_summary_maps_to_non_searchable_episodic() -> None:
    # ClickGap excludes [SUMMARY] PR-snapshot lessons from recall by default;
    # episodic reproduces that — summaries stay reachable by get/list but
    # never rank in memory.search.
    assert _clickgap_memory_type("summary") == "episodic"
    assert not is_searchable_type(_clickgap_memory_type("summary"))


def test_non_summary_kinds_pass_through_searchable() -> None:
    # The other 9 kinds ride through 1:1 and stay searchable.
    for k in (
        "observation",
        "decision",
        "principle",
        "false_positive",
        "fix_recipe",
        "test_pattern",
        "code_pattern",
        "maintainer_preference",
        "cidb_insight",
    ):
        assert _clickgap_memory_type(k) == k
        assert is_searchable_type(_clickgap_memory_type(k)), k


def test_empty_kind_defaults_to_semantic() -> None:
    assert _clickgap_memory_type("") == "semantic"


def test_remap_only_targets_summary() -> None:
    assert _CLICKGAP_TYPE_REMAP == {"summary": "episodic"}


def test_unintended_non_searchable_collision_raises() -> None:
    # A future kind that happens to equal a non-searchable Loom type (and is
    # NOT in the explicit remap) must fail loudly rather than vanish from
    # search. `profile` is non-searchable and not remapped.
    assert "profile" in NON_SEARCHABLE_TYPES
    assert "profile" not in _CLICKGAP_TYPE_REMAP
    with pytest.raises(ValueError, match="silently excluded"):
        _clickgap_memory_type("profile")


# ---- consolidation guard ------------------------------------------------


def _row(memory_type: str, tags: list[str] | None = None) -> dict:
    return {"memory_type": memory_type, "tags": tags or []}


def test_consolidation_same_type_same_polarity_compatible() -> None:
    assert _consolidation_compatible(_row("observation"), _row("observation"))


def test_consolidation_never_merges_across_kind() -> None:
    # An observation must not collapse into a principle.
    assert not _consolidation_compatible(_row("observation"), _row("principle"))


def test_consolidation_never_merges_across_polarity() -> None:
    # A suppression (false_positive) must not merge into a positive peer even
    # when both share memory_type.
    pos = _row("false_positive")
    neg = _row("false_positive", ["polarity:negative"])
    assert not _consolidation_compatible(pos, neg)
    # Two suppressions of the same type MAY merge.
    assert _consolidation_compatible(neg, _row("false_positive", ["polarity:negative"]))


_LESSON = {
    "id": 7,
    "text": "prefer EXPLAIN before OPTIMIZE",
    "kind": "semantic",
    "source": "pipeline",
    "created_at": "2026-01-01T00:00:00",
    "expires_at": None,
    "last_recalled_at": None,
    "recall_count": 0,
    "evidence_count": 1,
    "reasoning": None,
}


def _map_lesson(monkeypatch: pytest.MonkeyPatch, existing: dict) -> tuple[list, list]:
    """Run one lesson through the window mapper with `existing` as the live
    rows the carry-forward lookup finds."""
    import asyncio
    from datetime import datetime

    import server.memory.store as memory_repo
    from server.cli.import_clickgap import _clickgap_map_lesson_window, _ClickgapWrite

    async def _lookup(keys, *, ch=None):
        return {k: existing[k] for k in keys if k in existing}

    monkeypatch.setattr(memory_repo, "memory_rows_by_identity_keys", _lookup)

    class _Embedder:
        async def embed_many(self, texts: list[str]) -> list[list[float]]:
            return [[0.1, 0.2] for _ in texts]

    w = _ClickgapWrite(
        ch=None,
        org="o",
        ns="n",
        agent="clickgap",
        batch_size=10,
        embedder=_Embedder(),
        live_model="fake",
        now=datetime(2026, 1, 1),
        run_version=2,
        actor="process:cli:import-clickgap",
    )
    return asyncio.run(_clickgap_map_lesson_window(w, [dict(_LESSON)], {}, {}))


def test_lesson_rows_carry_the_import_provenance(monkeypatch: pytest.MonkeyPatch) -> None:
    """Both loom.memory and loom.memory_embeddings rows name the CLI writer
    and the `imported` class — search's trust term reads the embedding copy."""
    mem, emb = _map_lesson(monkeypatch, {})
    assert [(r["actor"], r["source_class"]) for r in mem] == [
        ("process:cli:import-clickgap", "imported")
    ]
    assert [r["source_class"] for r in emb] == ["imported"]


def test_lesson_hash_is_the_one_memory_set_stores(monkeypatch: pytest.MonkeyPatch) -> None:
    """content_sha256 is sha256_value of the value, the hash list/search hand
    out as if_match_sha256 — not a hash of the raw text."""
    from server.core.hashing import sha256_value, sha256_value_json

    (row,), _ = _map_lesson(monkeypatch, {})
    assert row["content_sha256"] == sha256_value(_LESSON["text"])
    assert row["content_sha256"] == sha256_value_json(row["value"])


def test_rerun_keeps_loom_side_columns_of_an_imported_lesson(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A re-run's version row of an already-imported lesson carries the pin,
    the review status and the attestation forward instead of resetting them."""
    from datetime import datetime

    from server.memory.store import MemoryRow

    (fresh,), _ = _map_lesson(monkeypatch, {})
    prior = MemoryRow.from_snapshot(
        fresh,
        version=1,
        pinned=True,
        status="conflicted",
        conflict_with=["lesson_9"],
        verified_by="human:alice",
        verified_at=datetime(2026, 2, 1),
        verified_sha256="abc",
        sensitivity="high",
    )
    key = ("o", "n", "clickgap", "-", fresh["session_id"], "lesson_7")
    (row,), _ = _map_lesson(monkeypatch, {key: prior})
    assert row["pinned"] == 1 and row["status"] == "conflicted"
    assert row["conflict_with"] == ["lesson_9"]
    assert (row["verified_by"], row["verified_sha256"], row["sensitivity"]) == (
        "human:alice",
        "abc",
        "high",
    )
    # the lesson's own columns still come from the export
    assert row["version"] == 2 and row["value"] == fresh["value"]
    assert row["content_sha256"] == fresh["content_sha256"]


def test_first_import_row_is_a_full_version_row(monkeypatch: pytest.MonkeyPatch) -> None:
    """A new lesson's row carries every persisted column, so the checked
    insert funnel accepts it; the export's own columns are kept."""
    from server.memory.store.inserts import check_memory_rows

    (row,), _ = _map_lesson(monkeypatch, {})
    check_memory_rows([row])
    assert row["duplicate_of"] == "" and row["lock_mode"] == "" and row["valid_to"] is None
    assert row["value"] == json.dumps(_LESSON["text"]) and row["version"] == 2
