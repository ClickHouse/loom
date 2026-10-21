# Customize the extraction pipeline

`memory.set_from_messages` runs its transcript through an ordered list of steps that share
one state. A request can replace that list through `pipeline_steps`: reorder or skip steps, or
call an in-process step an operator registered. This page covers only the step list;
[Extraction](../features/memory/extraction.md) covers what extraction does.

`pipeline_steps` is an HTTP body field. It is hidden from the MCP tool schema and is not a
parameter of the Python SDK's `memory.set_from_messages` helper; send it over HTTP or through
`call(...)`. The generated op input types (`sdk/python/loom/ops.py`,
`sdk/typescript/src/generated/ops.ts`) carry it.

## The default pipeline

`DEFAULT_PIPELINE` in `server/memory/app/pipeline.py`:

| Step                              | Does                                                                                             |
| --------------------------------- | ------------------------------------------------------------------------------------------------ |
| `loom.fetch_namespace_categories` | Loads the namespace's tag taxonomy for the extractor.                                            |
| `loom.fetch_existing_memories`    | Nearest existing memories (up to 50), shown to the extractor.                                    |
| `loom.llm_extract`                | The extraction call.                                                                             |
| `loom.drop_recall_echoes`         | Drops facts restating memories the session was served.                                           |
| `loom.drop_session_echoes`        | Drops facts restating what the session's own Loom calls carried.                                 |
| `loom.drop_activity_narration`    | Drops narration of tool activity with no durable outcome.                                        |
| `loom.drop_non_durable`           | Coding sessions: drops status snapshots, housekeeping, plans.                                    |
| `loom.verify_durable`             | Coding sessions: verifier pass over the remaining facts.                                         |
| `loom.hash_dedup`                 | Drops facts whose canonical hash is already stored.                                              |
| `loom.bulk_write`                 | Files bot rules (`about="agent"`) to the learnings queue, writes the rest via `memory.bulk_set`. |

A coding session is a transcript whose `extra_tags` carry a coding-agent source tag
(`source:claude-code`, `source:codex`, `source:cursor`), or a trajectory replay. The two coding
steps pass other transcripts through unchanged.

**An empty `pipeline_steps` runs this list. A non-empty one runs exactly the steps it
names, in order**, against one shared state. Leaving out `loom.bulk_write` means nothing is
written: the response still reports what was extracted.

## Run a shorter pipeline

Extract and write with no dedup context, for a one-shot import of data known to be new:

```bash
curl -s "$LOOM_BASE_URL/v1/memory.set_from_messages" \
  -H "Authorization: Bearer $LOOM_API_KEY" -H 'Content-Type: application/json' -d '{
  "org": "acme",
  "namespace": "support",
  "agent": "importer",
  "messages": [{ "role": "user", "content": "..." }],
  "pipeline_steps": [{ "step_id": "loom.llm_extract" }, { "step_id": "loom.bulk_write" }]
}'
```

This needs a real LLM provider (`LOOM_LLM_PROVIDER` and its key); the stock stack's fake
provider does not extract meaningful facts.

## Step behaviour

Each descriptor is `{"step_id": ..., ...}`; the whole object is passed to the step as its
config.

- **At most 16 steps** per request.
- **`hook.*` steps are refused** with 422 `VALIDATION_FAILED`: the server makes no outbound
  call on a caller's behalf.
- **Unknown step ids** are skipped and reported in the response's `pipeline_errors` as
  `unknown_step:<id>`.
- **Fail-open steps.** A fetch step that fails records the error in `pipeline_errors`
  (`fetch_existing_failed:<error>`, say) and the pipeline continues.
- **`loom.llm_extract` fails closed**: an extraction error aborts the request with 502, so a
  transcript is never silently answered with `written=[]`. It accepts `{"judge": true}` in
  its descriptor to turn the judge gate on; `judge_extraction=true` on the request does the
  same for the default list.
- **Timing.** The response's `stage_ms` carries each step's wall time (`step.<step_id>`).
  Each step also runs inside an OpenTelemetry span named `pipeline.step` with the step id
  as an attribute.

## Register an in-process step

An operator registers a step in code that the server process imports at start-up:

```python
from server.memory.app.pipeline import WorkflowState, register_step


async def spam_filter(state: WorkflowState, config: dict) -> WorkflowState:
    state.extracted_facts = [
        f for f in state.extracted_facts if "spam" not in str(f.get("value", "")).lower()
    ]
    return state


register_step("operator.spam_filter", spam_filter)
```

Requests can then name `{"step_id": "operator.spam_filter"}`; any other keys in the
descriptor arrive as `config`. The step runs in-process with read and write access to the
whole `WorkflowState`: identity, messages, existing memories, extracted facts, counters.
`register_step` replaces an existing registration under the same id, so an operator step
can also replace a built-in.

A step that replaces `loom.bulk_write` must leave `about="agent"` facts out of what it
writes: those are the bot's rules, which the built-in files to the learnings queue.

> **Warning:** a step that drops or rewrites `extracted_facts` changes what every caller
> naming it stores. A step placed after `loom.bulk_write` sees the written rows in
> `state.written` but cannot un-write them.

To add a built-in step to Loom itself, add the function and its `register_step` call in
`server/memory/app/pipeline.py`, place it in `DEFAULT_PIPELINE`, and run
`make bench-quick` before and after: the default list shapes every extraction.

## Limits

- Steps run sequentially; there is no branching or parallelism.
- The default list is global; there is no per-namespace pipeline.
- No step-level retry.

## Related

- [Extraction](../features/memory/extraction.md)
- [Extending Loom](../development/extending-loom.md)
- [Writing memory](../features/memory/writing.md)
