"""Restore drills: a store restored to an earlier point, then `loom reconcile`.

Each drill writes, takes a copy of one org's rows of the store it restores (the backup),
writes more (a redaction and a user's erasure among it), puts the copy back in place of
the org's rows (the restore), and reconciles. Then search serves what it served before
the restore, and nothing erased or redacted after the copy is back.

The Postgres authority job runs this file with no background workers, so the reconcile
is the only publisher; it runs in this process with `--allow-live` (the server under test
is up).
"""

from __future__ import annotations

import asyncio
import os
import uuid
from datetime import UTC, datetime
from typing import Any

import httpx
import pytest

from tests.integration._eventually import eventually
from tests.integration._resources import unique_org

pytestmark = pytest.mark.asyncio

_SEQUENTIAL = {"select_sequential_consistency": 1}
# The ClickHouse tables one org's memory lives in, the copy a drill restores.
_CH_TABLES = ("memory", "memory_versions", "memory_embeddings", "erasure_requests")
# The Postgres tables that hold an org's rows, parents first (memory_deliveries refers to
# memory_revisions).
_PG_TABLES = (
    "scope_generations",
    "memory_revisions",
    "memory_current",
    "memory_deliveries",
    "idempotency",
    "erased_memories",
    "audit_outbox",
    "knowledge_heads",
    "knowledge_deliveries",
    "erased_documents",
    "erasure_ledger",
)
_SECRET = "the vault code is 4417"


@pytest.fixture
async def stores(control_plane_url: str) -> Any:
    """This process's ClickHouse client and Postgres pool, under the Postgres authority."""
    from server.core import pg
    from server.core.ch import get_client

    os.environ.get("LOOM_PG_DSN") or pytest.skip("LOOM_PG_DSN not set")
    async with httpx.AsyncClient(base_url=control_plane_url, timeout=10) as c:
        if (await c.get("/v1/health")).json().get("storage_authority") != "postgres":
            pytest.skip("the server under test is not of the Postgres authority")
    assert pg.enabled(), "the pytest process needs LOOM_STORAGE_AUTHORITY=postgres too"
    ch = get_client()
    await ch.connect()
    try:
        yield ch
    finally:
        await ch.disconnect()
        await pg.close_pool()


class _Org:
    def __init__(self, url: str, mint: Any) -> None:
        self.org, self.ns = unique_org("restore"), f"rs-{uuid.uuid4().hex[:8]}"
        self.url = url
        self.headers = {
            "Authorization": "Bearer "
            + mint(org=self.org, namespaces=["*"], roles=["writer", "admin", "org_admin"])
        }

    async def call(self, op: str, body: dict[str, Any]) -> dict[str, Any]:
        # An erasure's mutations run as long as ClickHouse takes.
        async with httpx.AsyncClient(base_url=self.url, timeout=600) as c:
            r = await c.post(f"/v1/{op}", json={"org": self.org, **body}, headers=self.headers)
        assert r.status_code == 200, (op, r.text)
        return r.json()

    async def set(self, user: str, key: str, value: str) -> dict[str, Any]:
        return await self.call(
            "memory.set",
            {
                "namespace": self.ns,
                "agent": "a",
                "user_id": user,
                "memory_key": key,
                "memory_type": "semantic",
                "value": value,
            },
        )

    async def search(self, query: str) -> list[tuple[str, float]]:
        out = await self.call(
            "memory.search",
            {"namespace": self.ns, "agent": "*", "user_id": "*", "query": query, "top_k": 10},
        )
        return [(h["memory_key"], round(float(h.get("score") or 0), 4)) for h in out["results"]]


async def _settled(org: str) -> None:
    """Every delivery of the org published."""
    from server.core import pg

    async def none_owed() -> bool:
        n = await pg.fetchval("SELECT count(*) FROM loom.memory_deliveries WHERE org = $1", org)
        return int(n) == 0

    await eventually(none_owed, description="the org's deliveries published")


def _ch_text(ch_sync: Any, org: str, text: str) -> int:
    """Rows of the org's memory tables in ClickHouse whose value holds `text`."""
    n = 0
    for table, col in (("memory", "value"), ("memory_versions", "value")):
        n += int(
            ch_sync.query(
                f"SELECT count() FROM loom.{table} WHERE org = {{o:String}} "
                f"AND position({col}, {{t:String}}) > 0",
                settings=_SEQUENTIAL,
                parameters={"o": org, "t": text},
            ).result_rows[0][0]
        )
    return n


async def _writes_after_the_backup(o: _Org) -> None:
    """A new memory, an update then a redaction of the first version of k2, and the
    erasure of user u1 (whose memory holds the secret)."""
    await o.set("u2", "k3", "the gamma ray burst was detected at dawn")
    first = await o.set("u2", "k2", f"beta note: {_SECRET}")
    await o.set("u2", "k2", "beta note, the code removed")
    await o.call(
        "memory.redact",
        {"memory_id": first["memory_id"], "version": str(first["version"]), "reason": "leak"},
    )
    await o.call(
        "erasure.request",
        {"scope_type": "user_id", "scope_value": "u1", "purge": True, "requester": "drill"},
    )


async def test_a_clickhouse_restore_is_reconciled(
    control_plane_url: str, mint_test_token: Any, stores: Any, ch_client: Any
) -> None:
    from server.cli import restore as cli_restore

    o = _Org(control_plane_url, mint_test_token)
    await o.set("u1", "k1", f"alpha: {_SECRET}")
    await o.set("u2", "k2", "beta note")
    await _settled(o.org)
    backup = {
        t: ch_client.query(
            f"SELECT * FROM loom.{t} WHERE org = {{o:String}}",
            settings=_SEQUENTIAL,
            parameters={"o": o.org},
        )
        for t in _CH_TABLES
    }
    point = datetime.now(UTC)
    await asyncio.sleep(1)  # the point is before everything below, at a second's grain
    await _writes_after_the_backup(o)
    await o.set("u1", "k4", "the delta observatory opens at nine")  # after the erasure
    await _settled(o.org)
    queries = ["gamma ray burst", "beta note", "vault code", "alpha"]
    control = {q: await o.search(q) for q in queries}
    assert _ch_text(ch_client, o.org, _SECRET) == 0

    # The restore: the org's rows as the backup held them.
    for t, rows in backup.items():
        ch_client.command(f"DELETE FROM loom.{t} WHERE org = {{o:String}}", parameters={"o": o.org})
        if rows.result_rows:
            ch_client.insert(f"loom.{t}", rows.result_rows, column_names=rows.column_names)
    assert _ch_text(ch_client, o.org, _SECRET) > 0  # the erased memory came back

    rc = await cli_restore.reconcile("clickhouse", point, apply=True, live=True, ch=stores)
    assert rc == 0
    assert _ch_text(ch_client, o.org, _SECRET) == 0
    # The replayed erasure reads completed in the restored ledger: nothing for the
    # recovery worker to resume.
    states = ch_client.query(
        "SELECT status FROM loom.erasure_requests FINAL WHERE org = {o:String}",
        settings=_SEQUENTIAL,
        parameters={"o": o.org},
    ).result_rows
    assert states and {s for (s,) in states} == {"completed"}, states
    assert {q: await o.search(q) for q in queries} == control
    # What the erased user wrote after the erasure stays: the replay deletes no later
    # write, and the republish brings its row and search document back.
    for table in ("memory", "memory_embeddings"):
        rows = ch_client.query(
            f"SELECT count() FROM loom.{table} FINAL WHERE org = {{o:String}} "
            "AND memory_key = 'k4' AND tombstoned_at IS NULL",
            settings=_SEQUENTIAL,
            parameters={"o": o.org},
        ).result_rows
        assert rows[0][0] == 1, table


async def test_a_postgres_restore_is_reconciled(
    control_plane_url: str, mint_test_token: Any, stores: Any, ch_client: Any
) -> None:
    from server.cli import restore as cli_restore
    from server.core import pg

    o = _Org(control_plane_url, mint_test_token)
    await o.set("u1", "k1", f"alpha: {_SECRET}")
    leaked = await o.set("u2", "k2", f"beta note: {_SECRET}")
    await _settled(o.org)
    tag = uuid.uuid4().hex[:8]
    async with pg.dedicated() as conn:
        for t in _PG_TABLES:  # the backup: a copy of the org's rows of each table
            await conn.execute(
                f"CREATE TABLE public.drill_{tag}_{t} AS SELECT * FROM loom.{t} WHERE org = $1",
                o.org,
            )
    point = datetime.now(UTC)
    await asyncio.sleep(1)
    # After the point: k2 rewritten and its leaked version redacted, u1 erased. The
    # restore loses the rewrite, so the redacted version is the restored copy's head.
    await o.set("u2", "k2", "beta note, the code removed")
    await o.call(
        "memory.redact",
        {"memory_id": leaked["memory_id"], "version": str(leaked["version"]), "reason": "leak"},
    )
    await o.call(
        "erasure.request",
        {"scope_type": "user_id", "scope_value": "u1", "purge": True, "requester": "drill"},
    )
    await _settled(o.org)
    # Each Postgres step is recorded in the ledger row in the transaction it committed
    # in, which the restore below undoes with the rest.
    marked = await pg.fetchval("SELECT pg_steps FROM loom.erasure_ledger WHERE org = $1", o.org)
    assert {"pg_memory.delete", "pg_knowledge.delete"} <= set(marked)

    # The point-in-time restore: the org's rows as they were at the point.
    async with pg.dedicated() as conn, conn.transaction():
        for t in reversed(_PG_TABLES):
            await conn.execute(f"DELETE FROM loom.{t} WHERE org = $1", o.org)
        for t in _PG_TABLES:
            cols = ", ".join(
                r["column_name"]
                for r in await conn.fetch(
                    "SELECT column_name FROM information_schema.columns WHERE table_schema = "
                    "'loom' AND table_name = $1 AND is_generated = 'NEVER' ORDER BY ordinal_position",
                    t,
                )
            )
            await conn.execute(
                f"INSERT INTO loom.{t} ({cols}) SELECT {cols} FROM public.drill_{tag}_{t}"
            )
    async with pg.dedicated() as conn:
        for t in _PG_TABLES:
            await conn.execute(f"DROP TABLE public.drill_{tag}_{t}")
    heads = await pg.fetchval(
        "SELECT count(*) FROM loom.memory_current WHERE org = $1 AND user_id = 'u1'", o.org
    )
    assert heads == 1  # the erased memory is a live head again

    rc = await cli_restore.reconcile("postgres", point, apply=True, live=True, ch=stores)
    assert rc == 0
    assert not await pg.fetchval(
        "SELECT count(*) FROM loom.memory_current WHERE org = $1 AND user_id = 'u1'", o.org
    )
    # The search finds neither the erased memory nor the redacted text, and the head the
    # restore brought back serves no secret.
    for q in ("vault code", "alpha"):
        assert all(key != "k1" for key, _ in await o.search(q))
    got = await o.call(
        "memory.get",
        {"namespace": o.ns, "agent": "a", "user_id": "u2", "memory_key": "k2"},
    )
    assert _SECRET not in str(got)


async def test_the_ledger_copy_fills_in_what_postgres_missed_and_keeps_what_it_ended(
    stores: Any,
) -> None:
    """`pg_ledger.backfill` against this Postgres, from a ClickHouse ledger it is handed:
    a row only ClickHouse records is copied in, a completion this ledger missed is copied
    over its open row, a row it already ended is left alone, and the walk pages on to the
    end of ClickHouse's rows and starts over; `sweep` removes a row past the ledger's days."""
    from datetime import timedelta

    from server.core import pg
    from server.erasure.store import pg_ledger
    from server.settings import settings

    org = unique_org("ledger")
    now = datetime.now(UTC).replace(tzinfo=None, microsecond=0)
    old = now - timedelta(hours=1)  # past the open-row check's wait
    ids = {k: str(uuid.uuid4()) for k in ("only_ch", "missed", "ended", "swept")}

    def ch_row(
        key: str, *, at: datetime, completed: datetime | None, abandoned: datetime | None
    ) -> dict[str, Any]:
        return {
            "org": org, "id": ids[key], "scope_type": "user_id", "scope_value": key, "purge": True,
            "requester": "drill", "urgency": "normal", "requested_at": at,
            "completed_at": completed, "abandoned_at": abandoned,
        }  # fmt: skip

    ch_rows = [
        ch_row("only_ch", at=old, completed=old + timedelta(minutes=1), abandoned=None),
        ch_row("missed", at=old + timedelta(seconds=1), completed=old + timedelta(minutes=2), abandoned=None),
        ch_row("ended", at=old + timedelta(seconds=2), completed=None, abandoned=old + timedelta(minutes=3)),
    ]  # fmt: skip
    for key, completed in (("missed", None), ("ended", old + timedelta(minutes=4))):
        row = next(r for r in ch_rows if r["id"] == ids[key])
        await pg_ledger.record(row)
        if completed:
            await pg_ledger.complete(org, ids[key], completed)

    class _Ledger:
        """ClickHouse's ledger as `backfill` reads it: this org's rows, by its cursor."""

        async def query_all(self, sql: str, params: dict[str, Any]) -> list[dict[str, Any]]:
            if "id IN" in sql:
                wanted = {str(i) for i in params["ids"]}
                return [
                    r
                    for r in ch_rows
                    if r["id"] in wanted and (r["completed_at"] or r["abandoned_at"])
                ]
            # The walk has passed the missed row: only the open-row check finds it.
            after = (params["at"], params["id"])
            walked = [r for r in ch_rows if r["id"] != ids["missed"]]
            page = [r for r in walked if (r["requested_at"], r["id"]) > after]
            return page[: params["limit"]]  # fmt: skip

    pg_ledger._cursor = None
    await pg_ledger.backfill(2, ch=_Ledger())  # type: ignore[arg-type]
    assert pg_ledger._cursor is not None  # a full page: the walk goes on from its last row
    await pg_ledger.backfill(2, ch=_Ledger())  # type: ignore[arg-type]
    assert pg_ledger._cursor is None  # a short page: the next walk starts over

    got = {
        str(r["id"]): r
        for r in await pg.fetch(
            "SELECT id, completed_at, abandoned_at FROM loom.erasure_ledger WHERE org = $1", org
        )
    }
    assert got[ids["only_ch"]]["completed_at"] is not None  # copied in
    # Open here past `_OPEN_CHECK_AFTER`: its completion copied over by the check.
    assert got[ids["missed"]]["completed_at"] is not None
    # Already ended here: ClickHouse's abandonment does not replace the completion.
    assert got[ids["ended"]]["completed_at"] is not None
    assert got[ids["ended"]]["abandoned_at"] is None

    gone = now - timedelta(days=settings.erasure_ledger_days + 1)
    await pg_ledger.record(ch_row("swept", at=gone, completed=None, abandoned=None))
    while await pg_ledger.sweep(1000):
        pass
    assert not await pg.fetchval(
        "SELECT count(*) FROM loom.erasure_ledger WHERE org = $1 AND id = $2",
        org,
        uuid.UUID(ids["swept"]),
    )
    await pg.execute("DELETE FROM loom.erasure_ledger WHERE org = $1", org)


async def test_a_held_delivery_parked_out_of_attempts_is_owed_in_full_again(
    control_plane_url: str, mint_test_token: Any, stores: Any
) -> None:
    """One parked out of attempts before the restore would be parked again at its claim,
    or sent as the last attempt's stub: held, its attempts count from none."""
    from server.core import pg, pg_lease
    from server.memory.store import pg_memory

    o = _Org(control_plane_url, mint_test_token)
    await o.set("u1", "k1", "a parked delivery")
    await _settled(o.org)
    seq = await pg.fetchval(
        "SELECT max(seq) FROM loom.memory_revisions WHERE org = $1 AND namespace = $2",
        o.org,
        o.ns,
    )
    await pg.execute(
        "INSERT INTO loom.memory_deliveries (org, namespace, seq, targets, lease_until, "
        "fenced_until, attempts, held, last_error) "
        "VALUES ($1, $2, $3, $4, 'infinity', now(), 99, true, 'ClickHouse refused')",
        o.org,
        o.ns,
        seq,
        pg_memory.AUDIT_ROW,
    )
    try:
        await pg_memory.hold([(o.org, o.ns, int(seq))], [pg_memory.MEMORY_ROW])
        row = await pg.fetchrow(
            "SELECT targets, attempts, held, last_error, lease_until = 'infinity' AS parked "
            "FROM loom.memory_deliveries WHERE org = $1 AND namespace = $2 AND seq = $3",
            o.org,
            o.ns,
            seq,
        )
        assert row["targets"] == pg_memory.AUDIT_ROW | pg_memory.MEMORY_ROW
        assert (row["attempts"], row["held"]) == (0, False)
        assert row["last_error"] == pg_lease.HELD_BY_RECONCILE and row["parked"]
    finally:
        await pg.execute(
            "DELETE FROM loom.memory_deliveries WHERE org = $1 AND namespace = $2 AND seq = $3",
            o.org,
            o.ns,
            seq,
        )


async def test_held_knowledge_deliveries_count_their_attempts_from_none(
    control_plane_url: str, mint_test_token: Any, stores: Any
) -> None:
    """As for memory: a knowledge delivery parked out of attempts is owed in full."""
    from datetime import timedelta

    from server.core import pg, pg_lease
    from server.knowledge.store import pg_documents

    o = _Org(control_plane_url, mint_test_token)
    await o.call(
        "knowledge.add",
        {"namespace": o.ns, "source_uri": "kb://parked", "content": "a parked document"},
    )
    head = await pg.fetchrow(
        "SELECT version, committed_at FROM loom.knowledge_heads "
        "WHERE org = $1 AND namespace = $2 AND source_uri = 'kb://parked'",
        o.org,
        o.ns,
    )

    async def park() -> None:
        # The head's delivery, parked out of attempts (written again if it was published).
        await pg.execute(
            "INSERT INTO loom.knowledge_deliveries AS d (org, namespace, source_uri, version, "
            "user_id, row, targets, lease_until, fenced_until, attempts, last_error) "
            "SELECT org, namespace, source_uri, version, user_id, row, 1, 'infinity', now(), 99, "
            "'ClickHouse refused' FROM loom.knowledge_heads "
            "WHERE org = $1 AND namespace = $2 AND source_uri = 'kb://parked' "
            "ON CONFLICT (org, namespace, source_uri, version) DO UPDATE SET attempts = 99, "
            "lease_until = 'infinity', fenced_until = now(), last_error = 'ClickHouse refused'",
            o.org,
            o.ns,
        )

    async def read() -> Any:
        return await pg.fetchrow(
            "SELECT attempts, last_error, lease_until = 'infinity' AS parked "
            "FROM loom.knowledge_deliveries WHERE org = $1 AND namespace = $2 "
            "AND source_uri = 'kb://parked' AND version = $3",
            o.org,
            o.ns,
            head["version"],
        )

    try:
        for hold in (
            lambda: pg_documents.hold_for_republish(head["committed_at"] - timedelta(seconds=1)),
            lambda: pg_documents.hold_heads([(o.org, o.ns, "kb://parked")]),
        ):
            await park()
            await hold()
            got = await read()
            assert got["attempts"] == 0
            assert got["last_error"] == pg_lease.HELD_BY_RECONCILE and got["parked"]
    finally:
        await pg_documents.release_held()


async def test_forget_deletes_only_an_open_ledger_row(stores: Any) -> None:
    """A request whose first checkpoint failed leaves no open row; one that ended is kept."""
    from server.core import pg
    from server.erasure.store import pg_ledger

    org = unique_org("forget")
    now = datetime.now(UTC).replace(tzinfo=None)
    ids = [str(uuid.uuid4()) for _ in range(2)]
    for i in ids:
        await pg_ledger.record(
            {"org": org, "id": i, "scope_type": "user_id", "scope_value": "u", "requested_at": now}
        )
    await pg_ledger.complete(org, ids[1], now)
    for i in ids:
        await pg_ledger.forget(org, i)
    left = {
        str(r["id"])
        for r in await pg.fetch("SELECT id FROM loom.erasure_ledger WHERE org = $1", org)
    }
    assert left == {ids[1]}
    await pg.execute("DELETE FROM loom.erasure_ledger WHERE org = $1", org)
