# n8n adapter

The n8n adapter in the TypeScript SDK stores conversation history and durable facts in a Loom
deployment. The nodes add no storage service or local cache.

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

All JavaScript adapters ship in the `@clickhouse/loom` npm artifact. The n8n adapter
has the same version and publication workflow as the SDK. Its public subpath exports
the node, credential and history classes in ESM and CommonJS formats.

The SDK build also produces native `.node.js` and `.credentials.js` files under
`dist/integrations/n8n`. A nested package manifest marks these files as CommonJS,
while the SDK root remains ESM. The native files bundle the SDK source and framework
libraries, with their license files, to avoid runtime conflicts with other nodes.
`n8n-workflow` is an optional, host-provided peer. The SDK's browser-compatible entry
points do not import the adapter or its Node.js dependencies.

Self-hosted n8n loads the native files through `N8N_CUSTOM_EXTENSIONS`. This bypasses
the Community Nodes installer's package naming constraint using n8n's supported
custom-directory loader. The package manifest also lists the nodes and credential
for loaders that consume it. The adapter does not claim Cloud verification.

The SDK's distribution tests install its tarball without peers and in a separate,
locked n8n host. The hosts keep n8n's Zod 3 requirement apart from other frameworks'
Zod 4 requirement. They load the node files and exercise the adapter against its
pinned framework interfaces. The main release workflow publishes the
verified SDK tarball through its existing npm trusted publisher. There is no
integration-specific npm publisher, version or tag.

## Invariants

- The adapter sends data only to the Loom URL in its n8n credential.
- Token grants authorize access; a user parameter is a filter, not authorization.
- Conversation clearing does not delete durable facts created by the action node.
- SDK or server refusals fail the node, or become error items under continue-on-fail.
- The adapter creates no tables and uses Loom's existing erasure paths.

## Source entry points

- `sdk/typescript/src/integrations/n8n/nodes/LoomMemory/LoomChatHistory.ts`: message persistence.
- `sdk/typescript/src/integrations/n8n/nodes/LoomMemory/LoomMemory.node.ts`: framework memory adapter.
- `sdk/typescript/src/integrations/n8n/nodes/Loom/Loom.node.ts`: durable memory and document actions.
- `sdk/typescript/scripts/build-n8n.mjs`: native node bundles and licenses.
- `.github/workflows/release.yml`: shared SDK trusted publishing.

[Framework setup](../features/integrations/frameworks.md#n8n)
