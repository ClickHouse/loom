"""`reads.namespaces_of_id` and `reads.get_by_id` on the ClickHouse authority, against
the production `loom.memory` on clickhouse-local."""

from __future__ import annotations

from typing import Any

import pytest

from tests.unit.clickhouse_local import CLICKHOUSE, ddl, run

_ID = "00000000-0000-0000-0000-0000000000ff"


class _LocalCH:
    def __init__(self, *setup: str) -> None:
        self.setup = [ddl("memory"), *setup]

    async def query_all(self, sql: str, params: dict[str, Any] | None = None) -> list[Any]:
        return run(self.setup, sql, params)

    async def query_one(self, sql: str, params: dict[str, Any] | None = None) -> Any:
        rows = run(self.setup, sql, params)
        return rows[0] if rows else None


def _version(
    agent: str, version: int, *, deleted: bool = False, memory_id: str = _ID, user: str = "u1"
) -> str:
    return (
        "INSERT INTO loom.memory (org, namespace, agent, user_id, session_id, memory_key, "
        "memory_type, value, memory_id, version, created_at, tombstoned_at) VALUES "
        f"('o', 'n', '{agent}', '{user}', '-', 'k', 'fact', 'v', '{memory_id}', {version}, "
        f"'2026-01-01 00:00:00', {'now()' if deleted else 'NULL'})"
    )


@pytest.mark.skipif(CLICKHOUSE is None, reason="clickhouse binary not installed")
@pytest.mark.asyncio
async def test_a_memory_id_held_under_two_keys_is_live_while_either_is() -> None:
    """A namespace restored into itself under another agent holds the id under two keys:
    deleting the newer one leaves the namespace holding the id live under the other."""
    from server.memory.store import reads

    ch = _LocalCH(_version("b", 1), _version("a", 2), _version("a", 3, deleted=True))
    assert await reads.namespaces_of_id("o", _ID, strong=False, ch=ch) == ["n"]  # type: ignore[arg-type]
    ch.setup.append(_version("b", 4, deleted=True))
    assert await reads.namespaces_of_id("o", _ID, strong=False, ch=ch) == []  # type: ignore[arg-type]


@pytest.mark.skipif(CLICKHOUSE is None, reason="clickhouse binary not installed")
@pytest.mark.asyncio
async def test_a_key_restored_over_with_another_id_no_longer_holds_it() -> None:
    """A restore writes a snapshot's id over a key without a delete: the key's newest row
    names another memory, so the old id is held nowhere."""
    from server.memory.store import reads

    other = "00000000-0000-0000-0000-0000000000ee"
    ch = _LocalCH(_version("a", 1), _version("a", 2, memory_id=other))
    assert await reads.namespaces_of_id("o", _ID, strong=False, ch=ch) == []  # type: ignore[arg-type]
    assert await reads.namespaces_of_id("o", other, strong=False, ch=ch) == ["n"]  # type: ignore[arg-type]


@pytest.mark.asyncio
async def test_a_miss_is_read_again_with_sequential_consistency(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An id handed out moments ago may not be on the replica the first read reached: a
    miss is read again with sequential consistency."""
    from server.memory.store import reads

    asked: list[str] = []

    class _Lagging:
        async def query_all(self, sql: str, params: dict[str, Any] | None = None) -> list[Any]:
            asked.append(sql)
            return [{"namespace": "n"}] if "select_sequential_consistency" in sql else []

    monkeypatch.setattr(reads, "read_your_writes_enabled", lambda: True)
    assert await reads.namespaces_of_id("o", _ID, strong=False, ch=_Lagging()) == ["n"]  # type: ignore[arg-type]
    assert len(asked) == 2


@pytest.mark.skipif(CLICKHOUSE is None, reason="clickhouse binary not installed")
@pytest.mark.asyncio
async def test_an_id_live_under_two_keys_of_a_namespace_reads_the_newest() -> None:
    """A namespace restored into itself under another agent holds the id live under two
    keys with the same creation time: the newest row is read, as under Postgres."""
    from server.identity.domain.read_scope import WHOLE_NAMESPACE
    from server.memory.store import reads

    # The newer row written first, so storage order alone would pick the older.
    ch = _LocalCH(_version("b", 2), _version("a", 1))
    row = await reads.get_by_id(_ID, org="o", namespace="n", ch=ch, scope=WHOLE_NAMESPACE)  # type: ignore[arg-type]
    assert row is not None and (row.agent, row.version) == ("b", 2)


@pytest.mark.skipif(CLICKHOUSE is None, reason="clickhouse binary not installed")
@pytest.mark.asyncio
async def test_the_newest_copy_read_stays_in_the_callers_scope() -> None:
    """A caller scoped to its own user reads its own copy of the id, not a newer copy
    another user's key holds in the namespace."""
    from server.identity.domain.read_scope import ReadFilters
    from server.memory.store import reads

    ch = _LocalCH(_version("b", 2, user="u2"), _version("a", 1))
    row = await reads.get_by_id(
        _ID,
        org="o",
        namespace="n",
        ch=ch,
        scope=ReadFilters("own_user", "u1", None),  # type: ignore[arg-type]
    )
    assert row is not None and (row.user_id, row.version) == ("u1", 1)


@pytest.mark.asyncio
async def test_a_writers_holder_lookup_reads_current_at_once(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A write acts on the holder it finds: `strong` reads with sequential consistency
    at once, in one statement; a plain read does not."""
    from server.memory.store import reads

    asked: list[str] = []

    class _Recording:
        async def query_all(self, sql: str, params: dict[str, Any] | None = None) -> list[Any]:
            asked.append(sql)
            return [{"namespace": "n"}]

    monkeypatch.setattr(reads, "read_your_writes_enabled", lambda: True)
    assert await reads.namespaces_of_id("o", _ID, strong=True, ch=_Recording()) == ["n"]  # type: ignore[arg-type]
    assert await reads.namespaces_of_id("o", _ID, strong=False, ch=_Recording()) == ["n"]  # type: ignore[arg-type]
    assert ["select_sequential_consistency" in sql for sql in asked] == [True, False]
