"""run.begin holds a run id's Keeper claim only while it writes the run row.

Order: claim, read the row, write the row, drop the claim. Two begins by
different users on one caller-supplied id are run here under every schedule
of their ClickHouse calls: exactly one keeps the id, each row keeps its own
owner, and no claim is left. Also: a server-made id is never claimed; with
no Keeper (or a Keeper error) a narrowed caller keeps a supplied id only for
its own existing run, a namespace-wide one falls back to the row check; and
the boot probe fills the health flag. CH-free.
"""

from __future__ import annotations

import asyncio
import contextvars
import itertools
import logging
import random
from typing import Any

import pytest

from server.auth.context import set_current_auth
from server.auth.jwt import AuthInfo
from server.identity.domain.read_scope import bind_request_read_filters, resolve_read_filters
from server.observability import degradations, provider_health
from server.tasks.api.models_run import RunBeginInput
from server.tasks.app import run as run_app
from server.tasks.store import runs as runs_store

_who: contextvars.ContextVar[str] = contextvars.ContextVar("who", default="")


class _Schedule:
    """Each ClickHouse call waits for its begin's turn in `order`; a turn of a
    begin that has finished is skipped, and past the end calls run freely."""

    def __init__(self, order: str) -> None:
        self.order = list(order)
        self.done: set[str] = set()
        self.cond = asyncio.Condition()

    def _head(self) -> str:
        while self.order and self.order[0] in self.done:
            self.order.pop(0)
        return self.order[0] if self.order else ""

    async def turn(self) -> None:
        me = _who.get()
        async with self.cond:
            await self.cond.wait_for(lambda: self._head() in ("", me))
            if self.order:
                self.order.pop(0)
            self.cond.notify_all()

    async def finish(self, me: str) -> None:
        async with self.cond:
            self.done.add(me)
            self.cond.notify_all()


class _KeeperCH:
    """agent_run_claims as a strict KeeperMap; every call takes a turn."""

    def __init__(self, sched: _Schedule) -> None:
        self.sched = sched
        self.claims: dict[str, dict[str, str]] = {}
        self.inserts = 0

    async def query_one(self, sql: str, params: Any = None) -> Any:
        if "system.tables" in sql:
            return {"engine": "KeeperMap"}
        await self.sched.turn()
        return self.claims.get(params["k"])

    async def execute(self, sql: str, params: Any = None) -> None:
        await self.sched.turn()
        if "DELETE" in sql:
            self.claims.pop(params["k"], None)
            return
        self.inserts += 1
        if params["k"] in self.claims:
            raise RuntimeError("Code: 999. Coordination::Exception: Node exists")
        self.claims[params["k"]] = {"user_id": params["uid"], "session_id": params["sid"]}


def _pinned(user: str) -> AuthInfo:
    return AuthInfo(
        subject="sa:bot",
        org="o",
        namespaces=("n",),
        roles=("writer",),
        is_service_account=True,
        read_user_id=user,
    )


def _bind(auth: AuthInfo) -> None:
    set_current_auth(auth)
    bind_request_read_filters(
        auth, resolve_read_filters(requested=None, user_id=None, session_id=None, auth=auth)
    )


def _input(user: str, run_id: str = "shared") -> RunBeginInput:
    return RunBeginInput(
        org="o", namespace="n", agent="bot", user_id=user, run_id=run_id, goal=user
    )


@pytest.fixture
def ledger(monkeypatch):
    """The run rows, with fetch and insert taking turns like the claim calls."""
    rows: dict[str, dict[str, Any]] = {}
    state: dict[str, Any] = {}

    async def fetch_run(ch, org, ns, run_id):
        await ch.sched.turn()
        return rows.get(run_id)

    async def insert_runs(ch, new):
        await ch.sched.turn()
        rows.update({r["run_id"]: dict(r) for r in new})

    monkeypatch.setattr(run_app, "get_client", lambda: state["ch"])
    monkeypatch.setattr(runs_store, "_claims_atomic", None)
    monkeypatch.setattr(runs_store, "fetch_run", fetch_run)
    monkeypatch.setattr(runs_store, "insert_runs", insert_runs)
    return rows, state


async def _begin(sched: _Schedule, me: str, user: str) -> str:
    _who.set(me)
    _bind(_pinned(user))
    try:
        return (await run_app.handle_run_begin(_input(user))).run_id
    finally:
        await sched.finish(me)


# Each begin makes at most four turns (claim, read, write, drop; a lost claim
# makes claim, read-claim, read, write), so eight turns cover every interleaving.
_SCHEDULES = ["".join(p) for p in itertools.product("AB", repeat=8)]


def test_two_users_first_begins_never_share_an_id_under_any_schedule(ledger) -> None:
    rows, state = ledger
    for order in _SCHEDULES:
        rows.clear()
        runs_store._claims_atomic = None

        async def both(order: str = order) -> list[str]:
            sched = _Schedule(order)
            state["ch"] = ch = _KeeperCH(sched)
            ids = list(await asyncio.gather(_begin(sched, "A", "U"), _begin(sched, "B", "V")))
            assert ch.claims == {}, (order, ch.claims)
            return ids

        ids = contextvars.Context().run(lambda: asyncio.run(both()))
        assert ids.count("shared") == 1 and len(set(ids)) == 2, (order, ids)
        for run_id, user in zip(ids, ("U", "V"), strict=True):
            assert rows[run_id]["user_id"] == user, (order, run_id)


def test_two_begins_by_one_user_share_the_run_under_any_schedule(ledger) -> None:
    """A hook retry: the second begin re-opens the first's run, never a second run,
    whether it finds the claim held, released, or the row already written."""
    rows, state = ledger
    for order in _SCHEDULES:
        rows.clear()
        runs_store._claims_atomic = None

        async def both(order: str = order) -> list[str]:
            sched = _Schedule(order)
            state["ch"] = ch = _KeeperCH(sched)
            ids = list(await asyncio.gather(_begin(sched, "A", "U"), _begin(sched, "B", "U")))
            assert ch.claims == {}, (order, ch.claims)
            return ids

        ids = contextvars.Context().run(lambda: asyncio.run(both()))
        assert ids == ["shared", "shared"], (order, ids)
        assert rows["shared"]["user_id"] == "U", order


def test_a_stale_foreign_claim_does_not_fork_the_owners_retry(ledger) -> None:
    """V's crashed begin left its claim on U's id: U's re-begin still re-opens U's run,
    and V's next begin still goes to a fresh id."""
    rows, state = ledger

    async def go() -> tuple[str, str, str]:
        sched = _Schedule("")
        state["ch"] = ch = _KeeperCH(sched)
        first = await _begin(sched, "A", "U")
        ch.claims['["o", "n", "shared"]'] = {"user_id": "V", "session_id": "-"}
        retry = await _begin(sched, "B", "U")
        other = await _begin(sched, "C", "V")
        return first, retry, other

    first, retry, other = contextvars.Context().run(lambda: asyncio.run(go()))
    assert first == retry == "shared" and other != "shared"
    assert rows["shared"]["user_id"] == "U"


def test_a_finished_begin_leaves_no_claim_and_its_row_refuses_another_user(ledger) -> None:
    rows, state = ledger

    async def one_then_other() -> list[str]:
        sched = _Schedule("")
        state["ch"] = ch = _KeeperCH(sched)
        first = await _begin(sched, "A", "U")
        assert ch.claims == {}
        second = await _begin(sched, "B", "V")
        assert ch.claims == {}
        return [first, second]

    first, second = contextvars.Context().run(lambda: asyncio.run(one_then_other()))
    assert first == "shared" and second != "shared"
    assert rows["shared"]["user_id"] == "U"


def test_a_server_made_or_fresh_id_is_never_claimed(ledger) -> None:
    rows, state = ledger

    async def go() -> tuple[int, str]:
        sched = _Schedule("")
        state["ch"] = ch = _KeeperCH(sched)
        _bind(_pinned("U"))
        inp = RunBeginInput(org="o", namespace="n", agent="bot", user_id="U", goal="g")
        await run_app.handle_run_begin(inp)
        assert ch.inserts == 0, "a server-made id is not claimed"
        # V's claim on "shared" is held: U gets a fresh id and claims nothing more.
        ch.claims['["o", "n", "shared"]'] = {"user_id": "V", "session_id": "-"}
        out = await run_app.handle_run_begin(_input("U"))
        return ch.inserts, out.run_id

    inserts, run_id = contextvars.Context().run(lambda: asyncio.run(go()))
    assert inserts == 1 and run_id != "shared"


def _three_begins_with_a_failed_insert(order: str, rows: dict, state: dict) -> dict[str, str]:
    """A (user U) holds the claim and its row insert fails; C is U again, B is V."""

    async def go() -> dict[str, str]:
        sched = _Schedule(order)
        state["ch"] = _KeeperCH(sched)

        async def safe(me: str, user: str) -> tuple[str, str]:
            try:
                return me, await _begin(sched, me, user)
            except RuntimeError:
                return me, ""

        done = await asyncio.gather(safe("A", "U"), safe("B", "V"), safe("C", "U"))
        return dict(done)

    return contextvars.Context().run(lambda: asyncio.run(go()))


def test_a_failed_begin_keeps_its_claim_so_no_other_user_takes_the_id(ledger, monkeypatch) -> None:
    rows, state = ledger
    real = runs_store.insert_runs

    async def insert_runs(ch, new):
        if _who.get() == "A":
            await ch.sched.turn()
            raise RuntimeError("insert failed")
        await real(ch, new)

    monkeypatch.setattr(runs_store, "insert_runs", insert_runs)
    users = {"A": "U", "B": "V", "C": "U"}
    rng = random.Random(25)
    orders = ["ACCAAABBCBCB"] + ["".join(rng.choice("ABC") for _ in range(12)) for _ in range(3000)]
    for order in orders:
        rows.clear()
        runs_store._claims_atomic = None
        ids = _three_begins_with_a_failed_insert(order, rows, state)
        on_shared = {users[me] for me, run_id in ids.items() if run_id == "shared"}
        assert len(on_shared) <= 1, (order, ids)
        if on_shared:
            assert rows["shared"]["user_id"] in on_shared, (order, ids, rows["shared"])


class _BrokenKeeperCH(_KeeperCH):
    async def execute(self, sql: str, params: Any = None) -> None:
        await self.sched.turn()
        raise RuntimeError("Code: 999. Coordination::Exception: Session expired")


class _NoClaimsCH(_KeeperCH):
    """A server without Keeper: no claims table, so nothing is ever claimed."""

    async def query_one(self, sql: str, params: Any = None) -> Any:
        assert "system.tables" in sql
        return None

    async def execute(self, sql: str, params: Any = None) -> None:
        raise AssertionError("no claims table to write")


_WIDE = AuthInfo(
    subject="sa:bot", org="o", namespaces=("n",), roles=("writer",), is_service_account=True
)


def test_a_keeper_error_falls_back_to_the_row_check(ledger, monkeypatch, caplog) -> None:
    """A namespace-wide caller keeps the supplied id, as before Keeper claims."""
    rows, state = ledger
    monkeypatch.setattr(runs_store, "_keeper_errors_logged", set())
    provider_health.reset()

    async def go() -> tuple[list[str], list[str]]:
        state["ch"] = _BrokenKeeperCH(_Schedule(""))
        degradations.begin()
        _bind(_WIDE)
        ids = [(await run_app.handle_run_begin(_input("U"))).run_id for _ in range(2)]
        return ids, degradations.current()

    with caplog.at_level(logging.WARNING, logger=runs_store.__name__):
        ids, notes = contextvars.Context().run(lambda: asyncio.run(go()))
    assert ids == ["shared", "shared"] and rows["shared"]["user_id"] == "U"
    assert notes and all(n.startswith("run_claims: row_check_only") for n in notes)
    assert len([r for r in caplog.records if "Keeper claim failed" in r.message]) == 1
    assert "run_claims" not in provider_health.status().get("providers", {})


_UNCLAIMED = [
    pytest.param(_BrokenKeeperCH, id="keeper-error"),
    pytest.param(_NoClaimsCH, id="no-keeper"),
]


@pytest.mark.parametrize("ch_type", _UNCLAIMED)
def test_without_claims_two_narrowed_users_never_share_an_id(ledger, ch_type) -> None:
    rows, state = ledger
    for order in _SCHEDULES:
        rows.clear()
        runs_store._claims_atomic = None

        async def both(order: str = order) -> list[str]:
            sched = _Schedule(order)
            state["ch"] = ch_type(sched)
            return list(await asyncio.gather(_begin(sched, "A", "U"), _begin(sched, "B", "V")))

        ids = contextvars.Context().run(lambda: asyncio.run(both()))
        assert "shared" not in ids and len(set(ids)) == 2, (order, ids)
        for run_id, user in zip(ids, ("U", "V"), strict=True):
            assert rows[run_id]["user_id"] == user, (order, run_id)


@pytest.mark.parametrize("ch_type", _UNCLAIMED)
def test_without_claims_a_narrowed_user_reopens_its_own_run(ledger, ch_type) -> None:
    """U's run already exists: U's re-begin keeps the id, V's racing begin does not."""
    rows, state = ledger
    for order in _SCHEDULES:
        rows.clear()
        rows["shared"] = {"run_id": "shared", "user_id": "U", "session_id": "-"}
        runs_store._claims_atomic = None

        async def both(order: str = order) -> list[str]:
            sched = _Schedule(order)
            state["ch"] = ch_type(sched)
            return list(await asyncio.gather(_begin(sched, "A", "U"), _begin(sched, "B", "V")))

        ids = contextvars.Context().run(lambda: asyncio.run(both()))
        assert ids[0] == "shared" and ids[1] != "shared", (order, ids)
        assert rows["shared"]["user_id"] == "U" and rows[ids[1]]["user_id"] == "V", order


@pytest.mark.parametrize("ch_type", _UNCLAIMED)
def test_without_claims_a_namespace_wide_caller_keeps_the_supplied_id(ledger, ch_type) -> None:
    rows, state = ledger

    async def go() -> str:
        state["ch"] = ch_type(_Schedule(""))
        _bind(_WIDE)
        return (await run_app.handle_run_begin(_input("U"))).run_id

    assert contextvars.Context().run(lambda: asyncio.run(go())) == "shared"
    assert rows["shared"]["user_id"] == "U"


@pytest.mark.parametrize(("auth", "user"), [(_pinned("U"), "U"), (_WIDE, "-")])
def test_an_unreadable_held_claim_is_another_users(ledger, auth: AuthInfo, user: str) -> None:
    """Whoever calls, and even with no user of its own ("-"), a held claim it cannot
    read sends the begin to a fresh id."""
    _, state = ledger

    class _Unreadable(_KeeperCH):
        async def query_one(self, sql: str, params: Any = None) -> Any:
            if "system.tables" in sql:
                return {"engine": "KeeperMap"}
            raise RuntimeError("Keeper read failed")

    async def go() -> str:
        state["ch"] = ch = _Unreadable(_Schedule(""))
        ch.claims['["o", "n", "shared"]'] = {"user_id": "V", "session_id": "-"}
        _bind(auth)
        return (await run_app.handle_run_begin(_input(user))).run_id

    assert contextvars.Context().run(lambda: asyncio.run(go())) != "shared"


@pytest.mark.parametrize(("engine", "atomic"), [("KeeperMap", True), (None, False)])
def test_the_boot_probe_sets_the_health_flag(monkeypatch, engine, atomic) -> None:
    monkeypatch.setattr(runs_store, "_claims_atomic", None)

    class _CH:
        async def query_one(self, sql: str, params: Any = None) -> Any:
            assert "system.tables" in sql
            return {"engine": engine} if engine else None

    asyncio.run(runs_store.probe_claims(_CH()))
    assert runs_store.claims_atomic() is atomic


def test_a_failed_boot_probe_leaves_it_to_the_first_begin(monkeypatch) -> None:
    monkeypatch.setattr(runs_store, "_claims_atomic", None)

    class _CH:
        async def query_one(self, sql: str, params: Any = None) -> Any:
            raise RuntimeError("connection refused")

    asyncio.run(runs_store.probe_claims(_CH()))
    assert runs_store.claims_atomic() is None


@pytest.mark.parametrize("table", ["agent_run_claims", "oauth_claims", "grant_claims"])
@pytest.mark.parametrize(
    ("rows", "atomic"), [([{"engine": "KeeperMap"}], True), ([], False), (None, None)]
)
def test_the_doctor_reads_the_same_claims_check(monkeypatch, rows, atomic, table) -> None:
    from server.auth import grant_claims, oauth_claims
    from server.cli import doctor
    from server.core.schema import migrate_lease
    from server.settings import settings

    table_sql = {
        "agent_run_claims": migrate_lease.RUN_CLAIMS_TABLE_SQL,
        "oauth_claims": oauth_claims.CLAIMS.table_sql,
        "grant_claims": grant_claims.CLAIMS.table_sql,
    }[table]

    def query(_settings, sql, params):
        assert f"'{table}'" in sql
        if rows is None:
            raise RuntimeError("connection refused")
        return rows

    monkeypatch.setattr(doctor, "_doctor_ch_query", query)
    assert doctor._doctor_keeper_table(settings, table_sql) is atomic


@pytest.mark.parametrize("oauth", [True, False])
def test_the_doctor_reports_unclaimed_oauth_exchanges_only_with_oauth_on(
    monkeypatch, oauth: bool
) -> None:
    from server.cli import doctor
    from server.settings import settings

    asked: list[str] = []

    def query(_settings, sql, params):
        asked.append(sql)
        return [] if "'oauth_claims'" in sql else [{"engine": "KeeperMap"}]

    monkeypatch.setattr(settings, "oauth_enabled", oauth)
    monkeypatch.setattr(doctor, "_doctor_ch_query", query)
    monkeypatch.setattr(doctor, "_doctor_reader_probe", lambda _s: ("ok", ""))
    codes = [p.code for p in doctor._doctor_problems()]
    assert ("oauth_claims_not_atomic" in codes) is oauth
    assert any("'oauth_claims'" in sql for sql in asked) is oauth


def test_the_doctor_reports_grant_changes_without_a_keeper(monkeypatch) -> None:
    from server.cli import doctor

    def _missing(_settings, table_sql):
        return "'grant_claims'" not in table_sql

    monkeypatch.setattr(doctor, "_doctor_keeper_table", _missing)
    assert "grant_changes_not_atomic" in [p.code for p in doctor._doctor_problems()]
