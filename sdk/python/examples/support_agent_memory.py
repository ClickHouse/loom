"""Per-user memory for a support agent: two customers kept apart, a fact revised,
and one customer erased on request and checked gone.

    LOOM_BASE_URL=https://loom.example LOOM_API_KEY=… LOOM_ORG=acme python support_agent_memory.py

Erasing a `user_id` takes an org admin, so LOOM_API_KEY must be a token that may
erase (see the examples README). Registers a throwaway namespace (`py-example-*`),
as agent_memory.py does. Both customer ids are new on every run: an erasure by
`user_id` reaches every namespace in the org, so the erased id must exist only here.
Exits 1 when a check fails.
"""

from __future__ import annotations

import asyncio
import os
import secrets
import sys
import time
from typing import Any

from loom import Agent, LoomError

# What each customer told support. The keys repeat across customers: `user_id` is the scope.
FACTS = {
    "keep": {
        "plan": "The customer is on the Pro plan, renewed in March 2026.",
        "device": "The customer uses the Android app on a Pixel 8.",
    },
    "erase": {
        "plan": "The customer is on the Free plan.",
        "device": "The customer uses Safari on macOS and sees a login loop.",
    },
}
QUERY = "Which plan is the customer on, and which device do they use?"
REVISED_PLAN = "The customer upgraded to the Business plan in November 2026."
ERASURE_WAIT_S = 120


async def main() -> int:
    org = os.environ.get("LOOM_ORG", "dev")
    namespace = os.environ.get("LOOM_NAMESPACE") or f"py-example-{secrets.token_hex(3)}"
    run = secrets.token_hex(4)
    users = {"keep": f"customer-keep-{run}", "erase": f"customer-erase-{run}"}
    async with Agent(org, namespace, "support-agent") as bot:
        # A throwaway namespace for the example. Registering one that already exists
        # takes its admin, so a refusal here means "it is there already".
        try:
            await bot.call(
                "namespace.register", {"owner_email": "example@loom.test", "kind": "bench"}
            )
        except LoomError:
            pass

        try:
            failures = await remember_revise_erase(bot, users)
        finally:
            # Leave nothing behind, even when a step failed. Deleting a key the
            # erasure already removed is a no-op.
            for who, facts in FACTS.items():
                for key in facts:
                    await bot.memory.delete(key, user_id=users[who])

    for failure in failures:
        print(f"FAIL: {failure}")
    print("all checks passed" if not failures else f"{len(failures)} check(s) failed")
    return 1 if failures else 0


async def search_user(bot: Agent, user: str) -> list[str]:
    """The excerpts a search narrowed to one customer returns, in this namespace only."""
    hits = await bot.memory.search(QUERY, user_id=user, top_k=5, namespace_scope="this")
    return [h.content_excerpt for h in hits]


def _mentions(excerpts: list[str], facts: dict[str, str]) -> bool:
    return any(fact in excerpt for excerpt in excerpts for fact in facts.values())


async def remember_revise_erase(bot: Agent, users: dict[str, str]) -> list[str]:
    failures: list[str] = []

    # 1. Remember what each customer said.
    written: dict[str, dict[str, Any]] = {}
    for who, facts in FACTS.items():
        for key, text in facts.items():
            written[f"{who}/{key}"] = await bot.memory.set(
                key, text, user_id=users[who], tags=["support"]
            )
    print(f"wrote {len(written)} facts for {users['keep']} and {users['erase']}")

    # 2. A search narrowed to one customer returns only that customer's facts.
    for who, other in (("keep", "erase"), ("erase", "keep")):
        excerpts = await search_user(bot, users[who])
        print(f"\nsearch as {users[who]}:")
        for excerpt in excerpts:
            print(f"  {excerpt}")
        if not _mentions(excerpts, FACTS[who]):
            failures.append(f"{users[who]}: the search returned none of their facts")
        if _mentions(excerpts, FACTS[other]):
            failures.append(f"{users[who]}: the search returned the other customer's facts")

    # 3. Revise one fact in place by its id, then read it back.
    plan_id = written["keep/plan"]["memory_id"]
    await bot.memory.update_by_id(plan_id, REVISED_PLAN)
    got = await bot.call("memory.get", {"memory_key": "plan"}, user_id=users["keep"])
    print(f"\nrevised plan for {users['keep']}: {got.get('value')}")
    if got.get("value") != REVISED_PLAN:
        failures.append(f"{users['keep']}: the plan reads {got.get('value')!r} after the update")

    # 4. Erase one customer. A `user_id` scope covers every namespace in the org,
    # which is why this customer's id is new on every run.
    receipt = await bot.call(
        "erasure.request",
        {
            "scope_type": "user_id",
            "scope_value": users["erase"],
            "requester": "example@loom.test",
        },
    )
    status = await wait_for_erasure(bot, receipt)
    print(
        f"\nerasure {receipt['erasure_id']}: status={status.get('status')} "
        f"residue_count={status.get('residue_count')}"
    )
    if status.get("status") != "completed" or status.get("residue_count") != 0:
        failures.append(f"erasure of {users['erase']} ended as {status.get('status')!r}")

    # 5. The erased customer's search comes back empty; the other customer's does not.
    erased = await search_user(bot, users["erase"])
    kept = await search_user(bot, users["keep"])
    print(f"after erasure: {users['erase']} → {len(erased)} hit(s), {users['keep']} → {len(kept)}")
    if erased:
        failures.append(f"{users['erase']}: {len(erased)} fact(s) still found after erasure")
    if not kept:
        failures.append(f"{users['keep']}: the erasure removed the other customer's facts")
    return failures


async def wait_for_erasure(bot: Agent, receipt: dict[str, Any]) -> dict[str, Any]:
    """Poll erasure.status until the saga stops running, or ERASURE_WAIT_S passes."""
    deadline = time.monotonic() + ERASURE_WAIT_S
    while True:
        status = await bot.call("erasure.status", {"erasure_id": receipt["erasure_id"]})
        if status.get("status") != "in_progress" or time.monotonic() >= deadline:
            return status
        await asyncio.sleep(1)


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
