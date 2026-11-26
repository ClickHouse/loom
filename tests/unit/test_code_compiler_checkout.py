"""Compiler checkout initialization and metadata refusal through worker housekeeping."""

from __future__ import annotations

import asyncio
import subprocess
from pathlib import Path

import pytest

from server.code.app.index.compiler_tier import resolve_checkout
from tests.unit.code_fixtures import repo_config
from tests.unit.git_repo import git


@pytest.fixture(autouse=True)
def _sharing_on(monkeypatch: pytest.MonkeyPatch) -> None:
    """Every test here but the rollout's runs as a deployment that turned sharing on."""
    from server.settings import settings

    monkeypatch.setattr(settings, "code_compiler_share_mirror_packs", True)


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


def _commit(source: Path, name: str) -> None:
    (source / name).write_text(f"print({name!r})\n")
    git(source, "add", name)
    git(source, "commit", "-qm", name)


def _packed_bare(tmp_path: Path, *, packed: bool = True) -> Path:
    """A mirror as Loom clones one, its objects in a pack (or, not `packed`, loose)."""
    source = tmp_path / "source"
    source.mkdir(parents=True)
    git(source, "init", "-q")
    _commit(source, "main.py")
    bare = tmp_path / "repo.git"
    local = "--no-local" if packed else "--local"
    git(tmp_path, "clone", "-q", "--bare", local, "source", "repo.git")
    return bare


def _mirror_gets_a_commit_and_gc(tmp_path: Path, name: str) -> None:
    """The mirror fetches a new commit and its gc replaces every pack with a new one."""
    _commit(tmp_path / "source", name)
    bare = tmp_path / "repo.git"
    git(bare, "fetch", "-q", str(tmp_path / "source"), f"+HEAD:refs/heads/{name}")
    git(bare, "-c", "gc.auto=0", "gc", "-q", "--no-prune")


def _packs(git_dir: Path) -> set[tuple[str, int]]:
    """(name, inode) of each pack: equal sets mean the same files on disk."""
    return {(p.name, p.stat().st_ino) for p in (git_dir / "objects" / "pack").glob("*.pack")}


def _own_objects(checkout: Path) -> list[Path]:
    """The checkout's object files no other directory links: what it costs on disk."""
    objects = checkout / ".git" / "objects"
    files = [p for p in objects.rglob("*") if p.is_file() and p.parent.name != "info"]
    return [p for p in files if p.suffix != ".keep" and p.stat().st_nlink == 1]


def _left_behind(checkout: Path) -> list[str]:
    """Pack files git cannot use (no `.idx`) and link temporaries."""
    pack_dir = checkout / ".git" / "objects" / "pack"
    return sorted(
        p.name
        for p in pack_dir.iterdir()
        if p.name.startswith(".loom-link-")
        or (p.suffix in (".pack", ".rev", ".keep") and not p.with_suffix(".idx").exists())
    )


def _head(repo: Path) -> str:
    return git(repo, "rev-parse", "HEAD").strip()


def _fetch_and_check_out(checkout: Path, bare: Path) -> str:
    from server.code.app.index.compiler_tier import checkout_fetch_args
    from server.code.app.index.gitrepo import auth_env

    target = _head(bare)
    env = auth_env(remote=str(bare))
    for args in (checkout_fetch_args(bare, target), ["checkout", "-q", "--force", target]):
        subprocess.run(["git", "-C", str(checkout), *args], env=env, check=True)
    return target


def _gc_when_due(monkeypatch: pytest.MonkeyPatch) -> None:
    """The checkout gc on its weekly interval, with no free-space margin on the test host."""
    from server.settings import settings

    monkeypatch.setattr(settings, "code_mirror_gc_interval_h", 168.0)
    monkeypatch.setattr(settings, "code_mirror_gc_margin_gb", 0.0)


def _previous_build_pass(cfg: object, producer: str, bare: Path) -> str:
    """A tier pass of the version before shared packs: it initializes only a missing or
    empty `checkout_for` path, with a plain `git init`; its fetch mounts the mirror, its
    `checkout --force` only the checkout (the mirror is out of its sight); then its
    checkout gc, the same `gc_if_due` command."""
    from server.code.app.index import mirrors
    from server.code.app.index.compiler_tier import checkout_fetch_args
    from server.code.app.index.gitrepo import auth_env
    from server.code.domain.index.repos import checkout_for

    checkout = Path(checkout_for(cfg, producer))  # type: ignore[arg-type]
    if not checkout.exists() or not any(checkout.iterdir()):
        git(checkout.parent, "init", "-q", checkout.name)
    target = _head(bare)
    env = auth_env(remote=str(bare))
    subprocess.run(
        ["git", "-C", str(checkout), *checkout_fetch_args(bare, target)], env=env, check=True
    )
    hidden = bare.rename(bare.with_name(f"{bare.name}.unmounted"))
    try:
        git(checkout, "checkout", "-q", "--force", target)
    finally:
        hidden.rename(bare)
    (checkout / ".git" / mirrors._GC_MARK).unlink(missing_ok=True)
    asyncio.run(mirrors.gc_if_due(str(checkout / ".git")))
    return target


def _tier_pass(cfg: object) -> str | None:
    """What a tier pass with a new commit does to its checkout: resolve it, fetch and
    check out the mirror's head, then the checkout gc."""
    from server.code.app.index import compiler_tier

    checkout, bare, missing = resolve_checkout(cfg, "python")  # type: ignore[arg-type]
    _fetch_and_check_out(checkout, bare)
    asyncio.run(compiler_tier._gc_checkout(cfg, "python"))  # type: ignore[arg-type]
    return missing


def test_a_new_checkout_shares_the_mirrors_packs(tmp_path: Path) -> None:
    """The tier's fetch finds every object already there: the checkout's packs are the
    mirror's files, and it holds no object of its own."""
    bare = _packed_bare(tmp_path)
    cfg = repo_config(git_dir=str(bare), scip_checkout_dir=str(tmp_path / "co"))

    checkout, _bare, missing = resolve_checkout(cfg, "python")
    _fetch_and_check_out(checkout, bare)

    assert missing is None and checkout == tmp_path / "co.python"
    assert _packs(checkout / ".git") == _packs(bare) != set()
    assert _own_objects(checkout) == []
    assert (checkout / "main.py").read_text() == "print('main.py')\n"


def test_a_full_copy_from_an_older_version_is_reclaimed_in_place(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A checkout an older version filled with its own copy and gc'd a moment ago, at
    the same path: the next pass shares the mirror's packs and its gc, due again, drops
    the copy; HEAD and the files stay."""
    _gc_when_due(monkeypatch)
    bare = _packed_bare(tmp_path)
    cfg = repo_config(git_dir=str(bare), scip_checkout_dir=str(tmp_path / "co"))
    target = _previous_build_pass(cfg, "python", bare)
    checkout = tmp_path / "co.python"
    (checkout / "build-cache").write_text("kept\n")
    assert _own_objects(checkout) != []

    missing = _tier_pass(cfg)

    assert missing is None
    assert _packs(checkout / ".git") == _packs(bare) and _own_objects(checkout) == []
    assert _head(checkout) == target
    assert (checkout / "build-cache").read_text() == "kept\n"
    assert git(checkout, "status", "--porcelain", "--untracked-files=no") == ""


def test_the_tier_hands_off_between_versions_on_one_repos_volume(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A mixed-version rollout moves the tier lease new → previous → new over one
    checkout with sharing at its default, off: no checkout file is a mirror pack, so
    the previous version's git jobs, which mount the checkout writable, cannot write
    the mirror. Once the previous version is gone and sharing is on, the next pass
    shares the mirror's packs."""
    from server.code.app.index.compiler_tier import shared_packs
    from server.settings import settings

    monkeypatch.setattr(settings, "code_compiler_share_mirror_packs", False)
    _gc_when_due(monkeypatch)
    bare = _packed_bare(tmp_path)
    cfg = repo_config(git_dir=str(bare), scip_checkout_dir=str(tmp_path / "co"))
    assert _tier_pass(cfg) is None
    checkout = tmp_path / "co.python"
    assert shared_packs(checkout) == []

    assert _previous_build_pass(cfg, "python", bare) == _head(bare)
    assert shared_packs(checkout) == []

    assert _tier_pass(cfg) is None and _head(checkout) == _head(bare)
    assert shared_packs(checkout) == []

    monkeypatch.setattr(settings, "code_compiler_share_mirror_packs", True)
    assert _tier_pass(cfg) is None and _head(checkout) == _head(bare)
    assert _packs(checkout / ".git") == _packs(bare) and _own_objects(checkout) == []


def test_an_operators_keep_stays_through_a_mirror_rotation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An operator kept a pack the checkout holds under the mirror pack's name: linking
    it leaves that `.keep` as it is, and once the mirror's gc rotates the pack out the
    pass still leaves it, so no checkout gc repacks it."""
    import shutil

    _gc_when_due(monkeypatch)
    bare = _packed_bare(tmp_path)
    cfg = repo_config(git_dir=str(bare), scip_checkout_dir=str(tmp_path / "co"))
    checkout = tmp_path / "co.python"
    git(tmp_path, "init", "-q", checkout.name)
    pack_dir = checkout / ".git" / "objects" / "pack"
    for src in (bare / "objects" / "pack").glob("pack-*"):
        shutil.copy(src, pack_dir / src.name)
    (stem,) = {p.stem for p in pack_dir.glob("pack-*.pack")}
    keep = pack_dir / f"{stem}.keep"
    keep.write_text("pinned by the operator\n")

    assert _tier_pass(cfg) is None
    assert keep.read_text() == "pinned by the operator\n"
    assert (pack_dir / f"{stem}.pack").stat().st_nlink > 1

    _mirror_gets_a_commit_and_gc(tmp_path, "next.py")
    assert _tier_pass(cfg) is None

    assert keep.read_text() == "pinned by the operator\n"
    assert (pack_dir / f"{stem}.pack").exists()


@pytest.mark.parametrize("mirror", ["repacked", "cloned-again"])
def test_a_changed_mirror_is_shared_again_and_the_checkout_keeps_its_objects(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, mirror: str
) -> None:
    """The mirror's gc writes a new pack, or the mirror is cloned again without the
    commit the checkout is at: the next pass shares the mirror's current packs, and its
    gc moves any object only the old pack held into the checkout's own pack."""
    import shutil

    _gc_when_due(monkeypatch)
    bare = _packed_bare(tmp_path)
    cfg = repo_config(git_dir=str(bare), scip_checkout_dir=str(tmp_path / "co"))
    _tier_pass(cfg)
    checkout = tmp_path / "co.python"
    old = _head(checkout)
    if mirror == "repacked":
        _mirror_gets_a_commit_and_gc(tmp_path, "next.py")
    else:
        shutil.rmtree(bare)
        other = _packed_bare(tmp_path / "other").parent / "source"
        git(other, "commit", "--amend", "-qm", "o")
        git(tmp_path, "clone", "-q", "--bare", "--no-local", str(other), str(bare))
        assert subprocess.run(["git", "--git-dir", str(bare), "cat-file", "-e", old]).returncode
    assert _packs(checkout / ".git") != _packs(bare)

    missing = _tier_pass(cfg)

    assert missing is None and _packs(bare) <= _packs(checkout / ".git")
    assert git(checkout, "cat-file", "-t", f"{old}^{{tree}}").strip() == "tree"
    shared = {p.name for p in (checkout / ".git" / "objects" / "pack").glob("*.keep")}
    assert shared == {name.replace(".pack", ".keep") for name, _ino in _packs(bare)}
    if mirror == "repacked":
        assert _packs(checkout / ".git") == _packs(bare) and _own_objects(checkout) == []


@pytest.mark.parametrize("layout", ["cross-device", "no-mirror-pack"])
def test_a_checkout_that_cannot_share_keeps_one_copy_and_no_pass_rewrites_it(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    layout: str,
) -> None:
    """A hard link across filesystems fails, or the mirror has no pack to share: the tier
    works as before with its own copy, and a second pass leaves that copy's files as
    they are, the checkout gc waiting for its interval."""
    import errno
    import os

    _gc_when_due(monkeypatch)
    bare = _packed_bare(tmp_path, packed=layout != "no-mirror-pack")
    cfg = repo_config(git_dir=str(bare), scip_checkout_dir=str(tmp_path / "co"))

    def cross_device(_src: object, _dst: object) -> None:
        raise OSError(errno.EXDEV, "Invalid cross-device link")

    if layout == "cross-device":
        monkeypatch.setattr(os, "link", cross_device)
    assert _tier_pass(cfg) is None
    checkout = tmp_path / "co.python"
    first = _packs(checkout / ".git")

    assert _tier_pass(cfg) is None

    assert _packs(checkout / ".git") == first != set() and (checkout / "main.py").exists()
    pack_dir = checkout / ".git" / "objects" / "pack"
    assert not [p for p in pack_dir.iterdir() if p.suffix == ".keep" or p.name.startswith(".")]
    assert ("cross-device" in caplog.text) is (layout == "cross-device")


def test_a_mirror_gc_while_links_fail_keeps_the_old_pack_shared(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The mirror's gc replaced its pack and the new one cannot be linked this pass: the
    old pack stays kept, so no gc copies it out; the next pass shares the new pack and
    its gc drops the old one."""
    import errno
    import os

    _gc_when_due(monkeypatch)
    bare = _packed_bare(tmp_path)
    cfg = repo_config(git_dir=str(bare), scip_checkout_dir=str(tmp_path / "co"))
    _tier_pass(cfg)
    checkout = tmp_path / "co.python"
    before = _packs(checkout / ".git")
    _mirror_gets_a_commit_and_gc(tmp_path, "next.py")

    def refused(_src: object, _dst: object) -> None:
        raise OSError(errno.EPERM, "Operation not permitted")

    with monkeypatch.context() as m:
        m.setattr(os, "link", refused)
        assert _tier_pass(cfg) is None
    pack_dir = checkout / ".git" / "objects" / "pack"
    assert _packs(checkout / ".git") == before
    assert {p.name for p in pack_dir.glob("*.keep")} == {
        name.replace(".pack", ".keep") for name, _ino in before
    }

    assert _tier_pass(cfg) is None
    assert _packs(checkout / ".git") == _packs(bare) and _own_objects(checkout) == []


@pytest.mark.parametrize("interrupted", ["killed", "raced"])
def test_an_interrupted_link_leaves_no_pack_without_its_index(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, interrupted: str
) -> None:
    """A pass killed after linking a `.pack` whose mirror pack then went, or a link that
    loses the `.idx` to the mirror's gc mid-way: no pack file git cannot use stays to
    hold a dead mirror pack's blocks."""
    import os

    _gc_when_due(monkeypatch)
    bare = _packed_bare(tmp_path)
    cfg = repo_config(git_dir=str(bare), scip_checkout_dir=str(tmp_path / "co"))
    _tier_pass(cfg)
    checkout = tmp_path / "co.python"
    pack_dir = checkout / ".git" / "objects" / "pack"
    _mirror_gets_a_commit_and_gc(tmp_path, "next.py")
    if interrupted == "killed":
        from server.code.app.index.compiler_tier import SHARED_PACK_KEEP

        (stem,) = {p.stem for p in (bare / "objects" / "pack").glob("*.pack")}
        (pack_dir / f"{stem}.keep").write_text(SHARED_PACK_KEEP)
        os.link(bare / "objects" / "pack" / f"{stem}.pack", pack_dir / f"{stem}.pack")
        _mirror_gets_a_commit_and_gc(tmp_path, "last.py")
        assert _left_behind(checkout) != []
        _tier_pass(cfg)
    else:
        link = os.link

        def idx_gone(src: str | Path, dst: str | Path) -> None:
            if str(src).endswith(".idx"):
                raise FileNotFoundError(src)
            link(src, dst)

        with monkeypatch.context() as m:
            m.setattr(os, "link", idx_gone)
            resolve_checkout(cfg, "python")

    assert _left_behind(checkout) == []
    _tier_pass(cfg)
    assert _packs(checkout / ".git") == _packs(bare) and _own_objects(checkout) == []


def test_a_mirror_pack_gone_at_link_time_keeps_the_stale_pack_shared(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The mirror's gc replaced the shared pack, and the new one is gone by the time the
    pass links it: the old pack may hold what only that one held, so it stays kept and
    no gc is brought forward to copy it out."""
    import os

    from server.code.app.index import mirrors

    _gc_when_due(monkeypatch)
    bare = _packed_bare(tmp_path)
    cfg = repo_config(git_dir=str(bare), scip_checkout_dir=str(tmp_path / "co"))
    _tier_pass(cfg)
    checkout = tmp_path / "co.python"
    keeps = {p.name for p in (checkout / ".git" / "objects" / "pack").glob("*.keep")}
    _mirror_gets_a_commit_and_gc(tmp_path, "next.py")

    def gone(src: str | Path, _dst: str | Path) -> None:
        raise FileNotFoundError(src)

    with monkeypatch.context() as m:
        m.setattr(os, "link", gone)
        resolve_checkout(cfg, "python")

    pack_dir = checkout / ".git" / "objects" / "pack"
    assert {p.name for p in pack_dir.glob("*.keep")} == keeps != set()
    assert (checkout / ".git" / mirrors._GC_MARK).exists()


def test_a_pack_git_is_still_writing_is_left_alone(tmp_path: Path) -> None:
    """Git installs a pack's `.pack` before its `.idx`: an own pack without its `.idx`
    may be one another git process is writing, so the pass leaves it."""
    bare = _packed_bare(tmp_path)
    cfg = repo_config(git_dir=str(bare), scip_checkout_dir=str(tmp_path / "co"))
    checkout, _bare, _missing = resolve_checkout(cfg, "python")
    writing = checkout / ".git" / "objects" / "pack" / f"pack-{'0' * 40}.pack"
    writing.write_bytes(b"PACK")

    resolve_checkout(cfg, "python")

    assert writing.read_bytes() == b"PACK"


@pytest.mark.asyncio
async def test_a_job_that_removes_a_keep_still_gets_the_shared_pack_read_only(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A git job can rewrite or remove the `.keep` files: the next job still mounts every
    file the checkout shares with the mirror read-only, chosen by its link count."""
    from server.code.app.index import compiler_tier
    from server.code.app.index.sandbox import JobResult

    bare = _packed_bare(tmp_path)
    cfg = repo_config(git_dir=str(bare), scip_checkout_dir=str(tmp_path / "co"))
    checkout, _bare, _missing = resolve_checkout(cfg, "python")
    pack_dir = checkout / ".git" / "objects" / "pack"
    linked = {p.name for p in pack_dir.iterdir() if p.suffix in (".pack", ".rev", ".idx")}
    for keep in pack_dir.glob("*.keep"):
        keep.unlink()
    seen: list[list[Path]] = []

    async def _job(argv, **kw):
        seen.append(kw["readonly"])
        return JobResult(0, b"", b"")

    monkeypatch.setattr(compiler_tier, "run_job", _job)
    await compiler_tier.git(checkout, "checkout", "--force", "HEAD")

    assert {p.name for p in seen[0]} == linked != set()


def test_the_checkout_gc_needs_no_room_for_the_packs_it_keeps(tmp_path: Path) -> None:
    """No gc rewrites a kept pack, so the gc's free-space test leaves the shared packs
    out of the size it needs room for."""
    from server.code.app.index import mirrors

    bare = _packed_bare(tmp_path)
    cfg = repo_config(git_dir=str(bare), scip_checkout_dir=str(tmp_path / "co"))
    checkout, _bare, _missing = resolve_checkout(cfg, "python")
    git_dir = checkout / ".git"
    pack = next((git_dir / "objects" / "pack").glob("*.pack"))
    kept = mirrors._tree_bytes(str(git_dir))

    for keep in pack.parent.glob("*.keep"):
        keep.unlink()

    assert mirrors._tree_bytes(str(git_dir)) - kept > pack.stat().st_size
