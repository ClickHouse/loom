"""Real git checkouts and installer subprocesses, including interrupted installs."""

from __future__ import annotations

import asyncio
import os
import subprocess
import time
from datetime import UTC, datetime
from pathlib import Path

import pytest

from server.code.app.index import tier_health as recorder
from server.code.app.index import typescript_dependencies as deps
from server.code.domain.index import tier_health
from server.observability.event_args import bounded_error

NOW = datetime(2026, 1, 1, tzinfo=UTC)


@pytest.fixture
def checkout(tmp_path: Path) -> Path:
    root = tmp_path / "checkout"
    root.mkdir()
    subprocess.run(["git", "init", "-q", str(root)], check=True)
    (root / "package.json").write_text('{"name":"smoke","version":"1.0.0"}')
    (root / "package-lock.json").write_text("{}")
    subprocess.run(["git", "-C", str(root), "add", "."], check=True)
    return root


def _installer(tmp_path: Path, name: str = "npm", *, fail: bool = False) -> dict[str, str]:
    executable = tmp_path / name
    executable.write_text(
        "#!/bin/sh\n"
        'printf "%s\\n" "$*" >> calls\n'
        "mkdir -p node_modules\n"
        'test "${LOOM_JWT_SECRET:-}" = "" || exit 10\n'
        f"exit {1 if fail else 0}\n"
    )
    executable.chmod(0o755)
    return {"PATH": f"{tmp_path}:{os.defpath}", "HOME": str(tmp_path)}


@pytest.mark.asyncio
async def test_fresh_checkout_installs_once_then_refreshes_changed_manifests(
    checkout: Path, tmp_path: Path
) -> None:
    env = _installer(tmp_path)
    await deps.prepare_dependencies(checkout, (), env)
    assert (checkout / "node_modules").is_dir()
    await deps.prepare_dependencies(checkout, (), env)
    assert len((checkout / "calls").read_text().splitlines()) == 1
    (checkout / "package.json").write_text('{"name":"smoke","version":"2.0.0"}')
    await asyncio.to_thread(
        subprocess.run, ["git", "-C", str(checkout), "add", "package.json"], check=True
    )
    await deps.prepare_dependencies(checkout, (), env)
    calls = (checkout / "calls").read_text().splitlines()
    assert len(calls) == 2 and all("ci --ignore-scripts" in call for call in calls)


@pytest.mark.asyncio
async def test_failed_install_is_retried_even_if_it_created_node_modules(
    checkout: Path, tmp_path: Path
) -> None:
    env = _installer(tmp_path, fail=True)
    with pytest.raises(RuntimeError, match="dependency installation failed"):
        await deps.prepare_dependencies(checkout, (), env)
    env = _installer(tmp_path)
    await deps.prepare_dependencies(checkout, (), env)
    assert len((checkout / "calls").read_text().splitlines()) == 2


@pytest.mark.asyncio
async def test_plain_scripts_do_not_run_a_package_manager(tmp_path: Path) -> None:
    await deps.prepare_dependencies(tmp_path, (), {})


@pytest.mark.parametrize(
    ("filename", "contents", "tool"),
    [
        ("package-lock.json", "{}", "npm"),
        ("pnpm-lock.yaml", "lockfileVersion: 9", "pnpm"),
        ("yarn.lock", "# yarn lockfile v1", "yarn"),
        ("yarn.lock", "__metadata:", "yarn-modern"),
        ("bun.lock", "{}", "bun"),
        ("bun.lockb", "binary", "bun"),
    ],
)
def test_lockfile_selects_the_shipped_manager_and_disables_scripts(
    tmp_path: Path, filename: str, contents: str, tool: str
) -> None:
    (tmp_path / filename).write_text(contents)
    command, env = deps._install_command(tmp_path, tmp_path)
    assert command[0] == tool
    if tool == "yarn-modern":
        assert env["YARN_ENABLE_SCRIPTS"] == "false"
        assert env["YARN_RC_FILENAME"].startswith(".loom-indexer-")
        assert "--immutable" in command
    else:
        assert "--ignore-scripts" in command
    if tool == "pnpm":
        assert "--ignore-pnpmfile" in command


@pytest.mark.asyncio
async def test_missing_package_manager_has_native_installation_instruction(
    checkout: Path, tmp_path: Path
) -> None:
    with pytest.raises(RuntimeError) as missing:
        await deps.prepare_dependencies(checkout, (), {"PATH": str(tmp_path)})
    assert "install-code-toolchains.sh" in recorder.error_text(missing.value)


@pytest.mark.asyncio
async def test_a_failed_install_is_recorded_with_the_installer_and_its_last_output(
    checkout: Path, tmp_path: Path
) -> None:
    env = _installer(tmp_path)
    noise = "x" * 1000
    (tmp_path / "npm").write_text(f"#!/bin/sh\necho {noise}\necho 'Failed to parse'\nexit 1\n")
    with pytest.raises(RuntimeError) as failed:
        await deps.prepare_dependencies(checkout, (), env)
    record = tier_health.failed(None, "typescript", recorder.error_text(failed.value), NOW)
    first = "ToolOutputError: TypeScript dependency installation failed (npm, exit 1) in the checkout root"
    assert record.error.startswith(first + "\n") and record.error.endswith("Failed to parse")
    assert "(npm, exit 1)" in bounded_error(failed.value)[1], "the ledger keeps the first line"


@pytest.mark.asyncio
async def test_a_failed_project_install_names_the_project_not_the_host_path(
    checkout: Path, tmp_path: Path
) -> None:
    env = _installer(tmp_path, fail=True)
    web = checkout / "apps" / "web"
    web.mkdir(parents=True)
    (web / "package.json").write_text('{"name":"web","version":"1.0.0"}')
    (web / "package-lock.json").write_text("{}")
    with pytest.raises(RuntimeError) as failed:
        await deps.prepare_dependencies(checkout, ("apps/web",), env)
    assert "(npm, exit 1) in project apps/web" in str(failed.value)
    assert str(tmp_path) not in str(failed.value)


@pytest.mark.asyncio
async def test_a_long_last_line_keeps_its_end(checkout: Path, tmp_path: Path) -> None:
    env = _installer(tmp_path)
    last = "ERR_PNPM_X " + "y" * 800 + " the cause"
    (tmp_path / "npm").write_text(f"#!/bin/sh\necho first\necho '{last}'\nexit 1\n")
    with pytest.raises(RuntimeError) as failed:
        await deps.prepare_dependencies(checkout, (), env)
    record = tier_health.failed(None, "typescript", recorder.error_text(failed.value), NOW)
    assert record.error.startswith("ToolOutputError: TypeScript dependency installation failed")
    assert record.error.endswith("y the cause")


@pytest.mark.asyncio
async def test_a_redacted_tail_still_fits_the_record_whole(checkout: Path, tmp_path: Path) -> None:
    head = recorder.ToolOutputError(
        "TypeScript dependency installation failed (npm, exit 1) in the checkout root"
    )
    room = tier_health.ERROR_CHARS - len(recorder.error_text(head)) - 1
    # A last line that fills the budget raw, with a one-character token that grows
    # when redacted: the record must still start with the whole first line.
    start = "GET /pkg?token=1 "
    last = start + "y" * (room - 1 - len(start))
    env = _installer(tmp_path)
    (tmp_path / "npm").write_text(f"#!/bin/sh\necho first\necho '{last}'\nexit 1\n")
    with pytest.raises(RuntimeError) as failed:
        await deps.prepare_dependencies(checkout, (), env)
    first = str(failed.value).partition("\n")[0]
    # The raw text fills the budget but one character, so the redacted growth must fit too.
    assert len(recorder.error_text(recorder.ToolOutputError(first))) + 1 + len(last) == (
        tier_health.ERROR_CHARS - 1
    )
    record = tier_health.failed(None, "typescript", recorder.error_text(failed.value), NOW)
    assert record.error.startswith("ToolOutputError: TypeScript dependency installation failed")
    assert "token=1 " not in record.error


@pytest.mark.asyncio
async def test_long_unbroken_output_does_not_stall_the_error(
    checkout: Path, tmp_path: Path
) -> None:
    env = _installer(tmp_path)
    # 100 lines of 2000 characters with no space: each line is costly to redact.
    (tmp_path / "npm").write_text(
        "#!/bin/sh\nfor i in $(seq 100); do printf 'a.%.0s' $(seq 1000); echo; done\nexit 1\n"
    )
    started = time.monotonic()
    with pytest.raises(RuntimeError) as failed:
        await deps.prepare_dependencies(checkout, (), env)
    assert time.monotonic() - started < 5, "redacting the whole output is quadratic"
    assert str(failed.value).endswith("a.a.a.")


@pytest.mark.asyncio
async def test_a_last_line_longer_than_the_window_is_left_out_by_name(
    checkout: Path, tmp_path: Path
) -> None:
    env = _installer(tmp_path)
    (tmp_path / "npm").write_text("#!/bin/sh\necho first\nprintf 'x%.0s' $(seq 3000)\nexit 1\n")
    with pytest.raises(RuntimeError) as failed:
        await deps.prepare_dependencies(checkout, (), env)
    assert str(failed.value).endswith(deps._LINE_TOO_LONG)


@pytest.mark.asyncio
async def test_a_credential_cut_at_the_window_start_is_not_kept(
    checkout: Path, tmp_path: Path
) -> None:
    env = _installer(tmp_path)
    # The redaction window (the output's last 4 * ERROR_CHARS characters) starts
    # inside "user:hunter2", after the scheme its pattern needs.
    start = "fetch https://us"
    rest = "er:hunter2@h.example/x?token="
    line = start + rest + "t" * (4 * tier_health.ERROR_CHARS - 1 - len(rest))
    (tmp_path / "npm").write_text(f"#!/bin/sh\necho first\necho '{line}'\nexit 1\n")
    with pytest.raises(RuntimeError) as failed:
        await deps.prepare_dependencies(checkout, (), env)
    record = tier_health.failed(None, "typescript", recorder.error_text(failed.value), NOW)
    assert "hunter2" not in record.error


@pytest.mark.asyncio
async def test_an_install_timeout_is_recorded_as_a_timeout(
    checkout: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def times_out(*a: object, **k: object) -> None:
        raise TimeoutError

    monkeypatch.setattr(deps, "run_job", times_out)
    with pytest.raises(RuntimeError) as failed:
        await deps.prepare_dependencies(checkout, (), _installer(tmp_path))
    assert tier_health.classify(recorder.error_text(failed.value)) == "timeout"
    text = str(failed.value)
    assert "(npm) timed out after" in text and "in the checkout root" in text
    assert str(tmp_path) not in text


def test_named_project_cannot_escape_checkout(checkout: Path) -> None:
    with pytest.raises(ValueError, match="escapes"):
        deps.install_roots(checkout, ("../outside",))
