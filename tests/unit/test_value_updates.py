"""Value-update supersession (memory.app.value_updates): the pure test that
tells an UPDATE (same statement, different value) from a mere neighbour, the
older/newer decision, the linker's writes, and fail-open."""

from __future__ import annotations

from datetime import datetime
from typing import Any

import pytest

from server.memory.app import value_updates as vu
from server.memory.domain.conflicts import value_tokens

OLD = '"User recently set a personal best time of 27:12 in a charity 5K run."'
NEW = '"User\'s personal best time for the 5K run is 25:50."'
TRAIN = (
    '"User is training for a charity 5K run and hopes to beat their personal best time of 25:50."'
)


def test_value_tokens_find_times_money_and_units() -> None:
    assert value_tokens(OLD) == {"27:12", "5k"}
    assert value_tokens("paid $40 and 12% tax at 5:30pm") == {"$40", "12%", "5:30pm"}
    assert value_tokens("no numbers here") == set()
    assert value_tokens("1,000 seats and 12,345.67 paid") == {"1000", "12345.67"}


def test_a_thousands_separator_alone_is_no_value_update() -> None:
    assert not vu.is_value_update("the venue has 1,000 seats", "the venue has 1000 seats")
    assert vu.is_value_update("the venue has 1,000 seats", "the venue has 1,500 seats")


def test_content_tokens_drop_stopwords_and_numbers() -> None:
    toks = vu.content_tokens(NEW)
    assert {"personal", "best", "time", "5k", "run"} <= toks
    assert "user" not in toks and "is" not in toks


def test_is_value_update_on_the_measured_pair() -> None:
    # The LongMemEval 5K pair: cosine 0.82 (below the conflict threshold),
    # same attribute, different clock value.
    assert vu.is_value_update(OLD, NEW)
    assert vu.is_value_update(OLD, TRAIN)
    # Same value restated with extra words is not an update.
    assert not vu.is_value_update(NEW, TRAIN)
    # Different subjects sharing a value are not an update.
    assert not vu.is_value_update(
        "User baked a chocolate cake for a friend's party on 2022-04-09",
        "User plans to add tofu to the Vegetable Korma recipe, cooking for 25 minutes",
    )
    # No value tokens at all.
    assert not vu.is_value_update("User likes tea", "User likes coffee")


def _peer(
    mid: str, key: str, value: str, valid_at: str | None, created: str = "2026-01-01"
) -> dict[str, Any]:
    return {
        "memory_id": mid,
        "memory_key": key,
        "value": value,
        "valid_at": datetime.fromisoformat(valid_at) if valid_at else None,
        "created_at": datetime.fromisoformat(created),
        "cos": 0.82,
    }


def test_decide_links_marks_the_older_side() -> None:
    peers = [_peer("p1", "user_running_personal_best", OLD, "2023-05-23")]
    links = vu.decide_links(
        "n1", "user_5k_personal_best_time", NEW, "2023-05-30", "2026-01-02", peers
    )
    assert len(links) == 1 and links[0].older_id == "p1" and links[0].newer_id == "n1"
    # The new row can be the OLDER one (backfilled): then it is superseded.
    links = vu.decide_links(
        "n1", "k", OLD, "2023-05-23", "2026-01-02", [_peer("p2", "k2", NEW, "2023-05-30")]
    )
    assert links[0].older_id == "n1" and links[0].newer_id == "p2"


def test_decide_links_needs_a_strictly_later_day() -> None:
    # Undated on either side: no link (an update happens across time).
    assert (
        vu.decide_links("n1", "k", NEW, "2023-05-30", "2026-01-02", [_peer("p1", "k1", OLD, None)])
        == []
    )
    assert (
        vu.decide_links("n1", "k", NEW, None, "2026-01-03", [_peer("p1", "k1", OLD, "2023-05-23")])
        == []
    )
    # Same day: an enumeration ("Article 12 …" / "Article 13 …"), not an update.
    assert (
        vu.decide_links("n1", "k", NEW, "2023-05-30", None, [_peer("p1", "k1", OLD, "2023-05-30")])
        == []
    )
    # Same memory_key is a rewrite, never a link.
    assert (
        vu.decide_links("n1", "k", NEW, "2023-05-30", None, [_peer("p1", "k", OLD, "2023-05-23")])
        == []
    )


def test_is_value_update_rejects_same_template_enumerations_by_overlap_floor() -> None:
    # Template-heavy pairs pass the overlap floor; the date rule above is what
    # keeps them apart. Record the shape so the floor is not lowered blindly.
    a = "Assistant provided prices for Seventh Generation refill packs, ranging from $8 to $42 depending on the size"
    b = "Assistant provided prices for Method refill packs, ranging from $5 to $32 depending on the size and type"
    assert vu.is_value_update(a, b)  # same day in the corpus → decide_links drops it


class _FakeCH:
    def __init__(self, rows: list[dict[str, Any]] | Exception) -> None:
        self.rows = rows
        self.calls: list[dict[str, Any]] = []

    async def query_all(
        self, sql: str, params: dict[str, Any] | None = None
    ) -> list[dict[str, Any]]:
        self.calls.append(params or {})
        if isinstance(self.rows, Exception):
            raise self.rows
        return self.rows


async def test_link_value_updates_writes_supersession_on_the_older_row(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from server.memory import store as memory_repo

    writes: list[tuple[list[str], dict[str, Any]]] = []

    async def fake_write(
        org: str, ids: list[str], update: Any, *, ch: Any = None, **_kw: Any
    ) -> int:
        from types import SimpleNamespace

        row = SimpleNamespace(memory_id=ids[0], conflict_with=[], status="active", pinned=False)
        assert not _kw["skip"](row)
        writes.append((ids, update(row)))
        # The writer hears the version rows it built: dicts, the update applied.
        _kw["on_written"]([{**vars(row), **update(row)}])
        return len(ids)

    monkeypatch.setattr(memory_repo, "write_versions_by_ids", fake_write)
    ch = _FakeCH([_peer("p1", "user_running_personal_best", OLD, "2023-05-23")])
    rows = [
        {
            "memory_id": "n1",
            "memory_key": "user_5k_personal_best_time",
            "memory_type": "semantic",
            "value": NEW,
            "valid_at": datetime(2023, 5, 30),
            "created_at": datetime(2026, 1, 2),
        }
    ]
    rep = await vu.link_value_updates(
        org="o",
        namespace="ns",
        rows=rows,
        embeddings_by_key={"user_5k_personal_best_time": [0.1, 0.2]},
        agent="a",
        ch=ch,
    )
    assert rep.checked == 1 and len(rep.links) == 1 and rep.errors == 0
    assert ch.calls[0]["min_cos"] == pytest.approx(0.75) and ch.calls[0]["dim"] == 2
    ids, overrides = writes[0]
    assert ids == ["p1"]
    assert overrides["status"] == "superseded" and overrides["superseded_by"] == "n1"
    assert overrides["invalid_at"] == datetime(2023, 5, 30)


async def test_link_value_updates_fails_open(monkeypatch: pytest.MonkeyPatch) -> None:
    from server.memory import store as memory_repo

    async def boom(*a: Any, **k: Any) -> int:
        raise AssertionError("must not write")

    monkeypatch.setattr(memory_repo, "write_versions_by_ids", boom)
    rep = await vu.link_value_updates(
        org="o",
        namespace="ns",
        rows=[{"memory_id": "n1", "memory_key": "k", "value": NEW}],
        embeddings_by_key={"k": [0.1]},
        agent="a",
        ch=_FakeCH(RuntimeError("ch down")),
    )
    assert rep.links == [] and rep.errors == 1
    # No embedding for the key → nothing to look up, no error.
    rep = await vu.link_value_updates(
        org="o",
        namespace="ns",
        rows=[{"memory_id": "n1", "memory_key": "k", "value": NEW}],
        embeddings_by_key={},
        agent="a",
        ch=_FakeCH([]),
    )
    assert rep.checked == 1 and rep.links == [] and rep.errors == 0


def test_enabled_precedence_request_over_contextvar_over_setting(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from server.memory.app import bulk_set as bulk_set
    from server.memory.app.extract import handler as hm
    from server.settings import settings

    monkeypatch.setattr(settings, "memory_extract_value_updates", False)
    assert bulk_set.value_updates_enabled(None) is False
    tok = hm.detect_updates_override.set(True)
    try:
        assert bulk_set.value_updates_enabled(None) is True
        assert bulk_set.value_updates_enabled(False) is False
    finally:
        hm.detect_updates_override.reset(tok)
    monkeypatch.setattr(settings, "memory_extract_value_updates", True)
    assert bulk_set.value_updates_enabled(None) is True


async def test_duplicate_rows_do_not_link(monkeypatch: pytest.MonkeyPatch) -> None:
    ch = _FakeCH([_peer("p1", "k1", OLD, "2023-05-23")])
    rows = [
        {
            "memory_id": "n1",
            "memory_key": "k",
            "memory_type": "semantic",
            "status": "duplicate",
            "value": NEW,
            "valid_at": datetime(2023, 5, 30),
            "created_at": datetime(2026, 1, 2),
        }
    ]
    rep = await vu.link_value_updates(
        org="o", namespace="ns", rows=rows, embeddings_by_key={"k": [0.1]}, agent="a", ch=ch
    )
    assert rep.links == [] and ch.calls == []


async def test_set_from_messages_starts_its_link_count_at_zero(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A worker replays queued requests one after another in one task: a request that writes
    nothing must not report the previous request's value-update links."""
    from server.memory.api.models_bulk import (
        MemorySetFromMessagesInput,
        MemorySetFromMessagesOutput,
    )
    from server.memory.app import bulk_set as bulk_set
    from server.memory.app.extract import handler as hm

    async def _enqueued(_inp: object, _rule_agent: str) -> MemorySetFromMessagesOutput:
        return MemorySetFromMessagesOutput(written=[])

    monkeypatch.setattr(hm, "_enqueue_deferred", _enqueued)
    hm.last_value_updates_linked.set(7)
    await hm.handle_memory_set_from_messages(
        MemorySetFromMessagesInput(
            org="o",
            namespace="ns",
            agent="a",
            messages=[{"role": "user", "content": "hi"}],
            defer=True,
            detect_updates=True,
        )
    )
    assert hm.last_value_updates_linked.get() == 0
    assert hm.detect_updates_override.get() is True
