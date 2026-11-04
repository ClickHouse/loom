"""What the harness reads and writes in its own Loom: the arms' namespaces, the documents,
the gold rows, the extraction run and the surfaces an agent would see.

Every call goes to the server given on the command line, one the run started itself
(never one found listening). `extract` runs `loom decisions extract --apply` in a child
process from this checkout, so it needs the server's ClickHouse and provider environment.
"""

from __future__ import annotations

import re
import subprocess
import sys
from typing import Any

import httpx

from bench._http import post_json
from bench.bench_namespaces import (
    erase_bench_namespaces_sync,
    register_bench_namespace_sync,
    remember_bench_namespace,
)

AGENT = "bench-design-rationale"
# The agent identity a gold row is written under: one author for every row.
GOLD_AGENT = "design-author"
CHARS_PER_TOKEN = 4  # the server's estimate (memory.for_file tokens_estimate)


def tokens(text: str) -> int:
    return (len(text) + CHARS_PER_TOKEN - 1) // CHARS_PER_TOKEN if text else 0


def memory_section(text: str) -> str:
    """The memory part of the loom_search text an agent reads, with the count line."""
    starts = [
        i for m in ("\nReading rule", "\nHow to read", "\nMemory hits") if (i := text.find(m)) >= 0
    ]
    if not starts:
        return ""
    start = min(starts)
    ends = [
        i for m in ("\nKnowledge hits:", "\nConversation hits:") if (i := text.find(m, start)) >= 0
    ]
    body = text[start : min(ends) if ends else len(text)].strip()
    return text.split("\n", 1)[0] + "\n" + body


def knowledge_section(text: str, results: list[dict[str, Any]], top_k: int = 5) -> str:
    """The knowledge hits of a loom_search, with the count line. Rendered from the
    structured result (source, section, chunk text): the text summary prints a knowledge
    hit's `content_excerpt`, which knowledge hits do not carry, so it lists bare sources."""
    if not results:
        return ""
    lines = [text.split("\n", 1)[0], "Knowledge hits:"]
    for h in results[:top_k]:
        where = h.get("source_uri") or "-"
        if h.get("section"):
            where += "#" + str(h["section"])
        body = " ".join(str(h.get("content") or "").split())
        lines.append(f"  - [{where}] {body}")
    return "\n".join(lines)


def section_keys(section: str) -> list[str]:
    """The memory keys a memory section served: reader-block paragraphs
    (`[date] [key] (type) ...`) or plain hit lines (`- [key] (ns=...)`)."""
    keys = re.findall(r"^\[[^\]]+\] \[([^\]]+)\]", section, re.M)
    keys += re.findall(r"^\s+- \[([^\]]+)\] \(ns=", section, re.M)
    return list(dict.fromkeys(keys))


class Loom:
    """Each arm's namespace lives in its own org: a search fans out over every namespace
    of its org, so arms sharing one would read each other's rows."""

    def __init__(self, base_url: str, token: str = "", timeout_s: float = 180.0):
        self.base_url = base_url.rstrip("/")
        self.token = token
        self.client = httpx.Client(timeout=timeout_s)
        self.orgs: dict[str, str] = {}

    def bind(self, namespace: str, org: str) -> None:
        self.orgs[namespace] = org

    def close(self) -> None:
        self.client.close()

    def op(self, name: str, body: dict[str, Any]) -> Any:
        return self._post(f"/v1/{name}", {"org": self.orgs[body["namespace"]], **body})

    def _post(self, path: str, body: dict[str, Any]) -> Any:
        return post_json(self.client, self.base_url + path, body, token=self.token, attempts=3)

    # ---- namespaces

    def register(self, namespace: str, description: str, keep: bool) -> None:
        org = self.orgs[namespace]
        register_bench_namespace_sync(
            self._post, org=org, namespace=namespace, description=description, keep=keep
        )
        remember_bench_namespace(org, namespace)

    def erase(self, namespaces: list[str]) -> None:
        for ns in namespaces:
            erase_bench_namespaces_sync(self._post, org=self.orgs[ns], namespaces=[ns])

    # ---- writes

    def upsert_document(self, namespace: str, source_uri: str, title: str, content: str) -> None:
        self.op(
            "knowledge.upsert",
            {"namespace": namespace, "source_uri": source_uri, "title": title, "content": content},
        )

    def write_gold(self, namespace: str, row: Any, source_resource_id: str) -> None:
        """memory.set as the author would, then memory.pin with the same body when the
        row is pinned (a human stands behind it). A pinned row written again after a
        revision is re-pinned with its new value: memory.set never replaces a pin."""
        body = {
            "namespace": namespace,
            "agent": GOLD_AGENT,
            "memory_key": row.memory_key,
            "memory_type": row.memory_type,
            "value": row.value,
            "tags": row.tags,
            "files": row.files,
            "source_resource_id": source_resource_id,
        }
        if not (row.pinned and row.after_revision):
            self.op("memory.set", body)
        if row.pinned:
            self.op("memory.pin", body)

    def extract(self, namespace: str, source_uri: str, repo: str = "") -> tuple[int, str]:
        """`loom decisions extract --apply` on one document: (exit code, its report)."""
        cmd = [
            sys.executable,
            "-m",
            "server.cli",
            "decisions",
            "extract",
            "--org",
            self.orgs[namespace],
            "--namespace",
            namespace,
            "--source-uri",
            source_uri,
            "--apply",
        ]
        if repo:
            cmd += ["--repo", repo]
        done = subprocess.run(cmd, capture_output=True, text=True, check=False)
        return done.returncode, done.stdout + done.stderr

    # ---- reads

    def for_file(self, namespace: str, path: str, token_budget: int = 300) -> dict[str, Any]:
        out = self.op(
            "memory.for_file",
            {"namespace": namespace, "path": path, "token_budget": token_budget},
        )
        return {
            "path": path,
            "rendered": out.get("rendered") or "",
            "tokens": int(out.get("tokens_estimate") or 0),
            "truncated": bool(out.get("truncated")),
            "keys": [h["memory_key"] for h in out.get("hits") or []],
        }

    def search(self, namespace: str, query: str, session: str) -> dict[str, Any]:
        """The loom_search tool's answer as an MCP client gets it: {"text": the summary,
        "knowledge": the structured knowledge hits}."""
        body = {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "tools/call",
            "params": {
                "name": "loom_search",
                "arguments": {"org": self.orgs[namespace], "namespace": namespace, "query": query},
            },
        }
        headers = {"Accept": "application/json, text/event-stream", "Mcp-Session-Id": session}
        if self.token:
            headers["Authorization"] = f"Bearer {self.token}"
        r = self.client.post(self.base_url + "/mcp", json=body, headers=headers)
        r.raise_for_status()
        result = r.json()["result"]
        knowledge = (result.get("structuredContent") or {}).get("knowledge") or {}
        return {
            "text": str(result["content"][0]["text"]),
            "knowledge": list(knowledge.get("results") or []),
        }

    def knowledge_get(self, namespace: str, source_uri: str) -> dict[str, Any]:
        return dict(self.op("knowledge.get", {"namespace": namespace, "source_uri": source_uri}))

    def memory_get(self, namespace: str, key: str) -> dict[str, Any]:
        return dict(
            self.op("memory.get", {"namespace": namespace, "agent": AGENT, "memory_key": key})
        )
