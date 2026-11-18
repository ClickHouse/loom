# Google ADK memory service

The Google ADK adapter implements the framework's memory service through Loom's
Python and TypeScript SDKs. Framework packages are optional dependencies. Every
application uses a separate Loom namespace because its knowledge documents are
shared with all readers of that namespace.

Session writes extract text from active user and model events, excluding tool
payloads and thoughts, then call `memory.set_from_messages` in bounded batches.
Both SDKs refuse empty, sentinel and whitespace-padded user ids before any call.
Each call carries the end user and a session derived from the user and native
session id. Python also implements the installed framework's incremental-event,
direct-memory and deletion methods. Python deletion ids carry the key, session,
writer and exact version. They call `memory.delete_version` directly. A missing or
rewritten version is a no-op, and
another writer's row is never selected. The additive `agent` response field on
memory search hits comes from the stored row identity, including parked pages.
A hit without a known writer or version has no deletion id. Older clients ignore
the extra field; exact ADK deletion requires a server that supplies it. This adds
no persisted column or migration. TypeScript returns writer-scoped informational
ids; its installed framework interface has no deletion method.

Search first calls `memory.search` for the bound user, then fills available result
slots from namespace-scoped `knowledge.search`. Knowledge is shared application
context, not private user memory. The two SDKs use the same digest encoding; the
framework's native timestamp representation is converted at the boundary. One
service instance is bound to one application. Namespace grants isolate mutually
untrusted applications.

## Source entry points

- `sdk/python/loom/integrations/google_adk.py`: `LoomMemoryService`.
- `sdk/typescript/src/integrations/google-adk.ts`: `LoomMemoryService`.

## Invariants

The server supplies actor and source provenance through its ordinary write path.
The adapter adds no database, schema or provider. Existing memory deletion,
knowledge erasure and rolling-upgrade rules apply. Semantic extraction can revise
facts; this service is not an exact transcript store. Knowledge results have no
user-memory id and cannot be deleted through the memory service.
