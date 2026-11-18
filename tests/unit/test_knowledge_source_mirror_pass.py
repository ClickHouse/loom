"""The mirrored sources' share of a sync pass: they run first, apart from the namespace
sources' document bound, rotate, keep their state under the mirror, attribute their
model calls to the mirror, and read their lists on from a cursor
(docs/architecture/document-sources.md, Every complete listing reads the lists again)."""

from __future__ import annotations

import asyncio
import json
from datetime import datetime
from typing import Any

import pytest

from server.clients import gdrive
from server.code.app.index.pass_budget import PassBudget
from server.knowledge.app import source_sync, source_sync_mirror
from server.knowledge.store import mirrors as mirror_store
from server.llm import customer
from server.settings import settings
from tests.unit.test_knowledge_source_sync import DOCS, _detail, _file, _source
from tests.unit.test_knowledge_source_sync import world as world  # the fixture
from tests.unit.test_knowledge_source_sync_mirror import MIRROR, READERS, _mirrored, _own
from tests.unit.test_knowledge_source_sync_mirror import mirror as mirror  # the fixture


def _pass(world: Any, *sources: Any) -> Any:
    world.sources = list(sources)
    return asyncio.run(source_sync.run_sync())


def _files(n: int, **kw: Any) -> list[dict[str, Any]]:
    return [_file(f"doc{i}", DOCS, f"2026-01-0{i}T00:00:00.000Z", **kw) for i in range(1, n + 1)]


def test_the_mirrored_sources_run_first_under_their_own_document_bound(
    world: Any, mirror: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(settings, "knowledge_source_sync_documents_per_pass", 3)
    monkeypatch.setattr(settings, "knowledge_source_mirror_documents_per_pass", 1)
    world.files = _files(3, permissions=READERS)
    result = _pass(world, _source(name="notes"), _mirrored())
    homes = [written.namespace for written, _ in world.upserts]
    # The mirror's one document first, then every namespace document: the mirror's size
    # takes nothing from the namespace sources' bound.
    assert homes == [MIRROR, *[_source().namespace] * 3]
    assert result.sources == 2 and result.cut


def test_the_list_reads_leave_time_for_the_new_files(
    world: Any, mirror: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    world.files = _files(1)
    _pass(world, _mirrored())  # doc1 stored, its list read with its write

    class _Spent(PassBudget):
        def exhausted(self) -> bool:
            return True  # the list reads' half of the leg is gone at once

    monkeypatch.setattr(source_sync_mirror, "PassBudget", _Spent)
    world.files = [*_files(1), _file("doc9", DOCS, "2026-01-09T00:00:00.000Z")]
    before = len(world.upserts)
    _pass(world, _mirrored())
    assert [w.source_uri.split("/")[-2] for w, _ in world.upserts[before:]] == ["doc9"]
    assert _own(world)["lists_cursor"].endswith("/doc1/edit#id-decks")


def test_the_mirrored_source_synced_longest_ago_goes_first_even_past_a_request(
    world: Any, mirror: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    order: list[str] = []
    real = source_sync._sync_one

    async def _seen(src: Any, *a: Any) -> Any:
        order.append(src.name)
        return await real(src, *a)

    monkeypatch.setattr(source_sync, "_sync_one", _seen)

    def row(**detail: str) -> dict[str, str]:
        return {"state_value": "", "detail": json.dumps(detail)}

    world.states = {
        "a": row(last_synced_at="2026-01-03T00:00:00+00:00"),
        "b": row(last_synced_at="2026-01-01T00:00:00+00:00"),
        "c": row(last_synced_at="2026-01-05T00:00:00+00:00", requested_at="2026-01-06T00:00:00"),
    }
    _pass(world, *(_mirrored(name=n) for n in "abc"))
    # A request waits its turn: a source cut every turn keeps its request, and going
    # first on it would starve the others.
    assert order == ["b", "a", "c"]


def test_a_mirrored_source_s_namespace_row_holds_only_its_codes(
    world: Any, mirror: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def _fails(token: str, file_id: str, **kw: Any) -> Any:
        raise gdrive.VendorError("503")

    monkeypatch.setattr(gdrive, "file_permissions", _fails)
    world.files = _files(2)  # listed without lists: each needs a read, and each fails
    # An N-1 turn left its whole state in the namespace row; this turn drops it.
    left = {"requested_at": "t", "file_waits": {"doc1": ["r", 1, "x"]}, "error": "export"}
    world.states["decks"] = {"state_value": "", "detail": json.dumps(left)}
    _pass(world, _mirrored())
    shown = _detail(world)
    assert set(shown) == {"last_synced_at", "status"}  # the request was answered
    own = _own(world)
    assert own["changed"] == 2 and own["error"].startswith("permissions:")


def test_a_source_added_again_under_the_name_inherits_no_state(world: Any, mirror: Any) -> None:
    world.files = _files(1, permissions=READERS)
    revision = world.files[0]["modifiedTime"]
    # The removed source gave the file up; its state is under its own id.
    given_up = {"file_waits": {"doc1": [revision, 99, "export_failed"]}}
    world.states["id-decks"] = {"state_value": "", "detail": json.dumps(given_up)}
    _pass(world, _mirrored(source_id="id-again"))
    assert [written.namespace for written, _ in world.upserts] == [MIRROR]


def test_a_mirrored_turn_attributes_model_calls_to_its_mirror(
    world: Any, mirror: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    seen: list[str] = []
    named: list[tuple[str, str]] = []
    real = source_sync.sync_file

    async def _attributed(*a: Any, **kw: Any) -> Any:
        seen.append(customer.attribution().namespace)
        return await real(*a, **kw)

    def _identity(name: str, *, org: str = "", namespace: str = "", **kw: Any) -> None:
        named.append((name, namespace))

    monkeypatch.setattr(source_sync, "sync_file", _attributed)
    monkeypatch.setattr(source_sync_mirror, "set_worker_identity", _identity)
    world.files = _files(1, permissions=READERS)
    _pass(world, _mirrored())
    assert seen == [MIRROR] and named == [(source_sync.WORKER, MIRROR)]


@pytest.fixture
def slow_lists(monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    """Each file's list read separately, and the deadline after `per_turn` of them."""
    reads: dict[str, Any] = {"per_turn": 0, "now": 0, "ids": [], "rows": []}

    async def _read(token: str, file_id: str, **kw: Any) -> Any:
        reads["now"] += 1
        reads["ids"].append(file_id)
        return READERS

    async def _listing(org: str, ns: str, source_id: str, name: str, started: Any) -> None:
        reads["rows"].append(started)

    def _exhausted(self: PassBudget) -> bool:
        return bool(reads["per_turn"]) and reads["now"] >= reads["per_turn"]

    monkeypatch.setattr(gdrive, "file_permissions", _read)
    monkeypatch.setattr(mirror_store, "record_listing", _listing)
    monkeypatch.setattr(PassBudget, "exhausted", _exhausted)
    return reads


def _turn_with(world: Any, reads: dict[str, Any], per_turn: int) -> None:
    reads.update(per_turn=per_turn, now=0)
    reads["ids"].clear()
    world.files = _files(4)  # listed without lists every time
    _pass(world, _mirrored())


def test_the_lists_go_on_from_the_cursor_and_the_cycle_s_row_lands_past_the_wrap(
    world: Any, mirror: Any, slow_lists: dict[str, Any]
) -> None:
    _turn_with(world, slow_lists, 0)  # every file written, each with its list read
    assert len(slow_lists["rows"]) == 1
    first = slow_lists["rows"][-1]
    _turn_with(world, slow_lists, 2)  # doc1 and doc2 read; doc3 is where it stopped
    assert slow_lists["ids"] == ["doc1", "doc2"] and len(slow_lists["rows"]) == 1
    began = datetime.fromisoformat(_own(world)["lists_since"])
    assert began > first and _own(world)["lists_cursor"].endswith("/doc3/edit#id-decks")
    _turn_with(world, slow_lists, 3)  # doc3, doc4, then doc1 past the wrap: the cycle is done
    assert slow_lists["ids"] == ["doc3", "doc4", "doc1"]
    assert slow_lists["rows"][-1] == began  # the row carries the start of the cycle
    assert _own(world)["lists_cursor"].endswith("/doc2/edit#id-decks")


def test_a_turn_that_vouches_for_nothing_keeps_the_cycle(
    world: Any, mirror: Any, slow_lists: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    _turn_with(world, slow_lists, 0)
    _turn_with(world, slow_lists, 2)
    kept = (_own(world)["lists_cursor"], _own(world)["lists_since"])

    async def _owed(org: str, namespace: str, uri_suffix: str) -> str:
        return "owed"

    monkeypatch.setattr(source_sync.documents, "publications_owed", _owed)
    rows = len(slow_lists["rows"])
    _turn_with(world, slow_lists, 0)  # every list read, but a write was not in ClickHouse
    assert len(slow_lists["rows"]) == rows
    assert (_own(world)["lists_cursor"], _own(world)["lists_since"]) == kept


def test_the_namespace_sources_keep_the_pass_s_own_budget_after_the_mirrors(
    world: Any, mirror: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    budgets: dict[str, PassBudget] = {}
    deadlines: dict[str, float] = {}
    real = source_sync._sync_one

    async def _seen(src: Any, state: Any, run: Any, result: Any) -> Any:
        budgets[src.name] = run.budget
        deadlines[src.name] = source_sync._read_deadline(run)
        return await real(src, state, run, result)

    monkeypatch.setattr(source_sync, "_sync_one", _seen)
    whole = source_sync._budget()
    monkeypatch.setattr(source_sync, "_budget", lambda: whole)
    world.files = _files(1, permissions=READERS)
    _pass(world, _source(name="notes"), _mirrored())
    span = whole.stop_after - whole.started
    assert budgets["decks"].stop_after <= budgets["decks"].started + span / 3 + 1e-6
    assert budgets["notes"] is whole  # its start and end, whatever the mirrors used
    pass_end = whole.started + float(settings.knowledge_source_sync_max_pass_seconds)
    assert deadlines["notes"] == pass_end - source_sync._MINE_MARGIN_S


def test_a_cycle_begun_at_a_wrap_keeps_its_start_until_its_own_wrap(
    world: Any, mirror: Any, slow_lists: dict[str, Any]
) -> None:
    for per_turn in (0, 2, 3):  # as above: the third turn wraps and begins a cycle at doc2
        _turn_with(world, slow_lists, per_turn)
    began = _own(world)["lists_since"]
    rows = len(slow_lists["rows"])
    _turn_with(world, slow_lists, 1)  # doc2 read, doc3 left: before this cycle's wrap
    assert slow_lists["ids"] == ["doc2"] and len(slow_lists["rows"]) == rows
    assert _own(world)["lists_since"] == began
    _turn_with(world, slow_lists, 3)  # doc3, doc4, doc1, then doc2 past the wrap
    assert slow_lists["ids"] == ["doc3", "doc4", "doc1"]
    assert slow_lists["rows"][-1] == datetime.fromisoformat(began)


def test_a_turn_that_fails_before_it_knows_its_mirror_leaves_the_mirror_s_state(
    world: Any, mirror: Any, slow_lists: dict[str, Any]
) -> None:
    _turn_with(world, slow_lists, 0)
    _turn_with(world, slow_lists, 2)
    kept = _own(world)
    slow_lists.update(per_turn=0, now=0)
    world.fail_listing = 401
    world.sources = [_mirrored()]
    asyncio.run(source_sync.run_sync())
    assert _own(world) == kept
    assert _detail(world)["status"] == "credential_invalid"


def test_a_publication_owed_at_the_read_moves_no_cursor(
    world: Any, mirror: Any, slow_lists: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    _turn_with(world, slow_lists, 0)
    answers = iter(["owed", ""])  # owed when the copies are read, settled by the end

    async def _owed(org: str, namespace: str, uri_suffix: str) -> str:
        return next(answers)

    monkeypatch.setattr(source_sync.documents, "publications_owed", _owed)
    _turn_with(world, slow_lists, 2)  # stops before the wrap: it would begin a cycle
    assert _own(world).get("lists_cursor", "") == ""


def test_a_publication_parked_by_the_end_keeps_the_cycle(
    world: Any, mirror: Any, slow_lists: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    _turn_with(world, slow_lists, 0)
    _turn_with(world, slow_lists, 2)
    kept = (_own(world)["lists_cursor"], _own(world)["lists_since"])
    answers = iter(["", "parked"])  # clear when the copies are read, parked by the end

    async def _owed(org: str, namespace: str, uri_suffix: str) -> str:
        return next(answers)

    monkeypatch.setattr(source_sync.documents, "publications_owed", _owed)
    rows = len(slow_lists["rows"])
    _turn_with(world, slow_lists, 1)  # doc3 read, doc4 left: it would move the cursor on
    assert len(slow_lists["rows"]) == rows
    assert (_own(world)["lists_cursor"], _own(world)["lists_since"]) == kept


def test_a_turn_that_reads_every_list_mid_cycle_lands_its_own_start_and_ends_the_cycle(
    world: Any, mirror: Any, slow_lists: dict[str, Any]
) -> None:
    _turn_with(world, slow_lists, 0)
    _turn_with(world, slow_lists, 2)
    began = datetime.fromisoformat(_own(world)["lists_since"])
    _turn_with(world, slow_lists, 0)  # time for every list
    assert slow_lists["rows"][-1] > began
    assert (_own(world)["lists_cursor"], _own(world)["lists_since"]) == ("", "")


def test_a_turn_whose_list_reads_stop_still_retires_a_gone_file_at_the_full_interval(
    world: Any, mirror: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    world.files = _files(4)
    _pass(world, _mirrored())  # each list read with its write

    class _Spent(PassBudget):
        def exhausted(self) -> bool:
            return True  # the list reads' half of the leg is gone at once

    monkeypatch.setattr(source_sync_mirror, "PassBudget", _Spent)
    # A full sync asked for, and doc4 gone from the store.
    asked = {"requested_at": "t", "full": True}
    world.states["decks"] = {"state_value": "", "detail": json.dumps(asked)}
    world.files = _files(3)
    result = _pass(world, _mirrored())
    assert result.removed == 0 and not any("/doc4/" in uri for uri in world.docs)
    assert "full_listed_at" in _own(world) and _own(world)["lists_cursor"]


def test_the_list_reads_get_half_of_what_is_left_of_the_mirrors_share(
    world: Any, mirror: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    world.files = _files(1)
    _pass(world, _mirrored())
    legs: list[PassBudget] = []
    lists: list[PassBudget] = []
    real = source_sync._sync_one

    async def _seen(src: Any, state: Any, run: Any, result: Any) -> Any:
        legs.append(run.budget)
        return await real(src, state, run, result)

    class _Kept(PassBudget):
        def __init__(self, started: float, stop_after: float) -> None:
            super().__init__(started, stop_after)
            lists.append(self)

    monkeypatch.setattr(source_sync, "_sync_one", _seen)
    monkeypatch.setattr(source_sync_mirror, "PassBudget", _Kept)
    _pass(world, _mirrored())
    [leg], [read] = legs, lists
    assert read.stop_after - read.started == pytest.approx((leg.stop_after - read.started) / 2)
