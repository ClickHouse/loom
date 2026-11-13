"""The reliance signal is split at its source, the
counters ride the hit when asked, a hit says why it was served, the consumer
view shows tagged against untagged per token identity, and a daily gauge
counts the labels agents wrote. Stub ClickHouse, no network."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from typing import Any

import pytest

from server.memory.api.models_search import MemorySearchInput, _MemorySearchHit
from server.memory.app import after_write
from server.memory.store import usage as usage
from server.transport import mcp_tools
from tests.unit.test_usage_fold_worker import (
    StubCH,
    _ident_row,
    _rc,
    _seed_active_pass,
    _totals_row,
    run_pass,
)


@pytest.fixture(autouse=True)
def _fresh_fold_state(monkeypatch: pytest.MonkeyPatch):
    """The fold tests below script the fold's own answers only (the `acted`
    derivation reads two statements of its own first)."""
    from server.memory.workers import usage_fold
    from server.workers.status import get_registry

    async def _no_acted(ch, result):
        return None

    monkeypatch.setattr(usage_fold, "acted_pass", _no_acted)
    usage_fold.reset_watermark()
    get_registry().reset()
    yield
    usage_fold.reset_watermark()
    get_registry().reset()


# ---------------------------------------------------------------- the split


def test_event_kinds_keep_the_self_credit_apart() -> None:
    assert usage.USED_EVENT == "used" and usage.USED_CITED_EVENT == "used_cited"
    # The agent-label set never names memory.ask's own credit or the server's
    # recall / evidence / contradicted writes.
    assert set(usage.AGENT_LABEL_EVENT_TYPES) == {"used", "acted", "harm"}
    assert "used_cited" not in usage.AGENT_LABEL_EVENT_TYPES
    assert "recall" not in usage.AGENT_LABEL_EVENT_TYPES


@pytest.mark.asyncio
async def test_ask_credits_its_citations_as_used_cited(monkeypatch: pytest.MonkeyPatch) -> None:
    from types import SimpleNamespace

    from server.memory.app.ask import handler as ask_mod

    seen: list[str] = []

    async def fake(
        org: str, ns_keys: dict[str, list[str]], *, event_type: str, agent: str = "", ch: Any = None
    ) -> int:
        seen.append(event_type)
        return 1

    monkeypatch.setattr(usage, "record_usage_for_keys", fake)
    inp = SimpleNamespace(org="o", namespace="ns", agent="bot", reinforce=True)
    res = SimpleNamespace(
        abstain=False,
        source="synthesized",
        answer="Blue [m1].",
        results=[SimpleNamespace(memory_key="m1", namespace="ns")],
    )
    ask_mod._schedule_cited_used(inp, res)  # type: ignore[arg-type]
    await after_write.drain()
    assert seen == ["used_cited"]


@pytest.mark.asyncio
async def test_fold_weighs_used_cited_at_the_used_weight_by_default(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from server.settings import settings

    monkeypatch.setattr(settings, "memory_used_reinforcement_weight", 3)
    monkeypatch.setattr(settings, "memory_used_cited_reinforcement_weight", None)
    ch = StubCH()
    ident = _ident_row(reinforcement_count=1)
    ident["used_cited_total"] = 4  # summed beside the identity row
    _seed_active_pass(ch, totals=[_totals_row(recall=1, used=2)], idents=[ident])
    result = await run_pass(ch=ch)
    assert result.folded == 1
    assert _rc(ch) == 1 + 1 + 3 * 2 + 3 * 4  # base + recall + w*used + w*used_cited
    ident_sql = ch.query_all_calls[1][0]
    assert "event_type = 'used_cited'" in ident_sql and "LEFT JOIN" in ident_sql
    assert "toString(m.memory_id) AS mid" in ident_sql and "AS memory_id" not in ident_sql


@pytest.mark.asyncio
async def test_fold_can_stop_the_self_credit_from_ranking(monkeypatch: pytest.MonkeyPatch) -> None:
    from server.settings import settings

    monkeypatch.setattr(settings, "memory_used_reinforcement_weight", 2)
    monkeypatch.setattr(settings, "memory_used_cited_reinforcement_weight", 0)
    ch = StubCH()
    ident = _ident_row(reinforcement_count=1)
    ident["used_cited_total"] = 40
    _seed_active_pass(
        ch, totals=[_totals_row(recall=0, used=1, last_recalled=None)], idents=[ident]
    )
    result = await run_pass(ch=ch)
    assert result.folded == 1
    assert _rc(ch) == 1 + 2 * 1  # the agent's `used` counts; the ask's credit does not


@pytest.mark.asyncio
async def test_used_cited_only_activity_still_folds(monkeypatch: pytest.MonkeyPatch) -> None:
    ch = StubCH()
    ident = _ident_row(reinforcement_count=1)
    ident["used_cited_total"] = 2
    _seed_active_pass(
        ch, totals=[_totals_row(recall=0, used=0, last_recalled=None)], idents=[ident]
    )
    result = await run_pass(ch=ch)
    assert result.folded == 1 and _rc(ch) == 1 + 2 * 2


def test_relied_on_set_reads_both_kinds() -> None:
    from pathlib import Path

    src = Path("bench/relied_on_set.py").read_text()
    assert "e.event_type IN ('used', 'used_cited')" in src


# ---------------------------------------------------------------- the hit


class _CH:
    def __init__(
        self, rows: list[dict[str, Any]], counts: list[dict[str, Any]], folded: list[dict[str, Any]]
    ) -> None:
        self.calls: list[tuple[str, dict[str, Any]]] = []
        self._answers = [rows, counts, folded]

    async def query_all(self, sql: str, params: dict[str, Any] | None = None):
        self.calls.append((sql, params or {}))
        return self._answers.pop(0) if self._answers else []


def _hit(key: str, source: str = "fact", ns: str = "n") -> _MemorySearchHit:
    return _MemorySearchHit(
        memory_key=key,
        memory_type="semantic",
        content_excerpt="x",
        distance=0.1,
        similarity=0.9,
        source=source,
        namespace=ns,
    )


@pytest.mark.asyncio
async def test_include_usage_fills_the_counters_and_leaves_the_default_payload_alone(
    monkeypatch,
) -> None:
    from server.memory.app.search import payload as mod

    off = _hit("k1")
    dumped = off.model_dump()
    for name in (
        "reinforcement_count",
        "used_count",
        "used_cited_count",
        "harm_count",
        "evidence_count",
        "acted_count",
        "pinned",
    ):
        assert name not in dumped  # absent, not zero, when not asked
    assert (
        await mod._fill_hit_usage(
            [off], MemorySearchInput(org="o", namespace="n", query="q"), ["n"]
        )
        == 0
    )

    ch = _CH(
        rows=[
            {
                "namespace": "n",
                "memory_key": "k1",
                "mids": ["a", "b"],
                "pinned": 1,
                "evidence_count": 3,
                "reinforcement_base": 1,
            }
        ],
        counts=[
            {
                "namespace": "n",
                "mid": "a",
                "used_count": 1,
                "used_cited_count": 5,
                "harm_count": 0,
                "evidence_count": 2,
                "acted_count": 1,
            },
            {
                "namespace": "n",
                "mid": "b",
                "used_count": 0,
                "used_cited_count": 2,
                "harm_count": 1,
                "evidence_count": 0,
                "acted_count": 0,
            },
        ],
        folded=[
            {
                "agent": "bot",
                "user_id": "-",
                "memory_key": "k1",
                "reinforcement_count": 17,
                "last_reinforced_at": datetime(2026, 9, 15, tzinfo=UTC).replace(tzinfo=None),
            }
        ],
    )
    monkeypatch.setattr(mod, "get_client", lambda: ch)
    fact, chunk = _hit("k1"), _hit("chunk:s:1", source="chunk")
    n = await mod._fill_hit_usage(
        [fact, chunk],
        MemorySearchInput(org="o", namespace="n", query="q", include_usage=True),
        ["n"],
    )
    assert n == 1
    assert (fact.used_count, fact.used_cited_count, fact.harm_count, fact.acted_count) == (
        1,
        7,
        1,
        1,
    )
    assert fact.evidence_count == 3 and fact.pinned is True and fact.reinforcement_count == 17
    # a chunk reads zeros / false, and the keys are on the wire for every hit
    d = chunk.model_dump()
    assert d["used_count"] == 0 and d["pinned"] is False and d["reinforcement_count"] == 0
    assert "used_cited_count" in fact.model_dump()
    # the counts read is keyed on every live row of the key
    assert sorted(ch.calls[1][1]["ids"]) == ["a", "b"]
    assert "memory_id IN {ids:Array(UUID)}" in ch.calls[1][0]


@pytest.mark.asyncio
async def test_a_user_scoped_read_counts_only_that_users_rows(monkeypatch) -> None:
    """The counters of an own_user read come from that user's rows of the key, and the
    folded reinforcement from that user's credit, not the busiest user's."""
    from server.memory.app.search import payload as mod

    row = {
        "namespace": "n",
        "memory_key": "k1",
        "mids": ["a"],
        "pinned": 0,
        "evidence_count": 0,
        "reinforcement_base": 1,
    }
    folded = [
        {
            "agent": "bot",
            "user_id": "u2",
            "memory_key": "k1",
            "reinforcement_count": 50,
            "last_reinforced_at": datetime(2026, 9, 1),
        },
        {
            "agent": "bot",
            "user_id": "u1",
            "memory_key": "k1",
            "reinforcement_count": 4,
            "last_reinforced_at": datetime(2026, 9, 1),
        },
    ]
    ch = _CH(rows=[row], counts=[], folded=folded)
    monkeypatch.setattr(mod, "get_client", lambda: ch)
    fact = _hit("k1")
    await mod._fill_hit_usage(
        [fact],
        MemorySearchInput(
            org="o",
            namespace="n",
            query="q",
            include_usage=True,
            user_id="u1",
            read_scope="own_user",
        ),
        ["n"],
    )
    rows_sql, rows_params = ch.calls[0]
    assert "user_id = {uid:String}" in rows_sql and rows_params["uid"] == "u1"
    assert fact.reinforcement_count == 4


@pytest.mark.asyncio
async def test_include_usage_fails_open(monkeypatch) -> None:
    from server.memory.app.search import payload as mod

    class _Boom:
        async def query_all(self, sql, params=None):
            raise RuntimeError("replica away")

    monkeypatch.setattr(mod, "get_client", lambda: _Boom())
    h = _hit("k1")
    n = await mod._fill_hit_usage(
        [h], MemorySearchInput(org="o", namespace="n", query="q", include_usage=True)
    )
    assert n == 0 and h.used_count == 0 and h.pinned is False


def test_sdk_hit_carries_every_wire_field_and_the_counters() -> None:
    from loom import Agent

    class _T:
        def __init__(self) -> None:
            self.calls: list[tuple[str, dict[str, Any]]] = []

        async def post(self, op_path, payload, idempotency_key=None, *, timeout_s=None):
            self.calls.append((op_path, payload))
            return {
                "results": [
                    {
                        "memory_key": "k",
                        "memory_type": "semantic",
                        "content_excerpt": "x",
                        "distance": 0.1,
                        "similarity": 0.9,
                        "score_components": {"rank_score_raw": 0.7, "date_proximity_boost": 0.12},
                        "explanation": "key_match",
                        "status": "active",
                        "version": 3,
                        "created_at": "2026-09-15T00:00:00",
                        "source_class": "extracted",
                        "verified": True,
                        "session_id": "s1",
                        "speaker": "audrey",
                        "about_other": False,
                        "derived_from": ["a", "b"],
                        "entities": ["acme"],
                        "reinforcement_count": 17,
                        "used_count": 1,
                        "used_cited_count": 7,
                        "harm_count": 0,
                        "evidence_count": 3,
                        "acted_count": 1,
                        "pinned": True,
                    }
                ],
                "embedding_model": "m",
            }

    agent = Agent(org="o", namespace="n", agent="ag", api_key="x")
    t = _T()
    agent.transport = t  # type: ignore[assignment]
    res = asyncio.run(agent.memory.search("q", include_usage=True))
    (h,) = list(res)
    assert t.calls[0][1]["include_usage"] is True
    assert h.score_components == {"rank_score_raw": 0.7, "date_proximity_boost": 0.12}
    assert (h.explanation, h.status, h.version) == ("key_match", "active", 3)
    assert (
        h.source_class == "extracted"
        and h.verified
        and h.session_id == "s1"
        and h.speaker == "audrey"
    )
    assert h.derived_from == ("a", "b") and h.entities == ("acme",)
    assert (h.reinforcement_count, h.used_count, h.used_cited_count, h.pinned) == (17, 1, 7, True)
    # unasked → not sent, counters None (not 0)
    t2 = _T()
    agent.transport = t2  # type: ignore[assignment]
    asyncio.run(agent.memory.search("q"))
    assert "include_usage" not in t2.calls[0][1]
    (h2,) = list(asyncio.run(agent.memory.search("q")))
    assert h2.reinforcement_count == 17  # the fake always sends it; the parser passes it through
    from loom.memory import MemorySearchHit

    bare = MemorySearchHit(memory_key="k", memory_type="semantic", content_excerpt="x")
    assert bare.used_count is None and bare.pinned is None and bare.score_components == {}


# ---------------------------------------------------------------- the why line


def test_why_note_names_the_terms_by_magnitude_and_skips_the_base() -> None:
    h = {
        "memory_key": "k",
        "explanation": "key_match",
        "score_components": {
            "rank_score_raw": 0.71,
            "date_proximity_boost": 0.12,
            "entity_ann_boost": -0.3,
        },
    }
    assert mcp_tools.why_note(h) == "key_match, entity-0.30, date+0.12"
    assert mcp_tools.why_note({"memory_key": "k"}) == ""
    # A memory whose code changed: the places it moved, whatever the score's scale.
    stale = {"memory_key": "k", "score_components": {"code_changed_places": -3.0}}
    assert mcp_tools.why_note(stale) == "code_changed-3"
    # A reranked row: its own fused score is a base and the trail is positions.
    reranked = {
        "memory_key": "k",
        "score_components": {
            "rank_score_pre_rerank": 0.37,
            "rerank_from": 4.0,
            "rerank_pos": 1.0,
            "rerank_delta": 3.0,
            "rerank_external": 1.0,
            "date_proximity_boost": 0.12,
        },
    }
    assert mcp_tools.why_note(reranked) == "reranked+3, date+0.12"
    reranked["score_components"]["rerank_delta"] = 0.0  # a kept order moved nothing
    assert mcp_tools.why_note(reranked) == "date+0.12"


def test_loom_search_text_appends_why_only_under_explain() -> None:
    hits = [
        {
            "memory_key": "user_hobby",
            "content_excerpt": "hiking",
            "namespace": "ns",
            "score_components": {"key_match_boost_effective": 0.25},
        },
        {"memory_key": "user_pet", "content_excerpt": "dog", "namespace": "ns"},
    ]
    plain = "\n".join(mcp_tools._memory_section_lines({}, hits, top_k=5))
    assert "why" not in plain
    text = "\n".join(mcp_tools._memory_section_lines({}, hits, top_k=5, explain=True))
    assert "[user_hobby] (ns=ns) hiking [why: key_match+0.25]" in text
    assert "[user_pet] (ns=ns) dog" in text and "user_pet] (ns=ns) dog [why" not in text
    memory = {
        "reader_block": "[d] [user_hobby] (t) hiking\n\n[d] [user_pet] (t) dog",
        "candidates_total": 2,
    }
    block = "\n".join(mcp_tools._memory_section_lines(memory, hits, top_k=5, explain=True))
    assert block.index("Why each was served:") > block.index("hiking")
    assert "  [user_hobby] why: key_match+0.25" in block and "user_pet] why" not in block
    assert "Why each" not in "\n".join(mcp_tools._memory_section_lines(memory, hits, top_k=5))


def test_loom_search_schema_declares_explain() -> None:
    tools = (
        mcp_tools.loom_search_descriptor() if hasattr(mcp_tools, "loom_search_descriptor") else None
    )
    import inspect

    src = inspect.getsource(mcp_tools)
    assert '"explain": {' in src and 'arguments.get("explain")' in src
    assert tools is None or "explain" in tools["inputSchema"]["properties"]


# ---------------------------------------------------------------- consumers


@pytest.mark.asyncio
async def test_consumer_tagging_statement_splits_on_the_fallback_classes() -> None:
    from server.insights.store import admin as admin_repo

    class _S:
        def __init__(self) -> None:
            self.calls: list[tuple[str, dict[str, Any]]] = []

        async def query_all(self, sql, params=None):
            self.calls.append((sql, params or {}))
            return []

    ch = _S()
    await admin_repo.consumer_tagging(3600, 10, org="dev", ch=ch)  # type: ignore[arg-type]
    sql, params = ch.calls[0]
    assert "GROUP BY org, agent" in sql and "AS tagged_ratio" in sql
    assert set(params["untagged"]) == {"", "anonymous", "user", "service-account"}
    assert params["org"] == "dev" and "AND org = {org:String}" in sql


def test_admin_consumers_window_carries_the_tagging_rows(monkeypatch: pytest.MonkeyPatch) -> None:
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from server.insights.app.admin_routes import admin_router
    from server.insights.store import admin as admin_repo
    from server.settings import settings

    monkeypatch.setattr(settings, "auth_required", False)
    app = FastAPI()
    app.include_router(admin_router)
    client = TestClient(app)

    async def _consumers(since, limit, *, org=None, ch=None):
        return []

    async def _outcomes(since, limit, *, org=None, ch=None):
        return []

    async def _tagging(since, limit, *, org=None, ch=None):
        return [
            {
                "org": "clickhouse",
                "agent": "clickgap",
                "calls": 2925,
                "tagged": 117,
                "untagged": 2808,
                "tagged_ratio": 0.04,
                "tag": "clickgap",
                "tagged_ops": 8,
                "untagged_ops": 5,
            }
        ]

    monkeypatch.setattr(admin_repo, "consumers", _consumers)
    monkeypatch.setattr(admin_repo, "consumer_outcomes", _outcomes)
    monkeypatch.setattr(admin_repo, "consumer_tagging", _tagging)
    body = client.post("/v1/admin/consumers", json={"windows_seconds": [86400]}).json()
    (row,) = body["windows"][0]["tagging"]
    assert row["agent"] == "clickgap" and row["tagged"] == 117 and row["untagged"] == 2808
    assert row["tagged_ratio"] == 0.04 and row["tag"] == "clickgap"

    async def _boom(since, limit, *, org=None, ch=None):
        raise RuntimeError("no column")

    monkeypatch.setattr(admin_repo, "consumer_tagging", _boom)
    body = client.post("/v1/admin/consumers", json={"windows_seconds": [86400]}).json()
    assert body["windows"][0]["tagging"] == []  # additive: a failed read leaves the window intact


# ---------------------------------------------------------------- the gauge


def test_agent_labels_series_is_per_namespace_and_kind() -> None:
    from server.observability import otel_probes

    series = otel_probes._agent_labels_series_from_rows(
        [
            ("clickhouse", "demo-full2", "used", 12),
            ("clickhouse", "demo-full2", "acted", 3),
            ("dev", "n", "harm", 1),
        ]
    )
    rows = series["agent_labels_24h"]  # the at-pace count rides beside it
    assert [(v, a["loom.namespace"], a["loom.kind"]) for v, a in rows] == [
        (12.0, "demo-full2", "used"),
        (3.0, "demo-full2", "acted"),
        (1.0, "n", "harm"),
    ]
    assert "used_cited" not in otel_probes._AGENT_LABELS_SQL  # kinds are a parameter
    assert "{kinds:Array(String)}" in otel_probes._AGENT_LABELS_SQL


def test_agent_labels_gauge_registered_and_silent_without_a_probe() -> None:
    import time

    from server.observability import metrics_gauges
    from tests.unit.test_code_state_gauges import _Meter

    meter = _Meter()
    metrics_gauges.register_state_gauges(meter, ch_probe=dict)
    assert "loom.memory.agent_labels_24h" not in meter.gauges

    series = {
        "agent_labels_24h": [(12.0, {"loom.org": "o", "loom.namespace": "n", "loom.kind": "used"})]
    }
    meter = _Meter()
    metrics_gauges.register_state_gauges(meter, ch_probe=dict, agent_labels_probe=lambda: series)
    assert "loom.memory.agent_labels_24h" in meter.gauges
    # The TTL cache produces in a background thread: the first read is empty
    # (no observation, never 0) and the value lands within the deadline —
    # bounded by time, not iterations, for a slow CI runner.
    obs: list[Any] = meter.gauges["loom.memory.agent_labels_24h"](None)
    assert obs == []
    deadline = time.monotonic() + 15.0
    while not obs and time.monotonic() < deadline:
        time.sleep(0.02)
        obs = meter.gauges["loom.memory.agent_labels_24h"](None)
    (o,) = obs
    assert o.value == 12.0 and o.attributes["loom.kind"] == "used"


# ---------------------------------------------------------------- the SDK payload builder


def test_search_payload_sends_wire_defaults_and_only_the_knobs_set() -> None:
    from datetime import datetime

    from loom.memory import _search_payload

    base = dict(
        org="o",
        namespace="n",
        query="q",
        memory_type=None,
        tags=None,
        scope="private",
        user_id=None,
        run_id=None,
        agent_id=None,
        entity_filter=None,
        top_k=5,
        search_mode="rrf",
        alpha=0.5,
        auto_touch=True,
        namespace_scope=None,
        bodies=None,
        body_budget_tokens=None,
        recipe="",
        hyde=None,
        rerank="",
        retrieval_budget=None,
        category="",
        observation_date="",
        include_top_n_unmatched=0,
        tags_soft=False,
        tag_boost=0.3,
        category_soft=False,
        category_boost=0.3,
        confidence_weight=None,
        demote_tags=None,
        reinforcement_weight=None,
        reinforcement_half_life_days=None,
        as_of=None,
        include_tags=False,
        include_usage=False,
    )
    plain = _search_payload(**base)  # type: ignore[arg-type]
    plain.pop("run_id", None)  # an ambient run (loom.run) may add it; every other key is fixed
    # the wire defaults, the wildcard agent, and nothing optional
    assert plain == {
        "org": "o",
        "namespace": "n",
        "agent": "*",
        "query": "q",
        "scope": "private",
        "top_k": 5,
        "search_mode": "rrf",
        "alpha": 0.5,
        "auto_touch": True,
    }
    for absent in (
        "include_tags",
        "include_usage",
        "tags_soft",
        "tag_boost",
        "as_of",
        "bodies",
        "reinforcement_weight",
        "namespace_scope",
    ):
        assert absent not in plain
    full = _search_payload(
        **{  # type: ignore[arg-type]
            **base,
            "agent_id": "bot",
            "tags": ["a"],
            "namespace_scope": "all_mine",
            "tags_soft": True,
            "reinforcement_weight": 0.0,
            "as_of": datetime(2026, 9, 15),
            "include_tags": True,
            "include_usage": True,
            "bodies": "unit",
            "body_budget_tokens": 400.0,
        }
    )
    assert (
        full["agent"] == "bot" and full["tags"] == ["a"] and full["namespace_scope"] == "all_mine"
    )
    assert full["tags_soft"] is True and full["tag_boost"] == 0.3
    assert full["reinforcement_weight"] == 0.0  # 0 is a value, not "unset"
    assert full["as_of"] == "2026-09-15T00:00:00" and full["body_budget_tokens"] == 400
    assert full["include_tags"] is True and full["include_usage"] is True
