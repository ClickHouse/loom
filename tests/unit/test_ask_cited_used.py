"""An answer's citations are the used signal.

memory.ask credits the memories its synthesized answer cites with a `used`
usage event — only the keys the answer text marks, only on a synthesized,
non-abstained answer, each in its own namespace — through the same
key-to-id funnel the recall auto-touch uses.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest

from server.memory.api.models_ask import MemoryAskInput
from server.memory.app import after_write
from server.memory.app.ask import handler as ask_mod
from server.memory.store import usage as usage


def _hit(key: str, ns: str = "") -> SimpleNamespace:
    return SimpleNamespace(memory_key=key, namespace=ns)


def _input(ns: str = "ns") -> MemoryAskInput:
    return MemoryAskInput(org="o", namespace=ns, agent="bot", query="q")


def _res(
    answer: str,
    keys: list[str],
    *,
    source: str = "synthesized",
    abstain: bool = False,
    namespaces: dict[str, str] | None = None,
) -> SimpleNamespace:
    namespaces = namespaces or {}
    return SimpleNamespace(
        answer=answer,
        source=source,
        abstain=abstain,
        results=[_hit(k, namespaces.get(k, "")) for k in keys],
    )


@pytest.fixture
def credit_spy(monkeypatch: pytest.MonkeyPatch) -> list[dict[str, Any]]:
    calls: list[dict[str, Any]] = []

    async def fake(
        org: str, ns_keys: dict[str, list[str]], *, event_type: str, agent: str = "", ch: Any = None
    ) -> int:
        calls.append({"org": org, "ns_keys": ns_keys, "event_type": event_type, "agent": agent})
        return sum(len(v) for v in ns_keys.values())

    monkeypatch.setattr(usage, "record_usage_for_keys", fake)
    return calls


async def _drain() -> None:
    await after_write.drain()


@pytest.mark.asyncio
async def test_only_the_keys_the_answer_marks_are_credited(
    credit_spy: list[dict[str, Any]],
) -> None:
    res = _res("Blue is the favourite [m1]; the deadline is Friday [#m3].", ["m1", "m2", "m3"])
    ask_mod._schedule_cited_used(_input(), res)
    await _drain()
    assert credit_spy == [
        {"org": "o", "ns_keys": {"ns": ["m1", "m3"]}, "event_type": "used_cited", "agent": "bot"}
    ]


@pytest.mark.asyncio
async def test_uncited_recalls_are_not_used(credit_spy: list[dict[str, Any]]) -> None:
    # The output's memory_keys would fall back to "all recalled"; the credit must not.
    ask_mod._schedule_cited_used(_input(), _res("No markers in this answer.", ["m1", "m2"]))
    await _drain()
    assert credit_spy == []


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "source,abstain",
    [("retrieval_only", False), ("abstained", True), ("record", False), ("synthesized", True)],
)
async def test_non_synthesized_or_abstained_answers_credit_nothing(
    credit_spy: list[dict[str, Any]],
    source: str,
    abstain: bool,
) -> None:
    ask_mod._schedule_cited_used(
        _input(), _res("Answer [m1].", ["m1"], source=source, abstain=abstain)
    )
    await _drain()
    assert credit_spy == []


@pytest.mark.asyncio
async def test_cross_namespace_hits_are_credited_in_their_own_namespace(
    credit_spy: list[dict[str, Any]],
) -> None:
    res = _res("[m1] and [m2].", ["m1", "m2"], namespaces={"m2": "shared-ns"})
    ask_mod._schedule_cited_used(_input("mine"), res)
    await _drain()
    assert credit_spy[0]["ns_keys"] == {"mine": ["m1"], "shared-ns": ["m2"]}


@pytest.mark.asyncio
async def test_credit_failure_is_logged_not_raised(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    import logging

    async def boom(*a: Any, **kw: Any) -> int:
        raise RuntimeError("ch down")

    monkeypatch.setattr(usage, "record_usage_for_keys", boom)
    with caplog.at_level(logging.WARNING, logger=ask_mod.__name__):
        ask_mod._schedule_cited_used(_input(), _res("[m1]", ["m1"]))
        await _drain()
    assert any("cited-used credit failed" in r.message for r in caplog.records)


# ---------------------------------------------------------------------------
# the key-to-id funnel
# ---------------------------------------------------------------------------


class _FakeCH:
    def __init__(self, rows: list[dict[str, Any]]) -> None:
        self.rows = rows
        self.params: dict[str, Any] = {}

    async def query_all(
        self, sql: str, params: dict[str, Any] | None = None
    ) -> list[dict[str, Any]]:
        assert "FROM loom.memory FINAL" in sql and "tombstoned_at IS NULL" in sql
        self.params = dict(params or {})
        return self.rows


@pytest.mark.asyncio
async def test_record_usage_for_keys_resolves_ids_and_skips_other_namespaces(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    events: list[dict[str, Any]] = []

    async def fake_event(**kw: Any) -> None:
        events.append(kw)

    monkeypatch.setattr(usage, "record_usage_event", fake_event)
    ch = _FakeCH(
        [
            {"memory_id": "id-1", "namespace": "ns", "memory_key": "m1"},
            {
                "memory_id": "id-9",
                "namespace": "other",
                "memory_key": "m1",
            },  # same key, wrong namespace
            {"memory_id": "id-2", "namespace": "shared", "memory_key": "m2"},
        ]
    )
    n = await usage.record_usage_for_keys(
        "o", {"ns": ["m1"], "shared": ["m2"]}, event_type="used", agent="bot", ch=ch
    )
    assert n == 2
    assert sorted(ch.params["namespaces"]) == ["ns", "shared"] and sorted(ch.params["keys"]) == [
        "m1",
        "m2",
    ]
    assert [(e["memory_id"], e["namespace"], e["event_type"], e["agent"]) for e in events] == [
        ("id-1", "ns", "used", "bot"),
        ("id-2", "shared", "used", "bot"),
    ]


@pytest.mark.asyncio
async def test_record_usage_for_keys_empty_is_a_noop() -> None:
    class _NoCH:
        async def query_all(self, *a: Any, **kw: Any) -> list[dict[str, Any]]:
            raise AssertionError("must not query")

    assert await usage.record_usage_for_keys("o", {"ns": []}, event_type="used", ch=_NoCH()) == 0


def test_flag_defaults_on() -> None:
    pass
