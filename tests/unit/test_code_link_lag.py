"""The `memory_links_behind` health warning: a repository whose memory links stay at an
older commit than its indexed head for longer than a link pass may take.

Each process keeps when it first saw a repository behind; the health probe reads that
snapshot. The reads are faked and time goes through the manual clock.
"""

from __future__ import annotations

from collections.abc import Iterator
from typing import Any

import pytest

from server.memory.app import code_link_lag
from server.observability.problems import HealthState, collect_problems, derive_status
from server.settings import settings
from tests.unit.clock_fake import ManualClock

pytestmark = pytest.mark.asyncio

A, B = ("o", "code-a"), ("o", "code-b")


class _Reads:
    """The two reads `refresh` makes: the repositories whose links are not at the indexed
    head (with the head they were checked at), and which of those hold a live link."""

    def __init__(self, unchecked: dict[tuple[str, str], str], linked: set[tuple[str, str]]):
        self.unchecked, self.linked = unchecked, linked
        self.asked: list[list[tuple[str, str]]] = []
        self.fail = False

    async def unchecked_heads(self, ch: Any = None) -> dict[tuple[str, str], str]:
        if self.fail:
            raise TimeoutError("read timed out")
        return dict(self.unchecked)

    async def code_namespaces(self, among: Any = None, *, ch: Any = None) -> list[Any]:
        self.asked.append(list(among))
        return [entry for entry in among if entry in self.linked]


@pytest.fixture
def clock(monkeypatch: pytest.MonkeyPatch) -> ManualClock:
    clock = ManualClock(t=1000.0)
    monkeypatch.setattr(code_link_lag.time, "monotonic", clock.monotonic)
    return clock


@pytest.fixture
def reads(monkeypatch: pytest.MonkeyPatch) -> Iterator[_Reads]:
    reads = _Reads({A: "c1"}, {A})
    monkeypatch.setattr(code_link_lag.code, "unchecked_heads", reads.unchecked_heads)
    monkeypatch.setattr(code_link_lag.code, "code_namespaces", lambda: ["code-a", "code-b"])
    monkeypatch.setattr(code_link_lag.store, "code_namespaces", reads.code_namespaces)
    monkeypatch.setattr(settings, "memory_code_links_enabled", True)
    monkeypatch.setattr(settings, "memory_code_links_interval_seconds", 60)
    monkeypatch.setattr(settings, "memory_code_links_max_pass_seconds", 600.0)
    code_link_lag.reset_for_tests()
    yield reads
    code_link_lag.reset_for_tests()


def _problems() -> list[Any]:
    st = HealthState(memory_links_behind=code_link_lag.behind())
    return [p for p in collect_problems(settings, st) if p.code == "memory_links_behind"]


async def test_links_behind_longer_than_a_pass_may_take_are_a_warning(
    reads: _Reads, clock: ManualClock
) -> None:
    assert code_link_lag.behind() is None and _problems() == []  # not read yet
    await code_link_lag.refresh()
    clock.advance(660)  # the interval plus the pass deadline: a pass may still be running
    await code_link_lag.refresh()
    assert code_link_lag.behind() == 0 and _problems() == []
    clock.advance(1)
    (problem,) = _problems()
    assert problem.severity == "warn" and problem.source == "workers"
    assert problem.message.startswith("The memory links of 1 repository have trailed")
    # `/v1/health` answers anyone: a count, never which repository
    assert "code-a" not in problem.message + problem.fix
    assert "memory_code_links" in problem.fix
    # a warning lists the problem and leaves the deployment's status alone
    assert derive_status(True, [problem]) == "ok"


async def test_a_disabled_worker_raises_nothing(
    reads: _Reads, clock: ManualClock, monkeypatch: pytest.MonkeyPatch
) -> None:
    """With linking off nothing follows the links, by configuration: the index moving on
    is not a fault, and no read is made."""
    await code_link_lag.refresh()
    clock.advance(10_000)
    assert code_link_lag.behind() == 1
    monkeypatch.setattr(settings, "memory_code_links_enabled", False)
    reads.asked.clear()
    await code_link_lag.refresh()
    assert code_link_lag.behind() is None and _problems() == [] and reads.asked == []


async def test_only_a_repository_holding_a_live_link_can_be_behind(
    reads: _Reads, clock: ManualClock
) -> None:
    """A repository with no link is never rechecked, so its mark never moves: not lag.
    The link table is asked about the repositories that differ, and not at all when none
    does."""
    reads.unchecked = {A: "c1", B: ""}
    await code_link_lag.refresh()
    clock.advance(5_000)
    assert code_link_lag.behind() == 1 and reads.asked == [[A, B]]
    reads.unchecked = {}
    await code_link_lag.refresh()
    assert code_link_lag.behind() == 0 and len(reads.asked) == 1


async def test_links_that_move_on_start_over_and_caught_up_ones_clear(
    reads: _Reads, clock: ManualClock
) -> None:
    await code_link_lag.refresh()
    clock.advance(700)
    await code_link_lag.refresh()
    assert code_link_lag.behind() == 1  # still at c1 while the index moved on
    reads.unchecked = {A: "c2"}  # a pass reached c2; the index is ahead again
    await code_link_lag.refresh()
    assert code_link_lag.behind() == 0
    clock.advance(700)
    assert code_link_lag.behind() == 1  # and stayed at c2 past the grace
    reads.unchecked = {}
    await code_link_lag.refresh()
    assert code_link_lag.behind() == 0 and _problems() == []


async def test_a_failed_read_keeps_the_last_snapshot(reads: _Reads, clock: ManualClock) -> None:
    await code_link_lag.refresh()
    clock.advance(700)
    reads.fail = True
    await code_link_lag.refresh()  # never raises
    assert code_link_lag.behind() == 1


async def test_a_deployment_that_indexes_no_repository_reads_nothing(
    reads: _Reads, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The memory plane alone: no watermark is read and nothing can be behind."""

    async def unread(ch: Any = None) -> dict[tuple[str, str], str]:
        raise AssertionError("read with no repository configured")

    monkeypatch.setattr(code_link_lag.code, "code_namespaces", lambda: [])
    monkeypatch.setattr(code_link_lag.code, "unchecked_heads", unread)
    await code_link_lag.refresh()
    assert code_link_lag.behind() == 0 and _problems() == []


async def test_an_unread_registry_keeps_the_snapshot_as_it_was(
    reads: _Reads, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Which repositories are configured is not known yet: nothing is read or reported."""
    from server.code.domain.index import repos

    monkeypatch.setattr(settings, "code_self_service_enabled", True)
    monkeypatch.setattr(repos, "_registry_loaded", False)
    await code_link_lag.refresh()
    assert code_link_lag.behind() is None and reads.asked == []


async def test_the_unchecked_heads_are_the_configured_trunks_past_their_mark(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from server.code.app import memory_links

    branch = ("o", "code-a-26-1")

    class _Store:
        def __init__(self, ch: Any = None) -> None:
            pass

        async def state_values_for_keys(self, keys: list[str]) -> dict[str, dict[Any, str]]:
            assert keys == ["git_head", "memory_links_checked"]
            heads = {A: "h2", B: "h9", ("o", "code-new"): "h1", ("o", "gone"): "h5", branch: "h7"}
            return {
                "git_head": heads,
                "memory_links_checked": {A: "h1", B: "h9", ("o", "gone"): "h4"},
            }

    def trunk(org: str, namespace: str) -> str:
        # `gone` is no longer configured; the release branch's links are its trunk's
        return {"gone": "", branch[1]: "code-a"}.get(namespace, namespace)

    monkeypatch.setattr(memory_links, "CodeRepo", _Store)
    monkeypatch.setattr(memory_links, "trunk_namespace", trunk)
    # behind its head, and never checked; not the one at its head, the branch namespace
    # or the repository no longer configured
    assert await memory_links.unchecked_heads() == {A: "h1", ("o", "code-new"): ""}
