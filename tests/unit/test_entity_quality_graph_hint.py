"""A trajectory transcript is not text-mined into the entity
graph nor injected as recall, the summary ranks live edges, and the graph hint
counts real entity rows and rides only the calls it is about.

Mining every content word of a trajectory row such as "Step 225/229 · After
Bash `command=sed …`: … revealed content" floods the graph with entities like
`command`, `step` and `bash` while ignoring the file paths the writer
declared.
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from typing import Any

import pytest

from server.insights.app import mentor as m
from server.insights.app import mentor_rules
from server.memory.app.entity_projections import set_entities_for_projection
from server.memory.domain.transcript_rows import TRANSCRIPT_ROW_SQL, is_transcript_row

_STEP = (
    "Step 225/229 · After Bash `command=sed -n 840,910p tests/integration/helpers/"
    "catalog_manager_databricks_rest.py`: Read SQL execution helpers from "
    "catalog_manager_databricks_rest.py. revealed content"
)


def _set(**kw: Any) -> SimpleNamespace:
    return SimpleNamespace(**{"tags": [], "source_trace": "", "entities": [], **kw})


# --- which rows are transcripts --------------------------------------------------


def test_transcript_rows_are_the_raw_trajectory_only() -> None:
    assert is_transcript_row(["observation", "command_run"], "observe.record")
    assert is_transcript_row(["event", "from_observation", "tool:Bash"], "")
    assert is_transcript_row([], "observe.extract:event:obs:a>obs:b")
    # What a session is DISTILLED into stays a fact: the extractor's rows and
    # the hint note carry `from_observation` but are claims, not transcripts.
    assert not is_transcript_row(["from_observation", "mined"], "pipeline:set_from_messages")
    assert not is_transcript_row(["hint", "from_observation"], "observe.extract:hint")
    assert not is_transcript_row(["preference"], "")
    assert "observe.record" in TRANSCRIPT_ROW_SQL and "'observation'" in TRANSCRIPT_ROW_SQL


# --- the point-write projection ---------------------------------------------------


def test_a_step_event_projects_its_declared_files_not_its_words() -> None:
    ents = set_entities_for_projection(
        _set(
            tags=["event", "from_observation", "tool:Bash"],
            source_trace="observe.extract:event:obs:a>obs:b",
            entities=[
                "tests/integration/helpers/catalog_manager_databricks_rest.py",
                "/sql/statements",
            ],
        ),
        _STEP,
    )
    assert ents == [
        "tests/integration/helpers/catalog_manager_databricks_rest.py",
        "/sql/statement",
    ]
    for junk_word in ("command", "step", "bash", "after bash", "revealed", "content"):
        assert junk_word not in ents


def test_a_transcript_without_declared_entities_projects_nothing() -> None:
    assert set_entities_for_projection(_set(tags=["observation"]), _STEP) == []


def test_declared_entities_win_over_mining_on_any_row() -> None:
    ents = set_entities_for_projection(
        _set(entities=["Acme Corp", "Bob"]), "Bob moved to Acme Corp in Berlin last spring"
    )
    assert ents == ["acme corp", "bob"]


def test_an_undeclared_fact_is_still_mined_as_before() -> None:
    ents = set_entities_for_projection(
        _set(), "Alice joined ClickHouse Inc as a mergetree engineer"
    )
    assert "alice" in ents and "clickhouse inc" in ents and "mergetree" in ents


# --- ambient recall ---------------------------------------------------------------


def test_the_pack_drops_transcript_hits_and_keeps_facts() -> None:
    from server.memory.app.context_pack import _recall_rows, _without_transcript_rows

    hits = [
        SimpleNamespace(memory_key="event:1e8b", tags=["event", "from_observation"]),
        SimpleNamespace(memory_key="deploy_day", tags=["ops"]),
        SimpleNamespace(memory_key="obs:1", tags=["observation", "shell"]),
        SimpleNamespace(memory_key="fix_recipe", tags=["from_observation", "mined"]),
    ]
    kept = _without_transcript_rows(hits)
    assert [h.memory_key for h in kept] == ["deploy_day", "fix_recipe"]
    assert all(h.tags is None for h in kept)  # asked for the filter, never on the wire
    rows = [
        SimpleNamespace(memory_key="pin", tags=[], source_trace=""),
        SimpleNamespace(memory_key="obs:2", tags=[], source_trace="observe.record"),
    ]
    assert [r.memory_key for r in _recall_rows(rows[:1], rows[1:])] == ["pin"]


# --- the graph hint --------------------------------------------------------------


def _profile(*codes: str) -> mentor_rules.MentorProfile:
    return mentor_rules.MentorProfile(
        org="o",
        namespace="ns",
        agent="a",
        window_seconds=604800,
        usage={"memory.set": 10},
        sessions=3,
        facts={},
        hints=tuple(
            mentor_rules.Hint(code=c, severity="info", text=f"{c} text", ops=("facts.traverse",))
            for c in codes
        ),
    )


@pytest.fixture(autouse=True)
def _clean_mentor() -> Any:
    m.reset_cache()
    yield
    m.reset_cache()


def _call(op: str, session: str) -> tuple[str, str] | None:
    return m.call_hint(
        op, {}, {"ok": True}, session_key=session, org="o", namespace="ns", agent="a"
    )


def test_the_graph_hint_rides_only_memory_context_and_facts_calls() -> None:
    m._cache_put(("o", "ns", "a"), 10**12, _profile("facts_graph_unused"))
    for op in ("knowledge.get", "code.search", "working_memory.set", "tracker.flaky"):
        assert _call(op, "g1") is None, op
    hint = _call("memory.search", "g1")
    assert hint is not None and hint[0] == "facts_graph_unused"
    assert _call("memory.search", "g2") is None  # once per agent per profile TTL


def test_other_profile_hints_keep_every_carrier() -> None:
    m._cache_put(("o", "ns", "a"), 10**12, _profile("runs_unused"))
    hint = _call("knowledge.get", "r1")
    assert hint is not None and hint[0] == "runs_unused"


def test_the_graph_hint_counts_rows_that_are_not_transcripts() -> None:
    import inspect

    from server.insights.store.mentor import ProfileReads

    src = inspect.getsource(ProfileReads.namespace_rows)
    assert "length(entities) > 0 AND NOT {_TRANSCRIPT_ROW}" in src


# --- the summary tile ------------------------------------------------------------


class _SummaryCH:
    def __init__(self) -> None:
        self.sqls: list[str] = []

    async def query_all(self, sql: str, params: dict[str, Any] | None = None) -> list:
        self.sqls.append(sql)
        if "loom.entity_edges" in sql:
            return [
                {"entity": "clickhouse", "degree": 900},
                {"entity": "we", "degree": 800},  # lexical junk
                {"entity": "mergetreedata.cpp", "degree": 40},
            ]
        return [{"memory_type": "semantic", "n": 3}]

    async def query_one(self, sql: str, params: dict[str, Any] | None = None) -> dict:
        self.sqls.append(sql)
        return {"writes": 1, "recalls": 0, "agents": 1, "last_at": None}


def test_the_summary_ranks_live_edges_without_lexical_junk(monkeypatch: pytest.MonkeyPatch) -> None:
    from server.identity.api.models_namespace_catalog import NamespaceSummaryInput
    from server.identity.app import namespace_summary as ns_summary

    ch = _SummaryCH()
    monkeypatch.setattr(ns_summary, "get_client", lambda: ch)
    out = asyncio.run(
        ns_summary.handle_namespace_summary(
            NamespaceSummaryInput(org="o", namespace="ns", agent="a", top_entities_limit=2)
        )
    )
    assert [(e.entity, e.degree) for e in out.top_entities] == [
        ("clickhouse", 900),
        ("mergetreedata.cpp", 40),
    ]
    edges_sql = next(s for s in ch.sqls if "loom.entity_edges" in s)
    assert "invalid_at IS NULL" in edges_sql and "entity_degree" not in edges_sql


def test_a_hint_another_worker_showed_is_already_said() -> None:
    """Four uvicorn workers, four "already said" sets: a profile built from a
    ledger that recorded the code on another worker's response starts with it
    said."""
    p = _profile("facts_graph_unused")
    seeded = mentor_rules.MentorProfile(
        org=p.org,
        namespace=p.namespace,
        agent=p.agent,
        window_seconds=p.window_seconds,
        usage=p.usage,
        sessions=p.sessions,
        facts={"shown_codes": ["facts_graph_unused"]},
        hints=p.hints,
    )
    m._cache_put(("o", "ns", "a"), 10**12, seeded)
    assert _call("memory.search", "w2") is None
    m._cache_put(("o", "ns", "a"), 10**12, p)  # nothing in the ledger: said once
    hint = _call("memory.search", "w3")
    assert hint is not None and hint[0] == "facts_graph_unused"


def test_the_profile_reads_the_codes_shown_within_the_ttl() -> None:
    import inspect

    from server.insights.store.mentor import ProfileReads

    src = inspect.getsource(ProfileReads.shown_codes)
    assert "args['out_hint_code']" in src and "INTERVAL {{ttl:UInt32}} SECOND" in src
    assert "shown_hints" in inspect.getsource(m._compute)
