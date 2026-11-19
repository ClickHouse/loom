"""Compiler checkout initialization and metadata refusal through worker housekeeping."""

from __future__ import annotations

import asyncio
import subprocess
from pathlib import Path

import pytest

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
    assert actual == tmp_path / "checkouts" / "code-example.python"
    assert (actual / ".git").exists()
    assert actual_bare == bare.resolve()


def test_operator_entry_gets_a_checkout_per_tier_scip_clang_included(tmp_path: Path) -> None:
    """A new pod starts with no tree: the C++ tier gets one like every tier."""
    bare = _bare_repo(tmp_path)
    checkout = tmp_path / "operator-owned"
    cfg = repo_config(source="env", git_dir=str(bare), scip_checkout_dir=str(checkout))

    actual, _bare, missing = resolve_checkout(cfg, "clang")
    assert missing is None and actual == checkout and (checkout / ".git").exists()

    actual, _bare, missing = resolve_checkout(cfg, "python")
    assert missing is None
    assert actual == tmp_path / "operator-owned.python" and (actual / ".git").exists()


def test_a_checkout_that_cannot_be_created_is_reported_with_the_reason(tmp_path: Path) -> None:
    bare = _bare_repo(tmp_path)
    (tmp_path / "a-file").write_text("")
    cfg = repo_config(git_dir=str(bare), scip_checkout_dir=str(tmp_path / "a-file" / "checkout"))

    _actual, _bare, missing = resolve_checkout(cfg, "python")

    assert missing is not None and missing.startswith("checkout missing at ")
    assert "(mkdir failed)" in missing  # made before `git init` runs


@pytest.mark.parametrize("producer", ["python", "clang"])
def test_an_existing_empty_directory_becomes_a_checkout(tmp_path: Path, producer: str) -> None:
    bare = _bare_repo(tmp_path)
    checkout = tmp_path / "checkout"
    checkout.mkdir()
    cfg = repo_config(git_dir=str(bare), scip_checkout_dir=str(checkout), scip_producer=producer)

    actual, actual_bare, missing = resolve_checkout(cfg, producer)

    assert missing is None
    assert actual == checkout and actual_bare == bare.resolve()
    assert (checkout / ".git").is_dir()


@pytest.mark.parametrize("kind", ["file", "nonempty", "bare", "nested"])
def test_an_invalid_existing_checkout_is_refused_without_changing_it(
    tmp_path: Path, kind: str
) -> None:
    bare = _bare_repo(tmp_path)
    checkout = tmp_path / "checkout"
    if kind == "bare":
        checkout = bare
    elif kind == "file":
        checkout.write_text("keep this file\n")
    else:
        checkout.mkdir()
        (checkout / "keep.txt").write_text("keep this content\n")
        if kind == "nested":
            subprocess.run(["git", "init", "-q", str(tmp_path)], check=True)
    cfg = repo_config(git_dir=str(bare), scip_checkout_dir=str(checkout), scip_producer="python")
    before = {p.relative_to(tmp_path): p.read_bytes() for p in tmp_path.rglob("*") if p.is_file()}

    _actual, _bare, missing = resolve_checkout(cfg, "python")

    assert missing is not None and "checkout" in missing
    after = {p.relative_to(tmp_path): p.read_bytes() for p in tmp_path.rglob("*") if p.is_file()}
    assert after == before


@pytest.mark.parametrize("linked", [False, True])
def test_git_checkout_metadata_must_stay_inside_the_checkout(tmp_path: Path, linked: bool) -> None:
    bare = _bare_repo(tmp_path)
    checkout = tmp_path / "checkout"
    if linked:
        subprocess.run(
            ["git", "--git-dir", str(bare), "worktree", "add", "--detach", str(checkout)],
            check=True,
            capture_output=True,
        )
    else:
        subprocess.run(["git", "clone", "-q", str(bare), str(checkout)], check=True)
    cfg = repo_config(git_dir=str(bare), scip_checkout_dir=str(checkout), scip_producer="python")
    before = (checkout / "main.py").read_bytes()
    metadata = (checkout / ".git").read_bytes() if linked else None

    actual, actual_bare, missing = resolve_checkout(cfg, "python")

    if linked:
        assert missing is not None and "standalone .git directory" in missing
        assert (checkout / ".git").read_bytes() == metadata
    else:
        assert missing is None and actual == checkout and actual_bare == bare.resolve()
    assert (checkout / "main.py").read_bytes() == before


@pytest.mark.parametrize("shared", [False, True], ids=["symlink", "commondir"])
def test_external_git_metadata_is_refused_without_changing_it(tmp_path: Path, shared: bool) -> None:
    bare = _bare_repo(tmp_path)
    checkout = tmp_path / "checkout"
    subprocess.run(["git", "clone", "-q", str(bare), str(checkout)], check=True)
    if shared:
        metadata = checkout / ".git"
        (metadata / "commondir").write_text(str(bare) + "\n")
    else:
        metadata = tmp_path / "external-metadata"
        (checkout / ".git").rename(metadata)
        (checkout / ".git").symlink_to(metadata, target_is_directory=True)
    cfg = repo_config(git_dir=str(bare), scip_checkout_dir=str(checkout), scip_producer="python")
    before = {p.relative_to(metadata): p.read_bytes() for p in metadata.rglob("*") if p.is_file()}

    _actual, _bare, missing = resolve_checkout(cfg, "python")

    assert missing is not None and "standalone .git directory" in missing
    assert (checkout / ".git").is_symlink() is not shared
    assert (checkout / "main.py").read_text() == "print('ok')\n"
    assert {
        p.relative_to(metadata): p.read_bytes() for p in metadata.rglob("*") if p.is_file()
    } == before


@pytest.mark.parametrize("local_metadata", [False, True], ids=["missing-local", "invalid-local"])
def test_ancestor_worktree_configuration_never_supplies_checkout_metadata(
    tmp_path: Path, local_metadata: bool
) -> None:
    from server.code.app.index.gitrepo import auth_env

    bare = _bare_repo(tmp_path)
    ancestor = tmp_path / "ancestor"
    subprocess.run(["git", "init", "-q", str(ancestor)], check=True)
    checkout = ancestor / "checkout"
    checkout.mkdir()
    (checkout / "keep.py").write_text("keep = True\n")
    subprocess.run(
        ["git", "-C", str(ancestor), "config", "core.worktree", str(checkout)], check=True
    )
    if local_metadata:
        (checkout / ".git").mkdir()
    else:
        probe = subprocess.run(
            ["git", "-C", str(checkout), "rev-parse", "--show-toplevel"],
            env=auth_env(),
            capture_output=True,
            text=True,
            check=True,
        )
        assert Path(probe.stdout.removesuffix("\n")).resolve() == checkout.resolve()
    cfg = repo_config(git_dir=str(bare), scip_checkout_dir=str(checkout), scip_producer="python")
    before = {p.relative_to(ancestor): p.read_bytes() for p in ancestor.rglob("*") if p.is_file()}

    _actual, _bare, missing = resolve_checkout(cfg, "python")

    assert missing is not None
    assert {
        p.relative_to(ancestor): p.read_bytes() for p in ancestor.rglob("*") if p.is_file()
    } == before


@pytest.mark.parametrize("producer", ["python", "go"])
@pytest.mark.parametrize("shared", [False, True], ids=["symlink", "commondir"])
def test_worker_checkout_refusal_prevents_gc_and_preserves_metadata(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, producer: str, shared: bool
) -> None:
    import importlib
    from unittest.mock import AsyncMock

    from server.code.app.index import compiler_tier, mirrors
    from server.code.domain.index import repos
    from server.code.store import index
    from server.settings import settings
    from server.workers import pass_context

    worker_module = "scip_py_delta" if producer == "python" else "scip_go_delta"
    worker = importlib.import_module(f"server.code.workers.{worker_module}")
    bare = _bare_repo(tmp_path)
    checkout = tmp_path / "checkout"
    subprocess.run(["git", "clone", "-q", str(bare), str(checkout)], check=True)
    if shared:
        metadata = checkout / ".git"
        (metadata / "commondir").write_text(str(bare) + "\n")
    else:
        metadata = tmp_path / "external-metadata"
        (checkout / ".git").rename(metadata)
        (checkout / ".git").symlink_to(metadata, target_is_directory=True)
    sentinel = metadata / "keep.txt"
    sentinel.write_bytes(b"keep metadata content\n")
    before = {p.relative_to(tmp_path): p.read_bytes() for p in tmp_path.rglob("*") if p.is_file()}
    cfg = repo_config(git_dir=str(bare), scip_checkout_dir=str(checkout), scip_producer=producer)
    repo = AsyncMock()
    repo.get_state.side_effect = lambda _org, _ns, key: "a" * 40 if key == "git_head" else ""
    monkeypatch.setattr(index, "CodeRepo", lambda: repo)
    monkeypatch.setattr(repos, "configured_repos", lambda: [cfg])
    monkeypatch.setattr(compiler_tier, "worker_owns", AsyncMock(return_value=True))
    monkeypatch.setattr(settings, "code_mirror_gc_interval_h", 1.0)
    # Exercise the real marker path while forbidding a repack on the test host.
    monkeypatch.setattr(settings, "code_mirror_gc_margin_gb", 1e9)
    original_gc = mirrors.gc_if_due
    gc_calls: list[str] = []

    async def observe_gc(git_dir: str) -> None:
        gc_calls.append(git_dir)
        await original_gc(git_dir)

    monkeypatch.setattr(mirrors, "gc_if_due", observe_gc)
    pass_context.begin("checkout-refusal")
    try:
        result = asyncio.run(worker.run_pass())
    finally:
        pass_context.clear()

    assert result.errors == [] and result.deltas_applied == 0
    assert len(result.skipped) == 1 and "standalone .git directory" in result.skipped[0]
    after = {p.relative_to(tmp_path): p.read_bytes() for p in tmp_path.rglob("*") if p.is_file()}
    assert (gc_calls, (metadata / "loom-gc-at").exists(), after == before) == ([], False, True)
    assert sentinel.read_bytes() == b"keep metadata content\n"
    repo.set_state.assert_not_awaited()


@pytest.mark.parametrize("layout", ["missing", "empty", "standalone"])
def test_checkout_gc_never_initializes_a_directory(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, layout: str
) -> None:
    from unittest.mock import AsyncMock

    from server.code.app.index import compiler_tier, mirrors

    bare = _bare_repo(tmp_path)
    checkout = tmp_path / "checkout"
    if layout == "standalone":
        subprocess.run(["git", "clone", "-q", str(bare), str(checkout)], check=True)
    elif layout == "empty":
        checkout.mkdir()
    cfg = repo_config(git_dir=str(bare), scip_checkout_dir=str(checkout), scip_producer="python")
    gc = AsyncMock()
    monkeypatch.setattr(mirrors, "gc_if_due", gc)

    asyncio.run(compiler_tier._gc_checkout(cfg, "python"))

    if layout == "standalone":
        gc.assert_awaited_once_with(str(checkout / ".git"))
    else:
        gc.assert_not_awaited()
        assert not (checkout / ".git").exists()
        assert checkout.exists() is (layout == "empty")


@pytest.mark.parametrize("existing", [False, True])
def test_checkout_git_commands_have_a_timeout_and_report_expiry(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, existing: bool
) -> None:
    from server.settings import settings

    bare = _bare_repo(tmp_path)
    checkout = tmp_path / "checkout"
    if existing:
        subprocess.run(["git", "clone", "-q", str(bare), str(checkout)], check=True)
    cfg = repo_config(git_dir=str(bare), scip_checkout_dir=str(checkout), scip_producer="python")
    seen: list[float] = []

    def timed_out(argv, **kwargs):
        seen.append(kwargs["timeout"])
        raise subprocess.TimeoutExpired(argv, kwargs["timeout"])

    monkeypatch.setattr("server.code.app.index.compiler_tier.subprocess.run", timed_out)

    _actual, _bare, missing = resolve_checkout(cfg, "python")

    assert missing is not None and "timed out" in missing
    assert seen == [settings.code_git_timeout_s]


def test_checkout_os_errors_log_details_without_retaining_them(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    bare = _bare_repo(tmp_path)
    checkout = tmp_path / "checkout"
    checkout.mkdir()
    cfg = repo_config(git_dir=str(bare), scip_checkout_dir=str(checkout), scip_producer="python")

    def unavailable(_checkout: Path) -> None:
        raise OSError("backend-private-detail")

    monkeypatch.setattr("server.code.app.index.compiler_tier._checkout_metadata_error", unavailable)

    _actual, _bare, missing = resolve_checkout(cfg, "python")

    assert missing is not None
    assert "backend-private-detail" not in missing
    assert "OSError" in missing
    assert "backend-private-detail" in caplog.text


@pytest.mark.asyncio
async def test_no_git_command_in_a_checkout_starts_a_repack(tmp_path: Path, monkeypatch) -> None:
    """Auto-gc inside a fetch writes a pack as big as the repository; every git
    command a tier runs turns it off, in trees made by any build."""
    from server.code.app.index import compiler_tier
    from server.code.app.index.sandbox import JobResult

    seen: list[list[str]] = []

    async def _job(argv, **kw):
        seen.append(argv)
        return JobResult(0, b"", b"")

    monkeypatch.setattr(compiler_tier, "run_job", _job)
    await compiler_tier.git(tmp_path, "fetch", "origin")
    assert seen[0][3:7] == ["-c", "gc.auto=0", "-c", "maintenance.auto=false"]
