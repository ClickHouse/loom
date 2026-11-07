"""An op addressed by a memory id alone acts in the one namespace holding it that the
caller reaches; a namespace restored from another's snapshot keeps the ids."""

from __future__ import annotations

from typing import Any

import pytest

import server.memory.store as memory_repo
from server.memory.app.id_namespace import namespace_for_id
from server.memory.store import reads
from server.operations.base import Role
from tests.unit.clickhouse_local import CLICKHOUSE, ddl, run

pytestmark = pytest.mark.asyncio


def _wire(
    monkeypatch: pytest.MonkeyPatch, holders: list[str], reach: dict[str, bool | None]
) -> list[bool]:
    """`reach`: each holder's verdict, None for a grant that could not be read; returns
    whether the request was marked the server's failure, once it ran."""
    noted: list[bool] = []

    async def _holders(org: str, memory_id: str, **_: Any) -> list[str]:
        return holders

    async def _reach(org: str, ns: str, role: Role) -> bool | None:
        assert role is Role.WRITER
        return reach.get(ns, False)

    monkeypatch.setattr(memory_repo, "namespaces_of_id", _holders)
    monkeypatch.setattr("server.auth.authz.row_reach", _reach)
    monkeypatch.setattr(
        "server.auth.fail_closed.note_answered_unverified", lambda: noted.append(True)
    )
    return noted


async def test_the_one_reachable_holder_answers(monkeypatch: pytest.MonkeyPatch) -> None:
    noted = _wire(monkeypatch, ["a", "b"], {"b": True})
    assert await namespace_for_id("o", "m", Role.WRITER, strong=True) == "b"
    assert noted == []


async def test_two_reachable_holders_answer_none(monkeypatch: pytest.MonkeyPatch) -> None:
    _wire(monkeypatch, ["a", "b"], {"a": True, "b": True})
    assert await namespace_for_id("o", "m", Role.WRITER, strong=True) is None


async def test_an_unverified_holder_is_the_servers_failure_whatever_the_others(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Choosing `b` past an unread grant on `a` could pick the wrong one of two."""
    noted = _wire(monkeypatch, ["a", "b"], {"a": None, "b": True})
    assert await namespace_for_id("o", "m", Role.WRITER, strong=True) is None
    assert noted == [True]


_MID = "11111111-1111-1111-1111-111111111111"


def _version(ns: str, version: int, *, tombstoned: bool = False) -> str:
    gone = "now()" if tombstoned else "NULL"
    return (
        "INSERT INTO loom.memory (org, namespace, agent, user_id, session_id, memory_key, "
        "memory_type, value, memory_id, version, tombstoned_at) VALUES "
        f"('o', '{ns}', 'a', '-', '-', 'k', 'fact', '\"v\"', '{_MID}', {version}, {gone})"
    )


class _LocalCH:
    """`query_all` on clickhouse-local over these rows."""

    def __init__(self, *setup: str) -> None:
        self.setup = [ddl("memory"), *setup]
        self.reads = 0

    async def query_all(self, sql: str, params: dict[str, Any]) -> list[dict[str, Any]]:
        self.reads += 1
        return run(self.setup, sql.strip(), params)


@pytest.mark.skipif(CLICKHOUSE is None, reason="needs the clickhouse binary")
async def test_a_holder_is_a_namespace_whose_newest_version_is_live() -> None:
    ch = _LocalCH(
        _version("live", 1),
        _version("deleted", 1),
        _version("deleted", 2, tombstoned=True),
        _version("revived", 1, tombstoned=True),
        _version("revived", 2),
        _version("other-org", 1).replace("('o',", "('p',"),
    )
    assert await reads.namespaces_of_id("o", _MID, strong=False, ch=ch) == ["live", "revived"]
    assert ch.reads == 1


@pytest.mark.skipif(CLICKHOUSE is None, reason="needs the clickhouse binary")
async def test_a_miss_is_read_again_with_sequential_consistency(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(reads, "read_your_writes_enabled", lambda: True)
    ch = _LocalCH(_version("deleted", 1, tombstoned=True))
    assert await reads.namespaces_of_id("o", _MID, strong=False, ch=ch) == []
    assert ch.reads == 2


async def test_verify_by_id_ignores_the_namespace_mcp_fills_in(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from server.memory.api.models_crud import MemoryVerifyInput
    from server.memory.app import verify

    _wire(monkeypatch, ["team"], {"team": True})
    asked: list[str] = []

    async def _get_by_id(memory_id: str, *, namespace: str, **_: Any) -> Any:
        asked.append(namespace)
        return None

    monkeypatch.setattr(memory_repo, "get_by_id", _get_by_id)
    await verify._resolve_row(MemoryVerifyInput(org="o", memory_id="m", namespace="home"))
    assert asked == ["team"]
