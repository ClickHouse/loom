# Session protection

`SessionProtection` binds an application-owned durable authority to a logical conversation. Its clones share the authority independently of client credentials and identity. The built-in store requires a private POSIX directory. It contains an identity, a monotonic protection bit and pending request markers; it contains no bodies or credentials. Other filesystem platforms refuse readiness.

```rust,no_run
use clickhouse_loom::{Client, SessionProtection};
use serde_json::Value;

# async fn example(directory: &std::path::Path, arguments: Value) -> Result<(), clickhouse_loom::Error> {
let family = SessionProtection::create(directory)?;
let pointer = family.pointer(); // Persist in the trusted conversation record.
let client = Client::builder("org", "namespace", "agent")
    .session_protection(family.clone())
    .build()?;
let answer: Value = client.call("knowledge.source.mcp_read", &arguments).await?;
# Ok(())
# }
```

On resume, reopen the stored pointer with `SessionProtection::open`. Missing or invalid state refuses. Never create a replacement family after restore failure. Any interrupted request still pending on reopen latches protection. Share handle clones among active clients rather than reopening the directory per request. The application must retain the pointer and its directory together and prevent trusted storage rollback or replacement.

Retain a `SessionCapture` before scheduling work. It holds all contributing handles and checks them on every automatic attempt, including another client or namespace. Use `capture.contribute(&other_family)` for additional contributors. Nested captures retain their parent's contributors. Client clones and `with_retry` preserve the bound authority.

```rust,no_run
# use clickhouse_loom::{Client, SessionProtection, Error};
# use serde_json::{json, Value};
# async fn example(family: SessionProtection, another_client: Client) -> Result<(), Error> {
let capture = family.capture();
let result: Value = another_client.call_automatic(
    &capture, "memory.set", &json!({"memory_key":"note", "value":"automatic"})
).await?;
// Several operations can run inside capture.scope(async { ... }).await?.
# Ok(())
# }
```

The capture scope follows its Tokio task across awaits. Spawned tasks and queued callbacks must receive a retained capture and enter its scope. Every automatic write must use a capture, including calls made through a newly constructed client. Explicit calls remain available and still observe protection metadata.

`Client::call` owns the declaration, durable admission and response observer. It sends `x-loom-session-protection: v1` only with an installed authority. Each retry checks the retained capture immediately before `send`. Every HTTP status is observed before typed deserialization or API error construction. Protected JSON responses are bounded to 4 MiB; malformed envelopes and unsupported media refuse. Dropping an incomplete request future latches its opening authorities; a process interruption leaves durable pending proof. Cancellation cannot undo a request already sent.

Protected streaming is unavailable. A stream response refuses without returning its body. Ordinary unbound calls send no declaration and preserve their existing behavior. Generic `call` supports explicit operations without requiring generated helpers. Real HTTP, source operations and task cancellation require hosted qualification before activation.
