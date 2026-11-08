# clickhouse-loom

Python SDK for [Loom](https://github.com/ClickHouse/loom) — the shared-production memory, knowledge, conflict, and observability data plane for AI agents, built on ClickHouse.

This package contains the **client SDK only**. Loom is designed for shared production deployment: an operator runs the server (Docker image at `ghcr.io/clickhouse/loom`) against a real ClickHouse instance (Cloud or self-hosted OSS), then application developers like you use this SDK to talk to it.

## Install

Not on PyPI yet, so install from source — pure Python, no build step — or from
the wheel attached to a GitHub Release
([docs/development/release-process.md](https://github.com/ClickHouse/loom/blob/main/docs/development/release-process.md#installing-from-a-github-release)):

```bash
pip install ./sdk/python                     # from a clone of ClickHouse/loom
# without a clone (needs the same GitHub access as `git clone`):
pip install "clickhouse-loom @ git+https://github.com/ClickHouse/loom#subdirectory=sdk/python"
```

<!-- restore `pip install clickhouse-loom` when release.yml publishes -->

Python 3.11+. Runtime deps: `httpx[http2]` (HTTP/2 via `h2`).

## Use

You'll need:

- A `base_url` pointing at your team's Loom deployment.
- A credential. Either sign in once with `python -m loom login --server <url>` (or `loom.login(url)` in a notebook), approving on any browser with your password or SSO; every `Agent` you point at that server (`base_url=` or `LOOM_BASE_URL`) then uses that session and refreshes it. Or use a JWT your operator mints (`POST /v1/admin/token.mint`, `loom mint-token`), passed as `api_key=` or `LOOM_API_KEY` — the right choice for CI and services.

```python
from loom import Datapack
import os

dp = Datapack(
    org="clickhouse",
    namespace="eng-tooling",
    base_url=os.environ["LOOM_BASE_URL"],  # e.g. https://loom.example.com
    api_key=os.environ["LOOM_API_KEY"],  # JWT from your operator
)

agent = dp.agent("claude-desktop")

# Save a memory
await agent.memory.set(
    memory_key="deploy_window",
    value="Deploy Tue/Thu, never Friday",
    memory_type="preference",
)

# Recall by key
got = await agent.memory.get(memory_key="deploy_window")

# Or semantic search
hits = await agent.memory.search("when do we deploy")
```

The `Agent` exposes 27 client modules: `client`, `account`, `deployment`, `memory`, `working_memory`, `knowledge`, `conversations`, `profile`, `context`, `facts`, `learnings`, `conflicts`, `blacklist`, `grants`, `erasure`, `cost`, `audit`, `namespaces`, `procedure`, `run`, `tasks`, `policy`, `decision`, `calibration`, `briefs`, `code`, `tracker`. Each of the named ones is the hand-written layer over a wire-op group on the Loom server (`account` and `deployment` cover the routes outside the op registry: service accounts, MCP setup, model settings, worker health); `client` holds every served op as a generated method (below).

Full guide: [docs/reference/python-sdk.md](https://github.com/ClickHouse/loom/blob/main/docs/reference/python-sdk.md). Deployment paths (how an operator stands up the server): [docs/operations/deployment.md](https://github.com/ClickHouse/loom/blob/main/docs/operations/deployment.md).

## Every op by name, any op by call, a probe before you rely

`loom.ops` is generated from the server's operation registry: `OPS` (name → idempotency, required role, maturity, latency budget), `OpName`, and one `TypedDict` per request / response (`MemorySearchInput`, …) with `Required[...]` on the fields the server requires — names for your editor and type checker while the SDK stays dict-in / dict-out. `MemorySearchBody` is the same shape with the identity keys (`org`, `namespace`, `agent`, `user_id`, `session_id`, `run_id`) optional: what you write when `agent.call` stamps the identity for you.

`agent.client` has one method per served op, generated from the same registry: the group is the op name's first segment and the method the rest with `.` → `_`, so `tracker.source.add` is `agent.client.tracker.source_add({...})`. It takes the op's `<Op>Body`, returns its `<Op>Output`, and passes `user_id=`, `session_id=`, `timeout_s=` and the other `agent.call` options through. The TypeScript SDK exposes the same ops at the same group, camel-cased: `loom.tracker.sourceAdd({...})`; a unit test fails when the two differ.

```python
from loom.ops import OPS, MemorySearchBody

body: MemorySearchBody = {"query": "deploy window", "top_k": 5, "exclude_keys": ["seen_1"]}
out = await agent.call(
    "memory.search", body, user_id="u-42"
)  # identity stamped; Idempotency-Key when OPS says idempotent
p = await agent.probe(
    ops=["memory.ask", "context.pack"], fields={"memory.search": ["exclude_keys"]}
)
assert p["ok"], (p["missing_ops"], p["missing_fields"])
print(OPS["memory.search"].idempotent, OPS["memory.search"].role)
```

`agent.memory.search(...)` and `agent.knowledge.search(...)` also accept `**extra`, so a field the server shipped this week rides the body without an SDK release. Errors are typed by the server's `detail.code`, and by HTTP status when a body carries none: `NotFound`, `ValidationError`, `AuthInvalid`, `AuthInsufficient`, `PreconditionFailed`, `RateLimited`, `BackendUnavailable` — every one carries `.request_id` (the `X-Request-Id` the server echoed), `.status`, and the server's `.category` and `.retryable`, the SDK's own when the body names none ([the error envelope](../../docs/reference/errors.md#the-error-envelope-over-http)).

`await agent.capabilities()` pairs `ops.capabilities` with what the bearer token itself says (`roles` / `ns_roles`, decoded, not verified) so an integration does not poll ops its token cannot call: `caps.can_call("conflicts.list")` is `True` / `False` when known and `None` when the SDK cannot tell — then make the call; `await agent.can_call(op)` answers for the Agent's namespace; `caps.callable()` lists every served op the token may call. Cached 60 s.

`agent.call_with_meta(...)` is the same call returning a `CallResult`: `.data`, `.request_id` (finds the call in the server's ledger), `.schema_version`, `.server_timing_ms`, `.rate_limit` and `.attempts`. Every request identifies itself (`User-Agent` / `X-Loom-SDK: loom-py/<version>`, `X-Loom-Runtime`, `X-Loom-Retry-Count`); `Retry-After` is honoured as seconds or an HTTP-date. The credential never appears in `repr(agent.transport)` (it shows `api_key=…1234`), and a blank `LOOM_API_KEY` is treated as unset. The package ships `py.typed` and passes `mypy --strict`.

## Give your agent the guide

`await agent.guide()` returns how to use Loom, written for a model: the loop,
where each kind of state goes and how to read what comes back
(the experimental `mentor.guide`, the `loom` skill in `skills/loom/`). Put it in the system
prompt of an agent that has no plugin to carry it; `part="reference"` returns
every plane with its ops.

```python
system_prompt = "You triage support tickets.\n\n" + await agent.guide()
```

## Ask the mentor

The store reads what your agent actually calls and names the planes it never
uses — the code index, the tracker source, documents, records, offload,
the entity graph — with the counts behind each hint. Two channels need nothing
from you: every successful response may carry one `X-Loom-Hint`, which the
transport logs at INFO once per distinct text and exposes as
`CallResult.hint`, and a `context.pack` at session start carries up to three
hints. A bot that never renders a pack asks once at startup:

```python
async with Agent(org="acme", namespace="support", agent="triage-bot") as bot:
    brief = await bot.mentor()  # logs up to three hints; returns the brief
    for h in brief["hints"]:
        print(h["code"], h["severity"], h["text"], h["evidence"])
```

`mentor_brief=True` (or `LOOM_MENTOR_BRIEF=on`) runs it in the background on
the first call instead. Name your agent: `""`, `"-"` and `"*"` default to
`sdk-python:<script>` with a warning, because an anonymous agent is every
other anonymous caller to the ledger, the mentor and the cost views.

## Sessions and tools: what an agent framework holds

`agent.session(id, user_id=...)` binds one (user, session) scope to the verbs an agent loop needs between turns: `resume()` returns the context pack as `system` plus the transcript tail as `messages`; `append(*turns)` writes the transcript now and runs `memory.set_from_messages` every `flush_every` turns; `offload()` / `fetch()` park a large tool result and read it back by pattern; `note()` appends to working memory; `close()` flushes. It is an async context manager; `SyncAgent.session()` blocks.

```python
async with agent.session("thread-42", user_id="u1") as s:
    resumed = await s.resume(query="resume the billing migration")
    system_prompt, history = resumed.system, resumed.messages
    await s.append({"role": "user", "content": text}, {"role": "assistant", "content": reply})
    blob = await s.offload(build_log, summary="pytest run 812")
    await s.note(blob["pointer"])
```

`agent.tools(user_id=..., session_id=...)` gives a model six loom verbs — `loom_remember`, `loom_recall`, `loom_ask`, `loom_offload`, `loom_fetch`, `loom_note` — as `definitions("openai" | "anthropic" | "generic")` and one `await execute(name, arguments)` that returns the string the model reads. The schemas are the same as the TypeScript SDK's.

## Retries and logging

The retry policy is one frozen dataclass, set on the Agent and overridable per call with a `RetryPolicy` or a dict of its fields; every field is validated and named when wrong. Defaults: 3 attempts; 0.1 s backoff doubling to 5 s with ±25 % jitter; 408 and 5xx retried for registry-idempotent ops (any other op only on a 408 or a 503 `AUTH_UNAVAILABLE`, which the server answers before running it), except an answer the server classes `configuration`; connection errors retried; a read timeout, a cut-off response or a write that broke off retried only for an op the registry marks idempotent and not read-only (the server dedups its `Idempotency-Key`); a `Retry-After` on a retried status honoured up to 60 s. A 429 is paced, not retried.

```python
from loom import Agent, RetryPolicy

agent = Agent(
    "acme",
    "support",
    "bot",
    retry=RetryPolicy(max_attempts=5, total_backoff_budget_s=30),
    consumer="support-bot",
)  # the label every ledger row carries
await agent.call("memory.get", memory_key="k", retry={"max_attempts": 1})  # this call: no retries
await agent.call("memory.set_from_messages", messages=msgs, retry={"retry_timeouts": "always"})
```

The SDK logs through the stdlib logger `loom` (a `NullHandler` by default; your logging config routes it): one line per completed call at INFO (`/v1/memory.get 200 12 ms attempts=2 rid=…`), retries and rate-limit waits at WARNING, the final failure at ERROR, the request headers at DEBUG. `LOOM_LOG_LEVEL` (any stdlib level name — `debug`, `info`, `warning`, `error`, `critical` — or `off`) sets the logger's level when your application has not; a value it cannot read is a warning, not an import error. Credential headers are redacted to their scheme and last four characters, and a filter on the logger rewrites any bearer token that reaches a record anyway.

## Examples

[`examples/agent_memory.py`](./examples/agent_memory.py) — probe → remember ×3 → recall → ask with citations → outcome → typed errors, with its actual run in [`examples/README.md`](./examples/README.md). The TypeScript SDK ships the same example.

## License

Apache-2.0. See [LICENSE](https://github.com/ClickHouse/loom/blob/main/LICENSE).
