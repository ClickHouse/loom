"""The extractor behind the exact-prompt response cache.

Off by default for production; on for `traffic_class = bench` through the
class policy; a per-call pin wins. The key is the one the ask's cache
reads by (sha256 of system, user, max_tokens, response format), it rides the
ledger as `extract_prompt_keys` (no text), the hit / miss counts ride the
output, and the build-side gauge reports provider dollars per row written."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from typing import Any

import pytest

from server.llm.client import LLMResult

_FACTS = '{"facts": [{"key": "user_city", "value": "Lives in Lisbon since 2021."}]}'
_MSGS = [
    {"role": "user", "content": "I moved to Lisbon in 2021 and I still live there today."},
]


class _Provider:
    provider = "fake"
    model = "fake-extractor"

    def __init__(self) -> None:
        self.calls: list[tuple[str, str, int, dict[str, str] | None]] = []

    async def complete(
        self,
        system: str,
        user: str,
        max_tokens: int = 1024,
        response_format: dict[str, str] | None = None,
        **kw: Any,
    ) -> LLMResult:
        self.calls.append((system, user, max_tokens, response_format))
        return LLMResult(
            text=_FACTS, tokens_in=2000, tokens_out=60, provider=self.provider, model=self.model
        )


class _Store:
    """Dict-backed stand-in for the cache table (the shape of the ask-cache tests)."""

    def __init__(self) -> None:
        self.rows: dict[tuple[str, str], dict[str, Any]] = {}
        self.inserted: list[dict[str, Any]] = []

    async def query_one(self, sql: str, params: dict[str, Any] | None = None):
        assert "FINAL" not in sql
        return self.rows.get((params["model"], params["key"]))  # type: ignore[index]

    async def insert(self, table: str, rows: list[dict[str, Any]], settings=None) -> None:
        for r in rows:
            self.inserted.append(dict(r, table=table))
            self.rows[(r["model"], r["key"])] = {
                "text": r["text"],
                "tokens_in": r["tokens_in"],
                "tokens_out": r["tokens_out"],
            }


async def _settle() -> None:
    for _ in range(3):
        await asyncio.sleep(0)


@pytest.fixture
def extractor(monkeypatch: pytest.MonkeyPatch, settings_override) -> SimpleNamespace:
    import server.llm.cache as lc
    from server.memory.app.extract import client as extract_client

    provider = _Provider()
    store = _Store()
    monkeypatch.setattr(extract_client, "get_llm_client", lambda: provider)
    monkeypatch.setattr(lc.CachedLLMClient, "_client", lambda self: store)
    # The side calls are not what is under test; keep the first pass alone.
    from server.memory.app.extract import extract as extract_mod

    async def _no_backfill(facts, *, client):
        return facts, None, 0

    monkeypatch.setattr(extract_mod, "_backfill_relations", _no_backfill)
    settings_override(
        memory_extract_coverage_sweep_auto_min_statements=0,
        memory_extract_coverage_sweep_auto_min_uncovered=0,
    )
    return SimpleNamespace(provider=provider, store=store)


# ---- the switch --------------------------------------------------------------


def test_default_off_for_production_on_for_the_bench_class(settings_override) -> None:
    import server.observability.traffic_class as tc
    from server.memory.app.extract import extractor_llm_cache_on

    tc.set_current_traffic_class("")
    assert extractor_llm_cache_on() is False, "unclassified: the deployment default (off)"
    tc.set_current_traffic_class("production", tc.SOURCE_NAMESPACE)
    assert extractor_llm_cache_on() is False, "production is not in the shipped policy map"
    tc.set_current_traffic_class("bench", tc.SOURCE_NAMESPACE)
    assert extractor_llm_cache_on() is True, "the class policy's llm_cache turns it on"
    assert extractor_llm_cache_on(False) is False, "a per-call pin wins over the class"
    tc.set_current_traffic_class("")
    assert extractor_llm_cache_on(True) is True
    settings_override(llm_policy_by_traffic_class='{"bench": {"llm_cache": false}}')
    tc.set_current_traffic_class("bench", tc.SOURCE_NAMESPACE)
    assert extractor_llm_cache_on() is False, "the class map can turn it off too"
    tc.set_current_traffic_class("")


# ---- the wrap ----------------------------------------------------------------


async def test_a_re_ingest_of_the_same_bytes_is_served_from_the_cache(extractor) -> None:
    from server.llm.cache import scoped_key
    from server.memory.app.extract import extract_from_messages

    first = await extract_from_messages(
        _MSGS, judge=False, observation_date="2026-09-01", org="o", namespace="ns-a", llm_cache=True
    )
    await _settle()
    assert len(extractor.provider.calls) == 1
    system, user, max_tokens, fmt = extractor.provider.calls[0]
    assert fmt == {"type": "json_object"} and "observation_date: 2026-09-01" in user
    assert first.prompt_key == scoped_key(system, user, max_tokens, fmt, org="o"), (
        "the same key the ask's cache reads by"
    )
    assert (first.llm_cache_hits, first.llm_cache_misses) == (0, 1)
    assert len(extractor.store.inserted) == 1
    row = extractor.store.inserted[0]
    assert row["key"] == first.prompt_key and row["namespace"] == "ns-a" and row["org"] == "o"
    assert "Lisbon" not in row["key"], "no prompt text in the key"

    second = await extract_from_messages(
        _MSGS, judge=False, observation_date="2026-09-01", org="o", namespace="ns-b", llm_cache=True
    )
    assert len(extractor.provider.calls) == 1, "the second namespace's ingest made no call"
    assert (second.llm_cache_hits, second.llm_cache_misses) == (1, 0)
    assert second.prompt_key == first.prompt_key
    assert [(f.memory_key, f.value) for f in second.facts] == [
        (f.memory_key, f.value) for f in first.facts
    ], "byte-identical facts by construction"
    assert second.llm_result.from_cache is True
    assert (second.llm_result.tokens_in, second.llm_result.tokens_out) == (2000, 60)


async def test_existing_memories_and_observation_date_change_the_key(extractor) -> None:
    from server.memory.app.extract import extract_from_messages

    base = await extract_from_messages(
        _MSGS, judge=False, observation_date="2026-09-01", llm_cache=True
    )
    dated = await extract_from_messages(
        _MSGS, judge=False, observation_date="2026-09-02", llm_cache=True
    )
    known = await extract_from_messages(
        _MSGS,
        judge=False,
        observation_date="2026-09-01",
        existing_memories=[{"memory_key": "user_city", "value": "Lives in Lisbon."}],
        llm_cache=True,
    )
    assert len({base.prompt_key, dated.prompt_key, known.prompt_key}) == 3
    assert len(extractor.provider.calls) == 3, "three different prompts, three provider calls"


async def test_off_keys_the_prompt_but_makes_no_cache_read(extractor) -> None:
    from server.memory.app.extract import extract_from_messages

    out = await extract_from_messages(
        _MSGS, judge=False, observation_date="2026-09-01", llm_cache=False
    )
    assert out.prompt_key and (out.llm_cache_hits, out.llm_cache_misses) == (0, 0)
    assert extractor.store.inserted == [], "nothing written when the cache is off"
    again = await extract_from_messages(
        _MSGS, judge=False, observation_date="2026-09-01", llm_cache=False
    )
    assert again.prompt_key == out.prompt_key, (
        "the key is logged either way — the repeat share offline"
    )
    assert len(extractor.provider.calls) == 2


async def test_the_side_calls_take_the_extractions_client(monkeypatch) -> None:
    """The sweep, the relation backfill and the judge use the client the
    extraction hands them (the wrapped one), not a fresh provider client."""
    from server.memory.app.extract import judge as judge_mod
    from server.memory.app.extract import relations, sweep
    from server.memory.app.extract.models import ExtractedFact

    def _no_fresh_client() -> Any:
        raise AssertionError("a side call fetched its own client")

    for mod in (sweep, relations, judge_mod):
        monkeypatch.setattr(mod, "get_llm_client", _no_fresh_client)

    class _Rel(_Provider):
        async def complete(
            self, system: str, user: str, max_tokens: int = 1024, **kw: Any
        ) -> LLMResult:
            self.calls.append((system, user, max_tokens, kw.get("response_format")))
            return LLMResult(
                text='{"relations": [{"index": 0, "relation": ["Ana", "lives_in", "Lisbon"]}]}',
                tokens_in=10,
                tokens_out=5,
                provider="fake",
                model="fake-extractor",
            )

    rel = _Rel()
    facts = [
        ExtractedFact(
            memory_key="ana_city", value="Ana lives in Lisbon.", entities=["Ana", "Lisbon"]
        )
    ]
    _out, usage, n = await relations._backfill_relations(facts, client=rel)
    assert len(rel.calls) == 1 and usage is not None

    judge = _Provider()
    judge_result = LLMResult(
        text='{"verdicts": [{"key": "ana_city", "verdict": "CORRECT"}]}',
        tokens_in=1,
        tokens_out=1,
        provider="fake",
        model="fake-extractor",
    )

    async def _judge_complete(
        system: str, user: str, max_tokens: int = 512, **kw: Any
    ) -> LLMResult:
        judge.calls.append((system, user, max_tokens, None))
        return judge_result

    judge.complete = _judge_complete  # type: ignore[method-assign]
    verdicts, _ = await judge_mod._judge_facts(_MSGS, facts, client=judge)
    assert len(judge.calls) == 1 and verdicts.get("ana_city") == "CORRECT"


# ---- the pipeline and the wire ----------------------------------------------


async def test_pipeline_carries_the_keys_and_counts_to_the_state(monkeypatch) -> None:
    import server.memory.app.extract as mx
    from server.memory.app.pipeline import _step_llm_extract
    from server.memory.app.pipeline_state import WorkflowState

    seen: list[dict[str, Any]] = []

    async def _fake(messages: list[dict[str, Any]], **kw: Any) -> Any:
        seen.append(kw)
        return mx.ExtractionResult(
            facts=[],
            llm_result=LLMResult(text="{}", tokens_in=1, tokens_out=1, provider="p", model="m"),
            prompt_key="k" * 64,
            llm_cache_hits=1,
            llm_cache_misses=0,
        )

    monkeypatch.setattr(mx, "extract_from_messages", _fake)
    state = WorkflowState(
        org="dev", namespace="kcurve-0", agent="a", messages=list(_MSGS), llm_cache=False
    )
    await _step_llm_extract(state, {})
    assert seen and seen[0]["org"] == "dev" and seen[0]["namespace"] == "kcurve-0"
    assert seen[0]["llm_cache"] is False, "the per-call pin reaches the extractor"
    assert state.extract_prompt_keys == ["k" * 64]
    assert (state.llm_cache_hits, state.llm_cache_misses) == (1, 0)


def test_set_from_messages_input_takes_the_pin() -> None:
    from server.memory.api.models_bulk import MemorySetFromMessagesInput

    base = {
        "org": "o",
        "namespace": "n",
        "agent": "a",
        "messages": [{"role": "user", "content": "x"}],
    }
    assert MemorySetFromMessagesInput(**base).llm_cache is None
    assert MemorySetFromMessagesInput(**base, llm_cache=False).llm_cache is False


def test_output_and_ledger_carry_the_keys_and_counts() -> None:
    from server.memory.api.models_bulk import MemorySetFromMessagesOutput
    from server.observability.event_args import project_result

    out = MemorySetFromMessagesOutput(
        extract_prompt_keys=["a" * 64, "b" * 64], llm_cache_hits=2, llm_cache_misses=1
    )
    projected = project_result(out)
    assert projected["out_extract_prompt_keys"] == "a" * 64 + "," + "b" * 64
    assert projected["out_llm_cache_hits"] == "2" and projected["out_llm_cache_misses"] == "1"


def test_cost_per_row_gauge_records_dollars_per_row_by_kind(monkeypatch) -> None:
    import server.observability.metrics as m

    recorded: list[tuple[float, dict[str, str]]] = []

    class _Hist:
        def record(self, value: float, attributes: dict[str, str] | None = None) -> None:
            recorded.append((value, dict(attributes or {})))

    monkeypatch.setattr(m, "_extract_cost_per_row", _Hist())
    m.record_extract_cost_per_row(namespace_kind="bench", cost_usd=0.5, rows=5)
    m.record_extract_cost_per_row(namespace_kind="", cost_usd=0.02, rows=0)
    assert recorded[0] == (0.1, {"loom.namespace_kind": "bench"})
    assert recorded[1] == (0.02, {"loom.namespace_kind": "unclassified"}), (
        "a write of zero rows is charged against one row, not read as free"
    )
    monkeypatch.setattr(m, "_extract_cost_per_row", None)
    m.record_extract_cost_per_row(namespace_kind="bench", cost_usd=1.0, rows=1)  # no meter: no-op
