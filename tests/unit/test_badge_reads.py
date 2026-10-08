"""The two sidebar badge reads do not scan.

A badge needs a number, not rows: `conflicts.list(count_only)` answers it
from one FINAL count with no value column, cached 60 s per namespace and
dropped by a resolve. `agents.connected` looks back 30 days for
`first_seen` instead of scanning the org's whole event history."""

from __future__ import annotations

from typing import Any

import pytest

import server.insights.app.agents as agents_mod
import server.memory.app.conflicts_ops as conflicts_mod


class _FakeConflictsRepo:
    def __init__(self) -> None:
        self.counts = 0
        self.lists = 0

    async def count_conflicted(self, org: str, namespace: str) -> int:
        self.counts += 1
        return 4

    async def list_conflicted(self, org: str, namespace: str, **_: Any) -> list[dict[str, Any]]:
        self.lists += 1
        return []


@pytest.fixture(autouse=True)
def _reset():
    conflicts_mod.reset_conflicts_count_cache()
    agents_mod.reset_connected_cache()
    yield
    conflicts_mod.reset_conflicts_count_cache()
    agents_mod.reset_connected_cache()


@pytest.mark.asyncio
async def test_count_only_answers_the_number_from_one_cached_count(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from server.memory.api.models_conflicts import ConflictsListInput
    from server.memory.store import conflicts as conflicts_repo

    fake = _FakeConflictsRepo()
    monkeypatch.setattr(conflicts_repo, "count_conflicted", fake.count_conflicted)
    monkeypatch.setattr(conflicts_repo, "list_conflicted", fake.list_conflicted)

    out1 = await conflicts_mod.handle_conflicts_list(
        ConflictsListInput(org="acme", namespace="team", count_only=True)
    )
    out2 = await conflicts_mod.handle_conflicts_list(
        ConflictsListInput(org="acme", namespace="team", count_only=True)
    )
    assert (
        (out1.total, out2.total) == (4, 4) and out1.rows == [] and out1.next_before_version is None
    )
    assert fake.counts == 1, "one count per namespace per minute serves every badge tick"
    assert fake.lists == 0, "no row (and no value column) is read for a number"

    # another namespace is its own count; a resolve drops the cached one
    await conflicts_mod.handle_conflicts_list(
        ConflictsListInput(org="acme", namespace="other", count_only=True)
    )
    assert fake.counts == 2
    conflicts_mod._count_cache.pop(("acme", "team"), None)
    await conflicts_mod.handle_conflicts_list(
        ConflictsListInput(org="acme", namespace="team", count_only=True)
    )
    assert fake.counts == 3


@pytest.mark.asyncio
async def test_the_listing_path_is_unchanged_without_count_only(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from server.memory.api.models_conflicts import ConflictsListInput
    from server.memory.store import conflicts as conflicts_repo

    fake = _FakeConflictsRepo()
    monkeypatch.setattr(conflicts_repo, "count_conflicted", fake.count_conflicted)
    monkeypatch.setattr(conflicts_repo, "list_conflicted", fake.list_conflicted)
    out = await conflicts_mod.handle_conflicts_list(
        ConflictsListInput(org="acme", namespace="team")
    )
    assert out.rows == [] and out.total is None and fake.lists == 1 and fake.counts == 0


def test_count_conflicted_reads_no_value_column() -> None:
    import inspect

    from server.memory.store import conflicts as conflicts_repo

    src = inspect.getsource(conflicts_repo.count_conflicted)
    assert "count()" in src and "FINAL" in src
    assert "value" not in src.split('"""')[3], "the count statement selects no value column"


@pytest.mark.asyncio
async def test_agents_connected_bounds_first_seen_to_the_lookback(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from server.insights.api.models_agents import AgentsConnectedInput

    seen: dict[str, Any] = {}

    class _FakeCH:
        async def query_all(
            self, sql: str, params: dict[str, Any] | None = None
        ) -> list[dict[str, Any]]:
            seen["sql"], seen["params"] = sql, dict(params or {})
            return []

    monkeypatch.setattr(agents_mod, "get_client", lambda: _FakeCH())
    out = await agents_mod.handle_agents_connected(
        AgentsConnectedInput(org="acme", namespace="team")
    )
    assert out.rows == []
    cte = seen["sql"].split("SELECT")[2] if "WITH all_time" in seen["sql"] else ""
    assert "toIntervalDay({lb:UInt16})" in seen["sql"], "first_seen is bounded, not all-time"
    assert seen["params"]["lb"] == agents_mod._FIRST_SEEN_LOOKBACK_DAYS == 30
    # the bound sits inside the first_seen CTE, before the window subquery
    assert seen["sql"].index("toIntervalDay({lb:UInt16})") < seen["sql"].index(
        "max(ts) AS last_seen"
    ), cte
