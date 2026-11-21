# OpenAI Agents conversation sessions

`LoomSession` implements the native Python and TypeScript session interfaces over
Loom episodic memories. Existing tools can separately retain facts beyond a
conversation. The framework packages remain optional dependencies.

Each nonempty addition encodes every native item before discovery or mutation,
checks that the server advertises `memory.append_batch`, and sends one request.
The server requires the Postgres memory authority. It prepares every episodic row
and commits the whole addition only when every key is fresh under the generation
and head locks. A refused item or existing key refuses the batch. There is no
point-write or ordinary-bulk fallback; an empty addition sends nothing. The SDKs
and server come from the same checkout, as the
[installation instructions](../features/integrations/frameworks.md#openai-agents-sdk)
require.

Each item retains its native JSON, timestamp-and-random key, end user and session
derived from the user and native session id. Episodic rows preserve repeated
values as distinct occurrences without semantic extraction or duplicate merging.
Python and TypeScript use separate derived-session prefixes because their native
item formats differ. A committed batch can report `PUBLICATION_PENDING`; resolve
an uncertain outcome with the original request's idempotency key, without a fresh
addition or compensating deletion.

Reads page through `memory.list`, deduplicate overlapping keys and hydrate missing
or truncated values with `memory.get`. Items are returned in key order, with a
requested limit retaining the latest items. Positive limited reads also scan the
history because server arrival times cannot bound writer-generated keys. The TypeScript adapter
snapshots the validated user at construction. Its native image and file byte
payloads use JSON arrays in storage and return as `Uint8Array`; ordinary JSON and
media information remain unchanged. Deletion carries the user and derived
session: `pop_item` deletes the latest item, and `clear_session` deletes the listed
history. Each addition is atomic; concurrent history paging, pops and clears do
not form a transaction across the conversation.
If an item disappears before hydration, the read fails instead of returning an
incomplete native item. Keys use writer clocks; concurrent writers do not share a
server sequence.

Both implementations retain the application's existing bound protection family
from construction and each callback. Python also retains contributors active in
the callback's context. Rebinding the client cannot remove an earlier contributor.
Native user and session ids select stored history; they do not establish a
protection family. Use one session instance per logical conversation family, and
reopen the same application-owned protection capsule when restoring that
conversation.

Native history reads and additions run as automatic work under all retained
contributors. A marked reply protects every contributor before its value returns.
A complete first marked history reply may still return its items; subsequent
pagination, hydration or batch writes are suppressed. A marked discovery response
prevents the append request. A marked append acknowledgement protects the retained
contributors after the whole batch may already have committed; it does not imply
rollback. Explicit pop and clear operations remain usable after protection and
keep observing replies under the retained contributors.
Model-selected tools keep their explicit operation semantics.

## Source entry points

- `sdk/python/loom/integrations/openai_agents.py`: `LoomSession`.
- `sdk/typescript/src/integrations/openai-agents.ts`: `LoomSession`.

## Invariants

The agent's organization, namespace and name scope each history. An omitted user
writes shared namespace rows; an explicit user selects that user's history. The
server supplies actor and source provenance. Ordinary memory deletion, erasure
and rolling-upgrade rules apply. No local history database or schema is added.
