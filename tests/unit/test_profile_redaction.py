"""memory.redact reaches the profiles a model built from the redacted memory.

A profile version names the memories it was built from in its trace
(`profile.fold:<label>:<memory ids>`); redacting one of them scrubs every
version that names it, and leaves the versions built without it. A lock on
the profile key refuses the redact before anything is scrubbed.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

import pytest
from clickhouse_connect.driver.binding import format_bind_value

from tests.unit.clickhouse_local import CLICKHOUSE, ddl, run

_SECRET = "00000000-0000-0000-0000-00000000000a"
_OTHER = "00000000-0000-0000-0000-00000000000b"
_PROFILE = "00000000-0000-0000-0000-0000000000ff"


def _set_params(params: dict[str, Any] | None) -> list[str]:
    """`SET param_<name>` statements, so a statement's parameters stay bound when
    the script replays it before a later query."""
    sets = []
    for name, value in (params or {}).items():
        text = format_bind_value(value).replace("\\", "\\\\").replace("'", "\\'")
        sets.append(f"SET param_{name} = '{text}'")
    return sets


class _StatefulLocalCH:
    """clickhouse-local that keeps every executed statement: each query replays them."""

    def __init__(self, *setup: str) -> None:
        self.setup = list(setup)

    async def execute(self, sql: str, params: dict[str, Any] | None = None) -> None:
        self.setup += [*_set_params(params), sql]

    async def query_all(self, sql: str, params: dict[str, Any] | None = None) -> list[Any]:
        return run([*self.setup, *_set_params(params)], sql)

    async def query_one(self, sql: str, params: dict[str, Any] | None = None) -> Any:
        rows = await self.query_all(sql, params)
        return rows[0] if rows else None


def _profile_version(table: str, version: int, value: str, sources: list[str]) -> str:
    trace = "profile.fold:structured_profile:" + ",".join(sources)
    return (
        f"INSERT INTO loom.{table} (org, namespace, agent, user_id, session_id, memory_key, "
        "memory_type, value, memory_id, content_sha256, version, source_trace) VALUES "
        f"('o', 'n', '-', 'u1', '-', 'structured_profile', 'profile', '{value}', "
        f"'{_PROFILE}', '{'a' * 64}', {version}, '{trace}')"
    )


_TARGET_KW: dict[str, Any] = {
    "namespace": "n",
    "agent": "a",
    "user_id": "u1",
    "session_id": "-",
    "memory_key": "user_timezone",
    "version": 1,
    "tombstoned_at": None,
}


class _Head:
    version = 2
    lock_mode = ""
    lock_until = None


@pytest.mark.skipif(CLICKHOUSE is None, reason="clickhouse binary not installed")
@pytest.mark.asyncio
async def test_redact_scrubs_every_profile_version_built_from_the_memory(monkeypatch) -> None:
    import server.memory.store as memory_repo
    from server.memory.app import curation
    from server.memory.store import redaction
    from server.memory.store.history import HistoryTarget

    built_with = '{"timezone":"secret place"}'
    built_without = '{"timezone":null}'
    rows = [
        _profile_version(table, version, value, sources)
        for table in ("memory", "memory_versions")
        for version, value, sources in (
            (1, built_with, [_SECRET, _OTHER]),
            (2, built_without, [_OTHER]),
        )
    ]
    ch = _StatefulLocalCH(
        ddl("memory"),
        ddl("memory_versions"),
        ddl("memory_embeddings"),
        ddl("memory_chunks"),
        ddl("memory_code_links"),
        *rows,
    )
    monkeypatch.setattr(redaction, "_default_client", lambda: ch)

    async def head(*_a: Any, **_k: Any) -> _Head:
        return _Head()

    async def no_links(*_a: Any) -> str:
        return ""

    monkeypatch.setattr(memory_repo, "get_by_key", head)
    monkeypatch.setattr(curation, "_live_link_inputs", no_links)
    target = HistoryTarget(**_TARGET_KW)
    profiles = await curation._profiles_built_from("o", target, _SECRET)
    await curation._redact_profiles(
        "o", target, profiles, when=datetime(2026, 1, 1), reason_blob="r|admin"
    )

    for table in ("memory", "memory_versions"):
        values = await ch.query_all(
            f"SELECT version, value, redacted_at IS NOT NULL AS redacted FROM loom.{table} "
            "WHERE memory_key = 'structured_profile' ORDER BY version"
        )
        assert [(int(r["version"]), r["redacted"]) for r in values] == [(1, 1), (2, 0)]
        assert not any("secret place" in r["value"] for r in values)
        assert values[1]["value"] == built_without
    # A second redaction finds nothing left to scrub.
    assert await memory_repo.profile_versions_built_from("o", "n", "u1", _SECRET) == []


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["read_only", "no_delete"])
async def test_a_locked_profile_refuses_the_redact_before_any_scrub(monkeypatch, mode) -> None:
    from fastapi import HTTPException

    import server.memory.store as memory_repo
    from server.memory.app import curation
    from server.memory.store.history import HistoryTarget

    async def versions(*_a: Any) -> list[dict[str, Any]]:
        return [
            {
                "memory_id": _PROFILE,
                "version": 1,
                "agent": "-",
                "session_id": "-",
                "memory_key": "structured_profile",
            }
        ]

    class _Locked(_Head):
        lock_mode = mode

    async def head(*_a: Any, **_k: Any) -> _Locked:
        return _Locked()

    async def no_scrub(*_a: Any, **_k: Any) -> None:
        raise AssertionError("nothing is scrubbed under a lock")

    monkeypatch.setattr(memory_repo, "profile_versions_built_from", versions)
    monkeypatch.setattr(memory_repo, "get_by_key", head)
    monkeypatch.setattr(memory_repo, "redact_row", no_scrub)
    with pytest.raises(HTTPException) as e:
        await curation._profiles_built_from("o", HistoryTarget(**_TARGET_KW), _SECRET)
    assert e.value.status_code == 423
