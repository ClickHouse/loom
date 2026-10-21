"""A date carries how precisely its source pinned it.

CH-free: the rubric coercion, the prompt, the boost SQL, the parse → bulk
entry → row plumbing on both tables, and consolidation's choice of grade.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

import pytest

from server.memory.app.extract.parse import _parse_fact_list
from server.memory.domain import temporal_confidence as tc

# ----------------------------------------------------------------------
# Rubric coercion
# ----------------------------------------------------------------------


@pytest.mark.parametrize(
    ("raw", "has_anchor", "expected"),
    [
        (1.0, True, 1.0),
        ("0.85", True, 0.85),
        (0.35, True, 0.35),
        (7, True, 1.0),  # clamped
        (-2, True, 0.0),  # clamped
        (None, True, tc.UNSCORED),
        ("high", True, tc.UNSCORED),
        (True, True, tc.UNSCORED),  # bools are not grades
        (float("nan"), True, tc.UNSCORED),
        (0.9, False, tc.UNSCORED),  # no anchor → nothing to be sure about
    ],
)
def test_coerce_clamps_and_zeroes(raw: Any, has_anchor: bool, expected: float) -> None:
    assert tc.coerce(raw, has_anchor=has_anchor) == expected


def test_rubric_is_monotone_and_prompt_states_it() -> None:
    from server.memory.domain.extraction_prompts import _EXTRACTION_SYSTEM_PROMPT as PROMPT

    grades = [g for _, g in tc.RUBRIC]
    assert grades == sorted(grades, reverse=True) and grades[0] == 1.0
    assert '"temporal_confidence"' in PROMPT
    for grade in grades:
        assert f"{grade:.2f}".rstrip("0") in PROMPT, grade
    assert "null when temporal_anchor is null" in PROMPT


def test_anchor_weight_keeps_full_boost_for_unscored_rows() -> None:
    assert tc.anchor_weight(0.0) == 1.0
    assert tc.anchor_weight(0.35) == 0.35
    assert tc.anchor_weight(1.0) == 1.0
    assert tc.anchor_weight_sql() == "if(e.temporal_confidence > 0, e.temporal_confidence, 1.0)"


# ----------------------------------------------------------------------
# Parse
# ----------------------------------------------------------------------


def test_parse_carries_grade_only_with_an_anchor() -> None:
    raw = (
        '{"facts": ['
        '{"key": "a", "value": "v", "temporal_anchor": "2023-05-07", "temporal_confidence": 0.65},'
        '{"key": "b", "value": "v", "temporal_anchor": "2023-05-07"},'
        '{"key": "c", "value": "v", "temporal_confidence": 0.9},'
        '{"key": "d", "value": "v", "temporal_anchor": "2023-05-07", "temporal_confidence": "1"}'
        "]}"
    )
    facts = {f.memory_key: f for f in _parse_fact_list(raw)}
    assert facts["a"].temporal_confidence == 0.65
    assert facts["b"].temporal_confidence == tc.UNSCORED
    assert facts["c"].temporal_anchor is None and facts["c"].temporal_confidence == 0.0
    assert facts["d"].temporal_confidence == 1.0


# ----------------------------------------------------------------------
# Pipeline → bulk entry
# ----------------------------------------------------------------------


@pytest.mark.asyncio
async def test_pipeline_entry_carries_the_grade(monkeypatch: pytest.MonkeyPatch) -> None:
    from server.memory.app import bulk_set as bulk_set_mod
    from server.memory.app.pipeline import _step_bulk_write
    from server.memory.app.pipeline_state import WorkflowState

    captured: list = []

    async def _capture(bulk_input):
        captured.append(bulk_input)

        class _Out:
            written: list = []
            skipped_blacklisted: list = []

        return _Out()

    monkeypatch.setattr(bulk_set_mod, "handle_memory_bulk_set", _capture)
    state = WorkflowState(
        org="dev",
        namespace="ns",
        agent="a",
        observation_date="2023-05-07",
        extracted_facts=[
            {
                "key": "k1",
                "value": "User visited Kyoto a while back.",
                "temporal_anchor": "2022-01-01T00:00:00",
                "temporal_confidence": 0.35,
            }
        ],
    )
    await _step_bulk_write(state, {})
    entry = captured[0].entries[0]
    assert entry.temporal_anchor == "2022-01-01T00:00:00"
    assert entry.temporal_confidence == 0.35


def test_bulk_entry_rejects_out_of_range_grade() -> None:
    from pydantic import ValidationError

    from server.memory.api.models_bulk import _BulkSetEntry

    with pytest.raises(ValidationError):
        _BulkSetEntry(memory_key="k", value="v", temporal_confidence=1.5)


# ----------------------------------------------------------------------
# Bulk write → both tables
# ----------------------------------------------------------------------


class _StubCH:
    def __init__(self) -> None:
        self.inserts: list[tuple[str, list[dict[str, Any]]]] = []

    async def query_all(self, sql, params=None):
        return []

    async def query_one(self, sql, params=None):
        return None

    async def insert(self, table, rows, settings=None):
        self.inserts.append((table, rows))

    async def command(self, sql, params=None):
        return None


class _StubEmbedder:
    model_name = "fake"
    dim = 4

    async def embed_many(self, texts):
        return [[0.1, 0.2, 0.3, 0.4] for _ in texts]

    async def embed(self, text):
        return [0.1, 0.2, 0.3, 0.4]


@pytest.mark.asyncio
async def test_bulk_set_writes_the_grade_on_memory_and_embeddings(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import server.memory.app.extract.handler as mx
    from server.memory.api.models_bulk import MemoryBulkSetInput, _BulkSetEntry
    from server.memory.app import bulk_set as _bulk_set_mod
    from server.memory.app import session_summary as _session_summary_mod

    ch = _StubCH()
    embedding_rows: list[dict[str, Any]] = []
    monkeypatch.setattr(_bulk_set_mod, "get_client", lambda: ch)
    monkeypatch.setattr(_session_summary_mod, "get_client", lambda: ch)
    monkeypatch.setattr(_bulk_set_mod, "get_embedder", lambda: _StubEmbedder())
    monkeypatch.setattr(_session_summary_mod, "get_embedder", lambda: _StubEmbedder())

    async def _none(*_: Any, **__: Any) -> None:
        return None

    async def _no_entities(*_: Any, **__: Any) -> dict[str, str]:
        return {}

    async def _capture_embeddings(rows: list[dict[str, Any]], *, ch: Any) -> None:
        embedding_rows.extend(rows)

    monkeypatch.setattr("server.memory.app.blacklist_guard.find_match", _none)
    monkeypatch.setattr("server.memory.app.blacklist_guard.note_hit", _none)
    monkeypatch.setattr(
        "server.memory.app.entity_resolution.resolve_entities_for_write", _no_entities
    )
    monkeypatch.setattr("server.memory.store.insert_embedding_rows", _capture_embeddings)
    for proj in ("write_entity_vectors", "write_entity_edges", "write_entity_triples"):
        monkeypatch.setattr(_bulk_set_mod, proj, _none)
    inp = MemoryBulkSetInput(
        org="dev",
        namespace="ns",
        agent="a",
        user_id="alice",
        session_id="s1",
        entries=[
            _BulkSetEntry(
                memory_key="dated",
                value="Visited Kyoto in March 2023.",
                temporal_anchor="2023-03-01",
                temporal_confidence=0.9,
            ),
            _BulkSetEntry(
                memory_key="undated",
                value="Likes matcha.",
                temporal_confidence=0.9,
            ),
        ],
    )
    await mx.handle_memory_bulk_set(inp)
    by_table: dict[str, list[dict[str, Any]]] = {}
    for table, rows in ch.inserts:
        by_table.setdefault(table, []).extend(rows)
    mem = {r["memory_key"]: r for r in by_table["loom.memory"]}
    assert mem["dated"]["temporal_confidence"] == 0.9
    assert mem["dated"]["temporal_anchor"] == datetime(2023, 3, 1)
    # No anchor → the grade is meaningless and must not be stored.
    assert mem["undated"]["temporal_confidence"] == 0.0
    emb = {r["memory_key"]: r for r in embedding_rows}
    assert emb["dated"]["temporal_confidence"] == 0.9
    assert emb["undated"]["temporal_confidence"] == 0.0


# ----------------------------------------------------------------------
# Row builders and the boost
# ----------------------------------------------------------------------


def test_embedding_row_builder_and_worker_copy_carry_the_column() -> None:
    from server.memory.store import EMBEDDING_PERSISTED_COLS, build_embedding_row
    from server.memory.workers.embedding import _carry_signals

    assert EMBEDDING_PERSISTED_COLS["temporal_confidence"] == 0.0
    row = build_embedding_row(temporal_confidence=0.75)
    assert row["temporal_confidence"] == 0.75
    copied = _carry_signals(
        {
            "org": "o",
            "namespace": "n",
            "agent": "a",
            "user_id": "u",
            "session_id": "s",
            "memory_key": "k",
            "memory_type": "semantic",
            "scope": "private",
            "version": 3,
            "temporal_confidence": 0.65,
        }
    )
    assert copied["temporal_confidence"] == 0.65


@pytest.mark.asyncio
async def test_anchor_boost_is_scaled_by_the_grade() -> None:
    """Graph mode with a resolved query date: the 0.4 date-match boost is
    multiplied by the row's grade, unscored rows (0) keep the full 0.4, and
    the grade is projected so hits can carry it."""
    from server.memory.app.retrieval import vector_search
    from server.memory.domain.search_types import SearchQuery

    class _CH:
        def __init__(self) -> None:
            self.sql: list[str] = []

        async def query_all(self, sql, params=None):
            self.sql.append(sql)
            return []

    ch = _CH()
    q = SearchQuery(
        org="o",
        namespace="n",
        agent="a",
        scope="private",
        embedding_model="fake",
        query_vec=[0.1] * 4,
        top_k=5,
        search_mode="graph",
        query_anchor_iso="2023-05-07",
    )
    await vector_search(q, ch=ch)
    joined = "\n".join(ch.sql)
    assert "0.4 * if(e.temporal_confidence > 0, e.temporal_confidence, 1.0)" in joined
    assert "e.temporal_confidence AS temporal_confidence" in joined


def test_consolidation_keeps_the_grade_of_the_earliest_anchor() -> None:
    from server.memory.app.consolidate import _confidence_of_earliest_anchor

    cluster = [
        {"temporal_anchor": datetime(2024, 1, 1), "temporal_confidence": 0.35},
        {"temporal_anchor": datetime(2023, 1, 1), "temporal_confidence": 0.9},
        {"temporal_anchor": None, "temporal_confidence": 1.0},
    ]
    assert _confidence_of_earliest_anchor(cluster) == 0.9
    assert _confidence_of_earliest_anchor([{"temporal_anchor": None}]) == 0.0


@pytest.mark.asyncio
async def test_an_extracted_fact_with_a_huge_entity_reaches_the_bulk_write(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The extractor's reply names one 32 KB entity: parse keeps it and the pipeline
    writes the fact with it, instead of refusing the batch."""
    import json

    from server.memory.app import bulk_set as bulk_set_mod
    from server.memory.app.extract.parse import _parse_fact_list
    from server.memory.app.pipeline import _step_bulk_write
    from server.memory.app.pipeline_state import WorkflowState

    huge = "rome" + " district" * 3600
    reply = {"facts": [{"key": "user/home", "value": "lives in Rome", "entities": [huge]}]}
    (fact,) = _parse_fact_list(json.dumps(reply))
    captured: list = []

    async def _capture(bulk_input):
        captured.append(bulk_input)

        class _Out:
            written: list = []
            skipped_blacklisted: list = []

        return _Out()

    monkeypatch.setattr(bulk_set_mod, "handle_memory_bulk_set", _capture)
    state = WorkflowState(
        org="dev",
        namespace="ns",
        agent="a",
        observation_date="2023-05-07",
        extracted_facts=[{"key": fact.memory_key, "value": fact.value, "entities": fact.entities}],
    )
    await _step_bulk_write(state, {})
    assert captured[0].entries[0].entities == [huge.lower()]
