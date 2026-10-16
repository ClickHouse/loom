"""Cross-tool memory validation.

Verifies the core promise: memory written by tool A is recalled by
tool B. The end-to-end path is:

  1. Seed N representative memories into the target namespace, tagged
     with agent='claude-desktop' (as if Claude Desktop had written
     them over a normal week of conversations).
  2. Search those memories using a token whose agent='cursor', via
     the same /v1/memory.search the MCP transport calls. We're
     specifically testing the *cross-agent read* path, not the
     same-agent read path which is trivially working.
  3. Score: for each seed memory, does a natural-language question
     about its content surface the right memory in the top-3?

Output a per-query report. 80% top-3 recall is the regression
baseline: below it the cross-agent search path is the bug, and the run
exits 1.

The tokens are minted in-process with this checkout's signing key, so the
target must be a server that shares it: `--base-url` (or LOOM_BASE_URL) is
required, with no default.

Run via:
    python3 bench/cross_tool_validate.py --base-url http://localhost:7777 \
        --namespace <your-namespace> [--seed]

`--seed` first writes the test memories. Re-running without `--seed`
re-uses what's there. Memories are tagged `tags=['x-tool-bench']` so
they're easy to clean up afterwards.
"""

from __future__ import annotations

import argparse
import asyncio
import os
import sys
from dataclasses import dataclass

import httpx

# The repo root on the path for in-process token minting.
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from server.auth import keyring
from server.auth.jwt import mint_token

MIN_TOP3_RECALL = 0.8

# Representative memories an engineer might tell Claude Desktop over a
# week. Cover: stable identity (employer, role), preferences (coffee,
# editor), relationships (pet), domain context (current project).
SEEDS: list[dict[str, object]] = [
    {
        "memory_key": "user_employer",
        "value": "ClickHouse Inc.",
        "tags": ["x-tool-bench", "work"],
    },
    {
        "memory_key": "user_role",
        "value": "Software engineer on the platform team",
        "tags": ["x-tool-bench", "work"],
    },
    {
        "memory_key": "user_coffee_pref",
        "value": "Espresso, no sugar, oat milk",
        "tags": ["x-tool-bench", "preference"],
    },
    {
        "memory_key": "user_dog_name",
        "value": "User's dog is named Bowie, a 7-year-old border collie",
        "tags": ["x-tool-bench", "personal"],
    },
    {
        "memory_key": "user_editor",
        "value": "Primary editor is Cursor; uses Claude Code for terminal-driven work",
        "tags": ["x-tool-bench", "preference", "tools"],
    },
    {
        "memory_key": "user_timezone",
        "value": "America/Los_Angeles (UTC-7)",
        "tags": ["x-tool-bench", "personal"],
    },
]

# The actual test: each query is a natural question someone might
# ask in a fresh conversation. The expected_key is the memory that
# should win on retrieval if cross-tool recall is working.
QUERIES: list[tuple[str, str]] = [
    ("where do I work", "user_employer"),
    ("what's my job", "user_role"),
    ("how do I take my coffee", "user_coffee_pref"),
    ("what's my dog's name", "user_dog_name"),
    ("what editor do I use", "user_editor"),
    ("what timezone am I in", "user_timezone"),
]


@dataclass
class QueryResult:
    query: str
    expected_key: str
    hit_at: int | None  # rank of the expected key in results (1-based); None = not in top-N
    top3_keys: list[str]


def _token(*, subject: str, role: str, org: str, namespace: str, service_account: bool) -> str:
    """A one-hour JWT scoped to the one namespace. The agent dimension is the
    `agent` field of each request body, not a claim."""
    keyring.load_for_offline_mint()
    return mint_token(
        subject=subject,
        org=org,
        namespaces=[namespace],
        roles=[role],
        ttl_seconds=3600,
        is_service_account=service_account,
    )


async def seed(
    client: httpx.AsyncClient, base_url: str, token: str, org: str, namespace: str
) -> None:
    """Write the seed memories as agent='claude-desktop'."""
    headers = {"Authorization": f"Bearer {token}"}
    for s in SEEDS:
        body = {
            "org": org,
            "namespace": namespace,
            "agent": "claude-desktop",
            "user_id": "-",
            "session_id": "-",
            "memory_key": s["memory_key"],
            "value": s["value"],
            "memory_type": "semantic",
            "tags": s["tags"],
        }
        r = await client.post(f"{base_url}/v1/memory.set", headers=headers, json=body)
        r.raise_for_status()
        out = r.json()
        print(f"  seed: {s['memory_key']:<22} → v{out.get('version', '?')}")


async def search_as_cursor(
    client: httpx.AsyncClient,
    base_url: str,
    token: str,
    org: str,
    namespace: str,
    query: str,
) -> list[str]:
    """Search Loom THROUGH THE MCP TRANSPORT — the path a real agent
    uses. MCP auto-wildcards agent on read ops so a Cursor-bound
    token finds Claude-Desktop-written memories. Hitting /v1/memory.search
    directly bypasses that defaulting and gives misleading results.

    The token's sub is sa:cursor-* so the JWT's identity is the
    cursor agent; MCP routes the call with agent='*' under the
    hood. Returns the top-3 memory_keys."""
    headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}
    rpc = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "tools/call",
        "params": {
            "name": "loom_search",
            "arguments": {"query": query, "top_k": 3},
        },
    }
    r = await client.post(f"{base_url}/mcp", headers=headers, json=rpc)
    r.raise_for_status()
    payload = r.json()
    # loom_search returns structuredContent.{memory, profile, knowledge,
    # conversations}; the bench measures cross-tool MEMORY retrieval, so
    # pull just the memory store's hits.
    structured = payload.get("result", {}).get("structuredContent", {})
    mem = structured.get("memory") or {}
    return [hit["memory_key"] for hit in mem.get("results") or []]


async def run(base_url: str, namespace: str, org: str, do_seed: bool) -> int:
    # Writer side: a personal-user JWT writing as agent='claude-desktop'.
    writer = _token(
        subject="bench-writer", role="writer", org=org, namespace=namespace, service_account=False
    )
    # Reader side: a service-account-shaped sub, so MCP treats the caller as
    # programmatic and applies its agent-wildcard default, as it does for a
    # Cursor MCP token (sub sa:cursor-<hash>).
    reader = _token(
        subject="sa:cursor-bench", role="viewer", org=org, namespace=namespace, service_account=True
    )

    async with httpx.AsyncClient(timeout=30.0) as client:
        if do_seed:
            print(f"=== Seeding {len(SEEDS)} memories as agent='claude-desktop' ===")
            await seed(client, base_url, writer, org, namespace)
            print()

        print("=== Searching as agent='cursor' (the cross-tool read) ===")
        results: list[QueryResult] = []
        for query, expected_key in QUERIES:
            top3 = await search_as_cursor(client, base_url, reader, org, namespace, query)
            hit_at = (top3.index(expected_key) + 1) if expected_key in top3 else None
            results.append(QueryResult(query, expected_key, hit_at, top3))
            marker = "✓" if hit_at == 1 else ("o" if hit_at else "✗")
            rank = f"#{hit_at}" if hit_at else "miss"
            print(
                f"  {marker} {query!r:<40} → expected={expected_key!r} got_rank={rank} top3={top3}"
            )
        print()

    hits = sum(1 for r in results if r.hit_at is not None)
    top1 = sum(1 for r in results if r.hit_at == 1)
    print(f"=== Summary: top-1={top1}/{len(results)} top-3={hits}/{len(results)} ===")
    misses = [r.query for r in results if r.hit_at is None]
    if misses:
        print(f"MISSES ({len(misses)}): {misses}")
    if not results or hits / len(results) < MIN_TOP3_RECALL:
        print(
            f"FAIL top-3 recall below {MIN_TOP3_RECALL:.0%}: the cross-agent read path lost writes"
        )
        return 1
    return 0


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    p.add_argument(
        "--base-url",
        default=os.environ.get("LOOM_BASE_URL", ""),
        help="server that shares this checkout's signing key (default LOOM_BASE_URL; required)",
    )
    p.add_argument("--namespace", required=True, help="Loom namespace to read/write seed memories.")
    p.add_argument("--org", default="bench-org", help="Loom org to scope the JWTs to.")
    p.add_argument(
        "--seed",
        action="store_true",
        help="(Re-)write the seed memories before searching.",
    )
    args = p.parse_args(argv)
    if not args.base_url:
        p.error("--base-url (or LOOM_BASE_URL) is required")
    return asyncio.run(run(args.base_url.rstrip("/"), args.namespace, args.org, args.seed))


if __name__ == "__main__":
    sys.exit(main())
