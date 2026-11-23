# clickhouse-loom

Use [ClickHouse Loom](https://clickhouse.github.io/loom/) from Rust. Loom is a
self-hosted server for shared long-term AI agent memory, document search and
code indexing.

This crate connects to a separately deployed Loom server backed by ClickHouse.
The server image is `ghcr.io/clickhouse/loom`. Use a local single-user installation
or connect to a team deployment managed by your operator.

`Client` provides typed calls for every operation in the server registry.
Calls are async and use the application's Tokio runtime. The optional `blocking`
feature provides the same operations through a synchronous client.

## Install

From your Rust application's directory, add the SDK from
[crates.io](https://crates.io/crates/clickhouse-loom):

```bash
cargo add clickhouse-loom
```

The equivalent `Cargo.toml` dependency is:

```toml
[dependencies]
clickhouse-loom = "0.1"
```

Cargo resolves the SDK's dependencies automatically.

Use the current stable Rust and Cargo toolchain for installation from crates.io.
The SDK source supports Rust 1.84+, but resolved dependencies may require a newer
toolchain.

`cargo install` installs executables. This SDK is a library added to your
application's dependencies. The crate name is `clickhouse-loom`; Rust imports it
as `clickhouse_loom`.

Runtime deps: reqwest with Rustls, serde, serde_json, Tokio, rand, uuid, httpdate,
log and derive_more.

## Use

The example uses `#[tokio::main]`. If your application does not already enable
Tokio's `macros` and `rt-multi-thread` features, add this dependency to its
`Cargo.toml`:

```toml
[dependencies]
tokio = { version = "1", features = ["macros", "rt-multi-thread"] }
```

For a local [single-user installation](https://github.com/ClickHouse/loom/blob/main/docs/getting-started/quickstart.md),
connect to `http://localhost:7777` with org `dev`, namespace `local` and no token:

```rust
use clickhouse_loom::{Client, SearchRequest, SetRequest};

#[tokio::main]
async fn main() -> Result<(), Box<dyn std::error::Error>> {
    let bot = Client::builder("dev", "local", "my-agent")
        .base_url("http://localhost:7777")
        .api_key("")
        .build()?;
    bot.set(SetRequest::new("deploy_window", "Deploy Tue/Thu, never Friday")
        .memory_type("semantic")
        .tags(["deployment"])).await?;
    let memory = bot.get("deploy_window").await?;
    let hits = bot.search(SearchRequest::new("when do we deploy").top_k(5)).await?;
    if memory.found {
        println!("{}", memory.value);
    } else {
        println!("not found");
    }
    println!("{:?}", hits.results.iter().map(|hit| &hit.memory_key).collect::<Vec<_>>());
    bot.delete("deploy_window").await?;
    Ok(())
}
```

For a team deployment, use your account's org and namespace with a writer token.
Set the server address through `.base_url(...)` or `LOOM_BASE_URL`, and the token
through `.api_key(...)` or `LOOM_API_KEY`. Obtain a token from **Settings → Tokens**
or ask your operator to mint one. CI and service processes can supply the address
and token through the environment.

Server setup is covered in the
[deployment guide](https://github.com/ClickHouse/loom/blob/main/docs/operations/deployment.md).

## Configuration

`Client::builder(org, namespace, agent)` accepts `base_url`, `api_key`, `timeout`,
`user_id`, `session_id`, `run_id`, `retry`, `pacing` and `ratelimit_max_wait`.
The default timeout is 60 seconds per attempt. It covers the whole HTTP request
and response body. The server address comes from `LOOM_BASE_URL`, defaulting to
`http://localhost:7777`. The token comes from `LOOM_API_KEY`; without a token,
requests carry no Authorization header.
Explicit settings override the environment. `.api_key("")` disables authorization.
Empty environment values are treated as unset.

Builder identity values apply to declared caller and authorship fields.
`knowledge_add` and `knowledge_upsert` receive agent and session identity;
`observe_record` receives user, session and run identity. Recall filters keep
their explicit values. A bound run labels reads through `X-Loom-Run-Id`;
`run_get` and `run_complete` keep the request's target run ID.
Search defaults to `agent="*"` and omits user, session and namespace-scope filters.
The server chooses its default namespace scope.
Use `.agent_id(...)`, `.namespace_scope(...)`, `.user_id(...)` or `.session_id(...)` to
filter a search explicitly. Org and the addressed namespace remain bound to the client.

Set requests support JSON values, memory type, tags, TTL, confidence and
`if_match_sha256`. Accepted JSON values are objects, arrays, strings, numbers
and booleans. Top-level `null` is not accepted. Options are sent only when `Some`;
zero, false and empty collections remain values. Unset options use server defaults.
Search requests support top-k, tags, memory type and search mode.

## Request and response types

Public request, response and API error structs are `#[non_exhaustive]`.
Create requests with `new`, then chain the options you need:

```rust
let request = SearchRequest::new("when do we deploy")
    .top_k(10)
    .tags(["deployment"])
    .search_mode("hybrid");
let hits = bot.search(request).await?;
```

Each option method returns the request, ready to pass to `Client`. Repeating a
method replaces its value. String methods accept borrowed or owned strings;
`tags` accepts an iterator of either. Public fields remain writable, including
setting an option to `None` to omit it.

Read response fields directly, or include `..` when destructuring. `Error` is
also non-exhaustive: include a fallback arm when matching it. Match
`Error::Protocol { status, source, .. }`. Error codes and categories are strings.

Response decoding ignores unknown JSON fields. Optional fields use `Option<T>`
or documented defaults. Missing required response fields produce
`Error::Protocol`.

## Generated contracts

Every registered operation has generated request and response types,
async methods and blocking methods. Method names replace dots with
underscores: `memory.pin` becomes `memory_pin`, and `task.create` becomes
`task_create`. Every method accepts its typed request and also provides a
`*_with_options` variant.

The memory facade preserves its public request and response types, and offers
key-only reads and deletes. It shares the transport with the generated methods:

| Method             | Operation          | Request                |
| ------------------ | ------------------ | ---------------------- |
| `set`              | `memory.set`       | `SetRequest`           |
| `get`              | `memory.get`       | key string             |
| `search`           | `memory.search`    | `SearchRequest`        |
| `delete`           | `memory.delete`    | key string             |
| `context_pack`     | `context.pack`     | `ContextPackInput`     |
| `knowledge_add`    | `knowledge.add`    | `KnowledgeAddInput`    |
| `knowledge_get`    | `knowledge.get`    | `KnowledgeGetInput`    |
| `knowledge_search` | `knowledge.search` | `KnowledgeSearchInput` |
| `health`           | `ops.health`       | `OpsHealthInput`       |
| `capabilities`     | `ops.capabilities` | `OpsCapabilitiesInput` |

`memory_set(MemorySetInput)`, `memory_get(MemoryGetInput)`,
`memory_search(MemorySearchInput)` and `memory_delete(MemoryDeleteInput)` expose
the full wire contracts. They return the matching `Memory*Output` types.
The facade's `set`, `get`, `search` and `delete` retain `SetResponse`,
`GetResponse`, `SearchResponse` and `DeleteResponse`.
Every generated request has `send(&client, CallOptions)` and uses the same transport.
Every method also has a `*_with_options` variant. Context and knowledge constructors
omit bound identity arguments; for example, `ContextPackInput::new().query("task")`
and `KnowledgeAddInput::new("urn:guide", "Guide text")`.

```rust
use clickhouse_loom::{CallOptions, MemoryGetInput};

# async fn example(client: clickhouse_loom::Client) -> Result<(), clickhouse_loom::CallFailure> {
let memory = MemoryGetInput::new("preference")
    .fields(["value", "tags"])
    .follow(true)
    .send(&client, CallOptions::default())
    .await?;
println!("{}", memory.data.found);
# Ok(())
# }
```

Nullable generated request fields use `Option<Option<T>>`. `None` omits the field,
`Some(None)` sends null and `Some(Some(value))` sends a value. Value setters keep
their plain argument types; `<field>_null()` sends null explicitly.
For example, `.ttl_seconds(60)` sends a number and `.ttl_seconds_null()` sends null.
Nonnullable options reject null during decoding.

Generated responses follow server defaults: `MemoryGetOutput.pinned` is `bool`,
and `MemorySearchHit.version` and `namespace` default to zero and an empty string.
`MemorySearchOutput.embedding_model` is required; `retrieval_risk` defaults to
`"abstain"` and `term_leg` to `"off"`. Unbounded integer fields use `Integer`.

The memory facade keeps its own decoding contract: `GetResponse.pinned`,
`SearchHit.version` and `SearchHit.namespace` are optional. `SearchResponse`
does not require `embedding_model`, and missing `retrieval_risk` and `term_leg`
decode as empty strings. `SetRequest.ttl_seconds` remains `Option<i64>`;
use `MemorySetInput` to send an explicit null or other full wire fields.

Maintainers regenerate from the repository root with Python dependencies and
`rustfmt` installed:

```bash
python scripts/codegen/sdk_rust_gen.py
python scripts/codegen/sdk_rust_gen.py --check
python scripts/codegen/sdk_rust_gen.py --fixtures
python scripts/codegen/sdk_rust_gen.py --fixtures --check
```

The published crate contains the generated Rust source and needs no Python runtime.

## Results and errors

`get` returns `GetResponse` with `found=false` for an absent memory.
`delete` returns `DeleteResponse` with `tombstoned=false` when nothing was deleted.
Deletion writes a tombstone; it is not a physical erasure.
`set` can merge with an existing memory: check `noop` and `merged_into_key`
before assuming the requested key holds a row. Versions use `u64`.

Search hits preserve source, chunk parent keys, attribution, provenance, ranking
and usage fields. Use a chunk's `parent_memory_key` to fetch or credit its parent.
Check `SearchResponse.abstain`, `abstain_code`, `retrieval_risk`, `degraded` and
`pending_extraction` before treating the hits as an answer. Pending extraction
means an empty response may precede a completed save. `reinforcement` is `None`
when recall credit was not disclosed. An omitted risk label is empty in
`SearchResponse` and defaults to `"abstain"` in the generated `MemorySearchOutput`.

`SearchHit.agent` names the row's writer. For governed definitions, inspect
`SearchHit.governed` and `SearchResponse.term_leg`; `retrieval_risk` describes the
other hits.

```rust
use clickhouse_loom::Error;

match bot.get("deploy_window").await {
    Err(Error::Api(error)) => {
        eprintln!("{} {} {:?} {:?}", error.status, error.code, error.category, error.retryable);
    }
    Err(error) => eprintln!("{error}"),
    Ok(memory) => println!("found={}", memory.found),
}
```

API errors preserve the server's code, message, optional category and retryable
flag, retry hints, request ID and raw detail. Unknown error codes and categories
remain strings. An error response without a JSON body still carries the HTTP
status. `Error::Transport` reports HTTP transport failures; `Error::Protocol`
reports an invalid successful response.

## Per-call options and metadata

The `*_with_options` methods take `CallOptions` and return
`Result<CallResult<T>, CallFailure>`. Methods without options return the typed
response and `Error`.

```rust
use clickhouse_loom::{CallOptions, RetryPolicy};
use std::time::Duration;

let options = CallOptions::default()
    .timeout(Duration::from_secs(5))
    .retry(RetryPolicy::disabled())
    .request_id("recall-1");
match bot.get_with_options("deploy_window", options).await {
    Ok(result) => println!("found={} rid={} attempts={}",
        result.data.found, result.metadata.request_id, result.metadata.attempts),
    Err(failure) => {
        if let Some(meta) = &failure.metadata {
            eprintln!("rid={} status={:?} attempts={}",
                meta.request_id, meta.status, meta.attempts);
        }
        eprintln!("{}", failure.error);
    }
}
```

Options override one call and leave the client's defaults intact. Timeout covers
each HTTP attempt, including its response body; it can exceed the client timeout.
Retries and 429 pacing reuse the call's request ID and write key. Disabling retries
leaves pacing enabled; configure pacing on the client.

Use `.idempotency_key("logical-write")` on a declared idempotent write to resubmit
the same operation and payload with the same key. Set, delete, knowledge add and
health generate a fresh key per logical call when none is supplied. Read-only,
nonidempotent and unknown operations reject explicit write keys.
Request IDs must contain 1–64 letters, digits, dots,
underscores or hyphens; write keys must contain visible ASCII characters.
Invalid options fail before dispatch.

`CallMetadata` contains the sent and echoed request IDs, optional HTTP status,
schema version, app duration from Server-Timing, rate-limit headers, hint headers
and the total number of HTTP attempts, including pacing rounds. It describes the
final attempt. Missing or invalid headers stay absent. Without an echoed ID,
`request_id` is the sent ID.

`CallFailure.error` preserves the original `Error` classification and source.
Its metadata is absent for validation and encoding failures. A network failure
before response headers has `status=None`; an interrupted response body retains
the received headers. The final attempt never inherits an earlier response's
metadata.

## Retries and logging

Operation policies come from the full server registry. Retries after dispatch
require declared idempotence, including HTTP status retries, incomplete responses
and 429 pacing. `RetryTimeouts::Always` does not enable nonidempotent operations.

Calls do not follow redirects. Retry and pacing defaults are:

- Up to 3 attempts on connection failures, HTTP 408 and HTTP 500–599.
  Configuration errors and the SDK's excluded error codes stop retries;
  `retryable=false` alone does not stop a status retry.
- Exponential backoff starts at 100 ms, doubles to a 5 second ceiling, and uses
  jitter in the range 0.75–1.25 times the delay. A valid `Retry-After` of up to
  60 seconds replaces backoff. Headers take precedence over `detail.retry_after_s`;
  numeric seconds and HTTP dates are accepted.
- Declared idempotent writes generate one UUID idempotency key per call and reuse it
  across retries and pacing. Timeouts and incomplete responses retry these
  writes by default. `get` and `search` do not retry those failures by default.
- Pacing is enabled separately from retries. A 429 waits at most 5 seconds per
  round, with a 15 second total sleep budget per call. Missing, invalid or
  over-cap hints use 1 second; a zero hint returns the error immediately.
  Each pacing round starts a fresh retry budget. Recorded `X-RateLimit-*`
  headers can also delay a subsequent call to the same operation.

Configure retries through `RetryPolicy::default()` and its public fields:

```rust
use clickhouse_loom::{Client, RetryPolicy, RetryTimeouts};
use std::time::Duration;

let mut retry = RetryPolicy::default();
retry.max_attempts = 4;
retry.total_backoff_budget = Some(Duration::from_secs(3));
retry.retry_timeouts = RetryTimeouts::Never;
let bot = Client::builder("dev", "local", "my-agent")
    .retry(retry)
    .ratelimit_max_wait(Duration::from_secs(5))
    .build()?;

// An override clone shares connections and rate state, keeping the original policy.
let once = bot.with_retry(RetryPolicy::disabled())?;
let memory = once.get("deploy_window").await?;
```

`RetryPolicy` also exposes `backoff`, `backoff_max`, `backoff_jitter`, `statuses`,
`retry_connection_errors`, `respect_retry_after` and `max_retry_after`.
`RetryTimeouts::Always` explicitly allows read retries after incomplete responses.
Policies are validated on `build` and `with_retry`. `None` for
`total_backoff_budget` means no total backoff limit.
To disable both automatic retries and 429 waiting, use
`.retry(RetryPolicy::disabled()).pacing(false)`.

The SDK logs through the `log` facade under the `clickhouse_loom` target:
completed calls at INFO, retries and rate-limit waits at WARN, final failures at
ERROR and attempts at DEBUG. Your application's logging configuration routes
these records. Records contain the operation, request ID, attempts and timing.
Tokens, request bodies, server addresses and server error text are not logged.
Every request carries SDK identity headers, `X-Request-Id` and a cumulative
`X-Loom-Retry-Count` starting at 0.

A final transport failure may follow a committed write. An independent call
generates a different idempotency key; read the result before submitting it again.
`Client` is `Clone`, `Send` and `Sync`. Clones share the HTTP connection pool
and rate state and can make concurrent calls. Dropping a call future or applying
an external timeout stops requests, retry sleeps and pacing sleeps in that future.
There are no detached retry tasks. Cancellation does not roll back a write or
guarantee the server stops processing it.

## Generic calls

`Client::call<T>` accepts a dotted operation name, a serializable object and
`CallOptions`. Every registered operation has a generated response type;
generic calls also accept `serde_json::Value`:

```rust
use clickhouse_loom::CallOptions;
use serde_json::{json, Value};

let result = bot.call::<Value>(
    "memory.pin",
    &json!({"memory_key": "deploy_window"}),
    CallOptions::default(),
).await?;
println!("{} attempts={}", result.data, result.metadata.attempts);
```

Known operations use generated identity and retry policies.
Bound org and namespace override declared request identity;
optional reader filters keep their explicit values. Unknown operations receive
bound org and namespace only and retry connection failures before dispatch.
They do not retry HTTP errors, timeouts, incomplete responses or 429 responses.
The server validates fields and authorizes every operation.

## Capability probe

`probe` fetches `ops.capabilities` on each call and compares the operations and
request fields your application needs:

```rust
use clickhouse_loom::{Client, ProbeRequest};

# async fn example(bot: Client) -> Result<(), clickhouse_loom::Error> {
let report = bot
    .probe(
        ProbeRequest::new()
            .ops(["memory.set", "memory.search"])
            .fields("memory.set", ["tags"]),
    )
    .await?;
println!("build={} schema={} ok={}", report.build_commit, report.schema_version, report.ok);
println!("missing operations: {:?}", report.missing_ops);
println!("missing request fields: {:?}", report.missing_fields);
println!("unknown to SDK: {:?}", report.unknown_to_sdk);
# Ok(())
# }
```

Missing capabilities return a report with `ok = false`. HTTP, transport and
response decoding failures return an error. `unknown_to_sdk` lists server
operations absent from the SDK's generated registry, sorted by name; these do
not make `ok` false. Missing operation and field lists preserve the requested
order and duplicates; the field map sorts operation names. An absent operation
makes all its requested fields missing.
An empty request returns `ok = true`.

`probe_with_options` accepts `CallOptions` and returns `CallResult<ProbeReport>`
with the capability response's metadata. The blocking client provides the same
methods without `.await`.

## Blocking calls

Enable the feature in the consuming application's `Cargo.toml`:

```toml
[dependencies]
clickhouse-loom = { path = "/path/to/loom/sdk/rust", features = ["blocking"] }
```

```rust
use clickhouse_loom::{blocking::Client, ProbeRequest};

let bot = Client::builder("dev", "local", "my-agent").build()?;
let report = bot.probe(ProbeRequest::new().ops(["memory.set", "memory.search"]))?;
println!("schema={} ok={}", report.schema_version, report.ok);
```

The blocking client owns a dedicated runtime thread and reuses the async
transport's wire contracts, retry rules and metadata. Clones share that thread
and connection pool and can call concurrently from separate OS threads.
The last clone joins the worker when dropped. `with_retry` changes only the
clone's policy; call-level retry options take precedence.

Construction and destruction are allowed inside Tokio, but blocking calls return
`Error::Configuration` on threads entered into a Tokio runtime, including
`spawn_blocking` closures. Use the async client there or call from a separate
OS thread. The default async feature set does not include the blocking client.

## Examples

The [examples guide](examples/README.md) covers configuration, prerequisites,
the call sequence, output and cleanup for each program:

| Example                                                           | Demonstrates                                                                             |
| ----------------------------------------------------------------- | ---------------------------------------------------------------------------------------- |
| [memory.rs](examples/memory.rs)                                   | Save → read → lexical search → delete → confirm absence                                  |
| [operations.rs](examples/operations.rs)                           | Async memory, knowledge and context calls, per-call options, metadata and request `send` |
| [blocking.rs](examples/blocking.rs)                               | The memory, knowledge and context flow with the blocking client                          |
| [agent_memory.rs](examples/agent_memory.rs)                       | Travel recall, grounded answers and cited-memory outcomes                                |
| [document_search.rs](examples/document_search.rs)                 | Document citations and off-topic abstention                                              |
| [code_lookup.rs](examples/code_lookup.rs)                         | Selected code lookup with bounded evidence                                               |
| [fact_document_evidence.rs](examples/fact_document_evidence.rs)   | Identity-checked fact and document section reads                                         |
| [multi_agent_handoff.rs](examples/multi_agent_handoff.rs)         | Task leases, result receipts and writer provenance                                       |
| [support_agent_memory.rs](examples/support_agent_memory.rs)       | Customer isolation, revision and erasure                                                 |
| [software_factory_memory.rs](examples/software_factory_memory.rs) | Derived source note and review-agent source recheck                                      |

These seven scenarios correspond to the portable Python examples. The
[parity table](examples/README.md#python-scenario-parity) also documents the
Python-only Pipecat adapter and each scenario's credentials and provider needs.

Run from a Loom checkout's repository root after setting the server address,
token, org and namespace as described in the guide:

```bash
cargo run --manifest-path sdk/rust/Cargo.toml --example memory
cargo run --manifest-path sdk/rust/Cargo.toml --example operations
cargo run --manifest-path sdk/rust/Cargo.toml --features blocking --example blocking
```

## Tests

From `sdk/rust`:

```bash
cargo fmt --check
CARGO_RESOLVER_INCOMPATIBLE_RUST_VERSIONS=fallback cargo generate-lockfile
cargo test --locked
cargo clippy --locked --all-targets -- -D warnings
RUSTDOCFLAGS="-D warnings" cargo doc --locked --no-deps
cargo package --locked
cargo test --locked --features blocking
cargo clippy --locked --all-targets --features blocking -- -D warnings
RUSTDOCFLAGS="-D warnings" cargo doc --locked --no-deps --features blocking
cargo package --locked --features blocking
```

CI runs both feature configurations on Rust 1.84.1 and stable and compares the packaged license
with the repository license. Unit and HTTP fixture tests cover response compatibility
without requiring Loom or a database.
Generated contract tests decode every operation's request and response and compare
response defaults with the server schema. HTTP tests check run targets, recall
filters and write authorship in both client configurations.

The authenticated integration job runs `tests/live.rs` against a disposable
Loom tenant. It checks memory persistence through the legacy and full wire APIs,
namespace configuration, idempotency replay, and scope authorization. The test is
ignored in ordinary Cargo runs. With an isolated authenticated server and its
ClickHouse configured, run from the repository root:

```bash
.venv/bin/python -m pytest -q tests/integration/_rust_sdk_ci.py
```

The Python fixture mints the test's scoped tokens and runs the live Cargo test.

The resolver setting selects dependencies compatible with your Rust version.
A consuming application controls its own dependency lockfile.

## License

Apache-2.0. See [LICENSE](LICENSE).
