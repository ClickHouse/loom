"""run_typescript_delta.sh with SCIP_TS_PROJECTS indexes each named project through its
own tsconfig, or through an untracked tsconfig.scip.json (plus a package.json stub when
the directory has none) that reads its JavaScript, and leaves the tree as it found it.
scip-typescript and the converter are stubs; git runs for real."""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

_SCRIPT = (
    Path(__file__).resolve().parents[2] / "scripts" / "scip_harness" / "run_typescript_delta.sh"
)

_FAKE_SCIP = """#!/usr/bin/env bash
echo "args $*" >>"$CALLS"
for f in plug/tsconfig.scip.json plug/package.json lib/tsconfig.scip.json lib/package.json; do
  [[ -f "$f" ]] && echo "present $f $(head -c 40 "$f")" >>"$CALLS"
done
while [[ $# -gt 0 ]]; do [[ "$1" == "--output" ]] && : >"$2"; shift; done
"""

_FAKE_PYTHON = """#!/usr/bin/env bash
while [[ $# -gt 0 ]]; do [[ "$1" == "--dry-run" ]] && echo '{}' >"$2"; shift; done
"""


def _git(cwd: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-C", str(cwd), *args], check=True, capture_output=True, text=True
    ).stdout.strip()


def test_named_projects_are_indexed_through_their_own_or_a_generated_tsconfig(
    tmp_path: Path,
) -> None:
    repo = tmp_path / "repo"
    for rel, text in {
        "app/tsconfig.json": "{}",
        "app/src/a.ts": "export const a = 1;\n",
        "plug/bin/x.mjs": "export function x() {}\n",
        "lib/package.json": '{"name": "lib"}',
        "lib/y.js": "module.exports = 1;\n",
    }.items():
        (repo / rel).parent.mkdir(parents=True, exist_ok=True)
        (repo / rel).write_text(text)
    _git(tmp_path, "init", "-q", str(repo))
    _git(repo, "add", "-A")
    _git(repo, "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-qm", "init")
    head = _git(repo, "rev-parse", "HEAD")
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
        "SCIP_TS_PROJECTS": "app plug lib missing",
    }
    subprocess.run(
        ["bash", str(_SCRIPT), str(repo), "--full", head],
        env=env,
        check=True,
        capture_output=True,
        text=True,
    )
    log = calls.read_text().splitlines()
    args = next(line for line in log if line.startswith("args ")).split()
    assert args[1:5] == ["index", "app", "plug/tsconfig.scip.json", "lib/tsconfig.scip.json"]
    assert "--infer-tsconfig" not in args, "inference writes a tsconfig.json into the tree"
    present = {line.split()[1]: line for line in log if line.startswith("present ")}
    assert set(present) == {
        "plug/tsconfig.scip.json",
        "plug/package.json",
        "lib/tsconfig.scip.json",
        "lib/package.json",
    }
    assert '"scip-plug"' in present["plug/package.json"]
    assert '"lib"' in present["lib/package.json"], "an existing package.json is kept as it is"
    assert _git(repo, "status", "--porcelain") == "", "generated files are removed on exit"
    assert (repo / "lib/package.json").read_text() == '{"name": "lib"}'
