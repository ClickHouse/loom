"""Prepare compiler checkouts without executing repository lifecycle scripts.

The image and native prerequisite installer provide the package managers. The
dependency fingerprint lives in git's metadata, so a cancelled/failed install
cannot mark dependencies current and a branch update refreshes them before SCIP.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
import shutil
import subprocess
import uuid
from pathlib import Path

from server.code.app.index.gitrepo import auth_env
from server.code.app.index.sandbox import run_job
from server.code.app.index.tier_health import ToolOutputError, error_text
from server.code.app.index.untrusted_files import read_checkout_file, read_package_json
from server.code.domain.index.tier_health import ERROR_CHARS, redact
from server.settings import settings

logger = logging.getLogger("loom.code_index.typescript_dependencies")

LOCKFILES = (
    "package-lock.json",
    "npm-shrinkwrap.json",
    "pnpm-lock.yaml",
    "yarn.lock",
    "bun.lockb",
    "bun.lock",
)
_INPUTS = (*LOCKFILES, "package.json", "pnpm-workspace.yaml", ".npmrc", ".yarnrc.yml")


def install_roots(checkout: Path, projects: tuple[str, ...]) -> list[Path]:
    """Install a workspace once at its root, or each independent named project."""
    manifest = checkout / "package.json"
    if os.path.lexists(manifest):  # a link of any kind reaches the read and is refused
        package = json.loads(read_package_json(manifest))
        if not isinstance(package, dict):
            raise ValueError(f"{manifest} is not a JSON object")
        if not projects or package.get("workspaces") or (checkout / "pnpm-workspace.yaml").exists():
            return [checkout]
    roots = [checkout / p for p in projects] if projects else [checkout]
    for root in roots:
        _require_inside(checkout, root)
    return [root for root in roots if os.path.lexists(root / "package.json")]


def _require_inside(checkout: Path, root: Path) -> None:
    if not root.resolve().is_relative_to(checkout.resolve()):
        raise ValueError(f"TypeScript project escapes its checkout: {root}")


def _dependency_state(checkout: Path) -> tuple[Path, str]:
    patterns = [p for name in _INPUTS for p in (name, f"**/{name}")]
    inputs = subprocess.run(
        ["git", "-C", str(checkout), "ls-files", "--stage", "-z", "--", *patterns],
        check=True,
        capture_output=True,
        timeout=settings.code_git_timeout_s,
        env=auth_env(),
    ).stdout
    metadata = subprocess.run(
        ["git", "-C", str(checkout), "rev-parse", "--git-path", "loom-typescript-dependencies"],
        check=True,
        capture_output=True,
        text=True,
        timeout=settings.code_git_timeout_s,
        env=auth_env(),
    ).stdout.strip()
    marker = Path(metadata)
    if not marker.is_absolute():
        marker = checkout / marker
    return marker, hashlib.sha256(inputs).hexdigest()


def _install_command(checkout: Path, root: Path) -> tuple[list[str], dict[str, str]]:
    """Use shipped managers and immutable lockfiles; never load JS config hooks."""
    # An earlier root's install may have made this folder a link: re-check before the
    # host reads its lockfile or the job runs in it.
    _require_inside(checkout, root)
    if (root / "pnpm-lock.yaml").exists() or (root / "pnpm-workspace.yaml").exists():
        args = [
            "pnpm",
            "install",
            "--ignore-scripts",
            "--ignore-pnpmfile",
            "--config.manage-package-manager-versions=false",
            "--config.package-manager-strict=false",
        ]
        if (root / "pnpm-lock.yaml").exists():
            args.append("--frozen-lockfile")
        return args, {}
    if os.path.lexists(root / "yarn.lock"):
        modern = b"__metadata:" in read_checkout_file(root / "yarn.lock")
        if modern:
            return ["yarn-modern", "install", "--immutable", "--mode=skip-build"], {
                "YARN_ENABLE_SCRIPTS": "false",
                "YARN_IGNORE_PATH": "true",
                "YARN_NODE_LINKER": "node-modules",
                "YARN_RC_FILENAME": f".loom-indexer-{uuid.uuid4().hex}.yml",
            }
        return [
            "yarn",
            "install",
            "--frozen-lockfile",
            "--ignore-scripts",
            "--non-interactive",
            "--no-default-rc",
        ], {"YARN_IGNORE_PATH": "1"}
    if any((root / name).exists() for name in ("bun.lock", "bun.lockb")):
        return ["bun", "install", "--frozen-lockfile", "--ignore-scripts"], {}
    if any((root / name).exists() for name in ("package-lock.json", "npm-shrinkwrap.json")):
        return ["npm", "ci", "--ignore-scripts", "--no-audit", "--no-fund"], {}
    return [
        "npm",
        "install",
        "--ignore-scripts",
        "--package-lock=false",
        "--no-audit",
        "--no-fund",
    ], {}


_LINE_TOO_LONG = "(the last output line is too long to keep; the server log has it)"


def _project(checkout: Path, root: Path) -> str:
    """The install root as an error names it: no host path."""
    return "the checkout root" if root == checkout else f"project {os.path.relpath(root, checkout)}"


async def prepare_dependencies(
    checkout: Path, projects: tuple[str, ...], env: dict[str, str], *, force: bool = False
) -> None:
    roots = await asyncio.to_thread(install_roots, checkout, projects)
    if not roots:
        return  # Plain scripts/tsconfig-only trees have no dependencies to install.
    marker, fingerprint = await asyncio.to_thread(_dependency_state, checkout)
    current = await asyncio.to_thread(marker.read_text) if marker.exists() else ""
    installed = all((root / "node_modules").exists() for root in roots)
    if installed and not force and current in ("", fingerprint):
        await asyncio.to_thread(marker.write_text, fingerprint)
        return  # Preserve operator-prepared checkouts; subsequently track changes.
    # An interrupted install may have created node_modules already. Keep a
    # pending marker until every root succeeded so the next pass retries it.
    await asyncio.to_thread(marker.write_text, "installing")
    timeout_s = settings.code_scip_ts_delta_timeout_seconds
    for root in roots:
        command, extra_env = await asyncio.to_thread(_install_command, checkout, root)
        # Do not pass the server environment (JWT, provider and ClickHouse keys).
        install_env = {
            **env,
            "CI": "true",
            "npm_config_ignore_scripts": "true",
            "npm_config_production": "false",
            **extra_env,
        }
        if not await asyncio.to_thread(shutil.which, command[0], path=install_env.get("PATH")):
            raise ToolOutputError(
                f"TypeScript dependency tool {command[0]} is missing; "
                "run sh scripts/install-code-toolchains.sh on native installations"
            )
        # Package downloads go through the sandbox's egress proxy; the checkout
        # is the one writable path, its .git read-only.
        try:
            done = await run_job(
                command,
                env=install_env,
                timeout_s=timeout_s,
                writable=[checkout],
                readonly=[checkout / ".git"],
                cwd=root,
                network=True,
                merge_stderr=True,
            )
        except TimeoutError:
            raise ToolOutputError(
                f"TypeScript dependency installation ({command[0]}) timed out after "
                f"{timeout_s}s in {_project(checkout, root)}"
            ) from None
        if done.returncode != 0:
            output = done.stdout.decode("utf-8", "replace")
            logger.warning(
                "TypeScript dependency installation in %s failed: %s", root, output[-2000:]
            )
            head = (
                f"TypeScript dependency installation failed ({command[0]}, exit "
                f"{done.returncode}) in {_project(checkout, root)}"
            )
            # Redact before the cut, so the budget is what the record keeps; only whole lines
            # of a bounded window: redaction is quadratic, and a cut line can lose a scheme.
            room = max(0, ERROR_CHARS - len(error_text(ToolOutputError(head))) - 1)
            window = output[-4 * ERROR_CHARS :]
            if len(output) > len(window):
                window = window.partition("\n")[2] or _LINE_TOO_LONG
            tail = redact(window)[-room:] if room else ""
            raise ToolOutputError(f"{head}\n{tail}")
    await asyncio.to_thread(marker.write_text, fingerprint)
