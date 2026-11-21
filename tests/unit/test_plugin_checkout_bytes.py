"""Shell launchers and skill files keep their committed LF bytes in a checkout."""

from __future__ import annotations

import fnmatch
import os
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
PROTECTED = (
    "*.sh",
    "integrations/*/bin/loom-node",
    "integrations/grep-shim/bin/*",
    "integrations/cline/hooks/*",
    "integrations/*/skills/**",
    "skills/**",
)
EXAMPLES = (
    "run.sh",
    "integrations/claude-code-plugin/bin/loom-node",
    "integrations/grep-shim/bin/grep",
    "integrations/cline/hooks/PreToolUse",
    "integrations/codex-plugin/skills/memory/SKILL.md",
    "skills/memory/SKILL.md",
)


def _git(root: Path, *args: str) -> bytes:
    env = {
        key: value
        for key, value in os.environ.items()
        if key not in {"GIT_DIR", "GIT_WORK_TREE", "GIT_INDEX_FILE", "GIT_COMMON_DIR"}
    }
    env.update(GIT_CONFIG_NOSYSTEM="1", GIT_CONFIG_GLOBAL=os.devnull)
    return subprocess.run(["git", *args], cwd=root, env=env, check=True, capture_output=True).stdout


def test_protected_committed_files_are_lf_and_disable_conversion() -> None:
    names = _git(ROOT, "ls-tree", "-rz", "--name-only", "HEAD").decode().split("\0")
    protected = [name for name in names if any(fnmatch.fnmatchcase(name, pat) for pat in PROTECTED)]
    assert protected, "No protected scripts or skills found"
    for name in protected:
        data = _git(ROOT, "show", f"HEAD:{name}")
        assert b"\r" not in data, f"Commit LF bytes in {name}; Git preserves them unchanged"
        attrs = _git(ROOT, "check-attr", "-z", "text", "eol", "--", name).decode().split("\0")
        assert attrs[1:3] == ["text", "unset"], name
        assert attrs[4:6] == ["eol", "unspecified"], name


@pytest.mark.parametrize("autocrlf", ["true", "input", "false"])
def test_checkout_preserves_protected_bytes_with_autocrlf(tmp_path: Path, autocrlf: str) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-q")
    (repo / ".gitattributes").write_bytes((ROOT / ".gitattributes").read_bytes())
    content = b"# Fixture\none\ntwo\n"
    for name in (*EXAMPLES, "ordinary.txt"):
        target = repo / name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(content)
    _git(repo, "-c", "core.autocrlf=false", "add", ".")
    dest = tmp_path / "checkout"
    dest.mkdir()
    _git(
        repo,
        "-c",
        f"core.autocrlf={autocrlf}",
        "-c",
        "core.eol=crlf",
        "checkout-index",
        "--all",
        f"--prefix={dest}/",
    )
    for name in EXAMPLES:
        assert (dest / name).read_bytes() == content, (autocrlf, name)
    expected = content.replace(b"\n", b"\r\n") if autocrlf == "true" else content
    assert (dest / "ordinary.txt").read_bytes() == expected
