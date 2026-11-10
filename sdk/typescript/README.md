# @clickhouse/loom — TypeScript SDK

Typed client for [Loom](https://github.com/ClickHouse/loom), the shared-production agent memory, knowledge, conflict and observability plane on ClickHouse. Every wire operation the server declares is a typed method here; the types are generated from the server's operation registry, so what the deployment serves is what the SDK types.

```bash
npm install @clickhouse/loom
```

The SDK's version is the server release's. A release not yet on npm installs from its
assets ([installing from a GitHub Release](https://github.com/ClickHouse/loom/blob/main/docs/development/release-process.md#installing-from-a-github-release)). For a clone at `<clone>`, build it with
`npm --prefix <clone>/sdk/typescript ci && npm --prefix <clone>/sdk/typescript run build`,
then run `npm install <clone>/sdk/typescript` in your project.

Node ≥ 20 (uses the platform `fetch`). Zero runtime dependencies.

## Quick start — an agent that remembers

Against a single-user install on this machine (`http://localhost:7777`, `dev/local`, no
token); for a team server, pass `baseUrl` and your org and namespace
([The credential](#the-credential)).

```ts
import { Datapack } from "@clickhouse/loom";

const pack = new Datapack("dev", "local", {
  consumer: "support-bot", // the label every ledger row carries
});
const bot = pack.agent("assistant");

// write: dedup, conflict detection and bitemporal validity are the server's job
await bot.remember("seat_pref", "Prefers an aisle seat on long flights.", {
  userId: "u-42",
  tags: ["travel"],
});

// read: semantic search over every agent's memories in the namespace
const hits = await bot.recall("which seat should I book?", { userId: "u-42", top_k: 5 });

// answer: grounded synthesis with citations, or an abstention when the memories do not say
const answer = await bot.ask("Which seat does the user prefer?", { userId: "u-42" });
if (!answer.abstain) console.log(answer.answer, answer.memory_keys);

// close the loop: tell loom which memory the agent actually used
for (const key of answer.memory_keys ?? []) await bot.outcome(key, "used", { userId: "u-42" });
```

`Agent` holds the identity tuple loom addresses memories by — `(org, namespace, agent)` from the constructor, `user_id` / `session_id` / `run_id` per call (the wire's sentinel `"-"` when omitted) — and stamps it on every request. `Datapack` holds `(org, namespace)` plus the platform defaults so `.agent(name)` spawns agents without repeating them. The server and credential the options leave out come from the environment and your `loom login` session ([The credential](#the-credential)).

## Every operation, typed

```ts
import { LoomClient } from "@clickhouse/loom";

const loom = new LoomClient({ baseUrl, apiKey });

// the dotted name, as it appears in the server's ledger
const out = await loom.call("memory.search", { org, namespace, query: "deploy window", top_k: 10 });
//    ^ MemorySearchOutput

// or the group accessor — the same call, camelCase
const same = await loom.memory.search({ org, namespace, query: "deploy window" });
await loom.knowledge.bulkAdd({
  org,
  namespace,
  entries: [{ source_uri: "kb://runbook.md", content: "…" }],
});
await loom.code.search({ query: "where is the parser?", path_prefix: "src/Parsers/" });
```

A file goes to the server's extractor as its bytes, and the job is followed until it ends:

```ts
const status = await loom.knowledge.ingestFile({
  org,
  namespace,
  data: new TextEncoder().encode("# Runbook\n\nRestart the worker after a config change."),
  filename: "runbook.md",
  source_uri: "repo://runbook.md",
});
// status.state: ready | partial | failed | cancelled; status.revision, status.chunks
```

`loom.knowledge.upload`, `loom.knowledge.ingest` and `loom.knowledge.waitForIngest` are its steps on their own.

`OpMap` binds every operation name (`GENERATED_OP_COUNT` of them) to its input and output interface; `OPS` is the table of specs (group, method, required role, idempotency, maturity, latency budget). An op the SDK does not know yet (a newer server) is reachable through `loom.callRaw(name, body)`.

## Errors

Every failure is a `LoomError` with `status`, `code` (the server's `detail.code`), `message`, `requestId` (echoed by the server, findable in its ledger), the raw `detail`, and the server's `category` and `retryable`, the SDK's own when the body names none ([the error envelope](https://github.com/ClickHouse/loom/blob/main/docs/reference/errors.md#the-error-envelope-over-http)). Subclasses mirror the Python SDK:

| class                                      | when                                                                                                                                                                                                                                                 |
| ------------------------------------------ | ---------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `ValidationError`                          | 400 / 422                                                                                                                                                                                                                                            |
| `AuthError`                                | 401 / 403 — no or insufficient authorization for the op or namespace                                                                                                                                                                                 |
| `NotFound`                                 | 404                                                                                                                                                                                                                                                  |
| `PreconditionFailed`, `IdempotentConflict` | 409 / 412 — a version, lifecycle or idempotency precondition                                                                                                                                                                                         |
| `RateLimited`                              | 429 past the pacing budget; `retryAfterS` is the server's hint                                                                                                                                                                                       |
| `BackendUnavailable`                       | 5xx after the retries, or a connection that never got a response — then the message reads `cannot reach <baseUrl>: …` (or `no response from <baseUrl>: …`, or `response from <baseUrl> cut off: …`) and `code` is `NETWORK_ERROR_CODE` (`"NETWORK"`) |
| `TimeoutError`                             | the per-call timeout elapsed; retried only for idempotent non-read-only ops (`retryTimeouts`), since the server may still be processing                                                                                                              |

A 2xx whose body is not JSON (a proxy or gateway page) is a `LoomError` with `code` `INVALID_RESPONSE_CODE` (`"INVALID_RESPONSE"`); it is not retried, and `detail.message` holds the start of the body.

```ts
import { NotFound, RateLimited } from "@clickhouse/loom";
try {
  await loom.memory.get({ org, namespace, agent, memory_key: "k" });
} catch (e) {
  if (e instanceof RateLimited) schedule(e.retryAfterS);
  else if (e instanceof NotFound) {
    /* … */
  } else throw e;
}
```

## Retries, pacing, idempotency

The transport does what the Python SDK's does: a failed connection is retried with exponential backoff (3 attempts, 250 ms base; the fetch named a connect-phase error, so the server did not process the request), and so are 5xx for registry-idempotent ops — any other op only on a 408 or a 503 `AUTH_UNAVAILABLE`, which the server answers before running it — except an answer the server classes `configuration`; a 429 is retried after the server's `Retry-After` (seconds or an HTTP-date, honoured up to 60 s) for up to `ratelimitMaxWaitS` (15 s) per call; `X-RateLimit-*` headers are read so a client at the floor waits for the refill before the next request instead of taking a 429. Idempotent operations (the registry says which) get an `Idempotency-Key` automatically, so a retry after a dropped response cannot double-write; pass your own key to make two deliberate calls one. Every request carries `X-Request-Id` (the server echoes it; a `requestId` that is not 1 to 64 letters, digits, `.`, `_` or `-` is replaced by a generated one before sending, with a warning; every error has `requestId`), `User-Agent` / `X-Loom-SDK` (`loom-ts/<version>`), `X-Loom-Runtime` and `X-Loom-Retry-Count`.

A response cut off mid-body, a socket dropped after the request went out, and an op request whose fetch failure names no connect error (a browser's bare `TypeError`, and Bun, Deno or Workers) are resent as a timeout is (`retryTimeouts`), since the server may have run the call; a token the sign-in could not fetch is asked for again either way.

```ts
const loom = new LoomClient({
  baseUrl,
  apiKey,
  timeoutMs: 30_000,
  retry: { maxAttempts: 4 },
  ratelimitMaxWaitS: 30,
});
await loom.memory.set(input, { idempotencyKey: "order-1234-confirmed", timeoutMs: 5_000 });
```

The policy is one object, set on the client and overridable per call; every field is optional and validated by name:

```ts
const loom = new LoomClient({
  baseUrl,
  apiKey,
  retry: {
    maxAttempts: 5,
    backoffMs: 500,
    backoffMaxMs: 10_000,
    statuses: [408, 500, 502, 503, 504],
    totalBackoffBudgetMs: 30_000,
  },
});
await loom.memory.get(input, { retry: { maxAttempts: 1 } }); // this call: no retries
await loom.memory.setFromMessages(input, { retry: { retryTimeouts: "always" } });
```

| field                                        | default        | meaning                                                                                                                                                                    |
| -------------------------------------------- | -------------- | -------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `maxAttempts`                                | 3              | total attempts including the first                                                                                                                                         |
| `backoffMs`, `backoffMaxMs`, `backoffJitter` | 250, 5000, 0.5 | exponential backoff between attempts, ±25 % jitter                                                                                                                         |
| `statuses`                                   | 408 + 5xx      | statuses retried (a 429 is paced, not retried)                                                                                                                             |
| `retryConnectionErrors`                      | true           | the connection failed, so the server never saw the request                                                                                                                 |
| `retryTimeouts`                              | `"idempotent"` | retry a timeout, a cut-off response or a socket dropped after sending: never, only for idempotent non-read-only ops (the server dedups their `Idempotency-Key`), or always |
| `respectRetryAfter`, `maxRetryAfterS`        | true, 60       | honour a `Retry-After` on a retried status (a 503 OVERLOADED sends one) up to the cap                                                                                      |
| `totalBackoffBudgetMs`                       | ∞              | total time one call may sleep between attempts                                                                                                                             |

## Logging

Pass a `logger` (four methods: debug/info/warn/error) and a `logLevel` (`debug | info | warn | error | off`; default `LOOM_LOG_LEVEL`, which also takes Python's names such as `warning` and `critical` and warns on a value it cannot read, or `warn`). The SDK writes one line per completed call at info (`memory.get 200 12 ms attempts=2 rid=…`), retries and rate-limit waits at warn, the final failure at error, and the request line with headers at debug. Credential headers are redacted to their scheme and last four characters before they reach the sink: the key never appears at any level.

```ts
const loom = new LoomClient({ baseUrl, apiKey, logger: myPinoAdapter, logLevel: "info" });
```

## Give your agent the guide

`await bot.guide()` returns how to use Loom, written for a model: the loop,
where each kind of state goes and how to read what comes back
(the experimental `mentor.guide`, the `loom` skill in `skills/loom/`). Put it in the system
prompt of an agent that has no plugin to carry it; `guide("reference")` returns
every plane with its ops.

```ts
const systemPrompt = `You triage support tickets.\n\n${await bot.guide()}`;
```

## Ask the mentor

The store reads what your agent actually calls and names the planes it never
uses — the code index, the tracker source, documents, records, offload, the
entity graph — with the counts behind each hint. Every successful response may
carry one `X-Loom-Hint`, which the transport logs at INFO once per distinct text
and returns as `hint` / `hintCode` on `callWithResponse`; a bot that never renders a
context pack asks once at startup:

```ts
const brief = await bot.mentor(); // logs up to three hints; returns the brief
for (const h of brief.hints ?? []) console.log(h.code, h.severity, h.text, h.evidence);
```

`LOOM_MENTOR_BRIEF=on` runs it in the background on the first call instead.
Name your agent: `""`, `"-"` and `"*"` default to `sdk-ts:<script>` with a
warning, because an anonymous agent is every other anonymous caller to the
ledger, the mentor and the cost views.

## Ask the assistant

The Loom assistant answers a question about a namespace as your token: it reads
only what you may read, and each answer spends from the namespace's daily model
cost cap. `loom.assistant.stream` yields the text as it is written, then the answer
with the citations the server checked against Loom; `loom.assistant.ask`, the op,
returns one whole:

```ts
for await (const event of loom.assistant.stream({
  namespace: "support",
  question: "When are deploys frozen?",
})) {
  if (event.type === "text") process.stdout.write(event.text);
  else if (!event.answer.citations.some((c) => c.verified)) console.log("\n(no verified citation)");
}
```

An answer that began is never resent, nor one refused with a 429: asking again is a new
answer with its own spend. Abort it with `signal`; the server stops the answer too.

## The response, not just the body

`callWithResponse` returns the body with what the server said about the call: the `requestId` that finds it in the server's ledger, the `schemaVersion` it answered with, its own `serverTimingMs`, the `rateLimit` reading and how many `attempts` the call took.

```ts
const r = await loom.callWithResponse("memory.search", {
  org,
  namespace,
  agent,
  query: "deploy window",
});
console.log(r.data.results.length, r.requestId, r.serverTimingMs, r.rateLimit?.remaining);
```

## The credential

A client picks its server and credential as the Python SDK does. `profile` names a `loom login` session and uses it on the server it signed in to; it cannot be combined with `apiKey` or another `baseUrl`. Otherwise the server is `baseUrl`, else `LOOM_BASE_URL`, else `http://localhost:7777`, and the credential is the first of:

1. `tokenProvider` or `apiKey`
2. `LOOM_API_KEY`
3. a bot's `LOOM_CLIENT_ID` ([Bots and CI jobs](#bots-and-ci-jobs))
4. the session stored in `~/.loom/credentials` for that server, under the profile `LOOM_PROFILE` (default `default`)
5. the first line of `~/.loom/token`, which `loom mint-token --save` writes

Steps 4 and 5 run in Node only, on the client's first request; a browser skips them. With no credential found, requests go out without an `Authorization` header.

### Signing in

```ts
import { LoomClient, login, logout, whoami } from "@clickhouse/loom";

await login("https://loom.example.com"); // prints a URL and a code; approve on any browser
const loom = new LoomClient({ baseUrl: "https://loom.example.com" }); // uses the stored session
console.log(await whoami());
await logout();
```

`login(server?, { profile, openBrowser, echo })` runs the OAuth device authorization grant (RFC 8628): it prints a URL and a short code, opens the page when stdout is a terminal, and resolves once you approve with your password or single sign-on. The access and refresh tokens go to `~/.loom/credentials` (mode 0600), the file `loom login` and the Python SDK's `loom.login()` use, so a session made by any of them serves all three. A client refreshes the hour-long access token a minute before it expires and once more on a 401, and sends a session only to the server it signed in to. `profile: "staging"` (or `LOOM_PROFILE=staging`) keeps a second server's session beside the first, and `new LoomClient({ profile: "staging" })` uses it (Node 20.16 or later, or 22.3 or later on Node 22: the constructor reads the profile's server synchronously). `logout(profile?)` revokes the session on the server and forgets it here; `accessToken(profile?)` returns a live token for requests of your own.

A refresh holds the lock directory `~/.loom/.credentials.lock.d`, which the Python SDK takes too, and re-reads the file first, so two processes never spend the same refresh token. A process takes the lock by renaming a private directory that holds an owner file (its pid and host) onto that path, so a lock always names its holder, and touches it every 10 s by its own clock while it holds it. A waiter breaks a lock untouched for 60 s, or one untouched for 20 s whose holder on this machine is no longer running; a Python waiter on Windows does not check the pid and waits the 60 s. Breaking a lock and releasing one both rename it away under the claim directory `~/.loom/.credentials.lock.d.breaking`, so a break never renames a lock taken after a release. Signing in is Node-only.

### The key

`apiKey` (or `LOOM_API_KEY`) is held in a private field: `JSON.stringify(loom)` and `util.inspect(loom)` show `apiKey: "…1234"`, never the token. A blank or whitespace value is no key, so an exported-but-empty variable does not turn into `Authorization: Bearer ` and a puzzling 401: the client goes on down the list above, and with nothing found the request carries no `Authorization` header.

### Bots and CI jobs

A bot registered with `loom bot create` needs no long-lived secret. With `LOOM_CLIENT_ID` set and no `apiKey` / `LOOM_API_KEY`, the client gets its own token from `/oauth/token` (an hour, less when the bot's maximum is shorter), keeps it in memory only, renews it before it runs out and retries a 401 once with a fresh one. It proves itself with one of:

- a key, set by exactly one of `LOOM_CLIENT_KEY_FILE` (a PEM file), `LOOM_CLIENT_KEY` (the PEM itself) or `LOOM_CLIENT_KEY_SECRET` (an AWS Secrets Manager id or ARN; install `@aws-sdk/client-secrets-manager`). The key is read again for each token, so rotation is picked up. P-256, Ed25519 and RSA keys sign as ES256, EdDSA and RS256.
- a workload token: `LOOM_WORKLOAD_TOKEN`, or the file `LOOM_WORKLOAD_TOKEN_FILE` (re-read for each token), or, in GitHub Actions (`permissions: id-token: write`), one asked for `LOOM_WORKLOAD_AUDIENCE`.

Bot sign-in is Node-only. `transport.apiKeyHint` reads `bot:<client id>`; pass `tokenProvider: new MachineLogin({...})`, or any `{ token(refused?) }`, to choose it in code.

### Your tokens

`loom.account` (and `bot.account` on an `Agent`) makes, lists and revokes your own tokens, as the dashboard's Settings → Tokens does. A new token is never wider than your own grants. `namespaces` is `"*"` by default: every namespace you belong to, now and later. A list without `"*"` reads exactly those, in read order, each a bare name or `{ namespace, role }`. `ttlDays` defaults to 90, `0` never expires. The token is in the answer once, with `saves_to` and each MCP client's setup in `clients`.

```ts
const loom = new LoomClient({ baseUrl, apiKey });
const mine = await loom.account.createToken(); // everything you belong to
const ci = await loom.account.createToken(["support", { namespace: "docs", role: "viewer" }], {
  name: "ci",
  role: "writer",
  ttlDays: 30,
});
console.log(ci.token, ci.saves_to, ci.expires_at);
for (const t of await loom.account.tokens()) console.log(t.name, t.namespaces, t.revoked_at);
await loom.account.revokeToken("ci");
```

A name a live token already holds is a `PreconditionFailed` with code `TOKEN_NAME_TAKEN`.

## Sessions and tools: what an agent framework holds

An agent framework wants a few things from its store: resume a session after a restart, append turns as they happen, park a large tool result behind a pointer, keep notes the next turn sees, scope everything to a tenant. Loom has an op for each; `Session` binds them to one (user, session) scope, and `AgentTools` hands the same verbs to the model.

| what the framework needs                    | loom, through the SDK                                                                                                                                       |
| ------------------------------------------- | ----------------------------------------------------------------------------------------------------------------------------------------------------------- |
| a new process resumes the session           | `bot.session(id, { userId }).resume({ query })` → `context.pack` as the system prompt + the transcript tail                                                 |
| turns persisted as they happen              | `session.append(...turns)` → `conversation.append` now, `memory.set_from_messages` every `flushEvery` turns (extraction, with `carryOver` turns of context) |
| a large tool result parked behind a pointer | `session.offload(text, { summary })` → `context.offload` (ZSTD, TTL, content-addressed); `session.fetch(blobKey, { pattern })` reads it back                |
| parked results that expire                  | `offload(..., { ttlSeconds })`; memory retention is the namespace's (`retention_days`)                                                                      |
| one store, many tenants                     | `new Datapack(org, namespace)`; `userId` / `sessionId` per call                                                                                             |
| semantic memory `search()`                  | `bot.recall()` / `bot.ask()`, or the model's `loom_recall` / `loom_ask` tools                                                                               |

```ts
const bot = new Datapack("acme", "support", { baseUrl, apiKey }).agent("assistant");
const session = bot.session("thread-42", { userId: "u1", flushEvery: 8 });

const { system, messages } = await session.resume({ query: "resume the billing migration" });
// … run the model with `system` and `messages` …
await session.append({ role: "user", content: userText }, { role: "assistant", content: reply });
const blob = await session.offload(buildLog, { summary: "pytest run 812" });
await session.note(blob.pointer); // the next turn's pack carries the pointer, not the log
await session.close(); // flush → extraction
```

If the namespace has conversation logging off (the default; an admin enables it with `conversation.set_logging`), the session warns once, sets `transcriptStored = false` and keeps extracting.

`bot.tools(scope)` gives a model six loom verbs — `loom_remember`, `loom_recall`, `loom_ask`, `loom_offload`, `loom_fetch`, `loom_note` — as tool definitions in the shape each provider expects, and one dispatcher:

```ts
const tools = bot.tools({ userId: "u1", sessionId: "thread-42" });
const completion = await openai.chat.completions.create({
  model,
  messages,
  tools: tools.definitions("openai"),
});
for (const call of completion.choices[0].message.tool_calls ?? []) {
  messages.push({
    role: "tool",
    tool_call_id: call.id,
    content: await tools.execute(call.function.name, call.function.arguments),
  });
}
// Anthropic: tools.definitions("anthropic"); anything that takes JSON schema: tools.definitions()
```

`execute()` returns the string the model reads (compact JSON, or a line it can act on: "no parked text under blob_key=…"), validates required arguments with messages a model can correct, and truncates past `maxResultChars`. [`examples/session-resume`](https://github.com/ClickHouse/loom/tree/main/sdk/typescript/examples/session-resume/) is the two-process run with a real `gpt-4o-mini` tool-use turn.

## Probe before you rely

```ts
import { probe } from "@clickhouse/loom";
const p = await probe(loom, {
  ops: ["memory.ask", "context.pack"],
  fields: { "memory.search": ["exclude_keys"] },
});
if (!p.ok) console.warn(p.missingOps, p.missingFields);
```

`ops.capabilities` lists what the deployment serves, with request fields, so a client learns about a missing op or field once instead of at a 404 or 422 mid-session. `p.unknownToSdk` names ops the server has that this SDK version does not.

`loom.capabilities()` pairs that list with what the bearer token itself says (its `roles` / `ns_roles`, decoded, not verified) so an integration does not poll ops its token cannot call. `canCall` is three-valued: `true` / `false` when both the op's required role and the token's role are known, `undefined` when the SDK cannot tell — then make the call and let the server decide.

```ts
const caps = await loom.capabilities();               // cached 60 s
if (caps.canCall("conflicts.list", namespace) !== false) await loom.conflicts.list({ org, namespace });
if (await bot.canCall("cost.budget_status")) …        // the Agent's own namespace
caps.callable(namespace);                             // every served op this token may call there
```

## Pagination

```ts
import { collect, paginate } from "@clickhouse/loom";
for await (const doc of paginate((cursor) =>
  loom.knowledge.list({ org, namespace, limit: 100, cursor }),
)) {
  /* … */
}
const recentAudit = await collect(
  paginate((cursor) => loom.audit.list({ org, namespace, cursor })),
  50,
);
```

## Strands Agents

```ts
import { LoomMemoryStore } from "@clickhouse/loom/integrations/strands";
const store = new LoomMemoryStore(bot, { tags: ["customer"], scope: { userId: "u-42" } });
// new MemoryManager({ stores: [store] }) from @strands-agents/sdk
await store.add("Prefers an aisle seat", { source: "chat" });
const entries = await store.search("which seat?", { maxSearchResults: 3 });
```

No dependency on the Strands package: the `MemoryStore` shape (`name`, `writable`, `add(content, metadata)`, `search(query, { maxSearchResults })`) is structural, and the store behaves like the Python adapter. A write is one `memory.set` keyed `strands/<sha256 prefix>` of the content, so re-adding a fact is idempotent and both SDKs write the same key. The metadata's short `kind`, `source`, `topic` and `category` strings become `key:value` tags. A search is limited to the store's tags and `memoryType` (default `semantic`) and returns nothing when loom abstains (`honourAbstain: false` keeps the hits); each entry carries `memory_key`, `memory_type`, `similarity` and, when known, `source` and `valid_at`.

## How the types stay true

`src/generated/ops.ts` is produced by `python scripts/codegen/sdk_ts_gen.py` from `server.operations.registry.ALL_OPERATIONS` (one TypeScript interface per Pydantic input/output model, shared definitions hoisted once). CI runs it with `--check`, so a server change that alters a request or response shape cannot merge without regenerating the SDK's types. Dates ride the wire as ISO-8601 strings; unconstrained `Any` fields are `unknown`.

## Examples

[`examples/`](https://github.com/ClickHouse/loom/tree/main/sdk/typescript/examples/) holds runnable, self-contained scripts whose READMEs show the actual run: start with [agent-memory](https://github.com/ClickHouse/loom/tree/main/sdk/typescript/examples/agent-memory/) (probe → remember → recall → ask with citations → outcome; typed errors), then [session-resume](https://github.com/ClickHouse/loom/tree/main/sdk/typescript/examples/session-resume/) (a session resumed in a new process, a parked tool result, a real model calling loom's tools).

## Development

```bash
npm install
npm run check                 # biome lint + tsc --noEmit + version check + README examples + vitest
npm run check:dist            # tsdown build (ESM + CJS) + dist tests + publint + arethetypeswrong
npm run docs                  # typedoc → docs/api
LOOM_BASE_URL=… LOOM_API_KEY=… LOOM_ORG=dev npm run test:integ   # against a live loom
python ../../scripts/codegen/sdk_ts_gen.py     # regenerate the types after a server change
```

The package ships ESM (`dist/index.mjs`) and CommonJS (`dist/index.cjs`) with their own declaration files, has no side effects, and imports no Node built-in (it runs on `fetch`). `test/package/exports.test.ts` lists every runtime export; adding one is a deliberate API change. `src/types.test-d.ts` holds the type-level contract (what `call`, the group accessors and `Body<K>` infer and reject), checked by `vitest --typecheck` inside `npm test`. `prepublishOnly` runs both check scripts.
