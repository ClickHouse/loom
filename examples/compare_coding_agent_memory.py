"""Run the coding-agent memory comparison on a disposable local Loom."""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import secrets
import select
import shutil
import subprocess
import sys
import tempfile
from collections.abc import Callable
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

import clickhouse_connect
import httpx

from server.auth import keyring
from server.auth.jwt import mint_token
from server.cli.sql_access import _login_check
from server.code.app.index.gitrepo import auth_env
from server.code.app.index.repo_registry import refresh_db_repos
from server.code.app.repo_admin import _register
from server.code.store.index import CodeRepo
from server.code.workers import ingest
from server.core.ch import get_client
from server.insights.app.sql_access import grant, revoke
from server.insights.domain.sql_surface import Scope
from server.memory.workers import code_links
from server.settings import settings

NAMESPACE = "retry-policy"
KEY = "decision/retry-policy"
PATH = "retry.py"
WRITER = "fixture-writer"
REVIEWER = "fixture-reviewer"
ORIGINAL = "`should_retry` retries on 503."
UPDATED = "`should_retry` retries on 429."


def git(repo: Path, *args: str) -> str:
    return subprocess.check_output(
        ["git", "-c", "core.hooksPath=/dev/null", "-c", "commit.gpgsign=false", *args],
        cwd=repo,
        env=auth_env(remote=str(repo)),
        text=True,
        stderr=subprocess.PIPE,
        timeout=20,
    ).strip()


def save(repo: Path, filename: str, content: str, author: str) -> str:
    (repo / filename).write_text(content + "\n")
    git(repo, "add", filename)
    git(
        repo,
        "-c",
        f"user.name={author}",
        "-c",
        "user.email=fixture@example.invalid",
        "commit",
        "-qm",
        filename,
    )
    return git(repo, "rev-parse", "HEAD")


def source(repo: Path, status: int) -> str:
    return save(
        repo, PATH, f"def should_retry(status: int) -> bool:\n    return status == {status}", WRITER
    )


class Stdio:
    def __init__(self, script: Path, storage: Path) -> None:
        self.proc = subprocess.Popen(
            ["node", str(script)],
            env={**os.environ, "MEMORY_FILE_PATH": str(storage)},
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            text=True,
        )

    def request(self, packet: dict[str, Any]) -> dict[str, Any]:
        assert self.proc.stdin and self.proc.stdout
        self.proc.stdin.write(json.dumps(packet) + "\n")
        self.proc.stdin.flush()
        if "id" not in packet:
            return {}
        if not select.select([self.proc.stdout], [], [], 10)[0]:
            raise TimeoutError("The local stdio MCP server did not answer")
        return json.loads(self.proc.stdout.readline())["result"]

    def close(self) -> None:
        self.proc.terminate()
        try:
            self.proc.communicate(timeout=5)
        except subprocess.TimeoutExpired:
            self.proc.kill()
            self.proc.communicate()


class MCP:
    def __init__(self, request: Callable[[dict[str, Any]], dict[str, Any]], name: str) -> None:
        self.request = request
        self.counter = 0
        self.call(
            "initialize",
            {
                "protocolVersion": "2025-11-25",
                "capabilities": {},
                "clientInfo": {"name": name, "version": "1"},
            },
        )
        request({"jsonrpc": "2.0", "method": "notifications/initialized"})

    def call(self, method: str, params: dict[str, Any]) -> dict[str, Any]:
        self.counter += 1
        return self.request(
            {"jsonrpc": "2.0", "id": self.counter, "method": method, "params": params}
        )

    def tool(self, name: str, **arguments: Any) -> Any:
        result = self.call("tools/call", {"name": name, "arguments": arguments})
        assert result.get("isError") is not True, result
        return result.get("structuredContent") or json.loads(result["content"][0]["text"])


def graph_tool(script: Path, storage: Path, client: str, name: str, **arguments: Any) -> Any:
    process = Stdio(script, storage)
    try:
        return MCP(process.request, client).tool(name, **arguments)
    finally:
        process.close()


class Loom:
    def __init__(
        self, origin: str, org: str, name: str, role: str, *, namespace: str = NAMESPACE
    ) -> None:
        token = mint_token(
            subject=name, org=org, namespaces=[namespace], roles=[role], ttl_seconds=600
        )
        self.client = httpx.Client(
            base_url=origin,
            timeout=15,
            trust_env=False,
            headers={
                "Authorization": f"Bearer {token}",
                "Accept": "application/json, text/event-stream",
            },
        )
        self.mcp = MCP(self.request, name)

    def request(self, packet: dict[str, Any]) -> dict[str, Any]:
        response = self.client.post("/mcp", json=packet)
        response.raise_for_status()
        if session := response.headers.get("Mcp-Session-Id"):
            self.client.headers["Mcp-Session-Id"] = session
        if "id" not in packet:
            return {}
        return response.json()["result"]

    def close(self) -> None:
        try:
            if "Mcp-Session-Id" in self.client.headers:
                self.client.delete("/mcp").raise_for_status()
        finally:
            self.client.close()


async def index(org: str, head: str) -> None:
    for _ in range(35):
        result = await ingest.run_pass()
        assert not result.errors, result.errors
        if await CodeRepo().get_state(org, NAMESPACE, "git_head") == head:
            return
        await asyncio.sleep(1)
    raise TimeoutError("The disposable repository head was not indexed")


async def code_state(loom: Loom, state: str) -> dict[str, Any]:
    for _ in range(35):
        result = await code_links.run_pass()
        assert not result["errors"], result["errors"]
        memory = loom.mcp.tool("loom_memory_get", namespace=NAMESPACE, memory_key=KEY)
        for link in memory.get("code", []):
            if link["path"] == PATH and link["state"] == state:
                return link
        await asyncio.sleep(1)
    raise TimeoutError(f"The stored memory did not return a {state} code link: {memory}")


def read_history(user: str, password: str, org: str) -> list[tuple[Any, ...]]:
    with clickhouse_connect.get_client(
        host=settings.ch_host,
        port=settings.ch_port,
        username=user,
        password=password,
        database=settings.ch_database,
    ) as sql:
        return sql.query(
            "SELECT version, actor, value FROM {db:Identifier}.sql_v1_memory_history "
            "WHERE org = {org:String} AND namespace = {namespace:String} "
            "AND memory_key = {key:String} ORDER BY version",
            parameters={"db": settings.ch_database, "org": org, "namespace": NAMESPACE, "key": KEY},
        ).result_rows


async def sql_history(org: str) -> list[tuple[Any, ...]]:
    user = "comparison_" + secrets.token_hex(5)
    credential = await grant(user, Scope(org, (NAMESPACE,)), login_check=_login_check)
    try:
        assert credential.password and credential.rows == {(org, NAMESPACE)}
        return await asyncio.to_thread(read_history, user, credential.password, org)
    finally:
        await revoke(user)


async def compare(origin: str, graph: Path, directory: Path) -> dict[str, Any]:
    repo = directory / "source"
    repo.mkdir()
    git(repo, "init", "-qb", "main")
    first = source(repo, 503)
    note = f"{ORIGINAL} Check {PATH} at `{first}`."
    save(repo, "AGENTS.md", note, WRITER)
    clone = directory / "review"
    git(directory, "clone", "-q", str(repo), str(clone))
    assert (clone / "AGENTS.md").read_text().strip() == note
    storage = directory / "graph.jsonl"
    graph_tool(
        graph,
        storage,
        WRITER,
        "create_entities",
        entities=[{"name": KEY, "entityType": "decision", "observations": [note]}],
    )
    assert graph_tool(graph, storage, REVIEWER, "open_nodes", names=[KEY])["entities"][0][
        "observations"
    ] == [note]
    org = "comparison-" + secrets.token_hex(5)
    await refresh_db_repos(force=True)
    await _register(
        org=org,
        namespace=NAMESPACE,
        repo="example/retry",
        git_remote=str(repo),
        provider="local",
        branch="main",
        classification="standard",
        join_policy="open",
        github_token="",
        path_prefixes=[""],
        exclude=[],
        remote_head=git(repo, "rev-parse", "HEAD"),
        scip_producer="python",
        exclude_generated=False,
    )
    await refresh_db_repos(force=True)
    await index(org, git(repo, "rev-parse", "HEAD"))
    writer = Loom(origin, org, WRITER, "writer")
    try:
        memory = writer.mcp.tool("loom_memory_set", namespace=NAMESPACE, memory_key=KEY, value=note)
    finally:
        writer.close()
    reviewer = Loom(origin, org, REVIEWER, "writer")
    try:
        read = reviewer.mcp.tool("loom_memory_get", namespace=NAMESPACE, memory_key=KEY)
        assert (
            read["found"] and read["memory_id"] == memory["memory_id"] and read["value"] == note
        ), read
        check_project_boundary(origin, org, reviewer, memory["memory_id"])
        await code_state(reviewer, "fresh")
        second = source(repo, 429)
        git(clone, "pull", "-q", "--ff-only")
        assert (clone / "AGENTS.md").read_text().strip() == note and git(
            clone, "diff", first, second, PATH
        )
        graph_note = graph_tool(graph, storage, REVIEWER, "open_nodes", names=[KEY])["entities"][0]
        assert graph_note["observations"] == [note]
        await index(org, second)
        changed = await code_state(reviewer, "changed")
        assert changed["changed_since"] == second, changed
        revised = f"{UPDATED} Check {PATH} at `{second}`."
        result = reviewer.mcp.tool(
            "loom_op",
            op="memory.update_by_id",
            arguments={"namespace": NAMESPACE, "memory_id": memory["memory_id"], "value": revised},
        )
        assert result["updated"], result
    finally:
        reviewer.close()
    save(repo, "AGENTS.md", revised, REVIEWER)
    assert git(repo, "log", "-2", "--format=%an", "--", "AGENTS.md").splitlines() == [
        REVIEWER,
        WRITER,
    ]
    graph_tool(
        graph,
        storage,
        REVIEWER,
        "add_observations",
        observations=[{"entityName": KEY, "contents": [revised]}],
    )
    observations = graph_tool(graph, storage, REVIEWER, "open_nodes", names=[KEY])["entities"][0]
    assert observations["observations"] == [note, revised], observations
    rows = await sql_history(org)
    assert len(rows) == 2 and 0 < rows[0][0] < rows[1][0], rows
    assert [(actor, json.loads(value)) for _, actor, value in rows] == [
        (f"process:{WRITER}", note),
        (f"process:{REVIEWER}", revised),
    ], rows
    git_copy = directory / "restored-repo"
    git(directory, "clone", "-q", "--no-hardlinks", str(repo), str(git_copy))
    assert (git_copy / "AGENTS.md").read_text().strip() == revised
    assert git(git_copy, "show", "HEAD~1:AGENTS.md") == note

    graph_copy = directory / "restored-graph.jsonl"
    shutil.copyfile(storage, graph_copy)
    copied = graph_tool(graph, graph_copy, "restored-reader", "open_nodes", names=[KEY])
    assert copied["entities"][0]["observations"] == [note, revised], copied

    import_actor = check_loom_portability(origin, org, directory / "memory-bundle", revised)
    return {
        "source_before": first,
        "source_after": second,
        "git_authors": [WRITER, REVIEWER],
        "graph_fields": sorted(observations),
        "loom_denied_other_project": True,
        "loom_changed_since": changed["changed_since"],
        "loom_sql_versions": [[row[0], row[1]] for row in rows],
        "portable_copies": {
            "git_note_versions": 2,
            "mcp_observations": len(copied["entities"][0]["observations"]),
            "loom_import_actor": import_actor,
        },
    }


def check_project_boundary(origin: str, org: str, reviewer: Loom, memory_id: str) -> None:
    arguments = {"org": org, "namespace": NAMESPACE, "agent": REVIEWER, "memory_key": KEY}
    authorized = reviewer.client.post("/v1/memory.get", json=arguments)
    authorized.raise_for_status()
    assert authorized.json()["memory_id"] == memory_id

    other = Loom(origin, org, "other-project-reader", "viewer", namespace="other-project")
    try:
        response = other.client.post("/v1/memory.get", json=arguments)
        assert response.status_code == 403, response.status_code
    finally:
        other.close()


def run_okf(origin: str, org: str, namespace: str, name: str, role: str, *arguments: str) -> None:
    token = mint_token(subject=name, org=org, namespaces=[namespace], roles=[role], ttl_seconds=600)
    subprocess.run(
        [
            sys.executable,
            "-m",
            "server.cli",
            *arguments,
            "--server-url",
            origin,
            "--org",
            org,
            "--namespace",
            namespace,
        ],
        env={**os.environ, "LOOM_API_KEY": token},
        stdout=subprocess.PIPE,
        check=True,
        timeout=60,
    )


def check_loom_portability(origin: str, org: str, bundle: Path, expected: str) -> str:
    target = "restored-memory"
    importer = "fixture-importer"
    reader = Loom(origin, org, "restored-reader", "viewer", namespace=target)
    try:
        assert not reader.mcp.tool("loom_memory_get", namespace=target, memory_key=KEY)["found"]
        run_okf(
            origin,
            org,
            NAMESPACE,
            "export-viewer",
            "viewer",
            "export-okf",
            "--out",
            str(bundle),
            "--no-evidence",
            "--no-links",
        )
        assert any(expected in file.read_text() for file in bundle.rglob("*.md"))
        run_okf(
            origin,
            org,
            target,
            importer,
            "writer",
            "import-okf",
            str(bundle),
            "--agent",
            importer,
            "--no-evidence",
        )
        restored = reader.mcp.tool("loom_memory_get", namespace=target, memory_key=KEY)
        assert restored["found"] and restored["value"] == expected, restored
        actor = str(restored["actor"])
        assert actor == f"process:{importer}", restored
        return actor
    finally:
        reader.close()


async def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--graph-memory", type=Path, required=True)
    args = parser.parse_args()
    origin = os.environ["LOOM_BASE_URL"].rstrip("/")
    assert urlsplit(origin).hostname in {"127.0.0.1", "localhost"}, (
        "Use a local disposable deployment"
    )
    assert settings.ch_host in {"127.0.0.1", "localhost"}, "Use a local disposable database"
    async with httpx.AsyncClient(base_url=origin, timeout=5, trust_env=False) as client:
        health = (await client.get("/v1/health")).json()
    label = os.environ["LOOM_INSTANCE_LABEL"]
    assert label.startswith("iso:coding-agent-comparison:")
    assert health["instance_label"] == label and health["fake_llm"], health
    keyring.sign_with_root_only()
    ch = get_client()
    await ch.connect()
    try:
        with tempfile.TemporaryDirectory(prefix="loom-coding-agent-comparison-") as path:
            print(
                json.dumps(await compare(origin, args.graph_memory.resolve(), Path(path)), indent=2)
            )
    finally:
        await ch.disconnect()


if __name__ == "__main__":
    asyncio.run(main())
