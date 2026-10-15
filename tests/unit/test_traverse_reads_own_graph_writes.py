"""A walk sees the graph its caller just wrote.

`memory.set` projects its entity edges beside the response, and those edges are
async inserts. `facts.traverse` remembers a walk for a minute. Together, a
client that wrote and then walked could otherwise be handed, for a whole minute, the walk
read before its edges landed. The walk waits for this process's own
projection of the namespace, never serves a walk read before this process's last
graph write there, reads with sequential consistency inside the replication
window after one, and remembers no walk read inside that window.
"""

from __future__ import annotations

import asyncio
import time
from typing import Any

import pytest

from server.core import ch as ch_mod
from server.memory.api.models_facts import FactsTraverseInput, FactsTraverseOutput
from server.memory.app import after_write, recent_writes
from server.memory.app import facts as facts_handler
from server.memory.app import graph_write_marks as gwm


@pytest.fixture(autouse=True)
def _fresh(monkeypatch: pytest.MonkeyPatch):
    local = gwm.GraphWriteMarks(None)
    gwm._reset_for_tests(local)
    monkeypatch.setattr(facts_handler, "_memo", facts_handler._WalkMemo())
    yield
    gwm._reset_for_tests(None)


def _note(org: str, ns: str, **kw: Any) -> None:
    gwm.marks().note_write(org, ns, **kw)


def _walk_counter(monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    state: dict[str, Any] = {"walks": 0, "sequential": [], "graph": ["alice"]}

    async def fake_walk(inp: FactsTraverseInput) -> FactsTraverseOutput:
        state["walks"] += 1
        state["sequential"].append(ch_mod.sequential_reads_active())
        return FactsTraverseOutput(
            seed=inp.seed_entity, nodes=[], truncated=len(state["graph"]) > 1
        )

    monkeypatch.setattr(facts_handler, "_handle_facts_traverse_uncached", fake_walk)
    return state


def _walk(ns: str = "n") -> FactsTraverseInput:
    return FactsTraverseInput(org="o", namespace=ns, seed_entity="alice", max_hops=2)


@pytest.mark.asyncio
async def test_a_graph_write_retires_the_walk_remembered_before_it(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from server.settings import settings

    monkeypatch.setattr(settings, "ch_read_your_writes_window_s", 0.0)
    state = _walk_counter(monkeypatch)
    await facts_handler.handle_facts_traverse(_walk())
    await facts_handler.handle_facts_traverse(_walk())
    assert state["walks"] == 1  # remembered

    _note("o", "n")
    await facts_handler.handle_facts_traverse(_walk())
    assert state["walks"] == 2  # read before the write: not served
    await facts_handler.handle_facts_traverse(_walk())
    assert state["walks"] == 2  # read after it: remembered again

    _note("o", "other")
    await facts_handler.handle_facts_traverse(_walk())
    assert state["walks"] == 2  # another namespace's write leaves this walk alone


@pytest.mark.asyncio
async def test_inside_the_window_a_walk_reads_sequentially_and_is_not_remembered(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from server.settings import settings

    monkeypatch.setattr(settings, "ch_read_your_writes_window_s", 20.0)
    monkeypatch.setattr(settings, "ch_read_your_writes", True)
    state = _walk_counter(monkeypatch)
    _note("o", "n")
    await facts_handler.handle_facts_traverse(_walk())
    await facts_handler.handle_facts_traverse(_walk())
    assert state["walks"] == 2 and state["sequential"] == [True, True]

    # Outside the window: plain reads, remembered.
    gwm._reset_for_tests(gwm.GraphWriteMarks(None))
    _note("o", "n", at_ns=time.time_ns() - 60 * 10**9)
    await facts_handler.handle_facts_traverse(_walk())
    await facts_handler.handle_facts_traverse(_walk())
    assert state["walks"] == 3 and state["sequential"][-1] is False


@pytest.mark.asyncio
async def test_a_walk_waits_for_its_namespaces_projection_in_flight(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    state = _walk_counter(monkeypatch)

    async def projection() -> None:
        await asyncio.sleep(0.05)
        state["graph"].append("bob")

    after_write.schedule(projection(), name="t-projection", scope=("o", "n"))
    out = await facts_handler.handle_facts_traverse(_walk())
    assert out.truncated is True  # the walk ran after the projection landed

    async def slow_other() -> None:
        await asyncio.sleep(5)

    other = after_write.schedule(slow_other(), name="t-other", scope=("o", "elsewhere"))
    assert other is not None
    try:
        assert await after_write.settle("o", "n", timeout_s=1.0) == 0
    finally:
        other.cancel()


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
async def test_the_projection_beside_the_response_waits_for_its_edges_to_flush() -> None:
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
    for wait_visible, want in ((True, 1), (False, 0)):
        ch = _CH()
        await write_entity_edges(
            inp,
            [_BulkSetResult(memory_key="m", version=0)],
            ch=ch,
            entity_map={},
            wait_visible=wait_visible,
        )
        tables = {t for t, _ in ch.inserts}
        assert {"loom.entity_edges", "loom.entity_edge_memories"} <= tables
        assert all(s["wait_for_async_insert"] == want for _, s in ch.inserts)


# ---- two processes sharing the marks ------------------------------------------------


def _two_processes(tmp_path: Any) -> tuple[Any, Any, Any, Any]:
    """Two web workers of one server: each has its own marks and its own walk memo; the
    marks share one directory."""
    shared = str(tmp_path / "graph-marks")
    a_marks, b_marks = gwm.GraphWriteMarks(shared), gwm.GraphWriteMarks(shared)
    return a_marks, facts_handler._WalkMemo(a_marks), b_marks, facts_handler._WalkMemo(b_marks)


@pytest.mark.asyncio
async def test_a_write_on_one_process_retires_the_walk_another_remembered(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Any
) -> None:
    from server.settings import settings

    monkeypatch.setattr(settings, "ch_read_your_writes_window_s", 0.0)
    state = _walk_counter(monkeypatch)
    a_marks, _a_memo, _b_marks, b_memo = _two_processes(tmp_path)

    await b_memo.walk(_walk())
    await b_memo.walk(_walk())
    assert state["walks"] == 1  # B remembers its walk

    a_marks.note_write("o", "n")  # A's memory.set projected edges
    await b_memo.walk(_walk())
    assert state["walks"] == 2  # B does not serve the walk it read before A's write
    await b_memo.walk(_walk())
    assert state["walks"] == 2


@pytest.mark.asyncio
async def test_inside_the_window_after_another_processs_write_the_walk_is_not_remembered(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Any
) -> None:
    from server.settings import settings

    monkeypatch.setattr(settings, "ch_read_your_writes_window_s", 20.0)
    monkeypatch.setattr(settings, "ch_read_your_writes", True)
    state = _walk_counter(monkeypatch)
    a_marks, _a_memo, _b_marks, b_memo = _two_processes(tmp_path)
    a_marks.note_write("o", "n")
    await b_memo.walk(_walk())
    await b_memo.walk(_walk())
    assert state["walks"] == 2 and state["sequential"] == [True, True]


@pytest.mark.asyncio
async def test_a_walk_waits_for_another_processs_projection_in_flight(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Any
) -> None:
    state = _walk_counter(monkeypatch)
    a_marks, _a_memo, _b_marks, b_memo = _two_processes(tmp_path)
    token = a_marks.begin_pending("o", "n")
    assert token is not None

    async def a_projection_lands() -> None:
        await asyncio.sleep(0.1)
        state["graph"].append("bob")
        a_marks.note_write("o", "n")
        a_marks.end_pending(token)

    landing = asyncio.create_task(a_projection_lands())
    out = await b_memo.walk(_walk())
    await landing
    assert out.truncated is True  # B walked after A's projection landed


def test_marks_are_best_effort_when_the_directory_is_missing(tmp_path: Any) -> None:
    gone = tmp_path / "file-not-dir"
    gone.write_text("")  # a regular file where the directory should be
    m = gwm.GraphWriteMarks(str(gone))
    m.note_write("o", "n")  # never raises
    assert m.written_at_ns("o", "n") is not None  # the local mark still holds
    assert m.begin_pending("o", "n") is None
    assert m.pending("o", "n") == 0

    nothing = gwm.GraphWriteMarks(str(tmp_path / "never-created"))
    assert nothing.written_at_ns("o", "n") is None and nothing.pending("o", "n") == 0


def test_a_pending_mark_left_by_a_dead_process_is_ignored(tmp_path: Any) -> None:
    m = gwm.GraphWriteMarks(str(tmp_path))
    token = m.begin_pending("o", "n")
    assert token is not None and m.pending("o", "n") == 1
    later = time.time_ns() + int((gwm.STALE_PENDING_S + 1) * 1e9)
    assert m.pending("o", "n", now_ns=later) == 0


def test_single_worker_default_shares_nothing(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Any
) -> None:
    from server.settings import settings

    monkeypatch.setattr(gwm, "_default_dir", lambda: str(tmp_path / "default"))

    monkeypatch.setattr(settings, "graph_write_marks_shared", "auto")
    monkeypatch.setattr(settings, "graph_write_marks_dir", "")
    monkeypatch.setattr(settings, "web_workers", 1)
    assert gwm._resolve_dir() is None
    monkeypatch.setattr(settings, "web_workers", 4)
    assert gwm._resolve_dir() is not None
    monkeypatch.setattr(settings, "graph_write_marks_shared", "off")
    assert gwm._resolve_dir() is None


def test_no_private_runtime_directory_leaves_the_marks_in_process(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from server.settings import settings

    def nowhere() -> str:
        raise OSError("none of /run/loom, /tmp/loom-1 is a directory only this user can enter")

    monkeypatch.setattr(gwm, "_default_dir", nowhere)
    monkeypatch.setattr(settings, "graph_write_marks_shared", "auto")
    monkeypatch.setattr(settings, "graph_write_marks_dir", "")
    monkeypatch.setattr(settings, "web_workers", 4)
    assert gwm._resolve_dir() is None
