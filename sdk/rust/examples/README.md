# Examples

Runnable examples for `clickhouse-loom`. Each example uses the application's
Tokio runtime and connects to an existing Loom namespace. See the
[SDK README](../README.md) for installation and client configuration.

## memory.rs

[memory.rs](memory.rs) saves a memory, reads it, searches for it, deletes it and
confirms it is absent.

Run from the repository root against a local Loom server:

```bash
LOOM_BASE_URL=http://localhost:7777 LOOM_API_KEY= LOOM_ORG=dev LOOM_NAMESPACE=local cargo run --manifest-path sdk/rust/Cargo.toml --example memory
```

The example reads `LOOM_ORG` and `LOOM_NAMESPACE`, defaulting to `dev` and `local`.
The SDK reads `LOOM_BASE_URL` and `LOOM_API_KEY`; the default address is
`http://localhost:7777`. For a team server, use its address, a writer token and
an authorized org and namespace.

`Client::builder(org, namespace, "rust-example")` binds the client's identity.
The example uses a unique memory key for each invocation.

1. `Client::set` — saves the memory and returns its ID and version.
2. `Client::get` — checks that the saved value is returned.
3. `Client::search` — finds the memory with lexical search in the bound namespace
   and agent.
4. `Client::delete` — tombstones the memory. Cleanup is attempted even when a read
   or search fails.
5. `Client::get` — confirms `found=false` after deletion.

Output has this form; IDs, keys, versions and search metadata vary:

```text
set: memory_id=<id> version=<version>
get: found=true value="Rust SDK memory demo: <key>"
search: 1 hits abstain=false risk=low pending_extraction=0 degraded=[]
delete: tombstoned=true
get after delete: found=false
```
