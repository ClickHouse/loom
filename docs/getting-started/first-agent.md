# Your first agent

This tutorial builds a small agent with the Python SDK. It continues from the
[quickstart](./quickstart.md): the stack runs on `http://localhost:7777` and the Python
SDK is installed ([quickstart step 5](./quickstart.md#5-first-memory-from-python)). Every
step runs on the stock stack. Extraction and `memory.ask` synthesis need a real LLM;
ranking is meaningful on the released stack's local embedder, not on a checkout's fake one
([providers](../operations/deployment.md#providers)).

## 1. Give the agent an identity

Every row Loom stores carries the identity tuple `(org, namespace, agent, user_id,
session_id)`. The `Agent` binds the first three; `user_id` and `session_id` travel per
call and default to `-`.

```python
import asyncio

from loom import Agent

USER = "u-42"  # the end user this conversation is about


def make_agent() -> Agent:
    return Agent(
        org="dev",
        namespace="local",
        agent="support-bot",  # authorship: every row and ledger entry names it
        base_url="http://localhost:7777",
        consumer="support-demo",  # the product this process speaks for
    )
```

- **`namespace`** is the sharing unit. Every member of the namespace reads every row in
  it, whichever agent wrote it; `agent` records who wrote a row, it is not a read
  boundary. On the stock stack the only namespace is `dev/local`.
- **`agent`** must be a real name. `""`, `"-"` and `"*"` are replaced with a name derived
  from the script, with a warning, because per-agent cost, provenance and advice need to
  tell bots apart.
- **`consumer`** names the product this process speaks for and is stamped on every
  request. A search counts as a ranking signal only when the reader names a consumer and
  ties the read to a run or session, so set it from the start.
- The token comes from `api_key=`, then `LOOM_API_KEY`, then `~/.loom/token`. None is
  needed in single-user mode.

Why the tuple: one agent serving many users must not answer one user from another's
facts, and one namespace shared by several agents must still say who wrote what. The
[identities and namespaces](../concepts/identities-and-namespaces.md) page explains the
model.

## 2. Store facts and handle abstain

```python
async def remember_and_recall(a: Agent) -> None:
    res = await a.memory.set(
        "support/pref/seat",
        "Prefers an aisle seat on long flights.",
        tags=["travel"],
        user_id=USER,
    )
    print("stored:", res["memory_key"], res["status"], res.get("conflict_verdict", ""))

    hits = await a.memory.search("which seat should I book?", user_id=USER, top_k=3)
    if hits.abstain:
        # Loom does not know. Say so; do not answer from the weak rows.
        print("no answer:", hits.abstain_code, hits.abstain_reason)
        return
    for hit in hits:
        print(hit.memory_key, round(hit.similarity, 3), hit.content_excerpt)
```

- **One claim per key.** A key reads like a path (`<scope>/<kind>/<id>`). Writing the
  same key again revises the fact; writing a different key whose value is close to an
  existing fact is checked against it. The response's `status` and `conflict_verdict`
  say what happened (a conflict with a pinned fact is rejected with `PINNED_CONFLICT`).
  [Conflicts and refusal](../features/memory/conflicts-and-refusal.md) covers the
  verdicts.
- **`user_id` on the read filters to that user's rows.** Without it the search spans the
  whole namespace.
- **`abstain` is the answer, not an error.** When the best hit is below a similarity gate
  calibrated for the embedder, the result still carries the rows but sets `abstain=True`
  with a machine-readable `abstain_code` (`no_match`, `below_threshold`,
  `entity_absent`, `low_calibrated_confidence`, `page_expired`). Branch on the code, not
  the free-text reason. Each row below the gate also carries `below_threshold=True`.
  `pending_extraction` above zero means deferred extraction has not finished yet, so an
  empty result is not evidence of absence.

With the fake embedder the ranking is by character n-grams, so which query abstains is
arbitrary; with a real embedder a question about something never stored abstains.
[Retrieval and ranking](../concepts/retrieval-and-ranking.md) explains the gates and
the ranking terms.

For a grounded natural-language answer instead of hits, `await a.memory.ask("Which seat
does the user prefer?", user_id=USER)` returns `answer`, `abstain` and the `memory_keys`
it cited. It needs a real LLM; with the fake one there is no synthesis.

## 3. Hold a conversation as a session

A `Session` is what an agent loop keeps between turns and across restarts. It binds one
`(user_id, session_id)` scope and four verbs to it:

```python
async def converse(a: Agent) -> None:
    async with a.session("thread-42", user_id=USER) as s:
        resumed = await s.resume(query="book a flight for the user")
        system_prompt = resumed.system  # the context pack, rendered
        history = resumed.messages  # the transcript tail, oldest first; [] when new
        print(system_prompt[:300], len(history))

        await s.append(
            {"role": "user", "content": "Book me on the Friday flight to Berlin."},
            {"role": "assistant", "content": "Booked 14C, an aisle seat."},
        )
    # leaving the block calls close(): buffered turns go to extraction
```

- **`resume()`** is `context.pack` for this scope plus the last turns of the transcript:
  exactly what a restarted process needs to continue.
- **`append()`** writes each turn to the conversation transcript (`conversation.append`)
  at once, and every `flush_every` turns (default 8) sends the batch to
  `memory.set_from_messages`, where the server extracts the facts worth keeping.
  `close()` flushes the rest. On the fake LLM the transcript is stored but extraction
  writes nothing.
- **`offload()` / `fetch()`** park a large tool result once and keep only a pointer in
  the transcript; **`note()`** appends to the working-memory block the next pack carries.

If the namespace has conversation logging turned off, the transcript is not stored (the
SDK logs a warning) and extraction still runs.
[Conversations and sessions](../features/conversations-and-sessions.md) covers threads,
digests and session briefs.

## 4. Load context at session start

Outside a session, one call assembles the bounded block an agent wants before its first
turn: working memory first, then the profile, relevant memories, matching procedures and
decisions, within a token budget.

```python
async def session_start(a: Agent) -> str:
    pack = await a.context.pack("book a flight", mode="startup", user_id=USER)
    print(pack.section_count, "sections,", pack.tokens_estimate, "tokens")
    return str(pack)  # the text to put in the system prompt
```

`mode` (`startup`, `task`, `debug`, `handoff`) shifts the budget between sections;
`token_budget` must be between 200 and 12000 (the server rejects values outside it).
`pack.stable_prefix` is the part that does not change between queries for this identity,
which is where to put a provider cache breakpoint. A section that fails degrades to empty
instead of failing the pack. Working memory is scratchpad state, not retrieval memory: it
is loaded first and never embedded or ranked
([Working memory and profiles](../features/memory/working-memory-and-profiles.md)).

## 5. Record a task as a run

A run is one agent execution: what it was asked, what it relied on and how it ended.
Naming the memories a run relied on is the feedback loop: those keys are credited in
search ranking, and a later grade can count the outcome against them.

```python
async def handle_task(a: Agent) -> None:
    question = "which seat should I book?"
    hits = await a.memory.search(question, user_id=USER, top_k=3)

    async with a.run_scope(goal="Pick a seat for the Berlin booking", user_id=USER) as run:
        if hits.abstain:
            run.final_response = "I don't have the user's seat preference."
        else:
            best = hits[0]
            run.used(best.memory_key)  # relied on, not merely retrieved
            run.final_response = f"Booked an aisle seat ({best.content_excerpt})"
        await a.memory.set(
            "support/booking/berlin",
            "Booked seat 14C on the Friday flight to Berlin.",
            user_id=USER,
        )
    print(run.completed)  # the run.complete response
```

`run_scope` calls `run.begin` on entry and `run.complete` on exit, with status `completed`
(or the `run.status` the block set), `failed` when the block raises, or `cancelled` when
it is cancelled (either still propagates after the ledger row is written). Writes inside
the block carry the run's `run_id`, and the keys a grounded `memory.ask` answer cites
inside it (not an abstention) are added to `memories_used` automatically. A write under a
run is scoped to it: restating a fact that another run wrote is stored as a `duplicate`,
not merged.

The same two calls are available directly as `a.run.begin(goal=...)` and
`a.run.complete(run_id, status=..., memories_used=[...], final_response=...)`;
`run.complete` also takes a `reflection` (what worked, what failed, lessons) that is
stored as a `reflection` memory. `a.run.get(run_id)` replays a run with what it wrote,
recalled and cost. [Runs](../features/runs.md) covers reflections, retention and grades.

## 6. Put it together

```python
async def main() -> None:
    async with make_agent() as a:
        await remember_and_recall(a)
        await session_start(a)
        await converse(a)
        await handle_task(a)


asyncio.run(main())
```

Run it, then open `http://localhost:7777/memories` to see the rows with their writer
agent, `/runs` for the run, and `/conversations` for the thread. Blocking code uses
`loom.SyncAgent`, which takes the same arguments and exposes every method as a blocking
call.

## The same agent in TypeScript

The TypeScript SDK (`@clickhouse/loom`;
[install](../reference/typescript-sdk.md#install)) has the same identity
model and session, with shorter verb names. It has no run helper; runs go through the
typed `call`.

```ts
import { Agent } from "@clickhouse/loom";

const USER = "u-42";
const bot = new Agent("dev", "local", "support-bot", {
  baseUrl: "http://localhost:7777",
  consumer: "support-demo",
});

await bot.remember("support/pref/seat", "Prefers an aisle seat on long flights.", {
  userId: USER,
  tags: ["travel"],
});

const hits = await bot.recall("which seat should I book?", { userId: USER, top_k: 3 });
if (hits.abstain) console.log("no answer:", hits.abstain_code);
else for (const h of hits.results) console.log(h.memory_key, h.content_excerpt);

const s = bot.session("thread-42", { userId: USER });
const { system, messages } = await s.resume({ query: "book a flight for the user" });
console.log(system.slice(0, 300), messages.length);
await s.append({ role: "user", content: "Book me on the Friday flight to Berlin." });
await s.close();

const run = await bot.call(
  "run.begin",
  { goal: "Pick a seat for the Berlin booking" },
  { userId: USER },
);
// run_id is part of the identity scope, like userId
await bot.call(
  "run.complete",
  {
    status: "completed",
    memories_used: ["support/pref/seat"],
    final_response: "Booked an aisle seat.",
  },
  { runId: run.run_id },
);
```

`bot.contextPack({ query, mode: "startup" })` is `context.pack`, and `bot.ask(query)` is
`memory.ask`. The [TypeScript SDK reference](../reference/typescript-sdk.md) covers the
full surface.

## 7. Connect a coding agent

A coding agent reads the same namespace through MCP tools. What you stored from Python
is visible to it when its token reads that namespace.
[Connect a coding agent](./connect-a-coding-agent.md) covers the plugins and
`loom install`.

## Next

- [Memory](../concepts/memory.md): what happens between the write and the read.
- [Writing memory](../features/memory/writing.md) and
  [Search and ask](../features/memory/search-and-ask.md): every field of the calls above.
- [Agent integration guide](../guides/agent-integration.md): patterns for a production
  agent loop.
- [Python SDK reference](../reference/python-sdk.md).
