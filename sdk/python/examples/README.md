# Examples

Runnable, self-contained scripts for `clickhouse-loom`. The memory example's output below was recorded against a live Loom; the handoff output illustrates its checked result. The memory example reads `LOOM_BASE_URL`, `LOOM_API_KEY` and `LOOM_ORG` from the environment and registers a throwaway namespace. The handoff example uses an existing namespace and two separate tokens. The TypeScript SDK ships the twin of `agent_memory.py` (and a session-resume example) under `sdk/typescript/examples/`.

## fact_document_evidence.py

Read an existing saved fact and an explicitly selected document section for operator
review. Install `clickhouse-loom` and configure `LOOM_BASE_URL` and `LOOM_API_KEY` for
a deployment where your credential can read both records. Supply the fact's owning
agent, user and session identity; omitted user/session values are `-`. Both records
must be in the selected namespace. No source connector is configured by this example.
The command rejects anonymous agent names and stops before reading the document if
the returned fact's agent, user or session is missing or differs from the selection.

```bash
python sdk/python/examples/fact_document_evidence.py \
  --org "$LOOM_ORG" --namespace "$LOOM_NAMESPACE" --agent "$FACT_AGENT" \
  --memory-key "$FACT_KEY" --source-uri "$DOCUMENT_URI" \
  --section "Deployments" "Rollback"
```

Use the exact heading path recorded by
[`knowledge.outline`](../../../docs/features/knowledge.md#reading-a-document).
The script calls `memory.get` and `knowledge.outline` with `include_children=False`.
`knowledge.outline` is experimental and must be served by the deployment.
It makes no search, embedding or model call, writes no application data, and disables
automatic run recording, startup briefs, retries and rate-limit pacing.

For inspection in an authorized Python application, import `read_pair` from the
example and await it with your configured `Agent` and the same selection arguments.

```python
pair = await read_pair(
    bot,
    memory_key=fact_key,
    source_uri=document_uri,
    section_path=["Deployments", "Rollback"],
)
fact, document_section = pair["fact"], pair["document_section"]
```

The returned `fact["value"]` is the saved claim; `fact` also retains returned
`agent`, `user_id`, `session_id`, `version`, `status`, `actor`, `source_class`,
`source_trace` and `source_resource_id`
fields. `document_section["section_content"]` is the selected section's assembled
chunk text, with `source_uri` and `section_chunk_idxs` identifying its source. Compare
the claim with that passage privately; CLI output contains only presence and field
names, never the claim, passage or provenance values. Optional fields remain absent
when the server does not return them. Check `fact["found"]`, document `found` and
non-null `section_content` before inspecting bodies.

The pairing is caller-selected. A stored source pointer does not establish support,
and this example neither resolves that pointer nor verifies entailment or truth.
`support` remains `not_evaluated`, including when both reads succeed. The reads are
separate observations, not an atomic snapshot or a revision-pinned citation. A missing
or inaccessible fact, document or section supplies no support. Offline HTTP doubles
check the SDK contracts; no captured live output is supplied. See the
[memory and document boundary](../../../docs/features/knowledge.md#how-do-agents-connect-a-memory-to-a-source-document).

## software_factory_memory.py

A software-factory memory reference for an implementation agent handing source evidence
to a review agent. It uses the published Python SDK's `call_with_meta` interface and
records operation receipts. Application code awaits every operation; there is no model
tool selection, code generation or automated code review in this example.

Before running:

- Configure Loom to index a public open-source repository, such as `pallets/flask`.
  Choose one fully qualified function whose body fits the response budget. Set
  `INDEXED_COMMIT` to the full commit currently indexed and `PUBLIC_ISSUE_URL` to an
  actual issue in that repository. The example checks the configured repository and
  indexed commit; the operator establishes that the repository and issue are public.
- Register a shared memory namespace and a review inbox owned by `factory-reviewer`.
  Permit `factory-implementation` to post the `review` task kind. A task needing
  admission stops the example; it does not change inbox policies or permissions.
- Supply different service-account credentials with subjects `factory-implementation`
  and `factory-reviewer`. Each needs writer access to the memory namespace and read
  access to the code namespace. Export them as `LOOM_IMPLEMENTATION_API_KEY` and
  `LOOM_REVIEW_API_KEY`, with `LOOM_BASE_URL` pointing to the deployment. Different
  token strings alone do not establish distinct principals; the example checks the
  posting subject and saved note's writing actor.
- Copy the repository's `repo:<id>` tag from its configured checkout/session primer.
  Keep credentials, personal data and private issue content out of the review claim.

With `clickhouse-loom` installed, run from the repository root:

```bash
python sdk/python/examples/software_factory_memory.py \
  --org "$LOOM_ORG" --memory-namespace "$LOOM_NAMESPACE" \
  --code-namespace "$LOOM_CODE_NAMESPACE" --repo "$PUBLIC_REPO" \
  --commit "$INDEXED_COMMIT" --issue-url "$PUBLIC_ISSUE_URL" \
  --symbol "$QUALIFIED_FUNCTION" --repo-tag "$REPOSITORY_TAG" \
  --review-inbox "$REVIEW_INBOX" \
  --lesson "Check the function's error handling before changing its retry path."
```

The script performs this sequence:

1. Read the code index watermark, resolve one complete function body, and read the
   watermark again. An ambiguous, elided or moving index stops the sequence.
2. Begin a run. Write an operator-supplied review claim under an unpredictable example
   key, with its issue URL, commit, source URL and returned-body digest. Check the write
   receipt and read the saved note back. The claim remains unverified.
3. Post a review task carrying the source snapshot and memory key. The review agent
   claims it, heartbeats its lease fence and reads the implementation agent's note.
4. Fetch the source again through the review agent's credential. Compare its path,
   symbol and returned-body digest with the handed-off snapshot. `unchanged_body`
   means those fields match; `changed_body_needs_review` requests a recheck of the
   claim. Neither result establishes whether the claim is correct.
5. Complete the evidence-handoff task and report the run with outcome `none`, keeping
   code-review and CI status explicitly unperformed. A completed handoff is not a
   completed implementation, accepted review or passing CI result.
6. In `finally`, cancel its unfinished task when it still holds the observed ownership
   and fence, then delete only this invocation's generated memory key and verify it is
   missing. A changed lease is left for the operator. The inbox, task and run history
   remain; deletion tombstones the note and does not erase its history.

Output contains operation names, available request IDs and categorical outcomes. It
prints no source bodies, review claims, credentials or server error bodies. It disables
automatic run recording, optional startup briefs, write coalescing, retries and
rate-limit pacing; failed or ambiguous operations are not silently retried. Actual request IDs and
outcomes come from the deployment; no captured live output is supplied here.
A lost create response can leave a task or run whose ID the client did not receive;
its operator must reconcile that history rather than repeat the call automatically.
Cleanup reads and cancellation are separate operations. Do not let other workers
claim or defer this example's task while it runs; the read does not reserve the task.

The repository tag allows the configured code-link worker to consider the saved note,
but this short run does not wait for that worker or verify native link states. Its
source comparison is performed by application code against the served index, not
uncommitted editor changes or an atomic repository snapshot. The memory write may use
the server's configured embedding or conflict-check provider; the script makes no
agent-model call. This is a transport and evidence-handoff reference, not a measurement
of reasoning quality, security isolation, recommendation rank, time or cost.

See [shared-agent memory](../../../docs/use-cases/multi-agent.md),
[code-linked memory](../../../docs/use-cases/memory-linked-to-code.md) and
[run reporting](../../../docs/features/runs.md) for the supported boundaries.

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

## support_agent_memory.py

Per-user memory for a support agent: two customers kept apart, a fact revised, and one customer erased on request and checked gone.

```bash
LOOM_BASE_URL=https://loom.example LOOM_API_KEY=… LOOM_ORG=acme python support_agent_memory.py
```

Erasing a `user_id` takes an org admin. `loom mint-token --roles` takes `viewer`, `writer` or `admin` only, so on a local server with auth on, mint a short-lived platform-admin service token for the run:

```bash
loom mint-token --platform-admin --subject sa:support-example --org acme --ttl-days 1
```

1. `namespace.register` — the throwaway namespace; a refusal because it already exists is ignored.
2. `memory.set` ×4 — two facts for each of two customers, under the same keys: `user_id` is the scope. Both customer ids are new on every run.
3. `memory.search` narrowed to each customer — only that customer's facts come back.
4. `memory.update_by_id` — the first customer's plan is revised in place, then read back with `memory.get`.
5. `erasure.request` with `scope_type: "user_id"` for the second customer, then `erasure.status` until the saga is `completed` with `residue_count` 0. A `user_id` scope covers every namespace in the org, which is why the id must exist only in this run.
6. `memory.search` again — the erased customer gets nothing back; the other customer's facts are still there.
7. `memory.delete` — in a `finally`, every key of both customers, so nothing stays behind when a step fails.

The script exits 1 when a check fails.

<REAL_RUN>

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

## code_lookup.py

An explicit code lookup through the same `AgentTools` dispatcher used by a model's
selected call. Requires an existing code index and a token with read access; it creates
no namespace or memory and calls no agent model. Search uses server embeddings.

```bash
LOOM_BASE_URL=https://loom.example LOOM_API_KEY=… python code_lookup.py \
  --org acme --memory-namespace assistant-memory --code-namespace code-repo \
  --kind callers --depth 2 pkg.Reader.read
```

`--help` sends no requests. Mocked SDK transport tests cover the binding; no live
result or performance claim is attached to this example. See the
[code tools guide](https://clickhouse.github.io/loom/guides/code-tools/) for budgets,
scope and evidence limits.
