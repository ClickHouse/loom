"""The entity follow's key read (`bridge_rows_by_key`), run on clickhouse-local against the
real `loom.memory`: the bridge's rows by key slug, under the request's hard tags, at most
`memory_search_per_key_cap` rows of one key."""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest

from server.memory.app.ask.completion import bridge_rows_by_key
from tests.unit.clickhouse_local import CLICKHOUSE, LocalCH, ddl

pytestmark = pytest.mark.skipif(CLICKHOUSE is None, reason="clickhouse binary not installed")


def _row(session: str, key: str, value: str, tags: str = "[]", version: int = 1) -> str:
    return (
        "INSERT INTO loom.memory (org, namespace, agent, user_id, session_id, memory_key,"
        " memory_type, value, version, status, tags) VALUES ('o', 'n', 'a', 'u',"
        f" '{session}', '{key}', 'semantic', '{value}', {version}, 'active', {tags})"
    )


def _local_ch(*rows: str) -> LocalCH:
    return LocalCH(ddl("memory"), *rows)


def _ask(**kw: Any) -> Any:
    return SimpleNamespace(org="o", namespace="n", agent="a", user_id="u", session_id=None, **kw)


@pytest.mark.asyncio
async def test_the_read_keeps_tagged_rows_and_caps_rows_per_key(monkeypatch) -> None:
    from server.settings import settings

    ch = _local_ch(
        *(
            _row(f"s{i}", "bernard_arnault_citizen_of", "France", "['project:x']", i)
            for i in (1, 2, 3)
        ),
        _row("s4", "bernard_arnault_born_in", "Roubaix", "['project:x']"),
        _row("s5", "bernard_arnault_net_worth", "a lot"),  # off the hard tags
    )
    monkeypatch.setattr("server.core.ch.get_client", lambda: ch)
    monkeypatch.setattr(settings, "memory_search_per_key_cap", 2)
    hits = await bridge_rows_by_key(_ask(tags=["project:x"]), "Bernard Arnault")
    got = sorted((h.memory_key, h.session_id) for h in hits)
    assert got == [
        ("bernard_arnault_born_in", "s4"),
        ("bernard_arnault_citizen_of", "s2"),
        ("bernard_arnault_citizen_of", "s3"),
    ]
    assert all(h._row_tags == ("project:x",) for h in hits)


@pytest.mark.asyncio
async def test_with_only_off_tag_rows_the_read_is_empty(monkeypatch) -> None:
    # empty, so the ask takes the semantic follow, which honours the hard tags
    ch = _local_ch(_row("s1", "bernard_arnault_net_worth", "a lot"))
    monkeypatch.setattr("server.core.ch.get_client", lambda: ch)
    assert await bridge_rows_by_key(_ask(tags=["project:x"]), "Bernard Arnault") == []


@pytest.mark.asyncio
async def test_with_no_cap_one_key_takes_up_to_the_follows_top_k(monkeypatch) -> None:
    from server.settings import settings

    ch = _local_ch(
        *(_row(f"s{i}", "bernard_arnault_citizen_of", "France", version=i) for i in (1, 2, 3))
    )
    monkeypatch.setattr("server.core.ch.get_client", lambda: ch)
    monkeypatch.setattr(settings, "memory_search_per_key_cap", 0)
    hits = await bridge_rows_by_key(_ask(), "Bernard Arnault", top_k=2)
    assert [h.session_id for h in hits] == ["s3", "s2"]


@pytest.mark.asyncio
async def test_rows_of_one_key_at_one_version_keep_one_order(monkeypatch) -> None:
    from server.settings import settings

    ch = _local_ch(*(_row(s, "bernard_arnault_citizen_of", "France") for s in ("s3", "s1", "s2")))
    monkeypatch.setattr("server.core.ch.get_client", lambda: ch)
    monkeypatch.setattr(settings, "memory_search_per_key_cap", 2)
    hits = await bridge_rows_by_key(_ask(), "Bernard Arnault")
    assert [h.session_id for h in hits] == ["s1", "s2"]


@pytest.mark.asyncio
async def test_two_keys_at_one_version_keep_one_order_at_the_limit(monkeypatch) -> None:
    # read in this insert order, only the memory_key tiebreak puts `born_in` first
    ch = _local_ch(
        _row("s1", "bernard_arnault_born_in", "Roubaix"),
        _row("s1", "bernard_arnault_citizen_of", "France"),
    )
    monkeypatch.setattr("server.core.ch.get_client", lambda: ch)
    hits = await bridge_rows_by_key(_ask(), "Bernard Arnault", top_k=1)
    assert [h.memory_key for h in hits] == ["bernard_arnault_born_in"]
