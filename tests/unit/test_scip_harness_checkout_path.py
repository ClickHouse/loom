"""A compiler-tier harness hands the converter an absolute --source-root. The
workers configure checkouts relative to the server root; the harness `cd`s into the
checkout before converting, where a relative root names nothing: the TypeScript
converter would map no package directory, drop every symbol, and the tier's
watermark would advance over an empty delta."""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

_HARNESS = Path(__file__).resolve().parents[2] / "scripts" / "scip_harness"

_FAKE_SCIP = """#!/usr/bin/env bash
while [[ $# -gt 0 ]]; do [[ "$1" == "--output" ]] && : >"$2"; shift; done
"""

_FAKE_PYTHON = """#!/usr/bin/env bash
echo "$*" >>"$CALLS"
while [[ $# -gt 0 ]]; do [[ "$1" == "--dry-run" ]] && echo '{}' >"$2"; shift; done
"""


def test_a_relative_checkout_reaches_the_converter_as_an_absolute_root(tmp_path: Path) -> None:
    repo = tmp_path / "var" / "checkout"
    (repo / "src").mkdir(parents=True)
    (repo / "tsconfig.json").write_text("{}")
    (repo / "src" / "a.ts").write_text("export const a = 1;\n")
    subprocess.run(["git", "init", "-q", str(repo)], check=True)
    subprocess.run(["git", "-C", str(repo), "add", "-A"], check=True)
    subprocess.run(
        ["git", "-C", str(repo), "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-qm", "i"],
        check=True,
    )
    head = subprocess.run(
        ["git", "-C", str(repo), "rev-parse", "HEAD"], check=True, capture_output=True, text=True
    ).stdout.strip()
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    for name, body in (("scip-typescript", _FAKE_SCIP), ("python", _FAKE_PYTHON)):
        (bin_dir / name).write_text(body)
        (bin_dir / name).chmod(0o755)
    calls = tmp_path / "calls"
    env = {
        **os.environ,
        "CALLS": str(calls),
        "DELTA_OUT": str(tmp_path / "delta.json"),
        "PYTHON": str(bin_dir / "python"),
        "SCIP_TYPESCRIPT": str(bin_dir / "scip-typescript"),
        "SCIP_TS_PATHS": "0",
        "WORKSPACES": "",
    }
    subprocess.run(
        ["bash", str(_HARNESS / "run_typescript_delta.sh"), "var/checkout", "--full", head],
        cwd=tmp_path,
        env=env,
        check=True,
        capture_output=True,
        text=True,
    )
    args = calls.read_text().split()
    root = args[args.index("--source-root") + 1]
    assert Path(root).is_absolute() and Path(root) == repo.resolve()


def test_the_converter_refuses_a_source_root_that_names_nothing(tmp_path: Path) -> None:
    out = subprocess.run(
        [
            sys.executable,
            str(_HARNESS / "scip_to_delta.py"),
            "--index",
            str(tmp_path / "none.scip"),
            "--commit",
            "c" * 40,
            "--full",
            "--source-root",
            "var/code_repos/checkout-that-is-not-here",
            "--dry-run",
            str(tmp_path / "d.json"),
        ],
        capture_output=True,
        text=True,
    )
    assert out.returncode != 0 and "is not a directory" in out.stderr
    assert not (tmp_path / "d.json").exists()


def test_the_typescript_worker_resolves_its_checkout_to_an_absolute_path(
    tmp_path: Path, monkeypatch
) -> None:
    from server.code.domain.index.repos import RepoConfig
    from server.code.workers.scip_ts_delta import _resolve_paths

    (tmp_path / "var" / "co" / "node_modules").mkdir(parents=True)
    (tmp_path / "var" / "x.git").mkdir()
    compiler = tmp_path / "scip-typescript"
    compiler.touch()
    monkeypatch.setattr(
        "server.code.workers.scip_ts_delta.settings.code_scip_typescript_path", str(compiler)
    )
    monkeypatch.chdir(tmp_path)
    cfg = RepoConfig(
        org="o",
        namespace="n",
        repo="o/x",
        git_dir="var/x.git",
        git_remote="",
        git_branch="main",
        path_prefixes=("",),
        github_token="",
        scip_checkout_dir="var/co",
        scip_producer="typescript",
    )
    checkout, _bare, missing = _resolve_paths(cfg)
    assert missing is None and checkout.is_absolute() and checkout == tmp_path / "var" / "co"


def test_the_typescript_worker_skips_when_the_binary_is_missing(
    tmp_path: Path, monkeypatch
) -> None:
    from server.code.domain.index.repos import RepoConfig
    from server.code.workers.scip_ts_delta import _resolve_paths

    checkout = tmp_path / "co"
    (checkout / "node_modules").mkdir(parents=True)
    bare = tmp_path / "x.git"
    bare.mkdir()
    monkeypatch.setattr(
        "server.code.workers.scip_ts_delta.settings.code_scip_typescript_path",
        str(tmp_path / "no-scip-typescript"),
    )
    cfg = RepoConfig(
        org="o",
        namespace="n",
        repo="o/x",
        git_dir=str(bare),
        git_remote="",
        git_branch="main",
        path_prefixes=("",),
        github_token="",
        scip_checkout_dir=str(checkout),
        scip_producer="typescript",
    )
    _checkout, _bare, missing = _resolve_paths(cfg)
    assert missing is not None and "scip-typescript missing" in missing
