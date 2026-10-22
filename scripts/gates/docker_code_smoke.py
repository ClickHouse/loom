"""Run inside the production container against the smoke stack's real ClickHouse.

Local Git fixtures enter the same registry/bootstrap path used after code.repo.add
validates a remote. This keeps compiler tests independent of a public repo changing.
The stock worker configuration is retained; passes are triggered explicitly to avoid
waiting ten minutes for the compiler polling interval in a release gate.
"""

from __future__ import annotations

import asyncio
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

from server.code.app.index.repo_registry import refresh_db_repos
from server.code.app.repo_admin import _register
from server.code.domain.index.repos import registry_repos
from server.code.store.index import CodeRepo
from server.code.workers import ingest, scip_go_delta, scip_py_delta, scip_ts_delta
from server.core.ch import get_client
from server.settings import settings

ROOT = Path("/app/var/code_repos")
MANIFEST = ROOT / "docker-smoke.json"
# How long the ingest passes may take to give every fixture its first index, and the
# pause between two of them.
SEED_TIMEOUT_S = 180
SEED_POLL_S = 1.0


def run(root: Path, *command: str, env: dict[str, str] | None = None) -> str:
    result = subprocess.run(
        command,
        cwd=root,
        env=env,
        capture_output=True,
        text=True,
        timeout=180,
    )
    if result.returncode:
        raise RuntimeError(f"{command[0]} failed: {result.stdout[-1500:]} {result.stderr[-1500:]}")
    return result.stdout.strip()


def fixture(name: str, root: Path) -> tuple[Path, str]:
    source = root / name
    source.mkdir()
    if name == "python":
        (source / "pyproject.toml").write_text('[project]\nname="smoke"\nversion="0.1.0"\n')
        (source / "example.py").write_text(
            'def greet(name: str) -> str:\n    return "Hello " + name\n\n'
            'def welcome() -> str:\n    return greet("Loom")\n'
        )
    elif name == "go":
        (source / "go.mod").write_text("module example.com/smoke\n\ngo 1.23\n")
        (source / "example.go").write_text(
            'package smoke\nfunc Greet(name string) string { return "Hello " + name }\n'
            'func Welcome() string { return Greet("Loom") }\n'
        )
    else:
        package = {
            "name": "loom-docker-smoke",
            "version": "1.0.0",
            "private": True,
            "scripts": {"preinstall": "touch lifecycle-ran", "postinstall": "touch lifecycle-ran"},
            "devDependencies": {"@types/node": "22.10.2"},
        }
        (source / "package.json").write_text(json.dumps(package))
        (source / "tsconfig.json").write_text(
            '{"compilerOptions":{"target":"es2022","module":"commonjs","types":["node"]},"include":["*.ts"]}'
        )
        (source / "example.ts").write_text(
            'export function greet(name: string): string { return "Hello " + name; }\n'
            'export function welcome(): string { return greet("Loom"); }\n'
        )
        env = {"PATH": os.environ["PATH"], "HOME": str(Path.home()), "CI": "true"}
        commands = {
            "npm": ("npm", "install", "--package-lock-only", "--ignore-scripts", "--no-audit"),
            "pnpm": ("pnpm", "install", "--lockfile-only", "--ignore-scripts", "--ignore-pnpmfile"),
            "yarn": ("yarn", "install", "--ignore-scripts", "--non-interactive"),
            "yarn-modern": ("yarn-modern", "install", "--mode=update-lockfile"),
            "bun": ("bun", "install", "--lockfile-only", "--ignore-scripts"),
        }
        if name == "yarn-modern":
            env.update(
                YARN_ENABLE_SCRIPTS="false",
                YARN_IGNORE_PATH="true",
                YARN_NODE_LINKER="node-modules",
                YARN_RC_FILENAME=".loom-indexer-yarnrc.yml",
                YARN_ENABLE_IMMUTABLE_INSTALLS="false",
            )
        if name in commands:
            run(source, *commands[name], env=env)
        assert not (source / "lifecycle-ran").exists(), f"{name} executed lifecycle scripts"
    run(source, "git", "init", "-q", "-b", "main")
    tracked = [p.name for p in source.iterdir() if p.is_file() and not p.name.startswith(".")]
    run(source, "git", "add", "--", *tracked)
    run(
        source,
        "git",
        "-c",
        "user.name=smoke",
        "-c",
        "user.email=smoke@example.com",
        "commit",
        "-qm",
        "fixture",
    )
    return source, run(source, "git", "rev-parse", "HEAD")


async def seed(repo: CodeRepo) -> list[dict[str, str]]:
    await asyncio.to_thread(ROOT.mkdir, parents=True, exist_ok=True)
    sources = Path(tempfile.mkdtemp(prefix="smoke-sources-", dir=ROOT))
    entries = []
    for name in ("python", "go", "npm", "pnpm", "yarn", "yarn-modern", "bun", "no-lock"):
        source, head = await asyncio.to_thread(fixture, name, sources)
        ns = f"code-docker-smoke-{name}"
        producer = name if name in ("python", "go") else "typescript"
        await _register(
            org="dev",
            namespace=ns,
            repo=f"docker-smoke/{name}",
            git_remote=str(source),
            provider="local",
            branch="main",
            classification="standard",
            join_policy="open",
            github_token="",
            path_prefixes=[""],
            exclude=[],
            remote_head=head,
            scip_producer=producer,
            exclude_generated=False,
        )
        entries.append({"namespace": ns, "head": head, "producer": producer})
    MANIFEST.write_text(json.dumps(entries))
    # Trigger the same worker passes that the supervisor schedules, with real IO. A pass
    # hands a missing mirror's clone to a background job and moves on, so an entry's
    # first index (its git_head) lands on a later pass: pass until every entry has it.
    deadline = time.monotonic() + SEED_TIMEOUT_S
    while True:
        result = await ingest.run_pass()
        assert not result.errors, result.errors
        unseeded = [
            e["namespace"]
            for e in entries
            if await repo.get_state("dev", e["namespace"], "git_head") != e["head"]
        ]
        if not unseeded:
            break
        assert time.monotonic() < deadline, f"git_head not seeded: {unseeded} ({result.note})"
        await asyncio.sleep(SEED_POLL_S)
    for worker in (scip_py_delta, scip_go_delta, scip_ts_delta):
        result = await worker.run_pass()
        assert not result.errors and not result.skipped, result
        if worker is scip_go_delta:
            assert not result.modules_skipped, result.modules_skipped
    return entries


async def verify(repo: CodeRepo, entries: list[dict[str, str]]) -> None:
    watermarks = {
        "python": "scip_commit_py",
        "go": "scip_commit_go",
        "typescript": "scip_commit_ts",
    }
    for entry in entries:
        ns = entry["namespace"]
        watermark = await repo.get_state("dev", ns, watermarks[entry["producer"]])
        assert watermark == entry["head"], f"{ns}: compiler watermark {watermark} != head"
        cfg = next(c for c in registry_repos() if c.namespace == ns)
        checkout = Path(cfg.scip_checkout_dir)
        assert await asyncio.to_thread(checkout.is_dir), f"{ns}: checkout is not persistent"
        assert not (checkout / "lifecycle-ran").exists(), f"{ns}: lifecycle scripts ran"
        row = await get_client().query_one(
            "SELECT count() AS n FROM loom.code_symbols FINAL "
            "WHERE org = {org:String} AND namespace = {ns:String} "
            "AND tier = 'scip' AND tombstoned_at IS NULL",
            {"org": "dev", "ns": ns},
        )
        assert row and int(row["n"]) >= 2, f"{ns}: no compiler symbols: {row}"
        print(f"PASS {ns}: compiler watermark and symbols persisted", flush=True)


async def main() -> None:
    for flag in (
        settings.code_scip_delta_enabled,
        settings.code_scip_ts_delta_enabled,
        settings.code_scip_py_delta_enabled,
        settings.code_scip_go_delta_enabled,
    ):
        assert flag, "A compiler worker is disabled"
    for tool in (
        "node",
        "npm",
        "pnpm",
        "yarn",
        "yarn-modern",
        "bun",
        "scip-typescript",
        "scip-python",
        "go",
        "scip-go",
    ):
        assert shutil.which(tool), f"Missing shipped toolchain: {tool}"
    client = get_client()
    await client.connect()
    try:
        await refresh_db_repos(force=True)
        repo = CodeRepo()
        entries = (
            json.loads(MANIFEST.read_text()) if "--verify-only" in sys.argv else await seed(repo)
        )
        await verify(repo, entries)
    finally:
        await client.disconnect()


if __name__ == "__main__":
    asyncio.run(main())
