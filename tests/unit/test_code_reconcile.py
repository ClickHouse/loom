"""Content-based reconciliation: stale = chunk commit older than the file's
last change at head; one diff per distinct commit; heal is bounded and never
touches the watermark; the gauge reads the state row the worker writes."""

from __future__ import annotations

import asyncio
import json
from typing import Any
from unittest.mock import patch

import pytest

from server.code.app.index.gitrepo import DiffEntry
from server.code.app.index.reconcile import find_stale_paths, heal
from tests.unit.code_fixtures import repo_config


class _Repo:
    def __init__(self, rows):
        self.rows = rows
        self.state: dict[str, dict[str, Any]] = {}
        self.calls: list[str] = []

    async def file_identities(self, org, ns):
        return [(p, c, "") for p, c in self.rows]

    async def all_state(self, org, ns):
        return self.state

    async def set_state(self, org, ns, key, value, detail=""):
        self.state[key] = {"value": value, "detail": detail}


HEAD = "h" * 40


def _diff_factory(changed_by_commit: dict[str, set[str]], calls: list[str]):
    async def diff_files(git_dir, base, head):
        calls.append(base)
        if base == "gone":
            raise RuntimeError("bad object")
        return [DiffEntry(status="M", path=p) for p in changed_by_commit.get(base, set())]

    return diff_files


def test_find_stale_paths_one_diff_per_commit_and_floor_on_truncation():
    rows = [
        ("src/a.cpp", "c1"),
        ("src/b.cpp", "c1"),
        ("src/c.cpp", "c1"),  # biggest group
        ("src/d.cpp", "c2"),
        ("src/e.cpp", "c2"),
        ("src/f.cpp", HEAD),  # already at head: never diffed
        ("src/g.cpp", "gone"),  # commit unknown to the clone
    ]
    calls: list[str] = []
    diff = _diff_factory(
        {"c1": {"src/a.cpp", "src/zzz_not_indexed.cpp"}, "c2": {"src/e.cpp"}}, calls
    )
    repo = _Repo(rows)
    res = asyncio.run(find_stale_paths(repo, "/git", "o", "n", HEAD, diff_files=diff))
    assert res.stale == ["src/a.cpp", "src/e.cpp"], "changed since its chunk commit, and indexed"
    assert sorted(calls) == ["c1", "c2", "gone"], "one diff per distinct commit, head skipped"
    assert res.commits_distinct == 3 and res.commits_checked == 2 and res.commits_failed == 1
    assert res.paths_total == 7 and not res.truncated

    calls.clear()
    res2 = asyncio.run(
        find_stale_paths(repo, "/git", "o", "n", HEAD, diff_files=diff, max_commits=1)
    )
    assert res2.truncated and calls == ["c1"], "largest group first when capped"
    assert res2.stale == ["src/a.cpp"], "a floor, flagged as such"


def test_heal_reingests_only_listed_paths_and_counts_moves():
    class _Emb:
        model_name = "text-embedding-3-small"

    def _Cfg(*_a: Any, **_k: Any) -> Any:
        return repo_config(org="o", namespace="n", git_dir="/git", embed_reuse_from="")

    seen: list[str] = []

    async def show_file(git_dir, sha, path, max_bytes=0):
        return None if path == "src/deleted.cpp" else b"int x;"

    class _Stats:
        chunks_new, chunks_moved, symbols_moved = 0, 3, 2

    async def ingest_file(repo, org, ns, *, path, source, commit_sha, **kw):
        seen.append(path)
        assert commit_sha == HEAD and kw["embedding_model"] == "text-embedding-3-small"
        if path == "src/boom.cpp":
            raise ValueError("parse")
        return _Stats()

    res = asyncio.run(
        heal(
            object(),
            _Cfg(),
            HEAD,
            ["src/a.cpp", "src/deleted.cpp", "src/boom.cpp"],
            embedder=_Emb(),
            ingest_file=ingest_file,
            show_file=show_file,
        )
    )
    assert sorted(seen) == ["src/a.cpp", "src/boom.cpp"], (
        "missing file skipped without an ingest call"
    )
    assert (res.files, res.chunks_moved, res.symbols_moved, res.errors) == (1, 3, 2, 1)


@pytest.mark.asyncio
async def test_worker_reconcile_is_interval_gated_and_writes_the_state_row(monkeypatch):
    from server.code.app.index import ingest_pass as w
    from server.code.app.index import pass_budget as ingest_budget
    from server.settings import settings

    monkeypatch.setattr(settings, "code_reconcile_enabled", True)
    monkeypatch.setattr(settings, "code_reconcile_interval_seconds", 3600)
    # the dependents drain (default on) has its own test in
    # test_code_ingest_dependents.py; this one is about the interval gate
    monkeypatch.setattr(settings, "code_ingest_dependents_enabled", False)

    def _Cfg(*_a: Any, **_k: Any) -> Any:
        return repo_config(
            org="o", namespace="n", git_dir="/git", embed_reuse_from="", path_prefixes=("",)
        )

    repo = _Repo([])
    result = ingest_budget.CodeIngestResult()

    class _Found:
        stale = ["src/a.cpp", "src/b.cpp"]
        stale_count, paths_total, commits_distinct, commits_checked = 2, 10, 3, 3
        commits_failed, truncated, took_ms = 0, False, 12
        missing: dict[str, str] = {}

    class _Healed:
        files, chunks_moved, symbols_moved, errors = 2, 5, 4, 0

    async def find(*a, **k):
        return _Found()

    async def do_heal(*a, **k):
        return _Healed()

    with (
        patch("server.code.app.index.reconcile.find_stale_paths", new=find),
        patch("server.code.app.index.reconcile.heal", new=do_heal),
        patch("server.embedding.embedder.get_embedder", new=lambda: object()),
    ):
        await w._maybe_reconcile(repo, _Cfg(), HEAD, result)
        assert result.reconciled == 2 and result.healed == 2
        row = repo.state["reconcile"]
        assert row["value"] == "0", "value = stale paths remaining after the heal"
        detail = json.loads(row["detail"])
        assert detail["stale"] == 2 and detail["healed"] == 2 and detail["chunks_moved"] == 5
        assert detail["head"] == HEAD[:12] and detail["at"] > 0

        # second call inside the interval: gated, nothing rewritten
        repo.state["reconcile"]["value"] = "sentinel"
        await w._maybe_reconcile(repo, _Cfg(), HEAD, ingest_budget.CodeIngestResult())
        assert repo.state["reconcile"]["value"] == "sentinel"


def test_gauge_series_reads_stale_count_from_state_value():
    import datetime as dt

    from server.observability.otel_probes import _code_series_from_rows

    rows = [
        ("o", "n", "state:reconcile", 1_700_000_000.0, "37"),
        ("o", "n", "state:git_head", 1_700_000_000.0, "abc"),
    ]
    out = _code_series_from_rows(rows, now=dt.datetime.now(dt.UTC))
    assert out["code_stale_files"] == [(37.0, {"loom.org": "o", "loom.namespace": "n"})]
    assert "reconcile" not in json.dumps(out.get("code_index_age_seconds", [])), (
        "no bogus age series"
    )


def test_heal_passes_the_scope_and_the_live_hash_through_and_counts_identical_files():
    """A scoped heal hands ingest_file the stages that moved and
    the recorded content hash of each path; an unscoped heal passes neither
    (a content repair runs every stage, and older fakes need not know)."""

    class _Emb:
        model_name = "m"

    def _Cfg(*_a: Any, **_k: Any) -> Any:
        return repo_config(org="o", namespace="n", git_dir="/git", embed_reuse_from="")

    calls: list[dict[str, Any]] = []

    async def show_file(git_dir, sha, path, max_bytes=0):
        return b"int x;"

    class _Stats:
        chunks_new, chunks_moved, symbols_moved = 0, 0, 0

        def __init__(self, path):
            self.identical = path == "src/same.cpp"
            self.stages_run = ("chunk",)

    async def ingest_file(repo, org, ns, *, path, source, commit_sha, **kw):
        calls.append({"path": path, **kw})
        return _Stats(path)

    res = asyncio.run(
        heal(
            object(),
            _Cfg(),
            HEAD,
            ["src/same.cpp", "src/diff.cpp"],
            embedder=_Emb(),
            ingest_file=ingest_file,
            show_file=show_file,
            stages=frozenset({"chunk"}),
            live_hashes={"src/same.cpp": "abc"},
        )
    )
    by_path = {c["path"]: c for c in calls}
    assert (
        by_path["src/same.cpp"]["stages"] == {"chunk"}
        and by_path["src/same.cpp"]["live_content_hash"] == "abc"
    )
    assert by_path["src/diff.cpp"]["live_content_hash"] == "", (
        "unknown hash: ingest_file will run every stage"
    )
    assert (res.files, res.scoped, res.identical) == (2, 2, 1)

    calls.clear()
    res = asyncio.run(
        heal(
            object(),
            _Cfg(),
            HEAD,
            ["src/a.cpp"],
            embedder=_Emb(),
            ingest_file=ingest_file,
            show_file=show_file,
        )
    )
    assert "stages" not in calls[0] and "live_content_hash" not in calls[0]
    assert (res.scoped, res.identical) == (0, 0)


class _RepoWithBlobs(_Repo):
    """A repo whose rows carry the blob sha: (path, indexed_commit, blob_sha)."""

    def __init__(self, rows3):
        super().__init__([(p, c) for p, c, _b in rows3])
        self.rows3 = rows3

    async def file_identities(self, org, ns):
        return self.rows3


def test_find_stale_paths_judges_blob_rows_by_one_ls_tree_and_the_rest_by_diffs():
    """A row that recorded its blob is stale iff head's blob
    differs and gone iff head lacks the path — no diff for it, no ceiling;
    a row with no recorded blob ('') still takes the diff walk. One ls-tree total."""
    rows3 = [
        ("src/same.cpp", "c1", "b-same"),
        ("src/changed.cpp", "c1", "b-old"),
        ("src/removed.cpp", "c1", "b-rm"),
        ("src/at_head_changed.cpp", HEAD, "b-old2"),  # at head by commit, blob says otherwise
        ("src/legacy.cpp", "c2", ""),  # no blob recorded: diff walk
        ("src/legacy_same.cpp", "c2", ""),
    ]
    tree = {
        "src/same.cpp": "b-same",
        "src/changed.cpp": "b-new",
        "src/at_head_changed.cpp": "b-new2",
        "src/legacy.cpp": "x",
        "src/legacy_same.cpp": "x",
        "src/new_unindexed.cpp": "y",
    }
    ls_calls: list[str] = []

    async def ls_tree_blobs(git_dir, sha):
        ls_calls.append(sha)
        return dict(tree)

    diff_calls: list[str] = []
    diff = _diff_factory(
        {"c2": {"src/legacy.cpp"}, "c1": {"src/changed.cpp", "src/same.cpp"}}, diff_calls
    )
    repo = _RepoWithBlobs(rows3)
    res = asyncio.run(
        find_stale_paths(repo, "/git", "o", "n", HEAD, diff_files=diff, ls_tree_blobs=ls_tree_blobs)
    )
    assert ls_calls == [HEAD], "one tree listing for every blob row"
    assert diff_calls == ["c2"], (
        "only the pre-960 rows' commit is diffed; c1 rows were judged by blob"
    )
    assert res.stale == ["src/at_head_changed.cpp", "src/changed.cpp", "src/legacy.cpp"]
    assert res.gone == ["src/removed.cpp"] and res.by_tree == 4
    assert res.paths_total == 6 and res.commits_distinct == 1 and not res.truncated


def test_find_stale_paths_falls_back_to_diffs_when_head_cannot_be_listed():
    rows3 = [("src/a.cpp", "c1", "b1"), ("src/b.cpp", "c1", "b2")]

    async def ls_tree_blobs(git_dir, sha):
        raise RuntimeError("bad object")

    calls: list[str] = []
    diff = _diff_factory({"c1": {"src/b.cpp"}}, calls)
    res = asyncio.run(
        find_stale_paths(
            _RepoWithBlobs(rows3),
            "/git",
            "o",
            "n",
            HEAD,
            diff_files=diff,
            ls_tree_blobs=ls_tree_blobs,
        )
    )
    assert calls == ["c1"] and res.stale == ["src/b.cpp"] and res.by_tree == 0 and res.gone == []


def test_blob_sha_is_gits_hash_object():
    from server.code.app.index.gitrepo import blob_sha

    assert (
        blob_sha(b"") == "e69de29bb2d1d6434b8b29ae775ad8c2e48c5391"
    )  # `git hash-object -t blob /dev/null`
    assert (
        blob_sha(b"hello\n") == "ce013625030ba8dba906f756967f9e9ca394464a"
    )  # `echo hello | git hash-object --stdin`


def test_file_row_records_the_blob_it_parsed():
    from server.code.app.index.gitrepo import blob_sha
    from server.code.app.index.ingest import _file_row

    src = b"int main() { return 0; }\n"
    row = _file_row(
        "o",
        "n",
        path="src/m.cpp",
        pf=None,
        source=src,
        commit_sha="c" * 40,
        chunk_count=0,
        error="",
    )
    assert row["blob_sha"] == blob_sha(src) and len(row["blob_sha"]) == 40


def test_missing_lists_in_scope_paths_at_head_the_index_has_no_row_for():
    """A widened prefix list or a file the walk never reached: every path at head
    inside the scope with no row is `missing`, with its blob; a path an earlier
    backfill found unindexable at the same blob stays out, and comes back when its
    bytes change."""
    rows3 = [("src/a.py", "c1", "b-a")]
    tree = {
        "src/a.py": "b-a",  # indexed
        "dev/new.py": "b-new",  # in scope, no row
        "Makefile": "b-mk",  # in scope, no row
        "vendor/x.py": "b-v",  # scope refuses it
        "img/logo.bin": "b-bin",  # refused before at this blob
        "tools/blob.dat": "b-dat2",  # refused before at an older blob
    }

    async def ls_tree_blobs(git_dir, sha):
        return dict(tree)

    res = asyncio.run(
        find_stale_paths(
            _RepoWithBlobs(rows3),
            "/git",
            "o",
            "n",
            HEAD,
            ls_tree_blobs=ls_tree_blobs,
            in_scope=lambda p: not p.startswith("vendor/"),
            unindexable={"img/logo.bin": "b-bin", "tools/blob.dat": "b-dat1"},
        )
    )
    assert res.missing == {"Makefile": "b-mk", "dev/new.py": "b-new", "tools/blob.dat": "b-dat2"}
    assert res.stale == [] and res.gone == []


def test_missing_is_measured_even_when_no_row_recorded_a_blob():
    async def ls_tree_blobs(git_dir, sha):
        return {"src/a.cpp": "b1", "src/new.cpp": "b2"}

    res = asyncio.run(
        find_stale_paths(
            _RepoWithBlobs([("src/a.cpp", HEAD, "")]),
            "/git",
            "o",
            "n",
            HEAD,
            ls_tree_blobs=ls_tree_blobs,
            in_scope=lambda p: True,
        )
    )
    assert res.missing == {"src/new.cpp": "b2"} and res.by_tree == 0


def test_without_a_scope_nothing_is_missing_and_head_is_not_listed_for_it():
    calls: list[str] = []

    async def ls_tree_blobs(git_dir, sha):
        calls.append(sha)
        return {"src/new.cpp": "b2"}

    res = asyncio.run(
        find_stale_paths(
            _RepoWithBlobs([("src/a.cpp", HEAD, "")]),
            "/git",
            "o",
            "n",
            HEAD,
            ls_tree_blobs=ls_tree_blobs,
        )
    )
    assert res.missing == {} and calls == []


def test_heal_names_the_paths_it_indexed_nothing_for():
    from types import SimpleNamespace

    class _Emb:
        model_name = "m"

    def _Cfg(*_a: Any, **_k: Any) -> Any:
        return repo_config(
            org="o", namespace="n", git_dir="/git", embed_reuse_from="", ignore_file=""
        )

    async def show_file(git_dir, sha, path, max_bytes=0):
        return None if path == "gone.py" else b"x = 1\n"

    async def ingest_file(reads, org, ns, *, path, **kw):
        return SimpleNamespace(
            skipped=path == "blob.bin", chunks_new=0, chunks_moved=0, symbols_moved=0
        )

    out = asyncio.run(
        heal(
            _Repo([]),
            _Cfg(),
            HEAD,
            ["a.py", "blob.bin", "gone.py"],
            embedder=_Emb(),
            ingest_file=ingest_file,
            show_file=show_file,
            round_files=0,
        )
    )
    assert sorted(out.skipped_paths) == ["blob.bin", "gone.py"] and out.files == 2


def test_backfill_ingests_up_to_the_limit_and_returns_what_ingest_refused():
    from server.code.app.index.reconcile import HealResult, backfill_missing

    seen: list[list[str]] = []

    async def heal_fn(repo, cfg, head, paths, **kw):
        seen.append(list(paths))
        return HealResult(files=len(paths), attempted=len(paths), skipped_paths=["b.bin"])

    missing = {"a.py": "b1", "b.bin": "b2", "c.py": "b3"}
    healed, refused = asyncio.run(
        backfill_missing(None, None, HEAD, missing, limit=2, embedder=None, heal_fn=heal_fn)
    )
    assert seen == [["a.py", "b.bin"]] and refused == {"b.bin": "b2"}
    assert asyncio.run(
        backfill_missing(None, None, HEAD, missing, limit=0, embedder=None, heal_fn=heal_fn)
    ) == (None, {})


@pytest.mark.asyncio
async def test_reconcile_backfills_missing_paths_after_the_stale_heal_and_keeps_refusals(
    monkeypatch,
):
    from server.code.app.index import ingest_pass as w
    from server.code.app.index import pass_budget as ingest_budget
    from server.code.app.index.reconcile import HealResult
    from server.settings import settings

    monkeypatch.setattr(settings, "code_reconcile_enabled", True)
    monkeypatch.setattr(settings, "code_reconcile_heal_per_pass", 3)
    monkeypatch.setattr(settings, "code_ingest_dependents_enabled", False)

    def _Cfg(*_a: Any, **_k: Any) -> Any:
        return repo_config(
            org="o", namespace="n", git_dir="/git", embed_reuse_from="", path_prefixes=("",)
        )

    class _Found:
        stale = ["src/a.cpp"]
        stale_count, paths_total, commits_distinct, commits_checked = 1, 10, 1, 1
        commits_failed, truncated, took_ms = 0, False, 1
        missing = {"dev/x.py": "b1", "logo.bin": "b2", "later.py": "b3"}

    find_kw: dict[str, Any] = {}
    heals: list[list[str]] = []

    async def find(*a, **k):
        find_kw.update(k)
        return _Found()

    async def do_heal(repo, cfg, head, paths, **k):
        heals.append(list(paths))
        skipped = [p for p in paths if p.endswith(".bin")]
        return HealResult(files=len(paths), attempted=len(paths), skipped_paths=skipped)

    repo = _Repo([])
    repo.state["reconcile"] = {"value": "0", "detail": json.dumps({"unindexable": {"old": "b0"}})}
    with (
        patch("server.code.app.index.reconcile.find_stale_paths", new=find),
        patch("server.code.app.index.reconcile.heal", new=do_heal),
        patch(
            "server.embedding.embedder.get_embedder",
            new=lambda: type("E", (), {"model_name": "m"})(),
        ),
    ):
        await w._maybe_reconcile(repo, _Cfg(), HEAD, ingest_budget.CodeIngestResult())
    assert find_kw["unindexable"] == {"old": "b0"} and find_kw["in_scope"]("dev/x.py")
    assert heals == [["src/a.cpp"], ["dev/x.py", "logo.bin"]], "stale first, then the budget left"
    detail = json.loads(repo.state["reconcile"]["detail"])
    assert detail["missing"] == 3 and detail["backfilled"] == 1
    assert detail["unindexable"] == {"old": "b0", "logo.bin": "b2"}
