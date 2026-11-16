"""memory.definitions.list: each shared definition with the read-time check's verdict, the
names a governed one loses to another, and the ones past the ceiling marked capped."""

from __future__ import annotations

import json
from dataclasses import replace
from datetime import UTC, datetime, timedelta, timezone
from typing import Any

import pytest

from server.core.hashing import served_content_sha256
from server.identity.domain.read_scope import WHOLE_NAMESPACE
from server.memory import store as memory_repo
from server.memory.api.models_lifecycle import MemoryDefinitionsListInput
from server.memory.app import definitions
from server.memory.domain.definition_check import (
    SharedDefinition,
    Source,
    copy_lapse,
    lost_names,
    sensitivity_lapse,
    source_lapse,
)
from server.memory.domain.promotion_source import promotion_source
from server.memory.store import MemoryRow, shared
from server.settings import settings
from tests.unit.clickhouse_local import CLICKHOUSE, LocalCH, ddl

NOW = datetime(2026, 10, 1)
KEY = "definition/arr"
VALUE = {"term": "ARR", "aliases": ["annual recurring revenue"], "body": "Yearly revenue."}


def source_ref(org: str, namespace: str, key: str) -> str:
    return promotion_source(org, namespace, "-", "-", "-", key)


def _shared(key: str = KEY, value: Any = VALUE, **over: Any) -> SharedDefinition:
    base: dict[str, Any] = {
        "memory_key": key,
        "value": value,
        "promoted_from": (source_ref("o", "n", key),),
        "promoted_at": NOW - timedelta(days=1),
        "promotion_event": "e1",
    }
    return SharedDefinition(**{**base, **over})


SOURCE = Source(
    status="active",
    invalid_at=None,
    expires_at=None,
    valid_to=None,
    served_sha256=served_content_sha256(VALUE),
)


@pytest.mark.parametrize(
    ("shared_over", "source", "reason"),
    [
        ({}, SOURCE, None),
        ({"promoted_from": ("o/n/bot/-/-/definition/arr",)}, SOURCE, "not_namespace_wide"),
        ({"promoted_from": ("o/n/-//-/definition/arr",)}, SOURCE, "not_namespace_wide"),
        ({"promoted_from": (source_ref("o", "n", KEY),) * 2}, SOURCE, "not_namespace_wide"),
        ({"value": {"body": "no term"}}, SOURCE, "no_term"),
        ({}, None, "gone"),
        ({}, replace(SOURCE, status="rejected"), "not_active"),
        ({}, replace(SOURCE, status="superseded", invalid_at=NOW), "superseded"),
        ({}, replace(SOURCE, invalid_at=NOW), "superseded"),
        ({}, replace(SOURCE, status="conflicted"), "conflicted"),
        ({}, replace(SOURCE, status="duplicate"), "duplicate"),
        ({}, replace(SOURCE, expires_at=NOW), "expired"),
        ({}, replace(SOURCE, expires_at=NOW + timedelta(seconds=1)), None),
        ({}, replace(SOURCE, valid_to=NOW), "ended"),
        ({}, replace(SOURCE, valid_to=NOW + timedelta(seconds=1)), None),
        ({}, replace(SOURCE, served_sha256="other"), "changed"),
    ],
)
def test_the_read_time_check(shared_over: dict, source: Source | None, reason: str | None) -> None:
    shared = _shared(**shared_over)
    got = copy_lapse(shared, org="o", namespace="n") or source_lapse(shared, source, now=NOW)
    assert got == reason


@pytest.mark.parametrize(
    ("label", "graded", "reason"),
    [
        ("", "", None),
        ("low", "low", None),
        ("medium", "", "sensitive"),
        ("", "medium", "sensitive"),
        ("high", "", "sensitive"),
        ("low", "high", "sensitive"),
    ],
)
def test_a_source_labelled_or_graded_medium_or_high_lapses(
    label: str, graded: str, reason: str | None
) -> None:
    assert sensitivity_lapse(label, graded) == reason


def test_a_key_order_change_in_the_store_is_not_a_change() -> None:
    reordered = dict(reversed(list(VALUE.items())))
    assert source_lapse(_shared(value=reordered), SOURCE, now=NOW) is None


def test_the_latest_promotion_holds_a_name_two_definitions_hold() -> None:
    old = _shared(KEY, {"term": "ARR", "aliases": ["Run rate"]}, promotion_event="e1")
    new = _shared(
        "definition/rate",
        {"term": "run rate"},
        promoted_at=NOW,
        promotion_event="e0",
    )
    # Within one second, the promotion event settles it.
    same_second = _shared("definition/arr2", {"term": "arr"}, promotion_event="e2")
    assert lost_names([old, new, same_second]) == {
        KEY: [("ARR", "definition/arr2"), ("Run rate", "definition/rate")],
    }
    assert lost_names([old]) == {}


@pytest.fixture
def namespace(monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    """Shared rows as `definition_rows` reads them (newest first) and their sources."""
    state: dict[str, Any] = {"rows": [], "heads": {}, "asked": []}

    async def rows(org: str, ns: str, ceiling: int) -> list[dict[str, Any]]:
        out = []
        for i, r in enumerate(state["rows"][: 2 * ceiling]):
            value = r["value"] if i < ceiling else ""
            out.append({**r, "value": value, "total": len(state["rows"])})
        return out

    async def heads(
        identity: Any, keys: list[str], *, scope: Any, strong: bool, **_k: Any
    ) -> list[MemoryRow]:
        assert (identity.agent, identity.user_id, identity.session_id) == ("-", "-", "-")
        assert scope == WHOLE_NAMESPACE and strong, "every head, expired ones included"
        state["asked"] += keys
        return [state["heads"][k] for k in keys if k in state["heads"]]

    monkeypatch.setattr(shared, "definition_rows", rows)
    monkeypatch.setattr(memory_repo, "get_many_by_keys", heads)
    monkeypatch.setattr(definitions, "naive_utc_now", lambda: NOW)
    return state


def _row(key: str, value: Any, promoted_at: datetime, **over: Any) -> dict[str, Any]:
    return {
        "memory_key": key,
        "value": json.dumps(value),
        "promoted_from": [source_ref("o", "n", key)],
        "promoted_at": promoted_at,
        "promotion_event": "e",
        **over,
    }


def _head(key: str, value: Any, **over: Any) -> MemoryRow:
    base: dict[str, Any] = {
        "memory_id": "11111111-1111-1111-1111-111111111111",
        "org": "o",
        "namespace": "n",
        "agent": "-",
        "user_id": "-",
        "session_id": "-",
        "memory_key": key,
        "memory_type": "definition",
        "value": value,
        "confidence": 1.0,
        "version": 1,
        "pinned": False,
        "created_at": datetime(2026, 9, 12, tzinfo=UTC),
        "expires_at": None,
    }
    return MemoryRow(**{**base, **over})


async def _list() -> Any:
    return await definitions.handle_memory_definitions_list(
        MemoryDefinitionsListInput(org="o", namespace="n")
    )


async def test_each_definition_carries_its_verdict(namespace: dict[str, Any]) -> None:
    mrr = {"term": "MRR", "body": "Monthly."}
    namespace["rows"] = [
        _row("definition/mrr", mrr, NOW),
        _row(KEY, VALUE, NOW - timedelta(days=1)),
    ]
    namespace["heads"] = {
        KEY: _head(KEY, VALUE),
        "definition/mrr": _head("definition/mrr", {**mrr, "body": "Edited."}),
    }
    out = await _list()
    assert (out.count, out.ceiling) == (2, settings.memory_shared_definitions_max)
    by_key = {e.memory_key: e for e in out.definitions}
    assert by_key["definition/mrr"].verdict == "lapsed"
    assert by_key["definition/mrr"].lapse_reason == "changed"
    arr = by_key[KEY]
    assert (arr.verdict, arr.lapse_reason, arr.term) == ("governed", None, "ARR")
    assert arr.aliases == ["annual recurring revenue"]
    assert arr.lost == []


async def test_a_value_that_reads_medium_lapses_whatever_its_label(
    namespace: dict[str, Any],
) -> None:
    value = {"term": "ARR", "body": "call 415-555-0134 for it"}
    namespace["rows"] = [_row(KEY, value, NOW)]
    namespace["heads"] = {KEY: _head(KEY, value)}
    (entry,) = (await _list()).definitions
    assert entry.lapse_reason == "sensitive"


async def test_a_source_labelled_medium_lapses_though_its_text_reads_clean(
    namespace: dict[str, Any],
) -> None:
    namespace["rows"] = [_row(KEY, VALUE, NOW)]
    namespace["heads"] = {KEY: _head(KEY, VALUE, sensitivity="medium")}
    (entry,) = (await _list()).definitions
    assert entry.lapse_reason == "sensitive"


async def test_the_grading_carries_the_characters_read_so_far(
    namespace: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    seen: list[int] = []

    async def grade(text: str, *, read_before: int = 0) -> str:
        seen.append(read_before)
        return ""

    monkeypatch.setattr(definitions, "detect_level", grade)
    mrr = {"term": "MRR", "body": "Monthly."}
    namespace["rows"] = [
        _row(KEY, VALUE, NOW),
        _row("definition/mrr", mrr, NOW - timedelta(days=1)),
    ]
    namespace["heads"] = {KEY: _head(KEY, VALUE), "definition/mrr": _head("definition/mrr", mrr)}
    await _list()
    first = len(await definitions.decoded_text(VALUE))
    assert seen == [0, first]


async def test_a_changed_source_is_not_graded(
    namespace: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    async def must_not(*_a: Any, **_k: Any) -> str:
        raise AssertionError("a source that lapses otherwise is not graded")

    monkeypatch.setattr(definitions, "detect_level", must_not)
    namespace["rows"] = [_row(KEY, VALUE, NOW)]
    namespace["heads"] = {KEY: _head(KEY, {**VALUE, "body": "Edited."})}
    (entry,) = (await _list()).definitions
    assert entry.lapse_reason == "changed"


async def test_a_source_ending_in_another_zone_is_compared_in_utc(
    namespace: dict[str, Any],
) -> None:
    # 2026-10-01 01:30 at +02:00 is 23:30 UTC the day before, so past NOW.
    zone = timezone(timedelta(hours=2))
    namespace["rows"] = [_row(KEY, VALUE, NOW)]
    namespace["heads"] = {
        KEY: _head(KEY, VALUE, valid_to=datetime(2026, 10, 1, 1, 30, tzinfo=zone))
    }
    (entry,) = (await _list()).definitions
    assert entry.lapse_reason == "ended"


async def test_a_governed_definition_lists_the_names_it_loses(namespace: dict[str, Any]) -> None:
    rate = {"term": "Run rate", "aliases": ["ARR"]}
    namespace["rows"] = [
        _row("definition/rate", rate, NOW),
        _row(KEY, VALUE, NOW - timedelta(days=1)),
    ]
    namespace["heads"] = {KEY: _head(KEY, VALUE), "definition/rate": _head("definition/rate", rate)}
    by_key = {e.memory_key: e for e in (await _list()).definitions}
    assert [(n.name, n.winner) for n in by_key[KEY].lost] == [("ARR", "definition/rate")]
    assert by_key["definition/rate"].lost == []


async def test_a_lapsed_definition_holds_no_name(namespace: dict[str, Any]) -> None:
    rate = {"term": "Run rate", "aliases": ["ARR"]}
    namespace["rows"] = [
        _row("definition/rate", rate, NOW),
        _row(KEY, VALUE, NOW - timedelta(days=1)),
    ]
    namespace["heads"] = {KEY: _head(KEY, VALUE)}  # the newer one's source is gone
    by_key = {e.memory_key: e for e in (await _list()).definitions}
    assert by_key["definition/rate"].lapse_reason == "gone"
    assert by_key[KEY].lost == []


async def test_past_the_ceiling_the_oldest_are_capped_and_not_read(
    namespace: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(settings, "memory_shared_definitions_max", 1)
    namespace["rows"] = [
        _row(KEY, VALUE, NOW),
        _row("definition/mrr", {"term": "MRR"}, NOW - timedelta(days=1)),
        _row("definition/old", {"term": "OLD"}, NOW - timedelta(days=2)),
    ]
    namespace["heads"] = {KEY: _head(KEY, VALUE)}
    out = await _list()
    assert out.count == 3 and namespace["asked"] == [KEY]
    # Twice the ceiling is listed: the newest is read, the next is capped.
    assert [(e.memory_key, e.capped, e.verdict) for e in out.definitions] == [
        (KEY, False, "governed"),
        ("definition/mrr", True, None),
    ]
    assert out.definitions[1].term is None


async def test_a_copy_that_lapses_alone_reads_no_source(namespace: dict[str, Any]) -> None:
    namespace["rows"] = [
        _row(KEY, VALUE, NOW, promoted_from=["o/n/bot/-/-/definition/arr"]),
        _row("definition/x", {"body": "no term"}, NOW - timedelta(days=1)),
    ]
    reasons = [e.lapse_reason for e in (await _list()).definitions]
    assert reasons == ["not_namespace_wide", "no_term"] and namespace["asked"] == []


async def test_a_namespace_with_no_definition(namespace: dict[str, Any]) -> None:
    out = await _list()
    assert (out.count, out.definitions) == (0, [])


@pytest.mark.skipif(CLICKHOUSE is None, reason="clickhouse binary not installed")
async def test_the_store_reads_the_newest_first_with_values_within_the_read() -> None:
    def row(key: str, at: str, memory_type: str = "definition", tomb: str = "NULL") -> str:
        value = json.dumps({"term": key}).replace("'", "\\'")
        return (
            "INSERT INTO loom.shared (org, namespace, memory_key, value, memory_type, "
            "promoted_from, promoted_at, promotion_event, tombstoned_at, version) VALUES "
            f"('o', 'n', '{key}', '{value}', '{memory_type}', ['o/n/-/-/-/{key}'], "
            f"'{at}', generateUUIDv4(), {tomb}, 1)"
        )

    ch = LocalCH(
        ddl("shared"),
        row("definition/a", "2026-01-01 00:00:00"),
        row("definition/b", "2026-01-03 00:00:00"),
        row("definition/c", "2026-01-02 00:00:00"),
        row("definition/gone", "2026-01-04 00:00:00", tomb="'2026-01-05 00:00:00'"),
        row("definition/fact", "2026-01-04 00:00:00", memory_type="semantic"),
        row("note", "2026-01-04 00:00:00"),
    )
    rows = await shared.definition_rows("o", "n", 1, ch=ch)
    assert [(r["memory_key"], bool(r["value"]), r["total"]) for r in rows] == [
        ("definition/b", True, 3),
        ("definition/c", False, 3),
    ]
    assert list(rows[0]["promoted_from"]) == ["o/n/-/-/-/definition/b"]


@pytest.mark.skipif(CLICKHOUSE is None, reason="clickhouse binary not installed")
async def test_a_same_second_tie_is_ordered_as_the_shadow_rule_orders_it() -> None:
    # ClickHouse orders UUIDs by their second half first; the text order is the shadow
    # rule's.
    low, high = "ffffffff-0000-0000-0000-000000000001", "00000000-0000-0001-ffff-000000000000"

    def row(key: str, event: str) -> str:
        return (
            "INSERT INTO loom.shared (org, namespace, memory_key, value, memory_type, "
            "promoted_from, promoted_at, promotion_event, version) VALUES "
            f"('o', 'n', '{key}', '{{}}', 'definition', [], '2026-01-01 00:00:00', '{event}', 1)"
        )

    ch = LocalCH(ddl("shared"), row("definition/a", low), row("definition/b", high))
    rows = await shared.definition_rows("o", "n", 1, ch=ch)
    assert [r["memory_key"] for r in rows] == ["definition/a", "definition/b"]
    # The row the store lists first holds a name both hold.
    a, b = (
        _shared(
            r["memory_key"],
            {"term": "ARR"},
            promoted_at=r["promoted_at"],
            promotion_event=str(r["promotion_event"]),
        )
        for r in rows
    )
    assert lost_names([a, b]) == {"definition/b": [("ARR", "definition/a")]}
