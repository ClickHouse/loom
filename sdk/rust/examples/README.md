# Examples

Runnable examples for `clickhouse-loom`. The memory example uses an existing
namespace. See the [SDK README](../README.md) for installation and client
configuration.

## memory.rs

An agent that saves a memory, recalls it by key and lexical search, then deletes
it. The code is in [memory.rs](memory.rs).

Run from the repository root against a local Loom server:

```bash
LOOM_BASE_URL=http://localhost:7777 LOOM_API_KEY= LOOM_ORG=dev LOOM_NAMESPACE=local cargo run --manifest-path sdk/rust/Cargo.toml --example memory
```

The example reads `LOOM_ORG` and `LOOM_NAMESPACE`, defaulting to `dev` and `local`.
The SDK reads `LOOM_BASE_URL` and `LOOM_API_KEY`; the default address is
`http://localhost:7777`. For a team server, use its address, a writer token and
an authorized org and namespace.

`Client::builder(org, namespace, "rust-example")` binds org, namespace and agent
for the example's calls. Each invocation uses a unique memory key. Memory writes
use the server's configured embedding provider.

The example runs these steps:

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
