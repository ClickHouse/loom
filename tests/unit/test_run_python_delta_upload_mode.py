"""run_python_delta.sh in upload mode indexes the namespace's git_head, not
wherever its checkout was left: it brings the checkout to git_head (from the
bare clone when readable, else the checkout's origin), stays quiet when the
watermark is already there, and leaves alone an entry the in-process python
worker owns and a checkout a go/typescript worker moves. The server-side reads are a stub `python` that answers the identity
line; git runs for real."""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest

_SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "scip_harness" / "run_python_delta.sh"

_FAKE_PYTHON = """#!/usr/bin/env bash
if [[ "$1" == "-" ]]; then
  cat >/dev/null
  if [[ -n "${FAKE_IDENT_EXIT:-}" ]]; then echo "identity failed" >&2; exit "$FAKE_IDENT_EXIT"; fi
  echo "$FAKE_IDENT"
  exit 0
fi
echo "converter $*" >>"$CALLS"
while [[ $# -gt 0 ]]; do
  if [[ "$1" == "--changed-files" ]]; then sed 's/^/changed /' "$2" >>"$CALLS"; fi
  shift
done
"""

_FAKE_SCIP = """#!/usr/bin/env bash
while [[ $# -gt 0 ]]; do [[ "$1" == "--output" ]] && : >"$2"; shift; done
"""


def _git(cwd: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-C", str(cwd), *args], check=True, capture_output=True, text=True
    ).stdout.strip()


def _commit(work: Path, text: str) -> str:
    (work / "a.py").write_text(text)
    _git(work, "add", "a.py")
    _git(work, "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-qm", text)
    return _git(work, "rev-parse", "HEAD")


@pytest.fixture
def repos(tmp_path: Path) -> dict[str, object]:
    """A bare clone at c2 and a checkout cloned from it while it was at c1."""
    work = tmp_path / "work"
    work.mkdir()
    _git(work, "init", "-q", "-b", "main")
    c1 = _commit(work, "x = 1\n")
    bare = tmp_path / "repo.git"
    subprocess.run(["git", "clone", "-q", "--bare", str(work), str(bare)], check=True)
    checkout = tmp_path / "checkout"
    subprocess.run(["git", "clone", "-q", str(bare), str(checkout)], check=True)
    c2 = _commit(work, "x = 2\n")
    _git(work, "push", "-q", str(bare), "main")
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    for name, body in (("python", _FAKE_PYTHON), ("scip-python", _FAKE_SCIP)):
        (bin_dir / name).write_text(body)
        (bin_dir / name).chmod(0o755)
    return {
        "c1": c1,
        "c2": c2,
        "bare": bare,
        "checkout": checkout,
        "bin": bin_dir,
        "calls": tmp_path / "calls",
    }


def _run(
    r: dict[str, object], *, wm: str, head: str, bare: str, owner: str = "manual"
) -> tuple[str, str]:
    env = {
        **os.environ,
        "FAKE_IDENT": f"org code-x {wm} {head} {bare} {owner}",
        "CALLS": str(r["calls"]),
        "PYTHON": str(Path(str(r["bin"])) / "python"),
        "SCIP_PYTHON": str(Path(str(r["bin"])) / "scip-python"),
        "LOOM_API_KEY": "k",
    }
    env.pop("DELTA_OUT", None)
    out = subprocess.run(
        ["bash", str(_SCRIPT), str(r["checkout"]), "http://loom", "org", "code-x"],
        env=env,
        check=True,
        capture_output=True,
        text=True,
    ).stdout
    calls = Path(str(r["calls"]))
    return out, calls.read_text() if calls.exists() else ""


@pytest.mark.parametrize("source", ["bare", "origin"])
def test_a_stale_checkout_is_brought_to_git_head_before_indexing(repos, source: str) -> None:
    bare = str(repos["bare"]) if source == "bare" else str(repos["bare"]) + "-unreadable"
    out, calls = _run(repos, wm=str(repos["c1"]), head=str(repos["c2"]), bare=bare)
    assert "current" not in out
    assert _git(Path(str(repos["checkout"])), "rev-parse", "HEAD") == repos["c2"]
    assert f"--commit {repos['c2']} --base {repos['c1']}" in calls
    assert "changed a.py" in calls


def test_the_watermark_at_git_head_is_current_wherever_the_checkout_sits(repos) -> None:
    out, calls = _run(repos, wm=str(repos["c2"]), head=str(repos["c2"]), bare=str(repos["bare"]))
    assert f"python tier current at {repos['c2']}" in out
    assert calls == ""


def test_an_entry_the_worker_owns_is_left_to_the_worker(repos) -> None:
    out, calls = _run(
        repos, wm=str(repos["c1"]), head=str(repos["c2"]), bare=str(repos["bare"]), owner="worker"
    )
    assert "worker owns the python tier" in out
    assert calls == ""
    assert _git(Path(str(repos["checkout"])), "rev-parse", "HEAD") == repos["c1"]


@pytest.mark.parametrize("producer", ["go", "typescript"])
def test_a_checkout_another_worker_moves_is_never_touched(repos, producer: str) -> None:
    out, calls = _run(
        repos, wm=str(repos["c1"]), head=str(repos["c2"]), bare=str(repos["bare"]), owner=producer
    )
    assert f"belongs to the {producer} worker" in out
    assert calls == ""
    assert _git(Path(str(repos["checkout"])), "rev-parse", "HEAD") == repos["c1"]


def test_a_tree_without_a_python_build_file_gets_no_python_pass(repos) -> None:
    out, calls = _run(
        repos, wm=str(repos["c1"]), head=str(repos["c2"]), bare=str(repos["bare"]), owner="none"
    )
    assert "no python tier" in out
    assert calls == ""
    assert _git(Path(str(repos["checkout"])), "rev-parse", "HEAD") == repos["c1"]


def test_an_unseeded_git_head_indexes_nothing(repos) -> None:
    out, calls = _run(repos, wm="-", head="-", bare=str(repos["bare"]))
    assert "git_head not seeded" in out
    assert calls == ""


def test_scip_python_runs_with_the_wildcard_import_stubs_on_its_path(repos) -> None:
    """`from delta import *` with delta unresolved makes scip-python's pyright
    assert and abandon the whole index; the harness puts python_stubs/ first on
    PYTHONPATH so the import resolves."""
    scip = Path(str(repos["bin"])) / "scip-python"
    scip.write_text(
        '#!/usr/bin/env bash\necho "pythonpath $PYTHONPATH" >>"$CALLS"\n'
        + _FAKE_SCIP.split("\n", 1)[1]
    )
    _, calls = _run(repos, wm=str(repos["c1"]), head=str(repos["c2"]), bare=str(repos["bare"]))
    line = next(c for c in calls.splitlines() if c.startswith("pythonpath "))
    first = Path(line.split(" ", 1)[1].split(":")[0])
    assert first.is_absolute() and first.name == "python_stubs"
    assert (first / "delta" / "__init__.pyi").is_file()


def _ident_snippet() -> str:
    """The Python the script runs for the entry's identity, as written in it."""
    text = _SCRIPT.read_text()
    start = text.index('"$PYTHON" - <<PYEOF\n') + len('"$PYTHON" - <<PYEOF\n')
    return text[start : text.index("\nPYEOF", start)]


@pytest.mark.parametrize(
    ("tree", "producer", "extras", "py_worker", "owner"),
    [
        (["go.mod"], "", (), False, "none"),
        (["go.mod", "pyproject.toml"], "", (), False, "manual"),
        (["pyproject.toml"], "", (), False, "manual"),
        (["CMakeLists.txt"], "", (), False, "none"),
        (["go.mod", "pyproject.toml"], "go", (), False, "go"),
        (["go.mod", "pyproject.toml"], "go", ("python",), False, "go"),
        (["CMakeLists.txt"], "clang", (), False, "manual"),
        (["go.mod"], "", (), True, "none"),
        (["go.mod", "pyproject.toml"], "", (), True, "worker"),
        (["go.mod", "pyproject.toml"], "go", ("python",), True, "worker"),
    ],
)
def test_the_manual_python_pass_goes_where_the_entry_and_its_tree_say(
    monkeypatch, capsys, tree, producer, extras, py_worker, owner
) -> None:
    """The Python worker takes what it owns; a Go or TypeScript entry's worker moves its
    checkout; an entry naming no producer gets the manual pass only where its tree has a
    Python build file (its Go and TypeScript tiers work in their own trees)."""
    from unittest.mock import AsyncMock

    from server.code.app.index import repo_registry
    from server.code.domain.index import repos
    from server.code.store import index as store_index
    from server.core import ch
    from server.settings import settings
    from tests.unit.code_fixtures import repo_config

    repo = AsyncMock()
    repo.get_state.return_value = ""
    repo.live_paths_present.return_value = tree
    monkeypatch.setattr(store_index, "CodeRepo", lambda: repo)
    monkeypatch.setattr(ch, "get_client", lambda: AsyncMock())
    monkeypatch.setattr(settings, "code_scip_py_delta_enabled", py_worker)
    cfg = repo_config(
        org="org", namespace="code-x", scip_producer=producer, scip_extra_producers=extras
    )
    monkeypatch.setattr(repos, "configured_repos", lambda: [cfg])
    monkeypatch.setattr(repo_registry, "read_registry", AsyncMock(return_value=True))
    monkeypatch.setenv("ORG_ARG", "org")
    monkeypatch.setenv("NS_ARG", "code-x")

    exec(_ident_snippet(), {})

    assert capsys.readouterr().out.split()[-1] == owner


def test_the_identity_read_loads_the_repository_registry_before_listing_entries(
    monkeypatch, capsys
) -> None:
    """The script runs in a fresh process, which has not read the self-service registry:
    `configured_repos` refuses until it is read, so the snippet reads it first. The
    registry and `configured_repos` are the real ones; only the store is a stub."""
    from unittest.mock import AsyncMock

    from server.code.app.index import repo_registry
    from server.code.store import index as store_index
    from server.core import ch
    from server.settings import settings

    repo = AsyncMock()
    repo.get_state.return_value = ""
    repo.registry_rows.return_value = [
        {
            "org": "org",
            "namespace": "code-x",
            "repo": "acme/x",
            "git_branch": "main",
            "active": 1,
            "row_kind": "entry",
            "scip_producer": "go",
        }
    ]
    monkeypatch.setattr(store_index, "CodeRepo", lambda *a: repo)
    monkeypatch.setattr(ch, "get_client", lambda: AsyncMock())
    monkeypatch.setattr(settings, "code_self_service_enabled", True)
    repo_registry.reset_for_tests()
    monkeypatch.setenv("ORG_ARG", "org")
    monkeypatch.setenv("NS_ARG", "code-x")

    try:
        exec(_ident_snippet(), {})
    finally:
        repo_registry.reset_for_tests()

    assert capsys.readouterr().out.split()[-1] == "go"


def test_a_failed_identity_read_stops_the_script_before_indexing(repos) -> None:
    """When the identity step exits non-zero (an unreadable registry, an unreachable
    ClickHouse), the script stops: no converter runs on an empty identity line."""
    env = {
        **os.environ,
        "FAKE_IDENT_EXIT": "1",
        "CALLS": str(repos["calls"]),
        "PYTHON": str(Path(str(repos["bin"])) / "python"),
        "SCIP_PYTHON": str(Path(str(repos["bin"])) / "scip-python"),
        "LOOM_API_KEY": "k",
    }
    env.pop("DELTA_OUT", None)
    done = subprocess.run(
        ["bash", str(_SCRIPT), str(repos["checkout"]), "http://loom", "org", "code-x"],
        env=env,
        capture_output=True,
        text=True,
    )
    calls = Path(str(repos["calls"]))
    assert done.returncode != 0
    assert "identity failed" in done.stderr
    assert "converter" not in (calls.read_text() if calls.exists() else "")


def test_an_unreadable_registry_stops_the_identity_read(monkeypatch) -> None:
    """With the registry unreadable the snippet exits non-zero and names why, rather than
    guessing which entries are configured."""
    from unittest.mock import AsyncMock

    from server.code.app.index import repo_registry
    from server.code.store import index as store_index
    from server.core import ch

    monkeypatch.setattr(store_index, "CodeRepo", lambda: AsyncMock())
    monkeypatch.setattr(ch, "get_client", lambda: AsyncMock())
    monkeypatch.setattr(repo_registry, "read_registry", AsyncMock(return_value=False))

    with pytest.raises(SystemExit, match="registry could not be read; retry"):
        exec(_ident_snippet(), {})
