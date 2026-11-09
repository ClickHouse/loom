"""The code link pass's decisions without a store: which memories search hides, which
the `revise` policy re-checks or leaves due, how long a pending name is retried, which
stored memory a search hit is, and the worker row and table it runs on."""

from __future__ import annotations

import contextlib
import json
import re
import subprocess
from collections.abc import Callable
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from server.code.domain.anchor import NO_BASELINE, Definition, Shape
from server.code.domain.declaration import edit_pattern, fingerprint_version
from server.memory.app import (
    code_link_pass,
    code_link_policy,
    code_link_reads,
    code_link_recheck,
    code_link_rehash,
    code_links,
)
from server.memory.domain import code_change
from server.settings import settings

# the policy's own texts and judge, which the `world` fixture stubs
_TEXTS, _JUDGE = code_link_policy._texts, code_link_policy._judge


@pytest.fixture(autouse=True)
def _no_remembered_anchors():
    code_link_recheck._anchors.clear()  # tests reuse the commit names c0, c1, …
    yield
    code_link_recheck._anchors.clear()


@pytest.fixture(autouse=True)
def _free_leases(monkeypatch):
    # no ClickHouse here: every repository lease is free
    @contextlib.asynccontextmanager
    async def lease(name, *, wait_s=None):
        yield

    monkeypatch.setattr(code_link_pass.repository_locks, "lease", lease)


@pytest.fixture(autouse=True)
def _mirror_holds_every_commit(monkeypatch):
    async def has(self, commit):
        return True

    monkeypatch.setattr(code_link_pass.RepoReads, "has", has)


def _full(link: dict[str, Any]) -> dict[str, Any]:
    """A link row with every column: `link` over the table's defaults."""
    return code_links.store.LINK_COLS | {"fingerprint_version": fingerprint_version()} | link


def _row(mid: str, **kw: Any) -> dict[str, Any]:
    base = {
        "mid": mid,
        "org": "o",
        "namespace": kw.pop("namespace", "agent-a"),
        "agent": "a",
        "user_id": "-",
        "session_id": "-",
        "memory_key": mid,
        "status": "active",
        "tombstoned_at": None,
        "tags": [],
        "files": [],
        "value": "claim",
        "pinned": 0,
        "lock_mode": "",
        "lock_until": None,
        "verified_by": "",
        "verified_sha256": "",
        "sha": "s",
    }
    return base | kw


def _inputs(row: dict[str, Any]) -> str:
    return code_link_reads.inputs_hash(row["sha"], row["files"], "code-a", row["user_id"])


@pytest.fixture
def world(monkeypatch):
    rows: dict[str, dict[str, Any]] = {}
    states: dict[str, list[str]] = {}
    stale: set[str] = set()
    policy: dict[str, str] = {}
    judged: list[str] = []
    cleared: list[dict[str, Any]] = []

    async def policy_due(org, cns, version, *, limit):
        return list(states)

    async def for_memories(org, ids):
        return [
            _full(
                {
                    "memory_id": m,
                    "code_namespace": "code-a",
                    "state": s,
                    "path": "a.py",
                    "qualified_name": f"a.f{i}",
                    "policy_due": 1,
                    "complete": 1,
                    "inputs_hash": "old" if m in stale else _inputs(rows[m]),
                    "version": 1,
                }
            )
            for m in ids
            for i, s in enumerate(states.get(m, []))
        ]

    async def memory_heads(org, ids):
        return [rows[m] for m in ids if m in rows]

    async def policies(org, nss):
        return {ns: policy[ns] for ns in nss if ns in policy}

    async def insert(out):
        cleared.extend(out)

    async def judge(claim, link, texts, head, budget, *, writer=""):
        budget.judgements -= 1  # the judge was asked
        judged.append(str(link["memory_id"]))
        return {**link, "suggestion": "the fix"}

    monkeypatch.setattr(code_links.store, "policy_due", policy_due)
    monkeypatch.setattr(code_links.store, "for_memories", for_memories)
    monkeypatch.setattr(code_links.store, "memory_heads", memory_heads)
    monkeypatch.setattr(code_links.store, "code_change_policies", policies)
    monkeypatch.setattr(code_links.store, "insert", insert)
    monkeypatch.setattr(code_link_reads, "code_namespace_for", lambda org, ns, tags: "code-a")

    async def checked_head(org, cns):
        return "c2"

    async def texts(files, claim, link, head):
        return code_link_policy._Texts("old", "new", Definition("a.py", "a.f", "f"))

    monkeypatch.setattr(code_link_policy, "_judge", judge)
    monkeypatch.setattr(code_link_policy, "_texts", texts)
    monkeypatch.setattr(code_links.code, "checked_head", checked_head)
    return SimpleNamespace(
        rows=rows, states=states, stale=stale, policy=policy, judged=judged, cleared=cleared
    )


async def _apply(budget: int = 5) -> code_link_pass.PassResult:
    result = code_link_pass.PassResult()
    files = code_link_pass.RepoReads("o", "code-a")
    await code_link_policy.apply_policies(
        "o", "code-a", "c2", files, code_link_pass.Budget(budget), result
    )
    return result


def _cleared(world) -> list[str]:
    return sorted({r["memory_id"] for r in world.cleared if r["policy_due"] == 0})


def _stub_reads(monkeypatch, read) -> None:
    """Route the store's link reads by path, code namespace and commit to `read`, a
    fake taking them as keywords."""

    async def for_paths(org, cns, paths, **kw):
        return await read(org, cns, paths=paths, **kw)

    async def in_code_namespace(org, cns):
        return await read(org, [cns])

    async def for_commits(org, cns, commits):
        return await read(org, cns, checked_commits=commits)

    for fn in (for_paths, in_code_namespace, for_commits):
        monkeypatch.setattr(code_links.store, fn.__name__, fn)


async def _none_lost(org, cns, *, days):
    return []


@pytest.mark.asyncio
async def test_only_revise_judges_and_every_other_policy_just_clears_the_mark(world) -> None:
    world.rows.update(
        gone=_row("gone"),
        flagged=_row("flagged", namespace="agent-f"),
        edited=_row("edited"),
        older=_row("older"),
        deleted=_row("deleted", tombstoned_at="2026-01-01"),
    )
    world.states.update(
        gone=["missing"], flagged=["changed"], edited=["changed"], older=["changed"]
    )
    world.states["deleted"] = ["changed"]
    world.stale.add("older")  # links made for an older value: relinking comes first
    world.policy["agent-f"] = code_change.FLAG
    await _apply()
    assert world.judged == []  # Loom writes no memory: the default only marks links
    assert _cleared(world) == ["edited", "flagged", "gone"]
    # `older`'s links were made for an earlier value, and `deleted`'s memory is gone:
    # dropped, not left due forever
    for mid in ("older", "deleted"):
        dropped = [r for r in world.cleared if r["memory_id"] == mid]
        assert dropped and all(r["tombstoned_at"] is not None for r in dropped)


@pytest.mark.asyncio
async def test_revise_suggests_within_the_budget_and_the_rest_stays_due(world, monkeypatch) -> None:
    monkeypatch.setattr(settings, "memory_code_links_revise_per_pass", 1)
    for m in ("a", "b"):
        world.rows[m] = _row(m)
        world.states[m] = ["changed"]
    world.rows["held"] = _row("held", pinned=1)
    world.rows["obj"] = _row("obj", value='{"not": "text"}')
    world.states.update(held=["changed"], obj=["changed"])
    world.policy["agent-a"] = code_change.REVISE
    result = await _apply(budget=settings.memory_code_links_revise_per_pass)
    assert world.judged == ["a"] and result.suggested == 1
    # b waits for the next pass's budget; the held and non-text rows are decided
    assert _cleared(world) == ["a", "held", "obj"]
    (judged,) = [r for r in world.cleared if r["memory_id"] == "a"]
    assert judged["suggestion"] == "the fix"  # the cleared row is the one the policy wrote


def _checked_long_ago(monkeypatch) -> datetime:
    """Every link the world reads last checked at one old time, which it returns."""
    reads, then = code_links.store.for_memories, datetime(2026, 1, 1)

    async def for_memories(org, ids):
        return [a | {"checked_at": then} for a in await reads(org, ids)]

    monkeypatch.setattr(code_links.store, "for_memories", for_memories)
    return then


@pytest.mark.asyncio
async def test_a_failed_judgement_stays_due_behind_the_rest(world, monkeypatch) -> None:
    async def judge(claim, link, texts, head, budget, *, writer=""):
        budget.judgements -= 1
        if link["qualified_name"] == "a.f0":
            raise RuntimeError("provider 400")
        return None

    monkeypatch.setattr(code_link_policy, "_judge", judge)
    then = _checked_long_ago(monkeypatch)
    world.rows["a"] = _row("a")
    world.states["a"] = ["changed", "changed"]
    world.policy["agent-a"] = code_change.REVISE
    result = await _apply()
    assert result.errors
    # the failed link stays due, stamped now: `policy_due` reads it after older ones
    rows = {r["qualified_name"]: r for r in world.cleared}
    assert rows["a.f0"]["policy_due"] == 1 and rows["a.f0"]["checked_at"] > then
    assert (rows["a.f1"]["policy_due"], rows["a.f1"]["checked_at"]) == (0, then)


@pytest.mark.asyncio
async def test_a_malformed_verdict_stays_due_and_an_unsure_one_does_not(world, monkeypatch) -> None:
    from server.llm import client as llm_client

    reply = ""

    class _LLM:
        async def complete(self, *a: Any, **kw: Any) -> Any:
            return SimpleNamespace(text=reply)

    monkeypatch.setattr(llm_client, "get_llm_client_for_model", lambda model: _LLM())
    monkeypatch.setattr(code_link_policy, "_judge", _JUDGE)
    world.rows["a"] = _row("a")
    world.states["a"] = ["changed"]
    world.policy["agent-a"] = code_change.REVISE
    for reply, due in (("not json", 1), ('{"verdict": "maybe"}', 1), ('{"verdict": "unsure"}', 0)):
        world.cleared.clear()
        result = await _apply()
        assert [r["policy_due"] for r in world.cleared] == [due], reply
        assert bool(result.errors) == bool(due)


@pytest.mark.asyncio
async def test_a_memory_labelled_high_while_its_code_is_read_never_reaches_the_judge(
    world, monkeypatch
) -> None:
    from server.code.domain.anchor import Definition
    from server.memory.app import code_link_judge

    async def defs(self, commit, path, qn):
        world.rows["a"]["sensitivity"] = "high"  # labelled while git reads the code
        return [Definition(path, qn, "f", body_hash=commit, shown=f"code at {commit}")]

    sent: list[str] = []

    async def judge_claim(claim, before, after):
        sent.append(claim)
        return "unsure", ""

    monkeypatch.setattr(code_link_pass.RepoReads, "defs", defs)
    monkeypatch.setattr(code_link_judge, "judge_claim", judge_claim)
    monkeypatch.setattr(code_link_policy, "_texts", _TEXTS)
    monkeypatch.setattr(code_link_policy, "_judge", _JUDGE)
    world.rows["a"] = _row("a", value='"secret claim"')
    world.states["a"] = ["changed"]
    world.policy["agent-a"] = code_change.REVISE
    budget = code_link_pass.Budget(5)
    await code_link_policy.apply_policies(
        "o",
        "code-a",
        "c2",
        code_link_pass.RepoReads("o", "code-a"),
        budget,
        code_link_pass.PassResult(),
    )
    assert (sent, budget.judgements) == ([], 5)


def test_search_hides_only_a_complete_unheld_memory_whose_code_is_all_gone() -> None:
    hidden = code_change.hidden
    assert hidden(["missing", "missing"], True, code_change.RETIRE_REMOVED, False)
    assert hidden(["missing"], True, code_change.REVISE, False)
    assert not hidden(["missing", "fresh"], True, code_change.RETIRE_REMOVED, False)
    assert not hidden(["missing"], False, code_change.RETIRE_REMOVED, False)  # cut by the caps
    assert not hidden(["missing"], True, code_change.FLAG, False)
    assert not hidden(["missing"], True, code_change.RETIRE_REMOVED, True)
    assert not hidden([], True, code_change.RETIRE_REMOVED, False)


def test_the_held_rows() -> None:
    assert code_change.held(_row("p", pinned=1))
    assert code_change.held(_row("l", lock_mode="read_only"))
    assert code_change.held(_row("t", lock_mode="no_delete", lock_until=datetime(2999, 1, 1)))
    assert not code_change.held(_row("z", lock_mode="read_only", lock_until=datetime(2020, 1, 1)))
    assert code_change.held(_row("v", verified_by="human:ann", verified_sha256="s"))
    assert not code_change.held(_row("x", verified_by="human:ann", verified_sha256="old"))
    assert not code_change.held(_row("n"))


@pytest.mark.asyncio
async def test_a_pending_name_is_retried_until_the_pending_days_pass(monkeypatch) -> None:
    monkeypatch.setattr(settings, "memory_code_links_pending_days", 14)
    pending = [
        _full(
            {"qualified_name": "pkg.Planner.plan", "expired": 1, "version": 1, "memory_id": "m1"}
        ),
        _full(
            {"qualified_name": "pkg.Router.route", "expired": 0, "version": 1, "memory_id": "m2"}
        ),
    ]
    written: list[Any] = []

    async def rows(org, cns, *, days):
        assert days == 14
        return pending

    async def insert(out):
        written.extend(out)

    async def candidates(org, cns, *, names):
        assert names == ["route"]
        return []

    async def heads(org, ids):
        return []

    monkeypatch.setattr(code_links.store, "pending", rows)
    monkeypatch.setattr(code_links.store, "insert", insert)
    monkeypatch.setattr(code_links.store, "memory_heads", heads)
    monkeypatch.setattr(code_links.code, "candidates", candidates)
    files = code_link_pass.RepoReads("o", "code-a")
    assert (
        await code_link_recheck._resolve_pending(
            "o", "code-a", "c2", files, code_link_pass.PassResult()
        )
        == []
    )
    assert [(r["qualified_name"], r["tombstoned_at"] is not None) for r in written] == [
        ("pkg.Planner.plan", True)
    ]


def test_the_worker_row_and_its_table() -> None:
    from server.memory.store.code_links import TABLE
    from server.workers.registry import _WORKERS

    (row,) = [w for w in _WORKERS if w.name == "memory_code_links"]
    assert (row.module, row.enabled, row.interval) == (
        "server.memory.workers.code_links",
        "memory_code_links_enabled",
        "memory_code_links_interval_seconds",
    )
    assert settings.memory_code_links_enabled and settings.memory_code_links_batch >= 1
    assert TABLE == "loom.memory_code_links"


def test_the_queue_records_every_memory_insert_when_it_runs() -> None:
    from tests.unit.schema_ddl import ddl

    view = ddl("memory_code_link_queue_mv")
    assert "TO loom.memory_code_link_queue" in view and "FROM loom.memory" in view
    assert "DEFAULT now64(6)" in ddl("memory_code_link_queue")


def test_the_policy_word_defaults_to_retire_removed() -> None:
    from server.identity.api.models import NamespaceGetOutput, NamespaceRegisterInput

    for model in (NamespaceRegisterInput, NamespaceGetOutput):
        field = model.model_fields["code_change_policy"]
        assert field.annotation == code_change.CodeChangePolicy | None
    assert code_change.policy_of("") == code_change.RETIRE_REMOVED
    assert code_change.policy_of("bogus") == code_change.RETIRE_REMOVED
    assert code_change.policy_of("flag") == code_change.FLAG
    assert code_change.POLICIES == ("retire_removed", "flag", "revise")
    assert code_change.code_removed(["missing", "missing"])
    assert not code_change.code_removed(["missing", "moved"]) and not code_change.code_removed([])


def test_the_repo_tag_matches_the_plugins_for_every_remote_shape() -> None:
    from server.code.domain.index.remote import repo_tag

    # the id the coding plugins stamp for ClickHouse/loom (their repoIdentity)
    for remote in (
        "https://github.com/ClickHouse/loom.git",
        "git@github.com:ClickHouse/loom.git",
        "ssh://git@github.com:22/ClickHouse/Loom/",
    ):
        assert repo_tag(remote) == "repo:2b362b4d78", remote
    assert repo_tag("owner/name") == ""


@pytest.mark.asyncio
async def test_a_path_renamed_with_an_edit_is_traced_back_past_the_rename_limit(tmp_path) -> None:
    from server.code.app.index.gitrepo import renamed_from

    git, _ = _repo(tmp_path)
    git("config", "diff.renameLimit", "1")
    names = (":rows.py", "other.py", "gone.py")  # pathspec magic in a name is a plain path
    for name in names:
        (tmp_path / name).write_text("\n".join(f"{name} line {i}" for i in range(40)) + "\n")
    git("add", ".")
    git("commit", "-q", "-m", "one")
    base = git("rev-parse", "HEAD")
    git("mv", ":rows.py", "pkg_rows.py")
    git("mv", "other.py", "pkg_other.py")
    git("rm", "-q", "gone.py")
    for name in ("pkg_rows.py", "pkg_other.py"):
        with (tmp_path / name).open("a") as f:
            f.write("more\n")
    git("add", ".")
    git("commit", "-q", "-m", "two")
    head = git("rev-parse", "HEAD")
    old = await renamed_from(str(tmp_path / ".git"), base, head, ["pkg_rows.py", "keep.py"])
    assert old == {"pkg_rows.py": ":rows.py"}


def test_name_status_records_keep_both_paths_of_a_rename_or_copy_only() -> None:
    from server.code.app.index.gitrepo import name_status_z

    out = "M\0a.py\0C075\0b.py\0c.py\0T\0l\0R100\0d.py\0e.py\0D\0f.py\0A\0g.py\0"
    assert name_status_z(out) == [
        ("M", "a.py", "a.py"),
        ("C", "b.py", "c.py"),
        ("T", "l", "l"),
        ("R", "d.py", "e.py"),
        ("D", "f.py", "f.py"),
        ("A", "g.py", "g.py"),
    ]
    assert name_status_z("") == []


def _rename_beside_a_deleted_tree(root: Path, files: int) -> tuple[str, str]:
    """(base, head) of a repository at `root` whose head renames rows.py to pkg_rows.py
    with an edit, adds new.py and deletes `files` vendored files."""
    git, _ = _repo(root)
    rows = "\n".join(f"rows line {i}" for i in range(40)) + "\n"
    (root / "rows.py").write_text(rows)
    (root / "vendored.c").write_text("int vendored;\n")
    git("add", "rows.py")
    blob = git("hash-object", "-w", "vendored.c")
    vendored = "".join(
        f"100644 {blob}\tvendor/{i // 1000:03d}/{i:06d}{'x' * 40}.c\0" for i in range(files)
    )
    subprocess.run(
        ["git", "update-index", "-z", "--index-info"], cwd=root, input=vendored.encode(), check=True
    )
    base = git("commit-tree", "-m", "one", git("write-tree"))
    git("read-tree", "--empty")
    for name, text in (("pkg_rows.py", rows + "more\n"), ("new.py", "x = 1\n")):
        (root / name).write_text(text)
        git("add", name)
    return base, git("commit-tree", "-p", base, "-m", "two", git("write-tree"))


@pytest.mark.asyncio
async def test_a_rename_is_traced_past_more_deleted_files_than_an_argv_holds(tmp_path) -> None:
    from server.code.app.index.gitrepo import renamed_from

    base, head = _rename_beside_a_deleted_tree(tmp_path, 100_000)
    old = await renamed_from(str(tmp_path / ".git"), base, head, ["pkg_rows.py", "new.py"])
    assert old == {"pkg_rows.py": "rows.py"}


@pytest.mark.asyncio
async def test_a_commit_range_maps_each_changed_path_to_where_it_is_now(tmp_path) -> None:
    from server.code.app.index.gitrepo import diff_paths_renamed

    git, _ = _repo(tmp_path)
    git("config", "diff.renameLimit", "1")  # past the limit git drops edited renames
    for name in ("moved.py", "carried.py", "edited.py", "gone.py"):
        (tmp_path / name).write_text("\n".join(f"{name} line {i}" for i in range(40)) + "\n")
    git("add", ".")
    git("commit", "-q", "-m", "one")
    base = git("rev-parse", "HEAD")
    git("mv", "moved.py", "renamed.py")
    git("mv", "carried.py", "pkg_carried.py")
    for name in ("edited.py", "renamed.py", "pkg_carried.py"):
        with (tmp_path / name).open("a") as f:
            f.write("more\n")
    git("rm", "-q", "gone.py")
    (tmp_path / "new.py").write_text("x = 1\n")
    git("add", ".")
    git("commit", "-q", "-m", "two")
    kept = await diff_paths_renamed(str(tmp_path / ".git"), base, git("rev-parse", "HEAD"))
    assert kept == {
        "moved.py": "renamed.py",
        "carried.py": "pkg_carried.py",
        "edited.py": "edited.py",
        "gone.py": "",
        "new.py": "new.py",
    }


@pytest.mark.asyncio
async def test_a_repository_that_fails_parks_its_memories_and_the_cursor_moves_on(
    monkeypatch,
) -> None:
    def mem(v: int, repo: str) -> dict[str, Any]:
        return {
            "org": "o",
            "namespace": "agent-a",
            "agent": "a",
            "user_id": "-",
            "session_id": "-",
            "memory_key": f"k{v}",
            "mid": f"m{v}",
            "sha": f"s{v}",
            "value": "`Reader.read_rows` leaks",
            "tags": [f"repo:{repo}"],
            "files": [],
            "status": "active",
            "tombstoned_at": None,
        }

    heads = [mem(10, "ok"), mem(11, "bad"), mem(12, "ok")]
    # one bulk write: three entries with one queue time, ordered by version
    positions = [(5, v, "o", "agent-a", "a", "-", "-", f"k{v}") for v in (10, 11, 12)]

    async def queued(after, limit, code_namespaces, *, lag_s):
        return heads, positions, (9, 0, "", "", "", "", "", "")

    async def for_memories(org, ids):
        return []

    written: list[dict[str, Any]] = []

    async def insert(rows):
        written.extend(rows)

    async def link(org, cns, memories, existing, files):
        if cns == "code-bad":
            raise RuntimeError("down")
        return [], []

    async def memory_heads(org, ids):
        return [h for h in heads if h["mid"] in ids]

    monkeypatch.setattr(code_links.store, "queued_memories", queued)
    monkeypatch.setattr(code_links.store, "for_memories", for_memories)
    monkeypatch.setattr(code_links.store, "memory_heads", memory_heads)
    monkeypatch.setattr(code_links.store, "insert", insert)
    monkeypatch.setattr(code_links, "_link", link)
    monkeypatch.setattr(
        code_links, "code_namespace_for", lambda org, ns, tags: f"code-{tags[0][5:]}"
    )
    result = code_link_pass.PassResult()
    cursor = await code_links.link_queued(None, 100, result)
    # k11's repository failed: it waits behind the `*` marker, retried every pass
    assert cursor == positions[-1] and result.errors
    assert [(r["memory_key"], r["qualified_name"], r["state"]) for r in written] == [
        ("k11", "*", "pending")
    ]


@pytest.mark.asyncio
async def test_an_empty_queue_moves_the_cursor_to_the_reads_cutoff(monkeypatch) -> None:
    cutoff = (9, 0, "", "", "", "", "", "")

    async def queued(after, limit, code_namespaces, *, lag_s):
        return [], [], cutoff

    monkeypatch.setattr(code_links.store, "queued_memories", queued)
    old = (1, 1, "o", "n", "a", "-", "-", "k")
    assert await code_links.link_queued(old, 100, code_link_pass.PassResult()) == cutoff


@pytest.mark.asyncio
async def test_links_name_only_repositories_the_caller_may_read(monkeypatch) -> None:
    rows = [
        _full(
            {
                "memory_id": "m",
                "code_namespace": cns,
                "path": f"{cns}.py",
                "qualified_name": "f",
                "state": "fresh",
                "inputs_hash": _inputs(_row("m")),
            }
        )
        for cns in ("code-open", "code-secret")
    ]

    async def for_memories(org, ids):
        return rows

    async def readable(org, nss):
        return nss - {"code-secret"}

    monkeypatch.setattr(code_links.store, "for_memories", for_memories)
    monkeypatch.setattr(code_links.code, "code_namespaces", lambda: ["code-a"])
    monkeypatch.setattr(code_link_reads, "readable_namespaces", readable)
    monkeypatch.setattr(code_link_reads, "code_namespace_for", lambda org, ns, tags: "code-a")
    links = await code_link_reads.links_of("o", _row("m"))
    assert [x.code_namespace for x in links] == ["code-open"]


def test_a_memory_naming_more_than_the_cap_is_marked_incomplete() -> None:
    from server.code.domain.anchor import Definition, Mention

    defs = [Definition("a.py", f"a::fn_{i:02d}_x", f"fn_{i:02d}_x") for i in range(40)]
    names = [Mention(d.name, in_prose=False) for d in defs]
    by_name = {d.name: [d] for d in defs}
    links, _, complete = code_links._plan(names, by_name, [], {})
    assert len(links) == code_links._MAX_LINKS and not complete
    links, _, complete = code_links._plan(names[:3], by_name, [], {})
    assert len(links) == 3 and complete


def test_a_qualified_mention_finds_its_candidates_under_the_bare_name() -> None:
    from server.code.domain.anchor import Definition, Mention

    fn = Definition("a.py", "a::fn", "fn")
    links, waiting, _ = code_links._plan([Mention("a::fn", in_prose=False)], {"fn": [fn]}, [], {})
    assert links == [fn] and waiting == []


@pytest.mark.asyncio
async def test_a_qualified_mention_links_through_the_candidates_the_index_names(
    monkeypatch,
) -> None:
    from server.code.domain.anchor import Definition

    files = code_link_pass.RepoReads("o", "code-a")

    async def at(commit, path):
        return _file(path, "def fetch_rows(n):\n    return n\n") if path == "pkg/a.py" else None

    async def candidates(org, cns, *, names):
        assert names == ["fetch_rows"]
        return [Definition("pkg/a.py", "pkg.a::fetch_rows", "fetch_rows")]

    async def indexed_head(org, cns):
        return "c1"

    _reads(monkeypatch, files, at)
    monkeypatch.setattr(code_links.code, "indexed_head", indexed_head)
    monkeypatch.setattr(code_links.code, "candidates", candidates)
    memory = _row("m", value="`pkg.a::fetch_rows` reads one page")
    memory["inputs"] = "i"
    rows, gone = await code_links._link("o", "code-a", [memory], {}, files)
    assert [(r["path"], r["qualified_name"], r["state"]) for r in rows] == [
        ("pkg/a.py", "pkg.a::fetch_rows", "fresh")
    ]
    assert gone == []


def test_exactly_the_cap_of_mentions_is_not_cut() -> None:
    from server.code.domain.anchor import Mention

    cap = code_links._MAX_MENTIONS
    names = [Mention(f"fn_{i:03d}_x", in_prose=False) for i in range(cap + 1)]
    assert code_links._plan(names[:cap], {}, [], {})[2]
    assert not code_links._plan(names, {}, [], {})[2]


def test_definitions_come_from_the_file_with_every_overload_in_one_fingerprint() -> None:
    from server.code.app.memory_links import file_definitions

    source = b"""class Reader:
    def read_rows(self, n):
        return self.fetch(n)

def helper():
    return 1
"""
    defs = {d.qualified_name: d for d in file_definitions("pkg/io.py", source)}
    assert {"pkg.io::Reader", "pkg.io::Reader::read_rows", "pkg.io::helper"} <= set(defs)
    method = defs["pkg.io::Reader::read_rows"]
    assert (method.start_line, method.end_line) == (2, 3) and method.body_hash
    edited = source.replace(b"self.fetch(n)", b"self.fetch(n + 1)")
    again = {d.qualified_name: d for d in file_definitions("pkg/io.py", edited)}
    assert again["pkg.io::Reader::read_rows"].body_hash != method.body_hash
    assert again["pkg.io::helper"].body_hash == defs["pkg.io::helper"].body_hash
    renamed = source.replace(b"def helper", b"def assist")
    (assist,) = [d for d in file_definitions("pkg/io.py", renamed) if d.name == "assist"]
    assert assist.shape == defs["pkg.io::helper"].shape


def _hit(key: str, **kw: Any) -> Any:
    from server.memory.api.models_search import _MemorySearchHit

    hit = _MemorySearchHit(
        memory_key=key, memory_type="semantic", content_excerpt="x", distance=0.0, similarity=1.0
    )
    hit._row_identity = kw.pop("who", ("a", "-", "-"))
    for k, v in kw.items():
        setattr(hit, k, v)
    return hit


def _search(**kw: Any) -> Any:
    from server.memory.api.models_search import MemorySearchInput

    return MemorySearchInput(org="o", namespace="agent-a", query="q", **kw)


@pytest.fixture
def served(monkeypatch):
    heads: list[dict[str, Any]] = []
    links: dict[str, list[dict[str, Any]]] = {}
    policy: dict[str, str] = {}

    async def live_links(org, idents):
        made = {h["mid"]: _inputs(h) for h in heads}
        return [
            _full({"inputs_hash": made[h["mid"]], **a, **{c: h[c] for c in IDENT}})
            for h in heads
            if tuple(h[c] for c in IDENT) in set(idents)
            for a in links.get(h["mid"], [])
        ]

    async def served_heads(org, keys):
        return [h for h in heads if (h["namespace"], h["memory_key"]) in set(keys)]

    async def policies(org, nss):
        return {ns: policy[ns] for ns in nss if ns in policy}

    async def readable(org, nss):
        return set(nss)

    monkeypatch.setattr(code_links.store, "live_links", live_links)
    monkeypatch.setattr(code_links.store, "served_heads", served_heads)
    monkeypatch.setattr(code_links.code, "code_namespaces", lambda: ["code-a"])
    monkeypatch.setattr(code_links.store, "code_change_policies", policies)
    monkeypatch.setattr(code_link_reads, "readable_namespaces", readable)
    monkeypatch.setattr(code_link_reads, "code_namespace_for", lambda org, ns, tags: "code-a")
    return SimpleNamespace(heads=heads, links=links, policy=policy)


IDENT = ("namespace", "agent", "user_id", "session_id", "memory_key")


def _link(mid: str, state: str, complete: int = 1) -> dict[str, Any]:
    return {"memory_id": mid, "code_namespace": "code-a", "state": state, "complete": complete}


@pytest.mark.asyncio
async def test_a_hit_is_the_stored_memory_its_leg_read(served) -> None:
    served.heads += [
        _row("mine", memory_key="k", user_id="ann", session_id="s1"),
        _row("theirs", memory_key="k", user_id="bob", session_id="s2"),
    ]
    served.links.update(mine=[_link("mine", "changed")], theirs=[_link("theirs", "fresh")])
    hits = [_hit("k", who=("a", "ann", "s1")), _hit("k", who=("a", "bob", "s2"))]
    await code_link_reads.annotate_hits(_search(), hits)
    assert [h.code_state for h in hits] == ["changed", "fresh"]
    unknown = [_hit("k", who=None), _hit("k", who=("a", "cy", "s3"))]
    await code_link_reads.annotate_hits(_search(), unknown)
    assert [h.code_state for h in unknown] == ["", ""]


@pytest.mark.asyncio
async def test_a_key_several_agents_share_is_the_memory_its_row_names(served) -> None:
    served.heads += [_row("a1", memory_key="k", agent="a1"), _row("a2", memory_key="k", agent="a2")]
    served.links.update(a1=[_link("a1", "fresh")], a2=[_link("a2", "missing")])
    hits = [_hit("k", who=("a1", "-", "-")), _hit("k", who=("a2", "-", "-"))]
    await code_link_reads.annotate_hits(_search(), hits)
    # a2's code is all gone: its hit goes
    assert [(h._row_identity[0], h.code_state) for h in hits] == [("a1", "fresh")]


@pytest.mark.asyncio
async def test_a_shared_search_reads_code_state_as_a_private_one_does(served) -> None:
    served.heads += [
        _row("g", memory_key="g", agent="_shared"),
        _row("f", memory_key="f", agent="_shared"),
    ]
    served.links.update(g=[_link("g", "missing")], f=[_link("f", "changed")])
    hits = [_hit(k, who=("_shared", "-", "-")) for k in ("g", "f")]
    await code_link_reads.annotate_hits(_search(scope="shared"), hits)
    assert [(h.memory_key, h.code_state) for h in hits] == [("f", "changed")]


@pytest.mark.asyncio
async def test_the_heads_policies_and_readability_are_read_at_once(served, monkeypatch) -> None:
    import asyncio

    started: list[str] = []
    gate = asyncio.Event()

    def waits(name, read):
        async def run(*a, **k):
            started.append(name)
            if len(started) == 3:
                gate.set()
            await asyncio.wait_for(gate.wait(), 1)
            return await read(*a, **k)

        return run

    for name in ("served_heads", "code_change_policies"):
        monkeypatch.setattr(code_links.store, name, waits(name, getattr(code_links.store, name)))
    monkeypatch.setattr(
        code_link_reads,
        "readable_namespaces",
        waits("readable_namespaces", code_link_reads.readable_namespaces),
    )
    served.heads.append(_row("m", memory_key="k"))
    served.links["m"] = [_link("m", "changed")]
    hits = [_hit("k")]
    await code_link_reads.annotate_hits(_search(), hits)
    assert sorted(started) == ["code_change_policies", "readable_namespaces", "served_heads"]
    assert hits[0].code_state == "changed"


@pytest.mark.asyncio
async def test_search_drops_a_hit_whose_code_is_all_gone_unless_kept(served) -> None:
    served.heads += [
        _row("gone", memory_key="gone"),
        _row("pinned", memory_key="pinned", pinned=1),
        _row("capped", memory_key="capped"),
        _row("flagged", memory_key="flagged", namespace="agent-f"),
        _row("partly", memory_key="partly"),
    ]
    served.links.update(
        gone=[_link("gone", "missing")],
        pinned=[_link("pinned", "missing")],
        capped=[_link("capped", "missing", complete=0)],
        flagged=[_link("flagged", "missing")],
        partly=[_link("partly", "missing"), _link("partly", "fresh")],
    )
    served.policy["agent-f"] = code_change.FLAG
    hits = [_hit(k) for k in ("gone", "pinned", "capped", "partly")]
    hits.append(_hit("flagged", namespace="agent-f"))
    await code_link_reads.annotate_hits(_search(), hits)
    assert [(h.memory_key, h.code_state) for h in hits] == [
        ("pinned", "missing"),
        ("capped", "missing"),
        ("partly", "missing"),
        ("flagged", "missing"),
    ]


@pytest.mark.asyncio
async def test_code_the_caller_may_not_read_never_hides_a_hit(served, monkeypatch) -> None:
    async def readable(org, nss):
        return set()

    monkeypatch.setattr(code_link_reads, "readable_namespaces", readable)
    served.heads.append(_row("gone", memory_key="gone"))
    served.links["gone"] = [_link("gone", "missing")]
    hits = [_hit("gone")]
    await code_link_reads.annotate_hits(_search(), hits)
    assert [(h.memory_key, h.code_state) for h in hits] == [("gone", "")]


@pytest.mark.asyncio
async def test_links_made_for_an_older_value_neither_hide_nor_annotate(served) -> None:
    served.heads.append(_row("m", memory_key="lunch", sha="rewritten"))
    served.links["m"] = [{**_link("m", "missing"), "inputs_hash": _inputs(_row("m"))}]
    hits = [_hit("lunch")]
    await code_link_reads.annotate_hits(_search(), hits)
    assert [(h.memory_key, h.code_state) for h in hits] == [("lunch", "")]


@pytest.mark.asyncio
async def test_a_pending_link_keeps_a_memory_whose_other_code_is_gone(served) -> None:
    served.heads.append(_row("m", memory_key="k"))
    served.links["m"] = [_link("m", "missing"), _link("m", "pending")]
    hits = [_hit("k")]
    await code_link_reads.annotate_hits(_search(), hits)
    assert [h.code_state for h in hits] == ["missing"]


@pytest.mark.asyncio
async def test_revise_judges_every_changed_definition_and_keeps_the_unjudged_due(
    world, monkeypatch
) -> None:
    asked: list[str] = []

    async def judge(claim, link, texts, head, budget, *, writer=""):
        budget.judgements -= 1  # the judge was asked
        asked.append(link["qualified_name"])
        return None if link["qualified_name"] == "a.f0" else {**link, "suggestion": "fix"}

    monkeypatch.setattr(code_link_policy, "_judge", judge)
    world.rows["m"] = _row("m")
    world.states["m"] = ["changed", "changed"]
    world.policy["agent-a"] = code_change.REVISE
    await _apply()
    assert asked == ["a.f0", "a.f1"]  # an unjudgeable first link does not stop the second
    assert {(r["qualified_name"], r["policy_due"], r["suggestion"]) for r in world.cleared} == {
        ("a.f0", 0, ""),
        ("a.f1", 0, "fix"),
    }
    world.cleared.clear()
    asked.clear()
    await _apply(budget=1)
    assert asked == ["a.f0"]
    assert {(r["qualified_name"], r["policy_due"]) for r in world.cleared} == {
        ("a.f0", 0),
        ("a.f1", 1),  # not examined: due again next pass
    }


@pytest.mark.asyncio
async def test_a_policy_write_follows_the_version_it_read(world) -> None:
    """One version above the row read: a relink or recheck written meanwhile has a newer
    version and keeps its state whichever insert lands last."""
    world.rows["m"] = _row("m")
    world.states["m"] = ["changed"]
    world.policy["agent-a"] = code_change.REVISE
    result = await _apply()
    assert not result.errors
    assert [(r["version"], r["suggestion"]) for r in world.cleared] == [(2, "the fix")]


@pytest.mark.asyncio
async def test_a_hold_taken_during_judgement_only_flags(world, monkeypatch) -> None:
    async def judge(claim, link, texts, head, budget, *, writer=""):
        budget.judgements -= 1  # the judge was asked
        world.rows["m"]["lock_mode"] = "read_only"  # a person locked it meanwhile
        return {**link, "suggestion": "the fix"}

    monkeypatch.setattr(code_link_policy, "_judge", judge)
    world.rows["m"] = _row("m")
    world.states["m"] = ["changed"]
    world.policy["agent-a"] = code_change.REVISE
    await _apply()
    assert [(r["suggestion"], r["policy_due"]) for r in world.cleared] == [("", 0)]


@pytest.mark.asyncio
async def test_only_links_still_due_spend_the_budget(world, monkeypatch) -> None:
    asked: list[str] = []

    async def judge(claim, link, texts, head, budget, *, writer=""):
        budget.judgements -= 1  # the judge was asked
        asked.append(link["qualified_name"])
        return {**link, "suggestion": "fix"}

    reads = code_links.store.for_memories

    async def first_done(org, ids):
        return [
            {**a, "policy_due": int(a["qualified_name"] != "a.f0")} for a in await reads(org, ids)
        ]

    monkeypatch.setattr(code_link_policy, "_judge", judge)
    monkeypatch.setattr(code_links.store, "for_memories", first_done)
    world.rows["m"] = _row("m")
    world.states["m"] = ["changed", "changed"]
    world.policy["agent-a"] = code_change.REVISE
    await _apply(budget=1)
    assert asked == ["a.f1"]


@pytest.mark.asyncio
async def test_a_repository_another_host_holds_is_skipped(monkeypatch) -> None:
    from contextlib import asynccontextmanager

    from server.memory.store.key_write_lock import LeaseBusy

    async def namespaces():
        return [("o", "code-a"), ("o", "code-b")]

    ran: list[str] = []

    async def recheck(org, cns, budget, result):
        ran.append(cns)

    @asynccontextmanager
    async def lease(name):
        if name == code_link_recheck._lock_name("o", "code-a"):
            raise LeaseBusy(name)
        yield

    monkeypatch.setattr(code_links.store, "code_namespaces", namespaces)
    monkeypatch.setattr(code_link_recheck, "_recheck_repository", recheck)
    monkeypatch.setattr(code_link_pass.repository_locks, "lease", lease)
    monkeypatch.setattr(code_link_recheck, "repo_for_namespace", lambda org, cns: object())
    result = code_link_pass.PassResult()
    await code_link_recheck.recheck_all(result)
    assert ran == ["code-b"] and not result.errors


@pytest.mark.asyncio
async def test_links_of_a_repository_no_longer_configured_are_left_alone(monkeypatch) -> None:
    async def namespaces():
        return [("o", "code-gone"), ("o", "code-a")]

    ran: list[str] = []

    async def recheck(org, cns, budget, result):
        ran.append(cns)

    monkeypatch.setattr(code_links.store, "code_namespaces", namespaces)
    monkeypatch.setattr(code_link_recheck, "_recheck_repository", recheck)
    configured = {"code-a"}
    monkeypatch.setattr(
        code_link_recheck,
        "repo_for_namespace",
        lambda org, cns: object() if cns in configured else None,
    )
    result = code_link_pass.PassResult()
    await code_link_recheck.recheck_all(result)
    assert ran == ["code-a"] and not result.errors


def test_decorators_and_template_heads_are_part_of_the_definition() -> None:
    from server.code.app.memory_links import file_definitions

    py = b"""import functools


@functools.lru_cache(
    maxsize=128,
)
def cached():
    return 1


x = [1]
def plain():
    return 2
"""
    defs = {d.name: d for d in file_definitions("m.py", py)}
    assert (defs["cached"].start_line, defs["plain"].start_line) == (4, 12)
    off = {d.name: d for d in file_definitions("m.py", py.replace(b"128", b"0"))}
    assert off["cached"].body_hash != defs["cached"].body_hash
    assert off["plain"].body_hash == defs["plain"].body_hash
    cpp = b"namespace a {\ntemplate <int N = 1>\nint f() { return N; }\n}\n"
    (f,) = [d for d in file_definitions("m.cpp", cpp) if d.name == "f"]
    (g,) = [d for d in file_definitions("m.cpp", cpp.replace(b"N = 1", b"N = 2")) if d.name == "f"]
    assert f.start_line == 2 and f.body_hash != g.body_hash


@pytest.mark.asyncio
async def test_a_mirror_git_cannot_read_raises_and_a_missing_file_does_not(tmp_path) -> None:
    from server.code.app.index.gitrepo import GitError, file_at

    git, _ = _repo(tmp_path)
    (tmp_path / "a.py").write_text("x = 1\n")
    git("add", ".")
    git("commit", "-q", "-m", "one")
    head, gd = git("rev-parse", "HEAD"), str(tmp_path / ".git")
    assert await file_at(gd, head, "a.py") == b"x = 1\n"
    assert await file_at(gd, head, "gone.py") is None
    with pytest.raises(GitError):
        await file_at(str(tmp_path / "nowhere"), head, "a.py")
    with pytest.raises(GitError):
        await file_at(gd, "0" * 40, "a.py")


@pytest.mark.asyncio
async def test_a_directory_or_submodule_at_a_path_is_no_file(tmp_path) -> None:
    from server.code.app.index.gitrepo import file_at

    git, commit = _repo(tmp_path)
    first = commit({"lib/b.py": "y = 2\n"})
    git("update-index", "--add", "--cacheinfo", f"160000,{first},sub")
    git("commit", "-q", "-m", "submodule")
    head, gd = git("rev-parse", "HEAD"), str(tmp_path / ".git")
    assert await file_at(gd, head, "lib/b.py") == b"y = 2\n"
    assert await file_at(gd, head, "lib") is None
    assert await file_at(gd, head, "sub") is None


@pytest.mark.asyncio
async def test_a_recheck_cut_short_resumes_after_the_last_path_it_wrote(monkeypatch) -> None:
    paths = [f"src/f{i:03d}.py" for i in range(2 * code_link_recheck._PATHS_PER_WRITE + 5)]
    rows = [_full({"path": p, "qualified_name": "f", "memory_id": p}) for p in paths]
    cursor: dict[str, str] = {}
    seen: list[str] = []

    async def kept_paths(org, cns, checked, head):
        return {p: p for p in paths}

    async def for_code(org, cns, *, paths=None, states=None, **kw):
        return [] if states else rows

    async def recheck(org, cns, part, changed, head, files):
        seen.extend(a["path"] for a in part)
        if len(seen) > code_link_recheck._PATHS_PER_WRITE:
            raise TimeoutError("the pass deadline")
        return []

    async def get(name):
        return cursor.get(name, "")

    async def put(name, value):
        cursor[name] = value

    async def nothing(*a, **k):
        return []

    monkeypatch.setattr(code_links.code, "kept_paths", kept_paths)
    _stub_reads(monkeypatch, for_code)
    monkeypatch.setattr(code_links.store, "lost", _none_lost)
    monkeypatch.setattr(code_link_recheck, "_recheck", recheck)
    monkeypatch.setattr(code_link_recheck, "_current_moves", nothing)
    monkeypatch.setattr(code_link_recheck, "_write_moves", nothing)
    monkeypatch.setattr(code_links.code, "link_cursor", get)
    monkeypatch.setattr(code_links.code, "set_link_cursor", put)
    files = code_link_pass.RepoReads("o", "code-a")
    with pytest.raises(TimeoutError):
        await code_link_recheck._follow(
            "o", "code-a", "c1", "c2", files, code_link_pass.PassResult()
        )
    first = list(seen)
    seen.clear()
    monkeypatch.setattr(code_link_recheck, "_recheck", lambda *a, **_: _record(seen, a[2]))
    await code_link_recheck._follow("o", "code-a", "c1", "c2", files, code_link_pass.PassResult())
    # the first part was written: the next pass starts after it, and ends clean
    assert seen == paths[code_link_recheck._PATHS_PER_WRITE :] and first[0] == paths[0]
    assert cursor["recheck/o/code-a"] == ""


async def _record(seen: list[str], part: list[dict[str, Any]]) -> list[Any]:
    seen.extend(a["path"] for a in part)
    return []


@pytest.mark.asyncio
async def test_a_finished_sweep_waits_before_its_next_cycle(monkeypatch) -> None:
    import time

    pages: list[Any] = []

    async def sweep_page(after, limit):
        pages.append(after)
        return None, 0

    monkeypatch.setattr(code_links.store, "sweep_page", sweep_page)
    done = await code_links.sweep_erased({}, 10, code_link_pass.PassResult())
    assert pages == [None] and "done_at" in done
    assert await code_links.sweep_erased(done, 10, code_link_pass.PassResult()) == done
    stale = {"done_at": time.time() - settings.memory_code_links_sweep_interval_s - 1}
    await code_links.sweep_erased(stale, 10, code_link_pass.PassResult())
    assert pages == [None, None]


@pytest.mark.asyncio
async def test_new_fingerprint_rules_rehash_links_from_their_own_commits(monkeypatch) -> None:
    from server.code.domain.anchor import Definition
    from server.code.domain.declaration import fingerprint_version

    def link(state: str) -> dict[str, Any]:
        return _full(
            {
                "org": "o",
                "code_namespace": "code-a",
                "path": "src/a.py",
                "qualified_name": "a::f",
                "anchored_name": "a::f",
                "anchored_path": "src/a.py",
                "anchored_commit": "c0" if state == "changed" else "c1",
                "checked_commit": "c2",
                "anchored_hash": "old1",
                "checked_hash": "old2",
                "body_hash": "old1" if state == "changed" else "old2",
                "state": state,
                "memory_id": state,
                "version": 7,
                "fingerprint_version": "0-old",
            }
        )

    async def stale(org, cns, version, *, after, limit):
        assert version == fingerprint_version() and after is None
        return [link("fresh"), link("changed")], None

    # c1 and c2 hold the same code; c0 older code
    async def defs(commit, path, qn):
        return [Definition(path, qn, "f", body_hash="new-c0" if commit == "c0" else "new-c2")]

    written: list[dict[str, Any]] = []

    async def insert(rows):
        written.extend(rows)

    monkeypatch.setattr(code_links.store, "stale_fingerprints", stale)
    monkeypatch.setattr(code_links.store, "insert", insert)
    files = code_link_pass.RepoReads("o", "code-a")
    monkeypatch.setattr(files, "defs", defs)
    await code_link_rehash.refingerprint("o", "code-a", files, code_link_pass.PassResult())
    fresh, changed = written
    # an unchanged link's baseline is its checked text; a changed one's its anchored text
    assert (fresh["body_hash"], fresh["checked_hash"]) == ("new-c2", "new-c2")
    assert (changed["body_hash"], changed["checked_hash"]) == ("new-c0", "new-c2")
    assert {r["fingerprint_version"] for r in written} == {fingerprint_version()}
    assert fresh["state"] == "fresh" and changed["state"] == "changed"


@pytest.mark.asyncio
async def test_a_missing_link_named_in_a_changed_file_and_every_unresolved_one_are_rechecked(
    monkeypatch,
) -> None:
    from server.code.domain.anchor import Definition

    lost = [
        _full({"path": "src/A.cpp", "qualified_name": "DB::fetch_rows", "memory_id": "m1"}),
        _full({"path": "src/C.cpp", "qualified_name": "DB::other_rows", "memory_id": "m2"}),
    ]
    lost = [a | {"state": "missing"} for a in lost]
    # an unresolved link is rechecked every pass: a rival candidate may go anywhere
    lost.append(
        _full(
            {
                "path": "src/D.cpp",
                "qualified_name": "DB::d",
                "memory_id": "m3",
                "state": "unresolved",
            }
        )
    )

    async def read_lost(org, cns, *, days):
        assert days == code_link_recheck._LOST_SEARCHED_DAYS
        return lost

    async def candidates(org, cns, *, names):
        return [Definition("src/B.cpp", "DB::fetch_rows", "fetch_rows")]

    monkeypatch.setattr(code_links.store, "lost", read_lost)
    monkeypatch.setattr(code_links.code, "candidates", candidates)
    found = await code_link_recheck._lost("o", "code-a", {"src/B.cpp"})
    assert [a["memory_id"] for a in found] == ["m3", "m1"]


@pytest.mark.asyncio
async def test_a_hash_whose_code_is_gone_is_cleared_and_every_page_is_rehashed(
    monkeypatch,
) -> None:
    from server.code.domain.declaration import fingerprint_version

    def link(mid: str, state: str, body: str, checked: str) -> dict[str, Any]:
        return _full(
            {
                "org": "o",
                "code_namespace": "code-a",
                "path": "src/a.py",
                "qualified_name": "a::f",
                "anchored_name": "a::f",
                "anchored_path": "src/a.py",
                "anchored_commit": "c1",
                "checked_commit": "c2",
                "anchored_hash": "old-a",
                "checked_hash": checked,
                "body_hash": body,
                "state": state,
                "memory_id": mid,
                "version": 1,
                "fingerprint_version": "0-old",
            }
        )

    pages = [
        [link("m1", "missing", "old-a", "old-a"), link("m2", "missing", "old-a", "old-c")],
        [link("m3", "unresolved", "old-a", "old-c")],
    ]

    starts: list[Any] = []

    async def stale(org, cns, version, *, after, limit):
        starts.append(after)
        return pages.pop(0), ("m1",) if pages else None

    async def defs(commit, path, qn):
        return []  # neither commit has the code any more

    written: list[dict[str, Any]] = []

    async def insert(rows):
        written.extend(rows)

    monkeypatch.setattr(code_links.store, "stale_fingerprints", stale)
    monkeypatch.setattr(code_links.store, "insert", insert)
    monkeypatch.setattr(code_link_rehash, "_REFINGERPRINT_PAGE", 2)
    files = code_link_pass.RepoReads("o", "code-a")
    monkeypatch.setattr(files, "defs", defs)
    result = code_link_pass.PassResult()
    await code_link_rehash.refingerprint("o", "code-a", files, result)
    # a link that last matched its baseline adopts what the next check finds; one that
    # had changed keeps a baseline no fingerprint matches, whatever it reads now
    assert {r["memory_id"]: r["body_hash"] for r in written} == {
        "m1": "",
        "m2": NO_BASELINE,
        "m3": NO_BASELINE,
    }
    assert {r["fingerprint_version"] for r in written} == {fingerprint_version()}
    assert result.errors == []
    assert starts == [None, ("m1",)]  # each page starts at the last one's end


@pytest.mark.asyncio
async def test_stale_links_read_only_repositories_the_caller_may_read(monkeypatch) -> None:
    asked: dict[str, Any] = {}

    async def stale_of_served(org, ns, states, memory_keys, **kw):
        asked.update(kw, states=states, keys=memory_keys)
        return [{"memory_key": "k"}]

    async def readable(org, nss):
        return {n for n in nss if n != "code-secret"}

    cfgs = [
        SimpleNamespace(org="o", repo="acme/a", git_remote="", namespace="code-a"),
        SimpleNamespace(org="x", repo="acme/x", git_remote="", namespace="code-x"),
        SimpleNamespace(org="o", repo="acme/s", git_remote="", namespace="code-secret"),
    ]
    monkeypatch.setattr(code_links.store, "stale_of_served", stale_of_served)
    monkeypatch.setattr(code_link_reads, "readable_namespaces", readable)
    monkeypatch.setattr(code_link_reads, "configured_repos", lambda: cfgs)
    monkeypatch.setattr(code_link_reads, "repo_tag", lambda r: f"repo:{r}")
    monkeypatch.setattr(
        code_link_reads.code, "trunk_namespace", lambda org, ns: "" if ns == "ns" else ns
    )
    assert await code_link_reads.stale_links("o", "ns", ["k"], limit=3) == [{"memory_key": "k"}]
    assert asked["states"] == ["changed", "unresolved", "missing"] and asked["keys"] == ["k"]
    assert asked["trunk"] == "" and asked["readable"] == ["code-a"] and asked["limit"] == 3
    assert asked["tag_namespaces"] == [("repo:acme/a", "code-a"), ("repo:acme/s", "code-secret")]
    monkeypatch.setattr(code_link_reads, "readable_namespaces", lambda org, nss: _async(set()))
    assert await code_link_reads.stale_links("o", "ns", limit=3) == []  # nothing readable


async def _async(value: Any) -> Any:
    return value


@pytest.mark.asyncio
async def test_stale_links_read_served_heads_and_only_the_columns_they_need() -> None:
    from server.memory.store import code_links as store

    class _Rec:
        sql = ""
        params: Any = None

        async def query_all(self, sql: str, params: Any = None) -> list[dict[str, Any]]:
            self.sql, self.params = sql, params
            return []

    ch = _Rec()
    kw: dict[str, Any] = {
        "trunk": "",
        "tag_namespaces": [],
        "readable": ["c"],
        "limit": 0,
    }
    assert await store.stale_of_served("o", "ns", ["missing"], ["k"], ch=ch, **kw) == []
    assert "SELECT *" not in ch.sql and "value" not in ch.sql
    assert "status IN ('active', 'conflicted')" in ch.sql and "tombstoned_at IS NULL" in ch.sql
    assert ch.sql.count("memory_key IN {mks:Array(String)}") == 2 and ch.params["mks"] == ["k"]
    assert "ORDER BY l.checked_at DESC" in ch.sql and ch.params["lim"] == 1  # the count rides it
    await store.stale_of_served("o", "ns", ["missing"], ch=ch, **kw)
    assert "mks:" not in ch.sql


@pytest.mark.asyncio
async def test_a_memory_get_reads_no_links_where_no_repository_is_configured(monkeypatch) -> None:
    async def for_memories(org, ids):
        raise AssertionError("no link is read")

    monkeypatch.setattr(code_links.store, "for_memories", for_memories)
    monkeypatch.setattr(code_links.code, "code_namespaces", lambda: [])
    assert await code_link_reads.links_of("o", _row("m")) == []


@pytest.mark.asyncio
async def test_a_memory_get_reads_no_links_of_a_memory_naming_no_repository(monkeypatch) -> None:
    async def for_memories(org, ids):
        raise AssertionError("no link is read")

    monkeypatch.setattr(code_links.store, "for_memories", for_memories)
    monkeypatch.setattr(code_links.code, "code_namespaces", lambda: ["code-a"])
    monkeypatch.setattr(code_link_reads, "code_namespace_for", lambda org, ns, tags: "")
    assert await code_link_reads.links_of("o", _row("m")) == []


@pytest.mark.asyncio
async def test_a_change_only_new_rules_see_reads_changed(monkeypatch) -> None:
    from server.code.domain.anchor import Definition

    # fresh under the old rules, which did not see `if True` become `if False`
    a = _full(
        {
            "org": "o",
            "code_namespace": "code-a",
            "path": "src/a.py",
            "qualified_name": "a::f",
            "anchored_name": "a::f",
            "anchored_path": "src/a.py",
            "anchored_commit": "c1",
            "checked_commit": "c2",
            "anchored_hash": "old",
            "checked_hash": "old",
            "body_hash": "old",
            "state": "fresh",
            "memory_id": "m",
            "version": 1,
        }
    )

    async def defs(commit, path, qn):
        return [
            Definition(
                path, qn, "f", body_hash=f"new-{commit}", shape=Shape(f"shape-{commit}", "", "")
            )
        ]

    files = code_link_pass.RepoReads("o", "code-a")
    monkeypatch.setattr(files, "defs", defs)
    out = await code_link_rehash._rehashed(a, files)
    assert (out["body_hash"], out["checked_hash"]) == ("new-c1", "new-c2")
    assert (out["state"], out["changed_since"]) == ("changed", "c2")


async def _differs_by_commit(commit, path, qn):
    """Another text at every commit."""
    from server.code.domain.anchor import Definition

    return [Definition(path, qn, "f", body_hash=f"new-{commit}")]


async def _same_everywhere(commit, path, qn):
    from server.code.domain.anchor import Definition

    return [Definition(path, qn, "f", body_hash="same")]


@pytest.mark.asyncio
async def test_a_rehash_trusts_the_old_match_once_the_anchored_commit_is_gone(monkeypatch) -> None:
    a = _full(
        {
            "org": "o",
            "code_namespace": "code-a",
            "path": "src/a.py",
            "qualified_name": "a::f",
            "anchored_name": "a::f",
            "anchored_path": "src/a.py",
            "anchored_commit": "rewritten",
            "checked_commit": "c2",
            "body_hash": "old",
            "checked_hash": "old",
            "state": "fresh",
        }
    )

    async def has(commit):
        return commit != "rewritten"

    files = code_link_pass.RepoReads("o", "code-a")
    monkeypatch.setattr(files, "has", has)
    monkeypatch.setattr(files, "defs", _differs_by_commit)
    out = await code_link_rehash._rehashed(a, files)
    assert out["body_hash"] == out["checked_hash"] == "new-c2" and "state" not in out


def _changed_link(**change: Any) -> dict[str, Any]:
    """A link the old rules read changed, with a correction judged for it."""
    return (
        _full(
            {
                "org": "o",
                "namespace": "ns",
                "code_namespace": "code-a",
                "path": "src/a.py",
                "qualified_name": "a::f",
                "anchored_name": "a::f",
                "anchored_path": "src/a.py",
                "anchored_commit": "c1",
                "checked_commit": "c2",
                "body_hash": "old-1",
                "checked_hash": "old-2",
                "judged_hash": "old-2",
                "suggestion": "`f` returns two",
                "policy_due": 1,
                "state": "changed",
                "changed_since": "c2",
            }
        )
        | change
    )


async def _refingerprinted(monkeypatch, row, policies, defs, has=None) -> dict[str, Any]:
    """`row` as one re-hash pass writes it, its namespace under `policies`."""

    async def stale(org, cns, version, *, after, limit):
        return [row], None

    async def policies_of(org, nss):
        return policies

    written: list[dict[str, Any]] = []

    async def insert(rows):
        written.extend(rows)

    monkeypatch.setattr(code_links.store, "stale_fingerprints", stale)
    monkeypatch.setattr(code_links.store, "code_change_policies", policies_of)
    monkeypatch.setattr(code_links.store, "insert", insert)
    files = code_link_pass.RepoReads("o", "code-a")
    monkeypatch.setattr(files, "defs", defs)
    if has is not None:
        monkeypatch.setattr(files, "has", has)
    await code_link_rehash.refingerprint("o", "code-a", files, code_link_pass.PassResult())
    (link,) = written
    return link


@pytest.mark.asyncio
@pytest.mark.parametrize(("change", "state"), [({}, "fresh"), ({"path": "src/b.py"}, "moved")])
async def test_a_change_only_old_rules_saw_reads_fresh_again(monkeypatch, change, state) -> None:
    row = _changed_link(memory_id="m", version=1, **change)
    link = await _refingerprinted(monkeypatch, row, {"ns": "revise"}, _same_everywhere)
    assert (link["state"], link["changed_since"], link["body_hash"]) == (state, "", "same")
    assert (link["suggestion"], link["judged_hash"], link["policy_due"]) == ("", "", 0)


@pytest.mark.asyncio
async def test_a_method_its_renamed_class_carried_stays_changed(monkeypatch) -> None:
    files = code_link_pass.RepoReads("o", "code-a")
    monkeypatch.setattr(files, "defs", _same_everywhere)
    assert "state" not in await code_link_rehash._rehashed(_changed_link(carried=1), files)


@pytest.mark.asyncio
@pytest.mark.parametrize(("policies", "due"), [({"ns": "revise"}, 1), ({}, 0)])
async def test_a_link_new_rules_read_changed_is_due_under_revise(
    monkeypatch, policies, due
) -> None:
    row = _changed_link(
        memory_id="m", version=1, state="fresh", body_hash="old", checked_hash="old"
    )
    link = await _refingerprinted(monkeypatch, row, policies, _differs_by_commit)
    assert (link["state"], link["policy_due"]) == ("changed", due)


@pytest.mark.asyncio
async def test_a_link_still_changed_keeps_its_due_mark(monkeypatch) -> None:
    row = _changed_link(memory_id="m", version=1, judged_hash="")
    link = await _refingerprinted(monkeypatch, row, {"ns": "revise"}, _differs_by_commit)
    assert (link["state"], link["policy_due"]) == ("changed", 1)


@pytest.mark.asyncio
async def test_a_judged_link_is_due_again_after_a_re_hash(monkeypatch) -> None:
    row = _changed_link(policy_due=0, memory_id="m", version=1)
    link = await _refingerprinted(monkeypatch, row, {"ns": "revise"}, _differs_by_commit)
    assert (link["state"], link["judged_hash"], link["policy_due"]) == ("changed", "", 1)


@pytest.mark.asyncio
async def test_a_missing_link_is_rehashed_from_the_commit_it_was_last_read_at(monkeypatch) -> None:
    from server.code.domain.anchor import Definition

    # missing at the head: its row keeps c2, the commit that last had the code
    a = _full(
        {
            "org": "o",
            "code_namespace": "code-a",
            "path": "src/a.py",
            "qualified_name": "a::f",
            "anchored_name": "a::f",
            "anchored_path": "src/a.py",
            "anchored_commit": "c1",
            "checked_commit": "c2",
            "anchored_hash": "old-a",
            "checked_hash": "old-c",
            "body_hash": "old-c",
            "state": "missing",
            "memory_id": "m",
            "version": 1,
        }
    )

    async def defs(commit, path, qn):
        return [Definition(path, qn, "f", body_hash=f"new-{commit}", shape=Shape("same", "", ""))]

    files = code_link_pass.RepoReads("o", "code-a")
    monkeypatch.setattr(files, "defs", defs)
    # a shape the new rules agree on (a rename): rebased on the checked text
    assert await code_link_rehash._rehashed(a, files) == {
        "anchored_hash": "new-c1",
        "checked_hash": "new-c2",
        "body_hash": "new-c2",
        "judged_hash": "",
        "former": [],
    }
    # a judgement, current under the old rules or not, is dropped: they may have missed a
    # change since it was made
    for judged in ("old-c", "older"):
        assert (await code_link_rehash._rehashed(a | {"judged_hash": judged}, files))[
            "judged_hash"
        ] == ""


def _python_re(posix: str) -> str:
    """A POSIX ERE from `edit_pattern` as Python's re reads it."""
    return posix.replace("[:alnum:]", "A-Za-z0-9").replace("[[:space:]]", r"\s")


def _reads(monkeypatch, files, at) -> None:
    """`files` reading through the fake `at`, its names-only reads too."""

    async def names(commit, path):
        found = await at(commit, path)
        return {d.qualified_name for d in found.defs} if found is not None else None

    monkeypatch.setattr(files, "at", at)
    monkeypatch.setattr(files, "names", names)


def _history(monkeypatch, files, commits, renames=None, *, traceable=True) -> None:
    """Answer `files`' history questions from `commits` ((commit, path) -> text; a file
    a commit does not list is not there), the commits in name order; `renames` (old ->
    new) are the range's renames, none that git can tell when not `traceable`."""
    order = sorted({c for c, _ in commits})

    def named(commit, path, name):
        # the lines gitrepo.edits_of's git log -G weighs
        pattern = edit_pattern(name, path)
        text = commits.get((commit, path)) or ""
        found = re.compile(_python_re(pattern)) if pattern else None
        return [line for line in text.splitlines() if not found or found.search(line)]

    async def edits_of(base, head, path, name):
        out, prev = [], base
        for c in (c for c in order if base < c <= head):
            if named(c, path, name) != named(prev, path, name):
                out.append((c, prev))
            prev = c
        return out

    async def touched(base, head):
        now = {p for c, p in commits if c == head}
        return {p for p in now if commits.get((head, p)) != commits.get((base, p))}

    async def renamed_from(base, head, paths):
        if not traceable:
            return None
        return {new: old for old, new in (renames or {}).items() if new in paths and new != old}

    monkeypatch.setattr(files, "edits_of", edits_of)
    monkeypatch.setattr(files, "touched", touched)
    monkeypatch.setattr(files, "renamed_from", renamed_from)


def _file(path: str, src: str) -> Any:
    from server.code.app.memory_links import FileAt, file_definitions

    return FileAt(src.split("\n"), file_definitions(path, src.encode()))


@pytest.mark.asyncio
async def test_a_missing_link_is_not_handed_to_a_definition_added_later(monkeypatch) -> None:
    keep = "def keep():\n    return 0\n"
    c1 = keep + "\n\ndef fetch_rows(n):\n    return n + 1\n"
    c3 = keep + "\n\ndef load_rows(n):\n    return n * 2\n"
    commits = {"c1": c1, "c2": keep, "c3": c3}
    files = code_link_pass.RepoReads("o", "code-a")

    async def at(commit, path):
        return _file(path, commits[commit])

    async def candidates(org, cns, **kw):
        return []

    _reads(monkeypatch, files, at)
    monkeypatch.setattr(code_links.code, "candidates", candidates)
    # deleted at c2: missing, still holding c1, the commit it was last read at
    row = _full(
        {
            "org": "o",
            "code_namespace": "code-a",
            "path": "m.py",
            "qualified_name": "m::fetch_rows",
            "body_hash": "h",
            "checked_hash": "h",
            "state": "missing",
            "changed_since": "c2",
            "checked_commit": "c1",
            "memory_id": "m1",
            "version": 1,
        }
    )
    moves = await code_link_recheck._recheck(
        "o", "code-a", [row], code_link_pass.Range("c1", {"m.py": "m.py"}), "c3", files
    )
    assert moves == []  # `load_rows`, added later, is not it


@pytest.mark.asyncio
async def test_code_not_found_keeps_the_commit_it_was_last_read_at(monkeypatch) -> None:
    from server.code.domain.anchor import Link

    written: list[dict[str, Any]] = []

    async def insert(rows):
        written.extend(rows)

    async def revised(links):
        return set()

    monkeypatch.setattr(code_links.store, "insert", insert)
    monkeypatch.setattr(code_link_policy, "revised_namespaces", revised)
    row = _full(
        {
            "org": "o",
            "namespace": "ns",
            "code_namespace": "code-a",
            "path": "m.py",
            "qualified_name": "m::f",
            "checked_commit": "c1",
            "memory_id": "m1",
            "version": 1,
        }
    )
    moves = [
        code_link_recheck.Move({**row, "memory_id": state}, Link("m.py", "m::f", "h", state))
        for state in ("missing", "unresolved", "fresh", "changed")
    ]
    await code_link_recheck._write_moves(moves, "c2")
    assert {r["memory_id"]: r["checked_commit"] for r in written} == {
        "missing": "c1",
        "unresolved": "c1",
        "fresh": "c2",
        "changed": "c2",
    }


@pytest.mark.asyncio
async def test_a_search_with_no_linked_hits_reads_no_memory_heads(monkeypatch) -> None:
    asked: list[Any] = []

    async def live_links(org, idents):
        asked.append(idents)
        return []

    async def no_read(*a, **k):
        raise AssertionError("nothing more is read for hits without links")

    monkeypatch.setattr(code_links.store, "live_links", live_links)
    for name in ("served_heads", "code_change_policies"):
        monkeypatch.setattr(code_links.store, name, no_read)
    monkeypatch.setattr(code_link_reads, "readable_namespaces", no_read)
    monkeypatch.setattr(code_links.code, "code_namespaces", lambda: ["code-a"])
    hits = [_hit("k1"), _hit("k2", who=None)]  # the second hit's leg read no identity
    assert await code_link_reads.hit_memories(_search(), hits) == [None, None]
    assert asked == [[("agent-a", "a", "-", "-", "k1")]]
    await code_link_reads.annotate_hits(_search(), hits)
    assert [h.code_state for h in hits] == ["", ""] and len(asked) == 2
    # no repository configured: nothing is read at all
    monkeypatch.setattr(code_links.code, "code_namespaces", lambda: [])
    assert await code_link_reads.hit_memories(_search(), hits) == [None, None]
    assert len(asked) == 2


@pytest.mark.asyncio
async def test_live_links_are_read_by_identity_through_the_key_index() -> None:
    import uuid

    from server.memory.store import code_links as store

    class _Rec:
        def __init__(self) -> None:
            self.sql: list[tuple[str, Any]] = []

        async def query_all(self, sql: str, params: Any = None) -> list[dict[str, Any]]:
            self.sql.append((sql, params))
            return [{"memory_id": uuid.UUID(int=1), "memory_key": "k"}]

    ch = _Rec()
    assert await store.live_links("o", [], ch=ch) == [] and not ch.sql
    ident = ("n", "a", "-", "-", "k")
    (row,) = await store.live_links("o", [ident, ident], ch=ch)
    assert row["memory_id"] == str(uuid.UUID(int=1))
    ((sql, params),) = ch.sql
    assert params["ids"] == [ident] and params["mks"] == ["k"]  # the memory_key index narrows
    assert "FINAL WHERE tombstoned_at IS NULL" in sql


def test_links_off_never_hide_a_memory(monkeypatch) -> None:
    from server.settings import settings

    memory = {"namespace": "ns", "pinned": 0, "lock_mode": "", "verified_by": "", "sha": ""}
    gone = [{"state": "missing", "complete": 1}]
    assert code_link_reads._hidden(memory, gone, {})
    monkeypatch.setattr(settings, "memory_code_links_enabled", False)
    assert not code_link_reads._hidden(memory, gone, {})


@pytest.mark.asyncio
async def test_a_resumed_recheck_reads_the_links_a_cut_pass_already_moved(monkeypatch) -> None:
    read: list[Any] = []

    async def kept_paths(org, cns, checked, head):
        return {"a.py": "b.py", "gone.py": ""}

    async def for_code(org, cns, *, paths=None, states=None, **kw):
        read.append(paths)
        return []

    async def get(name):
        return ""

    async def put(name, value):
        return None

    monkeypatch.setattr(code_links.code, "kept_paths", kept_paths)
    _stub_reads(monkeypatch, for_code)
    monkeypatch.setattr(code_links.store, "lost", _none_lost)
    monkeypatch.setattr(code_links.code, "link_cursor", get)
    monkeypatch.setattr(code_links.code, "set_link_cursor", put)
    await code_link_recheck._follow(
        "o",
        "code-a",
        "c0",
        "h2",
        code_link_pass.RepoReads("o", "code-a"),
        code_link_pass.PassResult(),
    )
    assert read[0] == ["a.py", "b.py", "gone.py"]


def _repository(monkeypatch, *, head: str, checked: str, cursor: str, diffs: dict) -> Any:
    """A repository at `head`, checked at `checked`, with the recheck cursor `cursor`
    and `diffs` (from, to) -> kept paths; the state it ends in, and what was rechecked."""
    state: dict[str, Any] = {"checked": checked, "cursor": cursor, "rechecked": [], "read": []}

    async def nothing(*a, **k):
        return []

    async def kept_paths(org, cns, base, to):
        return diffs[(base, to)]

    async def for_code(org, cns, *, paths=None, states=None, **kw):
        if states:
            return []
        state["read"].append(paths)
        return [_full({"path": p, "qualified_name": "m::f", "memory_id": p}) for p in paths or []]

    async def recheck(org, cns, part, changed, at, files):
        state["rechecked"].append((at, [a["path"] for a in part]))
        return []

    async def get(name):
        return state["cursor"]

    async def put(name, value):
        state["cursor"] = value

    async def indexed_head(org, cns):
        return head

    async def checked_head(org, cns):
        return state["checked"]

    async def set_checked_head(org, cns, at):
        state["checked"] = at

    for module, name in (
        (code_link_recheck, "_resolve_pending"),
        (code_link_recheck, "_write_moves"),
        (code_link_recheck, "_current_moves"),
        (code_link_recheck, "_retry_unparsed"),
        (code_link_rehash, "refingerprint"),
        (code_link_policy, "apply_policies"),
    ):
        monkeypatch.setattr(module, name, nothing)
    monkeypatch.setattr(code_link_recheck, "_recheck", recheck)
    _stub_reads(monkeypatch, for_code)
    monkeypatch.setattr(code_links.store, "lost", _none_lost)
    for name, fn in (
        ("kept_paths", kept_paths),
        ("link_cursor", get),
        ("set_link_cursor", put),
        ("indexed_head", indexed_head),
        ("checked_head", checked_head),
        ("set_checked_head", set_checked_head),
    ):
        monkeypatch.setattr(code_links.code, name, fn)
    return state


@pytest.mark.asyncio
async def test_a_pass_cut_at_another_head_finishes_there_before_the_next(monkeypatch) -> None:
    # cut at h2 after b.py; h3 has landed since
    state = _repository(
        monkeypatch,
        head="h3",
        checked="c0",
        cursor=json.dumps({"head": "h2", "after": "b.py"}),
        diffs={
            ("c0", "h2"): {"a.py": "a.py", "b.py": "b.py", "c.py": "c.py"},
            ("h2", "h3"): {"a.py": "a.py"},
        },
    )
    await code_link_recheck._recheck_repository(
        "o", "code-a", code_link_pass.Budget(0), code_link_pass.PassResult()
    )
    # h2 resumes after b.py; then h3 is followed from its start
    assert state["rechecked"] == [("h2", ["c.py"]), ("h3", ["a.py"])]
    assert (state["checked"], state["cursor"]) == ("h3", "")


@pytest.mark.asyncio
async def test_a_cut_head_the_mirror_lost_is_dropped_not_followed(monkeypatch) -> None:
    state = _repository(
        monkeypatch,
        head="h3",
        checked="c0",
        cursor=json.dumps({"head": "gone", "after": "b.py"}),
        diffs={("c0", "h3"): {"a.py": "a.py"}},
    )

    async def has(self, commit):
        return commit != "gone"

    monkeypatch.setattr(code_link_pass.RepoReads, "has", has)
    await code_link_recheck._recheck_repository(
        "o", "code-a", code_link_pass.Budget(0), code_link_pass.PassResult()
    )
    # what that pass wrote is in no diff: every link is read again, as after a force-push
    assert state["read"] == [None] and state["checked"] == "h3"


@pytest.mark.asyncio
async def test_a_link_whose_last_read_commit_is_gone_is_unresolved(monkeypatch) -> None:
    async def has(self, commit):
        return False

    monkeypatch.setattr(code_link_pass.RepoReads, "has", has)
    files = code_link_pass.RepoReads("o", "code-a")
    row = _full({"checked_commit": "gone", "path": "m.py", "qualified_name": "m::f"})
    found = await code_link_recheck._renamed(files, code_link_pass.link_of(row), row, "m.py", "h3")
    assert found.definition is None and found.uncertain


def _repo(root: Path) -> tuple[Callable[..., str], Callable[[dict[str, str | None]], str]]:
    """A new git repository at `root`, for tests that read real history: `git` (its
    output) and `commit(files)`, which writes them (None deletes one) and commits."""

    def git(*args: str) -> str:
        done = subprocess.run(["git", *args], cwd=root, check=True, capture_output=True, text=True)
        return done.stdout.strip()

    def commit(files: dict[str, str | None]) -> str:
        for name, text in files.items():
            path = root / name
            if text is None:
                path.unlink()
            else:
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text(text)
        git("add", "-A")
        git("commit", "-q", "-m", "c")
        return git("rev-parse", "HEAD")

    root.mkdir(parents=True, exist_ok=True)
    git("init", "-q", "-b", "main")
    git("config", "user.email", "t@example.com")
    git("config", "user.name", "t")
    return git, commit


def _one_commit_repo(root: Any) -> tuple[str, str]:
    """A repository with one commit under `root`: (its git dir, the commit)."""
    _, commit = _repo(root / "w")
    return str(root / "w" / ".git"), commit({"f.py": "x = 1\n"})


@pytest.mark.asyncio
async def test_the_mirror_tells_a_commit_it_lost_from_a_mirror_it_cannot_read(tmp_path) -> None:
    from server.code.app.index import gitrepo

    git_dir, sha = _one_commit_repo(tmp_path)
    assert await gitrepo.has_commit(git_dir, sha)
    assert not await gitrepo.has_commit(git_dir, "d" * 40)
    with pytest.raises(gitrepo.GitError):
        await gitrepo.has_commit(str(tmp_path / "nowhere"), sha)


@pytest.mark.asyncio
async def test_a_rehash_with_no_baseline_text_left_adopts_the_next_check(
    monkeypatch,
) -> None:
    async def has(self, commit):
        return commit == "c1"  # the anchored commit is still held; the checked one is gone

    monkeypatch.setattr(code_link_pass.RepoReads, "has", has)
    files = code_link_pass.RepoReads("o", "code-a")
    monkeypatch.setattr(files, "defs", _differs_by_commit)
    link = _full(
        {
            "path": "a.py",
            "qualified_name": "a::f",
            "anchored_name": "a::f",
            "anchored_path": "a.py",
            "anchored_commit": "c1",
            "checked_commit": "c2",
            "body_hash": "old",
            "checked_hash": "old",
            "state": "fresh",
        }
    )
    assert (await code_link_rehash._rehashed(link, files))["body_hash"] == "new-c1"
    gone = link | {"anchored_commit": "c0"}  # both lost: the next check finds its baseline
    assert (await code_link_rehash._rehashed(gone, files))["body_hash"] == ""
    changed = gone | {"checked_hash": "older", "state": "changed"}
    assert (await code_link_rehash._rehashed(changed, files))["body_hash"] == NO_BASELINE


@pytest.mark.asyncio
async def test_a_rehash_that_finds_no_anchor_definition_keeps_a_fresh_link_fresh(
    monkeypatch,
) -> None:
    async def has(commit):
        return True

    async def defs(commit, path, qn):
        return [] if commit == "c1" else await _differs_by_commit(commit, path, qn)

    files = code_link_pass.RepoReads("o", "code-a")
    monkeypatch.setattr(files, "has", has)
    monkeypatch.setattr(files, "defs", defs)
    link = _full(
        {
            "path": "a.py",
            "qualified_name": "a::f",
            "anchored_name": "a::f",
            "anchored_path": "a.py",
            "anchored_commit": "c1",
            "checked_commit": "c2",
            "body_hash": "old",
            "checked_hash": "old",
            "state": "fresh",
        }
    )
    out = await code_link_rehash._rehashed(link, files)
    assert out["body_hash"] == "new-c2" and "state" not in out


def _judged_files(monkeypatch, texts: dict[str, str], path: str = "a.ts"):
    """`RepoReads` reading `texts` (commit -> source of `path`), and the judge's calls."""
    from server.memory.app import code_link_judge

    async def at(commit, p):
        return _file(p, texts[commit]) if p == path and commit in texts else None

    async def has(commit):
        return commit in texts

    calls: list[tuple[str, str]] = []

    async def judge(claim, before, after):
        calls.append((before, after))
        return "holds", ""

    files = code_link_pass.RepoReads("o", "code-a")
    _reads(monkeypatch, files, at)
    monkeypatch.setattr(files, "has", has)
    monkeypatch.setattr(code_link_judge, "judge_claim", judge)
    return files, calls


async def _judged(files, claim, link, head, budget) -> Any:
    """`link` judged as the policy judges it: its texts read, then the judge asked."""
    texts = await code_link_policy._texts(files, claim, link, head)
    return texts and await code_link_policy._judge(claim, link, texts, head, budget)


def _judged_link(qn: str, path: str = "a.ts") -> dict[str, Any]:
    return _full(
        {
            "path": path,
            "qualified_name": qn,
            "anchored_commit": "c1",
            "anchored_path": path,
            "anchored_name": qn,
            "state": "changed",
        }
    )


@pytest.mark.asyncio
async def test_a_method_its_renamed_class_carried_is_left_changed(monkeypatch) -> None:
    texts = {
        "c1": "class Reader {\n  f() { return 1; }\n}\n",
        "c2": "class Writer {\n  f() { return 2; }\n}\n",
    }
    files, calls = _judged_files(monkeypatch, texts)
    link = _judged_link("a::Writer::f") | {"carried": 1}
    budget = code_link_pass.Budget(judgements=1)
    assert await _judged(files, "claim", link, "c2", budget) is None
    assert (calls, budget.judgements) == ([], 1)


@pytest.mark.asyncio
async def test_the_judge_reads_an_overload_signature_the_index_does_not_list(monkeypatch) -> None:
    signatures = "export function f(a: number): number;\nexport function f(a: string): string;\n"
    body = "export function f(a: any): any {\n  return a;\n}\n"
    texts = {"c1": signatures + body, "c2": signatures.split("\n")[0] + "\n" + body}
    files, calls = _judged_files(monkeypatch, texts)
    budget = code_link_pass.Budget(judgements=1)
    await _judged(files, "claim", _judged_link("a::f"), "c2", budget)
    ((before, after),) = calls
    assert "f(a: string)" in before and "f(a: string)" not in after


@pytest.mark.asyncio
async def test_the_same_text_on_both_sides_is_not_judged(monkeypatch) -> None:
    from server.code.domain.anchor import Definition

    async def defs(commit, path, qn):
        return [Definition(path, qn, "f", body_hash=f"new-{commit}", shown="int f();")]

    files, calls = _judged_files(monkeypatch, {"c1": "", "c2": ""})
    monkeypatch.setattr(files, "defs", defs)
    budget = code_link_pass.Budget(judgements=1)
    assert await _judged(files, "claim", _judged_link("a::f"), "c2", budget) is None
    assert (calls, budget.judgements) == ([], 1)


@pytest.mark.asyncio
async def test_a_link_the_judge_cannot_read_costs_no_judgement(monkeypatch) -> None:
    async def has(self, commit):
        return False

    monkeypatch.setattr(code_link_pass.RepoReads, "has", has)
    budget = code_link_pass.Budget(judgements=1)
    link = _full({"path": "a.py", "qualified_name": "a::f", "anchored_commit": "gone"})
    assert await _judged(code_link_pass.RepoReads("o", "c"), "claim", link, "h", budget) is None
    assert budget.judgements == 1


@pytest.mark.asyncio
async def test_the_judge_reads_an_overload_that_was_removed(monkeypatch) -> None:
    from server.memory.app import code_link_judge

    texts = {
        "c1": "int f(double a) { return 0; }\n#ifdef FAST\nint f(int a) { return a; }\n#endif\n",
        "c2": "int f(int a) { return a + 1; }\n",
    }

    async def at(commit, path):
        return _file(path, texts[commit])

    async def has(commit):
        return True

    seen = []

    async def judge(claim, before, after):
        seen.append((before, after))
        return "unsure", ""

    files = code_link_pass.RepoReads("o", "code-a")
    _reads(monkeypatch, files, at)
    monkeypatch.setattr(files, "has", has)
    monkeypatch.setattr(code_link_judge, "judge_claim", judge)
    link = _full(
        {
            "path": "a.cpp",
            "qualified_name": "f",
            "anchored_commit": "c1",
            "anchored_path": "a.cpp",
            "anchored_name": "f",
        }
    )
    assert await _judged(files, "claim", link, "c2", code_link_pass.Budget(judgements=1)) is None
    old = (
        "int f(double a) { return 0; }\n\n(selected when: #ifdef FAST)\nint f(int a) { return a; }"
    )
    assert seen == [(old, "int f(int a) { return a + 1; }")]
    # no such definition where it was written: nothing to judge, no judgement spent
    budget = code_link_pass.Budget(judgements=1)
    assert await _judged(files, "claim", link | {"anchored_name": "g"}, "c2", budget) is None
    assert len(seen) == 1 and budget.judgements == 1


@pytest.mark.asyncio
async def test_a_full_recheck_after_a_lost_head_resumes_as_one(monkeypatch) -> None:
    paths = [f"p{i:03d}.py" for i in range(2 * code_link_recheck._PATHS_PER_WRITE)]
    state = _repository(
        monkeypatch,
        head="h3",
        checked="c0",
        cursor=json.dumps({"head": "gone", "after": "a.py"}),
        diffs={("c0", "h3"): {"p099.py": "p099.py"}},
    )

    async def every(org, cns, *, paths=None, states=None, **kw):
        listed = paths if paths is not None else [p for p in globals_paths]
        return (
            []
            if states
            else [_full({"path": p, "qualified_name": "m::f", "memory_id": p}) for p in listed]
        )

    globals_paths = paths
    cuts = {"left": 1}

    async def recheck(org, cns, part, changed, at, files):
        state["rechecked"].append((at, [a["path"] for a in part]))
        if len(state["rechecked"]) == 2 and cuts["left"]:
            cuts["left"] -= 1
            raise TimeoutError("the pass deadline")
        return []

    async def has(self, commit):
        return commit != "gone"

    _stub_reads(monkeypatch, every)
    monkeypatch.setattr(code_link_recheck, "_recheck", recheck)
    monkeypatch.setattr(code_link_pass.RepoReads, "has", has)
    budget, result = code_link_pass.Budget(0), code_link_pass.PassResult()
    with pytest.raises(TimeoutError):
        await code_link_recheck._recheck_repository("o", "code-a", budget, result)
    state["rechecked"].clear()
    await code_link_recheck._recheck_repository("o", "code-a", budget, result)
    # the second pass goes on with every link after the cut, not only the c0..h3 diff
    assert state["rechecked"] == [("h3", paths[code_link_recheck._PATHS_PER_WRITE :])]
    assert state["checked"] == "h3"


_ROWS = "def fetch_rows(n):\n    out = []\n    for i in range(n):\n        out.append(i)\n    return out\n"


@pytest.mark.parametrize(
    ("moved_to", "state"),
    [
        ({"pkg/b.py": _ROWS.replace("append(i)", "append(i * 2)")}, "changed"),  # close
        ({"pkg/b.py": "def fetch_rows(n):\n    return {'unrelated': n}\n"}, "unresolved"),
        (
            {
                "pkg/b.py": _ROWS.replace("append(i)", "append(i * 2)"),
                "pkg/c.py": _ROWS.replace("append(i)", "append(i * 3)"),
            },
            "unresolved",  # two may be it
        ),
    ],
)
@pytest.mark.asyncio
async def test_a_function_moved_into_another_module_and_edited(
    monkeypatch, moved_to, state
) -> None:
    from server.code.domain.anchor import Definition

    keep = "def keep():\n    return 0\n"
    commits = {("c1", "pkg/a.py"): _ROWS + "\n\n" + keep, ("c2", "pkg/a.py"): keep}
    commits |= {("c2", p): src for p, src in moved_to.items()}
    files = code_link_pass.RepoReads("o", "code-a")

    async def at(commit, path):
        src = commits.get((commit, path))
        return _file(path, src) if src is not None else None

    async def candidates(org, cns, *, names=None, qualified_names=None):
        return [
            Definition(p, p[:-3].replace("/", ".") + "::fetch_rows", "fetch_rows") for p in moved_to
        ]

    _reads(monkeypatch, files, at)
    _history(monkeypatch, files, commits)
    monkeypatch.setattr(code_links.code, "candidates", candidates)
    row = _full(
        {
            "org": "o",
            "code_namespace": "code-a",
            "path": "pkg/a.py",
            "qualified_name": "pkg.a::fetch_rows",
            "body_hash": "h",
            "checked_hash": "h",
            "state": "fresh",
            "changed_since": "",
            "checked_commit": "c1",
            "memory_id": "m1",
            "version": 1,
        }
    )
    # the range lists what it added, as git does
    changed = code_link_pass.Range("c1", {"pkg/a.py": "pkg/a.py"} | {p: p for p in moved_to})
    moves = await code_link_recheck._recheck("o", "code-a", [row], changed, "c2", files)
    assert [m.link.state for m in moves] == [state]


@pytest.mark.parametrize(
    "theirs",
    [
        _ROWS.replace("append(i)", "append(i + 1)"),  # close text
        "def fetch_rows(db):\n    return db.query('select 1')\n",  # unrelated
    ],
)
@pytest.mark.asyncio
async def test_a_namesake_already_in_another_module_never_takes_a_deleted_function(
    monkeypatch, theirs
) -> None:
    from server.code.domain.anchor import Definition

    keep = "def keep():\n    return 0\n"
    commits = {
        ("c1", "pkg/a.py"): _ROWS + "\n\n" + keep,
        ("c2", "pkg/a.py"): keep,
        ("c1", "pkg/b.py"): theirs,  # there before the deletion: another definition
        ("c2", "pkg/b.py"): theirs,
    }
    files = code_link_pass.RepoReads("o", "code-a")

    async def at(commit, path):
        src = commits.get((commit, path))
        return _file(path, src) if src is not None else None

    async def candidates(org, cns, *, names=None, qualified_names=None):
        return [Definition("pkg/b.py", "pkg.b::fetch_rows", "fetch_rows")]

    _reads(monkeypatch, files, at)
    _history(monkeypatch, files, commits)
    monkeypatch.setattr(code_links.code, "candidates", candidates)
    row = _full(
        {
            "org": "o",
            "code_namespace": "code-a",
            "path": "pkg/a.py",
            "qualified_name": "pkg.a::fetch_rows",
            "body_hash": "h",
            "checked_hash": "h",
            "state": "fresh",
            "changed_since": "",
            "checked_commit": "c1",
            "memory_id": "m1",
            "version": 1,
        }
    )
    moves = await code_link_recheck._recheck(
        "o", "code-a", [row], code_link_pass.Range("c1", {"pkg/a.py": "pkg/a.py"}), "c2", files
    )
    assert [(m.link.state, m.link.path) for m in moves] == [("missing", "pkg/a.py")]
    # and once missing, a later edit of b.py hands it nothing either
    gone = row | {"state": "missing", "changed_since": "c2"}
    commits[("c3", "pkg/b.py")] = theirs + "\n\ndef x():\n    return 1\n"
    later = await code_link_recheck._recheck(
        "o", "code-a", [gone], code_link_pass.Range("c2", {"pkg/b.py": "pkg/b.py"}), "c3", files
    )
    assert later == []


@pytest.mark.asyncio
async def test_a_namesake_carried_in_by_a_file_rename_is_not_new(monkeypatch) -> None:
    from server.code.domain.anchor import Definition

    keep = "def keep():\n    return 0\n"
    theirs = _ROWS.replace("append(i)", "append(i + 1)")
    commits = {
        ("c1", "pkg/a.py"): _ROWS + "\n\n" + keep,
        ("c1", "pkg/c.py"): theirs,  # there at the check, as pkg/c.py
        ("c2", "pkg/a.py"): keep,
        ("c2", "pkg/b.py"): theirs,  # c.py renamed to b.py in the same commit
    }
    files = code_link_pass.RepoReads("o", "code-a")

    async def at(commit, path):
        src = commits.get((commit, path))
        return _file(path, src) if src is not None else None

    async def candidates(org, cns, *, names=None, qualified_names=None):
        return [Definition("pkg/b.py", "pkg.b::fetch_rows", "fetch_rows")]

    _reads(monkeypatch, files, at)
    _history(monkeypatch, files, commits, {"pkg/c.py": "pkg/b.py"})
    monkeypatch.setattr(code_links.code, "candidates", candidates)
    row = _full(
        {
            "org": "o",
            "code_namespace": "code-a",
            "path": "pkg/a.py",
            "qualified_name": "pkg.a::fetch_rows",
            "body_hash": "h",
            "checked_hash": "h",
            "state": "fresh",
            "changed_since": "",
            "checked_commit": "c1",
            "memory_id": "m1",
            "version": 1,
        }
    )
    kept = {"pkg/a.py": "pkg/a.py", "pkg/c.py": "pkg/b.py"}
    moves = await code_link_recheck._recheck(
        "o", "code-a", [row], code_link_pass.Range("c1", kept), "c2", files
    )
    assert [(m.link.state, m.link.path) for m in moves] == [("missing", "pkg/a.py")]


async def _recheck_across(
    monkeypatch, commits, state, since, renames=None, checked="c0", traceable=True
) -> list[tuple[str, str]]:
    """A link on pkg/a.py::fetch_rows, last read at `checked`, rechecked at c2 over the
    range from `since` that changed a.py and b.py, with every namesake there at c2;
    `renames` (old -> new) are those from the link's check to c2. Commits run c0..c3."""
    from server.code.domain.anchor import Definition

    async def at(commit, path):
        src = commits.get((commit, path))
        return _file(path, src) if src is not None else None

    async def descends(ancestor, descendant):
        return ancestor <= descendant

    async def range_from(base, head):
        return None if renames is None else code_link_pass.Range(base, renames)

    async def candidates(org, cns, *, names=None, qualified_names=None):
        paths = sorted({p for c, p in commits if c == "c2"} - {"pkg/a.py"})
        return [Definition(p, f"pkg.{p[4]}::fetch_rows", "fetch_rows") for p in paths]

    files = code_link_pass.RepoReads("o", "code-a")
    _reads(monkeypatch, files, at)
    monkeypatch.setattr(files, "is_ancestor", descends)
    monkeypatch.setattr(files, "range", range_from)
    _history(monkeypatch, files, commits, renames, traceable=traceable)
    monkeypatch.setattr(code_links.code, "candidates", candidates)
    row = _full(
        {
            "org": "o",
            "code_namespace": "code-a",
            "path": "pkg/a.py",
            "qualified_name": "pkg.a::fetch_rows",
            "body_hash": "h",
            "checked_hash": "h",
            "state": state,
            "changed_since": "",
            "checked_commit": checked,
            "memory_id": "m1",
            "version": 1,
        }
    )
    kept = {"pkg/a.py": "pkg/a.py", "pkg/b.py": "pkg/b.py"}
    changed = code_link_pass.Range(since, kept) if since else None
    moves = await code_link_recheck._recheck("o", "code-a", [row], changed, "c2", files)
    return [(m.link.state, m.link.path) for m in moves]


@pytest.mark.asyncio
async def test_a_namesake_there_when_the_range_starts_is_not_new(monkeypatch) -> None:
    # the link was last written at c0 and this pass covers c1 -> c2 only; b.py's
    # fetch_rows came between, outside the range, while a.py still had its own
    keep = "def keep():\n    return 0\n"
    theirs = _ROWS.replace("append(i)", "append(i + 1)")
    commits = {
        ("c0", "pkg/a.py"): _ROWS + "\n\n" + keep,
        ("c1", "pkg/a.py"): _ROWS + "\n\n" + keep,
        ("c1", "pkg/b.py"): theirs,
        ("c2", "pkg/a.py"): keep,
        ("c2", "pkg/b.py"): theirs,
    }
    assert await _recheck_across(monkeypatch, commits, "fresh", "c1") == [("missing", "pkg/a.py")]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "found",
    [
        {"pkg/b.py": _ROWS.replace("append(i)", "append(i * 2)"), "pkg/c.py": _ROWS},  # several
        {"pkg/b.py": "def fetch_rows(n):\n    return {'unrelated': n}\n"},  # not close
    ],
    ids=["several", "not-close"],
)
async def test_an_unresolved_link_keeps_its_candidates_over_a_later_range(
    monkeypatch, found
) -> None:
    # fetch_rows left a.py between c0 and c1, an earlier pass left the link unresolved;
    # an unrelated edit to b.py in c1 -> c2 must not make its candidates "already there"
    commits = {("c0", "pkg/a.py"): _ROWS, ("c1", "pkg/a.py"): "", ("c2", "pkg/a.py"): ""}
    for path, src in found.items():
        commits[("c1", path)] = src
        commits[("c2", path)] = src + ("\nX = 1\n" if path == "pkg/b.py" else "")
    assert await _recheck_across(monkeypatch, commits, "unresolved", "c1") == []


_CLOSE_ROWS = _ROWS.replace("append(i)", "append(i * 2)")


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("state", "since", "later", "renames", "after"),
    [
        # unresolved at c0 over c.py and d.py; z.py -> b.py came with them, so b.py's
        # namesake was there at the check, and once c.py and d.py go nothing is new
        (
            "unresolved",
            "c1",
            {("c1", "pkg/c.py"): _CLOSE_ROWS, ("c1", "pkg/d.py"): _CLOSE_ROWS},
            {"pkg/a.py": "pkg/a.py", "pkg/z.py": "pkg/b.py"},
            [("missing", "pkg/a.py")],
        ),
        # a full recheck maps the same rename from the link's check
        (
            "fresh",
            "",
            {},
            {"pkg/a.py": "pkg/a.py", "pkg/z.py": "pkg/b.py"},
            [("missing", "pkg/a.py")],
        ),
        # and without a range from the check (a force-push) it cannot tell
        ("fresh", "", {}, None, [("unresolved", "pkg/a.py")]),
    ],
    ids=["unresolved", "full-recheck", "no-range"],
)
async def test_a_namesake_renamed_since_the_check_is_not_new(
    monkeypatch, state, since, later, renames, after
) -> None:
    commits = {
        ("c0", "pkg/a.py"): _ROWS,
        ("c0", "pkg/z.py"): _CLOSE_ROWS,
        ("c1", "pkg/a.py"): "",
        ("c1", "pkg/b.py"): _CLOSE_ROWS,
        ("c2", "pkg/a.py"): "",
        ("c2", "pkg/b.py"): _CLOSE_ROWS + "\nX = 1\n",
    } | later
    found = await _recheck_across(
        monkeypatch, commits, state, since, renames=renames, traceable=renames is not None
    )
    assert found == after


@pytest.mark.asyncio
async def test_a_namesake_there_when_a_newer_link_was_made_is_not_new(monkeypatch) -> None:
    # linked at c1 while the repository was still checked at c0: b.py's fetch_rows was
    # already beside it, so when a.py's goes it is another definition
    commits = {
        ("c0", "pkg/a.py"): _ROWS,
        ("c1", "pkg/a.py"): _ROWS,
        ("c1", "pkg/b.py"): _CLOSE_ROWS,
        ("c2", "pkg/a.py"): "",
        ("c2", "pkg/b.py"): _CLOSE_ROWS,
    }
    found = await _recheck_across(
        monkeypatch, commits, "fresh", "c0", renames={"pkg/a.py": "pkg/a.py"}, checked="c1"
    )
    assert found == [("missing", "pkg/a.py")]


@pytest.mark.asyncio
async def test_a_link_made_at_a_later_head_is_not_rechecked_back_to_this_one(monkeypatch) -> None:
    commits = {("c2", "pkg/a.py"): "", ("c3", "pkg/a.py"): _ROWS}
    assert await _recheck_across(monkeypatch, commits, "fresh", "c1", checked="c3") == []


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("story", "after"),
    [
        ("deleted", ("missing", "p.py")),
        ("renamed", ("changed", "q.py")),
        ("reverted", ("changed", "p.py")),
    ],
)
async def test_a_link_made_inside_the_range_follows_its_own_commit(
    monkeypatch, tmp_path, story, after
) -> None:
    git, commit = _repo(tmp_path)
    # the repository was checked at B; the memory was linked at C, which the index
    # reached first; B..H does not name p.py (or names only where it went)
    first = "def fetch_rows(n):\n    return n\n"
    edited = "def fetch_rows(n):\n    return n + 1\n"
    base = commit(
        {"keep.py": "def keep():\n    return 0\n"}
        | ({"p.py": first} if story == "reverted" else {})
    )
    made = commit({"p.py": edited})
    if story == "deleted":
        head = commit({"p.py": None})
    elif story == "renamed":
        commit({"p.py": edited.replace("n + 1", "n * 100")})
        git("mv", "p.py", "q.py")
        git("commit", "-q", "-m", "c")
        head = git("rev-parse", "HEAD")
    else:
        head = commit({"p.py": first})
    monkeypatch.setattr(
        code_links.code,
        "repo_for_namespace",
        lambda org, ns: SimpleNamespace(git_dir=str(tmp_path / ".git")),
    )
    linked = await code_links.code.parsed("o", "code-a", made, "p.py")
    (d,) = linked.defs
    row = _full(
        {
            "org": "o",
            "code_namespace": "code-a",
            "path": "p.py",
            "qualified_name": d.qualified_name,
            "body_hash": d.body_hash,
            "checked_hash": d.body_hash,
            "state": "fresh",
            "changed_since": "",
            "checked_commit": made,
            "memory_id": "m1",
            "version": 1,
        }
    )
    cursor: dict[str, str] = {}
    moves: list[Any] = []

    async def for_code(org, cns, *, paths=None, states=None, checked_commits=None, **kw):
        on_paths = paths is not None and row["path"] in paths
        return [row] if on_paths or made in (checked_commits or []) else []

    async def get(name):
        return cursor.get(name, "")

    async def put(name, value):
        cursor[name] = value

    async def none(*a, **k):
        return []

    async def write(found, at):
        moves.extend(found)

    async def current(org, found):
        return found

    _stub_reads(monkeypatch, for_code)
    monkeypatch.setattr(code_links.store, "lost", _none_lost)
    monkeypatch.setattr(code_links.code, "candidates", none)
    monkeypatch.setattr(code_links.code, "link_cursor", get)
    monkeypatch.setattr(code_links.code, "set_link_cursor", put)
    monkeypatch.setattr(code_link_recheck, "_write_moves", write)
    monkeypatch.setattr(code_link_recheck, "_current_moves", current)
    files = code_link_pass.RepoReads("o", "code-a")
    await code_link_recheck._follow("o", "code-a", base, head, files, code_link_pass.PassResult())
    assert [(m.link.state, m.link.path) for m in moves] == [after]


@pytest.mark.asyncio
async def test_a_pass_keeps_only_the_files_it_read_last(monkeypatch) -> None:
    reads: list[str] = []

    async def parsed(org, cns, commit, path, **kw):
        reads.append(path)
        return _file(path, "x = 1\n")

    monkeypatch.setattr(code_links.code, "parsed", parsed)
    monkeypatch.setattr(code_link_pass, "_FILES_CACHED", 2)
    files = code_link_pass.RepoReads("o", "code-a")
    for path in ("a.py", "b.py", "a.py", "c.py", "a.py", "b.py"):
        await files.at("c1", path)
    assert reads == ["a.py", "b.py", "c.py", "b.py"]  # a.py, read most, stays


@pytest.mark.asyncio
async def test_an_unresolved_link_follows_its_renamed_file_on_a_later_pass(monkeypatch) -> None:
    keep = "def keep():\n    return 0\n"
    moved = (
        _ROWS.replace("fetch_rows", "load_rows").replace("append(i)", "append(i * 2)")
        + "\n\n"
        + _ROWS.replace("fetch_rows", "load_more_rows").replace("append(i)", "append(i * 3)")
        + "\n\n"
        + keep
    )
    # c0 -> c1 renames a.py to b.py with two edited candidates in it: unresolved, and on
    # b.py now; c1 -> c2 edits only o.py
    commits = {
        ("c0", "pkg/a.py"): _ROWS + "\n\n" + keep,
        ("c1", "pkg/b.py"): moved,
        ("c2", "pkg/b.py"): moved,
        ("c2", "pkg/o.py"): "X = 1\n",
    }
    files = code_link_pass.RepoReads("o", "code-a")

    async def at(commit, path):
        src = commits.get((commit, path))
        return _file(path, src) if src is not None else None

    async def descends(ancestor, descendant):
        return ancestor <= descendant

    async def candidates(org, cns, *, names=None, qualified_names=None):
        return []

    _reads(monkeypatch, files, at)
    monkeypatch.setattr(files, "is_ancestor", descends)
    monkeypatch.setattr(code_links.code, "candidates", candidates)
    row = _full(
        {
            "org": "o",
            "code_namespace": "code-a",
            "path": "pkg/a.py",
            "qualified_name": "pkg.a::fetch_rows",
            "body_hash": "h",
            "checked_hash": "h",
            "state": "fresh",
            "changed_since": "",
            "checked_commit": "c0",
            "memory_id": "m1",
            "version": 1,
        }
    )
    renamed = code_link_pass.Range("c0", {"pkg/a.py": "pkg/b.py"})
    (move,) = await code_link_recheck._recheck("o", "code-a", [row], renamed, "c1", files)
    after = move.link
    assert (after.state, after.path, after.checked_path) == ("unresolved", "pkg/b.py", "pkg/a.py")
    # written as the pass writes it: on b.py, still read at a.py at c0
    row |= {"state": after.state, "path": after.path, "checked_path": after.checked_path}
    unrelated = code_link_pass.Range("c1", {"pkg/o.py": "pkg/o.py"})
    assert await code_link_recheck._recheck("o", "code-a", [row], unrelated, "c2", files) == []


@pytest.mark.asyncio
async def test_a_judged_change_that_only_shifted_lines_is_not_due_again(monkeypatch) -> None:
    from dataclasses import replace

    written: list[dict[str, Any]] = []

    async def revised(links):
        return {("o", "n")}

    async def insert(rows):
        written.extend(rows)

    monkeypatch.setattr(code_link_policy, "revised_namespaces", revised)
    monkeypatch.setattr(code_links.store, "insert", insert)
    judged = code_links.store.link_row(
        org="o",
        namespace="n",
        memory_id="m1",
        path="a.py",
        qualified_name="a::f",
        state="changed",
        checked_hash="h2",
        start_line=3,
        end_line=4,
        policy_due=0,
        suggestion="f now returns two",
        version=5,
    )
    shifted = replace(code_link_pass.link_of(judged), start_line=5, end_line=6)
    edited = replace(shifted, checked_hash="h3")
    await code_link_recheck._write_moves(
        [code_link_recheck.Move(judged, shifted), code_link_recheck.Move(judged, edited)], "c2"
    )
    assert [r["policy_due"] for r in written] == [0, 1]


@pytest.mark.asyncio
async def test_a_pending_name_resolves_by_the_files_its_memory_names(monkeypatch) -> None:
    from server.code.domain.anchor import Definition

    reader = "class Reader:\n    def get(self):\n        return 1\n"
    found = [
        Definition("src/reader.py", "src.reader::Reader::get", "get"),
        Definition("tests/fake.py", "tests.fake::Reader::get", "get"),
    ]

    async def rows(org, cns, *, days):
        return [
            _full({"qualified_name": "Reader.get", "expired": 0, "version": 1, "memory_id": "m1"})
        ]

    async def heads(org, ids):
        return [{"mid": "m1", "files": ["src/reader.py"]}]

    async def candidates(org, cns, *, names):
        return found

    files = code_link_pass.RepoReads("o", "code-a")

    async def at(commit, path):
        return _file(path, reader)

    monkeypatch.setattr(code_links.store, "pending", rows)
    monkeypatch.setattr(code_links.store, "memory_heads", heads)
    monkeypatch.setattr(code_links.code, "candidates", candidates)
    _reads(monkeypatch, files, at)
    moves = await code_link_recheck._resolve_pending(
        "o", "code-a", "c2", files, code_link_pass.PassResult()
    )
    assert [m.link.path for m in moves] == ["src/reader.py"]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("repositories", "spends", "granted"),
    [
        (2, [20, 20], [10, 10]),  # each a share, a backlog taking all of its own
        (3, [0, 20, 20], [7, 10, 10]),  # what an idle one leaves goes to the next
        (30, [1] * 30, [1] * 20 + [0] * 10),  # never past the cap
    ],
)
async def test_each_repository_gets_its_share_of_the_judgements(
    monkeypatch, repositories, spends, granted
) -> None:
    seen: list[int] = []

    async def namespaces():
        return [("o", f"code-{i}") for i in range(repositories)]

    async def recheck(org, cns, budget, result):
        seen.append(budget.judgements)
        budget.judgements -= min(budget.judgements, spends[len(seen) - 1])

    monkeypatch.setattr(settings, "memory_code_links_revise_per_pass", 20)
    monkeypatch.setattr(code_links.store, "code_namespaces", namespaces)
    monkeypatch.setattr(code_link_recheck, "repo_for_namespace", lambda org, cns: object())
    monkeypatch.setattr(code_link_recheck, "_recheck_repository", recheck)
    monkeypatch.setattr(code_link_recheck, "_passes", iter([0]))
    await code_link_recheck.recheck_all(code_link_pass.PassResult())
    assert seen == granted and sum(min(g, s) for g, s in zip(seen, spends, strict=True)) <= 20


@pytest.mark.asyncio
async def test_each_pass_starts_the_judgements_one_repository_further(monkeypatch) -> None:
    order: list[str] = []

    async def namespaces():
        return [("o", "code-a"), ("o", "code-b"), ("o", "code-c")]

    async def recheck(org, cns, budget, result):
        order.append(cns)

    monkeypatch.setattr(code_links.store, "code_namespaces", namespaces)
    monkeypatch.setattr(code_link_recheck, "repo_for_namespace", lambda org, cns: object())
    monkeypatch.setattr(code_link_recheck, "_recheck_repository", recheck)
    monkeypatch.setattr(code_link_recheck, "_passes", iter([0, 1]))
    await code_link_recheck.recheck_all(code_link_pass.PassResult())
    await code_link_recheck.recheck_all(code_link_pass.PassResult())
    assert order == ["code-a", "code-b", "code-c", "code-b", "code-c", "code-a"]


@pytest.mark.asyncio
@pytest.mark.parametrize("followed", [False, True], ids=["in-place", "file-renamed-first"])
async def test_a_lost_link_whose_file_moved_is_found_in_another_module(
    monkeypatch, followed
) -> None:
    from server.code.domain.anchor import Definition

    src = "def fetch_rows():\n    return 1\n"
    (d,) = _file("src/w.py", src).defs
    files = code_link_pass.RepoReads("o", "code-a")

    async def at(commit, path):
        if path == "src/w.py":  # added at c2, with the text the link was read with
            return _file(path, src) if commit == "c2" else None
        if path == "src/u.py" and commit == "c1":
            return _file(path, src)  # where it was before it went
        return _file(path, "X = 1\n")

    async def renamed_from(base, head, paths):
        return {}

    async def touched(base, head):
        return {"src/w.py"}

    async def edits_of(base, head, path, name):
        return [("c2", "c1")]

    async def candidates(org, cns, *, names=None, qualified_names=None):
        return [Definition("src/w.py", d.qualified_name, "fetch_rows")]

    _reads(monkeypatch, files, at)
    monkeypatch.setattr(files, "renamed_from", renamed_from)
    monkeypatch.setattr(files, "touched", touched)
    monkeypatch.setattr(files, "edits_of", edits_of)
    monkeypatch.setattr(code_links.code, "candidates", candidates)
    row = _full(
        {
            "org": "o",
            "code_namespace": "code-a",
            "path": "src/v.py" if followed else "src/u.py",
            "checked_path": "src/u.py" if followed else "",
            "qualified_name": "src.u::fetch_rows",
            "body_hash": d.body_hash,
            "checked_hash": d.body_hash,
            "state": "missing",
            "changed_since": "c1",
            "checked_commit": "c1",
            "memory_id": "m1",
            "version": 1,
        }
    )
    moves = await code_link_recheck._recheck("o", "code-a", [row], None, "c2", files)
    assert [(m.link.state, m.link.path) for m in moves] == [("moved", "src/w.py")]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("ours", "theirs", "src", "deleted"),
    [
        ("pkg/a.py", "pkg/b.py", "def fetch_rows():\n    return 1\n", False),
        ("pkg/a.py", "pkg/b.py", "def fetch_rows():\n    return 1\n", True),
        ("src/A.cpp", "src/B.cpp", "static int roundUp(int n) { return n + 1; }\n", True),
    ],
    ids=["py-removed", "py-file-deleted", "cpp-file-deleted"],
)
async def test_an_identical_copy_there_all_along_never_takes_a_removed_definition(
    monkeypatch, ours, theirs, src, deleted
) -> None:
    from server.code.domain.anchor import Definition

    keep = "int keep() { return 0; }\n" if ours.endswith(".cpp") else "def keep():\n    return 0\n"
    commits = {("c1", ours): src + "\n" + keep, ("c1", theirs): src, ("c2", theirs): src}
    if not deleted:
        commits[("c2", ours)] = keep
    (mine,) = [d for d in _file(ours, src).defs]
    (copy,) = _file(theirs, src).defs
    files = code_link_pass.RepoReads("o", "code-a")

    async def at(commit, path):
        text = commits.get((commit, path))
        return _file(path, text) if text is not None else None

    async def candidates(org, cns, *, names=None, qualified_names=None):
        return [Definition(theirs, copy.qualified_name, copy.name)]

    _reads(monkeypatch, files, at)
    _history(monkeypatch, files, commits)
    monkeypatch.setattr(code_links.code, "candidates", candidates)
    row = _full(
        {
            "org": "o",
            "code_namespace": "code-a",
            "path": ours,
            "qualified_name": mine.qualified_name,
            "body_hash": mine.body_hash,
            "checked_hash": mine.body_hash,
            "state": "fresh",
            "changed_since": "",
            "checked_commit": "c1",
            "memory_id": "m1",
            "version": 1,
        }
    )
    changed = code_link_pass.Range("c1", {ours: "" if deleted else ours})
    moves = await code_link_recheck._recheck("o", "code-a", [row], changed, "c2", files)
    assert [(m.link.state, m.link.path) for m in moves] == [("missing", ours)]


def _common_name_world(monkeypatch, files, changed_paths, moved_text=None, touch_all=False):
    """A link to tests/a/test.py::started_cluster, a name 33 other files define with
    their own text, over the range c1 -> c2 that changed `changed_paths` (and every one
    of those files with `touch_all`); `moved_text` adds tests/b/test.py with it at c2."""
    from server.code.domain.anchor import Definition

    others = [f"tests/t{i}/test.py" for i in range(code_link_recheck._MAX_ELSEWHERE + 1)]
    mine = "def started_cluster():\n    return 1\n"
    texts = {p: "def started_cluster():\n    return 2\n" for p in others}
    if moved_text is not None:
        texts["tests/b/test.py"] = moved_text
    read: list[str] = []

    async def at(commit, path):
        read.append(path)
        if path == "tests/a/test.py":
            return _file(path, mine + "\n\ndef keep():\n    return 0\n") if commit == "c1" else None
        if path == "tests/b/test.py" and commit == "c1":
            return None
        return _file(path, texts[path]) if path in texts else None

    async def candidates(org, cns, *, names=None, qualified_names=None):
        return [
            Definition(p, p[:-3].replace("/", ".") + "::started_cluster", "started_cluster")
            for p in texts
        ]

    if touch_all:
        changed_paths = changed_paths | {p: p for p in others}

    async def touched(base, head):
        return {p for p in changed_paths.values() if p}

    async def edits_of(base, head, path, name):
        return [("c2", "c1")]

    async def renamed_from(base, head, paths):
        return {}

    _reads(monkeypatch, files, at)
    monkeypatch.setattr(files, "touched", touched)
    monkeypatch.setattr(files, "edits_of", edits_of)
    monkeypatch.setattr(files, "renamed_from", renamed_from)
    monkeypatch.setattr(code_links.code, "candidates", candidates)
    (d,) = _file("tests/a/test.py", mine).defs
    row = _full(
        {
            "org": "o",
            "code_namespace": "code-a",
            "path": "tests/a/test.py",
            "qualified_name": d.qualified_name,
            "body_hash": d.body_hash,
            "checked_hash": d.body_hash,
            "state": "fresh",
            "changed_since": "",
            "checked_commit": "c1",
            "memory_id": "m1",
            "version": 1,
        }
    )
    return row, code_link_pass.Range("c1", changed_paths), others, read


@pytest.mark.asyncio
@pytest.mark.parametrize("moved", [False, True], ids=["file-deleted", "moved-unchanged"])
async def test_a_name_many_untouched_files_define_is_followed_as_any_other(
    monkeypatch, moved
) -> None:
    files = code_link_pass.RepoReads("o", "code-a")
    changed = {"tests/a/test.py": ""} | ({"tests/b/test.py": "tests/b/test.py"} if moved else {})
    text = "def started_cluster():\n    return 1\n" if moved else None
    row, rng, others, read = _common_name_world(monkeypatch, files, changed, text)
    moves = await code_link_recheck._recheck("o", "code-a", [row], rng, "c2", files)
    want = ("moved", "tests/b/test.py") if moved else ("missing", "tests/a/test.py")
    assert [(m.link.state, m.link.path) for m in moves] == [want]
    assert not set(read) & set(others)  # files the range did not touch are never read


@pytest.mark.asyncio
async def test_a_name_too_many_touched_files_define_is_never_weighed(monkeypatch) -> None:
    files = code_link_pass.RepoReads("o", "code-a")
    row, rng, others, read = _common_name_world(
        monkeypatch,
        files,
        {"tests/a/test.py": ""},
        touch_all=True,  # all of them edited
    )
    moves = await code_link_recheck._recheck("o", "code-a", [row], rng, "c2", files)
    assert [m.link.state for m in moves] == ["unresolved"]
    assert not set(read) & set(others)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "changed",
    [
        code_link_pass.Range("c0", {"pkg/a.py": "pkg/a.py", "pkg/b.py": "pkg/b.py"}),  # one pass
        code_link_pass.Range("c1", {"pkg/a.py": "pkg/a.py"}),  # a pass ran between the commits
    ],
    ids=["one-pass", "two-passes"],
)
async def test_the_state_a_history_ends_in_does_not_depend_on_when_passes_ran(
    monkeypatch, changed
) -> None:
    from server.code.domain.anchor import Definition

    # c1 copies fetch_rows into b.py; c2 deletes it from a.py: the copy was there when the
    # original went, so it is another definition however the passes fell
    keep = "def keep():\n    return 0\n"
    commits = {
        ("c0", "pkg/a.py"): _ROWS + "\n\n" + keep,
        ("c1", "pkg/a.py"): _ROWS + "\n\n" + keep,
        ("c1", "pkg/b.py"): _ROWS,
        ("c2", "pkg/a.py"): keep,
        ("c2", "pkg/b.py"): _ROWS,
    }
    files = code_link_pass.RepoReads("o", "code-a")

    async def at(commit, path):
        src = commits.get((commit, path))
        return _file(path, src) if src is not None else None

    async def candidates(org, cns, *, names=None, qualified_names=None):
        return [Definition("pkg/b.py", "pkg.b::fetch_rows", "fetch_rows")]

    _reads(monkeypatch, files, at)
    _history(monkeypatch, files, commits)
    monkeypatch.setattr(code_links.code, "candidates", candidates)
    (d,) = [x for x in _file("pkg/a.py", _ROWS).defs if x.name == "fetch_rows"]
    row = _full(
        {
            "org": "o",
            "code_namespace": "code-a",
            "path": "pkg/a.py",
            "qualified_name": d.qualified_name,
            "body_hash": d.body_hash,
            "checked_hash": d.body_hash,
            "state": "fresh",
            "changed_since": "",
            "checked_commit": "c0",
            "memory_id": "m1",
            "version": 1,
        }
    )
    moves = await code_link_recheck._recheck("o", "code-a", [row], changed, "c2", files)
    assert [(m.link.state, m.link.path) for m in moves] == [("missing", "pkg/a.py")]


@pytest.mark.asyncio
async def test_the_commits_that_edited_a_name_in_a_file_are_read_from_its_history(tmp_path) -> None:
    from server.code.app.index.gitrepo import edits_of

    _, commit = _repo(tmp_path)
    rows = "def fetch_rows():\n    return 1\n\n"
    base = commit({"a.py": rows + "def keep():\n    return 0\n"})
    commit({"a.py": rows + "def keep():\n    return 5\n"})  # an edit elsewhere in the file
    other = commit({"b.py": "b = 1\n"})
    gone = commit({"a.py": "def keep():\n    return 5\n"})
    gd = str(tmp_path / ".git")
    assert await edits_of(gd, base, gone, "a.py", edit_pattern("fetch_rows", "a.py")) == [
        (gone, other)
    ]
    assert len(await edits_of(gd, base, gone, "a.py", None)) == 2  # every edit


@pytest.mark.asyncio
@pytest.mark.parametrize("base", ["c0", "c1", "c2"], ids=["one-pass", "pass-at-c1", "pass-at-c2"])
async def test_an_earlier_edit_to_the_file_does_not_move_where_its_code_was_last(
    monkeypatch, base
) -> None:
    from server.code.domain.anchor import Definition

    # c1 edits keep in a.py, c2 copies fetch_rows into b.py, c3 deletes it from a.py: the
    # copy was there when the original went, however the passes fell
    keep, kept = "def keep():\n    return 0\n", "def keep():\n    return 1\n"
    commits = {
        ("c0", "pkg/a.py"): _ROWS + "\n\n" + keep,
        ("c1", "pkg/a.py"): _ROWS + "\n\n" + kept,
        ("c2", "pkg/a.py"): _ROWS + "\n\n" + kept,
        ("c2", "pkg/b.py"): _ROWS,
        ("c3", "pkg/a.py"): kept,
        ("c3", "pkg/b.py"): _ROWS,
    }
    files = code_link_pass.RepoReads("o", "code-a")

    async def at(commit, path):
        src = commits.get((commit, path))
        return _file(path, src) if src is not None else None

    async def candidates(org, cns, *, names=None, qualified_names=None):
        return [Definition("pkg/b.py", "pkg.b::fetch_rows", "fetch_rows")]

    _reads(monkeypatch, files, at)
    _history(monkeypatch, files, commits)
    monkeypatch.setattr(code_links.code, "candidates", candidates)
    (d,) = [x for x in _file("pkg/a.py", _ROWS).defs if x.name == "fetch_rows"]
    row = _full(
        {
            "org": "o",
            "code_namespace": "code-a",
            "path": "pkg/a.py",
            "qualified_name": d.qualified_name,
            "body_hash": d.body_hash,
            "checked_hash": d.body_hash,
            "state": "fresh",
            "changed_since": "",
            "checked_commit": base,  # a pass that reached it rechecked the link there
            "memory_id": "m1",
            "version": 1,
        }
    )
    paths = {"pkg/a.py": "pkg/a.py"} | ({"pkg/b.py": "pkg/b.py"} if base < "c2" else {})
    moves = await code_link_recheck._recheck(
        "o", "code-a", [row], code_link_pass.Range(base, paths), "c3", files
    )
    assert [(m.link.state, m.link.path) for m in moves] == [("missing", "pkg/a.py")]


@pytest.mark.asyncio
@pytest.mark.parametrize("separate", [False, True], ids=["one-pass", "separate-passes"])
async def test_where_its_code_was_last_follows_its_file_through_a_rename(
    monkeypatch, separate
) -> None:
    from server.code.domain.anchor import Definition

    # c1 renames a.py to c.py, c2 copies fetch_rows into b.py, c3 deletes it from c.py
    keep = "def keep():\n    return 0\n"
    commits = {
        ("c0", "pkg/a.py"): _ROWS + "\n\n" + keep,
        ("c1", "pkg/c.py"): _ROWS + "\n\n" + keep,
        ("c2", "pkg/c.py"): _ROWS + "\n\n" + keep,
        ("c2", "pkg/b.py"): _ROWS,
        ("c3", "pkg/c.py"): keep,
        ("c3", "pkg/b.py"): _ROWS,
    }
    files = code_link_pass.RepoReads("o", "code-a")

    async def at(commit, path):
        src = commits.get((commit, path))
        return _file(path, src) if src is not None else None

    async def range_of(base, head):
        renames = {"pkg/a.py": "pkg/c.py"} if (base, head) == ("c0", "c1") else {}
        return code_link_pass.Range(base, renames)

    async def candidates(org, cns, *, names=None, qualified_names=None):
        return [Definition("pkg/b.py", "pkg.b::fetch_rows", "fetch_rows")]

    _reads(monkeypatch, files, at)
    _history(monkeypatch, files, commits)
    monkeypatch.setattr(files, "range", range_of)
    monkeypatch.setattr(code_links.code, "candidates", candidates)
    (d,) = [x for x in _file("pkg/a.py", _ROWS).defs if x.name == "fetch_rows"]
    # separate passes: the link followed the rename to c.py and was checked at c2
    path, qualified_name, checked = (
        ("pkg/c.py", "pkg.c::fetch_rows", "c2")
        if separate
        else ("pkg/a.py", d.qualified_name, "c0")
    )
    row = _full(
        {
            "org": "o",
            "code_namespace": "code-a",
            "path": path,
            "qualified_name": qualified_name,
            "body_hash": d.body_hash,
            "checked_hash": d.body_hash,
            "state": "fresh",
            "changed_since": "",
            "checked_commit": checked,
            "memory_id": "m1",
            "version": 1,
        }
    )
    paths = (
        {"pkg/c.py": "pkg/c.py"} if separate else {"pkg/a.py": "pkg/c.py", "pkg/b.py": "pkg/b.py"}
    )
    moves = await code_link_recheck._recheck(
        "o", "code-a", [row], code_link_pass.Range(checked, paths), "c3", files
    )
    assert [m.link.state for m in moves] == ["missing"]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("old", "new", "before", "after"),
    [
        (
            "pkg/a.py",
            "pkg/b.py",
            "class Foo:\n    def fetch_rows(self):\n        return 1\n",
            "class LegacyFoo:\n    def fetch_rows(self):\n        return 1\n",
        ),
        (
            "src/a.cpp",
            "src/b.cpp",
            "namespace io {\nint fetch_rows() { return 1; }\n}\n",
            "namespace legacy {\nint fetch_rows() { return 1; }\n}\n",
        ),
        (
            "pkg/a.py",
            "pkg/b.py",
            "def outer():\n    def fetch_rows():\n        return 1\n    return fetch_rows\n",
            "def legacy_outer():\n    def fetch_rows():\n        return 1\n    return fetch_rows\n",
        ),
        (
            "src/a.rs",
            "src/b.rs",
            "mod io {\n    pub fn fetch_rows() -> u32 { 1 }\n}\n",
            "mod legacy {\n    pub fn fetch_rows() -> u32 { 1 }\n}\n",
        ),
    ],
    ids=["py-class", "cpp-namespace", "py-nested", "rs-mod"],
)
async def test_code_whose_scope_was_renamed_away_as_it_moved_is_followed(
    monkeypatch, tmp_path, old, new, before, after
) -> None:
    # the commit renames only the scope line in the old file (no line with the method's
    # name changes there) and adds the method under its old scope in another file
    _, commit = _repo(tmp_path)
    base = commit({old: before})
    head = commit({old: after, new: before})
    monkeypatch.setattr(
        code_links.code,
        "repo_for_namespace",
        lambda org, ns: SimpleNamespace(git_dir=str(tmp_path / ".git")),
    )
    (d,) = [
        x
        for x in (await code_links.code.parsed("o", "c", base, old)).defs
        if x.name == "fetch_rows"
    ]
    (theirs,) = [
        x
        for x in (await code_links.code.parsed("o", "c", head, new)).defs
        if x.name == "fetch_rows"
    ]

    async def candidates(org, cns, *, names=None, qualified_names=None):
        return [theirs]

    monkeypatch.setattr(code_links.code, "candidates", candidates)
    row = _full(
        {
            "org": "o",
            "code_namespace": "c",
            "path": old,
            "qualified_name": d.qualified_name,
            "body_hash": d.body_hash,
            "checked_hash": d.body_hash,
            "state": "fresh",
            "changed_since": "",
            "checked_commit": base,
            "memory_id": "m1",
            "version": 1,
        }
    )
    files = code_link_pass.RepoReads("o", "c")
    changed = await files.range(base, head)
    moves = await code_link_recheck._recheck("o", "c", [row], changed, head, files)
    assert [(m.link.state, m.link.path) for m in moves] == [("moved", new)]


@pytest.mark.asyncio
async def test_code_gone_in_a_way_the_edit_filter_missed_is_unsure(monkeypatch) -> None:
    from server.code.domain.anchor import Definition

    commits = {
        ("c0", "pkg/a.py"): _ROWS,
        ("c1", "pkg/a.py"): "# nothing defined here now\n",
        ("c1", "pkg/b.py"): _ROWS,
    }
    files = code_link_pass.RepoReads("o", "code-a")

    async def at(commit, path):
        src = commits.get((commit, path))
        return _file(path, src) if src is not None else None

    async def no_edits(base, head, path, name):
        return []  # a scope declared with a word the filter does not know

    async def candidates(org, cns, *, names=None, qualified_names=None):
        return [Definition("pkg/b.py", "pkg.b::fetch_rows", "fetch_rows")]

    _reads(monkeypatch, files, at)
    _history(monkeypatch, files, commits)
    monkeypatch.setattr(files, "edits_of", no_edits)
    monkeypatch.setattr(code_links.code, "candidates", candidates)
    (d,) = _file("pkg/a.py", _ROWS).defs
    row = _full(
        {
            "org": "o",
            "code_namespace": "code-a",
            "path": "pkg/a.py",
            "qualified_name": d.qualified_name,
            "body_hash": d.body_hash,
            "checked_hash": d.body_hash,
            "state": "fresh",
            "changed_since": "",
            "checked_commit": "c0",
            "memory_id": "m1",
            "version": 1,
        }
    )
    changed = code_link_pass.Range("c0", {"pkg/a.py": "pkg/a.py", "pkg/b.py": "pkg/b.py"})
    moves = await code_link_recheck._recheck("o", "code-a", [row], changed, "c1", files)
    assert [m.link.state for m in moves] == ["unresolved"]


@pytest.mark.parametrize(
    ("path", "line", "matches"),
    [
        ("src/a.cpp", "void f(const Reader & r)", False),  # a type qualifier, not a binding
        ("src/a.cpp", "class DLL_EXPORT Reader : public Base", True),
        ("src/a.rs", "let r = Reader::new(path);", False),
        ("src/a.rs", "mod Reader {", True),
        ("src/a.ts", "const r = new Reader(src);", False),
        ("src/a.ts", "const Reader = class {", True),
        ("pkg/a.py", "def Reader():", True),
        ("pkg/a.py", "x = read()", True),  # its own part, anywhere
        ("pkg/a.py", "class Foo(Reader):", False),  # a first Python base class
        ("pkg/a.py", "class Foo(Base, Reader):", True),  # a later one over-matches
        ("src/a.cpp", "struct alignas(64) Reader {", True),
        ("src/a.cpp", "class alignas(alignof(int)) Reader {", True),
        ("src/a.cpp", "struct __attribute__((packed)) Reader {", True),
        ("src/a.cpp", "struct __attribute__((aligned(8))) Reader {", True),
        ("src/a.rs", "impl{Reader", False),
    ],
)
def test_a_scope_counts_only_where_it_is_declared(tmp_path, path, line, matches) -> None:
    pattern = edit_pattern("Reader.read", path)
    assert pattern is not None
    git = ["git", "-C", str(tmp_path), "-c", "user.name=t", "-c", "user.email=t@t"]
    subprocess.run([*git, "init", "-q"], check=True)
    (tmp_path / "f").write_text(line + "\n")
    subprocess.run([*git, "add", "f"], check=True)
    subprocess.run([*git, "commit", "-qm", "c"], check=True)
    found = subprocess.run(
        [*git, "log", "-G", pattern, "--format=%H"], check=True, capture_output=True
    )
    assert bool(found.stdout.strip()) is matches  # as git's own regcomp reads it


def test_a_name_beyond_ascii_reads_every_edit_of_its_file() -> None:
    assert edit_pattern("café", "pkg/a.py") is None  # git's word boundaries follow its locale


@pytest.mark.asyncio
async def test_a_remembered_anchor_counts_only_in_the_history_it_came_from(monkeypatch) -> None:
    from server.code.domain.anchor import Link

    def files_over(commits, history):
        files = code_link_pass.RepoReads("o", "code-a")

        async def at(commit, path):
            src = commits.get((commit, path))
            return _file(path, src) if src is not None else None

        async def descends(ancestor, descendant):
            return ancestor in history

        _reads(monkeypatch, files, at)
        _history(monkeypatch, files, commits)
        monkeypatch.setattr(files, "is_ancestor", descends)
        return files

    link = Link("pkg/a.py", "pkg.a::fetch_rows")
    kept = _ROWS.replace("append(i)", "append(i + 1)")
    gone_at_c2 = {("c0", "pkg/a.py"): _ROWS, ("c1", "pkg/a.py"): _ROWS, ("c2", "pkg/a.py"): ""}
    first = files_over(gone_at_c2, {"c0", "c1", "c2"})
    assert await code_link_recheck._last_in_place(first, link, "c0", "c2", "fetch_rows") == "c1"

    # the same history again: the remembered answer, without reading git
    async def unread(*args):
        raise AssertionError("read again")

    again = files_over(gone_at_c2, {"c0", "c1", "c2"})
    monkeypatch.setattr(again, "edits_of", unread)
    assert await code_link_recheck._last_in_place(again, link, "c0", "c2", "fetch_rows") == "c1"
    # a force-push kept c1 but replaced c2: c3 edits the code, c4 removes it
    rewritten = {
        ("c0", "pkg/a.py"): _ROWS,
        ("c1", "pkg/a.py"): _ROWS,
        ("c3", "pkg/a.py"): kept,
        ("c4", "pkg/a.py"): "",
    }
    second = files_over(rewritten, {"c0", "c1", "c3", "c4"})
    assert await code_link_recheck._last_in_place(second, link, "c0", "c4", "fetch_rows") == "c3"


@pytest.mark.asyncio
async def test_where_its_code_was_last_is_unknown_when_it_went_unseen(monkeypatch) -> None:
    from server.code.domain.anchor import Link

    # c1 wraps it in a string (no line with its name changes), c2 mentions it in a comment
    code = "def fetch_rows():\n    return 1\n"
    commits = {
        ("c0", "pkg/a.py"): code,
        ("c1", "pkg/a.py"): '_ = """\n' + code + '"""\n',
        ("c2", "pkg/a.py"): '_ = """\n' + code + '"""\n# fetch_rows is gone\n',
    }
    files = code_link_pass.RepoReads("o", "code-a")

    async def at(commit, path):
        src = commits.get((commit, path))
        return _file(path, src) if src is not None else None

    _reads(monkeypatch, files, at)
    _history(monkeypatch, files, commits)
    link = Link("pkg/a.py", "pkg.a::fetch_rows")
    assert await code_link_recheck._last_in_place(files, link, "c0", "c2", "fetch_rows") is None


def test_the_edit_pattern_is_plain_posix() -> None:
    # git log -G compiles it with the platform's regcomp: no \b, \s or \w there
    for name, path in [
        ("fetch_rows", "pkg/a.py"),
        ("Reader.read", "src/a.rs"),
        ("io.read", "a.cpp"),
    ]:
        pattern = edit_pattern(name, path)
        assert pattern is not None and not re.search(r"\\[bswBSW]", pattern)


def test_a_carried_link_serves_no_suggestion_its_hashes_still_match(monkeypatch) -> None:
    monkeypatch.setattr(code_link_reads, "inputs_now", lambda org, memory: "i")
    monkeypatch.setattr(code_link_reads.code_change, "held", lambda row: False)
    link = _full({"inputs_hash": "i", "suggestion": "returns three", "judged_hash": "h"})
    link["checked_hash"] = "h"
    served = code_link_reads.current("o", {"mid": "m"}, [link, {**link, "carried": 1}])
    assert [a["suggestion"] for a in served] == ["returns three", ""]


@pytest.mark.asyncio
async def test_the_revise_judge_runs_on_its_own_model(monkeypatch) -> None:
    from server.llm import client as llm_client
    from server.memory.app import code_link_judge

    picked: list[str | None] = []

    class _LLM:
        async def complete(self, *a: Any, **kw: Any) -> Any:
            return SimpleNamespace(text='{"verdict": "holds"}')

    def for_model(model: str | None) -> _LLM:
        picked.append(model)
        return _LLM()

    monkeypatch.setattr(llm_client, "get_llm_client_for_model", for_model)
    monkeypatch.setattr(settings, "code_link_revise_model", "fake-revise-1")
    assert await code_link_judge.judge_claim("returns rows", "a", "b") == ("holds", "")
    assert picked == ["fake-revise-1"]


@pytest.mark.asyncio
async def test_a_memory_that_links_nothing_is_linked_once(monkeypatch) -> None:
    head = {
        "org": "o",
        "namespace": "agent-a",
        "agent": "a",
        "user_id": "-",
        "session_id": "-",
        "memory_key": "k",
        "mid": "m",
        "sha": "s",
        "value": "raise the retry_budget before a deploy",  # a word of the prose only
        "tags": ["repo:r"],
        "files": [],
        "status": "active",
        "tombstoned_at": None,
    }
    calls: list[str] = []

    async def record(name, out):
        calls.append(name)
        return out

    monkeypatch.setattr(code_links.store, "for_memories", lambda org, ids: record("links", []))
    monkeypatch.setattr(code_links.store, "memory_heads", lambda org, ids: record("heads", [head]))
    monkeypatch.setattr(code_links.store, "insert", lambda rows: record("insert", None))
    monkeypatch.setattr(code_links.code, "indexed_head", lambda org, cns: record("head", "h"))
    monkeypatch.setattr(code_links.code, "candidates", lambda *a, **k: record("candidates", []))
    for module in (code_links, code_link_reads):
        monkeypatch.setattr(module, "code_namespace_for", lambda org, ns, tags: "code-r")
    await code_links._link_org("o", [head], code_link_pass.PassResult())
    assert calls == ["links", "head", "insert", "heads"]  # one read back, nothing relinked


@pytest.mark.asyncio
async def test_the_policy_drops_the_links_of_a_memory_that_is_gone(world) -> None:
    world.rows["deleted"] = _row("deleted", tombstoned_at="2026-01-01")
    world.states.update(erased=["changed"], deleted=["changed"])  # "erased" has no head
    world.stale.add("erased")
    await _apply()
    assert {(r["memory_id"], r["tombstoned_at"] is not None) for r in world.cleared} == {
        ("erased", True),
        ("deleted", True),
    }


@pytest.mark.asyncio
async def test_a_stalled_queue_is_backfilled_once_while_it_catches_up(monkeypatch) -> None:
    from server.memory.workers import code_links as worker

    cursors = {"queue": json.dumps([100, 0, "", "", "", "", "", ""]), "repos": '[["o", "c"]]'}
    horizon = [10_000]  # the queue's cursor is behind its TTL

    async def link_cursor(name):
        return cursors.get(name, "")

    async def set_link_cursor(name, value):
        cursors[name] = value

    async def queue_horizon():
        return horizon[0]

    async def link_queued(position, batch, result):
        return (int(position[0]) + 1, *position[1:])  # catching up, slowly

    scans: list[Any] = []

    async def backfill(after, limit, result):
        scans.append(after)
        return None  # one page: done

    async def sweep_erased(cursor, limit, result):
        return cursor

    monkeypatch.setattr(code_links.code, "link_cursor", link_cursor)
    monkeypatch.setattr(code_links.code, "set_link_cursor", set_link_cursor)
    monkeypatch.setattr(code_links.code, "configured_code_namespaces", lambda: [["o", "c"]])
    monkeypatch.setattr(code_links.store, "queue_horizon", queue_horizon)
    for fn in (link_queued, backfill, sweep_erased):
        monkeypatch.setattr(code_links, fn.__name__, fn)
    for _ in range(5):
        await worker._link_writes(10, code_link_pass.PassResult())
    assert scans == [None]
    horizon[0] = 0  # caught up: the next stall scans again
    await worker._link_writes(10, code_link_pass.PassResult())
    assert json.loads(cursors["backfill"]) == {"done": True}
    horizon[0] = 10_000
    await worker._link_writes(10, code_link_pass.PassResult())
    assert scans == [None, None]


@pytest.mark.asyncio
async def test_an_unread_registry_links_nothing_but_still_sweeps_erased_memories(
    monkeypatch,
) -> None:
    from server.code.domain.index import repos
    from server.memory.workers import code_links as worker

    cursors = {"queue": json.dumps([100, 0, "", "", "", "", "", ""]), "repos": '[["o", "c"]]'}
    calls: list[str] = []

    async def link_cursor(name):
        return cursors.get(name, "")

    async def set_link_cursor(name, value):
        cursors[name] = value

    async def queue_horizon():
        return 0

    async def link_queued(position, batch, result):
        calls.append("link")
        return position

    async def sweep_erased(cursor, limit, result):
        calls.append("sweep")
        return cursor

    def unread() -> list[list[str]]:
        raise repos.RegistryUnread("not read")

    monkeypatch.setattr(code_links.code, "link_cursor", link_cursor)
    monkeypatch.setattr(code_links.code, "set_link_cursor", set_link_cursor)
    monkeypatch.setattr(code_links.code, "configured_code_namespaces", unread)
    monkeypatch.setattr(code_links.store, "queue_horizon", queue_horizon)
    monkeypatch.setattr(code_links, "link_queued", link_queued)
    monkeypatch.setattr(code_links, "sweep_erased", sweep_erased)
    await worker._link_writes(10, code_link_pass.PassResult())
    assert calls == ["sweep"] and cursors["repos"] == '[["o", "c"]]'


@pytest.mark.asyncio
async def test_a_linking_failure_still_rechecks_every_repository(monkeypatch) -> None:
    import contextlib

    from server.memory.workers import code_links as worker

    async def refresh_repos():
        return None

    @contextlib.asynccontextmanager
    async def queue_lease():
        yield

    async def link_writes(batch, result):
        raise TimeoutError("backfill page timed out")

    rechecked: list[Any] = []

    async def recheck_all(result):
        rechecked.append(result)

    monkeypatch.setattr(code_links.code, "refresh_repos", refresh_repos)
    monkeypatch.setattr(code_links, "queue_lease", queue_lease)
    monkeypatch.setattr(worker, "_link_writes", link_writes)
    monkeypatch.setattr(code_link_recheck, "recheck_all", recheck_all)
    assert (await worker.run_pass())["errors"] == 1
    assert len(rechecked) == 1 and "TimeoutError" in rechecked[0].errors[0]


@pytest.mark.asyncio
async def test_an_empty_registry_snapshot_neither_links_nor_restarts_the_backfill(
    monkeypatch,
) -> None:
    from server.memory.workers import code_links as worker

    cursors = {
        "queue": json.dumps([100, 0, "", "", "", "", "", ""]),
        "repos": '[["o", "c"]]',
        "backfill": '{"done": true}',
    }
    configured: list[list[list[str]]] = [[], [["o", "c"]]]

    async def link_cursor(name):
        return cursors.get(name, "")

    async def set_link_cursor(name, value):
        cursors[name] = value

    async def queue_horizon():
        return 0

    async def link_queued(position, batch, result):
        return position

    scans: list[Any] = []

    async def backfill(after, limit, result):
        scans.append(after)
        return None

    async def sweep_erased(cursor, limit, result):
        return cursor

    monkeypatch.setattr(code_links.code, "link_cursor", link_cursor)
    monkeypatch.setattr(code_links.code, "set_link_cursor", set_link_cursor)
    monkeypatch.setattr(code_links.code, "configured_code_namespaces", lambda: configured.pop(0))
    monkeypatch.setattr(code_links.store, "queue_horizon", queue_horizon)
    for fn in (link_queued, backfill, sweep_erased):
        monkeypatch.setattr(code_links, fn.__name__, fn)
    await worker._link_writes(10, code_link_pass.PassResult())  # the registry read failed
    assert json.loads(cursors["repos"]) == [["o", "c"]]
    await worker._link_writes(10, code_link_pass.PassResult())  # and is back
    assert scans == [] and json.loads(cursors["backfill"]) == {"done": True}


def test_the_backfill_cursor_reads_back_what_it_wrote() -> None:
    from server.memory.workers.code_links import Backfill

    for state in (Backfill(), Backfill(after=("o", "n"), stall=True), Backfill(done=True)):
        assert Backfill.from_json(json.loads(json.dumps(state.to_json()))) == state
    assert Backfill().to_json() == {} and Backfill(done=True).to_json() == {"done": True}
    stalled = Backfill(after=("o", "n"), stall=True)
    assert stalled.restarted() == Backfill(stall=True)
    assert stalled.read_to(None) == Backfill(done=True, stall=True)


@pytest.mark.asyncio
async def test_a_link_not_rehashed_to_these_rules_is_not_rechecked(monkeypatch) -> None:
    def link(mid: str, version: str) -> dict[str, Any]:
        return _full({"path": "a.py", "qualified_name": "a::f", "memory_id": mid}) | (
            {"fingerprint_version": version} if version else {}
        )

    ours, old = link("m1", ""), link("m2", "0-old")
    seen: list[list[str]] = []

    async def recheck(org, cns, rows, changed, head, files):
        seen.append([a["memory_id"] for a in rows])
        return []

    monkeypatch.setattr(code_link_recheck, "_recheck", recheck)
    unparsed: set[str] = set()
    await code_link_recheck._recheck_paths(
        "o",
        "code-a",
        {"a.py": [ours, old]},
        ["a.py"],
        None,
        "c2",
        code_link_pass.RepoReads("o", "code-a"),
        unparsed,
        set(),
    )
    # its path is retried from the link's own commit once the re-hash succeeds
    assert seen == [["m1"]] and unparsed == {"a.py"}


@pytest.mark.asyncio
async def test_a_link_not_rehashed_to_these_rules_stays_due_unjudged() -> None:
    old = _full(
        {
            "path": "a.py",
            "qualified_name": "a::f",
            "state": "changed",
            "policy_due": 1,
            "fingerprint_version": "0-old",
        }
    )
    budget = code_link_pass.Budget(judgements=5)
    out = await code_link_policy._revise(
        "o",
        _row("m1"),
        [old],
        code_change.REVISE,
        "c2",
        code_link_pass.RepoReads("o", "code-a"),
        budget,
        code_link_pass.PassResult(),
    )
    assert out is None and budget.judgements == 5  # deferred whole: nothing written


@pytest.mark.asyncio
@pytest.mark.parametrize("gone", [{"tombstoned_at": datetime(2026, 1, 1)}, {"status": "deleted"}])
async def test_a_move_of_a_memory_gone_since_it_was_read_is_dropped(monkeypatch, gone) -> None:
    head = _row("m1") | gone

    async def memory_heads(org, ids):
        return [head]

    monkeypatch.setattr(code_links.store, "memory_heads", memory_heads)
    inputs = code_link_reads.inputs_now("o", head)
    row = _full({"memory_id": "m1", "code_namespace": "code-a", "inputs_hash": inputs})
    move = code_link_recheck.Move(row, code_link_pass.link_of(row))
    assert await code_link_recheck._current_moves("o", [move]) == []
    head = _row("m1")
    assert await code_link_recheck._current_moves("o", [move]) == [move]


def test_the_settle_window_has_a_floor() -> None:
    from pydantic import ValidationError

    from server.settings import Settings

    with pytest.raises(ValidationError):
        Settings(memory_code_links_settle_seconds=5)


@pytest.mark.asyncio
async def test_elsewhere_finds_the_same_name_and_the_same_name_in_another_module(
    monkeypatch,
) -> None:
    # the same qualified name; its path gives it another local name
    same = Definition("lib/a.py", "pkg.a::fn", "fn")
    moved = Definition("pkg/b.py", "pkg.b::fn", "fn")  # the same local name in another module
    other = Definition("pkg/b.py", "pkg.b::gn", "gn")

    async def candidates(org, cns, *, names=None, qualified_names=None):
        return [same] if qualified_names else [moved, other]

    monkeypatch.setattr(code_link_recheck.code, "candidates", candidates)
    found = await code_link_recheck._elsewhere("o", "code-a", {"pkg.a::fn": "pkg/a.py"})
    assert dict(found) == {"pkg.a::fn": [same, moved]}
