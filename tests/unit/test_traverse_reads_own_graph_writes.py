"""A walk sees the graph its caller just wrote.

`memory.set` projects its entity edges beside the response. `facts.traverse` remembers a
walk for a minute. Together, a client that wrote and then walked could otherwise be
handed, for a whole minute, the walk read before its edges landed. The walk waits for
its namespace's projections in flight, never serves a walk read before the namespace's
last graph write, reads with sequential consistency inside the replication window after
one, and remembers no walk read inside that window. Both live in `loom.graph_projections`,
stamped by the server's clock, so a write on one process retires a walk another
remembered whatever the hosts' clocks say.
"""

from __future__ import annotations

import asyncio
import time
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest

from server.core import ch as ch_mod
from server.memory.api.models_facts import FactsTraverseInput, FactsTraverseOutput
from server.memory.app import after_write, recent_writes
from server.memory.app import facts as facts_handler
from server.memory.app import graph_write_marks as gwm
from server.memory.store import graph_projections as gp
from server.settings import settings


class _Server:
    """loom.graph_projections as every process sees it, on the server's clock
    (`skew_s` behind the hosts' clocks: no host clock is ever compared to it)."""

    def __init__(self) -> None:
        self.skew_s = 0.0
        self.busy: dict[str, datetime] = {}
        self.written: datetime | None = None
        self.log: list[str] = []
        self.reads = 0
        self.fail_reads = self.fail_begin = self.fail_end = False
        self.fail_writes = 0
        self.write_delay_s = 0.0
        self._n = 0

    def now(self) -> datetime:
        return datetime.now(UTC) - timedelta(seconds=self.skew_s)

    async def begin(self, org: str, namespace: str, *, stale_s: float, **_: Any) -> str:
        self._n += 1
        token = f"{namespace}/{self._n}"
        if self.fail_begin:
            raise gp.BeginUncertain(token, TimeoutError("begin timed out"))
        self.busy[token] = self.now()
        return token

    async def end(self, org: str, namespace: str, token: str, **_: Any) -> None:
        if self.fail_end:
            raise ConnectionError("end lost")
        self.busy.pop(token, None)
        self.log.append("end")

    async def note_writes(self, keys: list[tuple[str, str]], **_: Any) -> None:
        await asyncio.sleep(self.write_delay_s)
        if self.fail_writes:
            self.fail_writes -= 1
            raise ConnectionError("write lost")
        self.log.append("write")
        self.written = self.now()

    async def snapshot(self, org: str, namespace: str, *, stale_s: float, **_: Any) -> gp.Snapshot:
        self.reads += 1
        if self.fail_reads:
            raise ConnectionError("ClickHouse unreachable")
        now = self.now()
        busy = {
            t
            for t, at in self.busy.items()
            if t.startswith(f"{namespace}/") and (now - at).total_seconds() < stale_s
        }
        return gp.Snapshot(now=now, busy=frozenset(busy), written=self.written)

    def another_process(self, *, ns: str = "n", ago_s: float = 0.0) -> str:
        """A projection another process began; returns its token."""
        self._n += 1
        token = f"{ns}/{self._n}"
        self.busy[token] = self.now() - timedelta(seconds=ago_s)
        return token

    def another_process_writes(self) -> None:
        time.sleep(0.002)  # a later millisecond than the walk before it
        self.written = self.now()


@pytest.fixture(autouse=True)
def server(monkeypatch: pytest.MonkeyPatch):
    s = _Server()
    for name in ("begin", "end", "note_writes", "snapshot"):
        monkeypatch.setattr(gp, name, getattr(s, name))
    gwm._reset_for_tests()
    monkeypatch.setattr(facts_handler, "_memo", facts_handler._WalkMemo())
    yield s
    gwm._reset_for_tests()


def _walk_counter(monkeypatch: pytest.MonkeyPatch, server: _Server) -> dict[str, Any]:
    state: dict[str, Any] = {"walks": 0, "sequential": [], "graph": ["alice"], "read_at": []}

    async def fake_walk(inp: FactsTraverseInput) -> FactsTraverseOutput:
        state["walks"] += 1
        state["sequential"].append(ch_mod.sequential_reads_active())
        state["read_at"].append(server.now())
        return FactsTraverseOutput(
            seed=inp.seed_entity, nodes=[], truncated=len(state["graph"]) > 1
        )

    monkeypatch.setattr(facts_handler, "_handle_facts_traverse_uncached", fake_walk)
    return state


def _walk(ns: str = "n") -> FactsTraverseInput:
    return FactsTraverseInput(org="o", namespace=ns, seed_entity="alice", max_hops=2)


@pytest.fixture
def no_window(monkeypatch: pytest.MonkeyPatch) -> None:
    from server.settings import settings

    monkeypatch.setattr(settings, "ch_read_your_writes_window_s", 0.0)


@pytest.mark.asyncio
@pytest.mark.usefixtures("no_window")
async def test_a_graph_write_retires_the_walk_remembered_before_it(
    monkeypatch: pytest.MonkeyPatch, server: _Server
) -> None:
    state = _walk_counter(monkeypatch, server)
    await facts_handler.handle_facts_traverse(_walk())
    await facts_handler.handle_facts_traverse(_walk())
    assert state["walks"] == 1  # remembered

    gwm.note_write("o", "n")  # not recorded yet: this process counts it at once
    await facts_handler.handle_facts_traverse(_walk())
    assert state["walks"] == 2
    assert await gwm.flush_now() and server.log == ["write"]
    await asyncio.sleep(0.002)
    await facts_handler.handle_facts_traverse(_walk())
    assert state["walks"] == 3  # read before the recorded write: not served
    await facts_handler.handle_facts_traverse(_walk())
    assert state["walks"] == 3  # read after it: remembered again


@pytest.mark.asyncio
@pytest.mark.usefixtures("no_window")
async def test_another_processs_write_retires_the_memo_whatever_this_hosts_clock_says(
    monkeypatch: pytest.MonkeyPatch, server: _Server
) -> None:
    """The memo's stamp is the server's clock, read before the walk is: a server
    clock a minute behind this host still retires the walk."""
    server.skew_s = 60.0
    state = _walk_counter(monkeypatch, server)
    await facts_handler.handle_facts_traverse(_walk())
    (_, stamp, _) = next(iter(facts_handler._memo.cache.values()))
    assert stamp <= state["read_at"][0]  # stamped before the graph was read
    server.another_process_writes()
    await facts_handler.handle_facts_traverse(_walk())
    assert state["walks"] == 2


@pytest.mark.asyncio
async def test_inside_the_window_a_walk_reads_sequentially_and_is_not_remembered(
    monkeypatch: pytest.MonkeyPatch, server: _Server
) -> None:
    from server.settings import settings

    monkeypatch.setattr(settings, "ch_read_your_writes_window_s", 20.0)
    monkeypatch.setattr(settings, "ch_read_your_writes", True)
    state = _walk_counter(monkeypatch, server)
    server.another_process_writes()
    await facts_handler.handle_facts_traverse(_walk())
    await facts_handler.handle_facts_traverse(_walk())
    assert state["walks"] == 2 and state["sequential"] == [True, True]

    server.written = server.now() - timedelta(seconds=60)  # outside the window
    await facts_handler.handle_facts_traverse(_walk())
    await facts_handler.handle_facts_traverse(_walk())
    assert state["walks"] == 3 and state["sequential"][-1] is False


@pytest.mark.asyncio
async def test_a_walk_waits_for_its_namespaces_projection_in_this_process(
    monkeypatch: pytest.MonkeyPatch, server: _Server
) -> None:
    state = _walk_counter(monkeypatch, server)

    async def projection() -> None:
        await asyncio.sleep(0.05)
        state["graph"].append("bob")

    after_write.schedule(projection(), name="t-projection", scope=("o", "n"))
    out = await facts_handler.handle_facts_traverse(_walk())
    assert out.truncated is True


@pytest.mark.asyncio
async def test_a_projection_is_in_flight_from_before_the_write_answers_until_it_ends(
    server: _Server,
) -> None:
    """Recorded before `track` returns (the write answers after it); at its end the
    namespace's write is recorded and then the projection is cleared."""
    landed = asyncio.Event()

    async def projection() -> None:
        await landed.wait()

    task = after_write.schedule(projection(), name="t-projection", scope=("o", "n"))
    await gwm.track("o", "n", task)
    assert len(server.busy) == 1
    landed.set()
    await task
    await gwm.drain(timeout_s=2.0)
    assert server.busy == {} and server.log == ["write", "end"]


@pytest.mark.asyncio
async def test_a_projections_own_inserts_are_recorded_once_by_its_end(server: _Server) -> None:
    async def projection() -> None:
        recent_writes.observe_insert("loom.entity_edges", [{"org": "o", "namespace": "n"}])
        await asyncio.sleep(0.1)  # past the flush delay

    task = after_write.schedule(projection(), name="t-projection", scope=("o", "n"))
    await gwm.track("o", "n", task)
    await task
    await gwm.drain(timeout_s=2.0)
    assert server.log == ["write", "end"]
    recent_writes.observe_insert("loom.entity_edges", [{"org": "o", "namespace": "n"}])
    await gwm.drain(timeout_s=2.0)
    assert server.log == ["write", "end", "write"], "a write after the projection is recorded"


@pytest.mark.asyncio
async def test_a_walk_that_sees_a_projection_ended_sees_its_write(server: _Server) -> None:
    server.write_delay_s = 0.05
    await gwm.track("o", "n", None)
    await asyncio.sleep(0.02)
    assert server.busy and server.log == []  # not cleared before its write lands
    await gwm.drain(timeout_s=2.0)
    wrote, at = await gwm.last_write("o", "n", wait_s=0.0)
    assert not server.busy and wrote is not None and wrote <= at


@pytest.mark.asyncio
async def test_an_end_whose_write_fails_leaves_its_projection_in_flight(
    server: _Server, caplog: pytest.LogCaptureFixture
) -> None:
    server.fail_writes = 1
    await gwm.track("o", "n", None)
    await gwm.drain(timeout_s=2.0)
    assert len(server.busy) == 1 and "end" not in server.log
    assert "graph projection end not written" in caplog.text
    await asyncio.sleep(0.1)
    assert server.log == ["write"], "the flush records the write the end lost"


@pytest.mark.asyncio
async def test_an_uncertain_begin_is_still_ended_and_a_failed_end_is_logged(
    server: _Server, caplog: pytest.LogCaptureFixture
) -> None:
    server.fail_begin = True
    await gwm.track("o", "n", None)  # never raises
    await gwm.drain(timeout_s=2.0)
    assert server.log == ["write", "end"]

    server.fail_begin, server.fail_end = False, True
    await gwm.track("o", "n", None)
    await gwm.drain(timeout_s=2.0)
    assert "graph projection end not written" in caplog.text
    assert all(t.done() and t.exception() is None for t in gwm._ends)


@pytest.mark.asyncio
async def test_a_begin_that_recorded_nothing_still_notes_the_write(
    server: _Server, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def nothing(*_: Any, **__: Any) -> None:
        return None

    monkeypatch.setattr(gp, "begin", nothing)  # every slot held: no walk elsewhere waits on it
    await gwm.track("o", "n", None)
    assert server.log == ["write"], "a write before the answer"
    await gwm.drain(timeout_s=2.0)
    assert server.log == ["write", "write"], "and again at its end"


@pytest.mark.asyncio
async def test_a_walk_does_not_wait_on_projections_that_began_after_it(server: _Server) -> None:
    """Under steady writes every walk would otherwise wait its whole bound."""
    earlier = server.another_process()

    async def steady_writes() -> None:
        for _ in range(20):
            await asyncio.sleep(0.02)
            server.another_process()  # began after the walk, never ended

    async def earlier_lands() -> None:
        await asyncio.sleep(0.15)
        del server.busy[earlier]

    writers = asyncio.gather(steady_writes(), earlier_lands())
    t0 = time.monotonic()
    await gwm.last_write("o", "n", wait_s=2.0)
    took = time.monotonic() - t0
    await writers
    assert 0.15 <= took < 0.6
    assert server.reads <= 4  # one read, then one per poll


@pytest.mark.asyncio
@pytest.mark.usefixtures("no_window")
async def test_a_projection_still_in_flight_after_the_wait_retires_the_memo(
    monkeypatch: pytest.MonkeyPatch, server: _Server
) -> None:
    monkeypatch.setattr(settings, "memory_facts_projection_wait_s", 0.15)
    state = _walk_counter(monkeypatch, server)
    await facts_handler.handle_facts_traverse(_walk())
    await facts_handler.handle_facts_traverse(_walk())
    assert state["walks"] == 1
    server.another_process()  # its edges are still landing when the wait ends
    await facts_handler.handle_facts_traverse(_walk())
    assert state["walks"] == 2  # never served from a memo read before it lands


@pytest.mark.asyncio
async def test_a_projection_waited_out_once_is_not_waited_on_again(server: _Server) -> None:
    """A process killed mid-projection leaves its record for STALE_PENDING_S: one
    walk waits it out, the next walks of this process do not."""
    server.another_process()
    t0 = time.monotonic()
    wrote, at = await gwm.last_write("o", "n", wait_s=0.2)
    assert time.monotonic() - t0 >= 0.2 and wrote == at  # counted as a write now
    t0 = time.monotonic()
    wrote, at = await gwm.last_write("o", "n", wait_s=0.2)
    assert time.monotonic() - t0 < 0.1
    assert wrote == at, "still a write while it is recorded"


@pytest.mark.asyncio
@pytest.mark.usefixtures("no_window")
async def test_no_walk_is_remembered_while_a_waited_out_projection_is_recorded(
    monkeypatch: pytest.MonkeyPatch, server: _Server
) -> None:
    monkeypatch.setattr(settings, "memory_facts_projection_wait_s", 0.1)
    state = _walk_counter(monkeypatch, server)
    server.another_process()  # its process died mid-projection
    for _ in range(3):
        await facts_handler.handle_facts_traverse(_walk())
    assert state["walks"] == 3


@pytest.mark.asyncio
async def test_overlapping_projections_keep_the_namespace_busy_until_both_end(
    server: _Server,
) -> None:
    slow = server.another_process()
    quick = server.another_process()
    del server.busy[quick]
    t0 = time.monotonic()
    await gwm.last_write("o", "n", wait_s=0.2)
    assert time.monotonic() - t0 >= 0.2  # still waited on the slow one
    gwm._reset_for_tests()
    del server.busy[slow]
    t0 = time.monotonic()
    await gwm.last_write("o", "n", wait_s=2.0)
    assert time.monotonic() - t0 < 0.1


@pytest.mark.asyncio
async def test_a_walk_waits_for_another_processs_projection_in_flight(
    monkeypatch: pytest.MonkeyPatch, server: _Server
) -> None:
    state = _walk_counter(monkeypatch, server)
    token = server.another_process()

    async def its_projection_lands() -> None:
        await asyncio.sleep(0.15)
        state["graph"].append("bob")
        server.another_process_writes()
        del server.busy[token]

    landing = asyncio.create_task(its_projection_lands())
    out = await facts_handler.handle_facts_traverse(_walk())
    await landing
    assert out.truncated is True


@pytest.mark.asyncio
async def test_the_wait_for_another_processs_projection_is_bounded(
    monkeypatch: pytest.MonkeyPatch, server: _Server
) -> None:
    state = _walk_counter(monkeypatch, server)
    monkeypatch.setattr(settings, "memory_facts_projection_wait_s", 0.3)
    server.another_process()  # never ends
    t0 = time.monotonic()
    await facts_handler.handle_facts_traverse(_walk())
    assert 0.25 <= time.monotonic() - t0 < 1.5 and state["walks"] == 1


@pytest.mark.asyncio
async def test_unreadable_marks_walk_uncached_and_never_fail(
    monkeypatch: pytest.MonkeyPatch, server: _Server
) -> None:
    state = _walk_counter(monkeypatch, server)
    server.fail_reads = True
    await facts_handler.handle_facts_traverse(_walk())
    await facts_handler.handle_facts_traverse(_walk())
    assert state["walks"] == 2 and facts_handler._memo.cache == {}


# ---- recording the writes ------------------------------------------------------------


@pytest.mark.asyncio
async def test_one_flush_at_a_time_and_writes_meanwhile_go_with_the_next(
    server: _Server, monkeypatch: pytest.MonkeyPatch
) -> None:
    server.write_delay_s = 0.1
    calls: list[list[tuple[str, str]]] = []
    real = server.note_writes

    async def note_writes(keys: list[tuple[str, str]], **kw: Any) -> None:
        calls.append(list(keys))
        await real(keys, **kw)

    monkeypatch.setattr(gp, "note_writes", note_writes)
    gwm.note_write("o", "a")
    await asyncio.sleep(0.05)  # its flush is in flight
    gwm.note_write("o", "b")
    gwm.note_write("o", "c")
    await asyncio.sleep(0.3)
    assert calls == [[("o", "a")], [("o", "b"), ("o", "c")]]
    assert gwm._unrecorded == set()


@pytest.mark.asyncio
async def test_a_failed_flush_is_retried(server: _Server, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(gwm, "_RETRY_S", 0.05)
    server.fail_writes = 1
    gwm.note_write("o", "n")
    await asyncio.sleep(0.2)
    assert server.log == ["write"] and gwm._unrecorded == set()


@pytest.mark.asyncio
async def test_the_shutdown_drain_records_the_writes_and_lands_a_pending_end(
    server: _Server,
) -> None:
    server.write_delay_s = 0.1
    await gwm.track("o", "n", None)
    gwm.note_write("o", "other")
    assert server.log == []
    assert await gwm.drain(timeout_s=2.0) >= 1
    assert server.log.count("write") == 2 and "end" in server.log


@pytest.mark.asyncio
async def test_settle_is_bounded() -> None:
    async def stuck() -> None:
        await asyncio.sleep(5)

    task = after_write.schedule(stuck(), name="t-stuck", scope=("o", "n"))
    assert task is not None
    try:
        assert await asyncio.wait_for(after_write.settle("o", "n", timeout_s=0.05), 1.0) == 1
    finally:
        task.cancel()


def test_graph_table_inserts_mark_the_namespace_and_memory_tables_do_not() -> None:
    rows = [{"org": "o", "namespace": "n", "src": "a", "dst": "b"}]
    assert recent_writes.namespaces_of_rows("loom.entity_edges", rows, graph=True) == {("o", "n")}
    assert recent_writes.namespaces_of_rows("loom.entity_triples", rows, graph=True) == {("o", "n")}
    assert recent_writes.namespaces_of_rows("loom.memory", rows, graph=True) == set()
    assert recent_writes.namespaces_of_rows("loom.entity_edges", rows) == set()


@pytest.mark.asyncio
async def test_edges_wait_for_their_flush_unless_told_not_to() -> None:
    """A graph write mark follows the rows it answers for: `bulk_set` (the default)
    waits as the projection beside a `memory.set` does."""
    from server.memory.api.models_bulk import MemoryBulkSetInput, _BulkSetEntry, _BulkSetResult
    from server.memory.app.projections import write_entity_edges

    class _CH:
        def __init__(self) -> None:
            self.inserts: list[tuple[str, dict[str, Any]]] = []

        async def query_all(self, sql: str, params: dict[str, Any] | None = None):
            return []

        async def insert(self, table: str, rows: list[dict[str, Any]], settings=None):
            self.inserts.append((table, dict(settings or {})))

    inp = MemoryBulkSetInput(
        org="o",
        namespace="n",
        agent="t",
        entries=[
            _BulkSetEntry(
                memory_key="m", value="Bob works on Project X", entities=["bob", "project x"]
            )
        ],
    )
    for kw, want in (({}, 1), ({"wait_visible": False}, 0)):
        ch = _CH()
        await write_entity_edges(
            inp, [_BulkSetResult(memory_key="m", version=0)], ch=ch, entity_map={}, **kw
        )
        tables = {t for t, _ in ch.inserts}
        assert {"loom.entity_edges", "loom.entity_edge_memories"} <= tables
        assert all(s["wait_for_async_insert"] == want for _, s in ch.inserts)


@pytest.mark.asyncio
async def test_a_projections_end_is_attributed_in_the_query_log(
    server: _Server, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The end runs detached, in no request's context: it tags its own statements."""
    tags: list[str] = []

    async def _end(*_: Any, **__: Any) -> None:
        tags.append(ch_mod.current_query_tag())

    monkeypatch.setattr(gp, "end", _end)
    await gwm.track("o", "n", None)
    await gwm.drain(timeout_s=2.0)
    assert tags == [gwm.END_QUERY_TAG]


def test_a_path_waits_for_the_projections_in_flight_like_a_walk(monkeypatch) -> None:
    from server.memory.api.models_facts import FactsPathInput
    from server.memory.app import facts

    calls: list[str] = []

    async def wait(org: str, ns: str):
        calls.append(f"wait {org}/{ns}")
        raise RuntimeError("marks unreadable")  # the path still walks

    def walk():
        calls.append("walk")
        raise RuntimeError("stop at the walk")

    monkeypatch.setattr(facts, "_after_own_projections", wait)
    monkeypatch.setattr(facts, "get_client", walk)
    path = FactsPathInput(org="o", namespace="n", entity_a="a", entity_b="b")
    with pytest.raises(RuntimeError, match="stop at the walk"):
        asyncio.run(facts.handle_facts_path(path))
    assert calls == ["wait o/n", "walk"]
