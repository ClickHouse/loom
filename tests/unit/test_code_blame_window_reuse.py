"""The blame window `base..to` is derived from the widest graph already read for
`base`, not re-read from git per request. `br.subgraph` must equal a direct
`rev-list base..to` exactly (checked on the ClickHouse mirror: 5 windows from 12.7k
to 148k commits, identical parents / subjects / times / chain), a newer tip adds
only its own commits (`br.merge_graphs`), and concurrent ranges of one bisect run
one `rev-list` between them."""

from __future__ import annotations

import asyncio
from typing import Any

import pytest

from server.code.app import blame as cb
from server.code.domain.index import blame_range as br

# base <- m1 <- m2 <- m3(merge of s2) <- m4 <- m5 ; side branch s1 <- s2 off m1.
# `base` and its ancestors are outside every window.
HISTORY = {
    "m1": ["base"],
    "s1": ["m1"],
    "s2": ["s1"],
    "m2": ["m1"],
    "m3": ["m2", "s2"],
    "m4": ["m3"],
    "m5": ["m4"],
}


def _reachable(to: str) -> set[str]:
    out: set[str] = set()
    stack = [to]
    while stack:
        s = stack.pop()
        if s in out or s not in HISTORY:
            continue
        out.add(s)
        stack.extend(HISTORY[s])
    return out


def _rev_list_text(commits: set[str]) -> str:
    lines = []
    for sha in sorted(commits, reverse=True):
        lines.append(" ".join(["commit", sha, *HISTORY[sha]]))
        lines.append(f"{100 + len(sha)}\tsubject of {sha} (#{len(sha)})")
    return "\n".join(lines)


def _direct(to: str) -> br.WindowGraph:
    return br.parse_graph(_rev_list_text(_reachable(to)), to)


def _same(a: br.WindowGraph, b: br.WindowGraph) -> bool:
    return (a.parents, a.subjects, a.times, a.chain) == (b.parents, b.subjects, b.times, b.chain)


def test_a_window_derived_from_a_wider_one_equals_a_direct_read() -> None:
    wide = _direct("m5")
    for to in ("m1", "m2", "m3", "m4", "m5", "s2"):
        sub = br.subgraph(wide, to)
        assert sub is not None and _same(sub, _direct(to)), to
    assert br.subgraph(wide, "m3").chain == ["m3", "m2", "m1"]  # type: ignore[union-attr]
    assert set(br.subgraph(wide, "m3").parents) == {"m1", "m2", "m3", "s1", "s2"}  # type: ignore[union-attr]


def test_no_derivation_outside_the_graph_or_from_a_truncated_one() -> None:
    wide = _direct("m3")
    assert br.subgraph(wide, "m5") is None
    wide.truncated = True
    assert br.subgraph(wide, "m2") is None


def test_a_newer_tip_adds_only_its_own_commits() -> None:
    older = _direct("m2")
    newer = br.parse_graph(_rev_list_text(_reachable("m5") - _reachable("m2")), "m5")
    assert _same(br.merge_graphs(older, newer, "m5"), _direct("m5"))


@pytest.fixture
def git_calls(monkeypatch: pytest.MonkeyPatch) -> list[tuple[str, ...]]:
    from server.code.app.index import gitrepo

    calls: list[tuple[str, ...]] = []
    cb.reset_cache_for_tests()

    async def run(git_dir: str, *args: str, timeout_s: float = 0) -> str:
        calls.append(args)
        await asyncio.sleep(0.01)  # a real read takes time: concurrent callers overlap
        revs = [a for a in args if not a.startswith("-")][1:]
        tos = [r.split("..")[-1] for r in revs if not r.startswith("^")]
        exclude: set[str] = set()
        for r in revs:
            if r.startswith("^"):
                exclude |= _reachable(r[1:])
        return _rev_list_text(_reachable(tos[0]) - exclude)

    async def is_ancestor(git_dir: str, ancestor: str, descendant: str, **kw: Any) -> bool:
        return ancestor in _reachable(descendant)

    monkeypatch.setattr(gitrepo, "run", run)
    monkeypatch.setattr(gitrepo, "is_ancestor", is_ancestor)
    yield calls
    cb.reset_cache_for_tests()


def _window(to: str) -> Any:
    w = cb.Window()
    w.base_sha, w.to_sha = "base", to
    return w


def test_concurrent_ranges_of_one_bisect_read_the_window_once(
    git_calls: list[tuple[str, ...]],
) -> None:
    async def four() -> list[br.WindowGraph]:
        return await asyncio.gather(*(cb._graph("m.git", _window("m4")) for _ in range(4)))

    graphs = asyncio.run(four())
    assert len([c for c in git_calls if c[0] == "rev-list"]) == 1
    assert all(_same(g, _direct("m4")) for g in graphs)


def test_a_narrower_window_is_derived_and_a_newer_tip_reads_only_its_commits(
    git_calls: list[tuple[str, ...]],
) -> None:
    async def run() -> tuple[br.WindowGraph, br.WindowGraph]:
        await cb._graph("m.git", _window("m4"))
        narrow = await cb._graph("m.git", _window("m2"))
        newer = await cb._graph("m.git", _window("m5"))
        return narrow, newer

    narrow, newer = asyncio.run(run())
    rev_lists = [c for c in git_calls if c[0] == "rev-list"]
    assert len(rev_lists) == 2, "m2 came from memory; m5 read only ^m4"
    assert "^m4" in rev_lists[1]
    assert _same(narrow, _direct("m2")) and _same(newer, _direct("m5"))


def test_only_the_most_recently_used_bases_are_held(git_calls: list[tuple[str, ...]]) -> None:
    async def run() -> None:
        for i in range(cb._WIDE_MAX + 2):
            w = _window("m3")
            w.base_sha = f"base{i}"
            await cb._graph("m.git", w)

    asyncio.run(run())
    assert len(cb._WIDE) == cb._WIDE_MAX
    assert ("m.git", "base0") not in cb._WIDE and ("m.git", f"base{cb._WIDE_MAX + 1}") in cb._WIDE
