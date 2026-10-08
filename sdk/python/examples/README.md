# Examples

Runnable, self-contained scripts for `clickhouse-loom`; each was executed against a live Loom before it landed here and the output below is the actual run. Every script reads `LOOM_BASE_URL`, `LOOM_API_KEY` and `LOOM_ORG` from the environment and registers a throwaway namespace. The TypeScript SDK ships the twin of `agent_memory.py` (and a session-resume example) under `sdk/typescript/examples/`.

## agent_memory.py

An agent that remembers what a user said, recalls it by meaning, answers with citations (or abstains), and tells loom which memories it used.

```bash
LOOM_BASE_URL=https://loom.example LOOM_API_KEY=… LOOM_ORG=acme python agent_memory.py
```

`Agent(org, namespace, agent)` stamps the identity tuple on every call; `user_id` is the per-call scope.

1. `Agent.probe()` — the deployment serves `memory.set/search/ask/outcome`, and whether it has ops this SDK version does not know.
2. `namespace.register` — the throwaway namespace; a refusal because it already exists is ignored.
3. `memory.set` ×3 — three facts from two conversations; dedup, conflict detection and validity are the server's job.
4. `memory.search` — semantic search with similarity per hit.
5. `Agent.call("memory.ask", …)` — a grounded answer that cites `[memory_key]`, or `abstain=True` when the memories do not say.
6. `memory.outcome_by_key` — the feedback loop: cited memories are marked `used`.
7. `loom.ops.OPS` names every op; a missing key reads as `found=False`; an unknown op is a `NotFound`.
8. `memory.delete` ×3 — in a `finally`, so the three memories are gone even when a step fails. The namespace stays registered: dropping it takes an admin.

A run against an isolated Loom server on ClickHouse Cloud, gpt-4o-mini as the synthesis model. The server build, the similarities (they depend on the embedder) and the answer text (written by the model) differ from run to run; the rest of the output does not:

```text
server 2688e2476: ok=True, ops the SDK does not know: 0
  recall 0.51  [seat_pref] "The user prefers an aisle seat on flights over three hours." (#seat_pref)
  recall 0.23  [meal_pref] "The user asked for a vegetarian meal on their last two bookings." (#meal_pref)
  recall 0.21  [airline_status] "The user holds gold status with Lufthansa (as of 2026-08)." (#airline_status)
ask → abstain=False cited=['meal_pref', 'seat_pref', 'airline_status']
     Request an aisle seat, since the user prefers an aisle seat on flights over three hours and a flight to Tokyo well exceeds that [seat_pref]. For the meal, request a vegetarian meal, matching what the user asked for on their last two bookings [meal_pref].

Also note the user holds gold status with Lufthansa (as of 2026-08), which may help with seat selection [airline_status].
memory.search is idempotent: True; role viewer
memory.get of a missing key → found=False
unknown op → NotFound (RESOURCE_NOT_FOUND)
```
