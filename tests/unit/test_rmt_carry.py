"""A version write carries the columns its build does not know.

During a rolling upgrade the N-1 build writes version rows of loom.memory and
its sibling ReplacingMergeTree tables without the columns N's migration added.
Under FINAL those columns would read back as their defaults; the insert funnel
fills them from the newest stored version of the key instead.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import re
import time
from types import SimpleNamespace
from typing import Any

import pytest

import server.memory.store.rows
import server.memory.store.shared  # noqa: F401
from server.core import ch as ch_mod
from server.core import rmt_carry
from tests.unit.schema_ddl import column_names, ddl

TABLE = "loom.memory"


class _FakeCH:
    """Answers the statements rmt_carry issues."""

    def __init__(self, live: dict[str, str], stored: list[dict[str, Any]]) -> None:
        self.live = live
        self.stored = stored
        self.sql: list[str] = []
        self.migrations = 27
        self.altered = "2026-01-01 00:00:00"
        self.acks: dict[str, str] = {}
        self.columns_down = False
        self.acks_down = False
        self.ack_ages: dict[str, int] = {}
        # Processes with a carried insert running (system.processes); None: unreadable.
        self.running: set[str] | None = set()

    async def query_one(self, sql: str, params: Any = None) -> dict[str, Any]:
        if "AS ms" in sql:  # the server clock: this host's
            return {"ms": int(time.time() * 1000)}
        self.sql.append(sql)
        assert "loom._migrations" in sql and "system.tables" in sql
        assert ("loom", "memory") in (params or {})["keys"]
        return {"n": self.migrations, "at": None, "altered": self.altered}

    async def query_all(
        self, sql: str, params: dict[str, Any] | None = None
    ) -> list[dict[str, Any]]:
        self.sql.append(sql)
        if "carry_acks" in sql:
            me = (params or {})["me"]
            return [
                {"process": p, "fingerprints": f, "age": self.ack_ages.get(p, 0)}
                for p, f in self.acks.items()
                if p != me
            ]
        if "system.processes" in sql:
            if self.running is None:
                raise ConnectionError("down")
            return [{"process": p} for p in sorted(self.running)]
        if "system.columns" in sql:
            if self.columns_down:
                raise ConnectionError("down")
            keys = (params or {})["keys"]
            if ("loom", "memory") not in keys:
                return []
            return [{"database": "loom", "table": "memory", "name": c} for c in self.live]
        keys = {tuple(k) for k in (params or {})["keys"]}
        return [r for r in self.stored if TABLE_STATE.key_of(r) in keys]

    async def insert(self, table: str, rows: list[dict[str, Any]], **_: Any) -> None:
        assert table == rmt_carry.ACK_TABLE
        if self.acks_down:
            raise ConnectionError("down")
        for r in rows:
            self.acks[r["process"]] = r["fingerprints"]


TABLE_STATE = rmt_carry._TABLES[TABLE]
KNOWN = TABLE_STATE.known


def _row(key: str, **extra: Any) -> dict[str, Any]:
    base = {c: "" for c in KNOWN}
    base.update(org="o", namespace="n", agent="a", user_id="-", session_id="-", memory_key=key)
    base.update(extra)
    return base


@pytest.fixture(autouse=True)
def _fresh_cache() -> Any:
    rmt_carry.reset_for_tests()
    yield
    rmt_carry.reset_for_tests()


def _live(*extra: str) -> dict[str, str]:
    live = {c: "String" for c in KNOWN}
    live.update({c: ("UInt32" if c == "new_n" else "String") for c in extra})
    return live


def _carry(ch: _FakeCH, rows: list[dict[str, Any]]) -> list[list[dict[str, Any]]]:
    return asyncio.run(rmt_carry.carry_unknown_columns(ch, TABLE, rows))


def _column_reads(ch: _FakeCH) -> int:
    return sum("system.columns" in sql for sql in ch.sql)


def test_no_unknown_column_writes_rows_unchanged_without_a_lookup() -> None:
    ch = _FakeCH(_live(), [])
    rows = [_row("k1")]
    assert _carry(ch, rows) == [rows] and _carry(ch, rows)[0] is rows
    # The first write reads the column lists; the next ones use them as they are.
    assert _column_reads(ch) == 1


def test_unknown_columns_carry_from_the_stored_version_and_a_new_key_omits_them() -> None:
    """A new key's row leaves the column out, so ClickHouse computes its
    DEFAULT, one that names another column included."""
    ch = _FakeCH(_live("new_col", "new_n"), [_row("k1", new_col="from-N", new_n=7)])
    carried, fresh = _carry(ch, [_row("k1"), _row("k2")])
    assert [r["memory_key"] for r in carried] == ["k1"]
    assert (carried[0]["new_col"], carried[0]["new_n"]) == ("from-N", 7)
    assert [r["memory_key"] for r in fresh] == ["k2"]
    assert "new_col" not in fresh[0] and "new_n" not in fresh[0]


def test_a_value_the_row_sets_is_kept() -> None:
    ch = _FakeCH(_live("new_col"), [_row("k1", new_col="stored")])
    assert _carry(ch, [_row("k1", new_col="written")])[0][0]["new_col"] == "written"


def test_a_known_column_a_row_omits_is_not_carried() -> None:
    """The builder's column list, not the row, says what this build knows."""
    ch = _FakeCH(_live(), [])
    row = _row("k1")
    row.pop("evidence")
    assert "evidence" not in _carry(ch, [row])[0][0]


async def test_a_migration_rereads_the_column_lists_in_the_background(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(rmt_carry, "_POLL_S", 0.01)
    ch = _FakeCH(_live(), [_row("k1", new_col="from-N")])
    assert len(await rmt_carry.carry_unknown_columns(ch, TABLE, [_row("k1")])) == 1
    ch.live = _live("new_col")
    await asyncio.sleep(0.05)
    # The watcher's first poll reads them again; then, the ledger unmoved, never.
    assert _column_reads(ch) == 2
    ch.migrations += 1
    await asyncio.sleep(0.05)
    assert _column_reads(ch) == 3
    sql_before = len(ch.sql)
    (carried,) = await rmt_carry.carry_unknown_columns(ch, TABLE, [_row("k1")])
    assert carried[0]["new_col"] == "from-N"
    # The write itself read only the stored version, not system.columns.
    assert not any("system.columns" in sql for sql in ch.sql[sql_before:])


async def test_an_alter_rereads_the_column_lists_before_its_ledger_row_lands(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(rmt_carry, "_POLL_S", 0.01)
    ch = _FakeCH(_live(), [_row("k1", new_col="from-N")])
    await rmt_carry.carry_unknown_columns(ch, TABLE, [_row("k1")])
    await asyncio.sleep(0.05)
    ch.live = _live("new_col")
    ch.altered = "2026-01-01 00:00:05"
    await asyncio.sleep(0.05)
    assert _column_reads(ch) == 3
    (carried,) = await rmt_carry.carry_unknown_columns(ch, TABLE, [_row("k1")])
    assert carried[0]["new_col"] == "from-N"


def test_ch_insert_writes_each_batch_the_carry_returns(monkeypatch: pytest.MonkeyPatch) -> None:
    batches = [[{"org": "o", "c": 1}], [{"org": "o"}]]
    written: list[list[dict[str, Any]]] = []

    async def fake_carry(ch: Any, table: str, rows: Any) -> Any:
        return batches

    async def record(self: Any, table: str, rows: Any, *_a: Any) -> None:
        written.append(rows)

    async def no_guard(*_a: Any, **_k: Any) -> None:
        return None

    monkeypatch.setattr(rmt_carry, "carry_unknown_columns", fake_carry)
    monkeypatch.setattr(ch_mod, "guard_pending_schema", no_guard)
    monkeypatch.setattr(ch_mod.CHClient, "_insert_batch", record)
    asyncio.run(ch_mod.CHClient().insert(TABLE, [{"org": "o"}]))
    assert written == batches


def test_ch_insert_carries_for_every_registered_table(monkeypatch: pytest.MonkeyPatch) -> None:
    seen: list[str] = []

    async def fake_carry(ch: Any, table: str, rows: Any) -> Any:
        seen.append(table)
        raise RuntimeError("stop")

    async def no_guard(*_a: Any, **_k: Any) -> None:
        return None

    monkeypatch.setattr(rmt_carry, "carry_unknown_columns", fake_carry)
    monkeypatch.setattr(ch_mod, "guard_pending_schema", no_guard)
    client = ch_mod.CHClient()
    for table in rmt_carry._TABLES:
        with pytest.raises(RuntimeError, match="stop"):
            asyncio.run(client.insert(table, [{"org": "o"}]))
    assert seen == list(rmt_carry._TABLES)


@pytest.mark.parametrize("table", sorted(rmt_carry._TABLES))
def test_builder_columns_are_the_table_so_the_steady_state_takes_the_fast_path(table: str) -> None:
    """A stored column outside the builder would send every write of this
    build down the lookup path."""
    t = rmt_carry._TABLES[table]
    name = table.removeprefix("loom.")
    assert set(column_names(name, materialized=False)) == set(t.known)
    assert set(t.key) <= t.known
    assert re.search(r"ORDER BY \(" + ", ".join(t.key) + r"\)", ddl(name))


def test_unreadable_column_lists_refuse_the_write_until_a_read_lands(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A version row written without the lists could drop a column it does not
    know: the write waits for a read (503, retryable), and the watcher retries
    though the schema stamp has not moved."""
    from server.core.errors import BackendUnavailableError

    monkeypatch.setattr(rmt_carry, "_POLL_S", 0.01)
    ch = _FakeCH(_live("new_col"), [_row("k1", new_col="from-N")])
    ch.columns_down = True

    async def go() -> list[list[dict[str, Any]]]:
        with pytest.raises(BackendUnavailableError):
            await rmt_carry.carry_unknown_columns(ch, TABLE, [_row("k1")])
        ch.columns_down = False
        await asyncio.sleep(0.05)
        return await rmt_carry.carry_unknown_columns(ch, TABLE, [_row("k1")])

    (carried,) = asyncio.run(go())
    assert carried[0]["new_col"] == "from-N"


async def test_a_write_in_flight_holds_back_the_newer_lists_acknowledgement(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(rmt_carry, "_POLL_S", 0.01)
    ch = _FakeCH(_live(), [])
    await rmt_carry.carry_unknown_columns(ch, TABLE, [_row("k1")])
    await asyncio.sleep(0.03)  # the watcher has its first stamp
    old = rmt_carry.acknowledged()
    async with rmt_carry.writing():  # took the lists before the ALTER
        ch.live = _live("new_col")
        ch.altered = "2026-01-01 00:00:05"
        await asyncio.sleep(0.05)
        assert TABLE_STATE.stored is not None and "new_col" in TABLE_STATE.stored
        assert rmt_carry.acknowledged() == old
    assert rmt_carry.acknowledged() != old
    await asyncio.sleep(0.05)
    assert ch.acks[rmt_carry.process_id()] == json.dumps(rmt_carry.acknowledged(), sort_keys=True)


async def test_a_boot_waits_until_every_writer_carries_the_new_column(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The boot that migrated writes the new column only once every other live
    process acknowledges the lists that hold it; a version write by such a
    process right after the ALTER then carries the stored value."""
    monkeypatch.setattr(rmt_carry, "_POLL_S", 0.01)
    ch = _FakeCH(_live(), [_row("k1", new_col="preserve-me")])
    old = json.dumps({TABLE: rmt_carry.fingerprint(_live())})
    ch.acks["writer-2"] = old
    await rmt_carry.await_acks(ch, timeout_s=0)  # nothing changed: no wait
    ch.live = _live("new_col")  # the ALTER; writer-2 still carries by the old lists
    with pytest.raises(RuntimeError, match="writer-2"):
        await rmt_carry.await_acks(ch, timeout_s=0)
    waiting = asyncio.create_task(rmt_carry.await_acks(ch, timeout_s=5))
    await asyncio.sleep(0.05)
    assert not waiting.done()
    ch.acks["writer-2"] = json.dumps({TABLE: rmt_carry.fingerprint(_live("new_col"))})
    await asyncio.wait_for(waiting, 1)
    # This process, by the same rule, carries the value once it has the new lists.
    (carried,) = await rmt_carry.carry_unknown_columns(ch, TABLE, [_row("k1")])
    assert carried[0]["new_col"] == "preserve-me"


async def test_a_process_enrolls_before_its_first_read_and_withdraws_at_shutdown() -> None:
    ch = _FakeCH(_live(), [])
    await rmt_carry.carry_unknown_columns(ch, TABLE, [_row("k1")])
    assert TABLE in json.loads(ch.acks[rmt_carry.process_id()])
    await rmt_carry.withdraw(ch)
    assert ch.acks[rmt_carry.process_id()] == "{}"


async def test_an_alter_between_the_first_read_and_the_watchers_start_is_read(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(rmt_carry, "_POLL_S", 0.01)
    ch = _FakeCH(_live(), [_row("k1", new_col="from-N")])
    await rmt_carry.carry_unknown_columns(ch, TABLE, [_row("k1")])
    ch.live = _live("new_col")  # before the watcher's first stamp, which then never moves
    await asyncio.sleep(0.05)
    (carried,) = await rmt_carry.carry_unknown_columns(ch, TABLE, [_row("k1")])
    assert carried[0]["new_col"] == "from-N"


async def test_unreadable_acknowledgements_hold_the_boot_until_the_deadline() -> None:
    class _Down(_FakeCH):
        async def query_all(self, sql: str, params: Any = None) -> list[dict[str, Any]]:
            if "carry_acks" in sql:
                raise ConnectionError("reset by peer")
            return await super().query_all(sql, params)

    with pytest.raises(RuntimeError, match="could not be read"):
        await rmt_carry.await_acks(_Down(_live(), []), timeout_s=0)


async def test_a_process_that_never_wrote_holds_no_boot() -> None:
    """Its first write reads the lists the migration left: nothing to wait for."""
    ch = _FakeCH(_live(), [])
    assert rmt_carry.acknowledged() == {}
    ch.acks["idle"] = json.dumps(rmt_carry.acknowledged())
    ch.live = _live("new_col")
    await rmt_carry.await_acks(ch, timeout_s=0)


async def test_a_write_waits_for_its_first_acknowledgement() -> None:
    """Unenrolled, a writer is invisible to a boot's barrier: it must not write."""
    from server.core.errors import BackendUnavailableError

    ch = _FakeCH(_live(), [])
    ch.acks_down = True
    with pytest.raises(BackendUnavailableError, match="acknowledgement"):
        await rmt_carry.carry_unknown_columns(ch, TABLE, [_row("k1")])
    ch.acks_down = False
    assert await rmt_carry.carry_unknown_columns(ch, TABLE, [_row("k1")])
    assert rmt_carry.process_id() in ch.acks


async def test_a_stale_acknowledgement_that_cannot_be_renewed_refuses_the_write(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from server.core.errors import BackendUnavailableError

    ch = _FakeCH(_live(), [])
    await rmt_carry.carry_unknown_columns(ch, TABLE, [_row("k1")])
    value, sent = rmt_carry._published or ("", 0.0)
    rmt_carry._published = (value, sent - rmt_carry._ACK_LIVE_S)  # the refreshes failed
    ch.acks_down = True
    with pytest.raises(BackendUnavailableError, match="acknowledgement"):
        await rmt_carry.carry_unknown_columns(ch, TABLE, [_row("k1")])


async def test_a_process_resuming_past_its_acknowledgement_reads_the_new_schema_first() -> None:
    """Paused past expiry, a boot no longer waits for it and may have added a
    column: its next write reads the lists and acknowledges before writing."""
    ch = _FakeCH(_live(), [_row("k1", new_col="preserve-me")])
    await rmt_carry.carry_unknown_columns(ch, TABLE, [_row("k1")])
    if rmt_carry._watcher is not None:
        rmt_carry._watcher.cancel()  # paused: nothing refreshes the lists or the ack
    value, sent = rmt_carry._published or ("", 0.0)
    rmt_carry._published = (value, sent - rmt_carry._ACK_LIVE_S)
    ch.live = _live("new_col")  # the boot's ALTER, after it stopped counting us
    published_before = len(ch.sql)
    (carried,) = await rmt_carry.carry_unknown_columns(ch, TABLE, [_row("k1")])
    assert carried[0]["new_col"] == "preserve-me"
    assert any("system.columns" in q for q in ch.sql[published_before:])
    assert rmt_carry._fresh()


async def test_a_table_registered_after_the_last_read_is_read_before_its_first_write(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A store module imported late registers its table after the lists were read
    and acknowledged: its first write reads them instead of being refused."""
    ch = _FakeCH(_live(), [])
    await rmt_carry.carry_unknown_columns(ch, TABLE, [_row("k1")])  # fresh and published
    TABLE_STATE.stored = None  # as for a table registered since
    assert len(await rmt_carry.carry_unknown_columns(ch, TABLE, [_row("k1")])) == 1
    assert TABLE_STATE.stored is not None


async def test_concurrent_writes_that_find_the_acknowledgement_stale_renew_it_once() -> None:
    ch = _FakeCH(_live(), [])
    await rmt_carry.carry_unknown_columns(ch, TABLE, [_row("k1")])
    published: list[str] = []
    real = ch.insert

    async def counting(table: str, rows: list[dict[str, Any]], **kw: Any) -> None:
        published.append(table)
        await asyncio.sleep(0)  # the other writes run while this one is in flight
        await real(table, rows, **kw)

    ch.insert = counting  # type: ignore[method-assign]
    assert rmt_carry._published is not None
    rmt_carry._published = (rmt_carry._published[0], 0.0)  # aged past _ADMIT_S
    reads = _column_reads(ch)
    await asyncio.gather(
        *(rmt_carry.carry_unknown_columns(ch, TABLE, [_row(f"k{i}")]) for i in range(8))
    )
    assert len(published) == 2 and _column_reads(ch) == reads + 1  # publish, read, publish


class _Driver:
    """The insert driver behind CHClient.insert: records each send's settings."""

    def __init__(self) -> None:
        self.sent: list[tuple[str, dict[str, Any]]] = []
        self.statements: list[str] = []
        self.lands = 1
        self.stray = 0

    async def query(self, sql: str, parameters: Any = None, settings: Any = None) -> Any:
        class _Described:
            column_names = ["name", "type", "default_type"]
            result_rows = [[c, "String", ""] for c in KNOWN]

        return _Described()

    async def insert(self, table: str, data: Any, settings: Any = None, **_: Any) -> None:
        self.sent.append((table, dict(settings or {})))

    async def create_insert_context(
        self, table: str, columns: Any, column_type_names: Any = None, settings: Any = None
    ) -> Any:
        from clickhouse_connect.datatypes.registry import get_from_name
        from clickhouse_connect.driver.insert import InsertContext

        types = [get_from_name(t) for t in column_type_names or ["String"] * len(columns)]
        return InsertContext(table, columns, types, settings=settings)

    async def data_insert(self, ctx: Any) -> Any:
        """The guarded send: the statement the first block opens with, its
        settings, and as many rows written as `self.lands` says; the server's
        refusal when `self.stray` (the guard's throwIf)."""
        (first, *_rest) = list(ctx.next_block())
        self.statements.append(first.prefix.decode())
        self.sent.append((ctx.table, dict(ctx.settings or {})))
        if getattr(self, "late", False):
            raise RuntimeError("Code: 395. DB::Exception: rmt carry deadline.")
        if self.stray:
            raise RuntimeError(
                "Code: 395. DB::Exception: rmt carry fence. (FUNCTION_THROW_IF_VALUE_IS_NON_ZERO)"
            )
        return SimpleNamespace(written_rows=self.lands)


def _fenced_client(monkeypatch: pytest.MonkeyPatch, before_send: Any) -> tuple[Any, _Driver]:
    """A CHClient over `_Driver` whose carried insert was admitted on a fresh
    acknowledgement; `before_send()` runs between admission and the send."""
    driver = _Driver()

    async def _no_retry(self: Any, op_name: str, fn: Any, **_kw: Any) -> Any:
        return await fn()

    async def _types(self: Any, table: str, columns: list[str]) -> Any:
        before_send()
        return None

    monkeypatch.setattr(ch_mod.CHClient, "_with_retry", _no_retry)
    monkeypatch.setattr(ch_mod.CHClient, "_column_type_names", _types)
    monkeypatch.setattr(type(ch_mod.CHClient()), "client", property(lambda self: driver))

    async def _stray(self: Any, sql: str, params: Any = None) -> Any:
        assert "system.columns" in sql
        return {"n": driver.stray}

    monkeypatch.setattr(ch_mod.CHClient, "query_one", _stray)
    ch_mod.describe_cache_clear()
    TABLE_STATE.stored = frozenset(KNOWN)
    rmt_carry._read_ok = True
    rmt_carry._published = (json.dumps({}), rmt_carry.time.monotonic())
    rmt_carry._server_anchor = (
        rmt_carry.time.time(),
        rmt_carry.time.monotonic(),
    )  # this host's clock
    return ch_mod.CHClient(), driver


def test_an_insert_under_a_lease_starts_by_its_end_carried_or_not(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """However long they waited after the lease was checked, the inserts sent under it
    (gathered, as a publish sends them) start within the transit allowance of its end: a
    carried one in its fenced statement, any other in one with the start guard alone, not
    buffered, and each runs no longer than the write limit an erasure waits out."""
    client, driver = _fenced_client(monkeypatch, lambda: None)
    monkeypatch.setattr(rmt_carry, "_skew_at", rmt_carry.time.monotonic())
    ended = rmt_carry.time.monotonic() - 10

    async def _publish() -> None:
        with rmt_carry.under_lease(ended):
            await asyncio.gather(
                client.insert(TABLE, [_row("k1")]),
                client.insert("loom.audit_log", [{"row_hash": "h"}], settings={"async_insert": 1}),
            )

    asyncio.run(_publish())
    expected_ms = (time.time() - 10 + 15) * 1000
    sent = {
        t: (stmt, settings)
        for stmt, (t, settings) in zip(driver.statements, driver.sent, strict=True)
    }
    for table in (TABLE, "loom.audit_log"):
        statement, settings = sent[table]
        [deadline_ms] = re.findall(r"now64\(3\)\) > (\d+)", statement)
        assert int(deadline_ms) == pytest.approx(expected_ms, abs=1000), table
        assert settings["async_insert"] == 0 and settings["max_execution_time"] == 60
    assert "system.columns" in sent[TABLE][0]
    assert "system.columns" not in sent["loom.audit_log"][0]


def test_the_acknowledgement_itself_is_not_fenced(monkeypatch: pytest.MonkeyPatch) -> None:
    """A renewal publishes from inside the write it renews for: its own insert
    must not be refused for the staleness it is curing."""
    client, driver = _fenced_client(monkeypatch, lambda: None)
    rmt_carry._published = ("{}", rmt_carry.time.monotonic() - 60)

    async def _publish_inside_a_write() -> None:
        async with rmt_carry.writing():
            await client.insert(rmt_carry.ACK_TABLE, [{"process": "p", "fingerprints": "{}"}])

    asyncio.run(_publish_inside_a_write())
    ((table, sent),) = driver.sent
    assert table == rmt_carry.ACK_TABLE and sent.get("async_insert") != 0


def test_a_carried_batch_is_sent_guarded_by_the_lists_it_was_built_against(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """ClickHouse judges the guard on arrival: the statement lands the rows only
    while the table has no stored column outside the lists this write took."""
    client, driver = _fenced_client(monkeypatch, lambda: None)
    asyncio.run(client.insert(TABLE, [_row("k1")]))
    (statement,) = driver.statements
    assert statement.startswith(f"INSERT INTO {TABLE} (") and "FROM input('" in statement
    assert "FROM system.columns WHERE database = 'loom' AND table = 'memory'" in statement
    assert all(f"'{c}'" in statement for c in KNOWN) and statement.endswith("FORMAT Native\n")
    assert "throwIf(" in statement
    assert driver.sent[0][1]["max_execution_time"] > 0  # bounded even with no class limit


def test_a_guarded_batch_that_landed_nothing_after_an_alter_is_refused(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Held in transit past an ALTER, the server refuses it: the caller must
    retry with fresh lists, never take it for a stored write."""
    client, driver = _fenced_client(monkeypatch, lambda: None)
    driver.lands, driver.stray = 0, 1
    from server.core.errors import BackendUnavailableError

    with pytest.raises(BackendUnavailableError, match="changed under this write"):
        asyncio.run(client.insert(TABLE, [_row("k1")]))
    # The next write reads the lists again instead of meeting the same refusal.
    assert rmt_carry._TABLES[TABLE].stored is None
    # The server answered that query (its own refusal): nothing is held back.
    assert not rmt_carry._pinned and not rmt_carry._inflight


def test_a_host_clock_step_does_not_move_the_servers_clock(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An NTP step or a VM resume between measures: deadlines and releases keep
    to the server's clock, which advances with the monotonic one."""
    rmt_carry._server_anchor = (1_000_000.0, rmt_carry.time.monotonic())
    before = rmt_carry.server_now()
    assert before == pytest.approx(1_000_000.0, abs=1.0)
    real = rmt_carry.time.time
    monkeypatch.setattr(rmt_carry.time, "time", lambda: real() + 3600)
    assert rmt_carry.server_now() == pytest.approx(before, abs=1.0)


def test_a_guarded_resend_the_dedup_token_already_landed_is_a_success(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client, driver = _fenced_client(monkeypatch, lambda: None)
    driver.lands, driver.stray = 0, 0  # the table is as the lists say: deduplicated
    asyncio.run(client.insert(TABLE, [_row("k1")]))


class _ProcessesCH:
    """Answers the settle: `running` of the ids still run (None: unreadable)."""

    def __init__(self, running: int | None) -> None:
        self.running = running
        self.killed: list[list[str]] = []

    async def execute(self, sql: str, params: Any = None, **_: Any) -> None:
        assert sql.startswith("KILL QUERY") and sql.endswith("SYNC")
        self.killed.append(list((params or {})["ids"]))

    async def query_one(self, sql: str, params: Any = None) -> dict[str, Any]:
        if "AS ms" in sql:  # the server clock: this host's
            return {"ms": int(time.time() * 1000)}
        assert "system.processes" in sql
        if self.running is None:
            raise ConnectionError("down")
        return {"n": self.running}


async def _cut_off_write(deadline: float, max_s: float = 60.0) -> None:
    """A carried write whose send went out (to start by `deadline`, run for at
    most `max_s`), then whose caller failed."""
    with contextlib.suppress(asyncio.CancelledError):
        async with rmt_carry.writing():
            rmt_carry.note_send("q-old", deadline, max_s)
            raise asyncio.CancelledError


async def test_a_cut_off_write_stays_in_flight_until_no_query_of_it_can_run(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Cancelled after its send, the INSERT may run on the server, or still be in
    transit and start later: no newer lists are acknowledged until it cannot.
    A count of 0 before its start deadline proves nothing (it may not have
    started); after it, a kill and a count of 0 release it."""
    clock = [1000.0]
    monkeypatch.setattr(rmt_carry, "server_now", lambda: clock[0])
    rmt_carry._gen, rmt_carry._prints[1] = 1, {TABLE: "old"}
    await _cut_off_write(deadline=1015.0)
    rmt_carry._gen, rmt_carry._prints[2] = 2, {TABLE: "new"}  # the ALTER, read
    ch = _ProcessesCH(running=0)
    await rmt_carry._settle(ch)  # before the deadline: 0 running is no proof
    assert rmt_carry.acknowledged() == {TABLE: "old"} and ch.killed == [["q-old"]]
    clock[0] = 1016.0
    ch.running = 1
    await rmt_carry._settle(ch)
    assert rmt_carry.acknowledged() == {TABLE: "old"}
    ch.running = None  # unreadable: held, and named on /v1/health
    await rmt_carry._settle(ch)
    assert rmt_carry.acknowledged() == {TABLE: "old"}
    assert rmt_carry.pinned_overdue()[0]["query_ids"] == ["q-old"]
    ch.running = 0
    await rmt_carry._settle(ch)
    assert rmt_carry.acknowledged() == {TABLE: "new"} and not rmt_carry._inflight


async def test_a_cut_off_write_never_seen_running_is_released_by_the_clock(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Still in transit, it is in no system.processes: it is held until it can
    neither start (its deadline) nor still run (its max_execution_time)."""
    clock = [1000.0]
    monkeypatch.setattr(rmt_carry, "server_now", lambda: clock[0])
    rmt_carry._gen, rmt_carry._prints[1] = 1, {TABLE: "old"}
    await _cut_off_write(deadline=1015.0, max_s=60.0)
    ch = _ProcessesCH(running=None)  # and nothing can be counted
    clock[0] = 1070.0  # past the deadline, inside the limit
    await rmt_carry._settle(ch)
    assert rmt_carry._inflight
    clock[0] = 1015.0 + 60.0 + rmt_carry._RELEASE_MARGIN_S + 0.1
    await rmt_carry._settle(ch)
    assert not rmt_carry._inflight and not rmt_carry._pinned


def test_a_batch_carries_its_start_deadline_and_one_too_late_is_refused(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client, driver = _fenced_client(monkeypatch, lambda: None)
    asyncio.run(client.insert(TABLE, [_row("k1")]))
    (statement,) = driver.statements
    assert (
        "toUnixTimestamp64Milli(now64(3)) >" in statement and ch_mod.DEADLINE_MESSAGE in statement
    )
    driver.late = True
    from server.core.errors import BackendUnavailableError

    with pytest.raises(BackendUnavailableError, match="too late"):
        asyncio.run(client.insert(TABLE, [_row("k2")]))
    assert TABLE_STATE.stored is not None  # its lists are still right


async def test_a_write_that_failed_before_any_send_holds_nothing() -> None:
    rmt_carry._gen, rmt_carry._prints[1] = 1, {TABLE: "old"}
    with contextlib.suppress(RuntimeError):
        async with rmt_carry.writing():
            raise RuntimeError("lists unreadable")
    assert not rmt_carry._inflight and not rmt_carry._pinned


async def test_a_write_the_server_answered_releases_at_once() -> None:
    rmt_carry._gen, rmt_carry._prints[1] = 1, {TABLE: "old"}
    async with rmt_carry.writing():
        rmt_carry.note_send("q-done")
    assert not rmt_carry._inflight and not rmt_carry._pinned


def test_a_held_write_is_a_health_problem() -> None:
    from server.observability.problems import HealthState, collect_problems
    from server.settings import settings

    pinned = ({"query_ids": ["q-old"], "error": "system.processes cannot be read"},)
    codes = {p.code: p for p in collect_problems(settings, HealthState(carry_pinned=pinned))}
    assert "q-old" in codes["carry_write_pinned"].message


def test_a_reply_that_the_same_query_still_runs_keeps_the_write_pinned(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A driver re-send after a reset meets its own first attempt still running
    (216): that reply proves nothing ended, so the write stays held."""
    client, driver = _fenced_client(monkeypatch, lambda: None)

    async def _still_running(ctx: Any) -> Any:
        raise RuntimeError("Code: 216. DB::Exception: Query with id = q is already running.")

    driver.data_insert = _still_running
    with pytest.raises(RuntimeError, match="already running"):
        asyncio.run(client.insert(TABLE, [_row("k1")]))
    assert rmt_carry._pinned


async def test_a_boot_waits_for_the_insert_a_killed_writer_left_running() -> None:
    """A writer SIGKILLed with a carried insert in flight keeps no ack and no pin,
    but the insert runs on: the boot waits until the server shows it gone."""
    ch = _FakeCH(_live("new_col"), [])
    ch.acks["dead-host:7"] = json.dumps({TABLE: rmt_carry.fingerprint(_live())})
    ch.ack_ages["dead-host:7"] = rmt_carry._ACK_LIVE_S + 5  # expired
    ch.running = {"dead-host:7"}
    with pytest.raises(RuntimeError, match="dead-host:7 \\(gone"):
        await rmt_carry.await_acks(ch, timeout_s=0)
    ch.running = set()
    await rmt_carry.await_acks(ch, timeout_s=0)


async def test_a_boot_waits_for_a_withdrawn_process_whose_insert_still_runs() -> None:
    ch = _FakeCH(_live("new_col"), [])
    ch.acks["old-host:7"] = "{}"  # withdrew at shutdown, live acknowledgement
    ch.running = {"old-host:7"}
    with pytest.raises(RuntimeError, match="old-host:7 \\(gone"):
        await rmt_carry.await_acks(ch, timeout_s=0)
    ch.running = set()
    await rmt_carry.await_acks(ch, timeout_s=0)


def test_a_write_cancelled_mid_send_keeps_the_acknowledgement_at_shutdown(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A shutdown that cancelled a carried insert after its send proves nothing
    about the query on the server: withdrawing would let a boot pass it."""
    client, driver = _fenced_client(monkeypatch, lambda: None)
    sending = asyncio.Event()

    async def _hang(ctx: Any) -> Any:
        list(ctx.next_block())
        sending.set()
        await asyncio.sleep(30)

    driver.data_insert = _hang
    acks = _FakeCH(_live(), [])
    acks.acks[rmt_carry.process_id()] = "last"

    async def _shutdown() -> None:
        write = asyncio.create_task(client.insert(TABLE, [_row("k1")]))
        await sending.wait()
        write.cancel()
        with pytest.raises(asyncio.CancelledError):
            await write
        assert rmt_carry._pinned, "the cut-off write is held"
        await rmt_carry.withdraw(acks)

    asyncio.run(_shutdown())
    assert acks.acks[rmt_carry.process_id()] == "last", "not withdrawn while the insert may run"


async def test_an_unreadable_process_list_holds_the_boot_for_a_recently_gone_writer() -> None:
    ch = _FakeCH(_live(), [])
    ch.acks["dead-host:7"] = json.dumps({TABLE: rmt_carry.fingerprint(_live())})
    ch.ack_ages["dead-host:7"] = rmt_carry._ACK_LIVE_S + 5
    ch.running = None
    with pytest.raises(RuntimeError, match="dead-host:7"):
        await rmt_carry.await_acks(ch, timeout_s=0)
    ch.acks["dead-host:7"] = "{}"  # it withdrew at shutdown: nothing of it runs
    await rmt_carry.await_acks(ch, timeout_s=0)


def test_a_carried_send_names_its_process_in_its_query_id(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client, driver = _fenced_client(monkeypatch, lambda: None)
    asyncio.run(client.insert(TABLE, [_row("k1")]))
    qid = driver.sent[0][1]["query_id"]
    assert qid.startswith(f"{rmt_carry.CARRY_QUERY_PREFIX}{rmt_carry.process_id()}/")


def test_a_boot_waits_long_enough_to_outwait_a_dead_writers_last_insert(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A longer write timeout lengthens how long a dead writer's insert may run;
    the boot's wait follows it instead of failing first."""
    from server.settings import settings

    assert rmt_carry._boot_wait_s() == rmt_carry.ACK_WAIT_S  # the default write limit
    monkeypatch.setattr(settings, "ch_timeout_write_s", 300)
    assert rmt_carry._boot_wait_s() > rmt_carry._writer_bound_s() > rmt_carry.ACK_WAIT_S
