# Dify tools adapter

The Dify plugin calls Loom through the Python SDK and stores no content locally.
Its three native tools map to `memory.set`, `memory.search` and `knowledge.search`.
The optional standalone package requires the 0.11 plugin SDK minor series and the
Loom 1.x SDK; the server and core SDK do not depend on Dify.

## Invariants

Provider credentials bind the endpoint, organization, namespace, token and agent.
A required User ID form parameter binds the end user outside the model's arguments.
Recall can read that user's memories across Loom agents; documents are shared by the
namespace. Both search operations pin `namespace_scope=this`. Separate namespaces
and token grants constrain mutually untrusted applications. Loom applies its usual
actor, provenance, conflict handling, rolling schema and erasure rules to these calls.

The administrator controls the HTTP destination. The plugin checks its URL shape
and rejects embedded credentials, queries and fragments; deployment network policy
constrains which hosts the plugin runner can reach. Requests have finite timeouts
and one attempt. Tool errors expose no credentials or remote response detail.
The plugin creates no additional database, cache, model provider or telemetry path.

Dify patches Python threading with gevent greenlets. Each request constructs, calls
and closes its SDK client inside the current hub's native thread pool, so overlapping
clients do not run asyncio loops on the same native thread. Credentials stay local to
that request. The SDK call waits at most 30 seconds; pool admission, client setup and
close have separate waits rather than a whole-invocation deadline.

Dify installs the package from a local `.difypkg`. The official CLI packages source,
metadata, runtime requirements, icons, license, README and privacy statement. Its
ignore file excludes tests, environments, credentials and build state. CI invokes
the native tools against a fake SDK HTTP transport, checks overlapping native calls
over loopback HTTP, validates native tool schemas and builds the package.
Live tests run only with an explicit private Loom URL and
token; platform execution is documented separately from those SDK tests.

## Source entry points

- `integrations/dify/client.py`: scoped SDK calls and request failures.
- `integrations/dify/provider/loom.py`: credential validation.
- `integrations/dify/tools/`: native tool implementations and schemas.
- `.github/workflows/dify-plugin.yml`: tests and packaging.
