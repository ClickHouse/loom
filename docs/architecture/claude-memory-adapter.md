# Claude API memory file tree

The Claude API memory adapter implements the framework's Python memory-tool bases
and TypeScript memory handlers through Loom knowledge documents. The framework
SDKs are optional dependencies. Both implementations use the same document URI
and store tag, so either language reads files created by the other.

A file is `loom-memory://<store>/memories/<path>`. Directories are URI prefixes,
with no separate directory rows. Paths are normalized and refused unless they
remain under `/memories`. Directory reads use `knowledge.list` with a store tag,
a URI prefix and projected URI fields; file reads use `knowledge.get`. Edits use
`knowledge.upsert`, and deletion uses bounded `knowledge.bulk_delete` calls.
Rename copies each file before deleting its old URI. A failed rename can leave
copies, and simultaneous read-modify-write edits keep the later write.

## Source entry points

- `sdk/python/loom/integrations/claude_memory.py`: `AsyncLoomMemoryTool` and
  `LoomMemoryTool`.
- `sdk/typescript/src/integrations/claude-memory.ts`: `loomMemoryTool` and
  `loomMemoryHandlers`.

## Invariants

A store groups files; namespace authorization controls access. Private user trees
require separate namespaces. The adapter never interprets a store name as an
access grant. Actor, source provenance, document revisions, blob cleanup and
erasure follow existing knowledge operations. No local persistent state, database
or schema is added. Directory operations are not atomic snapshots.
