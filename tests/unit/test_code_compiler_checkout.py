"""Dashboard-added repositories receive persistent compiler working trees."""

from __future__ import annotations

import subprocess
from pathlib import Path

from server.code.app.index.compiler_tier import resolve_checkout
from tests.unit.code_fixtures import repo_config


def _bare_repo(tmp_path: Path) -> Path:
    source = tmp_path / "source"
    source.mkdir()
    subprocess.run(["git", "init", "-q", str(source)], check=True)
    (source / "main.py").write_text("print('ok')\n")
    subprocess.run(["git", "-C", str(source), "add", "main.py"], check=True)
    subprocess.run(
        [
            "git",
            "-C",
            str(source),
            "-c",
            "user.name=test",
            "-c",
            "user.email=test@example.com",
            "commit",
            "-qm",
            "initial",
        ],
        check=True,
    )
    bare = tmp_path / "repo.git"
    subprocess.run(["git", "clone", "--bare", "-q", str(source), str(bare)], check=True)
    return bare


def test_registry_entry_initializes_checkout_for_its_bare_mirror(tmp_path: Path) -> None:
    bare = _bare_repo(tmp_path)
    checkout = tmp_path / "checkouts" / "code-example"
    cfg = repo_config(
        source="registry",
        git_dir=str(bare),
        scip_checkout_dir=str(checkout),
        classification="standard",
    )

    actual, actual_bare, missing = resolve_checkout(cfg, "python")

    assert missing is None
    assert actual == checkout and (checkout / ".git").exists()
    assert actual_bare == bare.resolve()


def test_operator_entry_keeps_explicit_checkout_contract(tmp_path: Path) -> None:
    bare = _bare_repo(tmp_path)
    checkout = tmp_path / "operator-owned"
    cfg = repo_config(source="env", git_dir=str(bare), scip_checkout_dir=str(checkout))

    _actual, _bare, missing = resolve_checkout(cfg, "python")

    assert missing is not None and "checkout missing" in missing
    assert not checkout.exists()
