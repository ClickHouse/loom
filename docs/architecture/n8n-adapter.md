# n8n adapter

The n8n community package stores conversation history and durable facts in a Loom
deployment through the TypeScript SDK. The nodes add no storage service or local cache.

## Interfaces and storage

`LoomMemory.supplyData` returns the framework's `BufferWindowMemory`, backed by a
`LoomChatHistory` implementation of `BaseListChatMessageHistory`. Message serialization
uses the framework's stored-message format. The window changes the context sent to a
model, while older messages remain available to subsequent reads and explicit clearing.

Each message is an `episodic` memory, with a timestamp and random suffix in its key.
Episodic writes preserve repeated messages without semantic merging. The Loom agent's
organization, namespace and agent name scope the history. The user and a digest of
the workflow/session identifiers supply the remaining identity fields. Reads page
through `memory.list`, deduplicate overlapping pages when projected rows are filtered,
sort by key and use `memory.get` for truncated values. `clear` deletes only those items.
Concurrent writers have no transaction across a whole turn.

The Loom action node calls `memory.set`, `memory.search` and `knowledge.search`.
Its inputs supply the organization, namespace, agent name and optional user. The
server derives provenance from the token and operation and enforces namespace grants.

## Packaging

The package bundles the SDK and framework libraries to avoid runtime dependency
conflicts with other community nodes. Its build includes their license files.
`n8n-workflow` is the host-provided peer. Development dependencies pin the interfaces
used by tests; they are not added to Loom's server or SDK dependencies.

The package targets self-hosted n8n, with the official node CLI's configuration for
that target. It does not claim Cloud verification. The release-tag workflow builds
and checks the package before publishing with an npm trusted publisher and provenance;
no registry token is stored in the repository.

## Invariants

- The adapter sends data only to the Loom URL in its n8n credential.
- Token grants authorize access; a user parameter is a filter, not authorization.
- Conversation clearing does not delete durable facts created by the action node.
- SDK or server refusals fail the node, or become error items under continue-on-fail.
- The adapter creates no tables and uses Loom's existing erasure paths.

## Source entry points

- `integrations/n8n/nodes/LoomMemory/LoomChatHistory.ts`: message persistence.
- `integrations/n8n/nodes/LoomMemory/LoomMemory.node.ts`: framework memory adapter.
- `integrations/n8n/nodes/Loom/Loom.node.ts`: durable memory and document actions.
- `.github/workflows/n8n-publish.yml`: checks and trusted publishing.

[Framework setup](../features/integrations/frameworks.md#n8n)
