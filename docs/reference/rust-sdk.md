# Rust SDK

The `clickhouse-loom` crate provides typed async and blocking calls for every
registered Loom operation. It connects to a separately deployed server; see the
[quickstart](../getting-started/quickstart.md) for a local installation or the
[deployment guide](../operations/deployment.md) for a team server.

## Install

Add the crate to your application:

```bash
cargo add clickhouse-loom
```

Import it as `clickhouse_loom`. Use the current stable Rust toolchain for
crates.io installation. The SDK source supports Rust 1.84+, but resolved
dependencies can require a newer toolchain. The library leaves dependency locking
to the application; commit your application's `Cargo.lock`.

The client speaks HTTP/2 to an `https` address, negotiating it over ALPN with a
fallback to HTTP/1.1, and HTTP/1.1 to a plain `http` address.

The crate has an independent version in its
[manifest](https://github.com/ClickHouse/loom/blob/main/sdk/rust/Cargo.toml).
Use [docs.rs](https://docs.rs/clickhouse-loom) for the API of a published version,
and the [operation reference](operations.md) for the server's current contracts.
The repository reference describes the SDK shipped with that source revision.

## Save and read memory

For the local single-user server, org `dev`, namespace `local` and an empty token
match the quickstart. This async example uses the application's existing Tokio
runtime with `macros` and `rt-multi-thread` enabled:

```rust
use clickhouse_loom::{Client, SearchRequest, SetRequest};

#[tokio::main]
async fn main() -> Result<(), Box<dyn std::error::Error>> {
    let client = Client::builder("dev", "local", "rust-agent")
        .base_url("http://localhost:7777")
        .api_key("")
        .build()?;
    let saved = client.set(
        SetRequest::new("deploy_window", "Deploy Tue/Thu")
            .tags(["deployment"])
            .ttl_seconds(300),
    ).await?;
    println!("saved={:?}", saved);
    let memory = client.get("deploy_window").await?;
    println!("found={} value={}", memory.found, memory.value);
    let hits = client.search(
        SearchRequest::new("when do we deploy")
            .agent_id("rust-agent")
            .namespace_scope("this")
            .top_k(5),
    ).await?;
    println!("abstain={} hits={}", hits.abstain, hits.results.len());
    client.delete("deploy_window").await?;
    Ok(())
}
```

A save can merge with an existing memory: inspect `noop` and `merged_into_key`
before assuming the requested key holds the saved row. A missing key returns
`found=false`. Deletion writes a tombstone; use the
[erasure operations](operations.md) when physical erasure is required.
Check search abstention, retrieval risk, degradation and pending extraction
before using results as evidence.

## Configure a team connection

Obtain a writer token for your org and namespace through **Settings → Tokens**
or your operator. The builder reads `LOOM_BASE_URL` and `LOOM_API_KEY`:

```rust
use clickhouse_loom::Client;

let client = Client::builder("my-org", "my-namespace", "rust-agent")
    .user_id("service-user")
    .session_id("session-1")
    .build()?;
```

Set those environment variables in the process that runs your application.
Explicit `.base_url(...)` and `.api_key(...)` override them; `.api_key("")`
disables authorization. Empty environment values are unset. Without a server
address the default is `http://localhost:7777`; without a token the SDK sends
no Authorization header. `LOOM_ORG` and `LOOM_NAMESPACE` are example conventions,
not variables read by the client builder.

Org and the addressed namespace remain bound to the client. Bound identity
fills declared caller and authorship fields. Reads with explicit row selectors
and recall filters preserve those selectors. Search defaults to agent `"*"`
and omits user, session and namespace-scope filters; the server chooses its
default scope. Filter explicitly with request option methods.
`memory.list` defaults omitted agent, user and session filters to the client
identity. Collection deletion requires an explicit target namespace and
preserves it. A bound run labels reads through `X-Loom-Run-Id`; operations that
address a target run preserve the requested run ID.

## Generated requests

Method names replace dots with underscores: `memory.pin` becomes `memory_pin`.
Generated request and response structs cover the full wire contract.
The memory facade also offers `set`, key-only `get` and `delete`, and `search`.

```rust
use clickhouse_loom::{CallOptions, MemoryGetInput};

let result = MemoryGetInput::new("deploy_window")
    .fields(["value", "tags"])
    .follow(true)
    .send(&client, CallOptions::default())
    .await?;
println!("found={:?}", result.data.found);
```

Requests use `new` for required arguments and chain option methods. Public
fields remain writable. Options are sent only when present; zero, false and
empty collections remain values. Nullable options use `Option<Option<T>>`:
`None` omits the field, `Some(None)` sends null and `Some(Some(value))` sends
a value. A `<field>_null()` method sends null explicitly.

Public wire structs and `Error` are non-exhaustive. Use constructors for
requests and `..` when destructuring responses. Unknown response fields are
ignored; missing required fields return `Error::Protocol`. Projected responses
preserve omitted fields as `None`.

The SDK also provides `Client::call<T>` for dotted operation names and JSON
objects. Known operations retain generated identity and retry policies.
Unknown operations receive bound org and namespace, and retry only connection
failures before dispatch. The server authorizes every call.

## Routes outside the op registry

`request` and `request_json` reach the routes the operation registry does not
hold, such as `/v1/whoami` and `/v1/knowledge.upload`. A `RouteRequest` carries
the method, a path on the server the client is bound to, query parameters and
a JSON or raw body, all sent as given:

```rust
use clickhouse_loom::RouteRequest;
use serde_json::{json, Value};

let chosen: Value = client
    .request_json(RouteRequest::post("/v1/me/set-default-namespace").json(&json!({"namespace": "eng"})))
    .await?;
```

`request` returns the status, headers and bytes of a response below 400.
`request_json` decodes the body as the requested type. A status of 400 or
higher is `Error::Api`. The SDK resends on a 429 after the server's hint and
within the client's pacing budget. A refused HTTP/2 stream returns
`Error::Transport` without a retry. See the
[route contract](https://github.com/ClickHouse/loom/blob/main/sdk/rust/README.md#routes-outside-the-op-registry).

## Call options, errors and retries

Methods return typed responses and `Error`. Their `*_with_options` variants,
and request `send`, return `CallResult<T>` or `CallFailure`:

```rust
use clickhouse_loom::{CallOptions, RetryPolicy};
use std::time::Duration;

let result = client.get_with_options(
    "deploy_window",
    CallOptions::default()
        .timeout(Duration::from_secs(5))
        .retry(RetryPolicy::disabled())
        .request_id("recall-1"),
).await?;
println!("found={} attempts={}", result.data.found, result.metadata.attempts);
```

`CallMetadata` carries request IDs, HTTP status, schema version, server timing,
rate-limit hints and total attempts. `CallFailure` preserves the error and
available metadata. `Error::Api` retains status, code, category, retry hints and
raw detail. `Error::Transport` reports transport failures; `Error::Protocol`
reports an invalid successful response.

The default timeout is 60 seconds per HTTP attempt, including the response
body. Call options override client defaults for one call. Retries after
dispatch require declared idempotence. Declared idempotent writes reuse one
idempotency key across attempts. A separate call generates a separate key;
after a timeout, read the result before resubmitting a write.

Customize `RetryPolicy` through the client builder's `.retry(...)` or
`CallOptions::retry` for one call. Start with `RetryPolicy::default()` and set
`max_attempts`, backoff, HTTP statuses or timeout policy. By default, each retry
round allows at most three attempts, including the initial request, for connection
failures, HTTP 408 and HTTP 500–599. Backoff starts at 100 ms, doubles to a
five-second ceiling and uses jitter.

`RetryPolicy::disabled()` disables retries. HTTP 429 pacing is configured
separately and starts a fresh retry round after waiting. To disable both:

```rust
use clickhouse_loom::{Client, RetryPolicy};

let client = Client::builder("my-org", "my-namespace", "rust-agent")
    .retry(RetryPolicy::disabled())
    .pacing(false)
    .build()?;
```

See the [SDK retry contract](https://github.com/ClickHouse/loom/blob/main/sdk/rust/README.md#retries-and-logging)
for retry budgets and timeout policies.

Async clients are `Clone`, `Send` and `Sync`; clones share connections and rate
state. Cancelling a future stops its retry and pacing work, but does not undo
a committed write. Logs use the `clickhouse_loom` target through the `log` facade.

## Blocking client

Enable the optional feature:

```bash
cargo add clickhouse-loom --features blocking
```

```rust
use clickhouse_loom::blocking::Client;

fn main() -> Result<(), Box<dyn std::error::Error>> {
    let client = Client::builder("dev", "local", "rust-agent")
        .base_url("http://localhost:7777")
        .api_key("")
        .build()?;
    println!("found={}", client.get("deploy_window")?.found);
    Ok(())
}
```

The blocking client owns a runtime thread and offers the same generated
operations, call options and routes outside the op registry. Call it from an
ordinary OS thread. Calling from a thread entered into Tokio returns
`Error::Configuration`.

## Capabilities and examples

`Client::probe` checks which operations and request fields the server supports.
It reports missing operations, missing request fields and server operations
unknown to the SDK. Probe does not run automatically at
construction; see the
[probe contract](https://github.com/ClickHouse/loom/blob/main/sdk/rust/README.md#capability-probe).

The [examples guide](https://github.com/ClickHouse/loom/blob/main/sdk/rust/examples/README.md)
covers async and blocking memory, document search, code lookup and multi-agent
flows.
