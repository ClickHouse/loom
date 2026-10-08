"""The operation mix is a monitored signal."""

from __future__ import annotations

from typing import Any

import pytest

from server.insights.app import op_mix


def test_every_registered_memory_op_has_a_class() -> None:
    from server.operations.registry import ALL_OPERATIONS

    names = [
        o.name
        for o in ALL_OPERATIONS
        if o.name.startswith(("memory.", "working_memory.", "context.", "facts."))
    ]
    unclassified = sorted(n for n in names if op_mix.classify(n) == "other")
    assert not unclassified, unclassified


def test_classify_verbs() -> None:
    assert op_mix.classify("memory.set_from_messages") == "create"
    assert op_mix.classify("memory.get") == "read"
    assert op_mix.classify("memory.ask") == "read"
    assert op_mix.classify("memory.update_by_id") == "update"
    assert op_mix.classify("memory.update") == "update"
    assert op_mix.classify("memory.link") == "annotate"
    assert op_mix.classify("memory.unpin") == "annotate"
    assert op_mix.classify("memory.delete") == "delete"
    assert op_mix.classify("working_memory.append") == "scratchpad"
    assert op_mix.classify("memory.consolidate") == "maintain"
    assert op_mix.classify("knowledge.search") == "other"


def _week_rows() -> list[dict[str, Any]]:
    # A seven-day mix, rounded.
    return [
        {"operation": "memory.get", "c": 127_406, "agents": ["a", "b"]},
        {"operation": "memory.set_from_messages", "c": 80_840, "agents": ["a"]},
        {"operation": "memory.search", "c": 67_151, "agents": ["b", "c"]},
        {"operation": "memory.set", "c": 36_959, "agents": ["c"]},
        {"operation": "memory.ask", "c": 26_365, "agents": []},
        {"operation": "memory.link", "c": 8_466, "agents": ["a"]},
        {"operation": "memory.delete", "c": 220, "agents": ["a"]},
        {"operation": "memory.update_by_id", "c": 4, "agents": ["d"]},
        {"operation": "working_memory.set", "c": 1, "agents": ["d"]},
        {"operation": "memory.nonsense", "c": 0, "agents": []},
    ]


def test_fold_counts_ratios_and_top() -> None:
    mix = op_mix.fold(_week_rows(), window_days=7, top_n=3)
    assert mix.counts["read"] == 127_406 + 67_151 + 26_365
    assert mix.counts["create"] == 80_840 + 36_959
    assert mix.counts["update"] == 4
    assert mix.counts["scratchpad"] == 1
    assert mix.writes == 117_803
    assert mix.reads_per_write == pytest.approx(1.876, abs=0.001)
    assert mix.update_share == pytest.approx(4 / 117_803)
    assert mix.agents == 4
    assert [t["operation"] for t in mix.top] == [
        "memory.get",
        "memory.set_from_messages",
        "memory.search",
    ]
    data = mix.as_data()
    assert data["reads_per_write"] == 1.88 and data["update_share"] == 0.0
    assert data["top"][0]["class"] == "read"


def test_assess_flags_append_only_bank() -> None:
    mix = op_mix.fold(_week_rows(), window_days=7)
    status, detail, repair = op_mix.assess(
        mix, reads_per_write_warn=20.0, update_share_warn=0.01, min_writes=500
    )
    assert status == "warn"
    assert "update share 0.00%" in detail and "reads per write" not in detail
    assert "memory.update" in repair


def test_assess_flags_polling_reads() -> None:
    rows = [
        {"operation": "memory.get", "c": 50_000, "agents": ["a"]},
        {"operation": "memory.set", "c": 1_000, "agents": ["a"]},
        {"operation": "memory.update_by_id", "c": 100, "agents": ["a"]},
    ]
    mix = op_mix.fold(rows, window_days=7)
    status, detail, repair = op_mix.assess(
        mix, reads_per_write_warn=20.0, update_share_warn=0.01, min_writes=500
    )
    assert status == "warn"
    assert detail.startswith("45.5 reads per write")
    assert "if_version" in repair


def test_assess_is_quiet_below_min_writes_and_when_healthy() -> None:
    small = op_mix.fold([{"operation": "memory.get", "c": 10_000, "agents": []}], window_days=7)
    assert (
        op_mix.assess(small, reads_per_write_warn=20, update_share_warn=0.01, min_writes=500)[0]
        == "ok"
    )
    assert small.reads_per_write is None and small.update_share is None
    healthy = op_mix.fold(
        [
            {"operation": "memory.search", "c": 5_000, "agents": ["a"]},
            {"operation": "memory.set", "c": 900, "agents": ["a"]},
            {"operation": "memory.update", "c": 100, "agents": ["a"]},
            {"operation": "memory.delete", "c": 3, "agents": ["a"]},
        ],
        window_days=7,
    )
    status, detail, repair = op_mix.assess(
        healthy, reads_per_write_warn=20, update_share_warn=0.01, min_writes=500
    )
    assert status == "ok" and repair == ""
    assert detail == "5.0 reads per write, update share 10.00%, 3 deletes in 7 d"


class _FakeCH:
    def __init__(self) -> None:
        self.sql = ""
        self.params: dict[str, Any] = {}

    async def query_all(
        self, sql: str, params: dict[str, Any] | None = None
    ) -> list[dict[str, Any]]:
        self.sql, self.params = sql, params or {}
        return [{"operation": "memory.set", "c": 3, "agents": ["x"]}]


@pytest.mark.asyncio
async def test_op_mix_query_scopes_org_and_namespace() -> None:
    ch = _FakeCH()
    mix = await op_mix.op_mix(ch, org="o", namespace="n", window_days=3)
    assert mix.counts["create"] == 3 and mix.window_days == 3
    assert ch.params == {"org": "o", "days": 3, "ns": "n"}
    assert "namespace = {ns:String}" in ch.sql and "AND success" in ch.sql
    ch2 = _FakeCH()
    await op_mix.op_mix(ch2, org="o", window_days=7)
    assert "ns" not in ch2.params and "namespace =" not in ch2.sql
