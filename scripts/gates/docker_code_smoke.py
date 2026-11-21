"""Run inside the production container against the smoke stack's real ClickHouse.

Local Git fixtures enter the same registry/bootstrap path used after code.repo.add
validates a remote. This keeps compiler tests independent of a public repo changing.
Initial compiler passes run explicitly. The smoke then enables code ingest with
normal worker intervals; MCP memory links and the later edit wait for workers.
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
import uuid
from pathlib import Path
from typing import Any

import httpx

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
        entry = {"namespace": ns, "head": head, "producer": producer}
        if name == "python":
            entry["source"] = str(source)
        entries.append(entry)
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


async def mcp(client: httpx.AsyncClient, tool: str, arguments: dict[str, Any]) -> dict[str, Any]:
    response = await client.post(
        "/mcp?toolset=coding",
        json={
            "jsonrpc": "2.0",
            "id": 1,
            "method": "tools/call",
            "params": {"name": tool, "arguments": {"org": "dev", **arguments}},
        },
    )
    response.raise_for_status()
    envelope = response.json()
    assert "error" not in envelope, envelope
    result = envelope["result"]
    assert not result.get("isError"), result
    return dict(result["structuredContent"])


async def wait_for_link(
    client: httpx.AsyncClient, entry: dict[str, str], state: str, deadline: float
) -> None:
    identity = {"namespace": entry["namespace"], "memory_key": entry["memory_key"]}
    while True:
        memory = await mcp(client, "loom_memory_get", identity)
        assert memory.get("found") and memory["value"] == "`greet()` adds a greeting.", memory
        links = [
            link
            for link in memory.get("code", [])
            if link["path"] == "example.py" and link["code_namespace"] == entry["namespace"]
        ]
        if len(links) == 1 and links[0]["state"] == state:
            if state == "changed":
                assert links[0]["changed_since"] == entry["head"], links
            symbol = await mcp(
                client,
                "loom_code_symbol",
                {"namespace": entry["namespace"], "name": "greet", "include_body": False},
            )
            definitions = symbol.get("definitions", [])
            assert len(definitions) == 1 and definitions[0]["path"] == "example.py", symbol
            status = await mcp(
                client, "loom_code_index_status", {"namespace": entry["namespace"], "brief": True}
            )
            # Status can return its cached head after the link has changed.
            if status["git_head"] != entry["head"]:
                assert time.monotonic() < deadline, status
                await asyncio.sleep(SEED_POLL_S)
                continue
            lessons = definitions[0].get("lessons", [])
            for lesson in lessons:
                if lesson["memory_key"] == entry["memory_key"]:
                    assert lesson["code_state"] == state, lesson
                    if state == "changed":
                        assert lesson["changed_since"] == entry["head"], lesson
            print(f"PASS MCP code link: {state}, commit={entry['head']}", flush=True)
            return
        assert time.monotonic() < deadline, f"MCP code link did not become {state}: {memory}"
        await asyncio.sleep(SEED_POLL_S)


async def code_link_lifecycle(repo: CodeRepo, entries: list[dict[str, str]]) -> None:
    entry = next(e for e in entries if e["producer"] == "python")
    assert settings.code_ingest_enabled and settings.memory_code_links_enabled
    deadline = time.monotonic() + (
        SEED_TIMEOUT_S
        + settings.code_ingest_interval_seconds
        + settings.code_scip_py_delta_interval_seconds
        + settings.memory_code_links_interval_seconds
        + settings.memory_code_links_settle_seconds
    )
    async with (
        asyncio.timeout_at(deadline),
        httpx.AsyncClient(base_url="http://127.0.0.1:7777", timeout=30) as client,
    ):
        if "--verify-only" in sys.argv:
            await wait_for_link(client, entry, "changed", deadline)
            await mcp(
                client,
                "loom_memory_delete",
                {"namespace": entry["namespace"], "memory_key": entry["memory_key"]},
            )
            gone = await mcp(
                client,
                "loom_memory_get",
                {"namespace": entry["namespace"], "memory_key": entry["memory_key"]},
            )
            assert not gone.get("found"), gone
            return
        entry["memory_key"] = f"docker/code-link/{uuid.uuid4().hex}"
        await mcp(
            client,
            "loom_memory_set",
            {
                "namespace": entry["namespace"],
                "memory_key": entry["memory_key"],
                "value": "`greet()` adds a greeting.",
            },
        )
        await wait_for_link(client, entry, "fresh", deadline)
        source = Path(entry["source"])
        path = source / "example.py"
        before = path.read_text()
        assert before.count('return "Hello " + name') == 1, before
        path.write_text(before.replace('return "Hello " + name', 'return "Hello " + name.strip()'))
        await asyncio.to_thread(run, source, "git", "add", "example.py")
        await asyncio.to_thread(
            run,
            source,
            "git",
            "-c",
            "user.name=smoke",
            "-c",
            "user.email=smoke@example.com",
            "commit",
            "-qm",
            "change greeting",
        )
        entry["head"] = await asyncio.to_thread(run, source, "git", "rev-parse", "HEAD")
        while any(
            [
                await repo.get_state("dev", entry["namespace"], watermark) != entry["head"]
                for watermark in ("git_head", "scip_commit_py")
            ]
        ):
            assert time.monotonic() < deadline, "The workers did not index the source edit"
            await asyncio.sleep(SEED_POLL_S)
        await wait_for_link(client, entry, "changed", deadline)
        MANIFEST.write_text(json.dumps(entries))


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
        lifecycle = "--link-lifecycle" in sys.argv or "--verify-only" in sys.argv
        if not lifecycle:
            assert not settings.code_ingest_enabled, "Seed before starting scheduled ingest"
        entries = json.loads(MANIFEST.read_text()) if lifecycle else await seed(repo)
        if lifecycle:
            await code_link_lifecycle(repo, entries)
        await verify(repo, entries)
    finally:
        await client.disconnect()


if __name__ == "__main__":
    asyncio.run(main())
