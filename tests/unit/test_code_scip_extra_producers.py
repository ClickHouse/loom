"""A repository entry can run more than one compiler tier: `scip_extra_producers`
names the tiers beside its primary `scip_producer`, each in its own checkout, and
`scip_ts_projects` names the TypeScript projects of a tree whose root is not one."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest

from server.code.app.index.producer_inference import worker_owns
from server.code.domain.index.compiler_tiers import owns_path
from server.code.domain.index.repos import RepoConfig, checkout_for


def _cfg(**kw) -> RepoConfig:
    base = dict(
        org="o",
        namespace="code-x",
        repo="o/x",
        git_dir="var/code_repos/x.git",
        git_remote="",
        git_branch="main",
        path_prefixes=("",),
        github_token="",
        scip_checkout_dir="var/code_repos/checkout-x",
        scip_producer="python",
    )
    base.update(kw)
    return RepoConfig(**base)


def test_extra_producers_are_owned_beside_the_primary_one() -> None:
    cfg = _cfg(scip_extra_producers=("typescript",))
    owned = {p: asyncio.run(worker_owns(None, cfg, p)) for p in ("python", "typescript", "go")}
    assert owned == {"python": True, "typescript": True, "go": False}
    assert not asyncio.run(worker_owns(None, _cfg(), "typescript"))


def test_each_extra_producer_indexes_its_own_checkout() -> None:
    cfg = _cfg(scip_extra_producers=("typescript",))
    assert checkout_for(cfg, "python") == "var/code_repos/checkout-x"
    assert checkout_for(cfg, "typescript") == "var/code_repos/checkout-x-typescript"
    assert checkout_for(_cfg(scip_producer="typescript"), "typescript") == (
        "var/code_repos/checkout-x"
    )


def test_the_entry_json_carries_the_extra_producers_and_the_projects(monkeypatch) -> None:
    from server.code.domain.index import repos
    from server.settings import settings

    entry = {
        "namespace": "code-x",
        "org": "o",
        "repo": "o/x",
        "scip_producer": "python",
        "scip_extra_producers": ["typescript"],
        "scip_ts_projects": "frontend, sdk/typescript",
    }
    monkeypatch.setattr(settings, "code_repos_json", json.dumps([entry]))
    (cfg,) = repos._configured_from_env()
    assert cfg.scip_extra_producers == ("typescript",)
    assert cfg.scip_ts_projects == ("frontend", "sdk/typescript")


@pytest.mark.parametrize(
    ("producer", "path", "owned"),
    [
        ("typescript", "integrations/plugin/bin/a.mjs", True),
        ("typescript", "server/a.py", False),
        ("scip-python", "server/a.py", True),
        ("python", "frontend/src/App.tsx", False),
        ("clang", "src/Core/Settings.h", True),
        ("go", "cmd/main.go", True),
    ],
)
def test_a_producer_owns_its_languages_paths(producer: str, path: str, owned: bool) -> None:
    assert owns_path(producer, path) is owned


def test_only_projects_with_a_package_manifest_need_installing(tmp_path: Path) -> None:
    from server.code.workers.scip_ts_delta import _install_roots

    (tmp_path / "frontend").mkdir()
    (tmp_path / "frontend" / "package-lock.json").write_text("{}")
    (tmp_path / "frontend" / "package.json").write_text("{}")
    (tmp_path / "plugin").mkdir()
    roots = _install_roots(tmp_path, ("frontend", "plugin"))
    assert roots == [tmp_path / "frontend"]
    assert _install_roots(tmp_path, ()) == []


def test_named_projects_in_a_workspace_are_installed_from_the_root(tmp_path: Path) -> None:
    from server.code.workers.scip_ts_delta import _install_roots

    (tmp_path / "package.json").write_text('{"workspaces": ["packages/*"]}')
    assert _install_roots(tmp_path, ("packages/app", "packages/lib")) == [tmp_path]
