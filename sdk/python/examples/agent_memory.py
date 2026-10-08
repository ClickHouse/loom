"""An agent that remembers, recalls, answers with citations and closes the
loop — the Python twin of sdk/typescript/examples/agent-memory.

    LOOM_BASE_URL=https://loom.example LOOM_API_KEY=… LOOM_ORG=acme python agent_memory.py

Registers a throwaway namespace (`py-example-*`) so it runs against any
deployment you hold a writer token for; an operator registers real ones. It
deletes the three memories it wrote before it exits.
"""

from __future__ import annotations

import asyncio
import os
import secrets

from loom import Agent, LoomError, NotFound
from loom.ops import OPS

# What the user said across two conversations; step 8 deletes exactly these.
KEYS = {
    "seat_pref": "The user prefers an aisle seat on flights over three hours.",
    "meal_pref": "The user asked for a vegetarian meal on their last two bookings.",
    "airline_status": "The user holds gold status with Lufthansa (as of 2026-08).",
}


async def main() -> None:
    org = os.environ.get("LOOM_ORG", "dev")
    namespace = os.environ.get("LOOM_NAMESPACE") or f"py-example-{secrets.token_hex(3)}"
    async with Agent(org, namespace, "travel-assistant") as bot:
        # 1. Does this deployment serve what the loop needs?
        p = await bot.probe(ops=["memory.set", "memory.search", "memory.ask", "memory.outcome"])
        print(
            f"server {p['build_commit']}: ok={p['ok']}, "
            f"ops the SDK does not know: {len(p['unknown_to_sdk'])}"
        )

        # 2. A throwaway namespace for the example. Registering one that already
        # exists takes its admin, so a refusal here means "it is there already".
        try:
            await bot.call(
                "namespace.register", {"owner_email": "example@loom.test", "kind": "bench"}
            )
        except LoomError:
            pass

        user = "u-42"
        try:
            await remember_recall_ask(bot, user)
        finally:
            # 8. Leave nothing behind: tombstone what step 3 wrote, even when a
            # later step failed. (Dropping the namespace itself takes an admin.)
            for key in KEYS:
                await bot.memory.delete(key, user_id=user)


async def remember_recall_ask(bot: Agent, user: str) -> None:
    # 3. Remember three things the user said across two conversations.
    for key, text in KEYS.items():
        await bot.memory.set(key, text, user_id=user, tags=["travel"])

    # 4. Recall by meaning.
    hits = await bot.memory.search(
        "what seat should I book for a long flight?", user_id=user, top_k=3
    )
    for h in hits:
        print(f"  recall {h.similarity:.2f}  [{h.memory_key}] {h.content_excerpt}")

    # 5. Ask for a grounded answer; loom abstains when the memories do not say.
    answer = await bot.call(
        "memory.ask",
        {"query": "Which seat and meal should I request for the user's flight to Tokyo?"},
        user_id=user,
    )
    print(f"ask → abstain={answer.get('abstain')} cited={answer.get('memory_keys')}")
    print(f"     {answer.get('answer')}")

    # 6. Close the loop: the memories the agent actually used earn their keep.
    for key in answer.get("memory_keys") or []:
        await bot.memory.outcome_by_key(key, outcome="used", user_id=user)

    # 7. Names for every op, typed errors.
    print(
        f"memory.search is idempotent: {OPS['memory.search'].idempotent}; "
        f"role {OPS['memory.search'].role}"
    )
    miss = await bot.call("memory.get", {"memory_key": "never_written"}, user_id=user)
    print(f"memory.get of a missing key → found={miss.get('found')}")
    try:
        await bot.call("memory.no_such_op", {})
    except NotFound as e:
        print(f"unknown op → NotFound ({e.code})")


if __name__ == "__main__":
    asyncio.run(main())
