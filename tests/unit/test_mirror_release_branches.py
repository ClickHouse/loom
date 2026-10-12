"""The mirror carries every branch the ledger's merges land on, and the PR
ledger names its target branch.

The fetch of the configured branch is unchanged; the extra branches are
matched by glob against the remote's advertised heads and fetched in one
`fetch --stdin` without `--prune`, only when their tip moved; the ledger's
merge commits the mirror lacks are fetched by sha and pinned; the readers
whose record means "this branch's PRs" carry a predicate that leaves rows
written before the branch column existed as they read today.
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from typing import Any

import pytest

from server.code.app.index import gitrepo, sandbox
from server.code.app.index import pass_budget as ingest_budget
from server.code.app.index import pass_fetch as ingest_fetch
from server.code.domain.index import conventions as cv
from server.code.domain.index import conventions_rules
from server.code.domain.index.repos import configured_repos
from server.code.store import index as cr
from server.settings import settings

# The scalar code repository these tests model (none is configured by default).
pytestmark = pytest.mark.usefixtures("scalar_code_repo")

SHA_A = "b924c3414e5c3f5ff73b8212a71c616150beccee"
SHA_B = "ea96ff89686666b0d511d0b43fde8a6769f367da"
LS_REMOTE = (
    f"{SHA_A}\trefs/heads/master\n"
    f"{SHA_B}\trefs/heads/25.8\n"
    "0" * 40 + "\trefs/heads/25.8-broken-test\n"
    "1" * 40 + "\trefs/heads/26.3\n"
    "2" * 40 + "\trefs/heads/backport/26.3/118740\n"
    "3" * 40 + "\trefs/heads/vortex-format\n"
)


# ------------------------------------------------------------------ globs + refspecs


def _subcommand(argv: tuple[str, ...]) -> tuple[str, ...]:
    """The git arguments after `--git-dir <dir>`, whatever global options precede it."""
    return argv[argv.index("--git-dir") + 2 :]


def test_globs_match_release_branches_and_nothing_else_by_default() -> None:
    tips = gitrepo.parse_ls_remote_heads(LS_REMOTE)
    assert tips["25.8"] == SHA_B and len(tips) == 6
    globs = gitrepo.branch_globs(settings.code_git_extra_branches)
    assert globs == ("2[5-9].*",)
    assert gitrepo.match_branches(list(tips), globs) == ["25.8", "25.8-broken-test", "26.3"]
    assert gitrepo.match_branches(list(tips), gitrepo.branch_globs("backport/*")) == [
        "backport/26.3/118740"
    ]
    assert gitrepo.match_branches(list(tips), ()) == []
    assert gitrepo.branch_globs("") == () and gitrepo.branch_globs(" , ") == ()


def test_only_moved_tips_are_fetched_and_the_configured_branch_is_skipped() -> None:
    remote = gitrepo.parse_ls_remote_heads(LS_REMOTE)
    local = {"master": SHA_A, "25.8": SHA_B, "26.3": "f" * 40}
    got = gitrepo.branches_to_fetch(remote, local, ("2[5-9].*", "master"), skip="master")
    assert got == ["25.8-broken-test", "26.3"]  # 25.8 unchanged, master skipped
    assert gitrepo.branches_to_fetch(remote, remote, ("2[5-9].*",)) == []


def test_fetch_branches_sends_refspecs_on_stdin_without_prune(monkeypatch) -> None:
    seen: dict[str, Any] = {}

    async def _job(argv, **kw):
        seen.update(argv=tuple(argv), env=kw.get("env") or {}, stdin=kw.get("stdin"))
        return sandbox.JobResult(0, b"", b"")

    monkeypatch.setattr(sandbox, "run_job", _job)
    n = asyncio.run(
        gitrepo.fetch_branches(
            "/x.git", "https://github.com/o/r.git", ["25.8", "26.3", "25.8", ""], token="tok"
        )
    )
    assert n == 2
    assert _subcommand(seen["argv"]) == ("fetch", "--stdin", "https://github.com/o/r.git")
    assert "--prune" not in seen["argv"]
    assert seen["stdin"] == b"+refs/heads/25.8:refs/heads/25.8\n+refs/heads/26.3:refs/heads/26.3\n"
    assert "tok" not in " ".join(seen["argv"]) and "GIT_CONFIG_KEY_0" in seen["env"]
    assert asyncio.run(gitrepo.fetch_branches("/x.git", "r", [])) == 0


def test_fetch_shas_pins_each_commit_under_refs_loom_merges(monkeypatch) -> None:
    seen: dict[str, Any] = {}

    async def _job(argv, **kw):
        seen.update(argv=tuple(argv), stdin=kw.get("stdin"))
        return sandbox.JobResult(0, b"", b"")

    monkeypatch.setattr(sandbox, "run_job", _job)
    n = asyncio.run(gitrepo.fetch_shas("/x.git", "r", [SHA_A.upper(), SHA_A, "nope", SHA_B]))
    assert n == 2 and _subcommand(seen["argv"])[:2] == ("fetch", "--stdin")
    assert seen["stdin"] == (
        f"{SHA_A}:refs/loom/merges/{SHA_A}\n{SHA_B}:refs/loom/merges/{SHA_B}\n".encode()
    )


def test_missing_commits_reads_cat_file_batch_check(monkeypatch) -> None:
    class _Proc:
        returncode = 0

        async def communicate(self, stdin=None):
            assert stdin == f"{SHA_A}\n{SHA_B}\n".encode()
            return f"{SHA_A} commit 300\n{SHA_B} missing\n".encode(), b""

    async def _exec(*argv, **kw):
        assert _subcommand(argv)[:2] == ("cat-file", "--batch-check")
        return _Proc()

    monkeypatch.setattr(asyncio, "create_subprocess_exec", _exec)
    assert asyncio.run(gitrepo.missing_commits("/x.git", [SHA_A, SHA_B])) == [SHA_B]
    assert asyncio.run(gitrepo.missing_commits("/x.git", [])) == []


# ------------------------------------------------------------------ repos config


def test_entry_inherits_the_scalar_globs_and_may_override(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "code_git_extra_branches", "2[5-9].*")
    monkeypatch.setattr(
        settings,
        "code_repos_json",
        '[{"namespace": "code-clickhouse"},'
        ' {"namespace": "code-clickhouse-26-3", "git_branch": "26.3", "git_extra_branches": ""}]',
    )
    repos = configured_repos()
    assert repos[0].git_extra_branches == "2[5-9].*"
    assert repos[1].git_extra_branches == ""  # an explicit empty string switches it off
    monkeypatch.setattr(settings, "code_repos_json", "")
    assert configured_repos()[0].git_extra_branches == "2[5-9].*"


# ------------------------------------------------------------------ ingest worker


def _cfg(**kw: Any) -> Any:
    base = {
        "org": "o",
        "namespace": "code-x",
        "label": "o/code-x",
        "repo": "O/R",
        "git_remote": "r",
        "git_branch": "master",
        "github_token": "",
        "git_extra_branches": "2[5-9].*",
    }
    base.update(kw)
    return SimpleNamespace(**base)


def test_extra_fetch_runs_once_per_mirror_per_pass_and_never_fails_the_pass(monkeypatch) -> None:
    calls: list[str] = []

    async def _remote(git_dir, remote, *, token=""):
        calls.append("ls-remote")
        return gitrepo.parse_ls_remote_heads(LS_REMOTE)

    async def _local(git_dir):
        return {"master": SHA_A, "25.8": SHA_B}

    async def _fetch(git_dir, remote, branches, *, token=""):
        calls.append(f"fetch:{','.join(branches)}")
        return len(branches)

    monkeypatch.setattr(gitrepo, "remote_branches", _remote)
    monkeypatch.setattr(gitrepo, "local_branch_shas", _local)
    monkeypatch.setattr(gitrepo, "fetch_branches", _fetch)
    monkeypatch.setattr(settings, "code_ingest_interval_seconds", 120)
    ingest_fetch._EXTRA_FETCHED_AT.clear()
    res = ingest_budget.CodeIngestResult()
    assert asyncio.run(ingest_fetch.fetch_extra_branches(_cfg(), "/m.git", res)) == 2
    assert calls == ["ls-remote", "fetch:25.8-broken-test,26.3"]
    # a second entry on the same mirror within the pass interval skips
    assert (
        asyncio.run(ingest_fetch.fetch_extra_branches(_cfg(namespace="code-y"), "/m.git", res)) == 0
    )
    assert calls == ["ls-remote", "fetch:25.8-broken-test,26.3"]
    # unset globs: byte-identical to the trunk-only pass — nothing is called
    ingest_fetch._EXTRA_FETCHED_AT.clear()
    assert (
        asyncio.run(ingest_fetch.fetch_extra_branches(_cfg(git_extra_branches=""), "/m.git", res))
        == 0
    )
    assert calls == ["ls-remote", "fetch:25.8-broken-test,26.3"] and res.errors == []

    async def _boom(git_dir, remote, *, token=""):
        raise gitrepo.GitError("remote down")

    monkeypatch.setattr(gitrepo, "remote_branches", _boom)
    ingest_fetch._EXTRA_FETCHED_AT.clear()
    assert asyncio.run(ingest_fetch.fetch_extra_branches(_cfg(), "/m.git", res)) == 0
    assert res.errors == ["extra branches fetch: remote down"]


def test_missing_merge_sweep_walks_the_ledger_and_wraps(monkeypatch) -> None:
    state: dict[str, str] = {}
    pages = {0: [(300, SHA_A), (200, SHA_B)], 200: []}
    fetched: list[list[str]] = []

    class _Repo:
        async def get_state(self, org, ns, key):
            return state.get(key, "")

        async def set_state(self, org, ns, key, value, detail=""):
            state[key] = value

        async def merge_shas_page(self, org, ns, *, below_pr=0, limit=500):
            return pages[below_pr]

    async def _missing(git_dir, shas):
        return [s for s in shas if s == SHA_B]

    async def _fetch(git_dir, remote, shas, *, token=""):
        fetched.append(list(shas))
        return len(shas)

    monkeypatch.setattr(gitrepo, "missing_commits", _missing)
    monkeypatch.setattr(gitrepo, "fetch_shas", _fetch)
    monkeypatch.setattr(settings, "code_git_fetch_ledger_merges", True)
    res = ingest_budget.CodeIngestResult()
    assert asyncio.run(ingest_fetch.fetch_missing_merges(_cfg(), "/m.git", _Repo(), res)) == 1
    assert fetched == [[SHA_B]] and state["merge_sweep_below"] == "200"
    # the bottom of the ledger: nothing read, the cursor wraps to the top
    assert asyncio.run(ingest_fetch.fetch_missing_merges(_cfg(), "/m.git", _Repo(), res)) == 0
    assert state["merge_sweep_below"] == "0" and res.errors == []
    monkeypatch.setattr(settings, "code_git_fetch_ledger_merges", False)
    assert asyncio.run(ingest_fetch.fetch_missing_merges(_cfg(), "/m.git", _Repo(), res)) == 0
    assert fetched == [[SHA_B]]


# ------------------------------------------------------------------ the ledger column


def test_github_row_carries_base_ref() -> None:
    from server.clients.github import pr_base_ref

    assert pr_base_ref({"base": {"ref": "backport/26.3/118740"}}) == "backport/26.3/118740"
    assert pr_base_ref({"base": None}) == "" and pr_base_ref({}) == ""


def test_pr_history_row_writes_target_branch_and_defaults_empty() -> None:
    from server.code.store.index.rows import pr_history_row

    row = pr_history_row(
        org="o",
        namespace="n",
        pr_number=1,
        title="t",
        body_excerpt="",
        author="a",
        files=["src/a.cpp"],
        merged_at=None,
        target_branch="26.3",
    )
    assert row["target_branch"] == "26.3"
    plain = pr_history_row(
        org="o",
        namespace="n",
        pr_number=1,
        title="t",
        body_excerpt="",
        author="a",
        files=["src/a.cpp"],
        merged_at=None,
    )
    assert plain["target_branch"] == "" and "target_branch" in cr.CODE_PR_COLS


class _CH:
    def __init__(self) -> None:
        self.sql: list[str] = []
        self.params: list[dict[str, Any]] = []

    async def query_all(self, sql: str, params: Any = None) -> list[dict[str, Any]]:
        self.sql.append(sql)
        self.params.append(dict(params or {}))
        return [
            {
                "path": "tests/queries/0_stateless/04652_a.sql",
                "first_pr": 7,
                "first_merged_at": "2026-01-01 00:00:00",
                "label": "pr-bugfix",
                "prs": 3,
            }
        ]

    async def query_one(self, sql: str, params: Any = None) -> dict[str, Any] | None:
        rows = await self.query_all(sql, params)
        return rows[0] if rows else None


def _repo(ch: _CH) -> Any:
    repo = cr.CodeRepo.__new__(cr.CodeRepo)
    repo._ch = ch  # type: ignore[attr-defined]
    return repo


def test_branch_predicate_keeps_unfilled_rows_and_binds_the_branch() -> None:
    ch = _CH()
    repo = _repo(ch)
    asyncio.run(
        repo.first_owner_of_paths(
            "o", "n", ["tests/queries/0_stateless/04652_a.sql"], branch="master"
        )
    )
    assert "(target_branch = '' OR target_branch = {branch:String})" in ch.sql[-1]
    assert ch.params[-1]["branch"] == "master"
    # no branch → no predicate, byte-identical to the unbranched statement
    asyncio.run(repo.first_owner_of_paths("o", "n", ["tests/queries/0_stateless/04652_a.sql"]))
    assert "target_branch" not in ch.sql[-1]
    asyncio.run(repo.pr_category_census("o", "n", "src/Storages", branch="26.3"))
    assert "target_branch = {branch:String}" in ch.sql[-1] and ch.params[-1]["branch"] == "26.3"
    from datetime import datetime

    asyncio.run(
        repo.pr_history_in_window(
            "o",
            "n",
            "src/a.py",
            since=datetime(2026, 1, 1),
            until=datetime(2026, 2, 1),
            branch="master",
        )
    )
    assert "target_branch = {branch:String}" in ch.sql[-1]
    row = asyncio.run(repo.pr_facts("o", "n", 5))
    assert "merged_at, target_branch" in ch.sql[-1] and row is not None


# ------------------------------------------------------------------ the rules


def test_rules_are_not_applicable_when_the_ledger_says_another_branch() -> None:
    files = ["src/Storages/Foo.cpp"]
    r = conventions_rules.rule_bugfix_has_test(
        files, {}, "Fix a crash in Foo", ["pr-bugfix"], "", off_branch="backport/26.3/118740"
    )
    assert r.verdict == cv.NOT_APPLICABLE
    assert (
        r.evidence[0]["target_branch"] == "backport/26.3/118740" and "not this namespace" in r.note
    )
    on = conventions_rules.rule_bugfix_has_test(files, {}, "Fix a crash in Foo", ["pr-bugfix"], "")
    assert on.verdict == cv.VIOLATED  # unchanged without the branch
    tests = conventions_rules.new_stateless_tests(
        ["tests/queries/0_stateless/04652_a.sql"], {"tests/queries/0_stateless/04652_a.sql": "A"}
    )
    r5 = conventions_rules.rule_stateless_test_number(
        tests,
        ["tests/queries/0_stateless/04652_a.sql"],
        {"tests/queries/0_stateless/04652_a.sql": (7, "2026-01-01 00:00:00")},
        merged_at="2026-02-01 00:00:00",
        max_number=5000,
        title="Fixing 26.3 flaky tests",
        off_branch="26.3",
    )
    assert r5.verdict == cv.NOT_APPLICABLE and r5.evidence[0]["target_branch"] == "26.3"
    hit = conventions_rules.rule_stateless_test_number(
        tests,
        ["tests/queries/0_stateless/04652_a.sql"],
        {"tests/queries/0_stateless/04652_a.sql": (7, "2026-01-01 00:00:00")},
        merged_at="2026-02-01 00:00:00",
        max_number=5000,
        title="Fixing 26.3 flaky tests",
    )
    assert hit.verdict == cv.VIOLATED  # the title is not the bot's: only the ledger's branch says
