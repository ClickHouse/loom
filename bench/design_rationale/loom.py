"""What the harness reads and writes in its own Loom: the arms' namespaces, the
documents, the gold rows, the extraction run and the surfaces an agent would see.

Every call goes to the server given on the command line, a Loom server you started for
this run (never one found listening). `extract` runs `loom decisions extract --apply` in
a child process from this checkout, so it needs the server's ClickHouse and provider
environment.
"""

from __future__ import annotations

import re
import subprocess
import sys
from datetime import date, timedelta
from typing import Any

import httpx

from bench._http import post_json
from bench.bench_namespaces import (
    erase_bench_namespaces_sync,
    register_body,
    remember_bench_namespace,
)

AGENT = "bench-design-rationale"
# The agent identity a gold row is written under: one author for every row.
GOLD_AGENT = "design-author"
CHARS_PER_TOKEN = 4  # the server's estimate (memory.for_file tokens_estimate)
# The knowledge hits an agent reads in a loom_search answer: the tool's default top_k.
KNOWLEDGE_HITS_SHOWN = 5
# Seconds one call to the harness's Loom may take: an extraction-sized upsert included.
LOOM_TIMEOUT_S = 180.0
# Seconds one code.symbol lookup on the code index may take.
CODE_INDEX_TIMEOUT_S = 120.0
# The pre-edit block's token budget per file, as the coding-agent hook asks for it.
PRE_EDIT_BUDGET = 300
# One document's extraction: a model call per window, each with its own provider
# timeout.
EXTRACT_TIMEOUT_S = 3600


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


def knowledge_section(text: str, results: list[dict[str, Any]]) -> str:
    """The knowledge hits of a loom_search, with the count line. Rendered from the
    structured result (source, section, chunk text): the text summary prints a knowledge
    hit's `content_excerpt`, which knowledge hits do not carry, so it lists bare
    sources."""
    if not results:
        return ""
    lines = [text.split("\n", 1)[0], "Knowledge hits:"]
    for h in results[:KNOWLEDGE_HITS_SHOWN]:
        where = h["source_uri"]
        # A chunk above the first heading has no section.
        if h.get("section"):
            where += "#" + str(h["section"])
        body = " ".join(str(h["content"]).split())
        lines.append(f"  - [{where}] {body}")
    return "\n".join(lines)


def row_shown(rendered: str, key: str) -> bool:
    """Whether a memory.for_file block prints the row of `key`: a row of the caller's
    namespace opens `- <key>` then its marks or `:`, a team row ends `[id: <key>]`
    (server/memory/app/for_file.py)."""
    own = f"- {key}"
    for line in rendered.splitlines():
        if line.startswith(own) and line[len(own) : len(own) + 1] in (":", " "):
            return True
        if line.endswith(f"[id: {key}]"):
            return True
    return False


def section_keys(section: str) -> list[str]:
    """The memory keys a memory section served: reader-block paragraphs
    (`[date] [key] (type) ...`) or plain hit lines (`- [key] (ns=...)`)."""
    keys = re.findall(r"^\[[^\]]+\] \[([^\]]+)\]", section, re.M)
    keys += re.findall(r"^\s+- \[([^\]]+)\] \(ns=", section, re.M)
    return list(dict.fromkeys(keys))


def _text(output: bytes | str | None) -> str:
    if isinstance(output, bytes):
        return output.decode(errors="replace")
    return output or ""


class Loom:
    """Each arm's namespace lives in its own org: a search fans out over every namespace
    of its org, so arms sharing one would read each other's rows."""

    def __init__(self, base_url: str, token: str = ""):
        self.base_url = base_url.rstrip("/")
        self.token = token
        self.client = httpx.Client(timeout=LOOM_TIMEOUT_S)
        self.orgs: dict[str, str] = {}

    def bind(self, namespace: str, org: str) -> None:
        self.orgs[namespace] = org

    def close(self) -> None:
        self.client.close()

    def op(self, name: str, body: dict[str, Any]) -> Any:
        return self._post(f"/v1/{name}", {"org": self.orgs[body["namespace"]], **body})

    def _post(self, path: str, body: dict[str, Any]) -> Any:
        return post_json(
            self.client,
            self.base_url + path,
            body,
            token=self.token,
            attempts=3,
            timeout_s=LOOM_TIMEOUT_S,
        )

    # ---- namespaces

    def register(self, namespace: str, description: str, cap_usd: float) -> None:
        """A bench namespace whose daily cost cap is the run's cap, so the server's own
        model calls on its writes and reads stop there too. A refusal stops the run: a
        namespace without the cap would let the server spend past it. Registered as kept
        (`keep`): the idle gc would empty a namespace a resumed run still reads, and the
        harness erases its own."""
        org = self.orgs[namespace]
        extra = {"daily_cost_cap_usd": cap_usd} if cap_usd > 0 else None
        body = register_body(org, namespace, description=description, keep=True, extra=extra)
        self._post("/v1/namespace.register", body)
        remember_bench_namespace(org, namespace)

    def erase(self, namespaces: list[str]) -> bool:
        """Erase each namespace; True when every erasure was accepted. The shared helper
        logs a refused one as a warning with the command that erases it: with no logging
        configured, Python's last-resort handler prints it on stderr."""
        accepted = True
        for ns in namespaces:  # every namespace is tried, even after a refusal
            if not erase_bench_namespaces_sync(self._post, org=self.orgs[ns], namespaces=[ns]):
                accepted = False
        return accepted

    def server_spend(self, namespaces: list[str], since: date) -> float:
        """USD the server spent on model and embedding calls in these namespaces' orgs
        from the day before `since` (the run's first local day) to the day after today,
        as its own cost ledger records it."""
        total = 0.0
        for org in sorted({self.orgs[ns] for ns in namespaces}):
            # Every class: a worker's cost row on the run's rows has none, and the orgs
            # are the run's alone. One day wider at each end: the ledger's days are UTC,
            # ours local.
            body = {
                "org": org,
                "from_date": str(since - timedelta(days=1)),
                "to_date": str(date.today() + timedelta(days=1)),
            }
            out = self._post("/v1/cost.summary", body)
            total += float(out["total_cost_usd"])
        return total

    # ---- writes

    def upsert_document(self, namespace: str, source_uri: str, title: str, content: str) -> None:
        self.op(
            "knowledge.upsert",
            {"namespace": namespace, "source_uri": source_uri, "title": title, "content": content},
        )

    def write_gold(self, namespace: str, row: Any, source_resource_id: str) -> None:
        """One write as the author would: memory.pin for a pinned row (a human stands
        behind it; memory.pin is memory.set with the pin, and re-pins a row written
        again after a revision), memory.set otherwise."""
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
        self.op("memory.pin" if row.pinned else "memory.set", body)

    def extract(self, namespace: str, source_uri: str, window_chars: int) -> tuple[int, str]:
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
            "--window-chars",
            str(window_chars),
            "--apply",
        ]
        try:
            done = subprocess.run(
                cmd, capture_output=True, text=True, check=False, timeout=EXTRACT_TIMEOUT_S
            )
        except subprocess.TimeoutExpired as e:
            # The partial output arrives as bytes even in text mode.
            stopped = f"\nerror: stopped after {EXTRACT_TIMEOUT_S} s"
            return 1, _text(e.stdout) + _text(e.stderr) + stopped
        return done.returncode, done.stdout + done.stderr

    def decision_reasoning(self, namespace: str) -> dict[str, str]:
        """memory_key -> reasoning of each active decision record in the namespace."""
        out = self.op(
            "memory.record.query",
            {"namespace": namespace, "schema_name": "decision", "limit": 1000},
        )
        return {
            # A decision may be recorded with no reasoning (a ban).
            r["memory_key"]: str(r["record"].get("reasoning") or "")
            for r in out["rows"]
        }

    def set_files(
        self, namespace: str, key: str, files: list[str], reasoning: str
    ) -> list[str] | None:
        """Add `files` to an extracted row: a new version under the extractor's identity
        with every other field as read. The extractor also links the paths its quote and
        reasoning spell out; neither the row's files nor the quote can be read back, so
        the paths the record's reasoning spells out are kept. The row's files as
        written, or None when the row is gone."""
        from server.memory.domain.files import detect_value_files, normalize_files

        row = self.memory_get(namespace, key)
        if not row["found"]:
            return None
        merged = normalize_files([*detect_value_files(reasoning), *files])
        body = {
            "namespace": namespace,
            "agent": row["agent"],
            "user_id": row["user_id"],
            "session_id": row["session_id"],
            "memory_key": key,
            "memory_type": row["memory_type"],
            "value": row["value"],
            "tags": row["tags"],
            "entities": row["entities"],
            "files": merged,
            "confidence": row["confidence"],
            "source_resource_id": row["source_resource_id"],
            "source_trace": row["source_trace"],
            "source_class": "derived",
        }
        self.op("memory.set", body)
        return merged

    # ---- reads

    def for_file(self, namespace: str, path: str) -> dict[str, Any]:
        out = self.op(
            "memory.for_file",
            {"namespace": namespace, "path": path, "token_budget": PRE_EDIT_BUDGET},
        )
        rendered = out["rendered"]
        # Only the rows the block prints: the budget or the whole-row ceiling may leave
        # some hits out, and a left-out key is still named on the closing line.
        keys = [h["memory_key"] for h in out["hits"] if row_shown(rendered, h["memory_key"])]
        return {
            "path": path,
            "rendered": rendered,
            "tokens": out["tokens_estimate"],
            "truncated": out["truncated"],
            "keys": keys,
        }

    def search(self, namespace: str, query: str, session: str) -> dict[str, Any]:
        """The loom_search tool's answer as an MCP client gets it: {"text": the summary,
        "knowledge": the structured knowledge hits}."""
        arguments = {"org": self.orgs[namespace], "namespace": namespace, "query": query}
        body = {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "tools/call",
            "params": {"name": "loom_search", "arguments": arguments},
        }
        headers = {"Accept": "application/json, text/event-stream", "Mcp-Session-Id": session}
        if self.token:
            headers["Authorization"] = f"Bearer {self.token}"
        r = self.client.post(self.base_url + "/mcp", json=body, headers=headers)
        r.raise_for_status()
        reply = r.json()
        if "error" in reply:
            raise RuntimeError(f"loom_search failed: {reply['error']}")
        result = reply["result"]
        if result.get("isError"):
            raise RuntimeError(f"loom_search failed: {result['content'][0]['text']}")
        # The lean structured result leaves out a section with no hits.
        knowledge = result["structuredContent"].get("knowledge") or {}
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


class CodeIndex:
    """Read-only `code.symbol` lookups against the Loom that indexes the repository, for
    the files of extracted rows: the harness's own Loom has no code index, and an arm's
    org must not share one with another arm."""

    def __init__(self, base_url: str, org: str, namespace: str, token: str):
        self.base_url = base_url.rstrip("/")
        self.org = org
        self.namespace = namespace
        self.token = token
        self.client = httpx.Client(timeout=CODE_INDEX_TIMEOUT_S)

    def close(self) -> None:
        self.client.close()

    def definitions(self, name: str) -> list[tuple[str, str]] | None:
        """(path, qualified name) of each definition; None when the index did not
        answer."""
        body = {
            "org": self.org,
            "namespace": self.namespace,
            "name": name,
            "include_body": False,
            "include_history": False,
            "include_examples": False,
            "include_lessons": False,
        }
        try:
            out = post_json(
                self.client,
                self.base_url + "/v1/code.symbol",
                body,
                token=self.token,
                attempts=3,
                timeout_s=CODE_INDEX_TIMEOUT_S,
            )
        except httpx.HTTPError:
            return None
        return [(d["path"], d["symbol"]) for d in out["definitions"]]
