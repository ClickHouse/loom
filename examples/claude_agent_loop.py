"""Claude + Loom agent loop — the minimal end-to-end memory integration.

The shape every Loom-backed agent shares, as one runnable REPL:

  1. PULL    — `profile.system_prompt()` gives Claude the durable persona;
               `memory.search(user_msg)` recalls facts relevant to this turn.
  2. REASON  — the Anthropic SDK call. Loom is never in this path: Claude
               spend goes straight to Anthropic; Loom only stores and recalls.
  3. WRITE   — `memory.set_from_messages(transcript)` at session end. The
               server's extraction LLM decides what was durable — no
               client-side "what should I remember" logic.

Durable facts go through `memory.set_from_messages` / `memory.set`, NOT
`working_memory` (scratchpad blocks are not embedded or searchable).

Run:
    pip install clickhouse-loom anthropic
    export LOOM_BASE_URL=http://localhost:7777
    export LOOM_API_KEY=$(cat ~/.loom/token)
    export LOOM_ORG=dev LOOM_NAMESPACE=examples   # the defaults
    export ANTHROPIC_API_KEY=sk-ant-...
    python examples/claude_agent_loop.py

Tell it something worth remembering ("we deploy Tue/Thu, never Friday"),
quit, rerun, and ask about it — the second session answers from memory.

MCP-capable clients (Claude Desktop / Claude Code / Cursor) don't need this
file at all: the MCP `core` toolset already covers the loop (docs/reference/mcp.md).
This is the pattern for headless and custom runtimes.
"""

from __future__ import annotations

import asyncio
import datetime as dt
import os
import sys

from loom import Agent

try:
    from anthropic import AsyncAnthropic
except ImportError:
    sys.exit("This example needs the Anthropic SDK: pip install anthropic")

# Pin whatever Opus is current; override without editing the file.
MODEL = os.environ.get("ANTHROPIC_MODEL", "claude-opus-5")
BASE_SYSTEM = "You are a helpful assistant with persistent memory."
USER_ID = os.environ.get("LOOM_USER_ID", "demo-user")


def _memories_block(hits) -> str:
    """Render recalled memories as a system-prompt section. Honour the
    server's abstain signal — weak hits are worse than no hits."""
    if not hits or hits.abstain:
        return ""
    lines = [f"- ({h.memory_key}) {h.content_excerpt}" for h in hits]
    return "<memories>\n" + "\n".join(lines) + "\n</memories>"


async def main() -> None:
    claude = AsyncAnthropic()  # reads ANTHROPIC_API_KEY
    async with Agent(
        org=os.environ.get("LOOM_ORG", "dev"),
        namespace=os.environ.get("LOOM_NAMESPACE", "examples"),
        agent="claude-loop",
    ) as agent:
        # The persona is stable across turns — fetch once per conversation,
        # not per turn (see ProfileClient.system_prompt for the rationale).
        persona = await agent.profile.system_prompt(base=BASE_SYSTEM, user_id=USER_ID)

        history: list[dict] = []
        print(f"Claude+Loom loop ({MODEL}). Empty line, Ctrl-D, or Ctrl-C to end.")
        try:
            while True:
                try:
                    user_msg = (await asyncio.to_thread(input, "you> ")).strip()
                except EOFError:
                    break
                if not user_msg:
                    break

                # PULL — per-turn recall rides in the system prompt so the
                # message history stays a clean transcript for extraction
                # later. (One-call alternative: agent.context.pack(query=...)
                # lets the server assemble the whole mix; this example keeps
                # the granular path so each piece is visible.)
                hits = await agent.memory.search(user_msg, top_k=5, user_id=USER_ID)
                system = "\n\n".join(p for p in (persona, _memories_block(hits)) if p)

                # REASON
                history.append({"role": "user", "content": user_msg})
                # Opus 5 thinks by default and thinking counts against
                # max_tokens; a tight cap ends the turn before any text.
                resp = await claude.messages.create(
                    model=MODEL, max_tokens=16000, system=system, messages=history
                )
                text = "".join(b.text for b in resp.content if b.type == "text")
                # Anthropic rejects empty content blocks; keep the user/
                # assistant alternation intact even on a text-free response.
                history.append({"role": "assistant", "content": text or "(no text)"})
                print(f"claude> {text}\n")
        except KeyboardInterrupt:
            print()  # ^C still falls through to the write-back below

        # WRITE BACK — server-side extraction over the session. The
        # extractor decides what's durable; the next session's
        # memory.search recalls it. observation_date anchors relative-time
        # phrases ("yesterday") to absolute dates at extraction time.
        # The wire op caps messages at 100 per call, so chunk long
        # sessions (turns alternate, so a 100-slice keeps pairs intact).
        written: list[dict] = []
        for i in range(0, len(history), 100):
            result = await agent.memory.set_from_messages(
                history[i : i + 100],
                user_id=USER_ID,
                observation_date=dt.date.today().isoformat(),
            )
            written.extend(result.get("written") or [])
        if history:
            keys = ", ".join(str(w.get("memory_key", "")) for w in written)
            print(f"[loom] extracted {len(written)} memories" + (f": {keys}" if keys else ""))


if __name__ == "__main__":
    asyncio.run(main())
