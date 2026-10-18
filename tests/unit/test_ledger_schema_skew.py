"""A ledger insert survives schema skew, every ledger row says
which process wrote it, and /v1/health reports pending migrations.

Pure: a fake driver client records what the wrapper hands it."""

from __future__ import annotations

import logging
from datetime import UTC, datetime
from typing import Any

import pytest

import server.core.ch as ch_mod
from server.core.schema import status as ss
from server.observability import instance as inst

_LIVE = [
    "ts",
    "org",
    "namespace",
    "agent",
    "user_id",
    "session_id",
    "run_id",
    "operation",
    "request_id",
    "trace_id",
    "latency_ms",
    "input_tokens",
    "output_tokens",
    "embedding_tokens",
    "upstream_cost_usd",
    "bytes_written",
    "success",
    "error_code",
    "memory_key",
    "impersonated_by",
    "kind",
    "args",
    "model",
    "llm_calls",
]  # an older table: no build_commit, instance, consumer, response_tokens


class _FakeAsync:
    def __init__(self, live: list[str]) -> None:
        self.live = list(live)
        self.describes = 0
        self.inserts: list[dict[str, Any]] = []

    async def query(self, sql: str, parameters: Any = None, settings: Any = None) -> Any:
        assert sql.startswith("DESCRIBE TABLE ")
        self.describes += 1
        live = self.live

        class _R:
            column_names = ["name", "type", "default_type"]
            result_rows = [[c, "String", ""] for c in live]

        return _R()

    async def insert(
        self,
        table: str,
        data: Any,
        column_names: Any = None,
        column_type_names: Any = None,
        settings: Any = None,
    ) -> None:
        unknown = [c for c in column_names if c not in self.live]
        if unknown:
            raise RuntimeError(f"Code: 16. DB::Exception: No such column {unknown[0]}")
        self.inserts.append({"table": table, "columns": list(column_names), "rows": len(data)})


@pytest.fixture(autouse=True)
def _clean() -> Any:
    ch_mod.describe_cache_clear()
    yield
    ch_mod.describe_cache_clear()


def _client(fake: _FakeAsync, monkeypatch: pytest.MonkeyPatch) -> ch_mod.CHClient:
    c = ch_mod.CHClient()
    monkeypatch.setattr(type(c), "client", property(lambda self: fake))
    return c


def _rows(n: int) -> list[dict[str, Any]]:
    from server.observability.events import AgentEvent

    return [
        AgentEvent(
            ts=datetime.now(UTC),
            org="o",
            namespace="n",
            agent="a",
            user_id="u",
            session_id="s",
            operation="memory.get",
            request_id=f"r{i}",
            latency_ms=1,
            success=True,
        ).to_row()
        for i in range(n)
    ]


async def test_ledger_insert_drops_the_unknown_columns_not_the_batch(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    fake = _FakeAsync(_LIVE)
    c = _client(fake, monkeypatch)
    rows = _rows(3)
    assert {"build_commit", "instance", "instance_label", "response_tokens"} <= set(rows[0])
    with caplog.at_level(logging.WARNING, logger="server.core.ch"):
        await c.insert("loom.agent_events", rows, settings={"async_insert": 0})
        await c.insert("loom.agent_events", _rows(2))
    assert [i["rows"] for i in fake.inserts] == [3, 2]
    assert set(fake.inserts[0]["columns"]) == set(_LIVE)
    assert fake.describes == 1  # the DESCRIBE is cached per process
    # one WARNING per (table, column) per process — not per flush
    warned = [r.getMessage() for r in caplog.records if "has no column" in r.getMessage()]
    # Each of these is a column the older table lacks.
    newer = (
        "build_commit",
        "instance",
        "instance_label",
        "consumer",
        "response_tokens",
        "traffic_class",
        "subject",
        "transport",
        "op_class",
        "status_class",
        "budget_ms",
    )
    assert len(warned) == len(newer)
    assert {c for c in newer if any(f"'{c}'" in w for w in warned)} == set(newer)


async def test_a_migration_under_the_cache_is_picked_up_on_the_retry(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The cache says a column is missing; the table has since gained it. The
    projected insert still lands; the next one carries the column."""
    fake = _FakeAsync(_LIVE)
    c = _client(fake, monkeypatch)
    await c.insert("loom.agent_events", _rows(1))
    assert "build_commit" not in fake.inserts[-1]["columns"]
    fake.live = _LIVE + ["build_commit", "instance", "instance_label"]
    ch_mod.describe_cache_clear()  # what a process restart does
    await c.insert("loom.agent_events", _rows(1))
    assert {"build_commit", "instance", "instance_label"} <= set(fake.inserts[-1]["columns"])
    assert fake.inserts[-1]["rows"] == 1


async def test_non_ledger_tables_are_not_projected(monkeypatch: pytest.MonkeyPatch) -> None:
    from server.core import rmt_carry

    monkeypatch.setattr(rmt_carry, "carries", lambda _table: False)  # its own tests
    fake = _FakeAsync(["a"])
    c = _client(fake, monkeypatch)
    with pytest.raises(RuntimeError, match="No such column"):
        await c.insert("loom.memory", [{"a": 1, "b": 2}])
    assert fake.inserts == []


async def test_a_stale_column_set_is_reprojected_after_a_failed_insert(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A column dropped from the table AFTER the describe: the first insert
    fails, the wrapper re-describes and re-projects, the batch lands."""
    fake = _FakeAsync(_LIVE + ["build_commit"])
    c = _client(fake, monkeypatch)
    await c.insert("loom.agent_events", _rows(1))
    assert "build_commit" in fake.inserts[-1]["columns"]
    fake.live = list(_LIVE)  # the column is gone; the cache still lists it
    await c.insert("loom.agent_events", _rows(4))
    assert fake.inserts[-1]["rows"] == 4 and "build_commit" not in fake.inserts[-1]["columns"]


def test_every_ledger_row_carries_instance_build_and_label(monkeypatch: pytest.MonkeyPatch) -> None:
    from server.observability import events_stream as es
    from server.settings import settings

    monkeypatch.setattr(settings, "build_commit", "abc1234")
    monkeypatch.setenv("LOOM_INSTANCE_LABEL", "iso:l3:7873")
    row = _rows(1)[0]
    assert row["build_commit"] == "abc1234" and row["instance_label"] == "iso:l3:7873"
    host, _, pid = row["instance"].rpartition(":")
    assert host and pid.isdigit()
    ev = es.to_event(
        es.BUDGET_VIOLATION_KIND,
        {
            "event_type": "x",
            "org": "o",
            "namespace": "n",
            "actor": "a",
            "subject": "{}",
            "request_id": "r",
            "row_hash": "",
        },
    )
    assert (ev["build_commit"], ev["instance"], ev["instance_label"]) == (
        "abc1234",
        row["instance"],
        "iso:l3:7873",
    )
    # a writer's own stamp wins (a row relayed from another process)
    ev2 = es.to_event(
        es.BUDGET_VIOLATION_KIND,
        {
            "event_type": "x",
            "org": "o",
            "namespace": "n",
            "actor": "a",
            "subject": "{}",
            "request_id": "r",
            "row_hash": "",
            "instance": "other:1",
        },
    )
    assert ev2["instance"] == "other:1"
    attrs = inst.resource_attributes()
    assert attrs["service.instance.id"] == row["instance"]
    assert attrs["loom.build_commit"] == "abc1234" and attrs["loom.instance_label"] == "iso:l3:7873"


def test_schema_compare_is_pending_and_ledger_only() -> None:
    out = ss.compare(
        ["000_a.sql", "001_b.sql", "490_c.sql"], ["000_a.sql", "001_b.sql", "076_gone.sql"]
    )
    assert out == {
        "pending": ["490_c.sql"],
        "ledger_only": ["076_gone.sql"],
        "applied": 2,
        "superseded": 0,
    }


async def test_schema_status_reads_the_ledger_once_per_ttl(monkeypatch: pytest.MonkeyPatch) -> None:
    calls = {"n": 0}

    class _C:
        async def query_all(self, sql: str, params: Any = None) -> list[dict[str, Any]]:
            calls["n"] += 1
            assert "loom._migrations" in sql
            return [{"filename": "000_migrations.sql"}, {"filename": "999_ledger_only.sql\x00"}]

    monkeypatch.setattr(ch_mod, "get_client", lambda: _C())
    ss.reset_cache()
    a = await ss.schema_status(now=100.0)
    b = await ss.schema_status(now=130.0)
    assert calls["n"] == 1 and a == b
    assert a["pending"] == [f for f in ss.repo_migrations() if f != "000_migrations.sql"]
    assert a["ledger_only"] == ["999_ledger_only.sql"] and a["applied"] == 1
    await ss.schema_status(now=170.0)
    assert calls["n"] == 2
    ss.reset_cache()


async def test_schema_status_never_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    class _Down:
        async def query_all(self, sql: str, params: Any = None) -> list[dict[str, Any]]:
            raise RuntimeError("Code: 497. Not enough privileges")

    monkeypatch.setattr(ch_mod, "get_client", lambda: _Down())
    ss.reset_cache()
    out = await ss.schema_status(now=5.0)
    assert out["pending"] is None and out["error"].startswith("RuntimeError")
    ss.reset_cache()
