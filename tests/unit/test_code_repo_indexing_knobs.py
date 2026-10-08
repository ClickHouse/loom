"""A repository added from the UI carries `exclude_generated`
and `scip_producer`; an empty producer is inferred from the tree's build
manifests, and each compiler-tier worker owns an entry only for its own."""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock

import pytest
from pydantic import ValidationError

from server.code.api.models_repos import CodeRepoAddInput
from server.code.app.index import producer_inference as pi
from server.code.app.index.repo_registry import row_to_config
from tests.unit.code_fixtures import repo_config


def test_manifests_name_their_producers() -> None:
    assert pi.infer_producers(["go.mod", "cmd/main.go"]) == ["go"]
    assert pi.infer_producers(["svc/go.mod", "svc/main.go"]) == ["go"]  # a monorepo: below the root
    assert pi.infer_producers(["package.json", "src/a.js", "jsconfig.json"]) == ["typescript"]
    assert pi.infer_producers(["CMakeLists.txt", "src/a.cpp"]) == ["clang"]
    # a vendored library's CMake file is not the repository's build
    assert pi.infer_producers(["contrib/zlib/CMakeLists.txt", "src/a.cpp"]) == []
    assert pi.infer_producers(["pyproject.toml", "pkg/a.py"]) == ["python"]
    # a platform monorepo names several tiers, in one stable order
    assert pi.infer_producers(["svc/go.mod", "ui/package.json", "pyproject.toml"]) == [
        "go",
        "typescript",
        "python",
    ]
    assert pi.infer_producers(["README.md", "docs/a.md"]) == []


def test_explicit_producer_wins_and_empty_defers_to_the_tree() -> None:
    assert pi.applies("clang", "clang", ["go"]) is True  # an .env entry's default
    assert pi.applies("clang", "go", ["go"]) is False
    assert pi.applies("", "go", ["go", "typescript"]) is True
    assert pi.applies("", "typescript", ["go"]) is False
    assert pi.applies("", "clang", []) is False


@pytest.mark.asyncio
async def test_tree_producers_reads_root_markers_then_a_go_mod_below() -> None:
    repo = AsyncMock()
    repo.live_paths_present.return_value = {"package.json", "jsconfig.json"}
    repo.live_paths_named.return_value = ["services/go.mod"]
    assert await pi.tree_producers(repo, "o", "n") == ["go", "typescript"]
    repo.live_paths_present.assert_awaited_once()
    repo.live_paths_named.assert_awaited_once_with("o", "n", "go.mod", limit=1)
    # a root go.mod needs no second read
    repo2 = AsyncMock()
    repo2.live_paths_present.return_value = {"go.mod"}
    assert await pi.tree_producers(repo2, "o", "n") == ["go"]
    repo2.live_paths_named.assert_not_awaited()


def _Entry(**kw: Any) -> Any:
    return repo_config(**{"org": "o", "namespace": "n", "scip_producer": "", **kw})


@pytest.mark.asyncio
async def test_worker_ownership_by_explicit_producer_or_by_marker() -> None:
    repo = AsyncMock()
    repo.live_paths_present.return_value = {"go.mod"}
    repo.live_paths_named.return_value = []
    assert await pi.worker_owns(repo, _Entry(), "go") is True
    assert await pi.worker_owns(repo, _Entry(), "typescript") is False
    assert await pi.worker_owns(repo, _Entry(), "clang") is False

    assert await pi.worker_owns(repo, _Entry(scip_producer="clang"), "clang") is True
    assert await pi.worker_owns(repo, _Entry(scip_producer="clang"), "go") is False
    repo.live_paths_present.assert_awaited()  # only the inferred entry read the tree


def test_registry_row_carries_the_knobs_and_defaults_them_for_old_rows() -> None:
    row = {
        "org": "o",
        "namespace": "code-x",
        "repo": "acme/x",
        "git_branch": "main",
        "path_prefixes": [],
        "exclude": ["*.snap"],
        "classification": "standard",
        "join_policy": "open",
        "github_token_enc": "",
        "exclude_generated": True,
        "scip_producer": "go",
    }
    cfg = row_to_config(row)
    assert cfg.exclude_generated is True and cfg.scip_producer == "go"
    old = {k: v for k, v in row.items() if k not in ("exclude_generated", "scip_producer")}
    cfg_old = row_to_config(old)
    assert cfg_old.exclude_generated is False and cfg_old.scip_producer == ""  # inferred


def test_add_input_defaults_and_choices() -> None:
    inp = CodeRepoAddInput(org="o", repo="acme/x")
    assert inp.exclude_generated is True and inp.scip_producer == ""
    assert CodeRepoAddInput(org="o", repo="acme/x", scip_producer="go").scip_producer == "go"
    with pytest.raises(ValidationError):
        CodeRepoAddInput(org="o", repo="acme/x", scip_producer="rust")


@pytest.mark.asyncio
async def test_insert_entry_writes_the_knobs(monkeypatch) -> None:
    from server.code.app.index import repo_registry as reg

    ch = AsyncMock()
    await reg.insert_entry(
        org="o",
        namespace="code-x",
        repo="acme/x",
        branch="main",
        classification="standard",
        join_policy="open",
        github_token="",
        path_prefixes=[],
        exclude=[],
        created_by="me",
        exclude_generated=False,
        scip_producer="typescript",
        ch=ch,
    )
    table, rows = ch.insert.await_args.args
    assert table == "loom.code_repos"
    assert rows[0]["exclude_generated"] is False and rows[0]["scip_producer"] == "typescript"


def test_registry_entry_gets_its_own_checkout(monkeypatch, tmp_path) -> None:
    """A Repositories-page entry never shares the operator entry's checkout: a tier
    pass would check that repository out over it (the operator's C++ and Python
    tiers then fail on every pass)."""
    from server.code.app.index.compiler_tier import resolve_checkout
    from server.code.app.index.repo_registry import settings as reg_settings
    from server.code.domain.index.repos import checkout_for

    monkeypatch.setattr(reg_settings, "code_repos_root", str(tmp_path))
    monkeypatch.setattr(reg_settings, "code_scip_checkout_dir", str(tmp_path / "checkout"))
    row = {"org": "o", "namespace": "code-x", "repo": "acme/x", "scip_producer": "python"}
    cfg = row_to_config(row)
    assert checkout_for(cfg, "python") == str(tmp_path / "checkout-code-x")
    (tmp_path / "acme__x.git").mkdir()
    checkout, _bare, missing = resolve_checkout(cfg, "python")
    assert missing is None and (checkout / ".git").is_dir()  # created on first use
    assert not (tmp_path / "checkout").exists()
