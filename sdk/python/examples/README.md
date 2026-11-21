# Examples

Runnable, self-contained scripts for `clickhouse-loom`. The memory example's output below was recorded against a live Loom; the handoff output illustrates its checked result. The memory example reads `LOOM_BASE_URL`, `LOOM_API_KEY` and `LOOM_ORG` from the environment and registers a throwaway namespace. The handoff example uses an existing namespace and two separate tokens. The TypeScript SDK ships the twin of `agent_memory.py` (and a session-resume example) under `sdk/typescript/examples/`.

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

## multi_agent_handoff.py

A planner posts a task, a worker claims it with a lease fence and writes the result,
and the planner reads that result with its writing principal and source class.
The two agents calculate in Python without an agent LLM or extraction API call.
Memory writes use the server's configured embedding provider.

Start an auth-enabled Loom and register a namespace before running this example.
Set `LOOM_BASE_URL`, `LOOM_ORG` and `LOOM_NAMESPACE` to that deployment and namespace.
Run the token commands in an operator shell configured for the same deployment,
then run the script from the repository root with `clickhouse-loom` installed:

```bash
export LOOM_PLANNER_API_KEY="$(loom mint-token --subject handoff-planner \
  --org "$LOOM_ORG" --namespaces "$LOOM_NAMESPACE" --roles admin --ttl-days 1)"
export LOOM_WORKER_API_KEY="$(loom mint-token --subject handoff-worker \
  --org "$LOOM_ORG" --namespaces "$LOOM_NAMESPACE" --roles writer --ttl-days 1)"
python sdk/python/examples/multi_agent_handoff.py
```

Both commands mint service-account tokens, the default. The subjects must match
the commands: the script checks the planner's posting subject and the worker's
complete `process:handoff-worker` actor. The planner needs admin to register the
inbox; the worker needs writer on the same namespace.

The inbox belongs to the worker's agent. Its random suffix is shared with the
result memory key and task idempotency key. The worker sends the claim's fence
with its heartbeat and completion. The planner reads `outcome.memory_key`, then
verifies the stored value, agent, actor and `source_class`. It reads across the
two agents within their shared namespace and default user/session scope; the
example does not promote the memory to another namespace.

The stored value includes the task id so separate calculations have distinct
results. A write that reports no new memory cancels the task before completion;
cleanup never follows or deletes a merged peer's key.

Expected output with an auth-enabled server using fake embedding and LLM
providers and distinct signed principals:

```text
inbox: handoff-dbeb0efa3cae3e9a (retained)
task: done
result: 7 squared is 49
writer agent: handoff-worker
writer actor: process:handoff-worker
source class: asserted
cleanup: result memory is missing
```

The example checks the task lifecycle, provenance and cleanup; it does not measure
agent reasoning or retrieval quality. The suffix differs each run. In `finally`,
the example cancels unfinished work with its current owner's token and deletes
the result memory, then checks that the memory is missing. The inbox and task
history remain; there is no inbox-delete operation. A failed check or API call
raises an error.
