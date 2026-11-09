"""The Postgres memory authority's pure parts: the boot durability verdict, the
migration plan, the row codec, the admission's gap checks and the erasure
classification of every Postgres table."""

from __future__ import annotations

import asyncio
import contextlib
import json
import math
import re
import time
import uuid
from collections.abc import AsyncIterator
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from clickhouse_connect.driver.exceptions import DatabaseError

from server.core import ch_errors, pg, pg_lease, shielded
from server.core.errors import BackendTimeoutError, BackendUnavailableError, OverloadedError
from server.core.paths import PG_MIGRATIONS_DIR
from server.core.pg_profile import durability_verdict
from server.core.schema import pg_migrate
from server.erasure.store.steps_pg import PG_CENSUS, PG_NO_MEMORY_CONTENT
from server.memory.app import pg_admission
from server.memory.store import pg_memory
from server.memory.store.pg_publish import ClaimExpired
from server.memory.store.rows import MemoryRow, identity_key


@pytest.mark.parametrize(
    ("profile", "names", "sync", "refused", "warned"),
    [
        ("synchronous", "FIRST 1 (s1)", 1, False, False),
        ("synchronous", "", None, True, False),
        ("local", "", None, False, True),
        ("local", "ANY 1 (s1, s2)", 2, False, False),
        ("synchronous", "FIRST 1 (s1)", 0, False, True),
        ("synchronous", "FIRST 1 (s1)", None, False, True),
        ("synchronous", "ANY 2 (s1, s2, s3)", 1, False, True),
        ("synchronous", "FIRST 2 (s1, s2)", 2, False, False),
        ("synchronous", "2 (s1, s2)", 1, False, True),
    ],
)
def test_the_durability_verdict(
    profile: str, names: str, sync: int | None, refused: bool, warned: bool
) -> None:
    v = durability_verdict(profile, names, sync)
    assert bool(v.refusal) is refused
    assert bool(v.warnings) is warned


@pytest.mark.parametrize(
    ("names", "sync", "reported"),
    [
        ("FIRST 1 (s1)", 1, "synchronous"),
        ("FIRST 1 (s1)", 0, "waiting"),
        ("", None, "primary_only"),
        ("FIRST 1 (s1)", None, None),
        ("ANY 2 (s1, s2, s3)", 1, "waiting"),
        ("s1, s2", 1, "synchronous"),
        ("2 (s1, s2)", 1, "waiting"),
    ],
)
def test_health_reports_how_commits_are_acknowledged(
    monkeypatch: pytest.MonkeyPatch, names: str, sync: int | None, reported: str | None
) -> None:
    from server.core import pg_profile

    async def standby_state() -> tuple[str, int | None]:
        return names, sync

    monkeypatch.setattr(pg_profile, "standby_state", standby_state)
    assert asyncio.run(pg_profile.standby_now()) == reported


def _journal(tmp_path: Path, *files: tuple[str, str]) -> list[Path]:
    for name, text in files:
        (tmp_path / name).write_text(text)
    return pg_migrate.migration_files(tmp_path)


def test_the_plan_applies_what_the_ledger_lacks_in_order(tmp_path: Path) -> None:
    files = _journal(tmp_path, ("002_b.sql", "B"), ("001_a.sql", "A"), ("010_c.sql", "C"))
    applied = {"001_a.sql": pg_migrate.checksum(files[0])}
    assert [f.name for f in pg_migrate.plan(applied, files)] == ["002_b.sql", "010_c.sql"]


def test_an_edited_applied_file_refuses(tmp_path: Path) -> None:
    files = _journal(tmp_path, ("001_a.sql", "A"))
    with pytest.raises(pg_migrate.MigrationDrift, match="edited"):
        pg_migrate.plan({"001_a.sql": "0" * 64}, files)


def test_a_lost_applied_file_refuses_but_a_newer_builds_file_is_served(tmp_path: Path) -> None:
    files = _journal(tmp_path, ("002_b.sql", "B"))
    with pytest.raises(pg_migrate.MigrationDrift, match="not in this build"):
        pg_migrate.plan({"001_a.sql": "x"}, files)
    applied = {"002_b.sql": pg_migrate.checksum(files[0]), "003_new.sql": "y"}
    assert pg_migrate.plan(applied, files) == []


def test_a_row_round_trips_at_clickhouse_precision() -> None:
    mid = uuid.uuid4()
    row = {
        "org": "o",
        "memory_id": mid,
        "created_at": datetime(2026, 1, 2, 3, 4, 5, 678901),
        "expires_at": datetime(2026, 1, 2, 3, 4, 5, 999999, tzinfo=UTC),
        "tombstoned_at": None,
        "confidence": 0.1,
        "tags": ["a"],
    }
    back = pg_memory.decode_row(pg_memory.encode_row(row))
    assert back["memory_id"] == str(mid)
    assert back["created_at"] == datetime(2026, 1, 2, 3, 4, 5)
    assert back["expires_at"] == datetime(2026, 1, 2, 3, 4, 5)
    assert back["tombstoned_at"] is None
    assert back["confidence"] == pytest.approx(0.1, abs=1e-7) and back["confidence"] != 0.1
    assert back["tags"] == ["a"]


def test_the_codec_knows_every_datetime_column_of_loom_memory() -> None:
    schema = (Path(__file__).parents[2] / "schemas" / "schema.sql").read_text()
    body = schema.split("CREATE TABLE IF NOT EXISTS loom.memory\n", 1)[1].split("ENGINE", 1)[0]
    cols = set(re.findall(r"`(\w+)` (?:Nullable\()?DateTime\b(?!64)", body))
    assert cols == set(pg_memory.DATETIME_COLS)


def test_an_audit_row_keeps_its_milliseconds() -> None:
    ts = datetime(2026, 1, 2, 3, 4, 5, 678000, tzinfo=UTC)
    assert pg_memory.decode_audit(pg_memory.encode_audit({"ts": ts, "row_hash": "h"}))["ts"] == ts


def test_distances_mark_missing_and_mismatched_vectors() -> None:
    d = pg_admission.distances([[1.0, 0.0], None, [1.0, 0.0, 0.0], [0.0, 0.0]], [1.0, 0.0])
    assert d[0] == pytest.approx(0.0)
    assert d[1] is None
    assert d[2] == 2.0 and d[3] == 2.0


def _row(key: str, mt: str = "semantic", version: int = 1, **kw: object) -> dict[str, object]:
    return {
        "org": "o",
        "namespace": "n",
        "agent": "a",
        "user_id": "u",
        "session_id": "s",
        "memory_key": key,
        "memory_type": mt,
        "version": version,
        "memory_id": str(uuid.uuid4()),
        "status": "active",
        "tombstoned_at": None,
        "pinned": 0,
        **kw,
    }


def test_a_batch_gap_clears_only_rows_out_of_every_entrys_reach() -> None:
    near, far, other_type = _row("near"), _row("far"), _row("doc", mt="procedural")
    pg_admission.remember(identity_key(near), 1, [1.0, 0.1])
    pg_admission.remember(identity_key(far), 1, [0.0, 1.0])
    entry = ("semantic", "mine", [1.0, 0.0], 0.3)
    check = pg_admission.batch_gap_check([entry])
    assert check([far, other_type])
    assert not check([near])
    unknown = _row("unknown")  # no vector here: never ruled out
    assert not check([unknown])
    assert check([_row("mine")])  # the entry's own key is rechecked under its lock


def test_a_set_gap_never_clears_a_change_to_one_of_its_peers() -> None:
    far = _row("far")
    pg_admission.remember(identity_key(far), 1, [0.0, 1.0])
    check = pg_admission.set_gap_check(
        memory_type="semantic",
        memory_key="mine",
        embedding=[1.0, 0.0],
        reach=0.3,
        peer_ids={str(far["memory_id"])},
    )
    assert not check([far])


def test_every_postgres_table_is_classified_for_erasure() -> None:
    tables: set[str] = set()
    for f in PG_MIGRATIONS_DIR.glob("*.sql"):
        tables |= set(re.findall(r"CREATE TABLE IF NOT EXISTS (loom\.\w+)", f.read_text()))
    tables.add("loom.pg_migrations")  # the runner's ledger
    census = {t.removeprefix("pg:") for t in PG_CENSUS}
    assert tables == census | set(PG_NO_MEMORY_CONTENT)
    assert not census & set(PG_NO_MEMORY_CONTENT)


def _outbox_pass(
    monkeypatch: pytest.MonkeyPatch, claims: list[list[dict[str, str]]], refuse: Any
) -> tuple[list[str], list[str]]:
    """`publish_audit_outbox` over these claims, one namespace's rows each; `refuse(row)`
    is the error an insert holding that row meets. Returns the dropped and the counted
    row hashes."""
    from server.memory.store import pg_publish

    dropped: list[str] = []
    failed: list[str] = []

    async def _claim(limit: int) -> tuple[list[dict[str, str]], float]:
        return (claims.pop(0) if claims else []), 0.0

    async def _insert(batch: Any, **_: Any) -> None:
        for r in batch:
            if (error := refuse(r)) is not None:
                raise error

    async def _drop(batch: Any) -> None:
        dropped.extend(r["row_hash"] for r in batch)

    async def _fail(batch: Any, error: BaseException) -> None:
        failed.extend(r["row_hash"] for r in batch)

    monkeypatch.setattr(pg_memory, "claim_outbox", _claim)
    monkeypatch.setattr(pg_memory, "drop_outbox", _drop)
    monkeypatch.setattr(pg_memory, "fail_outbox", _fail)
    monkeypatch.setattr(pg_publish, "insert_audit_rows", _insert)
    monkeypatch.setattr("server.core.ch.get_client", lambda: None)
    _clickhouse(monkeypatch, answers=False)
    return dropped, failed


def _clickhouse(monkeypatch: pytest.MonkeyPatch, *, answers: bool) -> None:
    """Whether ClickHouse answers the probe a failed publish makes."""

    async def _probe() -> bool:
        return answers

    monkeypatch.setattr(ch_errors, "clickhouse_answers", _probe)


def _audits(namespace: str, *hashes: str) -> list[dict[str, str]]:
    return [{"org": "o", "namespace": namespace, "row_hash": h} for h in hashes]


@pytest.mark.asyncio
async def test_a_refused_outbox_batch_is_tried_row_by_row(monkeypatch: pytest.MonkeyPatch) -> None:
    """One audit row ClickHouse refuses is counted on that row alone: the rest of its
    namespace's batch, and the next namespace's, are published and dropped."""
    from server.memory.app import pg_delivery

    claims = [_audits("a", "bad", "ok1"), _audits("b", "ok2")]
    dropped, failed = _outbox_pass(
        monkeypatch, claims, lambda r: RuntimeError("refused") if r["row_hash"] == "bad" else None
    )
    assert await pg_delivery.publish_audit_outbox(10) == 2
    assert (sorted(dropped), failed) == (["ok1", "ok2"], ["bad"])


@pytest.mark.parametrize(
    "error",
    [BackendUnavailableError("down"), DatabaseError("Code: 252. Too many parts"), ClaimExpired()],
)
@pytest.mark.asyncio
async def test_an_unavailable_clickhouse_or_a_spent_claim_refuses_no_outbox_row(
    monkeypatch: pytest.MonkeyPatch, error: Exception
) -> None:
    """Neither counts an attempt: the pass stops, and the rows wait for their claim to end."""
    from server.memory.app import pg_delivery

    claims = [_audits("a", "x", "y"), _audits("b", "z")]
    dropped, failed = _outbox_pass(monkeypatch, claims, lambda r: error)
    assert await pg_delivery.publish_audit_outbox(10) == 0
    assert (dropped, failed, len(claims)) == ([], [], 1)


@pytest.mark.asyncio
async def test_an_outbox_pass_claims_at_most_its_limit_when_every_row_is_refused(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from server.memory.app import pg_delivery

    claims = [_audits("a", "x"), _audits("b", "y"), _audits("c", "z")]
    dropped, failed = _outbox_pass(monkeypatch, claims, lambda r: RuntimeError("refused"))
    assert await pg_delivery.publish_audit_outbox(2) == 0
    assert (dropped, failed, len(claims)) == ([], ["x", "y"], 1)


@pytest.mark.parametrize(
    "error", [BackendUnavailableError("down"), DatabaseError("Code: 242. Table is in readonly")]
)
@pytest.mark.asyncio
async def test_an_unavailable_clickhouse_counts_no_delivery_attempt(
    monkeypatch: pytest.MonkeyPatch, error: Exception
) -> None:
    """The delivery pass stops as the outbox's does: nothing is counted or parked."""
    from server.memory.app import pg_delivery

    _clickhouse(monkeypatch, answers=False)
    claims = [
        SimpleNamespace(org="o", namespace=ns, seq=1, attempts=8, taken_over=False)
        for ns in ("a", "b")
    ]
    counted: list[Any] = []
    released: list[Any] = []

    async def _release(batch: list[Any]) -> None:
        released.extend(batch)

    async def _renew(batch: list[Any]) -> list[Any]:
        return batch

    async def _publish(batch: list[Any], **_: Any) -> None:
        raise error

    async def _record(batch: list[Any], error: BaseException, **_: Any) -> int:
        counted.extend(batch)
        return len(batch)

    monkeypatch.setattr(pg_memory, "renew", _renew)
    monkeypatch.setattr(pg_memory, "record_failure", _record)
    monkeypatch.setattr(pg_memory, "release", _release)
    monkeypatch.setattr(pg_delivery, "_publish", _publish)
    assert await pg_delivery.publish_claims(claims) == (0, 0)
    # Released: taken over again, neither counts an attempt.
    assert counted == [] and released == claims


@pytest.mark.parametrize("answers", [True, False])
@pytest.mark.asyncio
async def test_a_connection_failure_counts_only_while_clickhouse_answers(
    monkeypatch: pytest.MonkeyPatch, answers: bool
) -> None:
    """A batch that drops the connection, or outlasts its timeout, while ClickHouse
    answers is the batch's own fault and must park; with ClickHouse down nothing was
    refused, however the outage shows (a refused connect, a connect timeout, a proxy)."""
    from clickhouse_connect.driver.exceptions import OperationalError

    _clickhouse(monkeypatch, answers=answers)
    for error in (
        OperationalError("Network Error: Server disconnected"),
        OperationalError("Network Error: Connection timeout to host"),
        OperationalError("HTTP driver received HTTP status 503"),
        BackendUnavailableError("down"),
    ):
        assert await ch_errors.refused_nothing(error, probe=True) is not answers
    # A refusal by ClickHouse itself, and the client's own deadline, count either way.
    for error in (
        DatabaseError("Code: 159. Timeout exceeded"),
        BackendTimeoutError("slow", statement_class="write", timeout_s=60),
    ):
        assert await ch_errors.refused_nothing(error, probe=True) is False


@pytest.mark.parametrize(
    ("error", "passing"),
    [
        (DatabaseError("Code: 225. No ZooKeeper"), True),
        (DatabaseError("Code: 319. Unknown status of insert"), True),
        (DatabaseError("Code: 252. Too many parts (3000) in table"), True),
        (DatabaseError("Code: 252. Too many partitions for single INSERT block (100)"), False),
        (OverloadedError("pool busy"), True),
        (DatabaseError("Code: 241. Memory limit (total) exceeded: would use 30 GiB"), True),
        (DatabaseError("Code: 241. DB::Exception: (total) memory limit exceeded: would use"), True),
        (DatabaseError("Code: 241. Memory limit (for user) exceeded: would use 12 GiB"), True),
        (DatabaseError("Code: 241. Memory limit (for query) exceeded: would use 9 GiB"), False),
    ],
)
def test_a_state_that_clears_on_its_own_is_told_from_the_rows_refusal(
    error: Exception, passing: bool
) -> None:
    from server.core.ch_errors import is_passing_state

    assert is_passing_state(error) is passing


def test_a_protocol_violation_is_not_retried_and_a_crash_shutdown_is() -> None:
    import asyncpg

    from server.core import pg

    assert not pg.is_outage(asyncpg.exceptions.ProtocolViolationError("08P01"))
    assert pg.is_outage(asyncpg.exceptions.CrashShutdownError("57P02"))
    assert pg.is_outage(asyncpg.exceptions.ConnectionFailureError("08006"))


@pytest.mark.parametrize(
    ("sqlstate", "retryable"),
    [("53100", True), ("53200", True), ("58030", True), ("53400", False), ("58P01", False)],
)
def test_a_server_short_of_a_resource_is_retried_and_a_configuration_error_is_not(
    sqlstate: str, retryable: bool
) -> None:
    from server.core import pg

    class _Err(Exception):
        pass

    err = _Err()
    err.sqlstate = sqlstate
    assert pg.is_outage(err) is retryable


def test_a_cached_statement_a_migration_changed_is_retried() -> None:
    import asyncpg

    from server.core import pg

    assert pg.is_outage(asyncpg.exceptions.InvalidCachedStatementError("0A000"))


@pytest.mark.asyncio
async def test_a_retried_audit_row_clickhouse_already_holds_is_not_written_again() -> None:
    """ClickHouse returns `row_hash` (a FixedString) as NUL-padded bytes: the dedup read
    still recognises the stored row."""
    from datetime import UTC, datetime

    from server.memory.store import pg_publish

    class _CH:
        inserted: list[Any] = []

        async def query_all(self, *_: Any, **__: Any) -> list[dict[str, Any]]:
            return [{"row_hash": b"h1" + b"\x00" * 62}]

        async def insert(self, table: str, rows: Any, **_: Any) -> None:
            _CH.inserted.extend(r["row_hash"] for r in rows)

    ts = datetime(2026, 1, 1, tzinfo=UTC)
    audits = [{"org": "o", "namespace": "n", "ts": ts, "row_hash": h} for h in ("h1", "h2")]
    await pg_publish.insert_audit_rows(audits, ch=_CH(), retried=True)
    assert _CH.inserted == ["h2"]


@pytest.mark.asyncio
async def test_an_audit_insert_is_not_sent_after_its_claim_ran_out() -> None:
    import time

    from server.memory.store import pg_publish

    class _CH:
        inserted = False

        async def insert(self, *_: Any, **__: Any) -> None:
            _CH.inserted = True

    with pytest.raises(pg_publish.ClaimExpired):
        await pg_publish.insert_audit_rows(
            [{"row_hash": "h"}], ch=_CH(), retried=False, start_by=time.monotonic() - 1
        )
    assert not _CH.inserted


def test_the_erasure_reads_the_audit_bit_the_commit_sets() -> None:
    from server.erasure.store import steps_pg

    assert steps_pg._AUDIT_ROW == pg_memory.AUDIT_ROW


def test_the_erasure_locks_heads_in_the_commits_order() -> None:
    """A different order would let an erasure and a commit deadlock on two heads."""
    from server.erasure.store import steps_pg

    assert steps_pg._KEY_ORDER == pg_memory._KEY_ORDER.replace("c.", "")


def test_the_knowledge_erasure_locks_heads_in_the_commits_order() -> None:
    """As for memory heads: a different order would let an erasure and a document
    commit deadlock on two heads."""
    from server.erasure.store import steps_pg
    from server.knowledge.store import pg_documents

    assert steps_pg._KNOWLEDGE_KEY_ORDER == pg_documents._KEY_ORDER


def test_the_exports_times_are_times_the_authority_stores() -> None:
    from server.erasure.store import exports

    assert set(exports._PG_TIMES) <= pg_memory.DATETIME_COLS


@pytest.mark.parametrize("bad", ["u\x00", "u\ud800"])
def test_an_erasure_of_a_scope_postgres_cannot_store_finds_nothing_there(
    monkeypatch, bad: str
) -> None:
    """A user id or namespace Postgres cannot store holds no row there: its erasure step
    and census count zero instead of failing the saga before ClickHouse is reached."""
    from server.erasure.store import steps_pg

    async def _never(*_a: Any, **_k: Any) -> Any:
        raise AssertionError("sent to Postgres")

    monkeypatch.setattr(pg, "enabled", lambda: True)
    monkeypatch.setattr(steps_pg, "_erase", _never)
    monkeypatch.setattr(pg, "transaction", _never)
    monkeypatch.setattr(pg, "dedicated", _never)

    async def _run() -> None:
        assert await steps_pg.delete_pg_memory_by_user("o", bad) == 0
        assert await steps_pg.delete_pg_memory_by_user(bad, "u") == 0
        assert await steps_pg.delete_pg_memory_by_namespace("o", bad) == 0
        assert await steps_pg.delete_pg_memory_by_namespace("o", [bad]) == 0
        assert await steps_pg.delete_pg_idempotency(bad) == 0
        zero = dict.fromkeys(steps_pg.PG_CENSUS, 0)
        assert await steps_pg.pg_residue("o", "user_id", bad) == zero
        assert await steps_pg.pg_residue("o", "namespace", [bad]) == zero
        assert await steps_pg.pg_residue(bad, "namespace", "n") == zero

    asyncio.run(_run())


class _AuthorityCH:
    """loom.storage_authority and whether loom.memory holds a row."""

    def __init__(self, record: dict[str, object] | None, holds_memory: bool = False) -> None:
        self.record, self.holds_memory, self.inserted = record, holds_memory, []

    async def query_one(self, sql: str, params: object = None) -> dict[str, object] | None:
        if "storage_authority" in sql:
            return self.record
        return {"n": int(self.holds_memory)}

    async def insert(self, table: str, rows: list[dict[str, object]], **_: object) -> None:
        assert table == "loom.storage_authority"
        self.inserted += rows


@pytest.mark.parametrize(
    ("configured", "recorded", "refused"),
    [
        ("clickhouse", "postgres", True),
        ("postgres", "clickhouse", True),
        ("postgres", None, True),
        ("postgres", "postgres", False),
        ("clickhouse", None, False),
    ],
)
async def test_a_cli_command_checks_the_recorded_authority_before_postgres(
    monkeypatch: pytest.MonkeyPatch, configured: str, recorded: str | None, refused: bool
) -> None:
    """Postgres is not read at connect, so a token revocation runs while it is down; a
    Postgres configuration leaves the deployment check to the pool's first open."""
    from server.core import pg_profile
    from server.settings import settings

    async def _down(*_: object, **__: object) -> object:
        raise pg.PostgresUnavailableError("postgres is down")

    armed: list[object] = []
    monkeypatch.setattr(settings, "storage_authority", configured)
    monkeypatch.setattr(settings, "pg_dsn", "postgres://u@h/d")
    monkeypatch.setattr(pg, "fetchrow", _down)
    monkeypatch.setattr(pg, "expect_deployment", armed.append)
    record = None if recorded is None else {"authority": recorded, "epoch": 1, "deployment_id": "d"}
    if refused:
        with pytest.raises(pg.AuthorityMismatch):
            await pg_profile.check_configured(_AuthorityCH(record))
    else:
        await pg_profile.check_configured(_AuthorityCH(record))
    assert armed == ([("d", 1)] if configured == "postgres" and not refused else [])


@pytest.mark.parametrize("dedicated", [False, True])
@pytest.mark.parametrize(
    ("held", "refused"),
    [
        ({"epoch": 1, "deployment_id": "d"}, False),
        ({"epoch": 1, "deployment_id": "other"}, True),
        (None, True),
    ],
)
async def test_a_cli_connection_to_another_deployments_postgres_is_refused(
    monkeypatch: pytest.MonkeyPatch, held: dict[str, object] | None, refused: bool, dedicated: bool
) -> None:
    """A LOOM_PG_DSN naming another deployment's database, or one that records none, is
    refused on the first connection, pooled or the dedicated one an erasure step opens,
    before any statement of the command."""
    import asyncpg

    ran: list[str] = []

    class _Conn:
        async def fetchrow(self, sql: str) -> object:
            return held

        async def execute(self, sql: str, *_: object) -> str:
            ran.append(sql)
            return ""

        async def set_type_codec(self, *_: object, **__: object) -> None:
            return None

        async def close(self, **_: Any) -> None:
            return None

        def terminate(self) -> None:
            return None

    class _Pool:
        terminated = False

        @contextlib.asynccontextmanager
        async def acquire(self) -> AsyncIterator[_Conn]:
            yield _Conn()

        def terminate(self) -> None:
            _Pool.terminated = True

    async def _connect(*_: object, **__: object) -> _Conn:
        return _Conn()

    async def _create_pool(*_: object, **__: object) -> _Pool:
        return _Pool()

    monkeypatch.setattr(asyncpg, "connect", _connect)
    monkeypatch.setattr(asyncpg, "create_pool", _create_pool)
    monkeypatch.setattr(pg.settings, "pg_dsn", "postgres://u@h/d")
    pg.expect_deployment(("d", 1))
    try:

        async def _use() -> None:
            if dedicated:
                async with pg.dedicated() as conn:
                    await conn.execute("DELETE FROM loom.memory_current")
            else:
                await pg.open_pool()

        if refused:
            with pytest.raises(pg.AuthorityMismatch):
                await _use()
            assert not pg._pools
            if dedicated:
                assert ran == []  # refused before the command's first statement
            else:
                assert _Pool.terminated
        else:
            await _use()
            assert pg._expected is None  # checked once
    finally:
        pg.expect_deployment(None)
        pg._pools.clear()


@pytest.fixture
def authority(monkeypatch: pytest.MonkeyPatch) -> dict[str, object]:
    """The Postgres side of the record, in memory."""
    from server.core import pg_profile

    held: dict[str, object] = {}

    async def _pg_record() -> dict[str, object] | None:
        return dict(held["row"]) if "row" in held else None

    async def _execute(sql: str, *args: object) -> str:
        held.setdefault("row", {"authority": args[0], "epoch": args[1], "deployment_id": args[2]})
        return "INSERT 0 1"

    async def _fetchrow(sql: str, *args: object) -> object:
        return held.get("memory")

    class _Conn:
        execute = staticmethod(_execute)

    @contextlib.asynccontextmanager
    async def _transaction() -> Any:
        yield _Conn()

    monkeypatch.setattr(pg_profile, "_pg_record", _pg_record)
    monkeypatch.setattr(pg_profile.pg, "transaction", _transaction)
    monkeypatch.setattr(pg_profile.pg, "fetchrow", _fetchrow)
    return held


def _configure(monkeypatch: pytest.MonkeyPatch, authority: str) -> None:
    from server.settings import settings

    monkeypatch.setattr(settings, "storage_authority", authority)


@pytest.mark.asyncio
async def test_a_clickhouse_replica_records_its_authority_once(
    monkeypatch: pytest.MonkeyPatch, authority: dict[str, object]
) -> None:
    from server.core.pg_profile import _require_authority

    _configure(monkeypatch, "clickhouse")
    ch = _AuthorityCH(None)
    await _require_authority(ch)
    assert [r["authority"] for r in ch.inserted] == ["clickhouse"]
    ch = _AuthorityCH({"authority": "clickhouse", "epoch": 1, "deployment_id": "d"})
    await _require_authority(ch)
    assert ch.inserted == []


@pytest.mark.asyncio
async def test_a_replica_of_the_other_authority_refuses(
    monkeypatch: pytest.MonkeyPatch, authority: dict[str, object]
) -> None:
    from server.core.pg import AuthorityMismatch
    from server.core.pg_profile import _require_authority

    _configure(monkeypatch, "clickhouse")
    with pytest.raises(AuthorityMismatch, match="postgres"):
        await _require_authority(
            _AuthorityCH({"authority": "postgres", "epoch": 1, "deployment_id": "d"})
        )
    _configure(monkeypatch, "postgres")
    with pytest.raises(AuthorityMismatch, match="needs a migration"):
        await _require_authority(
            _AuthorityCH({"authority": "clickhouse", "epoch": 1, "deployment_id": "d"})
        )


@pytest.mark.asyncio
async def test_postgres_never_takes_over_a_clickhouse_that_holds_memory(
    monkeypatch: pytest.MonkeyPatch, authority: dict[str, object]
) -> None:
    from server.core.pg import AuthorityMismatch
    from server.core.pg_profile import _require_authority

    _configure(monkeypatch, "postgres")
    with pytest.raises(AuthorityMismatch, match="already holds memory"):
        await _require_authority(_AuthorityCH(None, holds_memory=True))
    assert "row" not in authority


@pytest.mark.asyncio
async def test_a_first_postgres_boot_records_both_stores_with_one_id(
    monkeypatch: pytest.MonkeyPatch, authority: dict[str, object]
) -> None:
    from server.core.pg_profile import _require_authority

    _configure(monkeypatch, "postgres")
    ch = _AuthorityCH(None)
    await _require_authority(ch)
    row = authority["row"]
    assert ch.inserted == [
        {"authority": "postgres", "epoch": 1, "deployment_id": row["deployment_id"]}
    ]
    # The next boot finds both records agreeing.
    ch.record = {"authority": "postgres", "epoch": 1, "deployment_id": row["deployment_id"]}
    await _require_authority(ch)


@pytest.mark.asyncio
async def test_a_postgres_of_another_deployment_refuses(
    monkeypatch: pytest.MonkeyPatch, authority: dict[str, object]
) -> None:
    from server.core.pg import AuthorityMismatch
    from server.core.pg_profile import _require_authority

    _configure(monkeypatch, "postgres")
    recorded = {"authority": "postgres", "epoch": 1, "deployment_id": "ours"}
    with pytest.raises(AuthorityMismatch, match="records none"):
        await _require_authority(_AuthorityCH(recorded))
    authority["row"] = {"authority": "postgres", "epoch": 1, "deployment_id": "theirs"}
    with pytest.raises(AuthorityMismatch, match="different deployments"):
        await _require_authority(_AuthorityCH(recorded))
    # A ClickHouse that lost its record never adopts a Postgres that holds memory.
    authority["memory"] = {"one": 1}
    with pytest.raises(AuthorityMismatch, match="not restored"):
        await _require_authority(_AuthorityCH(None))


@pytest.mark.asyncio
async def test_the_pool_opens_with_the_configured_sizes_and_timeouts(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import asyncpg

    from server.core import pg
    from server.settings import settings

    seen: dict[str, object] = {}

    async def _create_pool(dsn: str, **kw: object) -> object:
        seen.update(kw, dsn=dsn)
        return object()

    monkeypatch.setattr(asyncpg, "create_pool", _create_pool)
    monkeypatch.setattr(settings, "pg_dsn", "postgres://u@h/d")
    monkeypatch.setattr(settings, "pg_pool_min_size", 3)
    monkeypatch.setattr(settings, "pg_pool_max_size", 7)
    monkeypatch.setattr(settings, "pg_statement_timeout_ms", 1500)
    await pg.open_pool()
    pg._pools.clear()
    assert (seen["min_size"], seen["max_size"]) == (3, 7)
    server_settings = seen["server_settings"]
    assert server_settings["statement_timeout"] == "1500"
    assert server_settings["synchronous_commit"] == "on"


@pytest.mark.asyncio
async def test_boot_refuses_by_the_configured_durability(monkeypatch: pytest.MonkeyPatch) -> None:
    from server.core import pg_profile
    from server.settings import settings

    async def _fetchrow(sql: str, *args: object) -> dict[str, object]:
        return {"names": "", "sync": 0, "hidden": 0}

    monkeypatch.setattr(pg_profile.pg, "fetchrow", _fetchrow)
    monkeypatch.setattr(settings, "pg_durability", "synchronous")
    with pytest.raises(pg_profile.DurabilityRefused):
        await pg_profile.check_durability()
    monkeypatch.setattr(settings, "pg_durability", "local")
    assert (await pg_profile.check_durability()).warnings


def test_a_lease_ends_its_margin_early(monkeypatch: pytest.MonkeyPatch) -> None:
    from server.settings import settings

    monkeypatch.setattr(settings, "pg_lease_margin_seconds", 0.5)
    assert pg_lease.lease_deadline(settings.pg_request_lease_seconds, 100.0) == (
        100.0 + settings.pg_request_lease_seconds - 0.5
    )
    assert pg_lease.lease_deadline(settings.pg_delivery_lease_seconds, 0.0) == (
        settings.pg_delivery_lease_seconds - 0.5
    )


def test_this_process_keeps_only_its_newest_vectors(monkeypatch: pytest.MonkeyPatch) -> None:
    from server.settings import settings

    monkeypatch.setattr(settings, "pg_admission_recent_vectors", 2)
    for i in range(3):
        pg_admission.remember(("o", "n", "a", "u", "s", f"k{i}"), 1, [float(i)])
    assert pg_admission._local_vector(_row("k0")) is None
    assert pg_admission._local_vector(_row("k2")) == [2.0]


@pytest.mark.asyncio
async def test_a_gap_longer_than_a_commit_reads_goes_back_to_the_check(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from server.settings import settings

    monkeypatch.setattr(settings, "pg_admission_gap_rows", 5)
    captured = {("o", "*"): 0, ("o", "n"): 10}
    assert await pg_admission._gap(captured, {("o", "n"): 16}, "o", "n") is None
    assert await pg_admission._gap(captured, {("o", "*"): 1}, "o", "n") is None


@pytest.mark.asyncio
async def test_a_gap_with_erased_seqs_is_read_without_them(monkeypatch: pytest.MonkeyPatch) -> None:
    """An erasure removes rows from the journal and advances the generation with none:
    the gap is the rows left, or a stuck watermark below the hole would never clear."""

    async def _since(org: str, ns: str, after: int, through: int) -> list[dict[str, Any]]:
        return [{"seq": 12}]

    monkeypatch.setattr(pg_memory, "revisions_since", _since)
    captured = {("o", "*"): 0, ("o", "n"): 10}
    assert await pg_admission._gap(captured, {("o", "n"): 14}, "o", "n") == (14, [{"seq": 12}])


@pytest.mark.asyncio
async def test_an_admission_waits_for_its_backlog_at_most_the_projection_wait(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from server.memory.app import pg_delivery
    from server.settings import settings

    async def _backlog(*_: object) -> int:
        return 1

    async def _deliver(*_: object) -> int:
        return 0

    monkeypatch.setattr(pg_memory, "search_backlog", _backlog)
    monkeypatch.setattr(pg_delivery, "deliver_namespace", _deliver)
    monkeypatch.setattr(settings, "pg_projection_wait_ms", 20)
    with pytest.raises(pg_admission.ProjectionBehindError):
        await pg_admission.wait_searchable("o", "n", 3)


@pytest.mark.asyncio
async def test_the_delivery_worker_claims_its_batch_and_idles_without_postgres(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from server.core import idempotency
    from server.memory.workers import pg_delivery as worker
    from server.settings import settings

    monkeypatch.setattr(settings, "storage_authority", "clickhouse")
    assert (await worker.run_pass()).claimed == 0
    monkeypatch.setattr(settings, "storage_authority", "postgres")
    monkeypatch.setattr(settings, "pg_delivery_batch_rows", 7)
    asked: list[int] = []

    async def _claim(limit: int, **_: object) -> list[object]:
        asked.append(limit)
        return []

    async def _sweep(limit: int) -> int:
        asked.append(limit)
        return 0

    monkeypatch.setattr(pg_memory, "claim_due", _claim)
    monkeypatch.setattr(idempotency, "sweep_expired", _sweep)
    monkeypatch.setattr(pg_memory, "sweep_erasure_evidence", _sweep)
    monkeypatch.setattr(pg_memory, "sweep_settled", _sweep)

    async def _claim_outbox(limit: int) -> tuple[list[object], float]:
        asked.append(limit)
        return [], 0.0

    monkeypatch.setattr(pg_memory, "claim_outbox", _claim_outbox)
    assert (await worker.run_pass()).claimed == 0
    # claims, idempotency keys, evidence, settled deliveries, the audit outbox
    assert asked == [7, 7, 7, 7, 7]
    assert settings.pg_delivery_enabled and settings.pg_delivery_idle_interval_seconds > 0


@pytest.mark.asyncio
async def test_a_head_that_keeps_moving_is_contended_after_the_rounds(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from server.memory.api.models_crud import MemorySetInput
    from server.memory.app import write
    from server.memory.store.pg_memory import HeadMoved
    from server.settings import settings

    monkeypatch.setattr(settings, "storage_authority", "postgres")
    monkeypatch.setattr(settings, "pg_admission_rounds", 2)
    passes: list[int] = []

    async def _once(input: object, **_: object) -> object:
        passes.append(1)
        raise HeadMoved()

    monkeypatch.setattr(write, "_memory_set_once", _once)
    inp = MemorySetInput(org="o", namespace="n", agent="a", memory_key="k", value="v")
    with pytest.raises(pg_memory.AdmissionContendedError):
        await write._memory_set_phases(inp)
    assert len(passes) == 2


def _bulk_input() -> Any:
    from server.memory.api.models_bulk import MemoryBulkSetInput

    return MemoryBulkSetInput(
        org="o", namespace="n", agent="a", entries=[{"memory_key": "k", "value": "v"}]
    )


def _bulk_postgres(monkeypatch: pytest.MonkeyPatch) -> Any:
    """bulk_set under the Postgres authority, on a manual clock."""
    from server.memory.app import bulk_rows, bulk_set
    from server.settings import settings
    from tests.unit.clock_fake import ManualClock

    clock = ManualClock(t=100.0)
    monkeypatch.setattr(settings, "storage_authority", "postgres")
    monkeypatch.setattr(settings, "pg_admission_deadline_ms", 100)
    monkeypatch.setattr(settings, "pg_admission_rounds", 3)
    monkeypatch.setattr(bulk_rows.time, "monotonic", clock.monotonic)
    monkeypatch.setattr(
        bulk_set, "get_embedder", lambda: type("E", (), {"model_name": "m", "dim": 2})()
    )
    return clock


@pytest.mark.asyncio
async def test_a_batch_whose_scope_keeps_moving_is_contended_at_the_deadline(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from server.memory.app import bulk_set

    clock = _bulk_postgres(monkeypatch)
    passes: list[int] = []

    async def _once(input: object, embedder: object, retry: Any, **_: object) -> object:
        passes.append(1)
        clock.advance(0.06)
        retry.start()  # as the batch's commit does
        raise pg_memory.AdmissionMoved({("o", "n"): 1})

    monkeypatch.setattr(bulk_set, "_bulk_set_once", _once)
    with pytest.raises(pg_memory.AdmissionContendedError):
        await bulk_set.handle_memory_bulk_set(_bulk_input())
    assert len(passes) == 3  # the deadline runs from the first commit, at the end of pass 1


@pytest.mark.asyncio
async def test_a_batch_whose_scope_moves_more_often_than_the_rounds_still_commits(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Rounds bound a head that keeps changing; a moved scope is bound by the deadline."""
    from server.memory.app import bulk_set

    _bulk_postgres(monkeypatch)
    passes: list[int] = []

    async def _once(input: object, embedder: object, retry: Any, **_: object) -> object:
        passes.append(1)
        retry.start()
        if len(passes) <= 5:
            raise pg_memory.AdmissionMoved({("o", "n"): len(passes)})
        return "committed"

    monkeypatch.setattr(bulk_set, "_bulk_set_once", _once)
    assert await bulk_set.handle_memory_bulk_set(_bulk_input()) == "committed"
    assert len(passes) == 6


@pytest.mark.asyncio
async def test_a_batch_whose_head_keeps_moving_is_contended_after_the_rounds(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from server.memory.app import bulk_set

    _bulk_postgres(monkeypatch)
    passes: list[Any] = []

    async def _once(input: object, embedder: object, retry: Any, **_: object) -> object:
        passes.append(retry)
        raise pg_memory.HeadMoved()

    monkeypatch.setattr(bulk_set, "_bulk_set_once", _once)
    with pytest.raises(pg_memory.AdmissionContendedError):
        await bulk_set.handle_memory_bulk_set(_bulk_input())
    assert len(passes) == 3 and passes[0] is passes[2]  # one retry state across the starts


def test_a_nul_character_is_refused_before_the_commit() -> None:
    from server.core.errors import ValidationError

    with pytest.raises(ValidationError, match="value"):
        pg_memory.encode_row({"memory_key": "k", "value": "a\x00b"})
    with pytest.raises(ValidationError, match="tags"):
        pg_memory.encode_row({"memory_key": "k", "tags": ["ok", "x\x00"]})
    assert pg_memory.encode_row({"memory_key": "k", "value": "ab"})["value"] == "ab"


@pytest.mark.asyncio
async def test_a_write_that_starts_over_embeds_each_text_once() -> None:
    from server.memory.app.reused_vectors import ReusedVectors

    calls: list[str] = []

    class _Counting:
        model_name, dim = "m", 1

        async def embed(self, text: str) -> list[float]:
            calls.append(text)
            return [float(len(text))]

        async def embed_many(self, texts: list[str]) -> list[list[float]]:
            return [await self.embed(t) for t in texts]

    vectors = ReusedVectors(_Counting())
    assert await vectors.embed_many(["a", "bb", "a"]) == [[1.0], [2.0], [1.0]]
    assert await vectors.embed("bb") == [2.0]
    assert await vectors.embed_many(["ccc", "a"]) == [[3.0], [1.0]]
    assert calls == ["a", "bb", "ccc"]


class _Unavailable(Exception):
    sqlstate = "57014"  # a statement timeout


class _FakeTx:
    def __init__(self, *, commit_fails: bool = False, rollback_fails: bool = False) -> None:
        self.commit_fails, self.rollback_fails = commit_fails, rollback_fails

    async def start(self) -> None:
        pass

    async def commit(self) -> None:
        if self.commit_fails:
            raise _Unavailable()

    async def rollback(self) -> None:
        if self.rollback_fails:
            raise RuntimeError("rollback failed")


def _fake_connection(monkeypatch: pytest.MonkeyPatch, tx: _FakeTx) -> None:
    from contextlib import asynccontextmanager

    from server.core import pg

    class _Conn:
        def transaction(self) -> _FakeTx:
            return tx

        def is_closed(self) -> bool:
            return False

        async def execute(self, sql: str) -> None:
            assert sql == "SET LOCAL statement_timeout = 0"  # the commit's own bound

    @asynccontextmanager
    async def _connection():
        yield _Conn()

    monkeypatch.setattr(pg, "connection", _connection)


@pytest.mark.asyncio
async def test_a_retryable_error_in_a_transaction_is_503_and_a_failed_rollback_never_hides_it(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from server.core import pg

    _fake_connection(monkeypatch, _FakeTx(rollback_fails=True))
    with pytest.raises(pg.PostgresUnavailableError) as e:
        async with pg.transaction():
            raise _Unavailable()
    assert e.value.status_code == 503 and not e.value.extra.get("outcome_unknown")
    with pytest.raises(KeyError):
        async with pg.transaction():
            raise KeyError("not Postgres's")


@pytest.mark.asyncio
async def test_a_commit_whose_answer_was_lost_says_its_outcome_is_unknown(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from server.core import pg

    _fake_connection(monkeypatch, _FakeTx(commit_fails=True))
    with pytest.raises(pg.PostgresUnavailableError) as e:
        async with pg.transaction():
            pass
    assert e.value.extra.get("outcome_unknown") is True


def _claim(seq: int, targets: int, *, ns: str = "n", attempts: int = 1) -> pg_memory.Claimed:
    return pg_memory.Claimed(
        org="o",
        namespace=ns,
        seq=seq,
        targets=targets,
        lease_token=2,
        attempts=attempts,
        row={"memory_key": f"k{seq}"},
        audit={},
        deadline=0.0,
    )


@pytest.mark.asyncio
async def test_a_revision_clickhouse_keeps_refusing_holds_back_only_itself(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from server.memory.app import pg_delivery
    from server.settings import settings

    monkeypatch.setattr(settings, "pg_delivery_max_attempts", 3)
    published: list[tuple[list[int], bool]] = []
    failed: list[int] = []

    async def _publish(claims: list[pg_memory.Claimed], *, stub: bool = False) -> None:
        if any(c.seq == 2 for c in claims) and not stub:
            raise RuntimeError("refused")
        published.append(([c.seq for c in claims], stub))

    async def _record_failure(
        claims: list[pg_memory.Claimed], error: BaseException, *, park: bool = False
    ) -> int:
        failed.extend(c.seq for c in claims)
        return len(claims)

    async def _renew(claims: list[pg_memory.Claimed]) -> list[pg_memory.Claimed]:
        return list(claims)

    monkeypatch.setattr(pg_delivery, "_publish", _publish)
    monkeypatch.setattr(pg_memory, "record_failure", _record_failure)
    monkeypatch.setattr(pg_memory, "renew", _renew)
    everything = pg_memory.MEMORY_ROW | pg_memory.SEARCH_DOC
    claims = [_claim(1, everything), _claim(2, everything), _claim(5, everything, ns="m")]
    assert (await pg_delivery.publish_claims(claims))[0] == 2
    # n first (it holds the oldest claim), one by one; then namespace m
    assert published == [([1], False), ([5], False)]
    assert failed == [2]

    # On its last attempt a claim owing only its search document lands as a stub.
    published.clear()
    assert (await pg_delivery.publish_claims([_claim(2, pg_memory.SEARCH_DOC, attempts=3)]))[0] == 1
    assert published == [([2], True)]

    # A stub refused too parks that claim alone; the rest of the pass is published.
    async def _refuse_2(claims: list[pg_memory.Claimed], *, stub: bool = False) -> None:
        if any(c.seq == 2 for c in claims):
            raise RuntimeError("refused")
        published.append(([c.seq for c in claims], stub))

    monkeypatch.setattr(pg_delivery, "_publish", _refuse_2)
    published.clear()
    last = [_claim(2, pg_memory.SEARCH_DOC, attempts=3), _claim(7, everything, ns="z")]
    assert (await pg_delivery.publish_claims(last))[0] == 1
    assert published == [([7], False)]


@pytest.mark.asyncio
async def test_a_call_without_a_key_never_commits_under_the_previous_calls_key() -> None:
    """An MCP batch runs its calls in one context: the claim one call bound is
    forgotten before the next is admitted."""

    from server.core import idempotency
    from server.transport import gates

    idempotency._bound.set(("o", "the previous call's key", 1))
    op = SimpleNamespace(idempotent=True, read_only=False)
    replay = await gates.idempotency_check(op, None, None, "", object())
    assert replay.scope is None
    assert idempotency.bound_key() is None


def test_only_an_op_whose_answer_replays_marks_its_key_committed() -> None:
    """profile.set writes through memory.set: its key is not marked committed, since
    the committed replay would answer in memory.set's shape."""
    from server.core import idempotency

    for op, marks in (("memory.set", True), ("memory.bulk_set", True), ("profile.set", False)):
        idempotency._bound.set(("o", "k", 1))
        idempotency._bound_op.set(op)
        assert (idempotency.bound_key() is not None) is marks, op
    idempotency.unbind()
    assert idempotency.bound_key() is None


def test_the_authoritys_erasure_step_runs_first_and_only_under_it(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from server.erasure.app.saga_steps import NAMESPACE_STEPS, USER_STEPS, authority_steps
    from server.erasure.store.census import census_tables
    from server.settings import settings

    monkeypatch.setattr(settings, "storage_authority", "postgres")
    for steps in (USER_STEPS, NAMESPACE_STEPS):
        assert authority_steps(steps)[0][0] == "pg_memory.delete"
    assert set(PG_CENSUS) <= set(census_tables("user_id"))
    monkeypatch.setattr(settings, "storage_authority", "clickhouse")
    for steps in (USER_STEPS, NAMESPACE_STEPS):
        assert all(not name.startswith("pg_") for name, _ in authority_steps(steps))
    assert not set(PG_CENSUS) & set(census_tables("user_id"))


@pytest.mark.asyncio
async def test_a_guard_reads_the_blacklist_again_when_another_replica_changed_it(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The write captured a policy generation the cache was not loaded at: the guard
    reloads before it checks, whatever the cache's own refresh interval says."""
    from server.memory.app import blacklist_guard

    loads: list[int] = []

    async def _refresh() -> None:
        loads.append(1)

    async def _fresh() -> bool:
        return False

    monkeypatch.setattr(blacklist_guard, "_refresh_cache", _refresh)
    monkeypatch.setattr(blacklist_guard, "_is_stale", _fresh)
    monkeypatch.setattr(blacklist_guard, "_covered", {})
    await blacklist_guard._ensure_loaded({("o", "*"): 3})
    await blacklist_guard._ensure_loaded({("o", "*"): 3})
    await blacklist_guard._ensure_loaded({("o", "*"): 4})
    await blacklist_guard._ensure_loaded(None)
    assert len(loads) == 2


def _hit(key: str, *, user: str | None = "u", version: int = 5, source: str = "fact") -> Any:
    from server.memory.api.models_search import _MemorySearchHit

    hit = _MemorySearchHit(
        memory_key=key,
        memory_type="semantic",
        content_excerpt=key,
        distance=0.1,
        similarity=0.9,
        source=source,
        version=version,
        namespace="n",
        session_id="s",
    )
    hit._row_identity = None if user is None else ("a", user, "s")
    return hit


@pytest.mark.asyncio
async def test_a_stale_hit_is_replaced_from_the_remainder_and_left_off_the_next_page(
    monkeypatch: pytest.MonkeyPatch,
) -> None:

    from server.memory.app.search import pg_validation
    from server.settings import settings

    monkeypatch.setattr(settings, "storage_authority", "postgres")
    gone = {"p2", "t1"}

    async def _live(org: str, ids: list[tuple[Any, ...]]) -> set[tuple[Any, ...]]:
        return {i for i in ids if i[4] not in gone}

    async def _heads(org: str, ids: list[tuple[Any, ...]]) -> dict[Any, Any]:
        return {}

    monkeypatch.setattr(pg_memory, "live_versions", _live)
    monkeypatch.setattr(pg_memory, "chunk_heads", _heads)
    page = [_hit("p1"), _hit("p2"), _hit("p3"), _hit("chunk:s:0", source="chunk")]
    tail = [_hit("t1"), _hit("t2"), _hit("nobody", user=None), _hit("t4"), _hit("t5")]
    ctx = SimpleNamespace(
        input=SimpleNamespace(org="o", namespace="n", as_of=None),
        final_hits=page,
        page_tail=tail,
        candidates_total=9,
        stale_dropped=0,
        actual_work={},
    )
    kept = await pg_validation.validate(ctx)
    # p2 dropped; t1 (stale) passed over while refilling to 4; the identity-less row is
    # left off the remainder and the total. Every stale row read is counted.
    assert [h.memory_key for h in kept] == ["p1", "p3", "chunk:s:0", "t2"]
    assert ctx.stale_dropped == 3 and ctx.candidates_total == 6
    assert [h.memory_key for h in ctx.page_tail] == ["t4", "t5"]


@pytest.mark.asyncio
async def test_a_folded_chunk_is_checked_by_its_parents_key(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from server.memory.app.search import pg_validation
    from server.settings import settings

    monkeypatch.setattr(settings, "storage_authority", "postgres")
    asked: list[Any] = []

    async def _live(org: str, ids: list[Any]) -> set[Any]:
        return set()

    async def _heads(org: str, ids: list[Any]) -> dict[Any, Any]:
        asked.extend(ids)
        return {("n", "a", "u", "long"): [("the long value", "semantic")]}

    monkeypatch.setattr(pg_memory, "live_versions", _live)
    monkeypatch.setattr(pg_memory, "chunk_heads", _heads)
    # The head's value is cut into chunk 0 with this hash, and no other.
    monkeypatch.setattr(
        pg_validation, "set_chunk_identities", lambda v, t, k, at_least=0: {(0, "sha")}
    )
    folded = _hit("long", user=None, version=0)
    folded.score_components = {"folded_from_chunk": 3.0}
    folded._row_chunk = ("a", "u", 0, "sha")
    old = _hit("long", user=None, version=0)
    old.score_components = {"folded_from_chunk": 1.0}
    old._row_chunk = ("a", "u", 0, "an older value's chunk")
    hits = [folded, _hit("bare", user=None), old]
    assert await pg_validation.stale_flags("o", "n", hits) == [False, True, True]
    assert asked == [("n", "a", "u", "long")]


async def test_a_hit_made_from_a_chunk_is_judged_by_its_row_and_its_chunk(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A chunk made into its row's hit shows the chunk's type: its own row (session and
    version) must still be served, under any type, and the head must still cut the chunk.
    A row lifting a chunk's passage is checked by its type and by the chunk. A chunk whose
    row in its session is gone is refused, though another session's head still cuts it."""
    from server.memory.app.retrieval.pool import ChunkId, SetChunkRow, fold_set_chunks
    from server.memory.app.search import pg_validation
    from server.memory.app.search.vector_legs import _stamp_row_fields
    from server.memory.domain.search_types import SearchHit
    from server.settings import settings

    monkeypatch.setattr(settings, "storage_authority", "postgres")
    live = {("n", "a", "u", "s1", "long", "", 7), ("n", "a", "u", "s1", "long", "semantic", 7)}
    asked: list[Any] = []

    async def _live(org: str, ids: list[Any]) -> set[Any]:
        asked.extend(ids)
        return live & set(ids)

    async def _heads(org: str, ids: list[Any]) -> dict[Any, Any]:
        return {i: [("the long value", "semantic")] for i in ids}

    monkeypatch.setattr(pg_memory, "live_versions", _live)
    monkeypatch.setattr(pg_memory, "chunk_heads", _heads)
    monkeypatch.setattr(
        pg_validation, "set_chunk_identities", lambda v, t, k, at_least=0: {(2, "sha")}
    )
    chunk = SearchHit(
        memory_key="chunk:memory.set:long:2",
        memory_type="episodic",
        content_excerpt="a passage",
        distance=0.1,
        rank_score=0.9,
        source="chunk",
        parent_memory_key="long",
        identity=("a", "u", "memory.set:long"),
        chunk_sha256="sha",
    )

    def _folded(session: str, *, pooled: bool) -> Any:
        rows = {ChunkId("a", "u", "long", 2, "sha"): SetChunkRow(("a", "u", session), 7, ())}
        row = replace(chunk, memory_key="long", source="fact", parent_memory_key="")
        row = replace(row, identity=("a", "u", session), version=7, memory_type="semantic")
        (made,) = fold_set_chunks(
            [replace(row, rank_score=0.1), chunk] if pooled else [chunk], rows
        )
        assert made.chunk == ("a", "u", 2, "sha")
        return _stamp_row_fields(made, _hit("long", version=7))

    made, lifted, gone = (
        _folded("s1", pooled=False),
        _folded("s1", pooled=True),
        _folded("s2", pooled=False),
    )
    for wire, session in ((made, "s1"), (lifted, "s1"), (gone, "s2")):
        wire._row_identity = ("a", "u", session)
    assert made._chunk_typed and not lifted._chunk_typed
    assert await pg_validation.stale_flags("o", "n", [made, lifted, gone]) == [False, False, True]
    assert sorted(i[3:6] for i in asked) == [
        ("s1", "long", ""),
        ("s1", "long", "semantic"),
        ("s2", "long", ""),
    ]


def test_a_parked_page_keeps_what_its_hits_are_checked_by(monkeypatch: pytest.MonkeyPatch) -> None:
    """A later page checks a parked hit as the first did: its row, chunk and version, and
    whether it shows a chunk's type."""
    from server.memory.app import search_pages
    from server.settings import settings

    monkeypatch.setattr(settings, "storage_authority", "postgres")
    hit = _hit("long", version=7)
    hit._row_chunk, hit._chunk_typed = ("a", "u", 2, "sha"), True
    back = search_pages.unparked(json.loads(json.dumps(search_pages._parked(hit))))
    assert back is not None
    assert (back._row_identity, back._row_chunk, back._chunk_typed) == (
        ("a", "u", "s"),
        ("a", "u", 2, "sha"),
        True,
    )


def _parked_page(hits: list[Any], **kw: Any) -> dict[str, Any]:
    from server.memory.app import search_pages

    return dict(
        json.loads(
            search_pages.encode_page(
                query="q",
                top_k=2,
                candidates_total=len(hits),
                tail=hits,
                namespaces_searched=["n"],
                namespaces_skipped=[],
                **kw,
            )
        )
    )


def _page_input(top_k: int) -> Any:
    return SimpleNamespace(org="o", namespace="n", top_k=top_k)


@pytest.mark.asyncio
async def test_a_parked_page_keeps_each_hits_identity(monkeypatch: pytest.MonkeyPatch) -> None:
    from server.core import pg
    from server.memory.app.search.output import _parked_slice

    with monkeypatch.context() as m:  # parked under the Postgres authority
        m.setattr(pg, "enabled", lambda: True)
        payload = _parked_page([_hit("k1"), _hit("k2", user=None)])
    hits, nxt, _ = await _parked_slice(_page_input(2), payload, 0, {"n"})
    assert [h._row_identity for h in hits] == [("a", "u", "s"), None]
    assert nxt == -1


@pytest.mark.asyncio
async def test_a_parked_page_reads_on_past_stale_rows_and_resumes_after_the_last_served(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from server.memory.app.search import pg_validation
    from server.memory.app.search.output import _parked_slice

    async def _flags(org: str, namespace: str, hits: list[Any]) -> list[bool]:
        return [h.memory_key.startswith("stale") for h in hits]

    monkeypatch.setattr(pg_validation, "stale_flags", _flags)
    rows = [_hit("stale1"), _hit("a"), _hit("b"), _hit("stale2"), _hit("c")]
    payload = _parked_page(rows)
    hits, nxt, dropped = await _parked_slice(_page_input(2), payload, 0, {"n"})
    assert [h.memory_key for h in hits] == ["a", "b"] and (nxt, dropped) == (3, 1)
    hits, nxt, dropped = await _parked_slice(_page_input(2), payload, nxt, {"n"})
    assert [h.memory_key for h in hits] == ["c"] and (nxt, dropped) == (-1, 1)


@pytest.mark.asyncio
async def test_an_as_of_replays_pages_are_served_unchecked(monkeypatch: pytest.MonkeyPatch) -> None:
    from server.memory.api.models_search import MemorySearchInput
    from server.memory.app import search_pages
    from server.memory.app.search import output, pg_validation

    async def _flags(org: str, namespace: str, hits: list[Any]) -> list[bool]:
        raise AssertionError("a replay's page is not checked")

    monkeypatch.setattr(pg_validation, "stale_flags", _flags)
    payload = _parked_page([_hit("a"), _hit("b")], replay=True)

    async def _load(*a: Any, **kw: Any) -> dict[str, Any]:
        return payload

    monkeypatch.setattr(search_pages, "load_page", _load)
    monkeypatch.setattr(output, "get_client", lambda: None)
    page = MemorySearchInput(
        org="o",
        namespace="n",
        query="q",
        top_k=2,
        page=search_pages.make_handle("page/" + "b" * 16, 0),
    )
    out = await output._serve_parked_page(page)
    assert [h.memory_key for h in out.results] == ["a", "b"] and out.stale_dropped == 0


@pytest.mark.parametrize("namespace", ["*", ""])
@pytest.mark.asyncio
async def test_a_namespace_named_like_a_policy_scope_is_refused(namespace: str) -> None:
    """A namespace's generation shares its key with the policy generations: '*' and ''
    are refused before any statement runs."""
    from server.core.errors import ValidationError

    row = {"org": "o", "namespace": namespace, "memory_key": "k", "memory_id": str(uuid.uuid4())}
    with pytest.raises(ValidationError):
        await pg_memory.commit_versions([row], [{}])


def test_a_page_parked_under_clickhouse_carries_no_postgres_fields() -> None:
    from server.memory.app.search_pages import _parked

    hit = _hit("k1")
    hit._row_version, hit._row_chunk = 42, ("a", "u", 3, "sha")
    assert not {"row_identity", "row_chunk", "row_version"} & set(_parked(hit))


@pytest.mark.asyncio
async def test_a_parked_page_keeps_each_hits_chunk_and_version(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from server.core import pg
    from server.memory.app.search.output import _parked_slice

    hit = _hit("k1", version=0)
    hit._row_version, hit._row_chunk = 42, ("a", "u", 3, "sha")
    with monkeypatch.context() as m:  # parked under the Postgres authority
        m.setattr(pg, "enabled", lambda: True)
        payload = _parked_page([hit])
    (back,), _, _ = await _parked_slice(_page_input(1), payload, 0, {"n"})
    assert (back._row_version, back._row_chunk) == (42, ("a", "u", 3, "sha"))
    assert "row_version" not in back.model_dump() and back.version == 0


@pytest.mark.asyncio
async def test_a_tier_row_the_authority_no_longer_serves_is_left_out_and_counted(
    monkeypatch: pytest.MonkeyPatch,
) -> None:

    from server.memory.app.search import pg_validation, tiers

    rows = [
        SimpleNamespace(
            memory_key=k,
            memory_type="semantic",
            value=k,
            created_at="",
            agent="a",
            user_id="u",
            session_id="s",
            version=7,
            run_id="",
        )
        for k in ("kept", "gone")
    ]

    async def _rows(*_: object, **__: object) -> list[object]:
        return rows

    async def _flags(org: str, namespace: str, hits: list[Any]) -> list[bool]:
        assert all(h._row_version == 7 for h in hits)
        return [h.memory_key == "gone" for h in hits]

    async def _annotate(*_: object) -> None:
        return None

    monkeypatch.setattr("server.memory.store.list_by_namespace", _rows)
    monkeypatch.setattr(tiers, "request_scope", lambda: None)
    monkeypatch.setattr(pg_validation, "stale_flags", _flags)
    monkeypatch.setattr("server.memory.app.code_link_reads.annotate_hits", _annotate)
    inp = SimpleNamespace(
        org="o", namespace="n", top_k=5, run_id="", anchor_first=2, recent_first=0
    )
    out, stale = await tiers._prepend_tier([], inp, "anchor_first", {})
    assert [h.memory_key for h in out] == ["kept"] and stale == 1


@pytest.mark.asyncio
async def test_a_refused_statement_is_the_claims_attempt_and_the_rest_still_publish(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A refused Postgres statement fails the same way on every pass: it counts on its
    claim, which parks at the limit, rather than ending each pass with nothing counted."""
    import asyncpg

    from server.memory.app import pg_delivery
    from server.settings import settings

    monkeypatch.setattr(settings, "pg_delivery_max_attempts", 3)
    recorded: list[tuple[int, bool]] = []
    published: list[int] = []
    refused = pg.PostgresRefusedError(asyncpg.exceptions.CheckViolationError("check"))

    async def _publish(claims: list[pg_memory.Claimed], *, stub: bool = False) -> None:
        if any(c.seq == 1 for c in claims):
            raise refused
        published.extend(c.seq for c in claims)

    async def _record(
        claims: list[pg_memory.Claimed], error: BaseException, *, park: bool = False
    ) -> int:
        recorded.extend((c.seq, park) for c in claims)
        return len(claims)

    async def _renew(claims: list[pg_memory.Claimed]) -> list[pg_memory.Claimed]:
        return list(claims)

    monkeypatch.setattr(pg_delivery, "_publish", _publish)
    monkeypatch.setattr(pg_memory, "record_failure", _record)
    monkeypatch.setattr(pg_memory, "renew", _renew)
    owed_row = pg_memory.MEMORY_ROW | pg_memory.SEARCH_DOC
    claims = [_claim(1, owed_row, attempts=2), _claim(2, owed_row, attempts=0)]
    assert await pg_delivery.publish_claims(claims) == (1, 1)
    assert recorded == [(1, True)] and published == [2]


@pytest.mark.asyncio
async def test_a_failed_publish_counts_once_and_the_last_one_parks(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Attempts count failed publishes, a lease that ran out during one included; the one
    reaching LOOM_PG_DELIVERY_MAX_ATTEMPTS parks the delivery."""
    from server.memory.app import pg_delivery
    from server.settings import settings

    monkeypatch.setattr(settings, "pg_delivery_max_attempts", 3)
    recorded: list[tuple[int, bool]] = []

    async def _publish(claims: list[pg_memory.Claimed], *, stub: bool = False) -> None:
        if claims[0].seq == 2:
            raise pg_lease.PublicationPending()
        raise RuntimeError("refused")

    async def _record(
        claims: list[pg_memory.Claimed], error: BaseException, *, park: bool = False
    ) -> int:
        recorded.extend((c.seq, park) for c in claims)
        return len(claims)

    async def _renew(claims: list[pg_memory.Claimed]) -> list[pg_memory.Claimed]:
        return list(claims)

    monkeypatch.setattr(pg_delivery, "_publish", _publish)
    monkeypatch.setattr(pg_memory, "record_failure", _record)
    monkeypatch.setattr(pg_memory, "renew", _renew)
    owed_row = pg_memory.MEMORY_ROW | pg_memory.SEARCH_DOC
    claims = [_claim(1, owed_row, attempts=1), _claim(2, owed_row, attempts=2)]
    assert await pg_delivery.publish_claims(claims) == (0, 2)
    assert sorted(recorded) == [(1, False), (2, True)]


@pytest.mark.asyncio
async def test_a_namespace_whose_claims_are_all_gone_leaves_the_rest_of_the_pass(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from server.memory.app import pg_delivery

    published: list[str] = []

    async def _publish(claims: list[pg_memory.Claimed], *, stub: bool = False) -> None:
        published.extend(c.namespace for c in claims)

    async def _renew(claims: list[pg_memory.Claimed]) -> list[pg_memory.Claimed]:
        return [c for c in claims if c.namespace != "erased"]

    monkeypatch.setattr(pg_delivery, "_publish", _publish)
    monkeypatch.setattr(pg_memory, "renew", _renew)
    claims = [_claim(1, pg_memory.MEMORY_ROW, ns="erased"), _claim(2, pg_memory.MEMORY_ROW)]
    assert (await pg_delivery.publish_claims(claims))[0] == 1
    assert published == ["n"]


@pytest.mark.asyncio
async def test_a_last_attempt_its_lease_ran_out_on_publishes_its_stub_under_a_new_lease(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import time
    from dataclasses import replace

    from server.memory.app import pg_delivery
    from server.settings import settings

    monkeypatch.setattr(settings, "pg_delivery_max_attempts", 2)
    stubs: list[pg_memory.Claimed] = []
    renewals: list[int] = []

    async def _publish(claims: list[pg_memory.Claimed], *, stub: bool = False) -> None:
        pg_lease.check_lease(claims[0].deadline)
        if not stub:
            raise pg_lease.PublicationPending()  # the embedder outlasted the lease
        stubs.extend(claims)

    async def _renew(claims: list[pg_memory.Claimed]) -> list[pg_memory.Claimed]:
        renewals.append(len(claims))
        # The batch's and the one-by-one renewals are spent by the slow publish, which
        # landed the memory row: the stub's renewal reads only the search document owed.
        if len(renewals) <= 2:
            return [replace(c, deadline=0.0) for c in claims]
        fresh = time.monotonic() + 30
        return [replace(c, targets=pg_memory.SEARCH_DOC, deadline=fresh) for c in claims]

    async def _record(*a: object, **kw: object) -> None:
        raise AssertionError("the stub landed: nothing is recorded")

    monkeypatch.setattr(pg_delivery, "_publish", _publish)
    monkeypatch.setattr(pg_memory, "renew", _renew)
    monkeypatch.setattr(pg_memory, "record_failure", _record)
    owed = pg_memory.MEMORY_ROW | pg_memory.SEARCH_DOC
    assert (await pg_delivery.publish_claims([_claim(1, owed, attempts=1)]))[0] == 1
    (stub,) = stubs
    assert stub.targets == pg_memory.SEARCH_DOC and stub.deadline > time.monotonic()


def test_a_failed_publish_is_recorded_without_its_text() -> None:
    from clickhouse_connect.driver.exceptions import DatabaseError

    from server.core.error_summary import error_summary

    err = DatabaseError("Code: 27. DB::Exception: Cannot parse input: 'the secret value'")
    assert error_summary(err) == "DatabaseError code 27"
    # Another error quoting a code is not a ClickHouse error.
    assert error_summary(ValueError("Code: 27. the secret value")) == "ValueError"

    class _ProviderError(Exception):
        status_code = 400

    assert error_summary(_ProviderError("the secret value")) == "_ProviderError status 400"

    class _PgError(Exception):
        sqlstate = "40P01"

    wrapped = RuntimeError("Postgres did not answer")
    wrapped.__cause__ = _PgError("deadlock over the secret value")
    assert error_summary(wrapped) == "RuntimeError sqlstate 40P01"


@pytest.mark.asyncio
async def test_a_claim_on_a_just_replaced_lease_sends_its_audit_row_through_the_outbox(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The holder a claim replaced may still land its audit row until the insert bound has
    passed after its lease, and the claim's read would miss it: that row goes to the
    outbox, due once it cannot, and the claim inserts only the others."""
    from dataclasses import replace as with_fields

    from server.memory.app import pg_delivery
    from server.memory.store import pg_publish

    sent: list[Any] = []
    deferred: list[Any] = []

    async def _rows(rows: Any, audits: Any, *_: object, **__: object) -> None:
        sent.extend(audits)

    async def _defer(claims: Any, bound_s: float) -> None:
        deferred.extend((c.seq, bound_s) for c in claims)

    async def _none(*_: object, **__: object) -> None:
        return None

    monkeypatch.setattr(pg_publish, "publish_rows", _rows)
    monkeypatch.setattr(pg_publish, "retire_decision_records", _none)
    monkeypatch.setattr(pg_memory, "defer_audits", _defer)
    monkeypatch.setattr("server.core.ch.get_client", lambda: object())
    fresh = with_fields(
        _claim(4, pg_memory.AUDIT_ROW), audit={"row_hash": "a4"}, replaced_lease_s=-2.0
    )
    old = with_fields(_claim(5, pg_memory.AUDIT_ROW), audit={"row_hash": "a5"})
    await pg_delivery._publish([fresh, old])
    assert deferred == [(4, pg.insert_bound_s())]
    assert sent == [{"row_hash": "a5"}]


@pytest.mark.asyncio
async def test_a_refused_audit_row_fails_the_attempt_after_the_search_document_is_in(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from server.memory.app import pg_delivery
    from server.memory.store import pg_publish

    steps: list[str] = []
    refusal = RuntimeError("audit_log refused")

    async def _rows(*_: object, **__: object) -> BaseException:
        steps.append("rows")
        return refusal

    async def _held(*_: object, **__: object) -> set[object]:
        return set()

    async def _docs(claims: list[object], *, stub: bool = False) -> list[dict[str, object]]:
        return [{"memory_key": "k"}]

    async def _insert(docs: list[object], ch: object = None) -> None:
        steps.append("documents")

    async def _complete(seqs: object, done: int, *, tokens: object = 1) -> None:
        steps.append(f"complete {done}")

    async def _no_late_rows(docs: object, ch: object = None) -> int:
        return 0

    monkeypatch.setattr(pg_publish, "publish_rows", _rows)
    monkeypatch.setattr(pg_publish, "held_documents", _held)
    monkeypatch.setattr(pg_publish, "document_id", lambda row: ("k",))
    monkeypatch.setattr(pg_delivery, "_search_documents", _docs)
    monkeypatch.setattr("server.memory.store.insert_embedding_rows", _insert)
    monkeypatch.setattr("server.memory.store.index_late_rows", _no_late_rows)

    async def _all_heads(claims: Any) -> Any:
        return claims

    monkeypatch.setattr(pg_delivery, "_still_heads", _all_heads)
    monkeypatch.setattr(pg_memory, "complete", _complete)
    monkeypatch.setattr(pg_lease, "check_lease", lambda deadline: None)
    monkeypatch.setattr("server.core.ch.get_client", lambda: object())
    claim = _claim(4, pg_memory.SEARCH_DOC | pg_memory.AUDIT_ROW)
    with pytest.raises(RuntimeError, match="audit_log refused"):
        await pg_delivery._publish([claim])
    assert steps == ["rows", "documents", f"complete {pg_memory.SEARCH_DOC}"]


@pytest.mark.asyncio
async def test_a_commit_waits_for_its_locks_at_most_the_commit_lock_timeout(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An erasure holds its scope's generations while it runs: a commit queued behind it
    gives its pooled connection back after LOOM_PG_COMMIT_LOCK_TIMEOUT_MS."""
    from contextlib import asynccontextmanager

    from server.core import pg
    from server.settings import settings

    monkeypatch.setattr(settings, "pg_commit_lock_timeout_ms", 1234)
    first: list[str] = []

    class _Stop(Exception):
        pass

    class _Conn:
        async def execute(self, sql: str, *args: object) -> None:
            first.append(sql)
            raise _Stop()

    @asynccontextmanager
    async def _transaction():
        yield _Conn()

    monkeypatch.setattr(pg, "transaction", _transaction)
    row = {
        "org": "o",
        "namespace": "n",
        "agent": "a",
        "user_id": "-",
        "session_id": "-",
        "memory_key": "k",
        "memory_id": str(uuid.uuid4()),
        "version": 1,
    }
    with pytest.raises(_Stop):
        await pg_memory.commit_versions([row], [{}])
    assert first == ["SET LOCAL lock_timeout = 1234"]


@pytest.mark.asyncio
async def test_an_admission_publishes_at_most_its_help_rows_of_another_writers_backlog(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from server.memory.app import pg_delivery
    from server.settings import settings

    monkeypatch.setattr(settings, "pg_admission_help_rows", 7)
    asked: list[tuple[int, object]] = []

    async def _claim(limit: int, *, scope: object = None) -> list[object]:
        asked.append((limit, scope))
        return []

    monkeypatch.setattr(pg_memory, "claim_due", _claim)
    assert await pg_delivery.deliver_namespace("o", "n") == 0
    assert asked == [(7, ("o", "n"))]


@pytest.mark.asyncio
async def test_erasure_evidence_is_swept_after_its_hours(monkeypatch: pytest.MonkeyPatch) -> None:
    from server.core import pg
    from server.settings import settings

    monkeypatch.setattr(settings, "pg_erasure_evidence_hours", 6)
    seen: list[tuple[object, ...]] = []

    async def _fetchval(sql: str, *args: object) -> int:
        seen.append(args)
        return 3

    monkeypatch.setattr(pg, "fetchval", _fetchval)
    assert await pg_memory.sweep_erasure_evidence(50) == 3
    assert seen == [(6, 50)]


@pytest.mark.asyncio
async def test_a_trace_leaves_out_the_candidates_the_authority_no_longer_serves(
    monkeypatch: pytest.MonkeyPatch,
) -> None:

    from server.memory.app.search import pg_validation, trace
    from server.memory.domain.search_types import SearchTraceHit

    seen: list[Any] = []

    async def _flags(org: str, namespace: str, hits: list[Any]) -> list[bool]:
        seen.extend(hits)
        return [h.memory_key == "gone" for h in hits]

    from server.settings import settings

    monkeypatch.setattr(settings, "storage_authority", "postgres")
    monkeypatch.setattr(pg_validation, "stale_flags", _flags)
    hits = [
        SearchTraceHit(
            memory_key=k,
            memory_type="semantic",
            content_excerpt=k,
            distance=0.1,
            version=7,
            identity=("a", "u", "s"),
        )
        for k in ("kept", "gone")
    ]
    out = await trace._served(SimpleNamespace(org="o", namespace="n", as_of=None), hits)
    assert [h.memory_key for h in out] == ["kept"]
    assert [(h._row_identity, h.version) for h in seen] == [(("a", "u", "s"), 7)] * 2
    # a replay (as_of) is served as it read: no authority check
    seen.clear()
    anchored = SimpleNamespace(org="o", namespace="n", as_of=datetime(2026, 1, 1))
    assert await trace._served(anchored, hits) == hits
    assert seen == []


@pytest.mark.asyncio
async def test_the_ledger_describes_the_page_the_authority_left(
    monkeypatch: pytest.MonkeyPatch,
) -> None:

    from server.memory.app.search import pg_validation
    from server.settings import settings

    async def _flags(org: str, namespace: str, hits: list[Any]) -> list[bool]:
        return [h.memory_key == "gone" for h in hits]

    monkeypatch.setattr(settings, "storage_authority", "postgres")
    monkeypatch.setattr(pg_validation, "stale_flags", _flags)
    ctx = SimpleNamespace(
        input=SimpleNamespace(org="o", namespace="n", as_of=None),
        final_hits=[_hit("kept"), _hit("gone")],
        page_tail=[],
        candidates_total=2,
        stale_dropped=0,
        actual_work={"candidates_total": 2, "items_by_source": {"fact": 2}},
    )
    await pg_validation.validate(ctx)
    assert ctx.actual_work["candidates_total"] == 1
    assert ctx.actual_work["items_by_source"] == {"fact": 1}
    assert ctx.stale_versions == {("n", "a", "u", "s", "gone"): 5}


@pytest.mark.asyncio
async def test_a_stale_row_only_in_the_tail_still_lowers_the_ledgers_total(
    monkeypatch: pytest.MonkeyPatch,
) -> None:

    from server.memory.app.search import pg_validation
    from server.settings import settings

    async def _flags(org: str, namespace: str, hits: list[Any]) -> list[bool]:
        return [h.memory_key == "gone" for h in hits]

    monkeypatch.setattr(settings, "storage_authority", "postgres")
    monkeypatch.setattr(pg_validation, "stale_flags", _flags)
    ctx = SimpleNamespace(
        input=SimpleNamespace(org="o", namespace="n", as_of=None),
        final_hits=[_hit("kept")],
        page_tail=[_hit("gone")],
        candidates_total=2,
        stale_dropped=0,
        actual_work={"candidates_total": 2},
    )
    await pg_validation.validate(ctx)
    assert ctx.candidates_total == ctx.actual_work["candidates_total"] == 1


@pytest.mark.asyncio
async def test_a_parked_page_under_clickhouse_resumes_past_the_rows_a_caller_lost() -> None:
    """Without the Postgres authority nothing is checked; a full page followed by rows of a
    namespace the caller lost hands the next page the first of them."""
    from server.memory.app.search.output import _parked_slice

    lost = _hit("lost")
    lost.namespace = "gone"
    payload = _parked_page([_hit("a"), _hit("b"), lost, _hit("c")])
    hits, nxt, dropped = await _parked_slice(_page_input(2), payload, 0, {"n"})
    assert [h.memory_key for h in hits] == ["a", "b"] and (nxt, dropped) == (2, 0)
    hits, nxt, dropped = await _parked_slice(_page_input(2), payload, nxt, {"n"})
    assert [h.memory_key for h in hits] == ["c"] and (nxt, dropped) == (-1, 0)


@pytest.mark.asyncio
async def test_a_batch_restarts_for_a_head_then_a_scope_under_one_deadline(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from server.memory.app import bulk_set

    clock = _bulk_postgres(monkeypatch)
    passes: list[Any] = []

    async def _once(input: object, embedder: object, retry: Any, **_: object) -> object:
        passes.append(retry)
        clock.advance(0.06)
        retry.start()
        if len(passes) == 1:
            raise pg_memory.HeadMoved()
        raise pg_memory.AdmissionMoved({("o", "n"): len(passes)})

    monkeypatch.setattr(bulk_set, "_bulk_set_once", _once)
    with pytest.raises(pg_memory.AdmissionContendedError):
        await bulk_set.handle_memory_bulk_set(_bulk_input())
    # The deadline started at the first commit, before the head moved: two more passes.
    assert len(passes) == 3 and len({id(r) for r in passes}) == 1


@pytest.mark.asyncio
async def test_a_tier_does_not_count_again_a_key_the_page_dropped(
    monkeypatch: pytest.MonkeyPatch,
) -> None:

    from server.memory.app.search import pg_validation, tiers

    async def _rows(*a: object, **kw: object) -> list[Any]:
        return [
            SimpleNamespace(
                memory_key=k,
                value=k,
                memory_type="semantic",
                created_at="",
                version=7,
                agent="a",
                user_id="u",
                session_id="s",
                run_id="",
            )
            for k in ("dropped", "newer", "kept")
        ]

    checked: list[str] = []

    async def _flags(org: str, namespace: str, hits: list[Any]) -> list[bool]:
        checked.extend(h.memory_key for h in hits)
        return [h.memory_key == "dropped" for h in hits]

    async def _annotate(*_: object) -> None:
        return None

    monkeypatch.setattr("server.memory.store.list_by_namespace", _rows)
    monkeypatch.setattr(tiers, "request_scope", lambda: None)
    monkeypatch.setattr(pg_validation, "stale_flags", _flags)
    monkeypatch.setattr("server.memory.app.code_link_reads.annotate_hits", _annotate)
    inp = SimpleNamespace(
        org="o", namespace="n", top_k=5, run_id="", anchor_first=0, recent_first=3
    )
    # The page dropped `dropped` at version 7 and `newer` at 6 (a stale passage, say):
    # every tier row is checked; `newer` is served, `dropped` is refused and not counted
    # again, by this tier or the next.
    skip = {("n", "a", "u", "s", "dropped"): 7, ("n", "a", "u", "s", "newer"): 6}
    out, stale = await tiers._prepend_recent([], inp, skip)
    assert [h.memory_key for h in out] == ["newer", "kept"] and stale == 0
    assert checked == ["dropped", "newer", "kept"]
    skip.pop(("n", "a", "u", "s", "dropped"))
    _, first = await tiers._prepend_recent([], inp, skip)
    _, second = await tiers._prepend_anchor(
        [], SimpleNamespace(**{**vars(inp), "anchor_first": 3}), skip
    )
    assert (first, second) == (1, 0)


async def _no_head(keys: Any) -> dict[Any, int]:
    """The authority's settled read finds no head: a first write that may be in, fenced."""
    return {}


@pytest.mark.parametrize(
    ("error", "fenced"),
    [
        (pg_memory.MemoryErased(), False),
        ("unavailable", False),
        ("outcome_unknown", True),
        ("cancelled_in_commit", True),
        (asyncio.CancelledError(), False),  # cancelled in the block: rolled back
        (pg_memory.AdmissionContendedError("moved"), False),
    ],
)
@pytest.mark.asyncio
async def test_a_high_update_is_fenced_only_when_its_commit_may_have_landed(
    monkeypatch: pytest.MonkeyPatch, error: Any, fenced: bool
) -> None:
    """A commit refused or unanswered before it ran writes nothing to fence; one whose
    answer was lost, or that was cancelled while it ran, may be in."""
    from server.core import pg
    from server.memory.app import high_fence, update
    from server.settings import settings

    if error == "unavailable":
        error = pg.PostgresUnavailableError("Postgres did not answer")
    elif error == "outcome_unknown":
        error = pg.PostgresUnavailableError("lost", outcome_unknown=True)
    elif error == "cancelled_in_commit":
        error = asyncio.CancelledError()
        error.commit_in_flight = True  # as `pg.transaction` marks a cancel at its COMMIT
    fences: list[Any] = []

    async def _commit(*a: object, **kw: object) -> Any:
        raise error

    async def _fence(keys: Any, *, ch: Any) -> None:
        fences.append(keys)

    async def _never() -> None:
        raise AssertionError("a commit that raised records nothing")

    monkeypatch.setattr(settings, "storage_authority", "postgres")
    monkeypatch.setattr(update, "_commit_revision", _commit)
    monkeypatch.setattr(high_fence, "fence_high", _fence)
    monkeypatch.setattr(update, "identity_key", lambda row: ("o", "n", "a", "u", "-", "k"))
    with pytest.raises(type(error)):
        await update._land_revision(
            SimpleNamespace(),
            {"version": 3},
            [],
            None,
            None,
            searchable=True,
            high=True,
            record=_never,
            exact=False,
            op="memory.update",
        )
    assert bool(fences) is fenced


async def test_a_commit_that_may_have_landed_keeps_its_error_when_its_fence_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from server.memory.app import high_fence, update
    from server.settings import settings

    lost = pg.PostgresUnavailableError("lost", outcome_unknown=True)
    dropped: list[str] = []

    async def _commit(*_a: Any, **_k: Any) -> Any:
        raise lost

    async def _fence(*_a: Any, **_k: Any) -> None:
        raise RuntimeError("clickhouse down")

    async def _never() -> None:
        raise AssertionError("a commit that raised records nothing")

    monkeypatch.setattr(settings, "storage_authority", "postgres")
    monkeypatch.setattr(pg_memory, "settled_head_versions", _no_head)
    monkeypatch.setattr(update, "_commit_revision", _commit)
    monkeypatch.setattr(high_fence, "fence_high", _fence)
    monkeypatch.setattr(update, "identity_key", lambda row: ("o", "n", "a", "u", "-", "k"))
    monkeypatch.setattr(high_fence, "record_drop", lambda _o, _n, _s, why, **_: dropped.append(why))
    with pytest.raises(pg.PostgresUnavailableError) as raised:
        await update._land_revision(
            SimpleNamespace(),
            {"version": 3, "org": "o", "namespace": "n", "agent": "a"}
            | {"user_id": "u", "session_id": "-", "memory_key": "k"},
            [],
            None,
            None,
            searchable=True,
            high=True,
            record=_never,
            exact=False,
            op="memory.update",
        )
    # it may never have committed: the drop says the outcome is unknown
    assert raised.value is lost and dropped == ["fence_failed_outcome_unknown"]


async def test_a_high_update_cancelled_while_publishing_is_recorded_and_fenced(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from server.memory.app import high_fence, update
    from server.settings import settings

    done: list[str] = []
    publishing = asyncio.Event()

    async def _commit(*_a: Any, **_k: Any) -> Any:
        return SimpleNamespace()

    async def _publish(*_a: Any, **_k: Any) -> Any:
        publishing.set()
        await asyncio.Event().wait()

    async def _record() -> None:
        done.append("recorded")

    async def _fence(keys: Any, *, ch: Any) -> None:
        done.append("fenced")

    monkeypatch.setattr(settings, "storage_authority", "postgres")
    monkeypatch.setattr(update, "_commit_revision", _commit)
    monkeypatch.setattr(update, "_publish_revision", _publish)
    monkeypatch.setattr(high_fence, "fence_high", _fence)
    monkeypatch.setattr(update, "identity_key", lambda row: ("o", "n", "a", "u", "-", "k"))
    task = asyncio.create_task(
        update._land_revision(
            SimpleNamespace(),
            {"version": 3},
            [],
            None,
            None,
            searchable=True,
            high=True,
            record=_record,
            exact=False,
            op="memory.update",
        )
    )
    await publishing.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert done == ["recorded", "fenced"]


@pytest.mark.parametrize("published", [True, False])
@pytest.mark.parametrize("high", [False, True])
async def test_a_committed_revision_is_recorded_once_and_its_fence_never_its_answer(
    monkeypatch: pytest.MonkeyPatch, published: bool, high: bool
) -> None:
    """Once the commit is in: its audit row and announcement are written once, published
    or pending; a 'high' one's fence that fails is recorded, and the answer stands."""
    from server.memory.app import high_fence, update
    from server.settings import settings

    pending = None if published else pg_lease.PublicationPending()
    recorded: list[str] = []
    dropped: list[tuple[str, str, str]] = []

    async def _commit(*_a: Any, **_k: Any) -> Any:
        return SimpleNamespace()

    async def _publish(*_a: Any, **_k: Any) -> Any:
        return pending

    async def _record() -> None:
        recorded.append("recorded")

    async def _fence(*_a: Any, **_k: Any) -> None:
        raise RuntimeError("clickhouse down")

    monkeypatch.setattr(settings, "storage_authority", "postgres")
    monkeypatch.setattr(update, "_commit_revision", _commit)
    monkeypatch.setattr(update, "_publish_revision", _publish)
    monkeypatch.setattr(high_fence, "fence_high", _fence)
    monkeypatch.setattr(
        high_fence,
        "record_drop",
        lambda _o, _n, stage, why, *, item: dropped.append((stage, why, item)),
    )
    key = {"org": "o", "namespace": "n", "agent": "a", "user_id": "u", "session_id": "-"}
    answer = await update._land_revision(
        key | {"memory_key": "k"},  # the head the revision fences, by its own key
        key | {"version": 3, "memory_key": "k"},
        [],
        None,
        None,
        searchable=True,
        high=high,
        record=_record,
        exact=False,
        op="memory.update_by_id",
    )
    assert answer is pending and recorded == ["recorded"]
    assert dropped == ([("memory.update_by_id", "fence_failed", "a/-/k")] if high else [])


@pytest.mark.parametrize("moved", [pg_memory.HeadMoved(), pg_memory.AdmissionMoved({})])
async def test_an_update_over_a_moved_head_or_policy_answers_contended(
    monkeypatch: pytest.MonkeyPatch, moved: Exception
) -> None:
    from server.memory.app import update
    from server.memory.store import pg_publish

    async def _commit(*_: Any, **__: Any) -> Any:
        raise moved

    monkeypatch.setattr(pg_publish, "commit", _commit)
    with pytest.raises(pg_memory.AdmissionContendedError):
        await update._commit_revision(SimpleNamespace(), _row("k"), [], None, exact=False)


@pytest.mark.parametrize("cancelled_first", [True, False])
async def test_a_revisions_cancelled_child_is_raised_beside_a_pending_one(
    monkeypatch: pytest.MonkeyPatch, cancelled_first: bool
) -> None:
    """Whichever child the gather lists first, a cancellation is raised as itself and a
    pending publication alone is returned."""
    from server.memory.app import update
    from server.memory.store import pg_publish

    async def _commit(*_: Any, **__: Any) -> Any:
        return SimpleNamespace(seqs=[1], deadline=None)

    async def _pending(*_: Any, **__: Any) -> None:
        raise pg_lease.PublicationPending()

    async def _cancelled(*_: Any, **__: Any) -> None:
        raise asyncio.CancelledError()

    import server.memory.store as memory_repo

    publish, document = (_cancelled, _pending) if cancelled_first else (_pending, _cancelled)
    monkeypatch.setattr(pg_publish, "commit", _commit)
    monkeypatch.setattr(pg_publish, "publish", publish)
    monkeypatch.setattr(memory_repo, "insert_embedding_rows", document)
    emb = [{"version": 1, "embedding": [0.1]}]
    committed = await update._commit_revision(SimpleNamespace(), _row("k"), emb, None, exact=False)
    with pytest.raises(asyncio.CancelledError):
        await update._publish_revision(committed, emb, None)
    monkeypatch.setattr(memory_repo, "insert_embedding_rows", _pending)
    monkeypatch.setattr(pg_publish, "publish", _pending)
    assert isinstance(
        await update._publish_revision(committed, emb, None), pg_lease.PublicationPending
    )


def test_a_constraint_error_is_raised_without_the_row_it_quotes() -> None:
    """A not-null or check violation's text quotes the failing row; another error (a
    missing table, which a read of an unmigrated store answers) passes through."""
    import asyncpg

    quoted = asyncpg.exceptions.NotNullViolationError.new(
        {"M": "null value", "D": "Failing row contains (sk-live-1234).", "C": "23502"}
    )
    with pytest.raises(pg.PostgresRefusedError) as refused:
        pg._raise_if_unavailable(quoted)
    assert "sk-live-1234" not in str(refused.value) and refused.value.__suppress_context__
    assert refused.value.sqlstate == "23502"  # what `error_summary` reports of it
    # The driver's own argument error quotes the value it could not encode.
    from asyncpg.exceptions._base import DataError as ClientDataError

    with pytest.raises(pg.PostgresRefusedError) as bad_argument:
        pg._raise_if_unavailable(
            ClientDataError("invalid input for query argument $1: 'sk-live-1234'")
        )
    assert "sk-live-1234" not in str(bad_argument.value)
    pg._raise_if_unavailable(asyncpg.exceptions.UndefinedTableError("relation does not exist"))
    # One raised in a transaction's block is already the answer: it keeps what it names.
    pg._raise_if_unavailable(refused.value)


def test_a_write_that_read_a_version_before_its_redaction_starts_over() -> None:
    """A redaction since the read is a head that moved: the write reads again and
    rebuilds from the scrubbed head, never from the text the redaction removed."""

    read = {"pinned": 0, "lock_mode": "", "status": "active", "redacted_at": None}
    redacted = {**read, "version": 7, "redacted_at": "2026-01-01 00:00:00"}
    for exact in (False, True):  # compare-and-set too: a redaction keeps the version
        then = SimpleNamespace(**read, version=7, tombstoned_at=None, lock_until=None)
        recheck = pg_memory.heads_unchanged({("k",): then}, exact=exact)
        recheck(("k",), {**read, "version": 7})
        with pytest.raises(pg_memory.HeadMoved):
            recheck(("k",), redacted)


class _PgConn:
    """A pooled connection whose statement, rollback or commit is told what to do."""

    def __init__(self, *, fails: BaseException | None = None, commit: BaseException | None = None):
        self.fails = fails
        self.commit_fails = commit
        self.terminated = False
        self.rolled_back = False

    def terminate(self) -> None:
        self.terminated = True

    def is_closed(self) -> bool:
        return self.terminated

    async def fetch(self, *a: object) -> list[object]:
        if self.fails is not None:
            raise self.fails
        return [(1,)]

    async def execute(self, *a: object) -> str:
        return "SET"

    def transaction(self) -> Any:
        conn = self

        class _Tx:
            async def start(self) -> None:
                return None

            async def rollback(self) -> None:
                conn.rolled_back = True

            async def commit(self) -> None:
                if conn.commit_fails is not None:
                    raise conn.commit_fails

        return _Tx()


def _pg_pool(
    monkeypatch: pytest.MonkeyPatch, conn: _PgConn, release: BaseException | None = None
) -> None:
    from server.core import pg

    class _Pool:
        async def acquire(self, **_: float) -> _PgConn:
            return conn

        async def release(self, c: _PgConn, **_: float) -> None:
            if release is not None:
                raise release  # asyncpg's reset ran past its bound and closed it

    async def _pool() -> _Pool:
        return _Pool()

    monkeypatch.setattr(pg, "_pool_for_loop", _pool)


@pytest.mark.parametrize(
    ("error", "answer", "closed"),
    [
        (TimeoutError(), "PostgresUnavailableError", False),
        (asyncio.CancelledError(), "CancelledError", False),
        (ConnectionResetError(), "PostgresUnavailableError", True),
    ],
)
@pytest.mark.asyncio
async def test_a_connection_is_closed_only_when_it_is_lost(
    monkeypatch: pytest.MonkeyPatch, error: BaseException, answer: str, closed: bool
) -> None:
    """A timed-out or cancelled statement is left to the bounded release, which sends its
    cancel (closing the connection would stop it, and the server would run the
    statement on with its locks held); a lost connection is closed. The caller sees its
    own outcome."""
    from server.core import pg

    conn = _PgConn(fails=error)
    _pg_pool(monkeypatch, conn)
    with pytest.raises(BaseException) as raised:
        await pg.fetch("SELECT 1")
    assert type(raised.value).__name__ == answer and conn.terminated is closed


@pytest.mark.asyncio
async def test_a_release_that_ran_past_its_bound_never_replaces_the_blocks_outcome(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from server.core import pg

    _pg_pool(monkeypatch, _PgConn(), release=TimeoutError())
    assert await pg.fetchval("SELECT 1") == 1
    async with pg.transaction() as conn:  # committed: the release's failure is not its
        await conn.execute("SELECT 1")
    _pg_pool(monkeypatch, _PgConn(fails=asyncio.CancelledError()), release=TimeoutError())
    with pytest.raises(asyncio.CancelledError):
        await pg.fetch("SELECT 1")


@pytest.mark.parametrize(
    ("error", "answer"),
    [(TimeoutError(), "PostgresUnavailableError"), (asyncio.CancelledError(), "CancelledError")],
)
@pytest.mark.asyncio
async def test_a_transaction_whose_statement_is_in_flight_is_rolled_back_within_its_bound(
    monkeypatch: pytest.MonkeyPatch, error: BaseException, answer: str
) -> None:
    """The rollback is bounded (the release's bound), so a statement still in flight never
    holds the request for a second full timeout; the connection is not closed, so its
    cancel still reaches the server."""
    from server.core import pg

    conn = _PgConn()
    _pg_pool(monkeypatch, conn)
    with pytest.raises(BaseException) as raised:
        async with pg.transaction():
            raise error
    assert type(raised.value).__name__ == answer
    assert conn.rolled_back and not conn.terminated


@pytest.mark.parametrize(
    ("error", "answer"),
    [
        (None, None),
        (TimeoutError(), "PostgresUnavailableError"),
        (asyncio.CancelledError(), "CancelledError"),
    ],
)
@pytest.mark.asyncio
async def test_a_rolled_back_read_rolls_back_whatever_ends_it(
    monkeypatch: pytest.MonkeyPatch, error: BaseException | None, answer: str | None
) -> None:
    """An error is answered as `transaction` answers it in its block; a cancel is never
    marked as a commit that may have landed: there is none."""
    conn = _PgConn(commit=AssertionError("a rolled-back read commits nothing"))
    _pg_pool(monkeypatch, conn)
    if error is None:
        async with pg.rolled_back():
            pass
    else:
        with pytest.raises(BaseException) as raised:
            async with pg.rolled_back():
                raise error
        assert type(raised.value).__name__ == answer
        assert not pg.may_have_committed(raised.value)
    assert conn.rolled_back and not conn.terminated


@pytest.mark.asyncio
async def test_a_rolled_back_read_whose_begin_fails_answers_503(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class _NoBegin(_PgConn):
        def transaction(self) -> Any:
            tx = super().transaction()

            async def _start() -> None:
                raise TimeoutError()

            tx.start = _start
            return tx

    _pg_pool(monkeypatch, _NoBegin())
    with pytest.raises(pg.PostgresUnavailableError):
        async with pg.rolled_back():
            raise AssertionError("the block never runs")


@pytest.mark.parametrize(
    ("error", "answer"),
    [(TimeoutError(), "PostgresUnavailableError"), (asyncio.CancelledError(), "CancelledError")],
)
@pytest.mark.asyncio
async def test_a_commit_whose_answer_will_not_come_may_have_landed(
    monkeypatch: pytest.MonkeyPatch, error: BaseException, answer: str
) -> None:
    """A COMMIT waiting on the standby is cancelled by the client, never closed under it:
    the cancel ends the wait and frees its locks; the outcome is unknown."""
    from server.core import pg

    conn = _PgConn(commit=error)
    _pg_pool(monkeypatch, conn)
    with pytest.raises(BaseException) as raised:
        async with pg.transaction():
            pass
    assert type(raised.value).__name__ == answer and not conn.terminated
    assert pg.may_have_committed(raised.value)


@pytest.mark.parametrize(("in_block", "landed"), [(False, True), (True, False)])
@pytest.mark.asyncio
async def test_a_cancel_while_the_connection_is_released_is_marked_only_after_the_commit(
    monkeypatch: pytest.MonkeyPatch, in_block: bool, landed: bool
) -> None:
    """asyncpg shields the release but still raises the caller's cancel: after the COMMIT
    it may have landed; a cancel inside the block was rolled back."""
    from server.core import pg

    _pg_pool(monkeypatch, _PgConn(), release=asyncio.CancelledError())
    with pytest.raises(asyncio.CancelledError) as raised:
        async with pg.transaction():
            if in_block:
                raise asyncio.CancelledError()
    assert pg.may_have_committed(raised.value) is landed


@pytest.mark.parametrize("at", ["start", "statement_timeout"])
@pytest.mark.asyncio
async def test_a_cancel_before_the_commit_is_not_marked(
    monkeypatch: pytest.MonkeyPatch, at: str
) -> None:
    from server.core import pg

    class _Cancelled(_PgConn):
        async def execute(self, *a: object) -> str:
            if at == "statement_timeout":
                raise asyncio.CancelledError()
            return "SET"

        def transaction(self) -> Any:
            tx = super().transaction()
            if at == "start":

                async def _start() -> None:
                    raise asyncio.CancelledError()

                tx.start = _start
            return tx

    _pg_pool(monkeypatch, _Cancelled())
    with pytest.raises(asyncio.CancelledError) as raised:
        async with pg.transaction():
            pass
    assert not pg.may_have_committed(raised.value)


@pytest.mark.asyncio
async def test_a_parked_page_checks_at_most_two_windows(monkeypatch: pytest.MonkeyPatch) -> None:
    """A tail mostly stale is not read a window at a time: the first window, then four
    rows for each hit still missing, in one check; a page still short hands on the rest."""
    from server.memory.app.search import pg_validation
    from server.memory.app.search.output import _parked_slice

    calls: list[int] = []

    async def _flags(org: str, namespace: str, hits: list[Any]) -> list[bool]:
        calls.append(len(hits))
        return [h.memory_key != "live" for h in hits]

    monkeypatch.setattr(pg_validation, "stale_flags", _flags)
    payload = _parked_page([_hit(f"s{i}") for i in range(4)] + [_hit("live")])
    hits, nxt, dropped = await _parked_slice(_page_input(1), payload, 0, {"n"})
    assert [h.memory_key for h in hits] == ["live"] and (nxt, dropped) == (-1, 4)
    assert calls == [1, 4]
    calls.clear()
    payload = _parked_page([_hit(f"s{i}") for i in range(40)] + [_hit("live")])
    hits, nxt, dropped = await _parked_slice(_page_input(1), payload, 0, {"n"})
    assert (hits, nxt, dropped, calls) == ([], 5, 5, [1, 4])


@pytest.mark.asyncio
async def test_an_empty_parked_page_with_more_behind_it_does_not_abstain(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from server.memory.api.models_search import MemorySearchInput
    from server.memory.app import search_pages
    from server.memory.app.search import output, pg_validation

    async def _flags(org: str, namespace: str, hits: list[Any]) -> list[bool]:
        return [h.memory_key.startswith("s") for h in hits]

    payload = _parked_page([*(_hit(f"s{i}") for i in range(12)), _hit("live")])

    async def _load(*a: Any, **kw: Any) -> dict[str, Any]:
        return payload

    monkeypatch.setattr(pg_validation, "stale_flags", _flags)
    monkeypatch.setattr(search_pages, "load_page", _load)
    monkeypatch.setattr(output, "get_client", lambda: None)

    async def _page(offset: int) -> Any:
        handle = search_pages.make_handle("page/" + "b" * 16, offset)
        return await output._serve_parked_page(
            MemorySearchInput(org="o", namespace="n", query="q", top_k=1, page=handle)
        )

    out = await _page(0)
    assert out.results == [] and out.next_page and not out.abstain
    assert "no row of it is served now" in out.score_explanation
    last = await _page(12)
    assert [h.memory_key for h in last.results] == ["live"] and not last.next_page
    gone = await _page(13)
    assert gone.abstain and gone.abstain_code == "no_match"


@pytest.mark.parametrize(("depth", "cap"), [(0, None), (1, 0.4)])
@pytest.mark.asyncio
async def test_a_search_caps_its_barrier_wait_on_a_reentrant_pass_only(
    monkeypatch: pytest.MonkeyPatch, depth: int, cap: float | None
) -> None:
    """A first pass waits the barrier's own bound; a fallback or re-read pass no longer
    than its parent's time left."""
    from contextlib import ExitStack

    from server.memory.app.search import handler, pg_validation

    seen: list[tuple[str, list[str], float | None]] = []

    async def _admit(org: str, namespaces: Any, cap_s: float | None) -> bool:
        seen.append((org, list(namespaces), cap_s))
        return False

    monkeypatch.setattr(pg_validation, "admit", _admit)
    run = SimpleNamespace(namespaces=["n"], sequential_scope=ExitStack())
    budget = SimpleNamespace(wall_left_s=lambda: 0.4)
    search = SimpleNamespace(org="o", _pass_depth=depth)
    await handler._admit(search, run, budget)
    assert seen == [("o", ["n"], cap)]


@pytest.mark.parametrize(("cap", "bound"), [(None, 2.0), (0.3, 0.3)])
@pytest.mark.asyncio
async def test_only_a_reentrant_pass_waits_at_the_barrier_less_than_its_bound(
    monkeypatch: pytest.MonkeyPatch, cap: float | None, bound: float
) -> None:
    """A first pass waits up to LOOM_PG_PROJECTION_WAIT_MS whatever its fallback budget
    has left; a re-entrant pass no longer than its parent's time left."""
    import time

    from server.memory.app.search import pg_validation
    from server.settings import settings

    waits: list[float] = []

    async def _behind(org: str, namespaces: Any, owed: int) -> dict[str, int]:
        return {"n": 3}

    async def _wait(org: str, namespace: str, through: int, *, deadline: float, owed: int) -> None:
        waits.append(deadline - time.monotonic())

    monkeypatch.setattr(settings, "storage_authority", "postgres")
    monkeypatch.setattr(settings, "pg_projection_wait_ms", 2000)
    monkeypatch.setattr(pg_memory, "unsearchable_through", _behind)
    monkeypatch.setattr(pg_validation, "wait_searchable", _wait)
    assert await pg_validation.admit("o", ["n"], cap) is True
    assert bound - 0.1 < waits[0] <= bound


@pytest.mark.asyncio
async def test_a_verdict_linking_an_erased_memory_is_reached_again(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An erasure leaves no revision in the gap: the commit asks whether a memory the
    row links to was erased, and sends the write back to read its peers anew."""
    from contextlib import asynccontextmanager

    from server.core import pg

    erased = str(uuid.uuid4())
    asked: list[list[Any]] = []

    class _Conn:
        async def execute(self, sql: str, *args: object) -> None:
            return None

        async def fetch(self, sql: str, *args: Any) -> list[dict[str, Any]]:
            return [{"org": "o", "scope": "n", "generation": 5}]

        async def fetchval(self, sql: str, *args: Any) -> bool:
            asked.append(list(args[2]))
            return uuid.UUID(erased) in args[2]

    @asynccontextmanager
    async def _transaction():
        yield _Conn()

    monkeypatch.setattr(pg, "transaction", _transaction)
    own = str(uuid.uuid4())
    row = {
        "org": "o",
        "namespace": "n",
        "agent": "a",
        "user_id": "-",
        "session_id": "-",
        "memory_key": "k",
        "memory_id": own,
        "version": 1,
        "duplicate_of": erased,
        "conflict_with": [],
    }
    admission = pg_memory.Admission(gap_check=lambda rows: True)
    with pytest.raises(pg_memory.PeerErased):
        await pg_memory.commit_versions([row], [{}], admission=admission)
    assert asked == [[uuid.UUID(own)], [uuid.UUID(erased)]]


@pytest.mark.asyncio
async def test_a_commits_gap_with_erased_seqs_is_checked_on_the_rows_left() -> None:
    """The in-commit check of `_gap`'s rule: seqs 11 and 13 were erased, 12 is read."""
    seen: list[list[dict[str, Any]]] = []

    class _Conn:
        async def fetch(self, sql: str, *args: Any) -> list[dict[str, Any]]:
            return [{"row": {"memory_id": "m12"}}]

    def _check(rows: list[dict[str, Any]]) -> bool:
        seen.append(rows)
        return True

    admission = pg_memory.Admission(generations={("o", "n"): 10}, gap_check=_check)
    assert await pg_memory._gap_cleared(_Conn(), admission, {("o", "n"): 13}) is True
    assert seen == [[{"memory_id": "m12"}]]


@pytest.mark.asyncio
async def test_a_redaction_finds_a_version_clickhouse_does_not_hold_yet(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Under the Postgres authority the target comes from the revision journal, whose
    version a parked or pending delivery has not published."""
    from server.core import pg
    from server.memory.store import history
    from server.settings import settings

    monkeypatch.setattr(settings, "storage_authority", "postgres")
    mid = str(uuid.uuid4())
    head = {
        "org": "o",
        "namespace": "n",
        "agent": "a",
        "user_id": "u",
        "session_id": "-",
        "memory_key": "k",
        "memory_id": mid,
        "version": 7,
        "tombstoned_at": None,
    }
    asked: list[Any] = []

    async def _fetchrow(sql: str, *args: Any) -> dict[str, Any]:
        asked.append(args)
        return {"row": pg_memory.encode_row(head)}

    monkeypatch.setattr(pg, "fetchrow", _fetchrow)
    target = await history.find_history_target("o", mid, 7, ch=object())
    assert target is not None and (target.namespace, target.version) == ("n", 7)
    assert asked == [("o", uuid.UUID(mid), 7)]


@pytest.mark.asyncio
async def test_a_verdict_is_never_reached_against_an_erased_peer(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """ClickHouse still serves an erased peer until the erasure's ClickHouse steps run;
    the retry after `PeerErased` must not link it again."""
    from server.memory.app import conflicts
    from server.settings import settings

    monkeypatch.setattr(settings, "storage_authority", "postgres")
    gone, kept = str(uuid.uuid4()), str(uuid.uuid4())

    async def _erased(org: str, namespace: str, ids: list[str]) -> set[str]:
        return {gone} & set(ids)

    monkeypatch.setattr(pg_memory, "erased_ids", _erased)
    rows = [{"memory_id": gone}, {"memory_id": kept}]
    assert await conflicts._without_erased("o", "n", rows) == [{"memory_id": kept}]


def _delivery_pass(monkeypatch: pytest.MonkeyPatch, publish: Any) -> dict[str, list[Any]]:
    """`publish_claims` with every store call recorded; `publish(batch)` stands in for
    the ClickHouse publish."""

    from server.memory.app import pg_delivery

    seen: dict[str, list[Any]] = {"published": [], "released": [], "stubbed": [], "counted": []}

    async def _renew(batch: list[Any]) -> list[Any]:
        # New objects, as the store's `replace` returns.
        return [SimpleNamespace(**vars(c)) for c in batch]

    async def _publish(batch: list[Any], **_: Any) -> None:
        seen["published"].append([c.seq for c in batch])
        await publish(batch)

    async def _release(batch: list[Any]) -> None:
        seen["released"].extend(c.seq for c in batch)

    async def _stub(c: Any, run: Any) -> bool:
        seen["stubbed"].append(c.seq)
        return True

    async def _record(batch: list[Any], error: BaseException, **_: Any) -> int:
        seen["counted"].extend(c.seq for c in batch)
        return len(batch)

    monkeypatch.setattr(pg_memory, "renew", _renew)
    monkeypatch.setattr(pg_memory, "release", _release)
    monkeypatch.setattr(pg_memory, "record_failure", _record)
    monkeypatch.setattr(pg_delivery, "_publish", _publish)
    monkeypatch.setattr(pg_delivery, "_publish_stub", _stub)
    return seen


def _pass_claim(seq: int, *, ns: str = "n", attempts: int = 0, taken_over: bool = False) -> Any:
    return SimpleNamespace(org="o", namespace=ns, seq=seq, attempts=attempts, taken_over=taken_over)


@pytest.mark.asyncio
async def test_a_cancelled_pass_releases_what_it_had_not_started(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A deadline, a shutdown or a client gone: a claim not yet published had no
    outcome and counts no attempt; the publish that was running keeps its mark, so one
    that always outlasts the pass still counts and parks."""
    from server.memory.app import pg_delivery

    async def _hang(batch: list[Any]) -> None:
        raise asyncio.CancelledError

    seen = _delivery_pass(monkeypatch, _hang)
    claims = [_pass_claim(1, ns="a"), _pass_claim(2, ns="a"), _pass_claim(3, ns="b")]
    with pytest.raises(asyncio.CancelledError):
        await pg_delivery.publish_claims(claims)
    assert seen["published"] == [[1, 2]]
    assert seen["released"] == [3] and seen["counted"] == []


@pytest.mark.asyncio
async def test_a_claim_taken_over_from_a_stopped_publish_goes_alone(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """One that ended the process must not take its namespace's batch with it again;
    one past the limit gets only its stub."""
    from server.memory.app import pg_delivery
    from server.settings import settings

    async def _land(batch: list[Any]) -> None:
        return None

    seen = _delivery_pass(monkeypatch, _land)
    limit = settings.pg_delivery_max_attempts
    claims = [
        _pass_claim(1),
        _pass_claim(2, taken_over=True),
        _pass_claim(3),
        _pass_claim(4, attempts=limit),
    ]
    assert await pg_delivery.publish_claims(claims) == (4, 0)
    assert seen["published"] == [[1, 3], [2]] and seen["stubbed"] == [4]


@pytest.mark.asyncio
async def test_a_spent_claim_whose_stub_fails_is_parked(monkeypatch: pytest.MonkeyPatch) -> None:
    """Past the limit the stub is the last try: refused, the delivery is parked, never
    claimed again on every pass."""
    from server.memory.app import pg_delivery
    from server.settings import settings

    async def _land(batch: list[Any]) -> None:
        return None

    seen = _delivery_pass(monkeypatch, _land)
    parked: list[Any] = []

    async def _refused_stub(c: Any, run: Any) -> bool:
        raise RuntimeError("audit row refused")

    async def _record(batch: list[Any], error: BaseException, *, park: bool = False) -> int:
        parked.extend((c.seq, park) for c in batch)
        return len(batch)

    monkeypatch.setattr(pg_delivery, "_publish_stub", _refused_stub)
    monkeypatch.setattr(pg_memory, "record_failure", _record)
    spent = _pass_claim(1, attempts=settings.pg_delivery_max_attempts)
    assert await pg_delivery.publish_claims([spent]) == (0, 1)
    assert parked == [(1, True)] and seen["released"] == []


@pytest.mark.parametrize(
    ("answer", "up"), [({"ok": 1}, True), (None, False), (OverloadedError("busy"), False)]
)
@pytest.mark.asyncio
async def test_the_probe_is_a_plain_read_of_clickhouse(
    monkeypatch: pytest.MonkeyPatch, answer: Any, up: bool
) -> None:

    class _CH:
        async def query_one(self, sql: str, params: Any = None) -> Any:
            assert sql == "SELECT 1 AS ok"
            if isinstance(answer, Exception):
                raise answer
            return answer

    monkeypatch.setattr("server.core.ch.get_client", lambda: _CH())
    assert await ch_errors.clickhouse_answers() is up


@pytest.mark.asyncio
async def test_a_cancelled_admission_help_releases_everything(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A request gone, or its deadline, is no fault of another writer's revision."""
    from server.memory.app import pg_delivery

    async def _hang(batch: list[Any]) -> None:
        raise asyncio.CancelledError

    seen = _delivery_pass(monkeypatch, _hang)
    claims = [_pass_claim(1), _pass_claim(2)]
    with pytest.raises(asyncio.CancelledError):
        await pg_delivery.publish_claims(claims, worker=False)
    assert seen["released"] == [1, 2]


@pytest.mark.asyncio
async def test_a_postgres_error_in_the_last_stub_ends_the_pass_uncounted(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from server.core.pg import PostgresUnavailableError
    from server.memory.app import pg_delivery
    from server.settings import settings

    async def _land(batch: list[Any]) -> None:
        return None

    seen = _delivery_pass(monkeypatch, _land)

    async def _stub(c: Any, run: Any) -> bool:
        raise PostgresUnavailableError("failover")

    monkeypatch.setattr(pg_delivery, "_publish_stub", _stub)
    spent = _pass_claim(1, attempts=settings.pg_delivery_max_attempts)
    with pytest.raises(PostgresUnavailableError):
        await pg_delivery.publish_claims([spent])
    assert seen["counted"] == [] and seen["released"] == [1]


@pytest.mark.asyncio
async def test_an_admission_helping_counts_no_connection_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """It does not probe: a failed connection refuses nothing."""
    from clickhouse_connect.driver.exceptions import OperationalError

    async def _probe() -> bool:
        raise AssertionError("an admission helping does not probe")

    monkeypatch.setattr(ch_errors, "clickhouse_answers", _probe)
    error = OperationalError("Network Error: Server disconnected")
    assert await ch_errors.refused_nothing(error, probe=False) is True


@pytest.mark.asyncio
async def test_the_profile_keys_of_a_redaction_match_any_spelling_of_the_id(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The trace holds the id in its canonical form; the caller may send another."""
    from server.core import pg

    mid = uuid.uuid4()
    asked: list[Any] = []

    async def _fetch(sql: str, *args: Any) -> list[dict[str, Any]]:
        asked.append(args[3])
        return [{"agent": "-", "session_id": "-", "memory_key": "structured_profile", "newest": 4}]

    monkeypatch.setattr(pg, "fetch", _fetch)
    keys = await pg_memory.profile_keys_built_from("o", "n", "u", str(mid).upper())
    assert asked == [mid] and keys[0]["newest"] == 4
    assert await pg_memory.profile_keys_built_from("o", "n", "u", "not-an-id") == []


@pytest.mark.asyncio
async def test_a_copy_writer_commits_only_over_the_version_it_read(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A copy is built from the authority's head and commits only over that version: a
    value another write committed in between, or a redaction of the version read (in
    place, the version kept), refuses it with 503, and the retry reads the newer head."""
    from datetime import datetime

    from server.memory.store import lifecycle, pg_publish
    from server.memory.store.rows import MemoryRow
    from server.settings import settings

    monkeypatch.setattr(settings, "storage_authority", "postgres")
    mid = str(uuid.uuid4())
    read = MemoryRow(
        memory_id=mid,
        org="o",
        namespace="n",
        agent="a",
        user_id="-",
        session_id="-",
        memory_key="k",
        memory_type="fact",
        value="v",
        confidence=1.0,
        version=1,
        pinned=False,
        created_at=datetime(2026, 1, 1),
        expires_at=None,
    )

    async def _get_by_id(*_: Any, **__: Any) -> MemoryRow:
        return read

    heads: list[dict[str, Any]] = []

    async def _commit(rows: list[dict[str, Any]], *, admission: Any, **_: Any) -> Any:
        admission.recheck(identity_key(rows[0]), heads[-1])
        return pg_memory.Committed(rows=rows, audits=[], seqs=[], targets=[])

    async def _publish(committed: Any, **_: Any) -> None:
        return None

    monkeypatch.setattr(lifecycle, "get_by_id", _get_by_id)
    monkeypatch.setattr(pg_publish, "commit", _commit)
    monkeypatch.setattr(pg_publish, "publish", _publish)
    base = {"pinned": False, "status": "active", "lock_mode": "", "tombstoned_at": None}
    heads.append({**base, "version": 1})  # the version read
    assert (
        await lifecycle.write_versions_by_ids(
            "o", [mid], lambda r: {"status": "superseded"}, namespace="n", ch=object()
        )
        == 1
    )
    for moved in (
        {"version": 5, "value": '"newer"'},  # a newer value
        {"version": 1, "redacted_at": "2026-01-02T00:00:00"},  # the same version, scrubbed
    ):
        heads.append({**base, **moved})
        with pytest.raises(pg_memory.AdmissionContendedError):
            await lifecycle.write_versions_by_ids(
                "o", [mid], lambda r: {"status": "superseded"}, namespace="n", ch=object()
            )


@pytest.mark.asyncio
async def test_a_search_document_clickhouse_holds_is_not_built_again() -> None:
    """The held read's keys compare equal to the claim's, version as the driver reads it:
    otherwise every owed document is rebuilt over the writer's own."""
    from server.memory.store import pg_publish
    from server.memory.store.pg_publish import document_id

    class _CH:
        async def query_all(self, sql: str, params: Any = None) -> list[dict[str, Any]]:
            return [
                {"agent": "a", "user_id": "-", "session_id": "-", "memory_key": "k", "version": 7}
            ]

    row = {"agent": "a", "user_id": "-", "session_id": "-", "memory_key": "k", "version": 7}
    held = await pg_publish.held_documents("o", "n", [document_id(row)], ch=_CH())
    assert document_id(row) in held
    assert await pg_publish.held_documents("o", "n", [], ch=_CH()) == set()


def test_a_raw_socket_error_is_no_clickhouse_connection_failure() -> None:
    """The client turns its own transport failures into `BackendUnavailableError`; a bare
    socket or timeout error comes from elsewhere and counts."""
    from server.core.ch_errors import is_connection_failure

    assert not is_connection_failure(ConnectionError("x"))
    assert not is_connection_failure(TimeoutError())


@pytest.mark.asyncio
async def test_the_worker_stops_between_namespaces_once_its_time_is_up(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A backlog that queued behind others is released uncounted, not cut by the pass
    deadline in the middle of its publish."""
    from server.memory.app import pg_delivery
    from server.settings import settings

    now = {"t": 0.0}
    monkeypatch.setattr(pg_delivery.time, "monotonic", lambda: now["t"])

    async def _land(batch: list[Any]) -> None:
        now["t"] = settings.pg_delivery_lease_seconds + 1.0  # the first namespace took it all

    seen = _delivery_pass(monkeypatch, _land)
    claims = [_pass_claim(1, ns="a"), _pass_claim(2, ns="b")]
    assert await pg_delivery.publish_claims(claims) == (1, 0)
    assert seen["published"] == [[1]] and seen["released"] == [1, 2]


@pytest.mark.asyncio
async def test_a_pass_whose_time_runs_out_inside_a_namespace_reports_what_landed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Cut between two claims of the one-by-one loop, the pass counts the one it
    published and releases the rest uncounted."""
    from server.memory.app import pg_delivery
    from server.settings import settings

    now = {"t": 0.0}
    monkeypatch.setattr(pg_delivery.time, "monotonic", lambda: now["t"])

    async def _land(batch: list[Any]) -> None:
        now["t"] = settings.pg_delivery_lease_seconds + 1.0

    seen = _delivery_pass(monkeypatch, _land)
    claims = [_pass_claim(seq, taken_over=True) for seq in (1, 2, 3)]
    assert await pg_delivery.publish_claims(claims) == (1, 0)
    assert seen["published"] == [[1]] and seen["counted"] == []
    assert seen["released"] == [1, 2, 3]


@pytest.mark.asyncio
async def test_a_lock_over_a_head_redacted_since_its_read_answers_contended(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A copy writer with no head to read again answers 503: its retry reads the newer
    head, and the redacted text is never written back."""
    from server.memory.store import lifecycle, pg_publish
    from server.settings import settings

    monkeypatch.setattr(settings, "storage_authority", "postgres")
    read: dict[str, Any] = {}

    async def _commit(rows: list[dict[str, Any]], *, admission: Any, **_: Any) -> Any:
        # The head as read, redacted since: only the redaction differs.
        admission.recheck(identity_key(rows[0]), {**read, "redacted_at": "2026-01-01"})
        raise AssertionError("committed over a changed head")

    monkeypatch.setattr(pg_publish, "commit", _commit)
    monkeypatch.setattr("server.memory.store.lifecycle.check_memory_rows", lambda rows: None)
    read.update(
        {
            "org": "o",
            "namespace": "n",
            "agent": "a",
            "user_id": "-",
            "session_id": "-",
            "memory_key": "k",
            "version": 1,
            "pinned": False,
            "status": "active",
        }
    )
    row = {**read, "version": 2, "lock_mode": "read_only"}
    with pytest.raises(pg_memory.AdmissionContendedError):
        await lifecycle.insert_versions_over_heads(
            [row],
            [SimpleNamespace(**read)],
            ch=object(),
        )


@pytest.mark.asyncio
async def test_a_clickhouse_boot_without_migrations_serves_without_the_authority_table(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """With boot migrations off a process serves the schema it finds: the table may not
    exist yet, and nothing is written into a service that never asked for a record."""
    from clickhouse_connect.driver.exceptions import DatabaseError

    from server.core import pg_profile
    from server.settings import settings

    monkeypatch.setattr(settings, "storage_authority", "clickhouse")
    monkeypatch.setattr(settings, "boot_migrate", False)

    class _CH:
        async def query_one(self, sql: str, params: Any = None) -> Any:
            raise DatabaseError("Code: 60. DB::Exception: Unknown table loom.storage_authority")

        async def insert(self, *a: Any, **k: Any) -> None:
            raise AssertionError("no record is written")

    await pg_profile.require_authority(_CH())


class _RecordOnlyCH:
    """A ClickHouse whose authority table answers `recorded` (None: no row); it never
    takes a write."""

    def __init__(self, recorded: dict[str, Any] | None) -> None:
        self.recorded = recorded

    async def query_one(self, sql: str, params: Any = None) -> Any:
        return self.recorded

    async def insert(self, *a: Any, **k: Any) -> None:
        raise AssertionError("a process that runs no migrations writes no record")


@pytest.mark.asyncio
async def test_a_no_migration_boot_only_checks_the_record(monkeypatch: pytest.MonkeyPatch) -> None:
    from server.core import pg_profile
    from server.settings import settings

    monkeypatch.setattr(settings, "boot_migrate", False)
    monkeypatch.setattr(settings, "storage_authority", "clickhouse")
    await pg_profile.require_authority(_RecordOnlyCH(None))  # an empty table: nothing written
    postgres = {"authority": "postgres", "epoch": 1, "deployment_id": "d"}
    with pytest.raises(pg.AuthorityMismatch, match="postgres"):
        await pg_profile.require_authority(_RecordOnlyCH(postgres))

    async def _no_pg_record() -> None:
        return None

    monkeypatch.setattr(settings, "storage_authority", "postgres")
    monkeypatch.setattr(pg_profile, "_pg_record", _no_pg_record)
    with pytest.raises(pg.AuthorityMismatch, match="LOOM_BOOT_MIGRATE"):
        await pg_profile.require_authority(_RecordOnlyCH(None))


@pytest.mark.asyncio
async def test_a_restore_refused_by_an_erasure_answers_not_found(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An erasure since the restore's read refuses its commit with 404, a final answer; a
    lost answer that did not land is the 503 that asks for a retry, its outcome unknown."""
    import server.memory.store as memory_repo
    from server.core.errors import NotFoundError
    from server.memory.app import restore

    async def _erased(rows: Any, read: Any, **_: Any) -> None:
        raise pg_memory.MemoryErased()

    async def _lost(rows: Any, read: Any, **_: Any) -> None:
        raise ConnectionError("answer lost")

    async def _not_landed(*_: Any) -> bool | None:
        return None  # the re-read could not tell

    async def _mirror() -> None:
        return None

    monkeypatch.setattr(restore, "_restore_landed", _not_landed)
    row = {"version": 7, "memory_key": "k"}
    monkeypatch.setattr(memory_repo, "insert_versions_over_heads", _erased)
    with pytest.raises(NotFoundError):
        await restore._insert_restore(None, row, None, ch=None, what="the restore", mirror=_mirror)
    from server.core.errors import PreconditionFailedError

    async def _refused(rows: Any, read: Any, **_: Any) -> None:
        raise PreconditionFailedError("refused")

    monkeypatch.setattr(memory_repo, "insert_versions_over_heads", _refused)
    with pytest.raises(PreconditionFailedError):  # an answer the server gave stays itself
        await restore._insert_restore(None, row, None, ch=None, what="the restore", mirror=_mirror)
    monkeypatch.setattr(memory_repo, "insert_versions_over_heads", _lost)
    with pytest.raises(BackendUnavailableError) as lost:
        await restore._insert_restore(None, row, None, ch=None, what="the restore", mirror=_mirror)
    assert lost.value.extra["outcome_unknown"] is True


@pytest.mark.parametrize(
    ("error", "checked"),
    [
        (pg.PostgresUnavailableError("down"), False),
        (pg.PostgresUnavailableError("lost", outcome_unknown=True), True),
        (pg_lease.PublicationPending(), True),
        (pg_memory.AdmissionContendedError("moved"), False),
        (BackendTimeoutError("slow insert"), True),
        (BackendUnavailableError("reconnected"), True),
    ],
)
@pytest.mark.asyncio
async def test_a_restore_checks_the_key_only_when_its_outcome_is_unknown(
    monkeypatch: pytest.MonkeyPatch, error: Exception, checked: bool
) -> None:
    """A refusal before the commit is final; a lost answer, a ClickHouse insert that timed
    out or a commit whose publication is pending reads the key, and a restore that landed
    answers as written."""
    import server.memory.store as memory_repo
    from server.memory.app import restore

    reads: list[int] = []

    async def _insert(rows: Any, read: Any, **_: Any) -> None:
        raise error

    async def _landed(_input: Any, row: dict[str, Any]) -> bool:
        reads.append(row["version"])
        return True

    monkeypatch.setattr(memory_repo, "insert_versions_over_heads", _insert)
    monkeypatch.setattr(restore, "_restore_landed", _landed)

    async def _mirror() -> None:
        return None

    call = restore._insert_restore(
        None,
        {"version": 7, "memory_key": "k"},
        None,
        ch=None,
        what="the restore",
        mirror=_mirror,
    )
    if checked:
        await call
        assert reads == [7]
    else:
        with pytest.raises(type(error)):
            await call
        assert reads == []


@pytest.mark.parametrize(
    ("memory_type", "sensitivity", "owed"),
    [("semantic", "", True), ("episodic", "", False), ("semantic", "high", False)],
)
async def test_a_committed_restore_whose_document_failed_owes_it_only_when_embedded(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    memory_type: str,
    sensitivity: str,
    owed: bool,
) -> None:
    """Under Postgres the mirror's failure reaches the commit, which settles nothing,
    publishes and announces; the restore answers as written without reading the key. Only
    an embedded row's document is owed."""
    from types import SimpleNamespace

    from server.memory.app import restore
    from server.memory.store import lifecycle, pg_publish

    seen: list[Any] = []

    async def _commit(rows: Any, *, targets: list[int], **_: Any) -> Any:
        seen.append(("owed", targets))
        return SimpleNamespace(seqs=[1], deadline=None)

    async def _publish(committed: Any, **_: Any) -> None:
        seen.append("published")

    async def _complete(seqs: Any, targets: int) -> None:
        seen.append("settled")

    async def _mirror() -> None:
        raise RuntimeError("insert into loom.memory_embeddings unavailable")

    async def _announce() -> None:
        seen.append("announced")

    async def _reread(_input: Any, _row: Any) -> bool | None:
        raise AssertionError("a committed restore is not read again")

    monkeypatch.setattr(pg, "enabled", lambda: True)
    monkeypatch.setattr(lifecycle, "check_memory_rows", lambda rows: None)
    monkeypatch.setattr(pg_publish, "commit", _commit)
    monkeypatch.setattr(pg_publish, "publish", _publish)
    monkeypatch.setattr(pg_memory, "complete", _complete)
    monkeypatch.setattr(restore, "_restore_landed", _reread)
    row = _row("k", mt=memory_type, version=7, sensitivity=sensitivity)
    await restore._insert_restore(
        None, row, None, ch=None, what="the restore", mirror=_mirror, landed=_announce
    )
    targets = pg_memory.MEMORY_ROW | pg_memory.AUDIT_ROW
    if owed:
        targets |= pg_memory.SEARCH_DOC
    assert seen == [("owed", [targets]), "published", "announced"]
    outcome = "is left owed" if owed else "failed"
    assert f"search document {outcome} for k" in caplog.text


def _retired_row() -> MemoryRow:
    """A row retired at version 9, as an Undo reads it."""
    return MemoryRow(
        org="o",
        namespace="n",
        agent="a",
        user_id="u",
        session_id="s",
        memory_key="k",
        memory_type="semantic",
        value="v",
        confidence=0.9,
        version=9,
        created_at=datetime(2026, 9, 1),
        status="superseded",
        memory_id="mid",
        pinned=False,
        expires_at=None,
    )


async def test_under_postgres_the_status_before_a_retirement_is_read_from_the_authority(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """ClickHouse's history may still lack the `conflicted` revision the retirement went over
    (its publication owed): the status is read from the authority, by the row's key."""
    from server.memory.store import history

    asked: list[tuple[Any, str, int]] = []

    async def _status_before(key: Any, memory_id: str, version: int) -> str:
        asked.append((key, memory_id, version))
        return "conflicted"

    monkeypatch.setattr(pg, "enabled", lambda: True)
    monkeypatch.setattr(pg_memory, "status_before", _status_before)
    assert await history.status_before(_retired_row(), ch=None) == "conflicted"  # no ClickHouse
    assert asked == [(("o", "n", "a", "u", "s", "k"), "mid", 9)]


@pytest.mark.parametrize(("found", "status"), [({"was": "duplicate"}, "duplicate"), (None, "")])
async def test_on_clickhouse_the_status_before_a_retirement_is_read_by_the_rows_key(
    monkeypatch: pytest.MonkeyPatch, found: dict[str, Any] | None, status: str
) -> None:
    """A namespace restored into itself under another agent holds the id under another key:
    the history read names the key and the id, takes the newest version below the retirement
    that is not `superseded`, and answers nothing when none matches."""
    from server.memory.store import history

    sent: list[tuple[str, dict[str, Any]]] = []

    class _CH:
        async def query_one(self, sql: str, params: dict[str, Any]) -> dict[str, Any] | None:
            sent.append((sql, params))
            return found

    monkeypatch.setattr(pg, "enabled", lambda: False)
    assert await history.status_before(_retired_row(), ch=_CH()) == status
    [(sql, params)] = sent
    # Every clause but the row limit and sequential consistency is also shown against real
    # rows (`test_undo_ch.py`); all stay pinned here so the check runs without a ClickHouse.
    clauses = (
        "org = {org:String}",
        "namespace = {ns:String}",
        "agent = {a:String}",
        "user_id = {u:String}",
        "session_id = {s:String}",
        "memory_key = {k:String}",
        "memory_id = {id:UUID}",
        "version < {version:UInt64}",
        "status != 'superseded'",
        "ORDER BY version DESC LIMIT 1",
        "select_sequential_consistency = 1",
    )
    for clause in clauses:
        assert clause in sql
    key = (params["org"], params["ns"], params["a"], params["u"], params["s"], params["k"])
    assert key == ("o", "n", "a", "u", "s", "k")
    assert (params["id"], params["version"]) == ("mid", 9)


async def test_a_clickhouse_restore_whose_document_fails_is_announced_once_and_logged(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """The row is in: a failed search document is logged by key, never answered as a lost
    insert that would announce the restore again."""
    from server.memory.app import restore
    from server.memory.store import lifecycle

    ran: list[str] = []

    async def _insert(rows: Any, **_: Any) -> None:
        ran.append("insert")

    async def _mirror() -> None:
        raise RuntimeError("clickhouse refused the document")

    async def _announce() -> None:
        ran.append("announce")

    async def _reread(_input: Any, _row: Any) -> bool | None:
        raise AssertionError("a restore whose row is in is not read again")

    monkeypatch.setattr(pg, "enabled", lambda: False)
    monkeypatch.setattr(lifecycle, "insert_memory_versions", _insert)
    monkeypatch.setattr(restore, "_restore_landed", _reread)
    row = {"version": 7, "memory_key": "k", "memory_type": "semantic", "sensitivity": ""}
    await restore._insert_restore(
        None,
        row,
        None,
        ch=None,
        what="the restore",
        mirror=_mirror,
        landed=_announce,
    )
    assert ran == ["insert", "announce"]
    assert "search document failed for k" in caplog.text


async def test_a_clickhouse_restore_that_lost_its_answer_still_announces_and_mirrors(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The insert landed but answered an error, so nothing after it ran: once the key reads
    as restored, the announce and the search document run."""
    from server.memory.app import restore
    from server.memory.store import lifecycle

    ran: list[str] = []

    async def _insert(rows: Any, **_: Any) -> None:
        raise BackendTimeoutError("slow insert")

    async def _landed(_input: Any, _row: Any) -> bool | None:
        return True

    async def _mirror() -> None:
        ran.append("mirror")

    async def _announce() -> None:
        ran.append("announce")

    monkeypatch.setattr(pg, "enabled", lambda: False)
    monkeypatch.setattr(lifecycle, "insert_memory_versions", _insert)
    monkeypatch.setattr(restore, "_restore_landed", _landed)
    row = {"version": 7, "memory_key": "k", "memory_type": "semantic", "sensitivity": ""}
    await restore._insert_restore(
        None, row, None, ch=None, what="the restore", mirror=_mirror, landed=_announce
    )
    assert ran == ["announce", "mirror"]


@pytest.mark.parametrize(
    ("committed", "reread", "fenced"),
    [
        (True, None, [True]),
        (False, True, [True]),  # the answer was lost, the re-read finds it landed
        (False, False, []),  # settled: the head never took it
        (False, None, [False]),  # unknown: fenced on the context's own settled read
    ],
)
async def test_a_postgres_restore_of_a_high_key_is_fenced_once_its_commit_may_have_landed(
    monkeypatch: pytest.MonkeyPatch, committed: bool, reread: bool | None, fenced: list[bool]
) -> None:
    """The restore marks its commit answered from inside the insert, or from its re-read
    after a lost answer; a re-read that settles it as not landed answers a plain 503."""
    import server.memory.store as memory_repo
    from server.memory.app import high_fence, restore

    seen: list[bool] = []

    async def _over_heads(rows: Any, read: Any, *, on_commit: Any, **_: Any) -> None:
        if committed:
            on_commit()
            return
        raise pg.PostgresUnavailableError("lost", outcome_unknown=True)

    async def _landed(_input: Any, _row: Any) -> bool | None:
        return reread

    async def _fence(items: Any, *, op: str, confirmed: bool = True, **_: Any) -> bool:
        seen.append(confirmed)
        return True

    async def _mirror() -> None:
        pass

    monkeypatch.setattr(pg, "enabled", lambda: True)
    monkeypatch.setattr(memory_repo, "insert_versions_over_heads", _over_heads)
    monkeypatch.setattr(restore, "_restore_landed", _landed)
    monkeypatch.setattr(high_fence, "fence_committed", _fence)
    monkeypatch.setattr(pg_memory, "settled_head_versions", _no_head)
    row = _row("k", version=7, sensitivity="high")
    with contextlib.suppress(BackendUnavailableError):
        async with high_fence.fenced_once_committed(
            lambda: [(identity_key(row), 7)], op="memory.restore"
        ) as answer:
            await restore._insert_restore(
                None,
                row,
                None,
                ch=None,
                what="the restore",
                mirror=_mirror,
                on_commit=answer.mark_committed,
            )
    assert seen == fenced


@pytest.mark.asyncio
async def test_a_boot_without_migrations_lists_the_postgres_files_it_skips(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The skipped files reach `/v1/health` as `pg:<file>`, once however often the step
    runs; a ledger that disagrees with the build refuses the boot, and one that cannot
    be read is listed as unknown."""
    from server import boot
    from server.core import pg, pg_profile

    async def _start() -> list[str]:
        return ["002_more.sql"]

    async def _nothing(ch: Any) -> None:
        return None

    monkeypatch.setattr(pg, "enabled", lambda: True)
    monkeypatch.setattr(pg_profile, "start", _start)
    monkeypatch.setattr(pg_profile, "require_authority", _nothing)
    monkeypatch.setattr(boot, "SKIPPED_PENDING_MIGRATIONS", ("900_ch.sql",))
    monkeypatch.setattr(boot, "SKIPPED_PG_MIGRATIONS", ())
    for _ in range(2):
        await boot.require_storage_authority(object())
    assert boot.skipped_migrations() == ("900_ch.sql", "pg:002_more.sql")

    async def _drifted() -> list[str]:
        raise pg_migrate.MigrationDrift("001_memory_authority.sql was edited")

    async def _unreadable() -> list[str]:
        raise OSError("connection reset")

    monkeypatch.setattr(pg_migrate, "pending", _drifted)
    with pytest.raises(pg_migrate.MigrationDrift):
        await pg_profile.pending_files()
    monkeypatch.setattr(pg_migrate, "pending", _unreadable)
    assert await pg_profile.pending_files() == ["<ledger unreadable>"]


@pytest.mark.asyncio
async def test_a_skipped_postgres_file_keeps_the_replica_not_ready(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """With ClickHouse's schema current, a Postgres file the boot skipped still fails the
    `migrations` readiness check, as a skipped ClickHouse file does; a ledger the boot
    could not read is read again."""
    from server import boot
    from server.core import readiness
    from server.core.schema import status

    checks: dict[str, Any] = {}
    monkeypatch.setattr(
        readiness, "register", lambda name, check, **_: checks.update({name: check})
    )

    async def _current() -> dict[str, Any]:
        return {"pending": []}

    monkeypatch.setattr(status, "schema_status", _current)
    monkeypatch.setattr(boot, "SKIPPED_PENDING_MIGRATIONS", ())
    boot.register_readiness_checks(object())
    monkeypatch.setattr(boot, "SKIPPED_PG_MIGRATIONS", ())
    assert (await checks["migrations"]()).ok
    monkeypatch.setattr(boot, "SKIPPED_PG_MIGRATIONS", ("pg:002_more.sql",))
    answer = await checks["migrations"]()
    assert not answer.ok and "pg:002_more.sql" in answer.reason

    # A ledger the boot could not read is read again, and the replica is ready once it is.
    from server.core import pg_profile

    async def _none_pending() -> list[str]:
        return []

    monkeypatch.setattr(pg_profile, "pending_files", _none_pending)
    monkeypatch.setattr(boot, "SKIPPED_PG_MIGRATIONS", (f"pg:{pg_profile.LEDGER_UNREADABLE}",))
    assert (await checks["migrations"]()).ok and boot.SKIPPED_PG_MIGRATIONS == ()


@pytest.mark.asyncio
async def test_an_unreadable_ledger_is_logged_once_until_it_reads_again(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """Readiness reads an unreadable ledger on every probe; the warning comes once, and
    again after a read in between succeeded."""
    import logging

    from server.core import pg_profile

    reads = iter(["fail", "fail", "fail", "ok", "fail"])

    async def _pending() -> list[str]:
        if next(reads) == "fail":
            raise OSError("connection reset")
        return []

    monkeypatch.setattr(pg_migrate, "pending", _pending)
    monkeypatch.setattr(pg_profile, "_ledger_unread", False)
    with caplog.at_level(logging.WARNING, logger=pg_profile.log.name):
        answers = [await pg_profile.pending_files() for _ in range(5)]
    assert answers == [[pg_profile.LEDGER_UNREADABLE]] * 3 + [[]] + [[pg_profile.LEDGER_UNREADABLE]]
    failed = [r for r in caplog.records if "pending-migration check failed" in r.getMessage()]
    assert len(failed) == 2


class _BusyTable:
    """A migration connection whose table stays locked for its first `busy` tries."""

    def __init__(self, busy: int) -> None:
        self.busy = busy
        self.recorded: list[str] = []

    def transaction(self) -> Any:
        return contextlib.nullcontext()

    async def execute(self, sql: str, *args: Any) -> None:
        import asyncpg

        if sql.startswith("INSERT INTO loom.pg_migrations"):
            self.recorded.append(args[0])
        elif self.busy:
            self.busy -= 1
            raise asyncpg.exceptions.LockNotAvailableError("55P03")


@pytest.mark.asyncio
@pytest.mark.parametrize(("busy", "applied"), [(4, True), (5, False)])
async def test_a_migration_waiting_on_a_lock_is_tried_five_times(
    monkeypatch: pytest.MonkeyPatch, busy: int, applied: bool
) -> None:
    import asyncpg

    async def _no_sleep(_: float) -> None:
        return None

    monkeypatch.setattr(pg_migrate, "sleep", _no_sleep)
    conn = _BusyTable(busy)
    if applied:
        await pg_migrate._apply(conn, "002_x.sql", "ALTER TABLE t ADD COLUMN x int", "d")
        assert conn.recorded == ["002_x.sql"]
    else:
        with pytest.raises(asyncpg.exceptions.LockNotAvailableError):
            await pg_migrate._apply(conn, "002_x.sql", "ALTER TABLE t ADD COLUMN x int", "d")
        assert conn.recorded == []


@pytest.mark.asyncio
async def test_a_redaction_keeps_the_chunks_a_searchable_head_writes_on_its_key(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Under Postgres a profile key's name may hold a long searchable value in another
    session: the redaction reads it from the authority and keeps its chunks, as far as
    they were written. The ClickHouse authority keeps what it holds as live."""
    import server.memory.store as memory_repo
    from server.memory.app import curation
    from server.memory.app.set_chunks import set_chunk_identities
    from server.settings import settings

    value = "\n\n".join(f"Paragraph {i}: the incident notes run on. " * 12 for i in range(40))

    asked: list[Any] = []

    async def _heads(org: str, ids: Any, *, servable: bool = True) -> dict[Any, Any]:
        asked.append((org, list(ids), servable))
        return {("n", "-", "u", "profile"): [(value, "semantic")]}

    async def _count(*_: Any) -> int:
        return 60

    monkeypatch.setattr(pg, "enabled", lambda: True)
    monkeypatch.setattr(pg_memory, "chunk_heads", _heads)
    monkeypatch.setattr(memory_repo, "set_chunk_count", _count)
    monkeypatch.setattr(settings, "memory_set_chunk_max_chunks", 4)  # lowered since
    kept = await curation._kept_set_chunks("o", "n", "-", "u", "profile")
    planned = set_chunk_identities(value, "semantic", "profile", at_least=60)
    assert kept == sorted({sha for _, sha in planned})
    assert len(kept) > len(set_chunk_identities(value, "semantic", "profile"))
    # Every live head in any session, those search does not serve included.
    assert asked == [("o", [("n", "-", "u", "profile")], False)]
    monkeypatch.setattr(pg, "enabled", lambda: False)
    assert await curation._kept_set_chunks("o", "n", "-", "u", "profile") is None


def test_a_gap_row_no_entry_pairs_with_may_hold_a_stubs_empty_vector() -> None:
    """A row of another type is measured against no entry: its stub's empty vector does
    not stop the gap from clearing the batch."""
    entries = [("semantic", "k1", [1.0, 0.0], 0.2)]
    rows = [
        {"memory_type": "semantic", "memory_key": "k2"},
        {"memory_type": "episodic", "memory_key": "k3"},
    ]
    assert pg_admission._clear([[0.0, 1.0], []], rows, entries) is True


@pytest.mark.parametrize(
    ("vector", "entries"),
    [
        ([1.0, 0.0, 0.0], [("semantic", "k1", [0.0, 1.0], 0.2)]),
        ([0.0, 1.0], [("semantic", "k1", [1.0, 0.0], 0.2), ("episodic", "k9", [1.0], 0.2)]),
    ],
    ids=["row-of-another-width", "entries-of-two-widths"],
)
def test_a_gap_of_mixed_widths_is_not_cleared(
    vector: list[float], entries: list[tuple[str, str, list[float], float]]
) -> None:
    """A width the batch's entries do not share cannot be measured: the gap is not clear
    and the batch starts again."""
    rows = [{"memory_type": "semantic", "memory_key": "k2"}]
    assert pg_admission._clear([vector], rows, entries) is False


@pytest.mark.asyncio
async def test_a_gap_is_measured_by_the_embedders_own_model(
    monkeypatch: pytest.MonkeyPatch,
) -> None:

    from server.memory.store import embeddings

    asked: list[str] = []

    async def _vectors(org: str, ns: str, idents: Any, *, model: str) -> dict[Any, Any]:
        asked.append(model)
        return {}

    async def _searchable(*_: Any) -> None:
        return None

    monkeypatch.setattr(embeddings, "live_vectors", _vectors)
    monkeypatch.setattr(pg_admission, "wait_searchable", _searchable)
    monkeypatch.setattr(
        "server.embedding.get_embedder", lambda: SimpleNamespace(model_name="model-now")
    )
    row = {
        "org": "o",
        "namespace": "n",
        "agent": "a",
        "user_id": "u",
        "session_id": "-",
        "memory_key": "k",
    }
    assert await pg_admission._measure("o", "n", 1, [row]) == [None]
    assert asked == ["model-now"]


async def test_a_restore_leaves_out_the_memories_whose_erasure_evidence_is_kept(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Under Postgres the snapshot's memories an erasure removed from the target are left
    out and counted; on the ClickHouse authority every memory is restored, with no read.
    An erasure that lands before the commit refuses it as a precondition."""

    from server.core.errors import PreconditionFailedError
    from server.memory.app import snapshot_rows

    async def _erased(org: str, ns: str, ids: list[str]) -> set[str]:
        assert pg.enabled()
        return {"erased"}

    async def _commit(*_: Any, **__: Any) -> int:
        raise pg_memory.MemoryErased()

    monkeypatch.setattr(pg_memory, "erased_ids", _erased)
    monkeypatch.setattr(snapshot_rows.store, "insert_memory_versions", _commit)
    kept, erased = SimpleNamespace(memory_id="kept"), SimpleNamespace(memory_id="erased")

    monkeypatch.setattr(pg, "enabled", lambda: False)
    assert await snapshot_rows.without_erased("o", "n", [kept, erased]) == ([kept, erased], 0)
    monkeypatch.setattr(pg, "enabled", lambda: True)
    assert await snapshot_rows.without_erased("o", "n", [kept, erased]) == ([kept], 1)
    with pytest.raises(PreconditionFailedError, match="restore it again"):
        await snapshot_rows.insert_version_rows([{}], ch=None)


@pytest.mark.parametrize("confidence", [float("nan"), float("inf"), 1e39])
def test_a_non_finite_or_out_of_range_float32_is_refused(confidence: float) -> None:
    """The jsonb row cannot hold NaN or Infinity, nor a Float32 column a value past its
    range: a write carrying one is a 422, not a failed commit."""
    from server.core.errors import ValidationError

    with pytest.raises(ValidationError, match="confidence"):
        pg_memory.encode_row({"confidence": confidence})


async def test_a_restore_commits_in_batches_and_leaves_a_pending_rest_to_the_worker(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A large restore commits batch by batch, each within a commit's lease. Once one's
    publication is pending, the rest are only committed, without waiting on ClickHouse
    again, and the restore completes: the delivery worker publishes them."""
    from server.memory.app import snapshot_rows
    from server.settings import settings

    calls: list[tuple[int, bool]] = []

    async def _insert(rows: Any, *, ch: Any, publish_now: bool = True) -> int:
        calls.append((len(rows), publish_now))
        if len(calls) == 2:
            raise pg_lease.PublicationPending()
        return len(rows)

    monkeypatch.setattr(pg, "enabled", lambda: True)
    monkeypatch.setattr(settings, "pg_restore_batch_rows", 2)
    monkeypatch.setattr(snapshot_rows.store, "insert_memory_versions", _insert)
    assert await snapshot_rows.insert_version_rows([{}] * 7, ch=None) is True
    assert calls == [(2, True), (2, True), (2, False), (1, False)]


async def test_an_erasure_between_batches_refuses_the_batch_it_meets(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The batches before it stay committed; the restore answers the precondition and
    tries no later batch."""
    from server.core.errors import PreconditionFailedError
    from server.memory.app import snapshot_rows
    from server.settings import settings

    tried: list[int] = []

    async def _commit(rows: Any, **_: Any) -> int:
        tried.append(len(rows))
        if len(tried) == 2:
            raise pg_memory.MemoryErased()
        return len(rows)

    monkeypatch.setattr(pg, "enabled", lambda: True)
    monkeypatch.setattr(settings, "pg_restore_batch_rows", 2)
    monkeypatch.setattr(snapshot_rows.store, "insert_memory_versions", _commit)
    with pytest.raises(PreconditionFailedError):
        await snapshot_rows.insert_version_rows([{}] * 6, ch=None)
    assert tried == [2, 2]


async def test_a_decision_record_is_judged_by_the_writers_the_authority_holds(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """ClickHouse may not hold a committed tombstone yet: under Postgres the record's
    writer is live only if the authority holds its decision head."""
    from server.memory.store import records

    class _CH:
        def __init__(self) -> None:
            self.sent: list[tuple[str, dict[str, Any], dict[str, Any]]] = []

        async def execute(self, sql: str, params: Any = None, settings: Any = None) -> None:
            self.sent.append((sql, params, settings))

    async def _writers(org: str, ns: str, keys: list[str]) -> list[tuple[str, str, str, str]]:
        return [("k", "a", "-", "-")]

    from server.core import rmt_carry
    from server.settings import settings

    monkeypatch.setattr(pg, "enabled", lambda: True)
    monkeypatch.setattr(settings, "ch_timeout_write_s", 0)
    monkeypatch.setattr(pg_memory, "live_decision_writers", _writers)

    async def _deadline(ch: Any) -> float:
        return 1_700_000_000.0

    monkeypatch.setattr(rmt_carry, "measured_send_deadline", _deadline)
    ch = _CH()
    await records.tombstone_decision_records("o", "n", ["k"], version=7, ch=ch)
    ((sql, params, limits),) = ch.sent
    assert "FROM loom.memory FINAL" not in sql
    assert params["live"] == [("k", "a", "-", "-")]
    assert params["start_by"] == 1_700_000_000_000  # starts by the server's send deadline
    # Bounded with no write timeout too, as long as an erasure waits for it.
    assert limits == {"max_execution_time": 60}
    assert pg.insert_bound_s() == 15 + 60 + 5


async def test_an_audit_insert_is_sent_under_its_claim() -> None:
    """An outbox claimer's audit insert starts on the server within the transit allowance
    of its claim's `start_by`, as an insert under a lease does, so it cannot land after
    the claim ran out and the next claimer inserted the row again."""
    from server.core import rmt_carry
    from server.memory.store import pg_publish

    sent_under: list[float | None] = []

    class _CH:
        async def insert(self, table: str, rows: Any, settings: Any = None) -> None:
            sent_under.append(rmt_carry._lease_end.get())

    start_by = time.monotonic() + 30
    await pg_publish.insert_audit_rows([{"row_hash": "h"}], ch=_CH(), retried=False)
    await pg_publish.insert_audit_rows(
        [{"row_hash": "h"}], ch=_CH(), retried=False, start_by=start_by
    )
    assert sent_under == [None, start_by]


async def test_a_writers_record_is_live_only_by_the_heads_the_authority_holds(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A decision row ClickHouse still holds but the authority deleted leaves its record
    to the extractor, and one the authority holds but ClickHouse lacks keeps it."""
    from server.memory.store import records

    writers: list[tuple[str, str, str, str]] = []

    class _CH:
        def __init__(self) -> None:
            self.sent: list[tuple[str, dict[str, Any]]] = []

        async def query_one(self, sql: str, params: Any = None) -> dict[str, Any]:
            self.sent.append((sql, params))
            return {
                "identity_agent": "a",
                "user_id": "-",
                "session_id": "-",
                "record_json": "{}",
                "created_at": None,
            }

    async def _writers(org: str, ns: str, keys: list[str]) -> list[tuple[str, str, str, str]]:
        return writers

    monkeypatch.setattr(pg, "enabled", lambda: True)
    monkeypatch.setattr(pg_memory, "live_decision_writers", _writers)
    ch = _CH()
    assert await records.live_writer_record("o", "n", "k", ch=ch) is None
    assert ch.sent == []
    writers.append(("k", "a", "-", "-"))
    found = await records.live_writer_record("o", "n", "k", ch=ch)
    assert found is not None and found.agent == "a"
    ((sql, params),) = ch.sent
    assert "FROM loom.memory FINAL" not in sql
    assert params["live"] == writers


async def test_a_snapshot_under_postgres_reads_the_authoritys_heads(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A delete ClickHouse has not published, or never will (a parked delivery), is not
    snapshotted: the heads come from the authority, every page, oldest first."""
    from server.memory.app import snapshot_rows

    pages = [
        [
            {"created_at": datetime(2026, 1, 2), "k": 2},
            {"created_at": datetime(2026, 1, 1), "k": 1},
        ],
        [{"created_at": datetime(2026, 1, 3), "k": 3}],
        [],
    ]
    asked: list[Any] = []

    async def _page(org: str, ns: str, *, after: Any = None) -> list[dict[str, Any]]:
        asked.append(after)
        return pages[len(asked) - 1]

    monkeypatch.setattr(pg, "enabled", lambda: True)
    monkeypatch.setattr(pg_memory, "live_heads_in", _page)
    monkeypatch.setattr(snapshot_rows, "identity_key", lambda row: row["k"])
    rows = await snapshot_rows.read_namespace(None, org="o", namespace="n")
    assert [r["k"] for r in rows] == [1, 2, 3]
    assert asked == [None, 1, 3]


async def test_an_inventory_behind_its_projection_answers_it(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from server.identity.api.models_inventory import NamespaceInventoryInput
    from server.identity.app import namespace_inventory
    from server.memory.app.pg_admission import ProjectionBehindError

    async def _behind(*_: Any, **__: Any) -> bool:
        raise ProjectionBehindError("behind")

    monkeypatch.setattr(namespace_inventory.pg_validation, "admit", _behind)
    with pytest.raises(ProjectionBehindError):
        await namespace_inventory.handle_namespace_inventory(
            NamespaceInventoryInput(org="o", namespace="n")
        )


async def test_an_imports_key_heads_come_from_the_authority(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """memory.import decides from the heads: under Postgres, the authority's, so a delete
    ClickHouse has not published reads as deleted."""
    from server.identity.domain.read_scope import WHOLE_NAMESPACE
    from server.memory.store import reads

    async def _heads(*args: Any, **kwargs: Any) -> list[dict[str, Any]]:
        return [{"memory_key": "k", "deleted": True}]

    monkeypatch.setattr(pg, "enabled", lambda: True)
    monkeypatch.setattr(pg_memory, "key_heads_across_sessions", _heads)
    got = await reads.key_heads_across_sessions(
        "o", "n", "a", "-", ["k"], scope=WHOLE_NAMESPACE, ch=object()
    )
    assert got == [{"memory_key": "k", "deleted": True}]


async def test_a_copys_follow_up_left_by_an_ended_lease_answers_none_copied(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A commit whose lease ended before its follow-up ran is published and answered with
    nothing copied; the heal copies what the follow-up would have."""
    from server.memory.store import lifecycle, pg_publish

    order: list[str] = []

    async def _commit(rows: Any, **_: Any) -> Any:
        order.append("commit")
        return SimpleNamespace(rows=rows, deadline=time.monotonic() - 1)

    async def _publish(*_: Any, **__: Any) -> None:
        order.append("publish")

    async def _then() -> int:
        order.append("then")
        return 1

    monkeypatch.setattr(pg, "enabled", lambda: True)
    monkeypatch.setattr("server.memory.store.lifecycle.check_memory_rows", lambda rows: None)
    monkeypatch.setattr(pg_publish, "commit", _commit)
    monkeypatch.setattr(pg_publish, "publish", _publish)
    copy = {"org": "o", "namespace": "n", "agent": "a", "user_id": "-", "session_id": "-"}
    answer = await lifecycle.insert_versions_over_heads(
        [{**copy, "memory_key": "k", "version": 2}],
        [SimpleNamespace(version=1)],
        ch=None,
        then=_then,
    )
    assert answer == 0 and order == ["commit", "publish"]


async def test_a_copys_follow_up_runs_under_its_lease_and_cannot_undo_the_commit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The follow-up runs under the commit's lease before the publish clears the delivery
    (a redaction waits it out); one that fails is answered once the commit is published."""
    from server.core import rmt_carry
    from server.memory.store import lifecycle, pg_publish

    order: list[str] = []

    async def _commit(rows: Any, **_: Any) -> Any:
        order.append("commit")
        return SimpleNamespace(rows=rows, deadline=time.monotonic() + 60)

    async def _publish(*_: Any, **__: Any) -> None:
        order.append("publish")

    async def _then() -> int:
        order.append("leased" if rmt_carry._lease_end.get() is not None else "unleased")
        raise RuntimeError("ClickHouse refused the copy")

    monkeypatch.setattr(pg, "enabled", lambda: True)
    monkeypatch.setattr("server.memory.store.lifecycle.check_memory_rows", lambda rows: None)
    monkeypatch.setattr(pg_publish, "commit", _commit)
    monkeypatch.setattr(pg_publish, "publish", _publish)
    copy = {"org": "o", "namespace": "n", "agent": "a", "user_id": "-", "session_id": "-"}
    with pytest.raises(RuntimeError, match="refused the copy"):
        await lifecycle.insert_versions_over_heads(
            [{**copy, "memory_key": "k", "version": 2}],
            [SimpleNamespace(version=1)],
            ch=None,
            then=_then,
        )
    assert order == ["commit", "leased", "publish"]


async def test_a_copy_whose_publication_is_pending_is_done(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A copy of a head committed over that head is done: the delivery worker publishes
    it, and the caller is not told to retry a write it must not repeat."""

    from server.memory.store import lifecycle, pg_publish

    committed: list[Any] = []

    async def _commit(rows: Any, **_: Any) -> Any:
        committed.extend(rows)
        return SimpleNamespace(rows=rows)

    async def _publish(*_: Any, **__: Any) -> None:
        raise pg_lease.PublicationPending()

    monkeypatch.setattr(pg, "enabled", lambda: True)
    monkeypatch.setattr("server.memory.store.lifecycle.check_memory_rows", lambda rows: None)
    monkeypatch.setattr(pg_publish, "commit", _commit)
    monkeypatch.setattr(pg_publish, "publish", _publish)
    row = {"org": "o", "namespace": "n", "agent": "a", "user_id": "-", "session_id": "-"}
    copy = {**row, "memory_key": "k", "version": 2}
    head = SimpleNamespace(version=1)
    assert await lifecycle.insert_versions_over_heads([copy], [head], ch=None) == 0
    assert committed == [copy]


async def test_a_by_id_write_under_postgres_is_confirmed_unless_overtaken(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The commit is over each head read and keeps the newer version: a row is confirmed
    and mirrored unless it is older than the head it was read from, published or not;
    ClickHouse, which may not hold them yet, is not asked."""

    from server.memory.store import lifecycle

    mirrored: list[Any] = []
    heard: list[Any] = []
    unconfirmed: list[Any] = []
    read = {"m1": 5, "m2": 9}  # m2's head moved past the version drawn for it

    async def _row(mid: str, **_: Any) -> Any:
        return SimpleNamespace(memory_id=mid, version=read[mid])

    async def _insert(*_: Any, **__: Any) -> None:
        return None

    async def _mirror(rows: Any, **_: Any) -> None:
        mirrored.extend(rows)

    async def _unconfirmed(rows: Any) -> None:
        unconfirmed.extend(rows)

    async def _no_reread(*_: Any, **__: Any) -> Any:
        raise AssertionError("read ClickHouse")

    identity = {"namespace": "n", "agent": "a", "user_id": "-", "session_id": "-"}
    monkeypatch.setattr(pg, "enabled", lambda: True)
    monkeypatch.setattr(lifecycle, "get_by_id", _row)
    monkeypatch.setattr(
        lifecycle,
        "build_memory_version_row",
        lambda e, **kw: {**identity, "memory_key": e.memory_id, **kw},
    )
    monkeypatch.setattr(lifecycle, "insert_versions_over_heads", _insert)
    monkeypatch.setattr(lifecycle, "mirror_status_to_embeddings", _mirror)
    monkeypatch.setattr(lifecycle, "_head_versions", _no_reread)
    written = await lifecycle.write_versions_by_ids(
        "o",
        ["m1", "m2"],
        lambda e: {"status": "superseded", "version": 7},
        namespace="n",
        ch=object(),
        on_written=heard.extend,
        on_unconfirmed=_unconfirmed,
    )
    assert written == 1
    assert [r["memory_key"] for r in heard] == ["m1"]
    assert unconfirmed == []
    assert [r["memory_key"] for r in mirrored] == ["m1"]


async def test_a_cleared_page_is_announced_before_its_vectors_retire(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """memory.clear under Postgres: a page committed with its publication pending is
    announced, and a failure retiring its vectors then answers that failure."""

    from server.memory.store import lifecycle, pg_publish

    head = {
        "org": "o",
        "namespace": "n",
        "agent": "a",
        "user_id": "-",
        "session_id": "-",
        "memory_key": "k",
        "memory_id": "m1",
        "version": 1,
    }
    announced: list[Any] = []

    async def _page(*_: Any, after: Any = None, **__: Any) -> list[dict[str, Any]]:
        return [] if after else [head]

    async def _commit(rows: Any, **_: Any) -> Any:
        return SimpleNamespace(rows=rows)

    async def _publish(*_: Any, **__: Any) -> None:
        raise pg_lease.PublicationPending()

    async def _announce(rows: Any) -> None:
        announced.extend(rows)

    async def _retire(*_: Any, **__: Any) -> int:
        raise ConnectionError("clickhouse down")

    monkeypatch.setattr(pg_memory, "live_heads_in", _page)
    monkeypatch.setattr(lifecycle, "_hydrate", lambda h: SimpleNamespace(**h))
    monkeypatch.setattr(lifecycle, "build_memory_version_row", lambda h, **kw: {**vars(h), **kw})
    monkeypatch.setattr("server.memory.domain.lock.effective_mode", lambda h: "")
    monkeypatch.setattr(pg_publish, "commit", _commit)
    monkeypatch.setattr(pg_publish, "publish", _publish)
    monkeypatch.setattr(lifecycle, "retire_vectors_for_keys", _retire)
    with pytest.raises(ConnectionError):
        await lifecycle._tombstone_heads(
            "o", "n", None, None, datetime.now(UTC), _announce, ch=None
        )
    assert [r["memory_id"] for r in announced] == ["m1"]


async def test_a_cleared_page_whose_head_moved_is_read_again(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """memory.clear under Postgres: a page whose head changed before the commit is read
    again and tombstoned over the new head."""

    from server.memory.store import lifecycle, pg_publish

    def head(version: int) -> dict[str, Any]:
        return {
            "org": "o",
            "namespace": "n",
            "agent": "a",
            "user_id": "-",
            "session_id": "-",
            "memory_key": "k",
            "memory_id": "m1",
            "version": version,
        }

    reads: list[int] = []
    committed: list[Any] = []

    async def _page(*_: Any, after: Any = None, **__: Any) -> list[dict[str, Any]]:
        if after:
            return []
        reads.append(1)
        return [head(1 if len(reads) == 1 else 5)]

    async def _commit(rows: Any, **_: Any) -> Any:
        if len(reads) == 1:
            raise pg_memory.HeadMoved()
        committed.extend(rows)
        return SimpleNamespace(rows=rows)

    async def _publish(*_: Any, **__: Any) -> None:
        return None

    async def _retire(*_: Any, **__: Any) -> int:
        return 0

    monkeypatch.setattr(pg_memory, "live_heads_in", _page)
    monkeypatch.setattr(lifecycle, "_hydrate", lambda h: SimpleNamespace(**h))
    monkeypatch.setattr(lifecycle, "build_memory_version_row", lambda h, **kw: {**vars(h), **kw})
    monkeypatch.setattr("server.memory.store.rows.next_version", lambda: 2)
    monkeypatch.setattr("server.memory.domain.lock.effective_mode", lambda h: "")
    monkeypatch.setattr(pg_publish, "commit", _commit)
    monkeypatch.setattr(pg_publish, "publish", _publish)
    monkeypatch.setattr(lifecycle, "retire_vectors_for_keys", _retire)
    total, _, locked = await lifecycle._tombstone_heads(
        "o", "n", None, None, datetime.now(UTC), None, ch=None
    )
    assert (total, locked) == (1, 0)
    assert len(reads) == 2
    assert [r["version"] for r in committed] == [7]


async def test_a_list_serves_only_what_the_authority_still_serves(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """memory.list reads ClickHouse: a row deleted, rewritten or redacted in the authority
    but not yet published there is not read back; a row both stores hold redacted is."""
    from server.memory.store import lifecycle

    identity = {"org": "o", "namespace": "n", "agent": "a", "user_id": "-", "session_id": "-"}
    when = datetime.now(UTC)

    def row(key: str, version: int, redacted: Any = None) -> dict[str, Any]:
        return {**identity, "memory_key": key, "version": version, "redacted_at": redacted}

    read = [
        row("live", 1),
        row("deleted", 1),
        row("rewritten", 1),
        row("redacted", 1),
        row("scrubbed", 1, when),
    ]
    heads = {
        ("o", "n", "a", "-", "-", "live"): {"version": 1},
        ("o", "n", "a", "-", "-", "rewritten"): {"version": 2},
        ("o", "n", "a", "-", "-", "redacted"): {"version": 1, "redacted_at": when.isoformat()},
        ("o", "n", "a", "-", "-", "scrubbed"): {"version": 1, "redacted_at": str(when)},
    }

    async def _heads(keys: Any, **_: Any) -> dict[Any, Any]:
        return {k: heads[k] for k in keys if k in heads}

    monkeypatch.setattr(pg_memory, "heads_by_keys", _heads)
    monkeypatch.setattr(pg, "enabled", lambda: False)
    assert await lifecycle.served_rows(read) == read
    monkeypatch.setattr(pg, "enabled", lambda: True)
    served = await lifecycle.served_rows(read)
    assert [r["memory_key"] for r in served] == ["live", "scrubbed"]


async def test_a_recovery_listing_lists_what_the_authority_holds(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A row the authority has since only deleted is listed as it holds it, with
    ClickHouse's preview; one rewritten, relabelled (even before a delete), restored, or
    redacted only in the authority, waits until ClickHouse holds it."""
    from server.memory.store import lifecycle
    from server.memory.store.rows import MemoryRow

    now = datetime.now(UTC).replace(tzinfo=None, microsecond=0)

    def row(key: str, **kw: Any) -> MemoryRow:
        base = {
            "memory_id": f"id-{key}",
            "org": "o",
            "namespace": "n",
            "agent": "a",
            "user_id": "-",
            "session_id": "-",
            "memory_key": key,
            "memory_type": "semantic",
            "value": "a preview",
            "confidence": 1.0,
            "version": 1,
            "pinned": False,
            "created_at": now,
            "updated_at": now,
            "expires_at": now,
            "content_sha256": "s",
            "value_truncated": True,
        }
        return MemoryRow(**{**base, **kw})

    def head(key: str, **kw: Any) -> dict[str, Any]:
        return {**row(key, value="the whole text", value_truncated=False, **kw).model_dump()}

    read = [
        row("gone"),
        row("restored", tombstoned_at=now, status="tombstoned"),
        row("rewritten"),
        row("redacted", tombstoned_at=now, status="tombstoned"),
        row("same"),
        row("superseded"),
        row("retagged", tags=["a"]),
        row("traced", source_trace="decision"),
        row("hidden"),
    ]
    heads = {
        "gone": head("gone", version=2, tombstoned_at=now, status="tombstoned", expires_at=None),
        "restored": head("restored", version=3),
        "rewritten": head("rewritten", version=2, content_sha256="other"),
        "redacted": head("redacted", tombstoned_at=now, status="tombstoned", redacted_at=now),
        "same": head("same"),
        "superseded": head("superseded", version=2, status="superseded"),  # a relabel
        # retagged, then deleted, at the same text: not what a tag filter selected
        "retagged": head("retagged", version=3, tags=["b"], tombstoned_at=now, status="tombstoned"),
        # deleted by memory.delete, which writes its tombstone with an empty trace
        "traced": head("traced", version=2, tombstoned_at=now, source_trace=""),
        # superseded, then deleted by memory.delete, which keeps the status
        "hidden": head("hidden", version=3, tombstoned_at=now, status="superseded"),
    }
    asked: list[bool] = []

    async def _heads(keys: Any, *, include_tombstoned: bool) -> dict[Any, Any]:
        asked.append(include_tombstoned)
        return {k: heads[k[5]] for k in keys if k[5] in heads}

    monkeypatch.setattr(pg, "enabled", lambda: True)
    monkeypatch.setattr(pg_memory, "heads_by_keys", _heads)
    listed = {r.memory_key: r for r in await lifecycle.recovered_rows(read)}
    assert asked == [True]
    assert sorted(listed) == ["gone", "hidden", "same", "traced"]
    gone = listed["gone"]
    assert (gone.version, gone.status, gone.expires_at) == (2, "tombstoned", None)
    assert gone.tombstoned_at is not None and gone.value == "a preview" and gone.value_truncated
    assert gone.value_text == read[0].value_text  # ClickHouse's, not the authority's text
    assert listed["traced"].source_trace == ""  # the delete's own trace
    assert listed["same"] is read[4]
    # Filtered by the trace the row had, the delete (whose trace is the delete's own) is not
    # what the listing selected; filtered by the delete's own trace, it is.
    traced = [r for r in read if r.memory_key == "traced"]
    assert await lifecycle.recovered_rows(traced, source_trace="decision") == []
    assert len(await lifecycle.recovered_rows(traced, source_trace="")) == 1
    # A status the listing hides, or an as_of it cannot judge from its columns: not swapped.
    gone = [r for r in read if r.memory_key == "gone"]
    superseded = [r for r in read if r.memory_key == "hidden"]
    assert await lifecycle.recovered_rows(superseded) != []
    assert (
        await lifecycle.recovered_rows(superseded, hidden_statuses=frozenset({"superseded"})) == []
    )
    assert await lifecycle.recovered_rows(gone, as_of=now) == []


async def test_a_send_deadline_measures_the_server_clock_it_lacks(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A process whose first ClickHouse write is not a carried insert (a delivery worker
    owing only a record retirement) measures the server's clock before its deadline."""
    from server.core import rmt_carry

    class _CH:
        def __init__(self) -> None:
            self.reads = 0

        async def query_one(self, sql: str, params: Any = None) -> dict[str, Any]:
            self.reads += 1
            return {"ms": 1_700_000_000_000}

    monkeypatch.setattr(rmt_carry, "_server_anchor", None)
    monkeypatch.setattr(rmt_carry, "_skew_at", -math.inf)
    ch = _CH()
    deadline = await rmt_carry.measured_send_deadline(ch)
    assert deadline == pytest.approx(1_700_000_000 + 15, abs=1)
    await rmt_carry.measured_send_deadline(ch)
    assert ch.reads == 1  # measured lately: not again
    monkeypatch.setattr(rmt_carry, "_skew_at", time.monotonic() - 61)
    await rmt_carry.measured_send_deadline(ch)
    assert ch.reads == 2  # stale: measured again, as the watcher would


@pytest.mark.asyncio
@pytest.mark.parametrize("authority", ["postgres", "clickhouse"])
async def test_an_update_whose_retirement_fails(
    monkeypatch: pytest.MonkeyPatch, authority: str
) -> None:
    """Under Postgres the commit owes the retirement of the old vectors (`OLD_VECTORS`),
    which its publish runs: a failed one is publication left to the delivery worker.
    Without it the retirement is the write's own error, as on any insert."""
    import server.memory.store as memory_repo
    from server.memory.app import update
    from server.memory.store import pg_publish
    from server.settings import settings

    async def _retire(*a: object, **kw: object) -> None:
        raise RuntimeError("retire refused")

    async def _ok(*a: object, **kw: object) -> None:
        return None

    async def _commit(rows: Any, *, targets: int, **kw: object) -> Any:
        owed = targets | pg_memory.OLD_VECTORS  # the head it replaced had vectors
        return pg_memory.Committed(
            rows=rows, audits=[], seqs=[("o", "n", 1)], targets=[owed], deadline=1e12
        )

    monkeypatch.setattr(settings, "storage_authority", authority)
    monkeypatch.setattr(memory_repo, "tombstone_vectors_of", _retire)
    monkeypatch.setattr("server.memory.store.embeddings.retire_vectors_for_keys", _retire)
    monkeypatch.setattr(update, "insert_memory_versions", _ok)
    monkeypatch.setattr(pg_publish, "commit", _commit)
    monkeypatch.setattr(pg_publish, "publish_rows", _ok)
    row = {"version": 3, "memory_key": "k", "org": "o", "namespace": "n", "agent": "a"}
    row |= {"user_id": "-", "session_id": "-"}
    existing = SimpleNamespace(memory_type="semantic", sensitivity="")
    args = (existing, row, [], None, None)
    kw = {"searchable": False, "high": False, "record": _ok, "exact": False, "op": "memory.update"}
    if authority == "postgres":
        pending = await update._land_revision(*args, **kw)
        assert isinstance(pending, pg_lease.PublicationPending)
    else:
        with pytest.raises(RuntimeError, match="retire refused"):
            await update._land_revision(*args, **kw)


@pytest.mark.asyncio
async def test_the_worker_retires_the_old_vectors_a_revision_owes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A claim owing OLD_VECTORS retires the vectors below its revision under the claim's
    lease, and clears the target once they are."""
    from server.memory.app import pg_delivery
    from server.memory.store import pg_publish

    retired: list[Any] = []
    completed: list[tuple[Any, int]] = []

    async def _none(*_: object, **__: object) -> None:
        return None

    async def _retire(items: Any, *, ch: Any, bare: bool) -> int:
        from server.core import rmt_carry

        assert rmt_carry.sent_under_lease()  # its copies start within the claim's lease
        assert bare  # a redaction's fence may let it land after the purge
        retired.extend(items)
        return len(items)

    async def _complete(seqs: Any, done: int, *, tokens: Any = 1) -> None:
        completed.append((seqs, done))

    monkeypatch.setattr(pg_publish, "publish_rows", _none)
    monkeypatch.setattr(pg_publish, "retire_decision_records", _none)
    monkeypatch.setattr("server.memory.store.embeddings.retire_vectors_for_keys", _retire)
    monkeypatch.setattr(pg_memory, "complete", _complete)
    monkeypatch.setattr("server.core.ch.get_client", lambda: object())
    row = {"org": "o", "namespace": "n", "agent": "a", "user_id": "-", "session_id": "-"}
    claim = replace(
        _claim(4, pg_memory.OLD_VECTORS),
        row={**row, "memory_key": "k4", "version": 9},
        deadline=time.monotonic() + 30,
    )
    await pg_delivery._publish([claim])
    assert retired == [(("o", "n", "a", "-", "-", "k4"), 9)]
    assert ([("o", "n", 4)], pg_memory.OLD_VECTORS) in completed


@pytest.mark.asyncio
async def test_an_expiry_pass_leaves_a_moved_head_for_the_next_one(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """One head that moved since the pass read it refuses the batch: the heads are read
    again and the batch goes once more without it, which the next pass sees."""
    from server.memory.app import expiry

    batches: list[list[str]] = []

    async def _over_heads(rows: list[dict[str, Any]], read: Any, *, ch: Any) -> None:
        batches.append([r["memory_key"] for r in rows])
        if any(r["memory_key"] == "moved" for r in rows):
            raise pg_memory.AdmissionContendedError("the memory changed while it was read")

    async def _heads(rows: list[Any]) -> list[Any]:
        return [SimpleNamespace(version=r.version + (r.memory_key == "moved")) for r in rows]

    monkeypatch.setattr("server.memory.store.insert_versions_over_heads", _over_heads)
    monkeypatch.setattr("server.memory.store.authority_heads", _heads)
    res = expiry.ExpiryResult(tombstoned=2, demoted=1)
    rows = [{"memory_key": k} for k in ("a", "moved", "b")]
    reads = [
        MemoryRow.model_construct(
            version=1, org="o", namespace="n", agent="a", user_id="-", session_id="-", memory_key=k
        )
        for k in ("a", "moved", "b")
    ]
    mirrors = [("tombstone", reads[0], 1), ("demote", reads[1], 2), ("tombstone", reads[2], 3)]
    landed, kept = await expiry._over_unmoved_heads(rows, mirrors, res, ch=None)
    assert batches == [["a", "moved", "b"], ["a", "b"]]
    assert [r["memory_key"] for r in landed] == ["a", "b"] and [m[2] for m in kept] == [1, 3]
    assert (res.moved_skipped, res.tombstoned, res.demoted) == (1, 2, 0)


@pytest.mark.asyncio
async def test_an_expiry_batch_refused_again_is_left_whole_for_the_next_pass(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A head redacted in place keeps its version but is no longer the row read, so it is
    left out; a batch refused once more (a head moved after the re-read) is left whole,
    counted for the next pass and not as a failure."""
    from server.memory.app import expiry

    async def _always_moved(rows: list[dict[str, Any]], read: Any, *, ch: Any) -> None:
        raise pg_memory.AdmissionContendedError("the memory changed while it was read")

    def _row(key: str, *, redacted: bool = False) -> Any:
        return MemoryRow.model_construct(
            org="o",
            namespace="n",
            agent="a",
            user_id="-",
            session_id="-",
            memory_key=key,
            version=1,
            tombstoned_at=None,
            pinned=False,
            lock_mode="",
            lock_until=None,
            status="active",
            redacted_at="now" if redacted else None,
        )

    async def _heads(rows: list[Any]) -> list[Any]:
        return [_row(r.memory_key, redacted=r.memory_key == "redacted") for r in rows]

    monkeypatch.setattr("server.memory.store.insert_versions_over_heads", _always_moved)
    monkeypatch.setattr("server.memory.store.authority_heads", _heads)
    res = expiry.ExpiryResult(tombstoned=2)
    reads = [_row("redacted"), _row("b")]
    mirrors = [("tombstone", reads[0], 1), ("tombstone", reads[1], 2)]
    landed, kept = await expiry._over_unmoved_heads(
        [{"memory_key": "redacted"}, {"memory_key": "b"}], mirrors, res, ch=None
    )
    assert (landed, kept) == ([], [])
    assert (res.moved_skipped, res.tombstoned) == (2, 0)


def test_a_send_under_a_lease_starts_by_the_leases_end(monkeypatch: pytest.MonkeyPatch) -> None:
    """However long a send waits after its lease was checked, it starts within the transit
    allowance of the lease's end, which is what an erasure waits out."""
    from server.core import rmt_carry

    monkeypatch.setattr(rmt_carry, "_server_anchor", (1_700_000_000.0, time.monotonic()))
    with rmt_carry.under_lease(time.monotonic() + 60):
        assert rmt_carry.send_deadline() == pytest.approx(1_700_000_015, abs=1)
    with rmt_carry.under_lease(time.monotonic() - 10):
        assert rmt_carry.send_deadline() == pytest.approx(1_700_000_005, abs=1)
    with pytest.raises(pg_lease.PublicationPending), pg_lease.leased(time.monotonic() - 1):
        pass


def test_a_deleted_decision_owes_its_record_retirement() -> None:
    """A deleted decision's delivery owes the retirement of its record as a target of its
    own, apart from the memory row the search barrier waits on."""
    deleted = {"memory_type": "decision", "tombstoned_at": "2026-01-01T00:00:00"}
    assert pg_memory.deleted_decision(deleted)
    assert not pg_memory.deleted_decision({**deleted, "tombstoned_at": None})
    assert not pg_memory.deleted_decision({**deleted, "memory_type": "semantic"})
    assert pg_memory.DECISION_RECORD & pg_memory.SEARCHED == 0


async def test_a_deleted_decisions_record_retires_where_it_is_published(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The records of deleted decisions retire a namespace at a time, below a version
    drawn before the live writers are read and under the lease, each namespace's target
    cleared as it lands; one that fails stays owed while the others go on, and its error
    is returned for the worker to count."""
    from server.memory.store import pg_publish

    events: list[Any] = []
    failing: set[str] = set()

    def _version() -> int:
        events.append("drawn")
        return 42

    async def _live(org: str, ns: str, keys: list[str]) -> list[Any]:
        events.append("live")
        return [(keys[0], "a", "-", "-")]

    async def _records(
        org: str, ns: str, keys: list[str], *, version: int, live: Any, **_: Any
    ) -> None:
        if ns in failing:
            raise ConnectionError("clickhouse down")
        events.append(("retired", ns, keys, version, live))

    async def _complete(seqs: Any, landed: int, **kw: Any) -> None:
        events.append(("cleared", seqs, landed, kw["tokens"]))

    def owe(ns: str, key: str, seq: int) -> Any:
        return pg_publish.OwedRecord({"memory_key": key}, ("o", ns, seq), 9)

    monkeypatch.setattr(pg_publish, "next_version", _version)
    monkeypatch.setattr(pg_memory, "live_decision_writers", _live)
    monkeypatch.setattr(pg_memory, "complete", _complete)
    monkeypatch.setattr(pg_lease, "check_lease", lambda deadline: None)
    monkeypatch.setattr(pg_publish, "tombstone_decision_records", _records)
    owing = [owe("a", "k1", 1), owe("a", "k2", 2), owe("b", "k3", 3)]
    assert await pg_publish.retire_decision_records(owing, deadline=0.0, ch=object()) is None
    assert events == [
        "drawn",
        "live",
        ("retired", "a", ["k1", "k2"], 42, [("k1", "a", "-", "-")]),
        ("cleared", [("o", "a", 1), ("o", "a", 2)], pg_memory.DECISION_RECORD, [9, 9]),
        "drawn",
        "live",
        ("retired", "b", ["k3"], 42, [("k3", "a", "-", "-")]),
        ("cleared", [("o", "b", 3)], pg_memory.DECISION_RECORD, [9]),
    ]

    failing.add("a")
    events.clear()
    error = await pg_publish.retire_decision_records(owing, deadline=0.0, ch=object())
    assert isinstance(error, ConnectionError)
    cleared = [e for e in events if isinstance(e, tuple) and e[0] == "cleared"]
    assert cleared == [("cleared", [("o", "b", 3)], pg_memory.DECISION_RECORD, [9])]


async def test_a_cascade_draws_its_version_once_every_row_is_read(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The tombstones' version is drawn after the reads, so a write that landed before a
    read sorts below it."""
    from server.memory.store import lifecycle

    events: list[str] = []

    async def _row(mid: str, **_: Any) -> Any:
        events.append(f"read {mid}")
        return _head(mid, "x")

    def _version() -> int:
        events.append("drawn")
        return 2

    async def _noop(*_: Any, **__: Any) -> None:
        return None

    monkeypatch.setattr(lifecycle, "get_by_id", _row)
    monkeypatch.setattr(lifecycle, "next_version", _version)
    monkeypatch.setattr(lifecycle, "build_memory_version_row", lambda e, **kw: dict(kw))
    monkeypatch.setattr(lifecycle, "memory_change", lambda e, v: {"version": v})
    monkeypatch.setattr(lifecycle, "insert_versions_over_heads", _noop)
    monkeypatch.setattr(lifecycle, "mirror_status_to_embeddings", _noop)
    done = await lifecycle.tombstone_cascade_by_ids(
        "o", ["m1", "m2"], when=datetime.now(UTC), namespace="n", ch=object()
    )
    assert events == ["read m1", "read m2", "drawn"]
    assert [c["version"] for c in done] == [3, 3]


async def test_a_bulk_read_answers_from_the_authority(monkeypatch: pytest.MonkeyPatch) -> None:
    """memory.bulk_get under Postgres reads the authority's heads, a newer one not yet in
    ClickHouse included, leaving out a 'high' head past its retention window as
    ClickHouse's read does; a strong read, which feeds a write, keeps it."""
    from server.identity.domain.read_scope import WHOLE_NAMESPACE
    from server.memory.store import reads
    from server.memory.store.rows import Identity

    asked: list[bool] = []

    async def _heads(identity: Any, keys: Any, **kw: Any) -> dict[str, Any]:
        asked.append(kw["retained_only"])
        return {}

    monkeypatch.setattr(pg, "enabled", lambda: True)
    monkeypatch.setattr(pg_memory, "heads_for_identity", _heads)
    ident = Identity(org="o", namespace="n", agent="a", user_id="-", session_id="-")
    await reads.get_many_by_keys(ident, ["k"], scope=WHOLE_NAMESPACE, ch=object())
    await reads.get_many_by_keys(ident, ["k"], scope=WHOLE_NAMESPACE, ch=object(), strong=True)
    assert asked == [True, False]


async def test_a_tombstone_goes_above_a_head_from_a_clock_ahead(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A head carrying a version above the one the cascade drew (written on a replica
    whose clock is ahead) is tombstoned just above it, announced, counted and its record
    retired."""
    from server.memory.app import blacklist_sweep

    future = _head("m2", "decided 5555", "decision")
    future.version = 2**62
    heads = {"m1": _head("m1", "card 1234"), "m2": future}
    ch, written, retired = _sweep_heads(monkeypatch, heads, matched=["m1", "m2"])
    monkeypatch.setattr(pg, "enabled", lambda: False)
    result = await blacklist_sweep.sweep_pattern(
        org="o",
        namespace="n",
        pattern_id="p",
        pattern="5",
        pattern_type="substring",
        scope="namespace",
        ch=ch,
    )
    assert written == ["m1", "m2"]
    assert (result.matched, result.tombstoned) == (2, 2)
    assert len(retired) == 1


def _sweep_heads(
    monkeypatch: pytest.MonkeyPatch, heads: dict[str, Any], *, matched: list[str]
) -> tuple[Any, list[str], list[str]]:
    """The real cascade over `heads` (by memory_id), and a ClickHouse matching the ids
    `matched`; with what was tombstoned and which decision keys were retired."""
    from server.memory.app import blacklist_sweep
    from server.memory.store import lifecycle

    written: list[str] = []
    retired: list[str] = []

    async def _row(mid: str, **_: Any) -> Any:
        return heads.get(mid)

    async def _insert(rows: Any, *_: Any, **__: Any) -> None:
        written.extend(r["memory_id"] for r in rows)

    async def _noop(*_: Any, **__: Any) -> Any:
        return None

    async def _records(org: str, ns: str, keys: list[str], **_: Any) -> None:
        retired.extend(keys)

    monkeypatch.setattr(lifecycle, "get_by_id", _row)
    monkeypatch.setattr(
        lifecycle, "build_memory_version_row", lambda e, **kw: {"memory_id": e.memory_id, **kw}
    )
    monkeypatch.setattr(
        lifecycle,
        "memory_change",
        lambda e, v: {"namespace": e.namespace, "memory_id": e.memory_id},
    )
    monkeypatch.setattr(lifecycle, "insert_versions_over_heads", _insert)
    monkeypatch.setattr(lifecycle, "mirror_status_to_embeddings", _noop)
    monkeypatch.setattr(lifecycle, "tombstone_decision_records", _records)

    async def _caught_up(*_: Any, **__: Any) -> bool:
        return False

    monkeypatch.setattr(blacklist_sweep.webhooks, "announce", _noop)
    monkeypatch.setattr("server.memory.app.drops.record_drop", lambda *a, **k: None)
    monkeypatch.setattr("server.memory.app.search.pg_validation.admit", _caught_up)

    class _CH:
        async def query_all(self, sql: str, params: Any = None) -> list[dict[str, Any]]:
            return [{"memory_id": m, "namespace": "n"} for m in matched]

    return _CH(), written, retired


def _head(mid: str, value: str, memory_type: str = "semantic") -> Any:
    return SimpleNamespace(
        memory_id=mid,
        namespace="n",
        version=1,  # below every version a tombstone draws
        memory_key=f"k-{mid}",
        memory_type=memory_type,
        value_text=json.dumps(value),
    )


async def test_a_sweep_under_postgres_judges_every_head_the_authority_holds(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Each head ClickHouse matched, or whose row it keeps refusing (read a page at a
    time), is judged as the authority holds it, with ClickHouse's RE2 match: one
    rewritten or redacted since is left, and `matched` counts what ClickHouse matched and
    the parked heads that match."""
    from server.memory.app import blacklist_sweep

    heads = {
        "m1": _head("m1", "card 1234"),
        "m2": _head("m2", "rewritten, no number"),
        "m3": _head("m3", "decided, no number", "decision"),
        "m4": _head("m4", "parked card 9876"),
        "m5": _head("m5", "decided 5555", "decision"),
        "m6": _head("m6", "parked, no number"),
        "m7": _head("m7", "parked card 1111"),
        "m8": _head("m8", "parked, none"),
        "m9": _head("m9", "parked card 2222"),
    }
    ch, written, retired = _sweep_heads(monkeypatch, heads, matched=["m1", "m2", "m3", "m5"])

    def park(seq: int, mid: str) -> Any:
        return pg_memory.ParkedHead("n", seq, mid, heads[mid].value_text)

    parked = [park(7, "m4"), park(8, "m6"), park(9, "m7"), park(10, "m8")]
    pages = {None: parked, ("n", 10): [park(11, "m9")]}
    afters: list[Any] = []

    async def _parked(org: str, ns: str, *, after: Any, limit: int) -> list[Any]:
        afters.append(after)
        return pages[after][:limit]

    sequential: list[bool] = []

    @contextlib.contextmanager
    def _sequential() -> Any:
        sequential.append(True)
        yield

    monkeypatch.setattr(pg, "enabled", lambda: True)
    monkeypatch.setattr(pg_memory, "parked_heads", _parked)
    monkeypatch.setattr(blacklist_sweep, "sequential_reads", _sequential)
    result = await blacklist_sweep.sweep_pattern(
        org="o",
        namespace="n",
        pattern_id="p",
        pattern="[[:digit:]]{4}",  # POSIX classes: RE2's, not Python's
        pattern_type="regex",
        scope="namespace",
        limit=4,  # ClickHouse's four matches; the parked heads come in pages of four too
        ch=ch,
    )
    assert sequential == [True]  # nothing was behind, and the match still reads sequentially
    assert afters == [None, ("n", 10)]  # a full page reads the next, a short one ends it
    assert sorted(written) == ["m1", "m4", "m5", "m7", "m9"]
    assert retired == []  # under Postgres the tombstones' publication retires the records
    assert (result.matched, result.tombstoned) == (7, 5)


async def test_a_clickhouse_sweep_tombstones_what_clickhouse_matched(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Without the Postgres authority ClickHouse's match decides, as it always has."""
    from server.memory.app import blacklist_sweep

    heads = {"m1": _head("m1", "rewritten, no number")}
    ch, written, _ = _sweep_heads(monkeypatch, heads, matched=["m1"])
    monkeypatch.setattr(pg, "enabled", lambda: False)
    result = await blacklist_sweep.sweep_pattern(
        org="o",
        namespace="n",
        pattern_id="p",
        pattern="[[:digit:]]{4}",
        pattern_type="regex",
        scope="namespace",
        ch=ch,
    )
    assert written == ["m1"]
    assert result.tombstoned == 1


@pytest.mark.parametrize(("scope", "waited"), [("namespace", ["n"]), ("org", None)])
async def test_a_sweep_waits_for_the_memory_rows_of_its_scope(
    monkeypatch: pytest.MonkeyPatch, scope: str, waited: list[str] | None
) -> None:
    """A namespace pattern waits for its namespace's memory rows, an org pattern for every
    namespace of the org, and reads the parked heads of that scope."""
    from server.memory.app import blacklist_sweep
    from server.memory.app.search import pg_validation

    admitted: list[Any] = []
    parked: list[Any] = []

    async def _admit(org: str, namespaces: list[str] | None, *, owed: int) -> bool:
        admitted.append((namespaces, owed))
        return False

    async def _parked(org: str, ns: str | None, *, after: Any, limit: int) -> list[Any]:
        parked.append(ns)
        return []

    class _CH:
        async def query_all(self, *_: Any, **__: Any) -> list[dict[str, Any]]:
            return []

    monkeypatch.setattr(pg, "enabled", lambda: True)
    monkeypatch.setattr(pg_validation, "admit", _admit)
    monkeypatch.setattr(pg_memory, "parked_heads", _parked)
    await blacklist_sweep.sweep_pattern(
        org="o",
        namespace="n",
        pattern_id="p",
        pattern="x",
        pattern_type="substring",
        scope=scope,
        ch=_CH(),
    )
    assert admitted == [(waited, pg_memory.MEMORY_ROW)]
    assert parked == [None if waited is None else "n"]


async def test_a_sweep_that_stops_answers_what_it_tombstoned(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """A failure after a row landed answers the rows that landed, with its code; one
    before any landed is the call's error, and blacklist.add then keeps its pattern."""
    from server.memory.app import blacklist, blacklist_sweep

    real_sweep = blacklist_sweep.sweep_pattern
    heads = {"m1": _head("m1", "card 1234")}
    ch, written, _ = _sweep_heads(monkeypatch, heads, matched=["m1"])

    async def _down(*_: Any, **__: Any) -> list[Any]:
        raise pg.PostgresUnavailableError("down")

    monkeypatch.setattr(pg, "enabled", lambda: True)
    monkeypatch.setattr(pg_memory, "parked_heads", _down)
    sweep = {
        "org": "o",
        "namespace": "n",
        "pattern_id": "p",
        "pattern": "1234",
        "pattern_type": "substring",
        "scope": "namespace",
        "ch": ch,
    }
    result = await blacklist_sweep.sweep_pattern(**sweep)
    assert written == ["m1"]
    assert (result.tombstoned, result.error) == (1, "POSTGRES_UNAVAILABLE: down")

    ch, written, _ = _sweep_heads(monkeypatch, {}, matched=[])
    with pytest.raises(pg.PostgresUnavailableError):
        await blacklist_sweep.sweep_pattern(**{**sweep, "ch": ch})

    # A cascade whose commit answer was lost may have landed: the sweep answers with the
    # error and claims nothing it cannot see.
    import server.memory.store as memory_repo

    announced: list[Any] = []

    async def _lost(*_: Any, **__: Any) -> list[Any]:
        raise pg.PostgresUnavailableError("lost", outcome_unknown=True)

    async def _announce(events: Any) -> None:
        announced.extend(events)

    ch, _, _ = _sweep_heads(monkeypatch, {}, matched=["m1"])
    monkeypatch.setattr(memory_repo, "tombstone_cascade_by_ids", _lost)
    monkeypatch.setattr(blacklist_sweep.webhooks, "announce", _announce)
    result = await blacklist_sweep.sweep_pattern(**{**sweep, "ch": ch})
    assert (result.tombstoned, result.error) == (0, "POSTGRES_UNAVAILABLE: lost")
    assert announced == []

    async def _noop(*_: Any, **__: Any) -> None:
        return None

    async def _sweep(**_: Any) -> Any:
        raise pg_memory.AdmissionContendedError("moved")

    monkeypatch.setattr(blacklist, "_require_scope_authority", lambda *a, **k: None)
    monkeypatch.setattr(blacklist.blacklist_repo, "insert_row", _noop)
    monkeypatch.setattr(blacklist, "_bump_version", _noop)
    monkeypatch.setattr(blacklist, "invalidate_cache", lambda: None)
    monkeypatch.setattr(blacklist_sweep, "sweep_pattern", _sweep)
    out = await blacklist.handle_blacklist_add(
        blacklist.BlacklistAddInput(org="o", namespace="n", pattern="x", reason="r", sweep=True)
    )
    assert out.pattern_id and out.sweep_error == "ADMISSION_CONTENDED: moved"
    # An error with no Loom code is named, its detail left to the server log.
    assert blacklist_sweep.sweep_error_text(RuntimeError("host:9000 refused")) == (
        "SWEEP_FAILED: the sweep did not finish; the server log has the cause"
    )

    # The rows a sweep writes match the pattern: an error quoting one logs its summary.
    async def _quotes_the_row(**_: Any) -> Any:
        raise RuntimeError("Failing row contains (card 1234)")

    monkeypatch.setattr(blacklist_sweep, "sweep_pattern", _quotes_the_row)
    await blacklist.handle_blacklist_add(
        blacklist.BlacklistAddInput(org="o", namespace="n", pattern="x", reason="r", sweep=True)
    )
    assert "sweep not finished" in caplog.text and "card 1234" not in caplog.text

    # Nor does the sweep's own log, after a row landed or when its match query fails.
    from server.memory.store import lifecycle

    monkeypatch.setattr(blacklist_sweep, "sweep_pattern", real_sweep)
    monkeypatch.setattr(memory_repo, "tombstone_cascade_by_ids", lifecycle.tombstone_cascade_by_ids)
    ch, written, _ = _sweep_heads(monkeypatch, {"m1": _head("m1", "card 1234")}, matched=["m1"])

    async def _quotes(*_: Any, **__: Any) -> list[Any]:
        raise RuntimeError("Failing row contains (card 1234)")

    monkeypatch.setattr(pg, "enabled", lambda: True)
    monkeypatch.setattr(pg_memory, "parked_heads", _quotes)
    caplog.clear()
    out = await blacklist_sweep.sweep_pattern(**{**sweep, "ch": ch})
    assert out.tombstoned == 1 and written == ["m1"]
    assert "stopped after" in caplog.text and "card 1234" not in caplog.text

    class _Refuses:
        async def query_all(self, *_: Any, **__: Any) -> list[Any]:
            raise RuntimeError("Code: 427. cannot compile regexp (?=card 1234)")

    caplog.clear()
    out = await real_sweep(**{**sweep, "ch": _Refuses()})
    assert out.error and "match query failed" in caplog.text and "card 1234" not in caplog.text


async def test_a_sweep_behind_its_projection_is_the_callers_to_retry(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """blacklist.sweep answers 503 PROJECTION_BEHIND; blacklist.add, whose pattern is
    stored, answers its id with the error."""
    from server.memory.app import blacklist, blacklist_sweep

    async def _behind(*_: Any, **__: Any) -> Any:
        raise pg_admission.ProjectionBehindError("behind")

    monkeypatch.setattr("server.memory.app.search.pg_validation.admit", _behind)
    with pytest.raises(pg_admission.ProjectionBehindError):
        await blacklist_sweep.sweep_pattern(
            org="o",
            namespace="n",
            pattern_id="p",
            pattern="secret",
            pattern_type="substring",
            scope="namespace",
            ch=object(),
        )

    async def _noop(*_: Any, **__: Any) -> None:
        return None

    monkeypatch.setattr(blacklist, "_require_scope_authority", lambda *a, **k: None)
    monkeypatch.setattr(blacklist.blacklist_repo, "insert_row", _noop)
    monkeypatch.setattr(blacklist, "_bump_version", _noop)
    monkeypatch.setattr(blacklist, "invalidate_cache", lambda: None)
    out = await blacklist.handle_blacklist_add(
        blacklist.BlacklistAddInput(
            org="o", namespace="n", pattern="secret", reason="r", sweep=True
        )
    )
    assert out.pattern_id and out.sweep_error == "PROJECTION_BEHIND: behind"


def test_a_fallback_that_is_no_providers_stays_out_of_health() -> None:
    """A tier left out is on the response, and /v1/health is not marked degraded."""
    from server.observability import degradations, provider_health

    provider_health.reset()
    degradations.begin()
    degradations.note("memory.search", "anchor_first_unchecked", "down", health=False)
    assert degradations.current() == ["memory.search: anchor_first_unchecked (down)"]
    assert "memory.search" not in provider_health.status()["providers"]


async def test_a_tier_whose_check_fails_is_left_out(monkeypatch: pytest.MonkeyPatch) -> None:
    """A tier never fails a search, and never serves rows it could not check."""
    import server.memory.store as memory_repo
    from server.memory.app.search import pg_validation, tiers

    checked: list[int] = []

    async def _broken(org: str, ns: str, rows: list[Any]) -> list[bool]:
        checked.append(len(rows))
        raise RuntimeError("postgres down")

    async def _rows(*_: Any, **__: Any) -> list[Any]:
        return [SimpleNamespace(memory_key="k", memory_type="semantic", value='"v"')]

    from server.observability import degradations

    monkeypatch.setattr(pg_validation, "stale_flags", _broken)
    monkeypatch.setattr(memory_repo, "list_by_namespace", _rows)
    degradations.begin()
    hits = [SimpleNamespace(memory_key="h")]
    got, stale = await tiers._prepend_tier(
        hits, SimpleNamespace(anchor_first=1, org="o", namespace="n"), "anchor_first", {}
    )
    assert checked and got == hits and stale == 0
    assert degradations.current() == [
        "memory.search: anchor_first_unchecked (RuntimeError)"  # a summary, not the text
    ]


@pytest.mark.asyncio
async def test_a_search_document_is_built_only_while_the_head_serves_it(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A newer head that copies the revision (a pin, a status) serves the document, built
    from that head's row; one deleted, redacted, made 'high' or of other content replaced
    it, and so does a copy whose content has no hash to match. A redacted head serves no
    document, its own revision's either."""
    from server.memory.app import pg_delivery

    ident = {"org": "o", "namespace": "n", "agent": "a", "user_id": "-", "session_id": "-"}
    revision = {**ident, "version": 1, "memory_type": "semantic", "content_sha256": "a" * 64}
    newer = {**revision, "version": 2, "status": "superseded"}
    heads = {
        "same": {**revision, "memory_key": "same"},
        "scrubbed": {**revision, "memory_key": "scrubbed", "redacted_at": "2026-01-01 00:00:00"},
        "copy": {**newer, "memory_key": "copy"},
        "deleted": {**newer, "memory_key": "deleted", "tombstoned_at": "2026-01-01 00:00:00"},
        "redacted": {**newer, "memory_key": "redacted", "redacted_at": "2026-01-01 00:00:00"},
        "high": {**newer, "memory_key": "high", "sensitivity": "high"},
        "other": {**newer, "memory_key": "other", "content_sha256": "b" * 64},
        "unhashed": {**newer, "memory_key": "unhashed", "content_sha256": ""},
    }

    async def _heads(keys: list[Any], *, include_tombstoned: bool) -> dict[Any, Any]:
        assert include_tombstoned
        return {identity_key(h): h for h in heads.values()}

    monkeypatch.setattr(pg_memory, "heads_by_keys", _heads)
    claims = [
        replace(_claim(i, pg_memory.SEARCH_DOC), row={**revision, "memory_key": k})
        for i, k in enumerate([*heads, "gone"])
    ]
    claims[-2] = replace(claims[-2], row={**claims[-2].row, "content_sha256": ""})
    served = await pg_delivery._still_heads(claims)
    assert [(c.row["memory_key"], c.row["version"]) for c in served] == [("same", 1), ("copy", 2)]
    assert served[1].row["status"] == "superseded"  # the head's own row, not the revision's


def test_a_commit_owes_the_retirement_only_over_a_live_embedded_head() -> None:
    embedded = {"memory_type": "semantic", "sensitivity": ""}
    high = {"memory_type": "semantic", "sensitivity": "high"}
    assert pg_memory._replaces_vectors(high, embedded)
    assert not pg_memory._replaces_vectors(high, None)
    assert not pg_memory._replaces_vectors(high, {**embedded, "tombstoned_at": "2026-01-01"})
    assert not pg_memory._replaces_vectors(high, high)
    assert not pg_memory._replaces_vectors(embedded, embedded)


@pytest.mark.asyncio
async def test_a_report_waits_once_for_the_projection_and_reads_sequentially(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A report's reads wait for ClickHouse to publish what the namespace committed, read
    sequentially when it was behind, and a report run inside another's reads (tune's
    audit) waits no second time."""
    from server.core.ch import sequential_reads_active
    from server.memory.app.search import pg_validation

    calls: list[tuple[str, list[str]]] = []

    async def admit(org: str, namespaces: list[str]) -> bool:
        calls.append((org, list(namespaces)))
        return True

    monkeypatch.setattr(pg_validation, "admit", admit)
    async with pg_validation.published_reads("o", ["n"]):
        assert sequential_reads_active()
        async with pg_validation.published_reads("o", ["n"]):
            assert sequential_reads_active()
    assert calls == [("o", ["n"])]
    assert not sequential_reads_active()


@pytest.mark.asyncio
async def test_a_report_whose_caller_found_postgres_down_does_not_wait_again(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`down`: the caller already found Postgres down and said so; the reads run
    sequentially with no wait and nothing more is reported, nested reads included."""
    from server.core.ch import sequential_reads_active
    from server.memory.app.search import pg_validation

    async def admit(*_: object) -> bool:
        raise AssertionError("no wait against a Postgres found down")

    monkeypatch.setattr(pg_validation, "admit", admit)
    reasons: list[str] = []
    async with pg_validation.published_reads("o", ["n"], reasons.append, down=True):
        assert sequential_reads_active()
        async with pg_validation.published_reads("o", ["n"], reasons.append):
            assert sequential_reads_active()
    assert reasons == []


@pytest.mark.asyncio
@pytest.mark.parametrize("unpublished", ["behind", "down"])
async def test_a_report_without_a_skip_fails_on_an_unpublished_projection(
    monkeypatch: pytest.MonkeyPatch, unpublished: str
) -> None:
    """Past the wait, or with Postgres down, a caller with nowhere to say so (the
    inventory) gets the 503; one with a `skip` reads on and is told why."""
    from server.core import pg
    from server.core.ch import sequential_reads_active
    from server.memory.app.pg_admission import ProjectionBehindError
    from server.memory.app.search import pg_validation

    error = ProjectionBehindError() if unpublished == "behind" else pg.PostgresUnavailableError()

    async def admit(*_: object) -> bool:
        raise error

    monkeypatch.setattr(pg_validation, "admit", admit)
    with pytest.raises(type(error)):
        async with pg_validation.published_reads("o", ["n"]):
            pass
    reasons: list[str] = []
    async with pg_validation.published_reads("o", ["n"], reasons.append):
        assert sequential_reads_active()
    assert [r.split(":")[0] for r in reasons] == [error.code]


@pytest.mark.asyncio
async def test_a_findings_citation_is_the_head_the_authority_serves(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Under Postgres a report cites a ClickHouse row only while it is its key's live head
    at the version read, in the same redaction state, and hashes the value memory.get serves
    there, whose object keeps a null field the JSON column drops. A row the authority has
    rewritten, deleted or redacted since is not cited."""
    from server.core import pg
    from server.core.hashing import served_content_sha256
    from server.identity.domain.read_scope import WHOLE_NAMESPACE
    from server.memory.store import reads

    def _row(key: str, version: int, *, redacted: bool = False) -> dict[str, Any]:
        return {
            "memory_key": key,
            "id": str(uuid.uuid5(uuid.NAMESPACE_OID, key)),
            "version": version,
            "sensitivity": "",
            "value_len": 10,
            "redacted": redacted,
            "agent": "a",
            "user_id": "-",
            "session_id": "-",
            "memory_type": "semantic",
            "source_class": "tool",
            "updated_at": datetime(2026, 1, 1),
        }

    class _CH:
        async def query_all(self, sql: str, **_: object) -> list[dict[str, Any]]:
            return [_row("k", 3), _row("moved", 4), _row("gone", 1), _row("scrubbed", 2)]

    async def _heads(keys: list[Any], **_: object) -> dict[Any, Any]:
        assert [k[-1] for k in keys] == ["k", "moved", "gone", "scrubbed"]
        return {
            keys[0]: {"version": 3, "value": '{"a": null, "b": 1}', "redacted_at": None},
            keys[1]: {"version": 9, "value": "the newer value", "redacted_at": None},
            keys[3]: {"version": 2, "value": "[redacted]", "redacted_at": datetime(2026, 1, 2)},
        }

    monkeypatch.setattr(pg, "enabled", lambda: True)
    monkeypatch.setattr(pg_memory, "heads_by_keys", _heads)
    cited = await reads.live_revisions(
        "o",
        "n",
        scope=WHOLE_NAMESPACE,
        memory_ids=[],
        identities=[("a", "-", "-", k) for k in ("k", "moved", "gone", "scrubbed")],
        hash_max_bytes=100,
        ch=_CH(),  # type: ignore[arg-type]
    )
    assert [(r["memory_key"], r["version"]) for r in cited] == [("k", 3)]
    assert cited[0]["content_sha256"] == served_content_sha256({"a": None, "b": 1})
    assert "redacted" not in cited[0]


@pytest.mark.asyncio
@pytest.mark.parametrize("admission", [None, pg_memory.Admission()])
async def test_a_commit_owes_the_retirement_from_the_head_it_locks(
    monkeypatch: pytest.MonkeyPatch, admission: Any
) -> None:
    """Whatever the write read, and with or without an admission: a row made 'high' over
    the embedded head the commit locks owes its vectors' retirement."""
    from contextlib import asynccontextmanager

    from server.core import pg

    row = {"org": "o", "namespace": "n", "agent": "a", "user_id": "-", "session_id": "-"}
    row |= {"memory_key": "k", "memory_id": str(uuid.uuid4()), "version": 2}
    head = {**row, "version": 1, "memory_type": "semantic", "sensitivity": ""}

    class _Conn:
        async def execute(self, sql: str, *args: object) -> None:
            return None

        async def fetch(self, sql: str, *args: Any) -> list[dict[str, Any]]:
            if "loom.memory_current c" in sql:
                return [{**row, "row": pg_memory.encode_row(head)}]
            return [{"org": "o", "scope": "n", "generation": 5}]

        async def fetchval(self, sql: str, *args: Any) -> bool:
            return False

    @asynccontextmanager
    async def _transaction():
        yield _Conn()

    monkeypatch.setattr(pg, "transaction", _transaction)
    high = {**row, "memory_type": "semantic", "sensitivity": "high"}
    committed = await pg_memory.commit_versions([high], [{}], admission=admission)
    assert committed.targets[0] & pg_memory.OLD_VECTORS
    committed = await pg_memory.commit_versions([{**high, "sensitivity": ""}], [{}])
    assert not committed.targets[0] & pg_memory.OLD_VECTORS


@pytest.mark.asyncio
async def test_a_publish_retires_the_old_vectors_and_records_it_owes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Once the rows are in, the old vectors are retired bare and their target cleared
    under the commit's token, then the deleted decisions' records are retired; a failed
    vector retirement answers PublicationPending and leaves the rest owed."""
    from server.memory.store import pg_publish

    calls: list[str] = []
    completed: list[Any] = []
    fail = False

    async def _rows(*_: object, **__: object) -> None:
        return None

    async def _records(owing: Any, **_: object) -> None:
        calls.extend(f"record {o.row['memory_key']}" for o in owing)

    async def _retire(items: Any, *, ch: Any, bare: bool) -> int:
        calls.append(f"vectors bare={bare}")
        if fail:
            raise RuntimeError("ClickHouse refused")
        return len(items)

    async def _complete(seqs: Any, done: int, *, tokens: Any = 1) -> None:
        completed.append((seqs, done, tokens))

    monkeypatch.setattr(pg_publish, "publish_rows", _rows)
    monkeypatch.setattr(pg_publish, "retire_decision_records", _records)
    monkeypatch.setattr("server.memory.store.embeddings.retire_vectors_for_keys", _retire)
    monkeypatch.setattr(pg_memory, "complete", _complete)
    ident = {"org": "o", "namespace": "n", "agent": "a", "user_id": "-", "session_id": "-"}
    decision = {**ident, "memory_key": "d", "version": 3, "memory_type": "decision"}
    decision["tombstoned_at"] = "2026-01-01 00:00:00"
    high = {**ident, "memory_key": "k", "version": 4}
    committed = pg_memory.Committed(
        rows=[decision, high],
        audits=[{}, {}],
        seqs=[("o", "n", 1), ("o", "n", 2)],
        targets=[pg_memory.MEMORY_ROW, pg_memory.MEMORY_ROW | pg_memory.OLD_VECTORS],
        deadline=time.monotonic() + 30,
    )
    await pg_publish.publish(committed, ch=object())
    assert calls == ["vectors bare=True", "record d"]
    assert completed == [([("o", "n", 2)], pg_memory.OLD_VECTORS, pg_memory.COMMIT_TOKEN)]
    calls.clear()
    completed.clear()
    fail = True
    with pytest.raises(pg_lease.PublicationPending):
        await pg_publish.publish(committed, ch=object())
    assert calls == ["vectors bare=True"] and completed == []


async def test_an_admission_in_a_namespace_postgres_cannot_store_is_refused() -> None:
    """Refused before Postgres sees it, as a row holding the character is."""
    from server.core.errors import ValidationError

    with pytest.raises(ValidationError):
        await pg_memory.capture("o", "n\x00s")


def test_a_row_left_for_the_next_expiry_pass_keeps_another_users_sample() -> None:
    from server.memory.app import expiry

    def row(user_id: str) -> MemoryRow:
        return MemoryRow.model_construct(
            org="o", namespace="n", agent="a", user_id=user_id, session_id="-", memory_key="k"
        )

    res = expiry.ExpiryResult(tombstoned=2)
    for user_id in ("u1", "u2"):
        res.sampled.append((identity_key(row(user_id)), {"memory_key": "k", "user": user_id}))
    expiry._leave(res, [("tombstone", row("u1"), 1)])
    assert res.sample == [{"memory_key": "k", "user": "u2"}]
    assert (res.tombstoned, res.moved_skipped) == (1, 1)


async def test_a_postgres_that_cannot_answer_the_deployment_check_is_unavailable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A dropped connection during the check, on the dedicated connection an erasure step
    opens, is the 503 outage it is, not a wrong LOOM_PG_DSN."""
    import asyncpg

    class _Conn:
        async def fetchrow(self, sql: str) -> object:
            raise ConnectionResetError("connection lost")

        async def close(self, **_: Any) -> None:
            return None

        def terminate(self) -> None:
            return None

    async def _connect(*_: object, **__: object) -> _Conn:
        return _Conn()

    monkeypatch.setattr(asyncpg, "connect", _connect)
    monkeypatch.setattr(pg.settings, "pg_dsn", "postgres://u@h/d")
    pg.expect_deployment(("d", 1))
    try:
        with pytest.raises(pg.PostgresUnavailableError):
            async with pg.dedicated():
                pass
    finally:
        pg.expect_deployment(None)


async def test_a_deadlock_in_an_erasure_block_reaches_its_retry(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A dedicated connection maps only its own opening to 503: the deadlock the real
    `_erase_once` meets on its connection reaches `_erase` as itself, which runs again."""
    import asyncpg

    from server.erasure.store import steps_pg

    class _Again(Exception):
        """The second attempt's first statement: it was retried."""

    attempts: list[int] = []

    class _Tx:
        async def __aenter__(self) -> None:
            return None

        async def __aexit__(self, *_: object) -> None:
            return None

    class _Conn:
        async def set_type_codec(self, *_: object, **__: object) -> None:
            return None

        def transaction(self) -> _Tx:
            return _Tx()

        async def execute(self, *_: object) -> str:
            attempts.append(1)
            if len(attempts) == 1:
                raise asyncpg.exceptions.DeadlockDetectedError("deadlock detected")
            raise _Again

        async def close(self, **_: Any) -> None:
            return None

        def terminate(self) -> None:
            return None

    async def _connect(*_: object, **__: object) -> _Conn:
        return _Conn()

    monkeypatch.setattr(asyncpg, "connect", _connect)
    monkeypatch.setattr(pg.settings, "pg_dsn", "postgres://u@h/d")
    with pytest.raises(_Again):
        await steps_pg._erase("o", ["n"], None)
    assert len(attempts) == 2


async def test_verify_deployment_runs_the_armed_check_and_nothing_else(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    opened: list[bool] = []

    async def _pool() -> object:
        opened.append(True)
        raise pg.AuthorityMismatch("LOOM_PG_DSN names a database that records no authority")

    monkeypatch.setattr(pg, "_pool_for_loop", _pool)
    await pg.verify_deployment()  # nothing armed: no connection
    assert opened == []
    pg.expect_deployment(("d", 1))
    try:
        with pytest.raises(pg.AuthorityMismatch):
            await pg.verify_deployment()
    finally:
        pg.expect_deployment(None)
    assert opened == [True]


async def test_a_cli_command_configured_for_postgres_with_no_dsn_is_refused(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The one-line refusal, before any statement, not a later RuntimeError."""
    from server.core import pg_profile
    from server.settings import settings

    monkeypatch.setattr(settings, "storage_authority", "postgres")
    monkeypatch.setattr(settings, "pg_dsn", "")
    with pytest.raises(pg.AuthorityMismatch, match="LOOM_PG_DSN"):
        await pg_profile.check_configured(
            _AuthorityCH({"authority": "postgres", "epoch": 1, "deployment_id": "d"})
        )


async def test_an_outage_during_the_deployment_check_is_unavailable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def _down() -> object:
        raise ConnectionRefusedError("postgres is down")

    monkeypatch.setattr(pg, "_pool_for_loop", _down)
    pg.expect_deployment(("d", 1))
    try:
        with pytest.raises(pg.PostgresUnavailableError):
            await pg.verify_deployment()
    finally:
        pg.expect_deployment(None)


async def test_a_copy_with_no_sources_asks_nothing() -> None:
    class _Conn:
        async def fetchval(self, *_a: Any) -> bool:
            raise AssertionError("no sources, no query")

    await pg_memory._refuse_moved_sources(_Conn(), [("o", "n")], pg_memory.Admission())
    await pg_memory._refuse_moved_sources(_Conn(), [("o", "n")], None)


async def test_verify_deployment_checks_a_pool_already_open(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class _Conn:
        async def fetchrow(self, sql: str) -> object:
            return {"epoch": 1, "deployment_id": "other"}

    class _Pool:
        @contextlib.asynccontextmanager
        async def acquire(self) -> AsyncIterator[_Conn]:
            yield _Conn()

    async def _open() -> _Pool:
        return _Pool()

    monkeypatch.setattr(pg, "_pool_for_loop", _open)
    pg.expect_deployment(("d", 1))
    try:
        with pytest.raises(pg.AuthorityMismatch):
            await pg.verify_deployment()
    finally:
        pg.expect_deployment(None)


async def test_verify_deployment_checks_a_pool_it_opens_once(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Opening the pool runs the check and disarms it: no second connection is taken."""

    class _Pool:
        def acquire(self) -> Any:
            raise AssertionError("the pool was checked as it opened")

    async def _open() -> _Pool:
        pg.expect_deployment(None)
        return _Pool()

    monkeypatch.setattr(pg, "_pool_for_loop", _open)
    pg.expect_deployment(("d", 1))
    try:
        await pg.verify_deployment()
    finally:
        pg.expect_deployment(None)


@pytest.mark.parametrize("error", [asyncio.CancelledError(), ConnectionResetError("reset")])
@pytest.mark.asyncio
async def test_a_dedicated_connection_that_fails_to_open_is_terminated(
    monkeypatch: pytest.MonkeyPatch, error: BaseException
) -> None:
    """A cancellation while it opens stays a cancellation; an outage there is the 503.
    Either way the connection is terminated, never handed out."""
    import asyncpg

    terminated: list[bool] = []

    class _Conn:
        def terminate(self) -> None:
            terminated.append(True)

    async def _connect(*_a: Any, **_k: Any) -> _Conn:
        return _Conn()

    async def _check(conn: Any) -> None:
        raise error

    monkeypatch.setattr(asyncpg, "connect", _connect)
    monkeypatch.setattr(pg, "_check_deployment", _check)
    monkeypatch.setattr(pg, "_require_dsn", lambda: None)
    expected = (
        type(error) if isinstance(error, asyncio.CancelledError) else pg.PostgresUnavailableError
    )
    with pytest.raises(expected):
        async with pg.dedicated():
            raise AssertionError("never handed out")
    assert terminated == [True]


@pytest.mark.asyncio
async def test_a_dedicated_blocks_own_error_passes_through(monkeypatch: pytest.MonkeyPatch) -> None:
    """A deadlock inside the block reaches its caller's retry as itself, not as a 503."""
    import asyncpg

    closed: list[bool] = []

    class _Conn:
        async def close(self, **kw: Any) -> None:
            closed.append(kw.get("timeout") is not None)

    async def _connect(*_a: Any, **_k: Any) -> _Conn:
        return _Conn()

    async def _nothing(conn: Any) -> None:
        return None

    monkeypatch.setattr(asyncpg, "connect", _connect)
    monkeypatch.setattr(pg, "_check_deployment", _nothing)
    monkeypatch.setattr(pg, "init_connection", _nothing)
    monkeypatch.setattr(pg, "_require_dsn", lambda: None)
    with pytest.raises(asyncpg.exceptions.DeadlockDetectedError):
        async with pg.dedicated():
            raise asyncpg.exceptions.DeadlockDetectedError("deadlock detected")
    assert closed == [True]


async def test_a_dedicated_close_that_fails_keeps_the_blocks_outcome(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """A close cut off by its bound (a cancel never acknowledged) is logged, not raised."""
    import asyncpg

    class _Conn:
        async def close(self, **_: Any) -> None:
            raise TimeoutError("cancel not acknowledged")

    async def _connect(*_a: Any, **_k: Any) -> _Conn:
        return _Conn()

    async def _nothing(conn: Any) -> None:
        return None

    monkeypatch.setattr(asyncpg, "connect", _connect)
    monkeypatch.setattr(pg, "_check_deployment", _nothing)
    monkeypatch.setattr(pg, "init_connection", _nothing)
    monkeypatch.setattr(pg, "_require_dsn", lambda: None)
    async with pg.dedicated() as conn:
        assert isinstance(conn, _Conn)
    with pytest.raises(asyncpg.exceptions.DeadlockDetectedError):  # what an erasure retries
        async with pg.dedicated():
            raise asyncpg.exceptions.DeadlockDetectedError("deadlock detected")
    assert "not closed cleanly" in caplog.text
    assert "cancel not acknowledged" not in caplog.text  # its summary, not its message


async def test_the_documents_a_delivery_holds_are_read_a_field_at_a_time() -> None:
    from server.core.ch_params import FIELD_BYTES, format_bind_value
    from server.memory.store import pg_publish

    sent: list[list[Any]] = []

    class _CH:
        async def query_all(self, sql: str, params: dict[str, Any]) -> list[Any]:
            sent.append(params["ids"])
            return []

    ids = [("a", "-", "-", "k/" + "x" * 300 + f"/{i}", i) for i in range(500)]
    assert await pg_publish.held_documents("o", "n", ids, ch=_CH()) == set()
    assert len(sent) > 1 and [i for part in sent for i in part] == ids
    assert all(len(format_bind_value(part).encode()) <= FIELD_BYTES for part in sent)


def test_a_redacted_row_written_over_an_embedded_head_owes_its_vectors_retirement() -> None:
    """A rollback that brings back a revision the authority holds redacted: the head's
    vectors would otherwise stay live under a row that serves no document."""
    head = {"memory_type": "semantic", "sensitivity": "", "redacted_at": None}
    redacted = {**head, "redacted_at": "2026-01-01T00:00:00"}
    assert pg_memory._replaces_vectors(redacted, head)
    assert not pg_memory._replaces_vectors(head, head)


async def test_a_copy_passes_its_sources_to_the_commit(monkeypatch: pytest.MonkeyPatch) -> None:
    """`insert_versions_over_heads` hands the revisions a copy brings back to the commit,
    which refuses one redacted since (`_refuse_moved_sources`)."""
    from server.memory.store import lifecycle, pg_publish

    asked: list[Any] = []

    async def _commit(rows: Any, *, targets: Any, admission: Any, **_: Any) -> Any:
        asked.append(list(admission.sources))
        raise pg_memory.HeadMoved()

    monkeypatch.setattr(pg, "enabled", lambda: True)
    monkeypatch.setattr(lifecycle, "check_memory_rows", lambda rows: None)
    monkeypatch.setattr(pg_publish, "commit", _commit)
    head = SimpleNamespace(version=1)
    row = {
        "org": "o",
        "namespace": "n",
        "agent": "a",
        "user_id": "-",
        "session_id": "-",
        "memory_key": "k",
        "version": 2,
    }
    with pytest.raises(pg_memory.AdmissionContendedError):
        await lifecycle.insert_versions_over_heads(
            [row],
            [head],
            sources=[("11111111-1111-1111-1111-111111111111", 1)],
        )
    assert asked == [[("11111111-1111-1111-1111-111111111111", 1)]]


@pytest.mark.parametrize(
    ("caller", "actor"),
    [
        ("token", "process:sa:deleter"),
        ("stage", "process:cli:period-digest"),
        ("request", "-"),
        ("job", "process:loom"),
    ],
)
async def test_a_commits_audit_row_names_the_caller_not_the_rows_earlier_writer(
    monkeypatch: pytest.MonkeyPatch, caller: str, actor: str
) -> None:
    """A copy (a delete's tombstone, a pin) carries its earlier writer's `actor`; the
    commit's audit row names who wrote this revision: the token's principal, else the
    pipeline stage, else '-' inside a request and `process:loom` for Loom's own jobs."""
    from server.core.logging_config import request_id_var
    from server.memory.domain.provenance import cli_actor, pipeline_actor
    from server.memory.store import pg_publish
    from tests.unit.principals import acting_as, principal

    audits: list[Any] = []

    async def _commit_versions(rows: Any, audit_rows: Any, **_: Any) -> Any:
        audits.extend(audit_rows)

    monkeypatch.setattr(pg_memory, "commit_versions", _commit_versions)
    row = {"org": "o", "namespace": "n", "memory_key": "k", "memory_id": "m", "version": 2}
    row |= {"actor": "user:earlier", "tombstoned_at": datetime(2026, 1, 1)}
    rid = request_id_var.set("req-1" if caller == "request" else None)
    try:
        with acting_as(principal("writer", subject="sa:deleter") if caller == "token" else None):
            stage = pipeline_actor(cli_actor("period-digest")) if caller == "stage" else None
            with stage or contextlib.nullcontext():
                await pg_publish.commit([row])
    finally:
        request_id_var.reset(rid)
    [audit] = audits
    assert audit["actor"] == actor


async def test_a_commit_in_a_process_never_wired_is_refused(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Rather than an audit row naming the wrong writer: every root calls `wire()`."""
    from server.memory.store import pg_publish

    async def _commit_versions(*_a: Any, **_k: Any) -> Any:
        raise AssertionError("nothing is committed")

    monkeypatch.setattr(pg_memory, "commit_versions", _commit_versions)
    monkeypatch.setattr(pg_publish, "_writer", None)
    with pytest.raises(pg_publish.WriterNotWired):
        await pg_publish.commit([{"org": "o", "namespace": "n"}])


@pytest.mark.parametrize(
    ("margin", "request_s", "delivery_s", "accepted"),
    [
        (1.0, 5, 30, True),
        (4.9, 5, 30, True),
        (5.0, 5, 30, False),  # as long as the request lease
        (10.0, 60, 10, False),  # as long as the delivery lease
        (6.0, 5, 30, False),
    ],
)
def test_a_lease_margin_must_be_shorter_than_both_leases(
    margin: float, request_s: int, delivery_s: int, accepted: bool
) -> None:
    from server.settings.catalog import StorageSettings

    def build() -> StorageSettings:
        return StorageSettings(
            storage_authority="postgres",
            pg_lease_margin_seconds=margin,
            pg_request_lease_seconds=request_s,
            pg_delivery_lease_seconds=delivery_s,
        )

    if accepted:
        assert build().pg_lease_margin_seconds == margin
    else:
        with pytest.raises(ValueError, match="LOOM_PG_LEASE_MARGIN_SECONDS"):
            build()


@pytest.mark.parametrize("confirmed", [True, False])
async def test_a_committed_revisions_fence_that_fails_is_recorded_never_raised(
    monkeypatch: pytest.MonkeyPatch, confirmed: bool
) -> None:
    """`memory.set`, `memory.bulk_set` and the revisions all fence a committed row this
    way: the drop names each key without its user id, and says when the commit's outcome
    is unknown."""
    from server.memory.app import high_fence

    dropped: list[tuple[str, str, str, str]] = []

    async def _down(*_a: Any, **_k: Any) -> None:
        raise RuntimeError("clickhouse down")

    monkeypatch.setattr(high_fence, "fence_high", _down)
    monkeypatch.setattr(
        high_fence,
        "record_drop",
        lambda org, _ns, stage, why, *, item: dropped.append((org, stage, why, item)),
    )
    keys = [(("o", "n", "a", "u", "-", "k1"), 3), (("o", "n", "a", "u", "s", "k2"), 4)]
    await high_fence.fence_committed(keys, op="memory.bulk_set", confirmed=confirmed)
    reason = "fence_failed" if confirmed else "fence_failed_outcome_unknown"
    assert dropped == [
        ("o", "memory.bulk_set", reason, "a/-/k1"),
        ("o", "memory.bulk_set", reason, "a/s/k2"),
    ]


def test_the_worker_and_the_cli_wire_the_commits_writer(monkeypatch: pytest.MonkeyPatch) -> None:
    """Each root calls `wire()` before it runs: a commit in a process that never did is
    refused (`WriterNotWired`)."""
    from server import worker_main
    from server.cli import app
    from server.memory.app import provenance
    from server.memory.store import pg_publish
    from server.workers import child

    def _ran(coro: Any) -> None:
        coro.close()

    monkeypatch.setattr(child, "die_with_parent", lambda: None)
    monkeypatch.setattr("server.core.logging_config.configure_logging", lambda: None)
    monkeypatch.setattr("server.observability.otel.init_otel", lambda: None)
    monkeypatch.setattr(worker_main.asyncio, "run", _ran)
    monkeypatch.setattr(pg_publish, "_writer", None)
    worker_main.main(["--lease-fd", "3", "--alive-fd", "4"])
    assert pg_publish._writer is provenance.writer_actor

    monkeypatch.setattr(app, "_dispatch", lambda args: 0)
    monkeypatch.setattr(app, "_load_dotenv_like_the_container", lambda: None)
    monkeypatch.setattr(pg_publish, "_writer", None)
    assert app.main(["period-digest", "--org", "o", "--namespace", "n"]) == 0
    assert pg_publish._writer is provenance.writer_actor


def _high_set(memory_key: str = "k") -> Any:
    from server.memory.api.models_crud import MemorySetInput
    from server.memory.app import set_commit

    row = {"org": "o", "namespace": "n", "agent": "a", "user_id": "u", "session_id": "-"}
    row |= {"memory_key": memory_key, "version": 7, "sensitivity": "high"}
    return set_commit._SetWrite(
        input=MemorySetInput(org="o", namespace="n", agent="a", memory_key=memory_key, value="v"),
        version=7,
        retyped_from="",
        existing=None,
        expires_at=None,
        new_sha="sha",
        memory_row=row,
        memory_id="m1",
        had_existing_row=False,
    )


@pytest.mark.parametrize("commit", ["landed", "answer_lost", "refused", "cancelled_publish"])
async def test_a_high_sets_failed_fence_after_its_commit_is_recorded(
    monkeypatch: pytest.MonkeyPatch, commit: str
) -> None:
    """Under Postgres a high memory.set fences whatever may have committed: a failed fence
    is a drop, never the answer; a commit refused before it ran fences nothing."""
    from server.memory.app import high_fence, set_commit
    from server.memory.store import pg_publish
    from server.settings import settings

    fenced: list[Any] = []
    dropped: list[tuple[str, str, str]] = []
    lost = pg.PostgresUnavailableError("lost", outcome_unknown=True)
    refused = pg.PostgresUnavailableError("down", outcome_unknown=False)

    async def _commit(*_a: Any, **_k: Any) -> Any:
        if commit == "answer_lost":
            raise lost
        if commit == "refused":
            raise refused
        return SimpleNamespace()

    async def _nothing(*_a: Any, **_k: Any) -> Any:
        return 7

    async def _publish(*_a: Any, **_k: Any) -> Any:
        if commit == "cancelled_publish":
            raise asyncio.CancelledError()
        return None

    async def _down(items: Any, **_k: Any) -> None:
        fenced.extend(items)
        raise RuntimeError("clickhouse down")

    monkeypatch.setattr(settings, "storage_authority", "postgres")
    monkeypatch.setattr(pg_memory, "settled_head_versions", _no_head)
    monkeypatch.setattr(set_commit, "commit_set_rows", _commit)
    monkeypatch.setattr(pg_publish, "publish", _publish)
    for name in ("_lift_above_deletes", "_lift_after_insert", "_announce_set"):
        monkeypatch.setattr(set_commit, name, _nothing)
    monkeypatch.setattr(set_commit, "project_stated_relations", _nothing)
    monkeypatch.setattr(high_fence, "fence_high", _down)
    monkeypatch.setattr(
        high_fence,
        "record_drop",
        lambda _o, _n, stage, why, *, item: dropped.append((stage, why, item)),
    )
    call = set_commit._set_plain_insert(_high_set(), object())
    if commit == "landed":
        assert (await call).version == 7
        assert dropped == [("memory.set", "fence_failed", "a/-/k")]
    elif commit == "cancelled_publish":  # its commit answered: fenced, and known committed
        with pytest.raises(asyncio.CancelledError):
            await call
        assert dropped == [("memory.set", "fence_failed", "a/-/k")]
    else:
        with pytest.raises(pg.PostgresUnavailableError):
            await call
        reason = [("memory.set", "fence_failed_outcome_unknown", "a/-/k")]
        assert dropped == (reason if commit == "answer_lost" else [])
    assert len(fenced) == (0 if commit == "refused" else 1)


async def test_a_high_set_on_clickhouse_is_fenced_after_a_failed_lift(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The row is in: the fence still runs at its version, and the lift's error is the
    answer, as a failed fence would be."""
    from server.memory.app import set_commit
    from server.settings import settings

    fenced: list[Any] = []

    async def _nothing(*_a: Any, **_k: Any) -> Any:
        return None

    async def _lift_fails(*_a: Any, **_k: Any) -> int:
        raise RuntimeError("lift failed")

    async def _fence(items: Any, **_k: Any) -> None:
        fenced.extend(items)

    monkeypatch.setattr(settings, "storage_authority", "clickhouse")
    monkeypatch.setattr(set_commit, "insert_memory_versions", _nothing)
    monkeypatch.setattr(set_commit, "_lift_above_deletes", _nothing)
    monkeypatch.setattr(set_commit, "_lift_after_insert", _lift_fails)
    monkeypatch.setattr(set_commit, "_announce_set", _nothing)
    monkeypatch.setattr(set_commit, "fence_high", _fence)
    with pytest.raises(RuntimeError, match="lift failed"):
        await set_commit._set_plain_insert(_high_set(), object())
    assert fenced == [(("o", "n", "a", "u", "-", "k"), 7)]


async def test_a_committed_bulk_batch_cancelled_while_publishing_is_announced(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from server.memory.app import bulk_set
    from server.memory.store import pg_publish

    done: list[str] = []
    publishing = asyncio.Event()

    async def _slow(*_a: Any, **_k: Any) -> None:
        publishing.set()
        await asyncio.Event().wait()  # until the request is cancelled

    async def _announce(state: Any) -> None:
        done.append("announced")

    monkeypatch.setattr(pg_publish, "publish", _slow)
    monkeypatch.setattr(bulk_set, "_bulk_announce", _announce)
    state = SimpleNamespace(embedding_rows=[], memory_rows=[{}])
    committed = SimpleNamespace(deadline=None, seqs=[], rows=[])
    task = asyncio.create_task(bulk_set._publish_batch(state, committed, None))
    await publishing.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert done == ["announced"]  # its 'high' rows are fenced by `fenced_once_committed`


def _commit_in_flight_cancel() -> asyncio.CancelledError:
    e = asyncio.CancelledError()
    e.commit_in_flight = True  # as `pg.transaction` marks a cancel at its COMMIT
    return e


@pytest.mark.parametrize(
    ("answered", "raised", "dropped_as"),
    [
        (True, None, "fence_failed"),  # committed and published
        (True, asyncio.CancelledError(), "fence_failed"),  # cancelled after the commit answered
        (False, "outcome_unknown", "fence_failed_outcome_unknown"),
        (False, "cancelled_in_commit", "fence_failed_outcome_unknown"),
        (False, "restore_unknown", "fence_failed_outcome_unknown"),  # a restore's re-read
        (False, asyncio.CancelledError(), None),  # cancelled before the commit: rolled back
        (False, "refused", None),
    ],
)
async def test_a_postgres_write_of_high_rows_is_fenced_once_whenever_its_commit_may_have_landed(
    monkeypatch: pytest.MonkeyPatch, answered: bool, raised: Any, dropped_as: str | None
) -> None:
    """`fenced_once_committed` fences once as the block ends, the rows read then (a lifted
    version is the one fenced), when the commit answered or may have landed."""
    from server.core.errors import BackendUnavailableError
    from server.memory.app import high_fence
    from server.settings import settings

    errors = {
        "outcome_unknown": pg.PostgresUnavailableError("lost", outcome_unknown=True),
        "cancelled_in_commit": _commit_in_flight_cancel(),
        "restore_unknown": BackendUnavailableError("may not have landed", outcome_unknown=True),
        "refused": pg.PostgresUnavailableError("down", outcome_unknown=False),
    }
    error = errors.get(raised, raised) if isinstance(raised, str) else raised
    fenced: list[Any] = []
    dropped: list[str] = []

    async def _down(items: Any, **_k: Any) -> None:
        fenced.append(list(items))
        raise RuntimeError("clickhouse down")

    monkeypatch.setattr(settings, "storage_authority", "postgres")
    monkeypatch.setattr(pg_memory, "settled_head_versions", _no_head)
    monkeypatch.setattr(high_fence, "fence_high", _down)
    monkeypatch.setattr(high_fence, "record_drop", lambda _o, _n, _s, why, **_: dropped.append(why))
    rows = [(("o", "n", "a", "u", "-", "k"), 7)]

    async def _write() -> None:
        async with high_fence.fenced_once_committed(lambda: rows, op="memory.set") as answer:
            if answered:
                answer.mark_committed()
            rows[0] = (rows[0][0], 9)  # lifted above a delete that landed meanwhile
            if error is not None:
                raise error

    if error is None:
        await _write()
    else:
        with pytest.raises(type(error)):
            await _write()
    assert dropped == ([dropped_as] if dropped_as else [])
    assert fenced == ([[(("o", "n", "a", "u", "-", "k"), 9)]] if dropped_as else [])


@pytest.mark.parametrize(
    ("held", "fenced", "dropped"),
    [
        ({}, [7], []),  # no head: a first write may still be in flight
        ({("o", "n", "a", "u", "-", "k"): 6}, [], []),  # settled below it: not landed
        ({("o", "n", "a", "u", "-", "k"): 7}, [7], []),
        ({("o", "n", "a", "u", "-", "k"): 8}, [], []),  # a later write owns the key
        (pg.PostgresUnavailableError("lock wait"), [7], ["fenced_outcome_unknown"]),
    ],
)
async def test_a_lost_commit_is_fenced_by_its_settled_head(
    monkeypatch: pytest.MonkeyPatch, held: Any, fenced: list[int], dropped: list[str]
) -> None:
    from server.memory.app import high_fence
    from server.settings import settings

    seen: list[int] = []
    drops: list[str] = []

    async def _heads(keys: Any) -> dict[Any, int]:
        if isinstance(held, Exception):
            raise held
        return dict(held)

    async def _fence(items: Any, **_k: Any) -> None:
        seen.extend(v for _key, v in items)

    monkeypatch.setattr(settings, "storage_authority", "postgres")
    monkeypatch.setattr(pg_memory, "settled_head_versions", _heads)
    monkeypatch.setattr(high_fence, "fence_high", _fence)
    monkeypatch.setattr(high_fence, "record_drop", lambda _o, _n, _s, why, **_: drops.append(why))
    with pytest.raises(pg.PostgresUnavailableError):
        async with high_fence.fenced_once_committed(
            lambda: [(("o", "n", "a", "u", "-", "k"), 7)], op="memory.set"
        ):
            raise pg.PostgresUnavailableError("lost", outcome_unknown=True)
    assert (seen, drops) == (fenced, dropped)


async def test_a_lost_commit_whose_heads_and_fence_both_fail_is_recorded_once(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from server.memory.app import high_fence
    from server.settings import settings

    drops: list[str] = []

    async def _unread(keys: Any) -> dict[Any, int]:
        raise pg.PostgresUnavailableError("lock wait")

    async def _down(*_a: Any, **_k: Any) -> None:
        raise RuntimeError("clickhouse down")

    monkeypatch.setattr(settings, "storage_authority", "postgres")
    monkeypatch.setattr(pg_memory, "settled_head_versions", _unread)
    monkeypatch.setattr(high_fence, "fence_high", _down)
    monkeypatch.setattr(high_fence, "record_drop", lambda _o, _n, _s, why, **_: drops.append(why))
    with pytest.raises(pg.PostgresUnavailableError):
        async with high_fence.fenced_once_committed(
            lambda: [(("o", "n", "a", "u", "-", "k"), 7)], op="memory.set"
        ):
            raise pg.PostgresUnavailableError("lost", outcome_unknown=True)
    assert drops == ["fence_failed_outcome_unknown"]


async def test_a_cancel_during_the_release_after_commit_reaches_the_fence(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The chain end to end: `pg.transaction` marks the cancel, the fence reads it."""
    from server.memory.app import high_fence
    from server.settings import settings

    seen: list[bool] = []

    async def _fence(items: Any, *, confirmed: bool = True, **_k: Any) -> None:
        seen.append(confirmed)

    monkeypatch.setattr(settings, "storage_authority", "postgres")
    monkeypatch.setattr(pg_memory, "settled_head_versions", _no_head)
    monkeypatch.setattr(high_fence, "fence_committed", _fence)
    _pg_pool(monkeypatch, _PgConn(), release=asyncio.CancelledError())
    with pytest.raises(asyncio.CancelledError):
        async with high_fence.fenced_once_committed(
            lambda: [(("o", "n", "a", "u", "-", "k"), 7)], op="memory.set"
        ):
            async with pg.transaction():
                pass
    assert seen == [False]


async def test_the_settled_read_locks_the_heads_in_order_and_never_commits(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Its row locks give it a transaction id: a COMMIT would wait on the synchronous
    standby as a write's does, so it is rolled back."""
    sent: list[str] = []

    class _Conn(_PgConn):
        async def execute(self, sql: object, *a: object) -> str:
            sent.append(str(sql))
            return "SET"

        async def fetch(self, sql: object, *a: object) -> list[Any]:
            sent.append(str(sql))
            head = {"org": "o", "namespace": "n", "agent": "a", "user_id": "u"}
            return [head | {"session_id": "-", "memory_key": "k", "version": 7}]

    conn = _Conn(commit=AssertionError("the settled read commits nothing"))
    _pg_pool(monkeypatch, conn)
    key = ("o", "n", "a", "u", "-", "k")
    assert await pg_memory.settled_head_versions([key]) == {key: 7}
    [read] = [q for q in sent if "FOR SHARE OF c" in q]
    assert "ORDER BY c.org" in read and any("lock_timeout" in q for q in sent)
    assert conn.rolled_back


@pytest.mark.parametrize(
    ("head", "landed"),
    [
        (7, True),
        (6, False),  # settled below it: heads only move up
        (8, None),  # a later write may sit over a restore that landed
        (None, None),
        (pg.PostgresUnavailableError("lock wait"), None),
    ],
)
async def test_a_postgres_restore_reads_its_lost_commit_back_settled(
    monkeypatch: pytest.MonkeyPatch, head: Any, landed: bool | None
) -> None:
    from server.memory.app import restore

    async def _heads(keys: Any) -> dict[Any, int]:
        if isinstance(head, Exception):
            raise head
        return {} if head is None else dict.fromkeys(keys, head)

    monkeypatch.setattr(pg, "enabled", lambda: True)
    monkeypatch.setattr(pg_memory, "settled_head_versions", _heads)
    assert await restore._restore_landed(None, _row("k", version=7)) is landed


@pytest.mark.parametrize(
    ("found", "landed"),
    [(7, True), (6, None), (None, None), (RuntimeError("down"), None)],
)
async def test_a_clickhouse_restore_never_settles_that_it_did_not_land(
    monkeypatch: pytest.MonkeyPatch, found: Any, landed: bool | None
) -> None:
    from server.memory.app import restore

    async def _read(_input: Any) -> Any:
        if isinstance(found, Exception):
            raise found
        return None if found is None else SimpleNamespace(version=found)

    monkeypatch.setattr(pg, "enabled", lambda: False)
    monkeypatch.setattr(restore, "_read_restorable", _read)
    assert await restore._restore_landed(None, _row("k", version=7)) is landed


async def test_a_clickhouse_restore_whose_reread_misses_answers_outcome_unknown(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import server.memory.store as memory_repo
    from server.memory.app import restore

    async def _insert(*_a: Any, **_k: Any) -> None:
        raise BackendTimeoutError("slow insert")

    async def _missing(_input: Any) -> None:
        return None

    async def _mirror() -> None:
        return None

    monkeypatch.setattr(pg, "enabled", lambda: False)
    monkeypatch.setattr(memory_repo, "insert_versions_over_heads", _insert)
    monkeypatch.setattr(restore, "_read_restorable", _missing)
    with pytest.raises(BackendUnavailableError) as raised:
        await restore._insert_restore(
            None,
            _row("k", version=7),
            None,
            ch=None,
            what="the restore",
            mirror=_mirror,
        )
    assert raised.value.extra["outcome_unknown"] is True


async def test_a_restore_cancelled_while_it_reads_a_lost_commit_back_is_fenced(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import server.memory.store as memory_repo
    from server.memory.app import high_fence, restore

    seen: list[bool] = []
    reading = asyncio.Event()

    async def _lost(*_a: Any, **_k: Any) -> None:
        raise pg.PostgresUnavailableError("lost", outcome_unknown=True)

    async def _stuck(_input: Any, _row: Any) -> bool | None:
        reading.set()
        await asyncio.Event().wait()
        return None

    async def _fence(items: Any, *, confirmed: bool = True, **_k: Any) -> bool:
        seen.append(confirmed)
        return True

    async def _mirror() -> None:
        return None

    monkeypatch.setattr(pg, "enabled", lambda: True)
    monkeypatch.setattr(memory_repo, "insert_versions_over_heads", _lost)
    monkeypatch.setattr(restore, "_restore_landed", _stuck)
    monkeypatch.setattr(high_fence, "fence_committed", _fence)
    monkeypatch.setattr(pg_memory, "settled_head_versions", _no_head)
    row = _row("k", version=7, sensitivity="high")

    async def _restore() -> None:
        async with high_fence.fenced_once_committed(
            lambda: [(identity_key(row), 7)], op="memory.restore"
        ) as answer:
            await restore._insert_restore(
                None,
                row,
                None,
                ch=None,
                what="the restore",
                mirror=_mirror,
                on_commit=answer.mark_committed,
            )

    task = asyncio.create_task(_restore())
    await reading.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert seen == [False]


async def test_the_shutdown_drain_names_the_writes_it_leaves_running() -> None:
    release = asyncio.Event()

    async def _slow() -> None:
        await release.wait()

    async def _quick() -> None:
        return None

    task = asyncio.create_task(shielded.shielded(_slow(), name="memory.set.fence"))
    await shielded.shielded(_quick(), name="quick")
    assert await shielded.drain(timeout_s=0.01) == ["memory.set.fence"]
    release.set()
    await task
    assert await shielded.drain(timeout_s=0.01) == []


async def test_a_fence_runs_to_its_end_when_the_request_is_cancelled_again(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """anyio repeats a cancellation at every await: the fence is shielded from it."""
    from server.memory.app import high_fence
    from server.settings import settings

    fencing, release = asyncio.Event(), asyncio.Event()
    done: list[str] = []

    async def _fence(items: Any, **_k: Any) -> None:
        fencing.set()
        await release.wait()
        done.append("fenced")

    monkeypatch.setattr(settings, "storage_authority", "postgres")
    monkeypatch.setattr(high_fence, "fence_high", _fence)

    async def _write() -> None:
        async with high_fence.fenced_once_committed(
            lambda: [(("o", "n", "a", "u", "-", "k"), 7)], op="memory.set"
        ) as answer:
            answer.mark_committed()
            await asyncio.Event().wait()

    task = asyncio.create_task(_write())
    await asyncio.sleep(0)
    task.cancel()
    await fencing.wait()
    task.cancel()  # the second cancel, during the fence
    with pytest.raises(asyncio.CancelledError):
        await task
    release.set()
    await shielded.drain(timeout_s=1)
    assert done == ["fenced"]


async def test_a_write_on_clickhouse_is_not_fenced_by_the_postgres_context(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from server.memory.app import high_fence
    from server.settings import settings

    async def _never(*_a: Any, **_k: Any) -> None:
        raise AssertionError("each ClickHouse write fences in place")

    monkeypatch.setattr(settings, "storage_authority", "clickhouse")
    monkeypatch.setattr(high_fence, "fence_high", _never)
    async with high_fence.fenced_once_committed(lambda: [(("o",) * 6, 1)], op="x") as answer:
        answer.mark_committed()


@pytest.mark.parametrize(
    ("outcome", "fenced"),
    [
        ("committed", [True]),
        ("outcome_unknown", [False]),
        ("cancelled_in_commit", [False]),
        ("unavailable", []),  # refused before it ran
    ],
)
async def test_a_postgres_bulk_batch_of_high_rows_is_fenced_once_its_commit_may_have_landed(
    monkeypatch: pytest.MonkeyPatch, outcome: str, fenced: list[bool]
) -> None:
    from server.memory.app import bulk_set, high_fence

    seen: list[bool] = []
    key = ("o", "n", "a", "u", "-", "k")
    errors: dict[str, BaseException] = {
        "outcome_unknown": pg.PostgresUnavailableError("lost", outcome_unknown=True),
        "cancelled_in_commit": _commit_in_flight_cancel(),
        "unavailable": pg.PostgresUnavailableError("down"),
    }

    async def _commit(state: Any, retire: Any) -> Any:
        if outcome in errors:
            raise errors[outcome]
        return SimpleNamespace()

    async def _land(*_a: Any) -> None:
        pass

    async def _no_lift(*_a: Any, **_k: Any) -> dict[Any, int]:
        return {}

    async def _fence(items: Any, *, confirmed: bool = True, **_k: Any) -> None:
        seen.append(confirmed)

    monkeypatch.setattr(pg, "enabled", lambda: True)
    monkeypatch.setattr(bulk_set, "lift_above_deletes", _no_lift)
    monkeypatch.setattr(bulk_set, "_vectors_to_retire", lambda state: {})
    monkeypatch.setattr(bulk_set, "_high_winners", lambda state: [(key, 7)])
    monkeypatch.setattr(bulk_set, "_commit_batch", _commit)
    monkeypatch.setattr(bulk_set, "_land_batch", _land)
    monkeypatch.setattr(high_fence, "fence_committed", _fence)
    state = SimpleNamespace(memory_rows=[{"org": "o", "namespace": "n"}], embedding_rows=[])
    state.results = []
    with contextlib.suppress(pg.PostgresUnavailableError, asyncio.CancelledError):
        await bulk_set._bulk_insert(None, state)
    assert seen == fenced


async def test_a_clickhouse_bulk_batch_is_fenced_after_a_failed_lift(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from server.memory.app import bulk_set

    fenced: list[Any] = []

    async def _published(*_a: Any) -> tuple[None, None]:
        return None, None

    async def _lift_fails(*_a: Any, **_k: Any) -> dict[Any, int]:
        raise RuntimeError("lift failed")

    async def _fence(items: Any, **_k: Any) -> None:
        fenced.append(items)

    async def _announce(state: Any) -> None:
        pass

    winners = [(("o", "n", "a", "u", "-", "k"), 7)]
    monkeypatch.setattr(pg, "enabled", lambda: False)
    monkeypatch.setattr(bulk_set, "_publish_batch", _published)
    monkeypatch.setattr(bulk_set, "lift_after_insert", _lift_fails)
    monkeypatch.setattr(bulk_set, "_high_winners", lambda state: winners)
    monkeypatch.setattr(bulk_set, "fence_high", _fence)
    monkeypatch.setattr(bulk_set, "_bulk_announce", _announce)
    state = SimpleNamespace(memory_rows=[{"org": "o", "namespace": "n"}], unpublished=None)
    state.embedding_rows = []
    with pytest.raises(RuntimeError, match="lift failed"):
        await bulk_set._land_batch(None, state, None, {})
    assert fenced == [winners]


@pytest.mark.parametrize("authority", ["postgres", "clickhouse"])
async def test_a_bulk_batch_is_announced_and_on_clickhouse_fenced_whatever_failed_before(
    monkeypatch: pytest.MonkeyPatch, authority: str
) -> None:
    from server.memory.app import bulk_set

    fenced: list[Any] = []
    announced: list[str] = []

    async def _down(items: Any, **_k: Any) -> None:
        fenced.append(items)
        raise RuntimeError("clickhouse down")

    async def _announce(state: Any) -> None:
        announced.append("announced")

    monkeypatch.setattr(pg, "enabled", lambda: authority == "postgres")
    monkeypatch.setattr(
        bulk_set, "_high_winners", lambda state: [(("o", "n", "a", "u", "-", "k"), 7)]
    )
    monkeypatch.setattr(bulk_set, "_bulk_announce", _announce)
    monkeypatch.setattr(bulk_set, "fence_high", _down)
    call = bulk_set._fence_and_announce(object(), ch=None)
    if authority == "clickhouse":
        with pytest.raises(RuntimeError, match="clickhouse down"):
            await call
    else:
        await call
    assert announced == ["announced"]
    assert len(fenced) == (1 if authority == "clickhouse" else 0)


@pytest.mark.parametrize("spent", [1, 2])
async def test_the_worker_stops_its_stubs_past_the_pass_deadline(monkeypatch, spent) -> None:
    """A namespace's claims past their attempts each publish a stub; once the worker's
    time for the pass is up after one, it starts neither the next stub nor the
    namespace's batch, as it does between claims it publishes one by one."""
    import time

    from server.memory.app import pg_delivery
    from server.memory.store.pg_memory import Claimed

    stubbed: list[int] = []
    renewed: list[Any] = []

    async def _last_attempt(c, error, run):
        stubbed.append(c.seq)
        run.stop_at = time.monotonic() - 1  # the pass's time runs out during this stub

    async def _renew(claims):
        renewed.extend(claims)
        return claims

    def _claim(seq: int, attempts: int) -> Claimed:
        return Claimed(
            org="o",
            namespace="n",
            seq=seq,
            targets=pg_memory.SEARCH_DOC,
            lease_token=1,
            attempts=attempts,
            row={},
            audit={},
            deadline=0.0,
        )

    monkeypatch.setattr(pg_delivery, "_last_attempt", _last_attempt)
    monkeypatch.setattr(pg_delivery.pg_memory, "renew", _renew)
    monkeypatch.setattr(pg_delivery.settings, "pg_delivery_max_attempts", 1)
    run = pg_delivery._Pass(worker=True, stop_at=time.monotonic() + 60)
    with pytest.raises(pg_delivery._Stopped):
        await pg_delivery._publish_namespace(
            [*(_claim(seq, 1) for seq in range(1, spent + 1)), _claim(9, 0)], run
        )
    assert (stubbed, renewed) == ([1], [])


@pytest.mark.parametrize(
    "field",
    ["retention_memory_days", "retention_memory_embeddings_days", "retention_documents_days"],
)
@pytest.mark.asyncio
async def test_the_postgres_profile_refuses_a_ttl_on_a_table_it_holds(monkeypatch, field) -> None:
    from server.core import pg_profile

    monkeypatch.setattr(pg_profile.settings, field, 30)
    with pytest.raises(RuntimeError, match="Unset them"):
        await pg_profile.start()
