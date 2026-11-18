# clickhouse-loom

Use [ClickHouse Loom](https://clickhouse.github.io/loom/) from Rust. Loom is a
self-hosted server for shared long-term AI agent memory, document search and
code indexing.

This crate connects to a separately deployed Loom server backed by ClickHouse.
The server image is `ghcr.io/clickhouse/loom`. Use a local single-user installation
or connect to a team deployment managed by your operator.

`Client` provides `set`, `get`, `search` and `delete` memory operations with typed
responses. Calls are async and use the application's Tokio runtime.

## Install

From your Rust application's directory, add the SDK from a local Loom checkout:

```bash
cargo add clickhouse-loom --path /path/to/loom/sdk/rust
```

Replace `/path/to/loom` with the checkout's location. The equivalent `Cargo.toml`
configuration, including the runtime used by the example, is:

```toml
[dependencies]
clickhouse-loom = { path = "/path/to/loom/sdk/rust" }
tokio = { version = "1", features = ["macros", "rt-multi-thread"] }
```

Rust 1.84+. Runtime deps: reqwest with Rustls, serde, serde_json, Tokio, rand, uuid,
httpdate, log and derive_more. The crate name is `clickhouse-loom`; Rust imports it as
`clickhouse_loom`.

## Use

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
    bot.set(SetRequest::new("deploy_window", "Deploy Tue/Thu, never Friday")).await?;
    let memory = bot.get("deploy_window").await?;
    let hits = bot.search(SearchRequest::new("when do we deploy")).await?;
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
`user_id`, `session_id`, `retry`, `pacing` and `ratelimit_max_wait`.
The default timeout is 60 seconds per attempt. It covers the whole HTTP request
and response body. The server address comes from `LOOM_BASE_URL`, defaulting to
`http://localhost:7777`. The token comes from `LOOM_API_KEY`; without a token,
requests carry no Authorization header.
Explicit settings override the environment. `.api_key("")` disables authorization.
Empty environment values are treated as unset.

Builder user and session values apply to `set`, `get` and `delete`.
Search defaults to `agent="*"` and omits user, session and namespace-scope filters.
The server chooses its default namespace scope.
Set `SearchRequest.agent_id`, `namespace_scope`, `user_id` or `session_id` to
filter a search explicitly. Org and the addressed namespace remain bound to the client.

Set requests support JSON values, memory type, tags, TTL, confidence and
`if_match_sha256`. Accepted JSON values are objects, arrays, strings, numbers
and booleans. Top-level `null` is not accepted. Options are sent only when `Some`;
zero, false and empty collections remain values. Unset options use server defaults.
Search requests support top-k, tags, memory type and search mode.

## Request and response types

Public request, response and API error structs are `#[non_exhaustive]`.
Create requests with `new`, then set the options you need:

```rust
let mut request = SearchRequest::new("when do we deploy");
request.top_k = Some(10);
let hits = bot.search(request).await?;
```

Read response fields directly, or include `..` when destructuring. `Error` is
also non-exhaustive: include a fallback arm when matching it. Match
`Error::Protocol { status, source, .. }`. Error codes and categories are strings.

Response decoding ignores unknown JSON fields. Optional fields use `Option<T>`
or documented defaults. Missing required response fields produce
`Error::Protocol`.

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
when recall credit was not disclosed. An omitted risk label is an empty string;
it does not imply low risk.

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

## Retries and logging

Calls do not follow redirects. Retry and pacing defaults are:

- Up to 3 attempts on connection failures, HTTP 408 and HTTP 500–599.
  Configuration errors and the SDK's excluded error codes stop retries;
  `retryable=false` alone does not stop a status retry.
- Exponential backoff starts at 100 ms, doubles to a 5 second ceiling, and uses
  jitter in the range 0.75–1.25 times the delay. A valid `Retry-After` of up to
  60 seconds replaces backoff. Headers take precedence over `detail.retry_after_s`;
  numeric seconds and HTTP dates are accepted.
- `set` and `delete` generate one UUID idempotency key per call and reuse it
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

## Examples

[`examples/memory.rs`](examples/memory.rs) — save → read → search → delete →
confirm absence. Configuration, the call sequence and output are in the
[examples guide](examples/README.md). Run from the repository root:

```bash
LOOM_BASE_URL=http://localhost:7777 LOOM_API_KEY= LOOM_ORG=dev LOOM_NAMESPACE=local cargo run --manifest-path sdk/rust/Cargo.toml --example memory
```

The example uses an existing namespace and lexical search scoped to its agent.
Memory writes use the server's configured embedding provider.

## Tests

From `sdk/rust`:

```bash
cargo fmt --check
CARGO_RESOLVER_INCOMPATIBLE_RUST_VERSIONS=fallback cargo generate-lockfile
cargo test --locked
cargo clippy --locked --all-targets -- -D warnings
RUSTDOCFLAGS="-D warnings" cargo doc --locked --no-deps
cargo package --locked
```

CI runs these checks on Rust 1.84.1 and stable and compares the packaged license
with the repository license. Tests cover response compatibility
and use a local fake HTTP server; they require neither Loom nor a database.

The resolver setting selects dependencies compatible with your Rust version.
A consuming application controls its own dependency lockfile.

## License

Apache-2.0. See [LICENSE](LICENSE).
