# Agno user-memory database

The Agno adapter implements the user-memory methods of its 3.2 `BaseDb` interface
through the synchronous Loom SDK. The optional minor-pinned dependency stays out
of the core SDK and server. `MemoryManager` accepts this database independently of
an agent's session database. Non-memory capabilities refuse instead of using a
framework-default store.

## Invariants

Each adapter binds a user and application store. Every SDK call carries that user
and a derived session; keys include the same scope digest plus a native-id digest.
A different explicit user is refused. Native user memories are JSON in episodic
Loom rows, preserving ids, metadata and revisions without semantic merging of
different native records. Actor and source provenance come from Loom's normal
write path. The ordinary memory deletion, erasure and rolling-schema rules apply.

Listing follows capped and projected pages, deduplicates overlap and hydrates
truncated values before converting them to native objects. Content, topics and
attribution filters, sorting, pagination and statistics run over this scoped
user/store snapshot. There is no local persistent state. Concurrent edits keep
the latest server write; bulk operations are a sequence of writes and can partially
finish on failure. Namespace grants isolate mutually untrusted applications.

## Source entry points

- `sdk/python/loom/integrations/agno.py`: `LoomMemoryDb`.
- `sdk/python/tests/test_agno.py`: native memory-manager behavior.
- `tests/integration/test_sdk_agno_memory.py`: live memory scope and provenance.
