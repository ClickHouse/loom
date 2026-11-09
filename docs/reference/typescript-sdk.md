# TypeScript SDK reference

The TypeScript SDK (`@clickhouse/loom`, source in `sdk/typescript/src/`) is a typed client
over the [HTTP API](./http-api.md). Its op types are generated from the server's operation
registry. It behaves like the [Python SDK](./python-sdk.md): identity binding, an
`Idempotency-Key` for every idempotent op, pacing on the rate-limit headers, retries, a
`consumer` tag, and typed errors that carry the server's `code`.

## Install

Node 20 or later (it uses the platform `fetch`). No runtime dependencies. The package is not
published to a package index and `dist/` is not committed, so build it from a clone:

```bash
git clone https://github.com/ClickHouse/loom
npm --prefix loom/sdk/typescript ci && npm --prefix loom/sdk/typescript run build
npm install ./loom/sdk/typescript          # or: npm pack in that directory, then install the .tgz
```

The package ships ESM and CommonJS builds with their own declaration files and
imports no Node built-in when it loads; signing in loads `node:fs` and the
other built-ins it needs on first use. The Strands adapter is a second entry
point, `@clickhouse/loom/integrations/strands`.

## Credentials

The client picks its server and credential as the Python SDK does (`readEnv`).
The `profile` option names a `loom login` session and its server (Node 20.16
or later, 22.3 or later on Node 22). Otherwise
`baseUrl` wins, else `LOOM_BASE_URL`, else `http://localhost:7777`, and the
credential is `tokenProvider` or `apiKey`, then `LOOM_API_KEY`, then a bot's
`LOOM_CLIENT_ID` with its key or workload token (`MachineLogin`), then the
session `login()` / `loom login` stored in `~/.loom/credentials` for that
server (profile `LOOM_PROFILE`, default `default`; it refreshes itself), and
finally the first line of `~/.loom/token`, the file `loom mint-token --save`
writes. The last two are read in Node only, on the first request. A stored
session is used only for the server it signed in to.

`login(server?, { profile })` signs in with the OAuth device flow and stores
the session in the file the Python SDK and the CLI share; `logout(profile?)`
revokes it and `whoami(profile?)` asks the server who it says you are. A
refresh holds the lock directory `~/.loom/.credentials.lock.d`, which the
Python SDK takes too. A process takes it by renaming a private directory that
holds an owner file (its pid and host) onto that path, and touches it every
10 s by its own clock while it holds it. A waiter breaks a lock untouched for
60 s, or one untouched for 20 s whose holder on this machine is no longer
running; a Python waiter on Windows does not check the pid and waits the 60 s.
Breaking a lock and releasing one both rename it away under the claim
directory `~/.loom/.credentials.lock.d.breaking`, so a break never renames a
lock taken after a release.

A blank or whitespace key counts as no key, so an exported-but-empty
variable does not become `Authorization: Bearer `. The key is held in an
ECMAScript private field: `JSON.stringify` shows `apiKey: "…1234"` and
`util.inspect` shows no key at all.

## Quick example

```ts
import { Datapack } from "@clickhouse/loom";

const pack = new Datapack("acme", "support", {
  baseUrl: process.env.LOOM_BASE_URL!,
  apiKey: process.env.LOOM_API_KEY,
  consumer: "support-bot",
});
const bot = pack.agent("assistant");

await bot.remember("deploy_window", "Deploys run Tue and Thu only; never Friday.");
const hits = await bot.recall("when can we deploy?", { top_k: 5 });
const answer = await bot.ask("When is the deploy window?");
if (!answer.abstain) console.log(answer.answer, answer.memory_keys);
```

`ask` synthesizes an answer only with a real LLM provider configured on the
server; on the stock stack's fake providers it abstains or returns a stub.

## Clients

- **`LoomClient(options)`** is the whole surface.
  - `client.call("memory.search", input)` is typed end to end through the
    generated `OpMap`.
  - `client.memory.search(input)` is the same call through a generated group
    accessor; multi-word verbs are camel-cased (`memory.bulk_set` →
    `client.memory.bulkSet`).
  - `callWithResponse(name, input)` returns the body with `requestId`,
    `schemaVersion`, `serverTimingMs`, `rateLimit`, `attempts` and the server's
    `hint` / `hintCode`.
  - `callRaw(name, body)` calls an op the generated table does not know (a newer
    server): untyped, and with no `Idempotency-Key` unless you pass
    `idempotencyKey`.
  - `capabilities()` pairs `ops.capabilities` with the token's own claims
    (decoded, not verified), cached 60 s; `spec(name)` reads one op's generated
    spec.
- **`Agent(org, namespace, agent, options | client)`** fills the identity
  `(org, namespace, agent)` on every request, with `userId`, `sessionId` and
  `runId` per call. Its verbs: `remember`, `recall`, `ask`, `ingest`,
  `outcome`, `contextPack`, `canCall`, `mentor`, `guide`, `session(sessionId)` and
  `tools(scope)`. An agent named `""`, `"-"` or `"*"` becomes
  `sdk-ts:<script>` with a warning.
- **`Datapack(org, namespace, options | client).agent(name)`** spawns agents
  that share one client.

### Options

`LoomClientOptions`: `baseUrl`, `apiKey`, `timeoutMs`, `retry`,
`ratelimitMaxWaitS` (15 s by default; 0 disables 429 waiting), `consumer`,
`onBehalfOf` (sent as `X-Loom-On-Behalf-Of`; the server honours it only when
the token's subject is listed in `LOOM_TRUSTED_CALLERS`), `headers`, `logger`, `logLevel`, and injectable
`fetch`, `sleep` and `now` for tests. Per call (`CallOptions`): `timeoutMs`,
`idempotencyKey`, `requestId`, `signal` and `retry`.

## Typed operations

`src/generated/ops.ts` is produced by `python scripts/codegen/sdk_ts_gen.py`
from `server.operations.registry.ALL_OPERATIONS`: one interface per Pydantic input and
output model, shared definitions hoisted once. `OpMap` binds every op name to
its input and output; `OPS` is the table of specs (group, method, required
role, idempotency, maturity, latency budget); `GENERATED_OP_COUNT` is its size.
Dates ride the wire as ISO-8601 strings; unconstrained fields are `unknown`. CI
runs the generator with `--check`, so a server change that alters a request or
response shape cannot merge without regenerating the types.

```ts
import { LoomClient } from "@clickhouse/loom";

const loom = new LoomClient({ baseUrl, apiKey });
const out = await loom.call("memory.search", { org, namespace, query: "deploy window", top_k: 10 });
const same = await loom.memory.search({ org, namespace, query: "deploy window" });
```

### Files

`loom.knowledge` also sends a file for the server's extractor to parse
([ingesting files](../features/knowledge.md#ingesting-files)); the SDK parses nothing.

- `upload(scope, data, filename)` posts the bytes (`Uint8Array`, `ArrayBuffer` or a `Blob`
  such as a browser `File`) to `/v1/knowledge.upload` with their sha256 (none where Web
  Crypto is missing, as on a browser page served over plain http). It is paced on a
  429 as an op is, retried after a connection failure, and on an answer only when it
  proves the server never ran it (a retry after a connection that dropped once the file
  was stored leaves a second upload, which expires unused).
- `waitForIngest(scope, ingestionId)` polls `knowledge.ingest_status` until the job ends: a
  `TimeoutError` after `timeoutMs` (600 s by default), at once for a job not found, and an
  abort through `signal` stops it. It polls on real timers, not a `Transport`'s injected
  `sleep`.
- `ingestFile(...)` does both around `knowledge.ingest`, titling the document by the file
  name less its extension unless `title` says otherwise. `waitTimeoutMs` bounds the wait;
  `call` holds the upload's and the ingest's own request options (`timeoutMs` for a large
  file over a slow link), and `call.signal` also stops the wait.

```ts
const status = await loom.knowledge.ingestFile({
  org,
  namespace,
  data: new TextEncoder().encode("# Runbook"),
  filename: "runbook.md",
  source_uri: "repo://runbook.md",
});
// status.state: ready | partial | failed | cancelled
```

### The assistant

`loom.assistant.stream({ namespace, question, history, signal, readTimeoutMs })` asks the Loom
assistant through `POST /v1/assistant/chat/completions` as the client's token and
yields `{ type: "text", text }` for each piece of the answer, then one
`{ type: "answer", answer }` with its `runId`, usage and the citations the server checked
(`{ kind, text, verified, reason, ref }`). A refusal throws its typed error before any
text; an `error` event after the answer began throws the same way. An answer that began
is never resent, nor one refused with a 429; a 401 with a token provider, which comes
before the answer starts, is sent once more with a fresh token. `readTimeoutMs`
(300 000) bounds the request and then each read, as an answer is silent while a tool
call runs: past it the stream throws `TimeoutError`. An abort through `signal` closes
the answer on the server too. `loom.assistant.ask` is the op: one whole answer.

### Probe before you rely

```ts
import { probe } from "@clickhouse/loom";

const p = await probe(loom, {
  ops: ["memory.ask", "context.pack"],
  fields: { "memory.search": ["exclude_keys"] },
});
if (!p.ok) console.warn(p.missingOps, p.missingFields);
```

`p.unknownToSdk` names ops the server has that this SDK version does not.
`caps.canCall(op, namespace)` is three-valued: `true` or `false` when both the
op's role and the token's role are known, `undefined` when the SDK cannot tell,
in which case make the call and let the server decide.

## Sessions and tools

`bot.session(id, { userId, flushEvery })` binds one (user, session) scope:
`resume({ query })` returns `context.pack` as `system` plus the transcript tail
as `messages`; `append(...turns)` writes `conversation.append` now and runs
`memory.set_from_messages` every `flushEvery` turns; `offload(text, { summary })`
parks a large tool result (`context.offload`) and `fetch(blobKey, { pattern })`
reads it back; `note()` appends to working memory; `close()` flushes. If the
namespace has conversation logging off, the session warns once, sets
`transcriptStored = false` and keeps extracting.

`bot.tools(scope)` gives a model six Loom verbs (`loom_remember`,
`loom_recall`, `loom_ask`, `loom_offload`, `loom_fetch`, `loom_note`) as
`definitions("openai" | "anthropic" | "generic")`, and
one `execute(name, args)` dispatcher that returns the string the model reads,
validates required arguments, and truncates past `maxResultChars`. The schemas
match the Python SDK's.

```ts
const session = bot.session("thread-42", { userId: "u1", flushEvery: 8 });
const { system, messages } = await session.resume({ query: "resume the billing migration" });
await session.append({ role: "user", content: userText }, { role: "assistant", content: reply });
const blob = await session.offload(buildLog, { summary: "pytest run 812" });
await session.note(blob.pointer);
await session.close();
```

## Pagination

`paginate(fetchPage)` walks a cursor-paginated op until `next_cursor` stops;
`collect(gen, limit)` gathers the items:

```ts
import { collect, paginate } from "@clickhouse/loom";

const recentAudit = await collect(
  paginate((cursor) => loom.audit.list({ org, namespace, cursor })),
  50,
);
```

## Strands adapter

`LoomMemoryStore` from `@clickhouse/loom/integrations/strands` implements the
Strands Agents memory-store shape structurally (no dependency on the Strands
package). A write is one `memory.set` keyed `strands/<sha256 prefix>` of the
content, so re-adding a fact is idempotent and both SDKs write the same key; a
search returns nothing when Loom abstains unless `honourAbstain: false`. See
[Frameworks](../features/integrations/frameworks.md).

## Errors

Every failure is a `LoomError` with `status`, `code` (the server's `detail.code`),
`message`, `requestId`, `detail` and `op`, plus `category` (an `ErrorCategory`) and
`retryable` from the server's `detail`
([the error envelope](./errors.md#the-error-envelope-over-http)). For an error whose body
has neither, or one the SDK raised itself, the SDK's own class answers (`detail` stays what
the server sent):

- an op request whose connection the server never saw (code `NETWORK`) is `dependency`
  and retryable. The fetch must name a connect error: `ECONNREFUSED`, `ENOTFOUND`, `EAI_AGAIN`,
  `EHOSTUNREACH`, `ENETUNREACH`, `UND_ERR_CONNECT_TIMEOUT`, or `ETIMEDOUT` while
  connecting. The SDK reads the code on the error, its causes and each error of an
  `AggregateError`, which is where Node's fetch and node-fetch put it. Any other op request
  with no response has the same code and is resent as a timeout is (`retryTimeouts`): a
  socket dropped after the request went out, a browser's bare `TypeError`, and fetch
  errors of runtimes that name no such code (Bun, Deno, Workers);
- a call that may have run (no response other than a failed connection, a response that
  was cut off, or a `TimeoutError` for a call that got no answer in time) is `dependency`
  and retryable only on an idempotent op;
- a 2xx whose body is not JSON (`INVALID_RESPONSE`) is `internal`;
- a `baseUrl` fetch cannot send to (not an http(s) URL, or with credentials in it) is
  refused when the transport is built, with an `input` `LoomError`, not retryable;
- an error raised before sending is `dependency` and retryable when a service the SDK
  needed first was down (an identity provider, a secret store), `auth` when the caller is
  not signed in, else `input`. A sign-in the server or identity provider could not
  answer is retryable, `capacity` on a 429 and `dependency` otherwise (away, or a refused
  or cut-off connection), and the transport asks for the token again under
  `retryConnectionErrors`;
- a `TimeoutError` from waiting on an ingest (`waitForIngest`) is `capacity` and retryable,
  since waiting again is safe; one for a token that did not arrive is `dependency` and
  retryable, since the call was never sent;
- a call the caller aborted is `input` and not retryable: the caller's choice, after which
  the call may have run;
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

The subclass follows the HTTP status (`errorFromResponse`):

| Class                  | When                                                                                                                                                                                                                 |
| ---------------------- | -------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `ValidationError`      | 400, 422                                                                                                                                                                                                             |
| `AuthError`            | 401, 403                                                                                                                                                                                                             |
| `NotFound`             | 404                                                                                                                                                                                                                  |
| `IdempotentConflict`   | 409 with code `IDEMPOTENT_CONFLICT`; a subclass of `PreconditionFailed`                                                                                                                                              |
| `PreconditionFailed`   | any other 409 (including `PINNED_CONFLICT`: check `code`) but `OVERRIDE_NOT_STORED` (a `LoomError`), 412                                                                                                             |
| `RateLimited`          | 429 past the pacing budget; `retryAfterS` is the server's hint                                                                                                                                                       |
| `IdempotentInProgress` | 503 with code `IDEMPOTENT_IN_PROGRESS` once the retries are spent: the first request with this `Idempotency-Key` still runs; a subclass of `BackendUnavailable` with `retryAfterS`                                   |
| `BackendUnavailable`   | 5xx after the retries, or no complete response: the message reads `<op>: cannot reach <baseUrl>: …` (or `no response from <baseUrl>: …`, or `response from <baseUrl> cut off: …`) and `code` is `NETWORK_ERROR_CODE` |
| `TimeoutError`         | the per-call timeout elapsed                                                                                                                                                                                         |
| `LoomError`            | any other status; a 2xx body that is not JSON has code `INVALID_RESPONSE_CODE`; a call whose `signal` aborted is a plain `LoomError`                                                                                 |

Because the class follows the status, match on `e.code` to tell codes that
share one (`PRECONDITION_FAILED` from `PINNED_CONFLICT`, `OVERLOADED` from
`SCHEMA_PENDING`, `MODEL_CALL_DEFERRED` from `BACKEND_UNAVAILABLE`). A model call the
inference gateway asked to wait is a `BackendUnavailable` with code
`MODEL_CALL_DEFERRED` and no `retryAfterS`: the wait is in `detail.retry_after_s` and the
`Retry-After` header. What each code means is in the [errors reference](./errors.md).

```ts
import { NotFound, RateLimited } from "@clickhouse/loom";

try {
  await loom.memory.get({ org, namespace, agent, memory_key: "k" });
} catch (e) {
  if (e instanceof RateLimited) schedule(e.retryAfterS);
  else if (!(e instanceof NotFound)) throw e;
}
```

## Retries, pacing and logging

The transport retries connection failures with exponential backoff (3
attempts, 250 ms base), and 5xx for registry-idempotent ops; any other op only
on a 408 or a 503 `AUTH_UNAVAILABLE`, which the server answers before running
it. An answer the server classes `configuration` is never resent, since only
an operator's fix ends it. A 429 is retried after `Retry-After` (seconds or an
HTTP date, honoured up to 60 s) for up to `ratelimitMaxWaitS` per call, and
the `X-RateLimit-*` headers are read so a client at the floor waits for the
refill instead of taking a 429. A per-call timeout is retried only for
idempotent ops that are not read-only (`retryTimeouts: "idempotent"`), since
the server may still be processing it and deduplicates only their
`Idempotency-Key`.

| `retry` field                                | Default        | Meaning                                                                                                                        |
| -------------------------------------------- | -------------- | ------------------------------------------------------------------------------------------------------------------------------ |
| `maxAttempts`                                | 3              | total attempts including the first                                                                                             |
| `backoffMs`, `backoffMaxMs`, `backoffJitter` | 250, 5000, 0.5 | exponential backoff between attempts                                                                                           |
| `statuses`                                   | 408 + 5xx      | statuses retried (a 429 is paced, not retried)                                                                                 |
| `retryConnectionErrors`                      | true           | the connection failed, so the server never saw the request                                                                     |
| `retryTimeouts`                              | `"idempotent"` | retry a timeout, a cut-off response or a socket dropped after sending: never, only for idempotent non-read-only ops, or always |
| `respectRetryAfter`, `maxRetryAfterS`        | true, 60       | honour a `Retry-After` on a retried status (a 503 `OVERLOADED` sends one)                                                      |
| `totalBackoffBudgetMs`                       | ∞              | total time one call may sleep between attempts                                                                                 |

Pass a `logger` (debug, info, warn, error; `console` by default) and a
`logLevel` (`debug`, `info`, `warn`, `error`, `off`; default from
`LOOM_LOG_LEVEL`, else `warn`). One line per completed
call is logged at info, retries at warn, the final failure at error, and the
request with redacted headers at debug. Every request carries `X-Request-Id` (a
`requestId` that is not 1 to 64 letters, digits, `.`, `_` or `-` is replaced by a
generated one before sending, with a warning),
`X-Loom-SDK` (`loom-ts/<version>`), `X-Loom-Runtime` and
`X-Loom-Retry-Count`, plus `User-Agent` outside a browser (browsers forbid
setting it) and an `Idempotency-Key` for a registry-idempotent op.

## Versioning

The SDK's version is the product release version; the server does not gate on
it. Guard a newer op or field with `probe` or `capabilities()` rather than on a
version number. See [Client and server versions](./http-api.md#client-and-server-versions).
Each release's changes are in its [GitHub Release](https://github.com/ClickHouse/loom/releases) notes.

## Examples and the package README

[`sdk/typescript/README.md`](../../sdk/typescript/README.md) is the package's
own guide (every `ts` block in it type-checks against `src/` in
`npm run check`). Runnable examples are in
[`sdk/typescript/examples/`](../../sdk/typescript/examples/): `agent-memory`
(probe, remember, recall, ask with citations, outcome, typed errors) and
`session-resume` (a session resumed in a new process, a parked tool result,
and Loom's tools; with `OPENAI_API_KEY` set a model calls them, without it
the script calls them itself).

## Related

- [Python SDK](./python-sdk.md).
- [HTTP API](./http-api.md) and [Operations reference](./operations.md).
- [Agent integration](../guides/agent-integration.md).
- [Code generation](../development/code-generation.md): the SDK generators.
