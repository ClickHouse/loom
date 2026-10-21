"""The consumer is a ledger column, a budget axis and an admin
aggregate; the wire contract is written down and its changelog generated.

Pure: fake drivers and monkeypatched emitters; no ClickHouse."""

from __future__ import annotations

import json
import logging
import time
from datetime import UTC, datetime
from types import SimpleNamespace
from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

import server.core.ch as ch_mod
from server.observability import consumer as cons
from server.observability import ledger
from server.observability.budgets import BudgetOverrides
from server.settings import settings
from server.transport import _common

# ---------------------------------------------------------------- resolution


def test_explicit_consumer_is_bounded_and_printable() -> None:
    assert cons.explicit_consumer({"consumer": "  qa-agent "}) == "qa-agent"
    assert cons.explicit_consumer({"consumer": "a\nb\x00c"}) == "abc"
    assert cons.explicit_consumer({"consumer": "x" * 200}) == "x" * cons.CONSUMER_MAX_CHARS
    assert cons.explicit_consumer({"consumer": 7}) == "7"
    for bad in (
        {},
        None,
        {"consumer": ""},
        {"consumer": "-"},
        {"consumer": True},
        {"consumer": {"a": 1}},
    ):
        assert cons.explicit_consumer(bad) == ""  # type: ignore[arg-type]


def test_subject_class_fallback() -> None:
    assert cons.subject_class(None) == "anonymous"
    assert (
        cons.subject_class(SimpleNamespace(subject="sa:plugin-1a2b", is_service_account=False))
        == "service-account"
    )
    assert (
        cons.subject_class(SimpleNamespace(subject="bot", is_service_account=True))
        == "service-account"
    )
    assert (
        cons.subject_class(SimpleNamespace(subject="avery@example.com", is_service_account=False))
        == "user"
    )
    assert cons.resolve_consumer_and_source({"consumer": "qa"}, None)[0] == "qa"
    assert cons.resolve_consumer_and_source({}, None)[0] == "anonymous"


def test_agent_event_row_carries_the_consumer_column() -> None:
    from server.observability.events import AgentEvent

    row = AgentEvent(
        ts=datetime.now(UTC),
        org="o",
        namespace="n",
        agent="a",
        user_id="u",
        session_id="s",
        operation="memory.get",
        request_id="r",
        latency_ms=1,
        success=True,
        consumer="qa-agent",
    ).to_row()
    assert row["consumer"] == "qa-agent"
    # `traffic_class` follows the consumer; the stamps stay last
    assert "consumer" in row and list(row)[-5:] == [
        "consumer",
        "traffic_class",
        "build_commit",
        "instance",
        "instance_label",
    ]


def test_emit_agent_event_resolves_the_consumer_from_args_then_context_then_token(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from server.observability import events as ev

    captured: list[Any] = []
    monkeypatch.setattr(ev, "get_emitter", lambda: SimpleNamespace(emit=captured.append))
    monkeypatch.setattr(ledger, "get_emitter", lambda: SimpleNamespace(emit=captured.append))
    ident = {"org": "o", "namespace": "n", "agent": "a", "user_id": "-", "session_id": "-"}
    common = dict(
        op_name="memory.get",
        identity=ident,
        request_id="r",
        latency_ms=3,
        success=True,
        error_code="",
    )
    cons.set_current_consumer("")
    ledger.emit_agent_event(raw_args={"consumer": "qa-agent", "memory_key": "k"}, **common)
    cons.set_current_consumer("plugin")
    ledger.emit_agent_event(raw_args={"memory_key": "k"}, **common)
    cons.set_current_consumer("")
    ledger.emit_agent_event(raw_args={"memory_key": "k"}, **common)
    ledger.emit_agent_event(raw_args=None, consumer="explicit", **common)
    assert [e.consumer for e in captured] == ["qa-agent", "plugin", "anonymous", "explicit"]
    # `args` still carries the request's own value, as before
    assert captured[0].args.get("consumer") == "qa-agent"


# ---------------------------------------------------------------- projection

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
    "build_commit",
    "instance",
    "instance_label",
]  # a table with the build stamps but no consumer column


class _FakeAsync:
    def __init__(self, live: list[str]) -> None:
        self.live = list(live)
        self.inserts: list[dict[str, Any]] = []

    async def query(self, sql: str, parameters: Any = None, settings: Any = None) -> Any:
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


def _row(consumer: str) -> dict[str, Any]:
    from server.observability.events import AgentEvent

    return AgentEvent(
        ts=datetime.now(UTC),
        org="o",
        namespace="n",
        agent="a",
        user_id="u",
        session_id="s",
        operation="memory.get",
        request_id="r",
        latency_ms=1,
        success=True,
        consumer=consumer,
    ).to_row()


async def test_consumer_is_projected_away_until_the_column_exists_and_carried_after(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    ch_mod.describe_cache_clear()
    try:
        fake = _FakeAsync(_LIVE)
        c = ch_mod.CHClient()
        monkeypatch.setattr(type(c), "client", property(lambda self: fake))
        with caplog.at_level(logging.WARNING, logger="server.core.ch"):
            await c.insert("loom.agent_events", [_row("qa-agent"), _row("plugin")])
        assert fake.inserts and fake.inserts[0]["rows"] == 2
        assert "consumer" not in fake.inserts[0]["columns"]
        warned = [
            r.getMessage() for r in caplog.records if "has no column 'consumer'" in r.getMessage()
        ]
        assert len(warned) == 1
        # the column now exists: it is carried on the next describe
        fake.live.append("consumer")
        ch_mod.describe_cache_clear()
        await c.insert("loom.agent_events", [_row("qa-agent")])
        assert "consumer" in fake.inserts[-1]["columns"]
    finally:
        ch_mod.describe_cache_clear()


# ---------------------------------------------------------------- budgets


def test_budget_resolution_order_per_consumer_then_op_then_declared(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    b = BudgetOverrides()
    monkeypatch.setattr(settings, "latency_budget_overrides_by_consumer", "")
    assert b.effective("memory.search", 500) == 500
    b.set("memory.search", 800)
    assert b.effective("memory.search", 500) == 800
    assert b.effective("memory.search", 500, consumer="bench") == 800  # no consumer rule yet
    b.set_for_consumer("bench", "*", "3x")
    assert (
        b.effective("memory.search", 500, consumer="bench") == 1500
    )  # multiplier of the DECLARED budget
    assert b.effective("memory.get", 100, consumer="bench") == 300
    b.set_for_consumer("bench", "memory.search", 2000)
    assert (
        b.effective("memory.search", 500, consumer="bench") == 2000
    )  # (consumer, op) beats (consumer, *)
    assert (
        b.effective("memory.search", 500, consumer="plugin") == 800
    )  # another consumer: the op override
    b.clear_for_consumer("bench", "memory.search")
    assert b.effective("memory.search", 500, consumer="bench") == 1500
    assert b.snapshot_by_consumer() == {"bench": {"*": 3.0}}
    with pytest.raises(ValueError):
        b.set_for_consumer("bench", "*", "fast")
    with pytest.raises(ValueError):
        b.set_for_consumer("", "*", 10)


def test_settings_map_loosens_or_tightens_without_a_redeploy(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    b = BudgetOverrides()
    monkeypatch.setattr(
        settings,
        "latency_budget_overrides_by_consumer",
        json.dumps({"bench": {"*": "4x", "memory.set": 5000}, "plugin": {"memory.search": 250}}),
    )
    assert b.effective("memory.search", 500, consumer="bench") == 2000
    assert b.effective("memory.set", 200, consumer="bench") == 5000
    assert b.effective("memory.search", 500, consumer="plugin") == 250  # tightened
    assert b.effective("memory.get", 100, consumer="plugin") == 100  # no rule: declared
    assert b.effective("memory.search", 500, consumer="qa") == 500
    # a runtime override for the consumer wins over the settings map
    b.set_for_consumer("plugin", "memory.search", 900)
    assert b.effective("memory.search", 500, consumer="plugin") == 900
    # a malformed map is one warning and no rule, never an exception
    monkeypatch.setattr(
        settings, "latency_budget_overrides_by_consumer", '{"bench": {"*": "fast"}}'
    )
    with caplog.at_level(logging.WARNING, logger="server.observability.budgets"):
        assert b.effective("memory.search", 500, consumer="bench") == 500
        assert b.effective("memory.get", 100, consumer="bench") == 100
    assert (
        sum("ignored" in r.getMessage() for r in caplog.records) == 1
    )  # parsed once per raw string


def test_effective_budget_ms_applies_the_deployment_multiplier_after_the_consumer_rule(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from server.core import runtime_overrides as ro
    from server.observability import budgets

    written: list[dict[str, Any]] = []

    class _CH:
        async def query_one(self, sql: str, params: Any = None) -> dict[str, Any]:
            return {"v": time.time_ns()}

        async def insert(self, table: str, rows: list[dict[str, Any]], settings: dict[str, Any]):
            assert table == ro.TABLE
            written.extend(rows)

    b = BudgetOverrides()
    monkeypatch.setattr(budgets, "_overrides", b)
    monkeypatch.setattr(ro, "_default_client", _CH)
    monkeypatch.setattr(settings, "latency_budget_overrides_by_consumer", "")
    monkeypatch.setattr(settings, "latency_budget_multiplier", 20.0)
    b.set_for_consumer("bench", "*", "2x")
    cons.set_current_consumer("bench")
    assert budgets.effective_budget_ms("memory.search", 500) == 20000
    cons.set_current_consumer("plugin")
    assert budgets.effective_budget_ms("memory.search", 500) == 10000
    assert budgets.effective_budget_ms("memory.search", 500, consumer="bench") == 20000
    cons.set_current_consumer("")


@pytest.mark.asyncio
async def test_budget_violation_row_carries_the_consumer(monkeypatch: pytest.MonkeyPatch) -> None:
    # The violation is a `budget_violation` event on loom.events;
    # the consumer rides the event's payload (the identity columns are typed).
    from server.observability import events_stream

    rows: list[dict[str, Any]] = []

    def _record(kind: str, row: dict[str, Any]) -> dict[str, Any]:
        rows.append({"kind": kind, **row})
        return row

    monkeypatch.setattr(events_stream, "record", _record)
    monkeypatch.setattr(settings, "budget_violation_audit_interval_s", 0.0)
    _common._BUDGET_LAST_EMIT.clear()
    cons.set_current_consumer("qa-agent")
    await _common.emit_budget_violation(
        op_name="memory.search",
        request_id="r",
        org="o",
        namespace="n",
        agent="a",
        elapsed_ms=900,
        budget_ms=600,
    )
    await _common.emit_budget_violation(
        op_name="memory.search",
        request_id="r",
        org="o",
        namespace="n",
        agent="a",
        elapsed_ms=900,
        budget_ms=600,
        consumer="plugin",
    )
    cons.set_current_consumer("")
    assert [r["consumer"] for r in rows] == ["qa-agent", "plugin"]
    assert all(r["kind"] == "budget_violation" for r in rows)


@pytest.mark.asyncio
async def test_audit_row_has_a_consumer_column_outside_the_hash(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from server.observability import audit

    inserted: list[tuple[str, list[dict[str, Any]]]] = []

    class _CH:
        async def insert(
            self, table: str, rows: list[dict[str, Any]], settings: dict[str, Any]
        ) -> None:
            inserted.append((table, rows))

    monkeypatch.setattr(audit, "get_client", lambda: _CH())
    ts = datetime(2026, 9, 14, 12, 0, 0, tzinfo=UTC)
    cons.set_current_consumer("qa-agent")
    rh = await audit.emit(
        "memory.set", org="o", namespace="n", actor="a", subject={"k": 1}, request_id="r", ts=ts
    )
    await audit.emit(
        "memory.set",
        org="o",
        namespace="n",
        actor="a",
        subject={"k": 1},
        request_id="r",
        ts=ts,
        consumer="plugin",
    )
    cons.set_current_consumer("")
    await audit.emit(
        "memory.set", org="o", namespace="n", actor="a", subject={"k": 1}, request_id="r", ts=ts
    )
    rows = [r for _, batch in inserted for r in batch]
    assert [r["consumer"] for r in rows] == ["qa-agent", "plugin", ""]
    assert rows[0]["row_hash"] == rh and audit.row_matches_hash(rows[0])
    # the same content with another consumer hashes the same: the column is attribution, not payload
    assert rows[0]["row_hash"] == rows[1]["row_hash"]


# ---------------------------------------------------------------- admin op


@pytest.fixture
def client(monkeypatch: pytest.MonkeyPatch) -> TestClient:
    from server.insights.app.admin_routes import admin_router

    monkeypatch.setattr(settings, "auth_required", False)
    app = FastAPI()
    app.include_router(admin_router)
    return TestClient(app)


def test_admin_consumers_joins_calls_and_outcome_feedback_per_window(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    from server.insights.store import admin as admin_repo

    seen: list[tuple[str, int]] = []

    async def _consumers(
        since: int, limit: int, *, org: str | None = None, ch: Any = None
    ) -> list[dict[str, Any]]:
        seen.append(("c", since))
        return [
            {
                "consumer_key": "qa-agent",
                "calls": 1000,
                "errors": 163,
                "server_errors": 163,
                "error_rate": 0.163,
                "p50_ms": 412.0,
                "p95_ms": 1800.0,
                "spend_usd": 1.25,
                "outcome_calls": 40,
                "namespaces": 1,
                "ops_distinct": 6,
                "last_seen": datetime(2026, 9, 14, 12, 0, tzinfo=UTC),
            },
            {
                "consumer_key": "service-account",
                "calls": 10,
                "errors": 0,
                "server_errors": 0,
                "error_rate": 0.0,
                "p50_ms": 90.0,
                "p95_ms": 120.0,
                "spend_usd": 0.0,
                "outcome_calls": 0,
                "namespaces": 2,
                "ops_distinct": 2,
                "last_seen": None,
            },
        ]

    async def _outcomes(
        since: int, limit: int, *, org: str | None = None, ch: Any = None
    ) -> list[dict[str, Any]]:
        seen.append(("o", since))
        return [
            {
                "consumer_key": "qa-agent",
                "outcome_rows": 40,
                "outcome_positive": 31,
                "outcome_negative": 9,
                "shunt_rows": 0,
                "tokens_saved": 0,
            },
            {
                "consumer_key": "claude-code-shunt",
                "outcome_rows": 5,
                "outcome_positive": 0,
                "outcome_negative": 0,
                "shunt_rows": 5,
                "tokens_saved": 12000,
            },
        ]

    monkeypatch.setattr(admin_repo, "consumers", _consumers)
    monkeypatch.setattr(admin_repo, "consumer_outcomes", _outcomes)
    r = client.post("/v1/admin/consumers", json={})
    assert r.status_code == 200, r.text
    body = r.json()
    assert [w["since_seconds"] for w in body["windows"]] == [86400, 604800]
    assert sorted(seen) == [("c", 86400), ("c", 604800), ("o", 86400), ("o", 604800)]
    rows = {row["consumer"]: row for row in body["windows"][0]["rows"]}
    qa = rows["qa-agent"]
    assert qa["errors"] == 163 and qa["server_errors"] == 163 and qa["error_rate"] == 0.163
    assert qa["p50_ms"] == 412.0 and qa["spend_usd"] == 1.25
    assert qa["outcome_calls"] == 40 and qa["outcome_rows"] == 40 and qa["outcome_positive"] == 31
    assert qa["last_seen"].startswith("2026-09-14T12:00")
    assert rows["service-account"]["outcome_rows"] == 0
    # a client that only ever reported outcomes still appears
    assert (
        rows["claude-code-shunt"]["calls"] == 0
        and rows["claude-code-shunt"]["tokens_saved"] == 12000
    )
    assert [row["consumer"] for row in body["windows"][0]["rows"]][0] == "qa-agent"  # by calls desc
    assert client.post("/v1/admin/consumers", json={"windows_seconds": []}).status_code == 422
    assert client.post("/v1/admin/consumers", json={"windows_seconds": [10**9]}).status_code == 422


def test_admin_budget_set_takes_a_consumer(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    from server.core import runtime_overrides as ro
    from server.observability import budgets

    written: list[dict[str, Any]] = []

    class _CH:
        async def query_one(self, sql: str, params: Any = None) -> dict[str, Any]:
            return {"v": time.time_ns()}

        async def insert(self, table: str, rows: list[dict[str, Any]], settings: dict[str, Any]):
            assert table == ro.TABLE
            written.extend(rows)

    b = BudgetOverrides()
    monkeypatch.setattr(budgets, "_overrides", b)
    monkeypatch.setattr(ro, "_default_client", _CH)
    monkeypatch.setattr(settings, "latency_budget_overrides_by_consumer", "")
    r = client.post("/v1/admin/budget.set", json={"op": "*", "consumer": "bench", "budget": "3x"})
    assert r.status_code == 200 and r.json()["consumer"] == "bench" and r.json()["budget"] == "3x"
    assert b.effective("memory.search", 500, consumer="bench") == 1500
    r = client.post(
        "/v1/admin/budget.set", json={"op": "memory.search", "consumer": "plugin", "budget_ms": 250}
    )
    assert r.status_code == 200 and r.json()["budget_ms"] == 250
    assert b.effective("memory.search", 500, consumer="plugin") == 250
    r = client.post("/v1/admin/budget.set", json={"op": "memory.set", "budget_ms": 10**400})
    assert r.status_code == 422 and b.get("memory.set") is None
    assert (
        client.post(
            "/v1/admin/budget.set", json={"op": "*", "consumer": "bench", "budget": "slow"}
        ).status_code
        == 422
    )
    assert (
        client.post("/v1/admin/budget.set", json={"op": "*", "consumer": "bench"}).status_code
        == 422
    )
    r = client.post("/v1/admin/budget.set", json={"op": "*", "consumer": "bench", "clear": True})
    assert r.status_code == 200 and r.json()["cleared"] is True
    assert b.effective("memory.search", 500, consumer="bench") == 500
    # the per-op path is unchanged
    r = client.post("/v1/admin/budget.set", json={"op": "memory.search", "budget_ms": 800})
    assert r.status_code == 200 and b.get("memory.search") == 800
    # every accepted change is a loom.runtime_overrides row, so every process adopts it
    assert [(w["kind"], json.loads(w["key"]), json.loads(w["value"])) for w in written] == [
        (ro.BUDGET_CONSUMER, ["bench", "*"], "3x"),
        (ro.BUDGET_CONSUMER, ["plugin", "memory.search"], 250),
        (ro.BUDGET_CONSUMER, ["bench", "*"], None),
        (ro.BUDGET_OP, ["memory.search"], 800),
    ]


# ---------------------------------------------------------------- capabilities + compat


@pytest.mark.asyncio
async def test_ops_capabilities_lists_every_served_op_with_fields_role_budget_and_tier() -> None:
    from server.operations.registry import ALL_OPERATIONS, OPERATIONS_BY_NAME

    op = OPERATIONS_BY_NAME["ops.capabilities"]
    out = await op.handler(op.input_model())  # an empty body is enough to discover the surface
    names = [o.name for o in out.ops]
    assert names == sorted(o.name for o in ALL_OPERATIONS) and "ops.capabilities" in names
    by = {o.name: o for o in out.ops}
    assert (
        by["memory.search"].required_role == "VIEWER"
        and "query" in by["memory.search"].request_fields
    )
    assert by["memory.set"].budget_p99_ms == 200 and by["memory.set"].required_role == "WRITER"
    assert (
        by["code.search"].mcp_tier == "code" and by["memory.pin"].mcp_tier == "core"
    )  # pin is core
    assert all(o.request_fields == sorted(o.request_fields) for o in out.ops)
    assert out.consumer_field == "consumer"
    assert op.required_role.name == "VIEWER" and op.skip_namespace_authz and op.idempotent


# ---------------------------------------------------------------- changelog


def _snapshot(ops: dict[str, dict[str, Any]], build: str) -> dict[str, Any]:
    full = {}
    for name, spec in ops.items():
        full[name] = {
            "required_role": "VIEWER",
            "budget_p99_ms": 500,
            "mcp_tier": "extended",
            "idempotent": True,
            "audit": False,
            "request_fields": [],
            "response_fields": [],
            **spec,
        }
    return {"build_commit": build, "ops": full}


def test_wire_changelog_diffs_two_snapshots(tmp_path: Any) -> None:
    from scripts.codegen import wire_changelog as mod

    a = _snapshot(
        {
            "memory.search": {
                "request_fields": ["org", "query", "top_k"],
                "response_fields": ["results"],
            },
            "memory.set": {"budget_p99_ms": 30, "request_fields": ["org", "value"]},
            "memory.old": {"request_fields": ["org"]},
        },
        "aaaa1111",
    )
    b = _snapshot(
        {
            "memory.search": {
                "request_fields": ["include_tags", "org", "query", "top_k"],
                "response_fields": ["abstain", "results"],
                "mcp_tier": "core",
            },
            "memory.set": {"budget_p99_ms": 200, "request_fields": ["org"]},
            "ops.capabilities": {"request_fields": ["consumer", "org"]},
        },
        "bbbb2222",
    )
    d = mod.diff_snapshots(a, b)
    assert d["ops_added"] == ["ops.capabilities"] and d["ops_removed"] == ["memory.old"]
    assert d["changed"]["memory.search"] == {
        "request_fields_added": ["include_tags"],
        "response_fields_added": ["abstain"],
        "mcp_tier": {"from": "extended", "to": "core"},
    }
    assert d["changed"]["memory.set"] == {
        "request_fields_removed": ["value"],
        "budget_p99_ms": {"from": 30, "to": 200},
    }
    assert d["counts"]["request_fields_added"] == 1 and d["counts"]["request_fields_removed"] == 1
    md = mod.render_markdown(d, a, b)
    assert "aaaa1111 → bbbb2222" in md and "`include_tags`" in md and "`ops.capabilities`" in md
    assert "### Removed operations" in md and "`memory.old`" in md
    (tmp_path / "a.json").write_text(json.dumps(a))
    (tmp_path / "b.json").write_text(json.dumps(b))
    assert mod.main(["--diff", str(tmp_path / "a.json"), str(tmp_path / "b.json")]) == 0
    (tmp_path / "c.json").write_text(
        json.dumps(_snapshot({"memory.search": {"request_fields": ["org", "query"]}}, "c"))
    )
    (tmp_path / "d.json").write_text(
        json.dumps(_snapshot({"memory.search": {"request_fields": ["org", "query", "z"]}}, "d"))
    )
    assert mod.main(["--diff", str(tmp_path / "c.json"), str(tmp_path / "d.json"), "--json"]) == 0


@pytest.mark.asyncio
async def test_consumer_aggregate_falls_back_to_args_for_rows_without_the_column_value() -> None:
    """Rows written before the `consumer` column existed carry it only in
    `args`, so the aggregate reads the column with the args fallback."""
    from server.insights.store import admin as admin_repo

    class _CH:
        def __init__(self) -> None:
            self.calls: list[tuple[str, dict[str, Any]]] = []

        async def query_all(self, sql: str, params: Any = None) -> list[dict[str, Any]]:
            self.calls.append((sql, params or {}))
            return []

    ch = _CH()
    await admin_repo.consumers(3600, 10, org="dev", ch=ch)  # type: ignore[arg-type]
    assert "if(consumer = '', args['consumer'], consumer)" in ch.calls[0][0]
    assert ch.calls[0][1]["org"] == "dev"


def test_scoped_token_reads_capabilities_over_http(monkeypatch: pytest.MonkeyPatch) -> None:
    """A scoped service-account token (authority per namespace, no
    org/namespace in a capabilities call) reads the build's capabilities —
    200, not IDENTITY_INCOMPLETE — so a discovery poll succeeds wherever the
    same token's memory reads and writes do."""
    from server.auth.jwt import mint_token
    from server.transport import http as http_mod
    from server.transport.http import build_router

    monkeypatch.setattr(settings, "auth_required", True)
    monkeypatch.setattr(type(settings), "auth_actually_required", lambda self: True)
    monkeypatch.setattr(http_mod, "_emit_event", lambda *a, **k: None)
    tok = mint_token(
        subject="sa:alerts-bot",
        org="dev",
        namespaces=["alerts-bot"],
        roles=[],
        ns_roles={"alerts-bot": "writer"},
        ttl_seconds=300,
        is_service_account=True,
    )
    app = FastAPI()
    app.include_router(build_router())
    c = TestClient(app)
    r = c.post("/v1/ops.capabilities", json={}, headers={"Authorization": f"Bearer {tok}"})
    assert r.status_code == 200, r.text
    assert "ops.capabilities" in {o["name"] for o in r.json()["ops"]}
