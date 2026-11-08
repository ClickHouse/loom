"""Files the host reads from a repository's checkout or from a sandboxed job's output
directory: a symlink, a FIFO or an oversize file is refused without being read, so a
committed `package.json -> /dev/zero` or a harness that links its delta to a host file
fails that entry's pass instead of exhausting the worker or leaking the file."""

from __future__ import annotations

import asyncio
import contextlib
import json
import os
import subprocess
from collections.abc import Iterator
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, Mock, patch

import pytest

from server.code.app.index import scip_full, untrusted_files
from server.code.app.index import typescript_dependencies as deps
from server.code.app.index.scip_delta import ScipApplyStats, ScipDelta, parse_delta
from server.code.app.index.untrusted_files import FileRefused, read_bounded, read_delta
from server.code.domain.index.tier_health import STATE_PREFIX
from server.code.workers import scip_delta, scip_go_delta, scip_py_delta, scip_ts_delta
from server.settings import settings
from tests.unit.code_fixtures import repo_config

_DELTA = {"delta_version": 1, "commit": "b" * 40, "files": []}


def _watermark_unmoved(repo: Any) -> None:
    """No state write but the failed pass's tier-health record: the watermark stays."""
    keys = [c.args[2] for c in repo.set_state.await_args_list]
    assert keys and all(k.startswith(STATE_PREFIX) for k in keys), keys


def _secret(tmp_path: Path) -> Path:
    secret = tmp_path / "outside" / ".env"
    secret.parent.mkdir(exist_ok=True)
    secret.write_text("LOOM_JWT_SECRET=hunter2\n")
    return secret


# --- the helper ---


def test_a_regular_file_reads_whole(tmp_path: Path) -> None:
    path = tmp_path / "a.json"
    path.write_bytes(b'{"a": 1}')
    assert read_bounded(path, 8) == b'{"a": 1}'


@pytest.mark.parametrize("target", ["/dev/zero", "secret"])
def test_a_symlink_is_refused(tmp_path: Path, target: str) -> None:
    link = tmp_path / "a.json"
    link.symlink_to(_secret(tmp_path) if target == "secret" else target)
    with pytest.raises(FileRefused, match="symlink"):
        read_bounded(link, 1024)


def test_a_device_or_fifo_is_refused(tmp_path: Path) -> None:
    fifo = tmp_path / "fifo"
    os.mkfifo(fifo)
    with pytest.raises(FileRefused, match="not a regular file"):
        read_bounded(fifo, 1024)  # no writer: a blocking open would hang here
    with pytest.raises(FileRefused, match="not a regular file"):
        read_bounded(Path("/dev/zero"), 1024)


def test_a_directory_is_refused(tmp_path: Path) -> None:
    (tmp_path / "delta.json").mkdir()
    with pytest.raises(FileRefused, match="not a regular file"):
        read_bounded(tmp_path / "delta.json", 1024)


def test_an_oversize_file_is_refused(tmp_path: Path) -> None:
    path = tmp_path / "big.json"
    path.write_bytes(b"x" * 9)
    with pytest.raises(FileRefused, match="9 bytes; max 8"):
        read_bounded(path, 8)


def test_a_missing_file_is_an_ordinary_os_error(tmp_path: Path) -> None:
    with pytest.raises(FileNotFoundError):
        read_bounded(tmp_path / "nope", 8)


@pytest.mark.parametrize("stated", [5, 15])
def test_a_file_that_changed_after_fstat_is_refused(tmp_path: Path, monkeypatch, stated) -> None:
    path = tmp_path / "a.json"
    path.write_bytes(b"x" * 10)
    real = os.fstat

    def fstat(fd: int) -> Any:
        return SimpleNamespace(st_mode=real(fd).st_mode, st_size=stated)

    monkeypatch.setattr(untrusted_files.os, "fstat", fstat)
    with pytest.raises(FileRefused, match="changed while it was read"):
        read_bounded(path, 1024)


def test_the_delta_cap_is_its_setting(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(settings, "code_scip_delta_file_max_mb", 1)
    path = tmp_path / "delta.json"
    path.write_bytes(b"x" * 1024 * 1024)
    assert len(read_delta(path)) == 1024 * 1024
    path.write_bytes(b"x" * (1024 * 1024 + 1))
    with pytest.raises(FileRefused):
        read_delta(path)


def _minimal_files(n: int) -> bytes:
    return b'{"commit":"c","files":[' + b",".join([b'{"path":""}'] * n) + b"]}"


def _realistic_file() -> dict[str, Any]:
    symbol = {
        "qualified_name": "DB::MergeTreeData::loadDataParts",
        "name": "loadDataParts",
        "kind": "function",
        # commas inside strings count too
        "signature": "void loadDataParts(bool skip_sanity_checks, std::map<String, DataPartPtr> & parts)",
        "line_start": 1200,
        "line_end": 1380,
        "scip_symbol": "cxx . . $ DB/MergeTreeData#loadDataParts(abcd1234).",
        "decl_paths": ["src/Storages/MergeTree/MergeTreeData.h"],
    }
    edge = {
        "edge_kind": "calls",
        "from_qualified_name": "DB::MergeTreeData::loadDataParts",
        "to_qualified_name": "DB::IMergeTreeDataPart::loadColumnsChecksumsIndexes",
        "line": 1234,
    }
    return {
        "path": "src/Storages/MergeTree/MergeTreeData.cpp",
        "symbols": [symbol] * 40,
        "edges": [edge] * 200,
    }


def _nodes(data: bytes) -> int:
    return data.count(b"{") + data.count(b"[") + data.count(b",")


def test_a_delta_over_the_node_cap_is_refused(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(settings, "code_scip_delta_max_nodes", 10)
    path = tmp_path / "delta.json"
    path.write_bytes(_minimal_files(4))  # 2 * 4 + 2 values
    assert read_delta(path) == _minimal_files(4)
    path.write_bytes(_minimal_files(5))
    with pytest.raises(
        FileRefused, match="12 `{`/`\\[`/`,` bytes; max 10 .code_scip_delta_max_nodes"
    ):
        read_delta(path)


def test_a_realistic_delta_meets_both_caps_near_the_byte_cap(tmp_path: Path) -> None:
    # About 1,480 counted bytes in a 47 KB file, one per about 32 bytes: a delta of these
    # files at the byte cap stays under the node cap, and reaches 95% of it
    unit = json.dumps(_realistic_file(), separators=(",", ":")).encode()
    files = settings.code_scip_delta_file_max_mb * 1024 * 1024 // (len(unit) + 1)
    counted = files * (_nodes(unit) + 1)
    assert 0.95 * settings.code_scip_delta_max_nodes < counted <= settings.code_scip_delta_max_nodes
    path = tmp_path / "delta.json"
    path.write_text(json.dumps({"commit": "c", "files": [_realistic_file()] * 3}))
    assert ScipDelta.model_validate_json(read_delta(path)).files[0].edges


def test_an_invalid_delta_raises_a_short_error() -> None:
    # pydantic names every bad item: 1,000,000 bad files print about 170 million characters
    raw = b'{"commit":"c","files":[' + b",".join([b"0"] * 100_000) + b"]}"
    with pytest.raises(ValueError) as caught:
        parse_delta(raw)
    assert str(caught.value) == "delta invalid: 100000 errors"
    assert caught.value.__cause__ is None and caught.value.__suppress_context__


def test_a_zero_delta_cap_is_refused_at_load() -> None:
    from pydantic import ValidationError

    from server.settings import Settings

    with pytest.raises(ValidationError, match="code_scip_delta_file_max_mb"):
        Settings(_env_file=None, code_scip_delta_file_max_mb=0)  # type: ignore[call-arg]
    with pytest.raises(ValidationError, match="code_scip_delta_max_nodes"):
        Settings(_env_file=None, code_scip_delta_max_nodes=0)  # type: ignore[call-arg]


# --- the TypeScript checkout's manifests ---


@pytest.fixture
def checkout(tmp_path: Path) -> Path:
    root = tmp_path / "checkout"
    root.mkdir()
    subprocess.run(["git", "init", "-q", str(root)], check=True)
    return root


@pytest.mark.parametrize("target", ["/dev/zero", "secret", "/nonexistent/loom-test"])
def test_a_linked_package_json_is_refused(checkout: Path, tmp_path: Path, target: str) -> None:
    (checkout / "package.json").symlink_to(_secret(tmp_path) if target == "secret" else target)
    with pytest.raises(FileRefused):
        deps.install_roots(checkout, ())


def test_a_package_json_directory_is_refused(checkout: Path) -> None:
    (checkout / "package.json").mkdir()
    with pytest.raises(FileRefused, match="not a regular file"):
        deps.install_roots(checkout, ())


def test_a_package_json_over_one_mib_is_refused_before_it_is_parsed(checkout: Path) -> None:
    # Minimal values parse at about 30 times their bytes: the checkout cap alone would let
    # a 64 MiB `[{},{},...]` take about 2 GiB.
    values = (1024 * 1024) // 3 + 1
    (checkout / "package.json").write_bytes(b"[" + b",".join([b"{}"] * values) + b"]")
    with pytest.raises(FileRefused, match="max 1048576"):
        deps.install_roots(checkout, ())


def test_a_yarn_lock_larger_than_a_package_json_may_be_is_still_read(checkout: Path) -> None:
    (checkout / "yarn.lock").write_bytes(b"__metadata:\n" + b"#" * (2 * 1024 * 1024))
    assert b"__metadata:" in untrusted_files.read_checkout_file(checkout / "yarn.lock")


@pytest.mark.parametrize("text", ["[]", '"x"', "1", "null"])
def test_a_package_json_that_is_no_json_object_is_refused_by_name(
    checkout: Path, text: str
) -> None:
    (checkout / "package.json").write_text(text)
    with pytest.raises(ValueError, match="package.json is not a JSON object"):
        deps.install_roots(checkout, ("p/a",))


def test_a_plain_package_json_still_names_its_roots(checkout: Path) -> None:
    (checkout / "package.json").write_text('{"name": "a", "workspaces": ["p/*"]}')
    assert deps.install_roots(checkout, ("p/a",)) == [checkout]


@pytest.mark.parametrize("target", ["/dev/zero", "/nonexistent/loom-test"])
def test_a_linked_yarn_lock_is_refused(checkout: Path, target: str) -> None:
    (checkout / "yarn.lock").symlink_to(target)
    with pytest.raises(FileRefused):
        deps._install_command(checkout, checkout)


def test_a_project_folder_linked_out_of_the_checkout_is_refused_before_its_yarn_lock(
    checkout: Path, tmp_path: Path
) -> None:
    outside = tmp_path / "outside-project"
    outside.mkdir()
    (outside / "yarn.lock").write_text("__metadata:\n")
    (checkout / "web").symlink_to(outside)  # what an earlier root's install could leave
    with pytest.raises(ValueError, match="escapes its checkout"):
        deps._install_command(checkout, checkout / "web")


def test_a_project_folder_linked_out_of_the_checkout_is_refused_for_any_manager(
    checkout: Path, tmp_path: Path
) -> None:
    outside = tmp_path / "outside-project"
    outside.mkdir()
    (outside / "package-lock.json").write_text("{}")  # npm: the job would run there
    (checkout / "web").symlink_to(outside)
    with pytest.raises(ValueError, match="escapes its checkout"):
        deps._install_command(checkout, checkout / "web")


def test_a_dangling_project_package_json_keeps_its_root(checkout: Path) -> None:
    # Not read on the host; the root stays listed, so its sandboxed install fails.
    (checkout / "web").mkdir()
    (checkout / "web" / "package.json").symlink_to("/nonexistent/loom-test")
    assert deps.install_roots(checkout, ("web",)) == [checkout / "web"]


def test_a_plain_yarn_lock_still_picks_the_manager(checkout: Path) -> None:
    (checkout / "yarn.lock").write_text("__metadata:\n  version: 6\n")
    assert deps._install_command(checkout, checkout)[0][0] == "yarn-modern"
    (checkout / "yarn.lock").write_text("# yarn lockfile v1\n")
    assert deps._install_command(checkout, checkout)[0][0] == "yarn"


# --- each compiler tier's delta ---


# A link to an endless device, and a dangling one, which must not read as "no delta".
_LINK_TARGETS = ["/dev/zero", "/nonexistent/loom-test"]


def _entry(producer: str, **kw: Any) -> Any:
    return repo_config(org="org", namespace=f"code-{producer}", scip_producer=producer, **kw)


def _repo(state: dict[str, str]) -> AsyncMock:
    repo = AsyncMock()

    async def get_state(org, ns, key):
        return state.get(key, "")

    repo.get_state.side_effect = get_state
    return repo


async def _run(worker: Any, cfg: Any, state: dict[str, str]) -> tuple[Any, AsyncMock, AsyncMock]:
    apply = AsyncMock(return_value=ScipApplyStats())
    repo = _repo(state)
    with (
        patch("server.code.store.index.CodeRepo", return_value=repo),
        patch("server.code.domain.index.repos.configured_repos", return_value=[cfg]),
        patch("server.code.app.index.scip_delta.apply_delta", apply),
    ):
        return await worker.run_pass(), apply, repo


def _host(monkeypatch, tmp_path: Path, worker: Any) -> None:
    (tmp_path / "checkout" / "node_modules").mkdir(parents=True)
    (tmp_path / "repo.git").mkdir()
    monkeypatch.setattr(worker.settings, "code_scip_checkout_dir", str(tmp_path / "checkout"))
    monkeypatch.setattr(worker.settings, "code_git_dir", str(tmp_path / "repo.git"))
    monkeypatch.setattr(worker, "git", AsyncMock())


def _refused(res: Any, apply: AsyncMock, repo: AsyncMock) -> None:
    assert res.deltas_applied == 0 and res.errors, res.errors
    assert "FileRefused" in res.errors[0]
    apply.assert_not_awaited()
    _watermark_unmoved(repo)  # the next pass retries


@pytest.mark.asyncio
@pytest.mark.parametrize("target", _LINK_TARGETS)
async def test_the_typescript_tier_refuses_a_linked_delta(
    monkeypatch, tmp_path: Path, target
) -> None:
    _host(monkeypatch, tmp_path, scip_ts_delta)
    monkeypatch.setattr(scip_ts_delta.settings, "code_scip_typescript_path", "/bin/sh")
    monkeypatch.setattr(scip_ts_delta, "prepare_dependencies", AsyncMock())

    async def harness(co, base, head, delta_path, projects=()):
        delta_path.symlink_to(target)

    monkeypatch.setattr(scip_ts_delta, "_run_harness", harness)
    res, apply, repo = await _run(scip_ts_delta, _entry("typescript"), {"git_head": "b" * 40})
    _refused(res, apply, repo)


@pytest.mark.asyncio
async def test_the_typescript_tier_refuses_a_linked_package_json(monkeypatch, tmp_path) -> None:
    _host(monkeypatch, tmp_path, scip_ts_delta)
    monkeypatch.setattr(scip_ts_delta.settings, "code_scip_typescript_path", "/bin/sh")
    (tmp_path / "checkout" / "package.json").symlink_to("/dev/zero")
    harness = AsyncMock()
    monkeypatch.setattr(scip_ts_delta, "_run_harness", harness)
    res, apply, repo = await _run(scip_ts_delta, _entry("typescript"), {"git_head": "b" * 40})
    _refused(res, apply, repo)
    harness.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "body",
    [
        _minimal_files(10),
        b'{"commit":"c","x":[' + b",".join([b"[]"] * 10) + b"]}",  # empty arrays
        b'{"commit":"c","x":[' + b",".join([b"0"] * 20) + b"]}",  # scalars
    ],
    ids=["objects", "empty-arrays", "scalars"],
)
async def test_the_python_tier_refuses_an_over_count_delta_before_parsing(
    monkeypatch, tmp_path: Path, body: bytes
) -> None:
    assert _nodes(body) == 22
    _host(monkeypatch, tmp_path, scip_py_delta)
    monkeypatch.setattr(scip_py_delta.settings, "code_scip_python_path", "/bin/sh")
    monkeypatch.setattr(settings, "code_scip_delta_max_nodes", 10)

    async def harness(co, base, head, delta_path):
        delta_path.write_bytes(body)

    monkeypatch.setattr(scip_py_delta, "_run_harness", harness)
    parse = Mock()
    monkeypatch.setattr("server.code.app.index.scip_delta.ScipDelta.model_validate_json", parse)
    res, apply, repo = await _run(scip_py_delta, _entry("python"), {"git_head": "b" * 40})
    _refused(res, apply, repo)
    assert "22 `{`/`[`/`,` bytes; max 10 (code_scip_delta_max_nodes)" in res.errors[0]
    parse.assert_not_called()


@pytest.mark.asyncio
async def test_an_invalid_delta_fails_the_pass_with_a_short_error(monkeypatch, tmp_path) -> None:
    _host(monkeypatch, tmp_path, scip_py_delta)
    monkeypatch.setattr(scip_py_delta.settings, "code_scip_python_path", "/bin/sh")

    async def harness(co, base, head, delta_path):
        delta_path.write_bytes(b'{"commit":"c","files":[' + b",".join([b"0"] * 100_000) + b"]}")

    monkeypatch.setattr(scip_py_delta, "_run_harness", harness)
    res, apply, repo = await _run(scip_py_delta, _entry("python"), {"git_head": "b" * 40})
    assert res.errors == ["code-python: ValueError: delta invalid: 100000 errors"], res.errors
    apply.assert_not_awaited()
    _watermark_unmoved(repo)


@pytest.mark.asyncio
async def test_the_python_tier_refuses_a_delta_directory(monkeypatch, tmp_path: Path) -> None:
    _host(monkeypatch, tmp_path, scip_py_delta)
    monkeypatch.setattr(scip_py_delta.settings, "code_scip_python_path", "/bin/sh")

    async def harness(co, base, head, delta_path):
        delta_path.mkdir()

    monkeypatch.setattr(scip_py_delta, "_run_harness", harness)
    res, apply, repo = await _run(scip_py_delta, _entry("python"), {"git_head": "b" * 40})
    _refused(res, apply, repo)


@pytest.mark.asyncio
@pytest.mark.parametrize("target", _LINK_TARGETS)
async def test_the_python_tier_refuses_a_linked_delta(monkeypatch, tmp_path: Path, target) -> None:
    _host(monkeypatch, tmp_path, scip_py_delta)
    monkeypatch.setattr(scip_py_delta.settings, "code_scip_python_path", "/bin/sh")

    async def harness(co, base, head, delta_path):
        delta_path.symlink_to(target)

    monkeypatch.setattr(scip_py_delta, "_run_harness", harness)
    res, apply, repo = await _run(scip_py_delta, _entry("python"), {"git_head": "b" * 40})
    _refused(res, apply, repo)


@pytest.mark.asyncio
@pytest.mark.parametrize("target", _LINK_TARGETS)
async def test_the_go_tier_refuses_a_linked_module_delta(
    monkeypatch, tmp_path: Path, target
) -> None:
    _go_host(monkeypatch, tmp_path)

    async def harness(co, base, head, out_dir, modules, cfg):
        (out_dir / "svc.json").symlink_to(target)
        return ""

    monkeypatch.setattr(scip_go_delta, "_run_harness", harness)
    res, apply, repo = await _run(scip_go_delta, _entry("go"), {"git_head": "b" * 40})
    _refused(res, apply, repo)


@pytest.mark.asyncio
async def test_the_go_tier_refuses_more_deltas_than_modules(monkeypatch, tmp_path: Path) -> None:
    _go_host(monkeypatch, tmp_path)  # one module, svc

    async def harness(co, base, head, out_dir, modules, cfg):
        for name in ("svc.json", "extra.json"):
            (out_dir / name).write_text(json.dumps(_DELTA))
        return ""

    monkeypatch.setattr(scip_go_delta, "_run_harness", harness)
    res, apply, repo = await _run(scip_go_delta, _entry("go"), {"git_head": "b" * 40})
    assert res.errors and "wrote more deltas than its 1 module(s)" in res.errors[0], res.errors
    apply.assert_not_awaited()
    _watermark_unmoved(repo)


@pytest.mark.asyncio
async def test_the_go_tier_applies_the_root_module_delta(monkeypatch, tmp_path: Path) -> None:
    _go_host(monkeypatch, tmp_path)
    monkeypatch.setattr(scip_go_delta, "_module_dirs", AsyncMock(return_value=[""]))

    async def harness(co, base, head, out_dir, modules, cfg):
        (out_dir / ".json").write_text(json.dumps(_DELTA))  # the harness's name for prefix ""
        return ""

    monkeypatch.setattr(scip_go_delta, "_run_harness", harness)
    res, apply, repo = await _run(scip_go_delta, _entry("go"), {"git_head": "b" * 40})
    assert not res.errors, res.errors
    apply.assert_awaited_once()


@pytest.mark.asyncio
async def test_a_go_window_refuses_more_deltas_than_its_modules(monkeypatch, tmp_path) -> None:
    _go_host(monkeypatch, tmp_path)
    monkeypatch.setattr(scip_go_delta, "_changed_paths", AsyncMock(return_value=["svc/a.go"]))
    monkeypatch.setattr(scip_go_delta, "modules_for_window", lambda changed, dirs: ["svc"])

    async def harness(co, base, head, out_dir, modules, cfg):
        assert base == "a" * 40 and modules == ["svc"]  # a window, not a full run
        for name in ("svc.json", "extra.json"):
            (out_dir / name).write_text(json.dumps(_DELTA))
        return ""

    monkeypatch.setattr(scip_go_delta, "_run_harness", harness)
    state = {"git_head": "b" * 40, "scip_commit_go": "a" * 40}
    res, apply, repo = await _run(scip_go_delta, _entry("go"), state)
    assert res.errors and "wrote more deltas than its 1 module(s)" in res.errors[0], res.errors
    apply.assert_not_awaited()
    _watermark_unmoved(repo)


@pytest.mark.asyncio
@pytest.mark.parametrize("target", _LINK_TARGETS)
async def test_a_go_window_refuses_a_linked_delta(monkeypatch, tmp_path, target) -> None:
    _go_host(monkeypatch, tmp_path)
    monkeypatch.setattr(scip_go_delta, "_changed_paths", AsyncMock(return_value=["svc/a.go"]))
    monkeypatch.setattr(scip_go_delta, "modules_for_window", lambda changed, dirs: ["svc"])

    async def harness(co, base, head, out_dir, modules, cfg):
        assert base == "a" * 40 and modules == ["svc"]  # a window, not a full run
        (out_dir / "svc.json").symlink_to(target)
        return ""

    monkeypatch.setattr(scip_go_delta, "_run_harness", harness)
    state = {"git_head": "b" * 40, "scip_commit_go": "a" * 40}
    res, apply, repo = await _run(scip_go_delta, _entry("go"), state)
    _refused(res, apply, repo)


def _go_host(monkeypatch, tmp_path: Path) -> None:
    _host(monkeypatch, tmp_path, scip_go_delta)
    tools = tmp_path / "tools"
    tools.mkdir()
    for name in ("go", "scip-go"):
        (tools / name).write_text("")
    monkeypatch.setattr(scip_go_delta.settings, "code_scip_go_bin_dir", str(tools))
    monkeypatch.setattr(scip_go_delta.settings, "code_scip_go_path", "scip-go")
    monkeypatch.setattr(scip_go_delta, "_module_dirs", AsyncMock(return_value=["svc"]))


@pytest.mark.asyncio
@pytest.mark.parametrize("target", _LINK_TARGETS)
async def test_the_clang_tier_refuses_a_linked_delta(monkeypatch, tmp_path: Path, target) -> None:
    _clang_host(monkeypatch, tmp_path)

    async def harness(script, checkout, args, env, **kw):
        os.symlink(target, env["DELTA_OUT"])
        return 0, ""

    monkeypatch.setattr(scip_delta, "run_harness", harness)
    state = {"git_head": "b" * 40, "scip_commit": "a" * 40}
    res, apply, repo = await _run(scip_delta, _entry("clang"), state)
    _refused(res, apply, repo)


@pytest.mark.asyncio
async def test_the_clang_tier_with_no_delta_advances_the_watermark(monkeypatch, tmp_path) -> None:
    _clang_host(monkeypatch, tmp_path)
    # the harness exits 0 and writes no delta: no C++ translation unit changed
    monkeypatch.setattr(scip_delta, "run_harness", AsyncMock(return_value=(0, "")))
    state = {"git_head": "b" * 40, "scip_commit": "a" * 40}
    res, apply, repo = await _run(scip_delta, _entry("clang"), state)
    assert not res.errors, res.errors
    apply.assert_not_awaited()
    repo.set_state.assert_awaited_with("org", "code-clang", "scip_commit", "b" * 40)


def _clang_host(monkeypatch, tmp_path: Path) -> None:
    from tests.unit.claims_fake import held_leases

    held_leases(monkeypatch)
    monkeypatch.setattr(scip_delta.settings, "code_scip_lock_dir", str(tmp_path / "locks"))
    _host(monkeypatch, tmp_path, scip_delta)
    (tmp_path / "scip-clang").touch()
    monkeypatch.setattr(scip_delta.settings, "code_scip_clang_path", str(tmp_path / "scip-clang"))


@pytest.mark.asyncio
@pytest.mark.parametrize("target", _LINK_TARGETS)
async def test_the_full_index_refuses_a_linked_delta_and_keeps_no_copy(
    monkeypatch, tmp_path: Path, target
) -> None:
    monkeypatch.setattr(scip_full, "move_checkout", AsyncMock())

    async def harness(checkout, clang, delta_path, **kw):
        delta_path.symlink_to(target)
        return 0, ""

    monkeypatch.setattr(scip_full, "run_full_harness", harness)
    apply = AsyncMock()
    monkeypatch.setattr(scip_full, "apply_delta", apply)
    kept = tmp_path / "kept.json"
    with pytest.raises(FileRefused):
        await scip_full._index_and_apply(
            AsyncMock(), _entry("clang"), tmp_path, tmp_path, tmp_path, "b" * 40, 1, 1, str(kept)
        )
    apply.assert_not_awaited()
    assert not kept.exists()


@pytest.mark.asyncio
async def test_the_full_index_reads_and_keeps_a_plain_delta(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setattr(scip_full, "move_checkout", AsyncMock())
    monkeypatch.setattr(scip_full, "check_held", lambda: None)
    monkeypatch.setattr(scip_full, "mark_done", AsyncMock())

    async def harness(checkout, clang, delta_path, **kw):
        delta_path.write_text(json.dumps(_DELTA))
        return 0, "ok"

    monkeypatch.setattr(scip_full, "run_full_harness", harness)
    apply = AsyncMock(return_value=ScipApplyStats())
    monkeypatch.setattr(scip_full, "apply_delta", apply)
    kept = tmp_path / "kept.json"
    await scip_full._index_and_apply(
        AsyncMock(), _entry("clang"), tmp_path, tmp_path, tmp_path, "b" * 40, 1, 1, str(kept)
    )
    assert apply.await_args.args[3].commit == "b" * 40
    assert json.loads(kept.read_text()) == _DELTA


def test_the_go_delta_listing_stops_at_the_first_delta_too_many(tmp_path, monkeypatch) -> None:
    for name in ("a.json", "b.json", "c.json", ".json", "notes.txt"):
        (tmp_path / name).write_text("{}")
    listed: list[str] = []
    scandir = os.scandir

    def recorded(entries: Iterator[os.DirEntry[str]]) -> Iterator[os.DirEntry[str]]:
        for entry in entries:
            listed.append(entry.name)
            yield entry

    @contextlib.contextmanager
    def counted(path: Path) -> Iterator[Iterator[os.DirEntry[str]]]:
        with scandir(path) as entries:
            yield recorded(entries)

    with monkeypatch.context() as m:
        m.setattr(scip_go_delta.os, "scandir", counted)
        with pytest.raises(RuntimeError, match=r"wrote more deltas than its 1 module\(s\)"):
            scip_go_delta._deltas(tmp_path, 1)
    assert sum(n.endswith(".json") for n in listed) == 2
    assert [p.name for p in scip_go_delta._deltas(tmp_path, 4)] == [
        ".json",
        "a.json",
        "b.json",
        "c.json",
    ]  # the root module's delta is `.json`; another file is no delta


def test_apply_delta_reads_its_kept_delta_under_the_caps(tmp_path: Path) -> None:
    from server.cli.code_scip import _read_delta

    kept = tmp_path / "delta.json"
    kept.write_text('{"commit": "c"}')
    assert asyncio.run(_read_delta(str(kept))).commit == "c"
    (tmp_path / "link.json").symlink_to(kept)
    with pytest.raises(FileRefused):
        asyncio.run(_read_delta(str(tmp_path / "link.json")))
    kept.write_text('{"commit": "c", "files": [0]}')
    with pytest.raises(ValueError, match="delta invalid: 1 errors"):
        asyncio.run(_read_delta(str(kept)))
