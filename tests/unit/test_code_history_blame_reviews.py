"""`code.history` and `code.blame_range` PR rows carry what
reviewers said about the code (one batched `code_pr_reviews` read, bounded,
humans only, line overlap for blame, `reviews_per_pr=0` omits), a blamed
commit maps to the OLDEST first-parent merge that contains it (every commit
of a multi-commit PR is credited — checked on a real synthetic repo), the
squash path is unchanged, and the code.ask cache threshold is per model."""

from __future__ import annotations

import os
import subprocess
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

import server.code.app.blame as h
import server.code.app.symbol.history as hs
from server.code.api.models_history import CodeBlameRangeInput, CodeHistoryInput
from server.code.app import answers_cache, answers_entry
from server.code.app.index import pr_reviews as pr
from server.code.domain.index import blame_range as br
from server.code.store.index import CodeRepo

ORG, NS, PATH = "clickhouse", "code-clickhouse", "src/Interpreters/SpillingHashJoin.cpp"
T = datetime(2026, 9, 1, tzinfo=UTC)


class _CH:
    def __init__(self, rows: list[dict[str, Any]] | None = None, *, fail: bool = False) -> None:
        self.rows = rows or []
        self.fail = fail
        self.calls: list[tuple[str, dict[str, Any]]] = []

    async def query_all(
        self, sql: str, params: dict[str, Any] | None = None
    ) -> list[dict[str, Any]]:
        self.calls.append((sql, dict(params or {})))
        if self.fail:
            raise RuntimeError("ch down")
        return list(self.rows)


def _row(
    n: int,
    cid: int,
    author: str,
    line: int,
    *,
    in_range: int = 1,
    on_file: int = 1,
    body: str = "Please keep the byte threshold check first.",
) -> dict[str, Any]:
    return {
        "pr_number": n,
        "comment_id": cid,
        "path": PATH,
        "line": line,
        "author": author,
        "excerpt": body,
        "comment_created_at": T,
        "on_file": on_file,
        "in_range": in_range,
    }


# ---- the batched read ---------------------------------------------------------


@pytest.mark.asyncio
async def test_one_keyed_read_without_final_for_every_pr() -> None:
    ch = _CH()
    repo = CodeRepo(ch)  # type: ignore[arg-type]
    await repo.review_comments_on_prs(
        ORG,
        NS,
        [3, 1, 2, 2],
        [PATH],
        lines=(170, 190),
        per_pr=3,
        bots=["mergify"],
        bot_suffixes=["[bot]"],
    )
    assert len(ch.calls) == 1
    sql, params = ch.calls[0]
    flat = " ".join(sql.split())
    assert "FINAL" not in flat and "PREWHERE" not in flat
    assert "pr_number IN {nums:Array(UInt32)}" in flat and "path IN {paths:Array(String)}" in flat
    assert "ORDER BY version DESC LIMIT 1 BY pr_number, comment_id" in flat
    assert "is_bot = false" in flat and "LIMIT {limit:UInt32}" in flat
    assert params["nums"] == [1, 2, 3] and (params["lo"], params["hi"]) == (170, 190)
    assert params["per"] == 3 and params["bots"] == ["mergify"] and params["sfx"] == ["[bot]"]
    # a file-level read (history) takes every line
    await repo.review_comments_on_prs(ORG, NS, [1], [PATH])
    assert (ch.calls[1][1]["lo"], ch.calls[1][1]["hi"]) == (0, 4294967295)
    # nothing to ask → no read
    await repo.review_comments_on_prs(ORG, NS, [], [PATH])
    await repo.review_comments_on_prs(ORG, NS, [1], [PATH], per_pr=0)
    assert len(ch.calls) == 2


def test_fold_is_bounded_newest_first_humans_only_and_counts_the_file() -> None:
    rows = [_row(10, i, f"rev{i}", 175, on_file=6) for i in range(5)]
    rows.append(_row(10, 99, "clickhouse-gh[bot]", 176, on_file=6))  # a bot the flag missed
    rows.append(_row(20, 7, "alice", 12, in_range=0, on_file=2))  # on the file, not the range
    rows.append(_row(99, 1, "bob", 175))  # a PR not asked for
    got = pr.fold(rows, [10, 20, 30], per_pr=3)
    assert set(got) == {10, 20, 30}
    reviews, count = got[10]
    assert [r["reviewer"] for r in reviews] == ["rev0", "rev1", "rev2"] and count == 6
    assert set(reviews[0]) == {"reviewer", "excerpt", "path", "line", "created_at"}
    assert reviews[0]["created_at"].startswith("2026-09-01")
    assert got[20] == ([], 2) and got[30] == ([], 0)
    long = pr.fold([_row(1, 1, "a", 1, body="x " * 400)], [1], per_pr=1)[1][0][0]["excerpt"]
    assert len(long) <= pr.EXCERPT_CHARS and long.endswith("…")


@pytest.mark.asyncio
async def test_reviews_for_clamps_skips_and_fails_open() -> None:
    ch = _CH([_row(10, 1, "alice", 175)])
    repo = CodeRepo(ch)  # type: ignore[arg-type]
    assert await pr.reviews_for(repo, ORG, NS, [10], [PATH], per_pr=0) == {}
    assert await pr.reviews_for(repo, ORG, NS, [], [PATH], per_pr=3) == {}
    assert not ch.calls
    got = await pr.reviews_for(repo, ORG, NS, [10], [PATH], per_pr=50, lines=(170, 190))
    assert got is not None and got[10][1] == 1 and ch.calls[0][1]["per"] == pr.MAX_PER_PR
    assert "robot-clickhouse" in ch.calls[0][1]["bots"] and "[bot]" in ch.calls[0][1]["sfx"]
    broken = CodeRepo(_CH(fail=True))  # type: ignore[arg-type]
    assert await pr.reviews_for(broken, ORG, NS, [10], [PATH], per_pr=3) is None


def test_inputs_default_to_three_and_cap_at_ten() -> None:
    assert CodeHistoryInput(path=PATH).reviews_per_pr == 3
    assert CodeBlameRangeInput(path=PATH, start_line=1, end_line=2).reviews_per_pr == 3
    with pytest.raises(ValueError):
        CodeHistoryInput(path=PATH, reviews_per_pr=11)
    with pytest.raises(ValueError):
        CodeBlameRangeInput(path=PATH, start_line=1, end_line=2, reviews_per_pr=-1)


# ---- code.history ---------------------------------------------------------------


class _HistoryRepo:
    def __init__(self) -> None:
        self.review_calls: list[tuple[list[int], list[str], dict[str, Any]]] = []

    async def pr_history_for(
        self, org: str, ns: str, *, paths: list[str], limit: int
    ) -> list[dict[str, Any]]:
        return [
            {"pr_number": n, "title": f"PR {n}", "author": "x", "merged_at": T, "labels": []}
            for n in (30, 20, 10)
        ]

    async def review_comments_on_prs(
        self, org: str, ns: str, nums: list[int], paths: list[str], **kw: Any
    ) -> list[dict[str, Any]]:
        self.review_calls.append((list(nums), list(paths), kw))
        return [_row(20, 1, "alice", 12, on_file=4), _row(20, 2, "dependabot[bot]", 13, on_file=4)]


@pytest.fixture
def history(monkeypatch: pytest.MonkeyPatch) -> _HistoryRepo:
    repo = _HistoryRepo()
    monkeypatch.setattr(hs, "CodeRepo", lambda: repo)
    monkeypatch.setattr(hs, "require_code_intel", lambda: None)
    monkeypatch.setattr(hs, "resolve_code_identity", lambda o, n: (ORG, NS))
    monkeypatch.setattr(hs, "schedule_code_trace", lambda **kw: None)
    return repo


@pytest.mark.asyncio
async def test_history_rows_carry_reviews_from_one_read(history: _HistoryRepo) -> None:
    out = await hs.handle_code_history(CodeHistoryInput(path=PATH, reviews_per_pr=2))
    assert len(history.review_calls) == 1
    nums, paths, kw = history.review_calls[0]
    assert nums == [30, 20, 10] and paths == [PATH] and kw["per_pr"] == 2 and kw["lines"] == (0, 0)
    by = {r["pr_number"]: r for r in out.prs}
    assert by[20]["review_count"] == 4 and [r["reviewer"] for r in by[20]["reviews"]] == ["alice"]
    assert by[30]["reviews"] == [] and by[30]["review_count"] == 0


@pytest.mark.asyncio
async def test_history_reviews_per_pr_zero_omits_them(history: _HistoryRepo) -> None:
    out = await hs.handle_code_history(CodeHistoryInput(path=PATH, reviews_per_pr=0))
    assert not history.review_calls
    assert all("reviews" not in r and "review_count" not in r for r in out.prs)


@pytest.mark.asyncio
async def test_history_paths_are_repo_relative_and_in_a_stable_order(
    history: _HistoryRepo, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A `./`-prefixed path reads as the index stores it, and the paths a name resolves
    to go out sorted, not in the process's set order."""
    out = await hs.handle_code_history(CodeHistoryInput(path="./" + PATH, reviews_per_pr=0))
    assert out.paths == [PATH]

    async def _seeds(*a: Any, **k: Any) -> list[dict[str, Any]]:
        return [{"path": "src/z.cpp"}, {"path": "src/a.cpp"}, {"path": "src/z.cpp"}]

    monkeypatch.setattr(hs, "_resolve_symbols", _seeds)
    out = await hs.handle_code_history(CodeHistoryInput(name="DB::f", reviews_per_pr=0))
    assert out.paths == ["src/a.cpp", "src/z.cpp"]


# ---- code.blame_range: reviews on the range --------------------------------------


@pytest.mark.asyncio
async def test_blame_candidates_get_reviews_overlapping_the_range() -> None:
    calls: list[dict[str, Any]] = []

    class _Repo:
        async def review_comments_on_prs(
            self, org: str, ns: str, nums: list[int], paths: list[str], **kw: Any
        ) -> list[dict[str, Any]]:
            calls.append({"nums": nums, "paths": paths, **kw})
            return [
                _row(100, 1, "alice", 180, on_file=3),
                _row(200, 2, "bob", 12, in_range=0, on_file=1),
            ]

    cands = [
        br.Candidate(pr_number=100, lines_blamed=5),
        br.Candidate(pr_number=200, lines_blamed=1),
    ]
    await h._attach_reviews(_Repo(), ORG, NS, PATH, (170, 190), cands, 3)  # type: ignore[arg-type]
    assert len(calls) == 1 and calls[0]["lines"] == (170, 190) and calls[0]["nums"] == [100, 200]
    d100, d200 = cands[0].as_dict(), cands[1].as_dict()
    assert d100["review_count"] == 3 and d100["reviews"][0]["line"] == 180
    assert d200["reviews"] == [] and d200["review_count"] == 1
    # reviews_per_pr=0: no read, no keys
    quiet = [br.Candidate(pr_number=100, lines_blamed=5)]
    await h._attach_reviews(_Repo(), ORG, NS, PATH, (170, 190), quiet, 0)  # type: ignore[arg-type]
    assert len(calls) == 1 and "reviews" not in quiet[0].as_dict()


# ---- code.blame_range: every commit of a PR, on a real repository ----------------


def _git(repo: Path, *args: str, when: int) -> str:
    env = {
        **os.environ,
        "GIT_AUTHOR_NAME": "t",
        "GIT_AUTHOR_EMAIL": "t@x",
        "GIT_COMMITTER_NAME": "t",
        "GIT_COMMITTER_EMAIL": "t@x",
        "GIT_AUTHOR_DATE": f"{1_700_000_000 + when} +0000",
        "GIT_COMMITTER_DATE": f"{1_700_000_000 + when} +0000",
        "GIT_CONFIG_GLOBAL": os.devnull,
        "GIT_CONFIG_SYSTEM": os.devnull,
    }
    return subprocess.run(
        ["git", "-C", str(repo), *args], check=True, capture_output=True, text=True, env=env
    ).stdout.strip()


def _edit(repo: Path, changes: dict[int, str]) -> None:
    f = repo / "file.cpp"
    lines = f.read_text().splitlines()
    for n, text in changes.items():
        lines[n - 1] = text
    f.write_text("\n".join(lines) + "\n")


def _commit(repo: Path, changes: dict[int, str], msg: str, when: int) -> str:
    _edit(repo, changes)
    _git(repo, "commit", "-qam", msg, when=when)
    return _git(repo, "rev-parse", "HEAD", when=when)


@pytest.fixture
def synthetic(tmp_path: Path) -> dict[str, str]:
    """master: base → M10 (`Merge pull request #10`, a three-commit branch
    f1-f2-f3) → M11 (`#11`, a stacked branch s1 cut from f1, merged AFTER
    #10 with the shorter path from f1) → a squash commit `(#12)` → a direct
    push naming no PR."""
    repo = tmp_path / "r"
    repo.mkdir()
    _git(repo, "init", "-q", "-b", "master", when=0)
    (repo / "file.cpp").write_text("".join(f"int l{i} = {i};\n" for i in range(1, 11)))
    _git(repo, "add", "file.cpp", when=0)
    _git(repo, "commit", "-qm", "base", when=0)
    base = _git(repo, "rev-parse", "HEAD", when=0)
    _git(repo, "checkout", "-qb", "feature", when=1)
    f1 = _commit(
        repo, {1: "int a1 = 1;", 2: "int a2 = 2;", 3: "int a3 = 3;"}, "Initial implementation", 1
    )
    f2 = _commit(repo, {4: "int a4 = 4;", 5: "int a5 = 5;"}, "Use it for multithreaded queries", 2)
    f3 = _commit(repo, {6: "int a6 = 6;", 7: "}"}, "Unified printing", 3)
    _git(repo, "checkout", "-qb", "stacked", f1, when=4)
    s1 = _commit(repo, {9: "int s9 = 9;"}, "Stacked follow-up", 4)
    _git(repo, "checkout", "-q", "master", when=5)
    _git(
        repo,
        "merge",
        "-q",
        "--no-ff",
        "feature",
        "-m",
        "Merge pull request #10 from org/feature",
        when=5,
    )
    _git(
        repo,
        "merge",
        "-q",
        "--no-ff",
        "stacked",
        "-m",
        "Merge pull request #11 from org/stacked",
        when=6,
    )
    sq = _commit(repo, {8: "int q8 = 8;"}, "Tune the eighth line (#12)", 7)
    direct = _commit(repo, {10: "int d10 = 10;"}, "tweak", 8)
    return {
        "git_dir": str(repo / ".git"),
        "base": base,
        "to": direct,
        "f1": f1,
        "f2": f2,
        "f3": f3,
        "s1": s1,
        "squash": sq,
        "direct": direct,
    }


async def _attribution(s: dict[str, str]) -> tuple[br.Attribution, br.WindowGraph]:
    h.reset_cache_for_tests()
    w = h.Window(base_sha=s["base"], to_sha=s["to"], ok=True)
    leg = await h._blame_leg(s["git_dir"], "file.cpp", w, 1, 10)
    assert leg.ok, leg.error
    return br.attribute(leg.lines, leg.graph, "file.cpp"), leg.graph


@pytest.mark.asyncio
async def test_every_commit_of_a_multi_commit_pr_is_credited(synthetic: dict[str, str]) -> None:
    att, graph = await _attribution(synthetic)
    children = br.children_of(graph)
    m10 = next(c for c in graph.chain if graph.subjects[c].startswith("Merge pull request #10"))
    # f1 is also an ancestor of the later #11 merge, through the shorter path
    for c in ("f1", "f2", "f3"):
        assert br.merge_of(synthetic[c], graph, children) == m10
    assert (
        att.lines[10] == 6 and att.touched[10] == 7
    )  # line 7 is a brace: in range, not substantive
    assert set(att.commits[10]) == {synthetic["f1"], synthetic["f2"], synthetic["f3"]}
    assert att.lines[11] == 1 and att.commits[11] == [synthetic["s1"]]
    assert att.unmapped == 1 and "1 blamed line(s)" in br.unmapped_note(att)
    cands = {c.pr_number: c for c in br.merge_legs(att, [], range_len=10, branch_points=[])}
    assert cands[10].lines_blamed == 6 and cands[10].as_dict()["lines_in_range"] == 7
    assert len(cands[10].as_dict()["commits"]) == 3 and cands[10].merge_commit_sha == m10


@pytest.mark.asyncio
async def test_squash_commit_is_its_own_pr_unchanged(synthetic: dict[str, str]) -> None:
    att, graph = await _attribution(synthetic)
    # the squash commit sits on the first-parent chain: it maps to itself,
    # its `(#12)` subject names the PR (or the ledger's merge_commit_sha does)
    assert br.merge_of(synthetic["squash"], graph, br.children_of(graph)) == synthetic["squash"]
    assert att.lines[12] == 1 and att.merge_sha[12] == synthetic["squash"]
    leg = await h._blame_leg(
        synthetic["git_dir"],
        "file.cpp",
        h.Window(base_sha=synthetic["base"], to_sha=synthetic["to"], ok=True),
        1,
        10,
    )
    via_ledger = br.attribute(leg.lines, leg.graph, "file.cpp", {synthetic["direct"]: 13})
    assert via_ledger.lines[13] == 1 and via_ledger.unmapped == 0
    assert br.unmapped_note(via_ledger) == ""


def test_merge_walk_keeps_the_oldest_chain_merge() -> None:
    # A → M_new directly (one hop) and A → B → C → M_old (three hops): the
    # older merge brought A in
    a, b, c, m_old, m_new, base = "a" * 40, "b" * 40, "c" * 40, "1" * 40, "2" * 40, "0" * 40
    rev = "\n".join(
        [
            f"commit {m_new} {m_old} {a}",
            "5\tMerge pull request #2 from x/late",
            f"commit {m_old} {base} {c}",
            "4\tMerge pull request #1 from x/early",
            f"commit {c} {b}",
            "3\tc",
            f"commit {b} {a}",
            "2\tb",
            f"commit {a} {base}",
            "1\ta",
        ]
    )
    g = br.parse_graph(rev, m_new)
    assert g.chain == [m_new, m_old]
    assert br.merge_of(a, g, br.children_of(g)) == m_old


# ---- code.ask: the cache threshold is per model ---------------------------------


def test_cache_threshold_is_per_model_with_an_override(monkeypatch: pytest.MonkeyPatch) -> None:
    from server.settings import settings

    monkeypatch.setattr(settings, "code_ask_cache_threshold", 0.0)
    assert answers_entry.cache_hit_threshold("text-embedding-3-small") == 0.87
    # with the identifier guard, 3-large's measured threshold is 0.85
    assert answers_entry.cache_hit_threshold("text-embedding-3-large") == 0.85
    assert (
        answers_entry.cache_hit_threshold("fake-ngram-1024") == 0.87
    )  # unlisted: the strictest measured
    monkeypatch.setattr(settings, "code_ask_cache_threshold", 0.93)
    assert answers_entry.cache_hit_threshold("text-embedding-3-large") == 0.93
    assert answers_entry.cache_hit_threshold("text-embedding-3-small") == 0.93


@pytest.mark.asyncio
async def test_cache_lookup_applies_the_models_threshold(monkeypatch: pytest.MonkeyPatch) -> None:
    from server.settings import settings

    monkeypatch.setattr(settings, "code_ask_cache_threshold", 0.0)
    key = answers_entry.cache_key_for({}, "gpt-4o")
    near = [
        {
            "org": ORG,
            "namespace": NS,
            "question_id": "q",
            "question": "how does TTL drop old parts?",
            "answer": "a",
            "distance": 0.145,
            "cache_key": key.digest(),  # similarity 0.855
            "prompt_version": key.prompt_version,
            "age_s": 0,
        }
    ]
    repo = CodeRepo(_CH(near))  # type: ignore[arg-type]

    def ctx(model: str) -> Any:
        return answers_entry._AskCtx(
            repo=repo,
            org=ORG,
            ns=NS,
            question="how does TTL drop old parts?",
            qvec=[0.1],
            model=model,
            key=key,
            index_head="",
            started=0.0,
            cost_at_start=0.0,
        )

    hit = await answers_cache._semantic(ctx("text-embedding-3-large"), NS, verify_ns=NS, own=True)
    assert hit[0] == "hit"
    miss = await answers_cache._semantic(ctx("text-embedding-3-small"), NS, verify_ns=NS, own=True)
    assert miss == ("miss", None, 0.0)
