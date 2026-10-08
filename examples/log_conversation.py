"""Log a conversation to Loom from your own chat loop — the reliable path.

Loom does NOT auto-capture turns: there is no run/chat loop in the SDK, and
MCP only sees tool calls the model chooses to make. When you own the chat
loop, you record each turn deterministically with one `conversations.append`
call per message — nothing is ever missed. The logged thread shows up in
`/me` → Sessions, and the background seeder mines memory candidates from it.

Run against a local server:

    export LOOM_API_KEY=$(cat ~/.loom/token)
    export LOOM_ORG=dev LOOM_NAMESPACE=examples LOOM_AGENT=sdk-demo   # the defaults
    python examples/log_conversation.py
"""

from __future__ import annotations

import asyncio
import os
import uuid

from loom import Agent

# A real chat loop would interleave these with model calls; the point is that
# the turns are persisted as they happen, not reconstructed afterward.
TURNS = [
    ("user", "How do I make a column-level TTL in ClickHouse?"),
    (
        "assistant",
        "Add `TTL <time_col> + INTERVAL 30 DAY` to the column in the "
        "CREATE TABLE (or via ALTER MODIFY COLUMN); a background merge "
        "resets expired values to the column default.",
    ),
    ("user", "Does that delete the row?"),
    (
        "assistant",
        "No — column TTL only clears that column's value. To drop whole "
        "rows use a table-level `TTL <time_col> + INTERVAL ...` instead.",
    ),
]


async def main() -> None:
    thread_id = f"sdk-demo-{uuid.uuid4().hex[:8]}"
    async with Agent(
        org=os.environ.get("LOOM_ORG", "dev"),
        namespace=os.environ.get("LOOM_NAMESPACE", "examples"),
        agent=os.environ.get("LOOM_AGENT", "sdk-demo"),
        base_url=os.environ.get("LOOM_BASE_URL", "http://localhost:7777"),
        api_key=os.environ.get("LOOM_API_KEY"),
    ) as agent:
        # Optional — append() creates the thread implicitly, but create() lets
        # you set a title up front so Sessions shows something readable.
        await agent.conversations.create(thread_id, title="ClickHouse TTL Q&A")
        for role, content in TURNS:
            # One append per message; msg_id is caller-minted so retries dedup.
            await agent.conversations.append(thread_id, uuid.uuid4().hex, role, content)

    print(f"Logged {len(TURNS)} turns to thread {thread_id} — open /me → Sessions.")


if __name__ == "__main__":
    asyncio.run(main())
