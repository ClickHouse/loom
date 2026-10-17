"""The Go compiler-tier worker — only `go` producers,
watermark `scip_commit_go`, a first run is a full per-module index, a
window maps its changed paths to the modules they fall under and runs the
harness over those only, an unbuildable module is named, not fatal."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, patch

import pytest

from server.code.workers import scip_go_delta as w
from tests.unit.code_fixtures import repo_config


def _Cfg(**kw: Any) -> Any:
    return repo_config(
        **{
            "org": "org",
            "namespace": "code-go",
            "scip_producer": "go",
            "exclude_generated": True,
            **kw,
        }
    )


def _Ts() -> Any:
    return _Cfg(namespace="code-ts", scip_producer="typescript")


def _repo(git_head: str, scip_commit_go: str) -> AsyncMock:
    repo = AsyncMock()

    async def get_state(org, ns, key):
        return {"git_head": git_head, "scip_commit_go": scip_commit_go}.get(key, "")

    repo.get_state.side_effect = get_state
    return repo


def _patched(repo, cfgs):
    return (
        patch("server.code.store.index.CodeRepo", return_value=repo),
        patch("server.code.domain.index.repos.configured_repos", return_value=cfgs),
    )


def _host(monkeypatch, tmp_path: Path) -> tuple[Path, Path]:
    checkout = tmp_path / "checkout"
    checkout.mkdir()
    bare = tmp_path / "repo.git"
    bare.mkdir()
    tools = tmp_path / "bin"
    tools.mkdir()
    (tools / "go").write_text("")
    (tools / "scip-go").write_text("")
    monkeypatch.setattr(w.settings, "code_scip_checkout_dir", str(checkout))
    monkeypatch.setattr(w.settings, "code_git_dir", str(bare))
    monkeypatch.setattr(w.settings, "code_scip_go_bin_dir", str(tools))
    monkeypatch.setattr(w.settings, "code_scip_go_path", "scip-go")
    return checkout, bare


def test_modules_for_window_maps_paths_to_their_nearest_module() -> None:
    mods = ["", "svc", "svc/internal/tool", "lib"]
    changed = [
        "svc/main.go",
        "svc/internal/tool/x.go",
        "lib/go.sum",
        "docs/readme.md",
        "cmd/root.go",
        "svc/README.md",
    ]
    assert w.modules_for_window(changed, mods) == [".", "lib", "svc", "svc/internal/tool"]
    assert w.modules_for_window(["docs/x.md", "svc/ui/App.vue"], mods) == []
    assert w.modules_for_window(["other/x.go"], ["svc"]) == []  # no module above it


def test_watermark_key_and_registry_field() -> None:
    from server.code.workers.scip_apply import watermark_key_for

    assert watermark_key_for("scip-go") == "scip_commit_go" == w.WATERMARK
    assert watermark_key_for("go") == "scip_commit_go"
    assert watermark_key_for("scip-python") == "scip_commit_py"
    assert watermark_key_for("scip_harness") == "scip_commit"
    assert w.PRODUCER == "go"


@pytest.mark.asyncio
async def test_only_go_producers_are_checked_and_a_current_tier_is_a_noop():
    repo = _repo("a" * 40, "a" * 40)
    p1, p2 = _patched(repo, [_Ts(), _Cfg()])
    with p1, p2:
        res = await w.run_pass()
    assert res.repos_checked == 1 and res.deltas_applied == 0
    assert not res.errors and not res.skipped


@pytest.mark.asyncio
async def test_missing_toolchain_skips_with_the_reason(monkeypatch, tmp_path: Path):
    checkout, bare = _host(monkeypatch, tmp_path)
    (tmp_path / "bin" / "scip-go").unlink()
    repo = _repo("a" * 40, "")
    p1, p2 = _patched(repo, [_Cfg()])
    with p1, p2:
        res = await w.run_pass()
    assert res.skipped and "scip-go not found" in res.skipped[0]


@pytest.mark.asyncio
async def test_first_run_is_full_over_every_module_and_applies_each_delta(monkeypatch, tmp_path):
    checkout, bare = _host(monkeypatch, tmp_path)
    git_calls: list[tuple[str, ...]] = []

    async def fake_git(cwd, *args, **kw):
        git_calls.append(args)

    harness_calls: list[tuple[str, str, list[str]]] = []

    async def fake_harness(co, base, head, out_dir, modules, cfg):
        harness_calls.append((base, head, modules))
        for name in ("svc.json", "lib.json"):
            (out_dir / name).write_text(
                json.dumps(
                    {
                        "delta_version": 1,
                        "commit": head,
                        "files": [
                            {
                                "path": f"{name[:-5]}/a.go",
                                "symbols": [
                                    {"qualified_name": f"x/{name[:-5]}::F", "kind": "function"}
                                ],
                                "edges": [],
                            }
                        ],
                    }
                )
            )
        return "done svc\nskip experiments/broken: go mod download failed: x\ndone lib\n"

    monkeypatch.setattr(w, "git", fake_git)
    monkeypatch.setattr(w, "_run_harness", fake_harness)
    applied: list[str] = []

    async def fake_apply(repo, org, ns, delta):
        applied.append(delta.files[0].path)
        from server.code.app.index.scip_delta import ScipApplyStats

        return ScipApplyStats(files=1, symbols_new=1)

    monkeypatch.setattr("server.code.app.index.scip_delta.apply_delta", fake_apply)
    repo = _repo("b" * 40, "")
    p1, p2 = _patched(repo, [_Cfg()])
    with p1, p2:
        res = await w.run_pass()
    assert res.deltas_applied == 1 and res.modules_indexed == 2 and not res.errors, res.errors
    assert harness_calls == [("--full", "b" * 40, [])]  # no watermark: every module
    assert git_calls[0][:2] == ("fetch", "--recurse-submodules=no") and str(bare) in git_calls[0]
    assert git_calls[1] == ("checkout", "--force", "b" * 40)
    assert sorted(applied) == ["lib/a.go", "svc/a.go"]
    assert res.modules_skipped == ["code-go: experiments/broken: go mod download failed: x"]
    repo.set_state.assert_awaited_with("org", "code-go", "scip_commit_go", "b" * 40)


@pytest.mark.asyncio
async def test_a_window_runs_only_the_modules_it_touched(monkeypatch, tmp_path: Path):
    checkout, bare = _host(monkeypatch, tmp_path)

    async def fake_git(cwd, *args, **kw):
        return None

    async def fake_changed(co, base, head):
        return ["svc/internal/x.go", "svc/go.sum", "docs/a.md"]

    async def fake_module_dirs(co):
        return ["", "svc", "lib"]

    harness_calls: list[tuple[str, list[str]]] = []

    async def fake_harness(co, base, head, out_dir, modules, cfg):
        harness_calls.append((base, modules))
        return "done svc\n"

    monkeypatch.setattr(w, "git", fake_git)
    monkeypatch.setattr(w, "_changed_paths", fake_changed)
    monkeypatch.setattr(w, "_module_dirs", fake_module_dirs)
    monkeypatch.setattr(w, "_run_harness", fake_harness)
    repo = _repo("b" * 40, "a" * 40)
    p1, p2 = _patched(repo, [_Cfg()])
    with p1, p2:
        res = await w.run_pass()
    assert harness_calls == [("a" * 40, ["svc"])]
    assert res.deltas_applied == 1 and res.modules_indexed == 0 and not res.errors
    repo.set_state.assert_awaited_with("org", "code-go", "scip_commit_go", "b" * 40)


@pytest.mark.asyncio
async def test_no_go_change_in_window_advances_the_watermark_without_the_harness(
    monkeypatch,
    tmp_path: Path,
):
    checkout, bare = _host(monkeypatch, tmp_path)

    async def fake_git(cwd, *args, **kw):
        return None

    async def fake_changed(co, base, head):
        return ["docs/a.md", "helm/values.yaml"]

    async def fake_module_dirs(co):
        return ["", "svc"]

    async def fake_harness(*a, **k):
        raise AssertionError("harness must not run")

    monkeypatch.setattr(w, "git", fake_git)
    monkeypatch.setattr(w, "_changed_paths", fake_changed)
    monkeypatch.setattr(w, "_module_dirs", fake_module_dirs)
    monkeypatch.setattr(w, "_run_harness", fake_harness)
    repo = _repo("b" * 40, "a" * 40)
    p1, p2 = _patched(repo, [_Cfg()])
    with p1, p2:
        res = await w.run_pass()
    assert res.deltas_applied == 0 and not res.errors
    repo.set_state.assert_awaited_with("org", "code-go", "scip_commit_go", "b" * 40)


def test_harness_env_carries_the_entrys_generated_rule_and_the_modules(monkeypatch, tmp_path):
    monkeypatch.setenv("PATH", "/usr/local/go/bin:/host/go/bin:/usr/bin")
    monkeypatch.setattr(w.settings, "code_scip_go_bin_dir", str(tmp_path))
    monkeypatch.setattr(w.settings, "code_scip_go_private", "go.example.com")
    env = w._harness_env(tmp_path, ["svc", "lib"], _Cfg())
    assert env["MODULES"] == "svc\nlib" and env["EXCLUDE_GENERATED"] == "1"
    assert env["GOPRIVATE"] == "go.example.com" and env["GOFLAGS"] == "-mod=mod"
    assert env["PATH"].startswith(str(tmp_path))
    assert "/usr/local/go/bin" in env["PATH"] and "/host/go/bin" in env["PATH"]
    assert "MODULES" not in w._harness_env(tmp_path, [], _Cfg())


def test_harness_env_carries_no_credential_into_module_downloads(monkeypatch, tmp_path):
    """`go mod download` runs the repository's module graph, so the entry's token
    never reaches it: no extraheader, nothing that names the token. Git still
    runs with the hardened settings (no system config, https and ssh only)."""
    monkeypatch.setattr(w.settings, "code_scip_go_bin_dir", str(tmp_path))
    monkeypatch.setattr(w.settings, "code_scip_go_modcache", str(tmp_path / "modcache"))
    private = _Cfg(github_token="ghp_secret", git_remote="https://github.com/acme/private.git")
    env = w._harness_env(tmp_path, ["svc"], private)
    assert "ghp_secret" not in " ".join(f"{k}={v}" for k, v in env.items())
    assert not any(v.startswith("AUTHORIZATION") for v in env.values())
    assert env["GIT_CONFIG_NOSYSTEM"] == "1" and env["GIT_TERMINAL_PROMPT"] == "0"


def test_each_entry_gets_its_own_module_cache(monkeypatch, tmp_path):
    """go reuses what is in GOMODCACHE without re-checking its sums, so a cache
    one repository's job can write must never be another's."""
    monkeypatch.setattr(w.settings, "code_scip_go_bin_dir", str(tmp_path))
    monkeypatch.setattr(w.settings, "code_scip_go_modcache", str(tmp_path / "modcache"))
    mine = w._harness_env(tmp_path, [], _Cfg(namespace="code-a"))
    theirs = w._harness_env(tmp_path, [], _Cfg(namespace="code-b"))
    assert mine["GOMODCACHE"] != theirs["GOMODCACHE"] and mine["GOPATH"] != theirs["GOPATH"]
    assert w._make_cache_dirs(_Cfg(namespace="code-a")) == [
        Path(mine["GOMODCACHE"]),
        Path(mine["GOPATH"]),
    ]
    assert Path(mine["GOMODCACHE"]).is_dir()
    assert Path(mine["GOMODCACHE"]).is_relative_to(tmp_path / "modcache")


def test_registry_gives_the_go_worker_its_own_deadline(monkeypatch):
    """The Go compiler-tier pass runs under
    `code_scip_go_delta_timeout_seconds` plus headroom for the steps around the
    harness, not the 900 s the interval implies — a first pass over a few hundred
    modules can take longer and would be cancelled with nothing applied."""
    from server.settings import settings
    from server.workers import registry

    monkeypatch.setattr(settings, "code_scip_go_delta_enabled", True, raising=False)
    monkeypatch.setattr(settings, "code_scip_go_delta_timeout_seconds", 7200, raising=False)
    monkeypatch.setattr(settings, "code_scip_delta_enabled", False, raising=False)
    monkeypatch.setattr(settings, "code_scip_ts_delta_enabled", False, raising=False)
    spec = next(w for w in registry.enabled_workers() if w.name == "code_scip_go_delta")
    assert spec.deadline_seconds() == 7200.0 + registry._TIER_HEADROOM_S
    assert spec.interval_seconds == settings.code_scip_go_delta_interval_seconds
