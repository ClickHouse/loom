# agent-memory

An agent that remembers what a user said, recalls it by meaning, answers with citations (or abstains), and tells loom which memories it used. One file, no framework.

```bash
LOOM_BASE_URL=https://loom.example LOOM_API_KEY=… LOOM_ORG=acme node index.mjs
```

The script registers a throwaway namespace (`ts-example-*`) so it can be run against any deployment you hold a writer token for; an operator registers real namespaces. It deletes the three memories it wrote before it exits.

## What it shows

1. `probe()` — the deployment serves `memory.set/search/ask/outcome`, and whether it has ops this SDK version does not know.
2. `Datapack(org, ns).agent(name)` — the identity tuple is stamped on every call; `userId` is the per-call scope.
3. `remember()` ×3 — three facts from two conversations; dedup, conflict detection and validity are the server's job.
4. `recall()` — semantic search with similarity per hit.
5. `ask()` — a grounded answer that cites `[memory_key]`, or `abstain=true` when the memories do not say.
6. `outcome()` — the feedback loop: cited memories are marked `used`.
7. Typed errors — a missing key reads as `found=false`; an unknown op is a `NotFound` carrying the server's request id.
8. `memory.delete` ×3 — in a `finally`, so the three memories are gone even when a step fails. The namespace stays registered: dropping it takes an admin.

## Actual output

A run against an isolated Loom server on ClickHouse Cloud, gpt-4o-mini as the synthesis model. The server build, the similarities, the answer text and the request id differ from run to run; the rest of the output does not:

```text
server b0b4ec38b: ok=true, ops the SDK does not know: 0
  recall 0.51  [seat_pref] "The user prefers an aisle seat on flights over three hours." (#seat_pref)
  recall 0.23  [meal_pref] "The user asked for a vegetarian meal on their last two bookings." (#meal_pref)
  recall 0.21  [airline_status] "The user holds gold status with Lufthansa (as of 2026-08)." (#airline_status)
ask → abstain=false cited=["meal_pref","seat_pref","airline_status"]
     Request an **aisle seat** — the user prefers an aisle seat on flights over three hours, and a flight to Tokyo qualifies [seat_pref]. Request a **vegetarian meal** — the user asked for a vegetarian meal on their last two bookings [meal_pref].

Also note the user holds gold status with Lufthansa (as of 2026-08), which may support seat-selection or upgrade perks [airline_status].
memory.get of a missing key → found=false
unknown op → NotFound (404) request 529780e8…
```

The recall similarities are cosine over `text-embedding-3-large`; the third hit sits under loom's abstention floor for a single-fact question and is returned only because `top_k: 3` asked for it. The `ask` answer cites all three keys and the outcome calls mark them `used`, which is what the usage fold and the harm signal read.
