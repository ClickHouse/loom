"""A repository's status reports the compiler tier it runs.

The Health tab reads the watermark of the tier the repository runs: a
TypeScript repository (scip-typescript advances `scip_commit_ts`) must not
show a stale C++ (`scip_commit`) card, an unset Python (`scip_commit_py`)
card, or a "Calls catching up" badge while its own tier is at the tip."""

from __future__ import annotations

from typing import Any

import pytest

from server.code.app import index_status
from server.code.domain.index.compiler_tiers import (
    TIERS,
    compiler_tiers,
    normalize_producer,
    primary_producer,
)
from server.code.domain.index.repos import RepoConfig
from server.code.workers.scip_apply import watermark_key_for
from server.settings import settings

pytestmark = pytest.mark.usefixtures("mirror_here")

TIP = "c" * 40
TS_HEAD = "e" * 40
STALE_CPP = "3" * 40


def _state(**rows: tuple[str, str]) -> dict[str, dict[str, Any]]:
    return {k: {"value": v, "detail": "", "updated_at": at} for k, (v, at) in rows.items()}


def test_every_tier_reads_the_key_its_producer_advances() -> None:
    # the table and the apply worker's key rule must agree, or a card reads
    # a key nobody writes
    for t in TIERS:
        assert watermark_key_for(t.tool) == t.watermark_key, t.producer


def test_normalize_producer_folds_tool_names() -> None:
    assert normalize_producer("scip-typescript") == "typescript"
    assert normalize_producer("TypeScript") == "typescript"
    assert normalize_producer("cpp") == "clang"
    assert normalize_producer("") == ""


def test_typescript_entry_headlines_its_own_tier_and_lists_the_stale_cpp_key() -> None:
    state = _state(
        git_head=(TS_HEAD, "2026-09-18 17:50:15"),
        scip_commit=(STALE_CPP, "2026-09-18 16:58:23"),
        scip_commit_ts=(TS_HEAD, "2026-09-18 20:54:08"),
    )
    lags = {"scip_commit": 5, "scip_commit_ts": 0}
    assert primary_producer("typescript", state) == "typescript"
    tiers = compiler_tiers("typescript", state, lags)
    assert [t["producer"] for t in tiers] == ["clang", "typescript"]
    ts = next(t for t in tiers if t["primary"])
    assert ts["producer"] == "typescript" and ts["tool"] == "scip-typescript"
    assert ts["commit"] == TS_HEAD and ts["lag"] == 0
    assert ts["updated_at"] == "2026-09-18 20:54:08"
    assert ts["workers"] == ["code_scip_ts_delta"]
    cpp = next(t for t in tiers if not t["primary"])
    assert cpp["lag"] == 5  # shown, and shown as what it is: another tier's watermark


def test_configured_tier_is_listed_unset_before_its_first_run() -> None:
    state = _state(git_head=(TS_HEAD, "x"))
    tiers = compiler_tiers("go", state, {})
    assert len(tiers) == 1
    assert tiers[0]["producer"] == "go" and tiers[0]["commit"] == "" and tiers[0]["lag"] == -1
    assert tiers[0]["updated_at"] == "" and tiers[0]["primary"] is True


def test_registry_row_without_a_producer_takes_the_tier_whose_watermark_is_set() -> None:
    state = _state(scip_commit_py=("a" * 40, "x"))
    assert primary_producer("", state) == "python"
    assert primary_producer("", {}) == ""
    assert compiler_tiers("", {}, {}) == []


# --- the status handler ------------------------------------------------------


_TS_STATE = _state(
    git_head=(TS_HEAD, "2026-09-18 17:50:15"),
    scip_commit=(STALE_CPP, "2026-09-18 16:58:23"),
    scip_commit_ts=(TS_HEAD, "2026-09-18 20:54:08"),
    clone_head=(TIP, "2026-09-19 17:24:48"),
)
_TS_STATE["clone_head"]["detail"] = "main"


class _FakeRepo:
    async def all_state(self, org: str, ns: str) -> dict[str, dict[str, Any]]:
        return {k: dict(v) for k, v in _TS_STATE.items()}

    async def table_counts(self, org: str, ns: str, **_: Any) -> dict[str, int]:
        return {"code_files": 3}


class _FakeGit:
    @staticmethod
    async def head_sha(git_dir: str, branch: str) -> str:
        raise AssertionError("clone_head row is published; git must not run")

    @staticmethod
    async def commit_count_between(git_dir: str, since: str, until: str) -> int:
        return {TS_HEAD: 0, STALE_CPP: 5}[since]


@pytest.fixture(autouse=True)
def _fakes(monkeypatch: pytest.MonkeyPatch):
    index_status.reset_repo_status_cache()
    monkeypatch.setattr(index_status, "CodeRepo", _FakeRepo)
    monkeypatch.setattr(settings, "code_intel_enabled", True)
    import server.code.app.index.gitrepo as gitrepo

    monkeypatch.setattr(gitrepo, "head_sha", _FakeGit.head_sha)
    monkeypatch.setattr(gitrepo, "commit_count_between", _FakeGit.commit_count_between)


def _cfg(producer: str) -> RepoConfig:
    return RepoConfig(
        org="clickhouse",
        namespace="code-langfuse",
        repo="langfuse/langfuse",
        git_dir="var/code_repos/langfuse.git",
        git_remote="",
        git_branch="main",
        path_prefixes=("web/",),
        github_token="",
        scip_producer=producer,
    )


@pytest.mark.asyncio
async def test_status_of_a_typescript_repo_is_its_typescript_tier() -> None:
    st = await index_status._repo_status(_cfg("typescript"))
    assert st.scip_producer == "typescript"
    assert st.scip_commit == TS_HEAD and st.scip_lag == 0  # not the stale C++ key
    assert st.fast_tier_lag == 0 and st.scip_py_lag == -1
    assert [t.producer for t in st.compiler_tiers] == ["clang", "typescript"]
    primary = next(t for t in st.compiler_tiers if t.primary)
    assert primary.watermark_key == "scip_commit_ts" and primary.lag == 0
    assert primary.updated_at == "2026-09-18 20:54:08"


@pytest.mark.asyncio
async def test_status_of_a_cpp_entry_keeps_the_cpp_key_as_before() -> None:
    st = await index_status._repo_status(_cfg("clang"))
    assert st.scip_producer == "clang"
    assert st.scip_commit == STALE_CPP and st.scip_lag == 5
    assert [t.producer for t in st.compiler_tiers] == ["clang", "typescript"]
    assert st.compiler_tiers[0].primary is True


@pytest.mark.asyncio
async def test_repo_listing_carries_the_producer() -> None:
    from server.code.api.models_repos import CodeRepoSummary

    row = CodeRepoSummary(org="o", namespace="n", repo="r", branch="b")
    assert row.scip_producer == ""  # additive: older clients read the row whole


def test_every_tier_names_workers_the_registry_builds() -> None:
    """A tier card names the workers that advance its watermark; a name the
    registry does not build (or a registered delta worker the card leaves
    out) sends a reader to the wrong process."""
    import json
    from pathlib import Path

    snap = json.loads((Path(__file__).parent / "snapshots" / "worker_registry.json").read_text())
    built = {w["name"] for w in snap["all_on"]}
    for t in TIERS:
        assert set(t.workers) <= built, (t.producer, set(t.workers) - built)
    deltas = {n for n in built if n.startswith("code_scip_") and n.endswith("_delta")}
    assert deltas <= {w for t in TIERS for w in t.workers}
