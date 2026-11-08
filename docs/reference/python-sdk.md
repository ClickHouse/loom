# Python SDK reference

The Python SDK (`clickhouse-loom`, source in `sdk/python/loom/`) is an async client over the
[HTTP API](./http-api.md). It binds an identity once and calls any operation by its dotted
name. It sends an `Idempotency-Key` on every request (the server deduplicates it for
idempotent ops), paces on the rate-limit headers, retries transient failures and stamps a
`consumer` tag. Errors are typed and carry the server's `code`, the HTTP `status` and the
`request_id`. The op table is generated from the server's registry, so every served op has a
name and a typed shape.

## Install

Python 3.11 or later. The only runtime dependency is `httpx[http2]`. The package is not
published to a package index; install it from a clone or from git:

```bash
pip install ./sdk/python                    # from a clone of the repository
pip install "clickhouse-loom @ git+https://github.com/ClickHouse/loom#subdirectory=sdk/python"
```

The package ships `py.typed` and passes `mypy --strict`.

## Credentials

The constructor's `profile` names a `loom login` session and its server.
Otherwise its `base_url` and `api_key` win, and the SDK reads `LOOM_BASE_URL`
(default `http://localhost:7777`), then `LOOM_API_KEY`, then a bot's
`LOOM_CLIENT_ID` with its key or workload token (`loom.machine`, tokens of an
hour, or the bot's shorter maximum, that it renews itself), then the session
`loom.login()` / `loom login` stored in `~/.loom/credentials` for that server
(profile `LOOM_PROFILE`, default `default`; it refreshes itself), and finally
the first line of `~/.loom/token`, the file `loom mint-token --save` writes. A
stored session is used only for the server it signed in to.
Routes outside the operation registry are on `agent.account` (service
accounts, MCP client setup, the default namespace, the sharing summary) and
`agent.deployment` (health, workers, model settings, claim release); see
[use Loom without the dashboard](../guides/without-the-dashboard.md). A blank `LOOM_API_KEY` counts as unset. The key never appears in a
`repr` or a log line: credential headers are redacted to their scheme and last
four characters.

## Quick example

```python
import asyncio

from loom import Agent


async def main() -> None:
    async with Agent("acme", "support", "assistant", consumer="support-bot") as bot:
        await bot.memory.set("deploy_window", "Deploys run Tue and Thu only; never Friday.")
        hits = await bot.memory.search("when can we deploy?", top_k=5)
        for hit in hits:
            print(hit.memory_key, hit.similarity, hit.content_excerpt)
        if hits.abstain:
            print("nothing strong enough to answer from")
        answer = await bot.memory.ask("When is the deploy window?")


asyncio.run(main())
```

`memory.search` returns a `MemorySearchResult`: a list of `MemorySearchHit`
plus the response envelope (`abstain`, `abstain_code`, `abstain_reason`,
`retrieval_risk`, `reinforcement`, `pending_extraction`, `degraded`, `next`). With the stock
stack's fake providers this runs end to end; `memory.ask` synthesizes an answer
only with a real LLM provider configured.

## Construction

```python
Agent(org, namespace, agent, *, base_url=None, api_key=None, timeout_s=60.0,
      pacing=True, coalesce_sets=True, ratelimit_max_wait_s=None, auto_run=None,
      retry=None, consumer="", mentor_brief=None, profile=None)
```

| Argument                    | Meaning                                                                                                                                                                                                                                                                                                                                   |
| --------------------------- | ----------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `org`, `namespace`, `agent` | The identity stamped on every call. `user_id`, `session_id` and `run_id` are per call (the wire's `"-"` when omitted).                                                                                                                                                                                                                    |
| `timeout_s`                 | Per-request timeout.                                                                                                                                                                                                                                                                                                                      |
| `profile`                   | The `loom login` session to use, on the server it signed in to; not combined with `api_key` or another `base_url` ([Credentials](#credentials)).                                                                                                                                                                                          |
| `consumer`                  | The product this process speaks for; stamped on every body that does not name its own and stored on the ledgers. `""` stamps nothing.                                                                                                                                                                                                     |
| `pacing`                    | Read `X-RateLimit-*`, sleep a refill tick when the budget is at the floor, and wait out a 429 after `Retry-After` (each sleep capped at 5 s) for up to `ratelimit_max_wait_s` (15 s by default) per call. `False` raises `RateLimited` at once.                                                                                           |
| `retry`                     | A `RetryPolicy` (or a dict of its fields); see [Retries](#retries-and-logging).                                                                                                                                                                                                                                                           |
| `coalesce_sets`             | From the third `memory.set` inside one 200 ms window, gather sets with the same identity into one `memory.bulk_set`. A coalesced result carries `coalesced: true` and no `memory_id` / `content_sha256`; a set with `if_match_sha256` or an expiry policy is never coalesced. `memory.flush_sets()` sends a batch now; `close()` flushes. |
| `auto_run`                  | Bind an ambient run to the Agent's lifetime (or `LOOM_AUTO_RUN=1`): writes carry its `run_id`, reads are labelled with `X-Loom-Run-Id`. Off by default, because a write under a run is scoped to it: a restatement of another scope's row is stored as a duplicate, not merged.                                                           |
| `mentor_brief`              | Fetch `mentor.brief` in the background on the first call (or `LOOM_MENTOR_BRIEF=on`); `await bot.mentor()` does it explicitly.                                                                                                                                                                                                            |

An `agent` of `""`, `"-"` or `"*"` is replaced with `sdk-python:<script>` and a
warning: an anonymous agent is indistinguishable from every other anonymous
caller to the ledger, the mentor and the cost views.

`Datapack(org, namespace, **options)` takes every `Agent` option as a default,
and `.agent(name, **overrides)` spawns an `Agent`; each spawned agent has its
own connection pool. `SyncAgent` takes the same arguments plus
`call_timeout_s` and exposes every method as a blocking call. It runs one event
loop on a background thread, is thread-safe, and should be constructed once per
process.

## Calling operations

- **Group clients.** The `Agent` exposes one client module per op group:
  `memory`, `working_memory`, `knowledge`, `conversations`, `profile`,
  `context`, `facts`, `learnings`, `conflicts`, `blacklist`, `grants`,
  `erasure`, `cost`, `audit`, `namespaces`, `procedure`, `run`, `tasks`,
  `policy`, `decision`, `calibration`, `briefs`, `code`, `tracker`. Methods map
  to ops with Python argument names. `memory.search` and `knowledge.search`
  also accept `**extra`, so a field newer than the SDK still rides the body.
- **Any op by name.** `await bot.call("memory.move", {"from_key": "a", "to_key": "b"})`
  fills the identity (`user_id=`, `session_id=`, `run_id=` override it per
  call), adds a fresh `Idempotency-Key` unless you pass `idempotency_key=`
  (the server honours it only for registry-idempotent ops), and returns the response dict. This is the path for an op or a field the group
  clients do not cover.
- **Response metadata.** `call_with_meta(...)` returns a `CallResult` with
  `data`, `status`, `request_id` (finds the call in the server's ledger),
  `schema_version`, `server_timing_ms`, `rate_limit`, `attempts`, `hint`
  (the server's `X-Loom-Hint`) and `hint_code` (`X-Loom-Hint-Code`).
- **What the deployment serves.**
  `await bot.probe(ops=[...], fields={op: [field, ...]})` compares your needs
  with `ops.capabilities` and returns `ok`, `missing_ops`, `missing_fields` and
  `unknown_to_sdk` (ops the server serves that this SDK version does not know).
  `await bot.capabilities()` pairs the catalogue with the token's own claims
  (decoded, not verified) and is cached 60 s: `caps.can_call(op)` is `True`,
  `False`, or `None` when the SDK cannot tell; `caps.callable()` lists every
  served op the token may call; `await bot.can_call(op)` answers for the
  Agent's namespace.

### Typed shapes

`loom.ops` is generated from the server's registry: `OPS` (per op: idempotency,
role, maturity, latency budget, audit), `OpName` (a `Literal` of every op) and a
`TypedDict` per input, body and output model (`MemorySearchInput`,
`MemorySearchBody`, `MemorySearchOutput`, …) with `Required[...]` on the fields
the server requires. A `…Body` type is the input with the identity keys
optional, which is what you write when `call` stamps the identity. The SDK
stays dict-in, dict-out; the types are for editors and type checkers:

```python
from loom.ops import OPS, MemorySearchBody

body: MemorySearchBody = {"query": "deploy window", "top_k": 5}
out = await bot.call("memory.search", body, user_id="u-42")
print(OPS["memory.search"].idempotent, OPS["memory.search"].role)
```

## Sessions and tools

What an agent framework holds between turns:

- `bot.session(session_id, user_id=..., flush_every=8)` binds one (user,
  session) scope: `resume()` returns the context pack as `system` plus the
  transcript tail as `messages`; `append(*turns)` writes the transcript now and
  runs `memory.set_from_messages` every `flush_every` turns; `offload()` /
  `fetch()` park a large tool result behind a pointer and read it back;
  `note()` appends to working memory; `flush()` and `close()` flush. It is an
  async context manager.
- `bot.tools(user_id=..., session_id=...)` hands a model six Loom verbs as tool
  definitions, `.definitions("openai" | "anthropic" | "generic")`, with one
  `await execute(name, arguments)` that returns the string the model reads:
  `loom_remember`, `loom_recall`, `loom_ask`, `loom_offload`, `loom_fetch`,
  `loom_note`. The schemas match the TypeScript SDK's.
- `bot.run_scope(goal=...)` is an `async with` block that begins a run, stamps
  its `run_id` on the writes inside and labels the reads with it (the
  `X-Loom-Run-Id` header, not a filter), and on exit credits the keys a
  non-abstained `memory.ask` cited as the run's `memories_used`. See
  [Runs](../features/runs.md).

```python
async with bot.session("thread-42", user_id="u1") as s:
    resumed = await s.resume(query="resume the billing migration")
    system_prompt, history = resumed.system, resumed.messages
    await s.append({"role": "user", "content": text}, {"role": "assistant", "content": reply})
    blob = await s.offload(build_log, summary="pytest run 812")
    await s.note(blob["pointer"])
```

`await bot.mentor()` asks the store what the agent's calls are missing (planes
it never uses, with the counts behind each hint) and logs up to three hints.
`await bot.guide()` returns how to use Loom as Markdown (`mentor.guide`), for the
system prompt of an agent with no plugin; `part="reference"` lists every plane.

## Framework adapters

- `loom.integrations.langgraph.LoomStore(agent, tags=...)` is a LangGraph
  `BaseStore`: `put` is `memory.set`, `search(query=...)` ranks with
  `memory.search` and reads values with `memory.bulk_get`, and namespace tuples
  become key prefixes and tags. Async graphs use `aput` / `aget` / `asearch` /
  `abatch`; the sync methods run on a loop the store owns, which `store.close()`
  shuts down with the agent.
- `loom.integrations.strands.LoomMemoryStore(agent, tags=...)` implements the
  Strands Agents memory-store protocol (`add` / `search`); `search` returns
  nothing when Loom abstains unless `honour_abstain=False`.

Both import their framework lazily. See
[Frameworks](../features/integrations/frameworks.md).

## Errors

Every error subclasses `loom.errors.LoomError` and carries `.code` (the
server's `detail.code`), `.status`, `.request_id` and `.detail` (the server's
whole `detail` object: a 422's `errors`, a `SCHEMA_PENDING`'s `table` and
`migration`). The class follows `detail.code`, else the HTTP status when the
body has no code (`loom.errors.from_response`); a 429 is always `RateLimited`
and a 5xx always a `BackendUnavailable`.

`.category` (an `ErrorCategory`, one of six names) and `.retryable` read the server's
`detail` ([the error envelope](./errors.md#the-error-envelope-over-http)). For an error
whose body has neither, or one the SDK raised itself:

- a connection the server never saw (code `NETWORK`) is `dependency` and retryable;
- a call that may have run (a response that was cut off, a write that broke off, or one
  that did not arrive in time) is `dependency` and retryable only on an idempotent op;
- a request that could not be sent (code `NETWORK`) is not retryable on any op: `input`
  for one httpx will not send (an invalid header), `dependency` for a proxy refusal;
- a 2xx whose body is not a JSON object (`INVALID_RESPONSE`) is `internal`;
- a `base_url` that is not an http(s) URL with a host is refused when the transport is
  built, with an `input` `LoomError`, not retryable;
- an error raised before sending is `dependency` and retryable when a service the SDK
  needed first was down (an identity provider, a secret store; a sign-in answered 429 is
  `capacity`), `auth` when the caller is not signed in, else `input`;
- a code the SDK never retries is `configuration` (`ANALYSIS_UNAVAILABLE`,
  `CODED_WIDTH_MISMATCH`, `GATEWAY_REQUIRED`), `internal` (`MODEL_CALL_UNATTRIBUTED`) or
  `dependency` (`PUBLICATION_PENDING`: the write is committed and its rows follow), not
  retryable;
- a 5xx on an op that is not idempotent is not retryable, since the call may have written,
  unless its code proves the handler never ran (`AUTH_UNAVAILABLE`). This is stricter than
  the server, which also keeps a refusal it makes before dispatch retryable
  (`CONFIG_DRIFT`, `MODEL_CHANGE_IN_PROGRESS`, an `OVERLOADED` from the auth gate): a body
  without the fields does not say where it came from;
- any other code is classed by its HTTP status alone, so one the server lists with its own
  class (the [error codes](./errors.md#error-codes) say which) can read differently there.

`knowledge.wait_for_ingest` running out raises the built-in `TimeoutError`, not a
`LoomError`, so it has neither field.

| Class                  | Code(s)                                                                         | Notes                                                                                                                                        |
| ---------------------- | ------------------------------------------------------------------------------- | -------------------------------------------------------------------------------------------------------------------------------------------- |
| `ValidationError`      | `VALIDATION_FAILED`; status 400, 422                                            |                                                                                                                                              |
| `AuthInvalid`          | `AUTH_INVALID`; status 401                                                      |                                                                                                                                              |
| `AuthInsufficient`     | `AUTH_INSUFFICIENT`; status 403                                                 |                                                                                                                                              |
| `NotFound`             | `RESOURCE_NOT_FOUND`; status 404                                                |                                                                                                                                              |
| `PreconditionFailed`   | `PRECONDITION_FAILED`; status 409, 412 without a known code                     | `.current_sha256`: retry without re-reading                                                                                                  |
| `IdempotentConflict`   | `IDEMPOTENT_CONFLICT`                                                           |                                                                                                                                              |
| `BackendUnavailable`   | `BACKEND_UNAVAILABLE`, any 5xx, and `NETWORK` (no response)                     | `cannot reach <base_url>: …`, `no response from <base_url>: …` (a write that broke off, a timeout), or `response from <base_url> cut off: …` |
| `RateLimited`          | `RATE_LIMITED`; status 429                                                      | subclass of `BackendUnavailable`; `.retry_after_s`                                                                                           |
| `Overloaded`           | `OVERLOADED`                                                                    | subclass of `BackendUnavailable`; `.retry_after_s`                                                                                           |
| `BackendTimeout`       | `BACKEND_TIMEOUT`                                                               | subclass of `BackendUnavailable`                                                                                                             |
| `SchemaPending`        | `SCHEMA_PENDING`                                                                | subclass of `BackendUnavailable`                                                                                                             |
| `IdempotentInProgress` | `IDEMPOTENT_IN_PROGRESS`                                                        | subclass of `BackendUnavailable`; `.retry_after_s`; retried by the transport                                                                 |
| `LoomError`            | any other code on any other status (423 `LOCKED`, …), and `OVERRIDE_NOT_STORED` | match on `.code`; a 409 `PINNED_CONFLICT` is a `PreconditionFailed`                                                                          |
| `LoomError`            | `INVALID_RESPONSE`                                                              | a 2xx body that is not a JSON object                                                                                                         |

A model call the inference gateway refused for its identity raises `AuthInsufficient`
with code `MODEL_CALL_REFUSED` (403, never retried). One it asked to wait raises
`BackendUnavailable` with code `MODEL_CALL_DEFERRED`, which has no `.retry_after_s`: the
wait is in `.detail["retry_after_s"]` and the `Retry-After` header, which the transport's
retries of an idempotent op honour.

Only `PRECONDITION_FAILED` carries `.current_sha256`. The other 409s that land in
`PreconditionFailed` (`PINNED_CONFLICT`, `GRANT_WOULD_DOWNGRADE`, `READS_LLM_FREE`)
are not fixed by resending; match on `.code`.

`loom` re-exports `LoomError`, `ValidationError`, `AuthInvalid`,
`AuthInsufficient`, `NotFound`, `PreconditionFailed`, `IdempotentConflict`,
`BackendUnavailable` and `RateLimited`, with the two codes
`NETWORK_ERROR_CODE` and `INVALID_RESPONSE_CODE`; `Overloaded`,
`BackendTimeout`, `SchemaPending` and `IdempotentInProgress` import from `loom.errors`. What each server
code means and what to do about it is in the [errors reference](./errors.md).

```python
from loom import PreconditionFailed, RateLimited

try:
    await bot.memory.set("deploy_window", new_value, if_match_sha256=seen_sha)
except PreconditionFailed as e:
    print("changed under us; live hash", e.current_sha256)
except RateLimited as e:
    print("retry in", e.retry_after_s)
```

## Retries and logging

The retry policy is one frozen dataclass, `RetryPolicy`, set on the Agent and
overridable per call with a `RetryPolicy` or a dict of its fields; every field
is validated and named when wrong. Defaults: 3 attempts; 0.1 s backoff doubling
to 5 s with jitter; 408 and 5xx retried for registry-idempotent ops (any other
op only on a 408 or a 503 `AUTH_UNAVAILABLE`, which the server answers before
running it), except an answer the server classes `configuration`, which only an
operator's fix ends; connection errors retried; a call that may have run (timed
out, cut off or broken off) retried only for idempotent ops that are not
read-only (`retry_timeouts="idempotent"`), since the server deduplicates their
`Idempotency-Key` (a timed-out `memory.ask` is not run twice); a `Retry-After`
on a retried status honoured up to 60 s. A 429 is paced, not retried.

```python
from loom import Agent, RetryPolicy

bot = Agent("acme", "support", "bot", retry=RetryPolicy(max_attempts=5, total_backoff_budget_s=30))
await bot.call("memory.get", memory_key="k", retry={"max_attempts": 1})  # this call: no retries
```

The SDK logs through the stdlib logger `loom` (a `NullHandler` by default): one
line per completed call at INFO, retries and rate-limit waits at WARNING, the
final failure at ERROR, request headers at DEBUG. `LOOM_LOG_LEVEL` (a stdlib
level name, or `off`) sets the logger's level when your application has not.
Every request carries `X-Request-Id` (the server echoes it; a `request_id` that is not
1 to 64 letters, digits, `.`, `_` or `-` is replaced by a generated one before sending,
with a WARNING; every error
carries it as `.request_id`), `Idempotency-Key`, `User-Agent` and `X-Loom-SDK`
(`loom-py/<version>`), `X-Loom-Runtime` and `X-Loom-Retry-Count`.

## The generated op table

`sdk/python/loom/ops.py` is generated from `server.operations.registry.ALL_OPERATIONS` by
`python scripts/codegen/sdk_py_gen.py`; CI runs it with `--check`, so a server
change that alters an op cannot merge without regenerating it. A new op is then
callable by name and as `agent.client.<group>.<method>(body)` (generated by
`python scripts/codegen/sdk_py_client_gen.py` into `sdk/python/loom/client.py`,
the same ops at the same names as the TypeScript `LoomClient`) with no other
SDK change; a method on the hand-written group clients is added only when an
idiomatic signature earns it. The package README,
[`sdk/python/README.md`](../../sdk/python/README.md), is what ships to the
package index. See [Code generation](../development/code-generation.md).

## Versioning

The SDK's version is the product release version; the server does not gate on
it. An older SDK keeps working against a newer server, and a newer SDK works for
every op and field the older server serves: guard newer ones with `probe` or
`capabilities()` rather than on version numbers. See
[Client and server versions](./http-api.md#client-and-server-versions). Each release's changes are in
its [GitHub Release](https://github.com/ClickHouse/loom/releases) notes.

## Examples

- [`sdk/python/examples/agent_memory.py`](../../sdk/python/examples/agent_memory.py):
  probe → remember → recall → ask with citations → outcome → typed errors.
- [`examples/`](../../examples/): `claude_agent_loop.py` (needs an Anthropic
  key), `ledger_run.py`, `log_conversation.py`, `onboarding_walkthrough.py`.

## Related

- [TypeScript SDK](./typescript-sdk.md): the same surface in TypeScript.
- [HTTP API](./http-api.md) and [Operations reference](./operations.md).
- [Agent integration](../guides/agent-integration.md): wiring an agent loop end to end.
- [Headless bots](../guides/headless-bots.md).
