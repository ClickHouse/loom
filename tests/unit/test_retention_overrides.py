"""Tests for server.storage.retention_overrides.

Pins the contract:

  - Default is indefinite. Both retention_*_days and tiering_*_warm_days
    zero, no per-namespace rows → no TTL; a TTL left by an earlier pass
    is removed.
  - Global retention only → simple INTERVAL N DAY DELETE.
  - Global tiering only → INTERVAL W DAY TO VOLUME 'warm'.
  - Per-namespace overrides → multiIf with one case per override,
    falling through to global default (or 2099 if no global).
  - Combined retention + tiering → two TTL clauses, move first.
  - loom.memory uses coalesce(expires_at, created_at + N) so caller
    expiry wins over operator backstop.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest

from server.storage import retention_overrides
from tests.unit.clickhouse_local import CLICKHOUSE, ddl, run


@dataclass
class _FakeSettings:
    """Stand-in for server.settings.settings; carries every retention
    + tiering setting exercised in tests. Unset fields default to 0."""

    retention_audit_log_days: int = 0
    retention_agent_events_days: int = 0
    retention_memory_days: int = 0
    retention_memory_embeddings_days: int = 0
    retention_conversations_days: int = 0
    retention_chunks_days: int = 0
    retention_documents_days: int = 0

    tiering_audit_log_warm_days: int = 0
    tiering_agent_events_warm_days: int = 0
    tiering_memory_warm_days: int = 0
    tiering_memory_embeddings_warm_days: int = 0
    tiering_conversations_warm_days: int = 0
    tiering_chunks_warm_days: int = 0
    tiering_documents_warm_days: int = 0

    tiering_warm_volume_name: str = "warm"


class StubCH:
    def __init__(self) -> None:
        self.execute_calls: list[str] = []

    async def execute(self, sql: str, params: Any = None) -> None:
        self.execute_calls.append(sql)


# ----------------------------------------------------------------------
# compute_alters — pure function
# ----------------------------------------------------------------------


def test_compute_alters_empty_when_no_globals_no_overrides() -> None:
    """Default boot: every global setting is 0 + no per-namespace
    rows. No ALTERs emitted; schema defaults stand."""
    assert retention_overrides.compute_alters(_FakeSettings()) == []


def test_compute_alters_global_retention_only() -> None:
    out = retention_overrides.compute_alters(
        _FakeSettings(
            retention_audit_log_days=3650,
        )
    )
    assert len(out) == 1
    _, sql, _ = out[0]
    assert "ALTER TABLE loom.audit_log MODIFY TTL ts + INTERVAL 3650 DAY DELETE" in sql
    assert "TO VOLUME" not in sql
    assert "multiIf" not in sql


def test_compute_alters_global_tiering_only() -> None:
    out = retention_overrides.compute_alters(
        _FakeSettings(
            tiering_audit_log_warm_days=90,
        )
    )
    _, sql, _ = out[0]
    assert "TO VOLUME 'warm'" in sql
    assert "DELETE" not in sql


def test_compute_alters_per_namespace_override_uses_multiif() -> None:
    """One per-namespace override with no global → multiIf dispatch
    on (org, namespace), falling through to a 2099 far-future literal
    so unmatched rows live indefinitely."""
    out = retention_overrides.compute_alters(
        _FakeSettings(),
        namespace_overrides=[
            {
                "org": "clickhouse",
                "namespace": "compliance",
                "retention_days": 3650,
                "warm_days": 0,
            },
        ],
    )
    # Override applies to every managed table — one ALTER per table.
    assert len(out) == 7
    audit_alter = next(s for t, s, _ in out if t.table == "loom.audit_log")
    assert "multiIf" in audit_alter
    assert "org = 'clickhouse'" in audit_alter
    assert "namespace = 'compliance'" in audit_alter
    assert "INTERVAL 3650 DAY" in audit_alter
    # 2099 fallback for unmatched rows
    assert "2099-12-31" in audit_alter


def test_compute_alters_override_with_global_falls_through_to_global() -> None:
    """Override + global both set: matching rows use override; others
    use the global default (not 2099)."""
    out = retention_overrides.compute_alters(
        _FakeSettings(retention_audit_log_days=365),
        namespace_overrides=[
            {
                "org": "clickhouse",
                "namespace": "compliance",
                "retention_days": 3650,
                "warm_days": 0,
            },
        ],
    )
    audit_alter = next(s for t, s, _ in out if t.table == "loom.audit_log")
    assert "INTERVAL 3650 DAY" in audit_alter  # override
    assert "INTERVAL 365 DAY" in audit_alter  # global fallback
    assert "2099" not in audit_alter  # global covers fallback


def test_compute_alters_memory_uses_coalesce_for_caller_expiry() -> None:
    """loom.memory's TTL must preserve caller expires_at via coalesce.
    With override 180: coalesce(expires_at, created_at + INTERVAL 180 DAY)."""
    out = retention_overrides.compute_alters(
        _FakeSettings(),
        namespace_overrides=[
            {"org": "x", "namespace": "y", "retention_days": 180, "warm_days": 0},
        ],
    )
    mem_alter = next(s for t, s, _ in out if t.table == "loom.memory")
    assert "coalesce(expires_at, created_at + INTERVAL 180 DAY)" in mem_alter


def test_compute_alters_combined_move_first_then_delete() -> None:
    """When both retention and tiering are set, the TTL clause must
    list move first (so CH evaluates it before delete)."""
    out = retention_overrides.compute_alters(
        _FakeSettings(
            retention_audit_log_days=365,
            tiering_audit_log_warm_days=90,
        )
    )
    _, sql, _ = out[0]
    move_pos = sql.find("TO VOLUME")
    delete_pos = sql.find("DELETE")
    assert 0 <= move_pos < delete_pos


def test_compute_alters_warm_volume_name_configurable() -> None:
    out = retention_overrides.compute_alters(
        _FakeSettings(
            tiering_audit_log_warm_days=90,
            tiering_warm_volume_name="s3",
        )
    )
    _, sql, _ = out[0]
    assert "TO VOLUME 's3'" in sql


def test_compute_alters_treats_negative_global_as_zero() -> None:
    """Operator typo (e.g. -1) must NOT round-trip to malformed SQL."""
    out = retention_overrides.compute_alters(
        _FakeSettings(
            retention_audit_log_days=-1,
        )
    )
    assert out == []


def test_compute_alters_sql_quotes_org_namespace_safely() -> None:
    """Per-namespace overrides' org/namespace are embedded as string
    literals. A stray apostrophe must not break the ALTER."""
    out = retention_overrides.compute_alters(
        _FakeSettings(),
        namespace_overrides=[
            {
                "org": "tenant's-org",
                "namespace": "ns",
                "retention_days": 30,
                "warm_days": 0,
            },
        ],
    )
    audit_alter = next(s for t, s, _ in out if t.table == "loom.audit_log")
    assert "tenant\\'s-org" in audit_alter


def test_compute_alters_per_namespace_applies_to_all_tables() -> None:
    """One namespace override produces one ALTER per managed table —
    retention is uniform across tables for a namespace, by design."""
    out = retention_overrides.compute_alters(
        _FakeSettings(),
        namespace_overrides=[
            {"org": "x", "namespace": "y", "retention_days": 90, "warm_days": 0},
        ],
    )
    tables = sorted(t.table for t, _, _ in out)
    assert tables == [
        "loom.agent_events",
        "loom.audit_log",
        "loom.chunks",
        "loom.conversations",
        "loom.documents",
        "loom.memory",
        "loom.memory_embeddings",
    ]


# ----------------------------------------------------------------------
# apply — runs ALTERs against a stub CH
# ----------------------------------------------------------------------


@pytest.mark.asyncio
async def test_apply_uses_provided_overrides_without_ch_query() -> None:
    """Passing namespace_overrides explicitly bypasses the CH read
    (test harness path; the boot hook fetches them itself)."""
    ch = _DiffCH({})
    n = await retention_overrides.apply(
        _FakeSettings(retention_audit_log_days=365),
        ch=ch,
        namespace_overrides=[],
        leader=True,
    )
    assert n > 0
    assert any("loom.audit_log" in c for c in ch.execute_calls)


@pytest.mark.asyncio
async def test_apply_per_namespace_emits_alters_for_all_tables() -> None:
    """One namespace override → one ALTER per managed table (8)."""
    ch = _DiffCH({})
    n = await retention_overrides.apply(
        _FakeSettings(),
        ch=ch,
        namespace_overrides=[
            {"org": "x", "namespace": "y", "retention_days": 90, "warm_days": 0},
        ],
        leader=True,
    )
    assert n == 7


@pytest.mark.asyncio
async def test_apply_continues_after_one_failure(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A bad ALTER on one table mustn't kill the rest."""

    class FlakyCH(_DiffCH):
        async def execute(self, sql: str, params: Any = None) -> None:
            if "loom.documents" in sql:
                raise RuntimeError("CH refused this one")
            await super().execute(sql, params)

    ch = FlakyCH({})
    with caplog.at_level(logging.ERROR, logger="server.storage.retention_overrides"):
        n = await retention_overrides.apply(
            _FakeSettings(retention_audit_log_days=365, retention_documents_days=180),
            ch=ch,
            namespace_overrides=[],
            leader=True,
        )
    assert n == 1  # audit_log succeeded; documents failed
    assert any("loom.audit_log" in c for c in ch.execute_calls)


# ----------------------------------------------------------------------
# Leader-only, change-detected
# ----------------------------------------------------------------------


class _DiffCH(StubCH):
    """A CH stub that knows each table's current TTL and formats queries
    as the identity, so the comparison is textual."""

    def __init__(self, current: dict[str, str], *, fail_lookup: bool = False) -> None:
        super().__init__()
        self.current = current
        self.fail_lookup = fail_lookup
        self.lookups = 0

    pending: dict[str, int] = {}

    async def query_one(self, sql: str, params: Any = None) -> dict[str, Any] | None:
        params = params or {}
        if "formatQuerySingleLine" in sql:
            return {"f": params["q"]}
        if "system.mutations" in sql:
            return {"n": self.pending.get(params["name"], 0)}
        if "system.tables" in sql:
            self.lookups += 1
            if self.fail_lookup:
                raise RuntimeError("Code: 60. DB::Exception: unknown table")
            ttl = self.current.get(params["name"])
            return {
                "engine_full": f"MergeTree ORDER BY x{(' TTL ' + ttl) if ttl else ''} SETTINGS index_granularity = 8192"
            }
        return None


def _planned_ttl(table: str) -> str:
    alters = retention_overrides.compute_alters(
        _FakeSettings(retention_audit_log_days=365, retention_memory_days=30),
        namespace_overrides=[],
    )
    for target, alter_sql, _ in alters:
        if target.table == table:
            return alter_sql.split(" MODIFY TTL ", 1)[1]
    raise AssertionError(table)


@pytest.mark.asyncio
async def test_unchanged_ttl_issues_no_alter_and_changed_ttl_does() -> None:
    settings_obj = _FakeSettings(retention_audit_log_days=365, retention_memory_days=30)
    ch = _DiffCH(
        {"audit_log": _planned_ttl("loom.audit_log"), "memory": "created_at + toIntervalDay(7)"}
    )
    n = await retention_overrides.apply(settings_obj, ch=ch, namespace_overrides=[], leader=True)
    assert n == 1
    assert all("loom.memory " in c for c in ch.execute_calls), ch.execute_calls  # audit_log skipped
    assert ch.lookups == len(retention_overrides.TARGETS)  # each table read once


@pytest.mark.asyncio
async def test_apply_zero_state_issues_no_alter_on_tables_without_ttl() -> None:
    """No globals, no per-namespace rows and no TTL on any table: indefinite
    retention, zero ALTERs issued."""
    ch = _DiffCH({})
    n = await retention_overrides.apply(_FakeSettings(), ch=ch, namespace_overrides=[], leader=True)
    assert n == 0
    assert ch.execute_calls == []


@pytest.mark.asyncio
async def test_clearing_the_last_override_removes_the_ttl_it_left() -> None:
    """A namespace that sets retention_days back to 0 (and no global window)
    leaves no desired TTL; the TTL an earlier pass set must go, or its rows
    keep being deleted."""
    left = "multiIf(org = 'o' AND namespace = 'ns1', created_at + toIntervalDay(30), now()) DELETE"
    ch = _DiffCH({"memory": left, "audit_log": left})
    n = await retention_overrides.apply(_FakeSettings(), ch=ch, namespace_overrides=[], leader=True)
    assert n == 2
    assert sorted(ch.execute_calls) == [
        "ALTER TABLE loom.audit_log REMOVE TTL",
        "ALTER TABLE loom.memory REMOVE TTL",
    ]


@pytest.mark.asyncio
async def test_a_table_ttl_is_not_removed_when_its_lookup_fails() -> None:
    """REMOVE TTL on a table without one is an error, so an unreadable
    system.tables row defers the removal to the next pass."""
    ch = _DiffCH({}, fail_lookup=True)
    n = await retention_overrides.apply(_FakeSettings(), ch=ch, namespace_overrides=[], leader=True)
    assert n == 0 and ch.execute_calls == []


@pytest.mark.asyncio
async def test_a_ttl_removal_waits_for_unfinished_mutations() -> None:
    left = "created_at + toIntervalDay(30) DELETE"
    ch = _DiffCH({"memory": left, "audit_log": left})
    ch.pending = {"memory": 2}
    n = await retention_overrides.apply(_FakeSettings(), ch=ch, namespace_overrides=[], leader=True)
    assert n == 1
    assert ch.execute_calls == ["ALTER TABLE loom.audit_log REMOVE TTL"]


@pytest.mark.asyncio
async def test_a_ttl_an_overlapping_pass_removed_first_is_not_an_error(
    caplog: pytest.LogCaptureFixture,
) -> None:
    class _AlreadyRemoved(_DiffCH):
        async def execute(self, sql: str, params: Any = None) -> None:
            raise RuntimeError(
                "Code: 36. DB::Exception: Table doesn't have any table TTL expression, "
                "cannot remove. (BAD_ARGUMENTS)"
            )

    ch = _AlreadyRemoved({"memory": "created_at + toIntervalDay(30) DELETE"})
    with caplog.at_level(logging.INFO, logger="server.storage.retention_overrides"):
        n = await retention_overrides.apply(
            _FakeSettings(), ch=ch, namespace_overrides=[], leader=True
        )
    assert n == 0
    assert not [r for r in caplog.records if r.levelno >= logging.ERROR]


def test_no_managed_table_has_a_schema_ttl() -> None:
    """apply issues REMOVE TTL on a managed table with no window set, which
    would strip a TTL the schema defines; a schema TTL on one of these tables
    needs retention_overrides to restore it instead."""
    schema = Path("schemas/schema.sql").read_text()
    for target in retention_overrides.TARGETS:
        m = re.search(rf"CREATE TABLE IF NOT EXISTS {re.escape(target.table)}\b.*?;", schema, re.S)
        assert m, target.table
        assert not re.search(r"^\s*TTL\b", m.group(0), re.M), (
            f"{target.table} has a schema TTL that server/storage/retention_overrides.py "
            "would remove when no window is set"
        )


class _LocalCH:
    """The two calls apply makes, run on clickhouse-local in a database kept under
    `path`, so each statement sees what the one before it did."""

    def __init__(self, path: Path) -> None:
        self.path = path
        self.execute_calls: list[str] = []

    async def query_one(self, sql: str, params: Any = None) -> dict[str, Any] | None:
        rows = run([], sql, params, path=self.path)
        return rows[0] if rows else None

    async def execute(self, sql: str, params: Any = None) -> None:
        self.execute_calls.append(sql)
        run([sql], "SELECT 1", path=self.path)

    def engine_full(self, table: str) -> str:
        (row,) = run(
            [], f"SELECT engine_full FROM system.tables WHERE name = '{table}'", path=self.path
        )
        return str(row["engine_full"])


@pytest.mark.skipif(CLICKHOUSE is None, reason="needs the clickhouse binary")
@pytest.mark.asyncio
async def test_a_cleared_window_removes_the_ttl_on_a_real_table(tmp_path: Path) -> None:
    ch = _LocalCH(tmp_path)
    run([ddl("audit_log")], "SELECT 1", path=tmp_path)
    set_ttl = await retention_overrides.apply(
        _FakeSettings(),
        ch=ch,
        namespace_overrides=[{"org": "o", "namespace": "ns1", "retention_days": 30}],
        leader=True,
    )
    # the other managed tables do not exist here and read as having no TTL
    assert set_ttl == 1 and " TTL " in ch.engine_full("audit_log")

    removed = await retention_overrides.apply(
        _FakeSettings(), ch=ch, namespace_overrides=[], leader=True
    )
    assert removed == 1
    assert ch.execute_calls[-1] == "ALTER TABLE loom.audit_log REMOVE TTL"
    assert " TTL " not in ch.engine_full("audit_log")

    # an overlapping pass sends the same statement; the text apply matches is ClickHouse's
    with pytest.raises(AssertionError, match=retention_overrides._NO_TTL_TO_REMOVE):
        run(["ALTER TABLE loom.audit_log REMOVE TTL"], "SELECT 1", path=tmp_path)


@pytest.mark.asyncio
async def test_non_leader_issues_nothing() -> None:
    ch = _DiffCH({})
    n = await retention_overrides.apply(
        _FakeSettings(retention_audit_log_days=365), ch=ch, namespace_overrides=[], leader=False
    )
    assert n == 0 and ch.execute_calls == [] and ch.lookups == 0


@pytest.mark.asyncio
async def test_comparison_failure_is_fail_open() -> None:
    ch = _DiffCH({}, fail_lookup=True)
    n = await retention_overrides.apply(
        _FakeSettings(retention_audit_log_days=365), ch=ch, namespace_overrides=[], leader=True
    )
    assert n >= 1 and any("loom.audit_log" in c for c in ch.execute_calls)


@pytest.mark.asyncio
async def test_current_ttl_matches_reads_the_table_and_compares_formatted_forms() -> None:
    ch = _DiffCH({"audit_log": "ts + toIntervalDay(365)"})
    assert (
        await retention_overrides.current_ttl_matches(
            ch, "loom.audit_log", "ALTER TABLE loom.audit_log MODIFY TTL ts + toIntervalDay(365)"
        )
        is True
    )
    assert (
        await retention_overrides.current_ttl_matches(
            ch, "loom.audit_log", "ALTER TABLE loom.audit_log MODIFY TTL ts + toIntervalDay(30)"
        )
        is False
    )
    assert (
        await retention_overrides.current_ttl_matches(
            ch, "loom.memory", "ALTER TABLE loom.memory MODIFY TTL created_at + toIntervalDay(30)"
        )
        is False
    )  # no TTL on the table
    assert (
        await retention_overrides.current_ttl_matches(
            _DiffCH({}, fail_lookup=True), "loom.memory", "ALTER TABLE loom.memory MODIFY TTL x"
        )
        is None
    )


# ----------------------------------------------------------------------
# A MODIFY TTL is a metadata change; the leader reconciles
# ----------------------------------------------------------------------


@pytest.mark.asyncio
async def test_alter_is_issued_metadata_only_by_default() -> None:
    """The executed statement carries materialize_ttl_after_modify = 0
    while the planned SQL (what current_ttl_matches compares) stays bare."""
    settings_obj = _FakeSettings(retention_audit_log_days=365)
    ch = _DiffCH({})
    n = await retention_overrides.apply(settings_obj, ch=ch, namespace_overrides=[], leader=True)
    assert n == 1
    (stmt,) = ch.execute_calls
    assert stmt.endswith(retention_overrides.MATERIALIZE_OFF)
    planned = retention_overrides.compute_alters(settings_obj, namespace_overrides=[])[0][1]
    assert "materialize_ttl_after_modify" not in planned
    assert stmt == planned + retention_overrides.MATERIALIZE_OFF


@pytest.mark.asyncio
async def test_reconcile_worker_applies_as_leader(monkeypatch: pytest.MonkeyPatch) -> None:
    from server.memory.workers import retention_reconcile

    seen: dict[str, Any] = {}

    async def _apply(settings_obj: Any, ch: Any = None, **kw: Any) -> int:
        seen.update(kw)
        return 3

    monkeypatch.setattr(retention_overrides, "apply", _apply)
    out = await retention_reconcile.run_pass()
    assert out.alters_issued == 3
    assert seen == {"leader": True}


def test_register_handler_no_longer_alters_tables() -> None:
    """namespace.register does not re-apply table TTLs inline; the worker owns
    them (an inline re-apply issues a MODIFY TTL mutation per registration)."""
    import inspect

    from server.identity.app import namespace as handler

    src = inspect.getsource(handler.handle_namespace_register)
    assert "retention_overrides" not in src or "0510" in src
    assert "_ro.apply(" not in src


def test_reconcile_worker_is_registered_by_default(monkeypatch: pytest.MonkeyPatch) -> None:
    from server.workers import registry

    names = [w.name for w in registry.enabled_workers()]
    assert "retention_reconcile" in names


@pytest.mark.asyncio
async def test_alter_is_deferred_while_the_table_has_unfinished_mutations() -> None:
    """A table with mutations still running is left alone this pass (the
    ALTER would queue behind them and, deep enough, be refused with
    Code: 517); a table with an empty queue is altered."""
    ch = _DiffCH({})
    ch.pending = {"audit_log": 2}
    n = await retention_overrides.apply(
        _FakeSettings(retention_audit_log_days=365, retention_memory_days=30),
        ch=ch,
        namespace_overrides=[],
        leader=True,
    )
    assert n == 1
    assert all("loom.memory " in c for c in ch.execute_calls), ch.execute_calls


@pytest.mark.asyncio
async def test_mutation_queue_read_failure_is_fail_open() -> None:
    class _NoMutationsView(_DiffCH):
        async def query_one(self, sql: str, params: Any = None) -> dict[str, Any] | None:
            if "system.mutations" in sql:
                raise RuntimeError("Code: 497. DB::Exception: not enough privileges")
            return await super().query_one(sql, params)

    ch = _NoMutationsView({})
    n = await retention_overrides.apply(
        _FakeSettings(retention_audit_log_days=365), ch=ch, namespace_overrides=[], leader=True
    )
    assert n == 1


def test_every_managed_table_reads_its_own_retention_and_tiering_settings() -> None:
    """Each target's accessors read the settings named after its table: set
    one table's pair alone and the plan alters that table only."""
    from server.settings import Settings

    assert retention_overrides.compute_alters(Settings()) == []
    for target in retention_overrides.TARGETS:
        leaf = target.table.removeprefix("loom.")
        fake = _FakeSettings(**{f"retention_{leaf}_days": 30, f"tiering_{leaf}_warm_days": 7})
        plan = retention_overrides.compute_alters(fake)
        assert [t.table for t, _, _ in plan] == [target.table]
        assert plan[0][2]["retention_global"] == 30 and plan[0][2]["tiering_global"] == 7
