"""`loom namespaces-gc`'s candidate scan, run on clickhouse-local against the real
`loom.memory` and `loom.namespaces`: a prefix is a literal string, so `_` and `%` in it
match only themselves, on the include and the exclude side alike."""

from __future__ import annotations

import pytest

from server.cli.hygiene import _gc_candidates
from tests.unit.clickhouse_local import CLICKHOUSE, LocalCH, ddl

pytestmark = pytest.mark.skipif(CLICKHOUSE is None, reason="clickhouse binary not installed")


def _row(namespace: str) -> str:
    return (
        "INSERT INTO loom.memory (org, namespace, agent, memory_key, memory_type, value,"
        f" created_at) VALUES ('o', '{namespace}', 'a', 'k', 'semantic', 'v',"
        " now() - INTERVAL 2 DAY)"
    )


def _local_ch(*namespaces: str) -> LocalCH:
    return LocalCH(ddl("memory"), ddl("namespaces"), *(_row(n) for n in namespaces))


async def _names(ch: LocalCH, prefixes: tuple[str, ...], exclude: tuple[str, ...] = ()) -> set[str]:
    rows = await _gc_candidates(
        ch,
        org="o",
        prefixes=prefixes,
        idle_hours=24,
        include_registered=False,
        exclude_prefixes=exclude,
    )
    return {r["namespace"] for r in rows}


@pytest.mark.asyncio
async def test_underscore_and_percent_in_a_prefix_match_only_themselves() -> None:
    ch = _local_ch("lme_r1", "lmeXr1", "lme%r2", "lmeYYr2", "other")
    assert await _names(ch, ("lme_r",)) == {"lme_r1"}
    assert await _names(ch, ("lme%r",)) == {"lme%r2"}


@pytest.mark.asyncio
async def test_an_exclude_prefix_fences_off_only_its_own_names() -> None:
    ch = _local_ch("lme_r1", "lmeXr1", "lme-a")
    assert await _names(ch, ("lme",), exclude=("lme_r",)) == {"lmeXr1", "lme-a"}
