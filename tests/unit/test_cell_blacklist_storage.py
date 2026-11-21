"""Global blacklist admission accepts the real engine clause and configured catalog."""

from __future__ import annotations

import time

import pytest

from server.core.ch_bounded import BoundedReadError
from server.memory.store import blacklist
from server.settings import settings


def catalog(engine_full, database="loom"):
    async def rows(sql, params, **kwargs):
        if database != "loom" and params.get("database") != database:
            return []
        if "system.tables" in sql:
            return [
                {
                    "engine": engine_full.split("(", 1)[0],
                    "engine_full": engine_full,
                    "sorting_key": "org, namespace, pattern_id",
                }
            ]
        return [{"name": name, "type": kind} for name, kind in blacklist._COLUMN_TYPES.items()]

    return rows


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "engine",
    [
        "ReplacingMergeTree(created_at) ORDER BY (org, namespace, pattern_id) SETTINGS index_granularity = 8192",
        "ReplicatedReplacingMergeTree('/tables/blacklist', 'replica', created_at) ORDER BY (org, namespace, pattern_id) SETTINGS index_granularity = 8192",
        "SharedReplacingMergeTree('/tables/black(list)', 'replica', created_at) ORDER BY (org, namespace, pattern_id)",
    ],
)
async def test_real_engine_full_admits_supported_global_actions(monkeypatch, engine):
    monkeypatch.setattr(blacklist, "read_rows", catalog(engine))
    await blacklist.check_bounded_storage(ch=object(), deadline=time.monotonic() + 30)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "engine",
    [
        "ReplacingMergeTree(hit_count) ORDER BY (org, namespace, pattern_id)",
        "ReplicatedReplacingMergeTree('/tables/created_at)', 'replica', hit_count) ORDER BY (org, namespace, pattern_id)",
    ],
)
async def test_another_replacement_version_never_admits_global_actions(monkeypatch, engine):
    monkeypatch.setattr(blacklist, "read_rows", catalog(engine))
    with pytest.raises(BoundedReadError, match="Unsupported blacklist storage"):
        await blacklist.check_bounded_storage(ch=object(), deadline=time.monotonic() + 30)


@pytest.mark.asyncio
async def test_storage_admission_reads_the_configured_database(monkeypatch):
    monkeypatch.setattr(settings, "ch_database", "private_cell")
    monkeypatch.setattr(
        blacklist, "read_rows", catalog("ReplacingMergeTree(created_at)", "private_cell")
    )
    await blacklist.check_bounded_storage(ch=object(), deadline=time.monotonic() + 30)
