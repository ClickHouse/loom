"""The observation buffer also flushes on size, not only on
an explicit observe.crystallize."""

from __future__ import annotations

import asyncio
from typing import Any

import pytest


class _CH:
    def __init__(self, ready: list[dict[str, Any]], big: list[dict[str, Any]]) -> None:
        self.ready, self.big = ready, big
        self.calls: list[tuple[str, dict[str, Any]]] = []

    async def query_all(
        self, sql: str, params: dict[str, Any] | None = None
    ) -> list[dict[str, Any]]:
        self.calls.append((" ".join(sql.split()), dict(params or {})))
        return self.big if "HAVING" in sql else self.ready


def _sess(sid: str, **extra: Any) -> dict[str, Any]:
    return {
        "org": "o",
        "namespace": "ns",
        "agent": "a",
        "user_id": "-",
        "session_id": sid,
        "n": 12,
        "last_at": "t",
        **extra,
    }


def test_ready_sessions_come_first_and_buffer_sessions_fill_up_to_the_cap(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from server.memory.workers.observe_extract import ObserveExtractConfig, find_ready_sessions

    ch = _CH(
        ready=[_sess("s1")],
        big=[_sess("s1", chars=9000), _sess("s2", chars=8000), _sess("s3", chars=7000)],
    )
    cfg = ObserveExtractConfig(
        max_sessions_per_pass=2, buffer_chars=6000, buffer_min_observations=8
    )
    out = asyncio.run(find_ready_sessions(cfg, ch=ch))
    assert [(s["session_id"], s["trigger"]) for s in out] == [("s1", "ready"), ("s2", "buffer")]
    assert len(ch.calls) == 2
    sql, params = ch.calls[1]
    assert "HAVING chars >= {buf_chars:UInt64} AND n >= {buf_min:UInt32}" in sql
    assert "sum(length(value)) AS chars" in sql and "ready_tag" not in sql
    assert params["buf_chars"] == 6000 and params["buf_min"] == 8 and "ready_tag" not in params


def test_buffer_disabled_by_default_issues_one_query(monkeypatch: pytest.MonkeyPatch) -> None:
    from server.memory.workers.observe_extract import ObserveExtractConfig, find_ready_sessions
    from server.settings import settings

    monkeypatch.setattr(settings, "observe_extract_buffer_chars", 0)
    ch = _CH(ready=[_sess("s1")], big=[_sess("s9", chars=99999)])
    out = asyncio.run(find_ready_sessions(ObserveExtractConfig(), ch=ch))
    assert [s["session_id"] for s in out] == ["s1"] and len(ch.calls) == 1
    # setting drives the threshold when the config leaves it None
    monkeypatch.setattr(settings, "observe_extract_buffer_chars", 5000)
    ch = _CH(ready=[], big=[_sess("s9", chars=99999)])
    out = asyncio.run(find_ready_sessions(ObserveExtractConfig(), ch=ch))
    assert [(s["session_id"], s["trigger"]) for s in out] == [("s9", "buffer")]


def test_buffer_triggered_session_loads_every_undone_observation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from server.memory import store as memory_repo
    from server.memory.workers.observe_extract import (
        ObserveExtractConfig,
        _load_session_observations,
    )

    class _LoadCH:
        def __init__(self) -> None:
            self.params: dict[str, Any] = {}
            self.sql = ""

        async def query_all(
            self, sql: str, params: dict[str, Any] | None = None
        ) -> list[dict[str, Any]]:
            self.sql, self.params = " ".join(sql.split()), dict(params or {})
            self.calls += 1
            stored = memory_repo.build_memory_version_row(
                memory_repo.MemoryRow.from_snapshot({"memory_key": "k1"}, org="o", namespace="n")
            )
            return [{**stored, "value_text": stored["value"]}]

        calls = 0

    ch = _LoadCH()
    rows = asyncio.run(
        _load_session_observations(_sess("s2", trigger="buffer"), ObserveExtractConfig(), ch)
    )
    # one statement, full rows, no per-key re-read
    assert ch.calls == 1 and [r.memory_key for r in rows] == ["k1"]
    assert ch.sql.startswith(" ".join(memory_repo._MEMORY_FINAL_PROJECTION.split()))
    assert "(has(tags, {ready_tag:String}) OR {buffer_trigger:UInt8} = 1)" in ch.sql
    assert ch.params["buffer_trigger"] == 1
    asyncio.run(
        _load_session_observations(_sess("s1", trigger="ready"), ObserveExtractConfig(), ch)
    )
    assert ch.params["buffer_trigger"] == 0
