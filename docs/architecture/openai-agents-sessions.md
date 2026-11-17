# OpenAI Agents conversation sessions

`LoomSession` implements the native Python and TypeScript session interfaces over
Loom episodic memories. Existing tools can separately retain facts beyond a
conversation. The framework packages remain optional dependencies.

Each item is one `memory.set` call with its native JSON, a timestamp-and-random
key, the end user and a session derived from the user and native session id.
Point writes bypass SDK coalescing so concurrent repeated items remain separate.
Episodic rows preserve repeated items without semantic extraction or duplicate
merging. Python and TypeScript use separate derived-session prefixes because
their native item formats differ.

Reads page through `memory.list`, deduplicate overlapping keys and hydrate missing
or truncated values with `memory.get`. Items are returned in key order, with a
requested limit retaining the latest items. Deletion carries the user and derived
session: `pop_item` deletes the latest item, and `clear_session` deletes the listed
history. Concurrent appends or clears are individual server operations, not a
transaction across the conversation.
If an item disappears before hydration, the read fails instead of returning an
incomplete native item. Keys use writer clocks; concurrent writers do not share a
server sequence.

## Source entry points

- `sdk/python/loom/integrations/openai_agents.py`: `LoomSession`.
- `sdk/typescript/src/integrations/openai-agents.ts`: `LoomSession`.

## Invariants

The agent's organization, namespace and name scope each history. An omitted user
writes shared namespace rows; an explicit user selects that user's history. The
server supplies actor and source provenance. Ordinary memory deletion, erasure
and rolling-upgrade rules apply. No local history database or schema is added.
