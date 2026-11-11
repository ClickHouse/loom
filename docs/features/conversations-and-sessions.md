# Conversations and sessions

Loom keeps three kinds of session state, each in its own plane:

| Plane              | Holds                                                                      | Embedded, searched                                                          | Written by                                                                  |
| ------------------ | -------------------------------------------------------------------------- | --------------------------------------------------------------------------- | --------------------------------------------------------------------------- |
| Conversation plane | The verbatim turns of a thread (`loom.conversations`)                      | User and assistant turns and compaction summaries, by `conversation.search` | `conversation.append`, the plugins' capture hooks, `loom ingest-transcript` |
| Session briefs     | The condensed, resumable state of one line of work (`loom.session_briefs`) | No                                                                          | `session.brief_save`, the coding plugins at compaction and session end      |
| Memory             | Durable facts extracted from what was said                                 | Yes, by `memory.search`                                                     | `memory.set`, `memory.set_from_messages`                                    |

Storing a turn does not create a memory; to turn a conversation into facts, post it to
[`memory.set_from_messages`](./memory/extraction.md). An owner can switch transcript recording off without losing
extracted memory, and a brief never competes with facts in recall.

## Conversations

### Recording is the owner's choice

`conversation.append` stores a turn only when the namespace has conversation logging
on. The switch (`conversation_logging_enabled` on `loom.namespaces`) is off by default
and fails closed: a namespace with no row is not
recorded. The owner turns it on with `conversation.set_logging(enabled=true)` (admin) or
the **Record conversations** switch on the dashboard's Conversations page.

When logging is off the append is accepted and nothing is kept: the response says
`stored: false, reason: "logging_disabled"`. A client cannot override the owner's choice.
If the turn is removed while its embedding runs, append skips the late vector and
answers `stored: false, reason: "deleted"`.

### Writing turns

`conversation.append` takes `thread_id`, `role` (`user`, `assistant`, `system` or
`tool`), `content`, and optionally `msg_id`, `reasoning` (the model's reasoning kept in
its own column), `user_id`, `run_id`, `metadata` and `tokens`. The thread is created on
the first append; `conversation.create` only attaches a `title`, `metadata` or `run_id`
up front.

- **Idempotent on `msg_id`.** A turn whose `(thread_id, msg_id)` already landed is not
  written again (`reason: "duplicate"`). Without a `msg_id` the server derives one from
  a hash of the thread, role and content, so a client that re-logs a turn does not
  double the transcript.
- **User and assistant turns are embedded**, up to their first 2,048 characters,
  which the embedding row keeps: a re-embed starts from the text the vector was built
  from. Rows written by earlier builds keep 240 characters, and a re-embed works from
  those. `system` and `tool` turns stay in the transcript unindexed, except a
  compaction's summary.
- **The embed is fail-open.** A failed embed still stores the turn, with the error
  recorded on its embedding row. Nothing retries it: the turn stays out of vector
  search and is found only by the substring fallback.
- **A turn that reads as high is not embedded.** A turn whose text holds a high shape (a
  pasted API key, a `password=` assignment, a card number, an SSN or a stated PIN) is
  stored as written but gets no embedding row, and its text is never sent to the embedder.
  `conversation.search` never returns it, by vector or by the substring fallback, and
  `conversation.threads` lists such a last turn with an empty excerpt (and a title that
  reads as high as empty); it checks the 240 characters its excerpt shows and
  1,024 past them. The titler never sends such a turn to its model. A thread whose
  first eight user and assistant turns all read as high, or whose generated
  title reads as high, stays untitled, marked `title_refused` in its metadata so the
  titler does not ask again; one with fewer such turns waits, unmarked, for a plain
  turn. New `metadata` from `conversation.update` or `conversation.create` replaces
  the mark, and the titler then asks once more and marks the thread again.
  `conversation.get` and `conversation.list` still serve the turn in its thread
  ([sensitivity](../architecture/security.md#sensitivity)).

### Saving single messages

A chat client can let a user save one message for later search without recording the
namespace's transcripts. `conversation.save_message` (writer) is that opt-in, and
`conversation.search_saved_messages` (viewer) is its search. A saved message is a
conversation row, never a document in the user's personal collection
([sharing](../architecture/sharing.md#saved-messages)).

Clients save messages only when every replica preserves saved state during
compaction. See the
[rolling-upgrade rules](../architecture/rolling-upgrades.md#the-rules).

They differ from `conversation.append` and `conversation.search` in six ways:

- **The Record conversations switch does not apply.** The save is stored even when the
  switch (or `conversation.set_logging`) has turned recording off, and saving does not turn
  recording on.
- **The message is flagged, not re-labelled.** The transcript row and its embedding row
  carry `is_saved` and `saved_at`, the time the user saved the message. A message the
  transcript already holds is flagged in place: its `run_id` and content stay as
  recorded.
- **The search is per user.** `conversation.search_saved_messages` returns only flagged
  messages owned by `user_id`, in the one namespace named. `conversation.search` ranks
  every recorded turn and fans out over the caller's namespaces by default.
- **The search is hybrid.** The vector leg embeds a message's first 2,048 characters;
  a BM25 leg reads its whole text, so an exact term (a ticket id, a table name) or a
  phrase deep in a long saved answer still ranks. `search_mode="hybrid"` (default) fuses
  the two by reciprocal rank, `semantic` and `lexical` run one leg, and `lexical_weight`
  (0–1) scales the BM25 leg. A BM25 hit's excerpt is centred on the matched terms. The
  corpus is one user's saved messages, so its statistics are computed per query.
- **A token pinned to one user works here.** An integration that serves many users through
  one credential mints it with `--read-scope own_user` (and `--read-user-id` for one
  user). The saved-message ops then act as that user only: a body naming another
  `user_id` is refused with 403, and every statement carries the token's user. An
  end-user token calls these ops with `namespace: "@me"`
  ([sharing](../architecture/sharing.md#saved-messages)). The conversation tables have no
  `session_id`, so an `own_session` token is refused.
  Every other conversation op (`append`, `get`, `list`, `search`, `threads`, ...) stays
  refused under such a token, because a thread mixes users and has no user axis to bound.
- **Ownership, deletion and role are checked on every save.** Saving a message stored
  under another `user_id` answers `indexed: false, reason: "owner_mismatch"`. A tombstoned
  message answers `reason: "deleted"`, and a stored system or tool turn, which is never
  embedded, answers `reason: "unsupported_role"`. A message whose text reads as high is
  never embedded either: it answers `reason: "high_sensitivity"`, and a new one is not
  stored. One an earlier build saved is never a vector or keyword hit of
  `conversation.search_saved_messages`, and `conversation.list_saved_messages` lists it
  with an empty excerpt (it checks the `conversation_search_excerpt_chars` characters it
  shows and 1,024 past them, as `conversation.threads` does).
  The checks also apply to a message that is already saved.

Save and unsave share a renewable thread lease with append embedding writes,
compaction retirement and thread deletion. Append and compaction finish provider
work before taking the lease.

The transcript row decides who owns a saved message. A save claims only rows that no
user owns or that the caller owns, in the mutation itself, so of two users saving one
unowned message the first claims it and the second is answered `owner_mismatch`. The
embedding row is flagged only after the transcript row reads back as the caller's.

A save can be retried safely. Two concurrent saves of a new message store one transcript
row: its insert carries a deduplication token derived from the message identity. Each
call re-reads the message and repairs what an interrupted or concurrent write left
behind: it writes a missing embedding row, embeds again when the last embed failed, and
flags any row still unflagged. A save whose embed fails stores the message saved and
answers `indexed: false, reason: "embed_failed"`; saving it again retries the embed. A
transcript append whose embedding lands after a concurrent save flags its own embedding
row.

`conversation.list_saved_messages` (viewer) pages through one user's saved messages with
no query, most recently saved first (`limit` up to 200, an opaque `cursor`, and an
optional `conversation_id`). Its `ts` is when the user saved the message, independent
of the transcript's message time. Rows written without a save timestamp use their
message timestamp. It is the first view of a "Saved" panel; the search
narrows it once the user types. `conversation.saved_status` (viewer) takes one
`conversation_id` and up to 500 `message_ids` and answers which of them this user has
saved, so a client draws every message's saved state when it opens a conversation in
one call.

`conversation.unsave_message` (writer) withdraws a saved message from its owner's
search. Where the namespace records transcripts, the turn stays in the transcript with
the flag cleared, because the owner chose to record it. Where it does not, the turn is
removed with a lightweight delete, because it existed only because of the save
(`removed: true`). The embedding keeps a tombstone so older versions cannot reappear.
A message
the caller's user never saved, or one another user saved, answers `found: false`; it
never says which. Unsaving is idempotent, and saving again restores the message. Erasure
by user removes saved messages with the rest of that user's conversation rows.

### Reading and searching

| Operation              | Role   | Does                                                                                                                       |
| ---------------------- | ------ | -------------------------------------------------------------------------------------------------------------------------- |
| `conversation.threads` | viewer | Recent threads of a namespace, newest first (`window_days`, `limit`, `offset` up to 100,000; `agent="*"` for every agent). |
| `conversation.get`     | viewer | One thread's metadata; `include_messages=true` pages its turns (`order="desc"` for the tail).                              |
| `conversation.list`    | viewer | A thread's turns, cursor-paginated, optionally by `run_id`.                                                                |
| `conversation.search`  | viewer | Vector search over embedded turns, optionally by thread, run or role.                                                      |
| `conversation.update`  | writer | Change a thread's `title`, `metadata` or `run_id`.                                                                         |

`conversation.search` ranks turns by cosine similarity, drops any below
`LOOM_CONVERSATION_SEARCH_MIN_SIMILARITY` (0.30) and fans out over the caller's namespaces by default
(`namespace_scope="all_mine"`). When the embedder is unavailable it falls back to a
substring match so the op still answers. Hits show the first
`LOOM_CONVERSATION_SEARCH_EXCERPT_CHARS` characters of a turn (240 by default); read the turn
with `conversation.get` or `conversation.list`. The op is hidden from MCP;
agents get the conversation section of [`loom_search`](./unified-search.md).

`memory.recall` is a different read: it searches the turns an extraction read (kept in
`loom.memory_evidence`), which exist whether or not the conversation plane records. See
[search and ask](./memory/search-and-ask.md).

### Compacting and deleting

- **`conversation.compact`** (writer, needs an LLM provider) summarises the oldest turns
  before the last `keep_recent` (default 10) that fit the summarizer's prompt, writes the
  summary as one embedded `system` turn whose `metadata.compacted_from` lists the
  summarised `msg_id`s, and tombstones the originals and their embeddings:
  `conversation.search` then finds the summary in their place. A turn another agent of the
  thread still holds live with the same text, as a turn search indexes, keeps its
  embedding (see delete below). A window of fewer than `min_to_compact` (default 4) new
  turns is left alone. An earlier summary counts toward neither `min_to_compact` nor
  `messages_summarized`. Summaries at the start of the window fold into the prompt from a
  separate allowance; one that doesn't fit what is left of it stays live, and one a failed
  run left (its turns all still live and in the window, at the first one's `ts`) is
  replaced unread. A summary further into the window takes prompt budget like any turn and
  is replaced with it. A turn that reads as high is left out of the window: it
  stays live and is never sent to the summarizer, so the summary, placed at the window's
  first turn, can come before it in the thread. A turn saved with
  `conversation.save_message` is never summarised or tombstoned either; it stays verbatim
  between the summary and the recent turns. A summary that cannot be embedded compacts
  nothing (`reason: "embed-failed"`), and neither does one that reads as high
  anyway (`reason: "summary-high-sensitivity"`), since it is never embedded. Unlike
  `embed-failed`, that refusal is not worth a retry: the window stays the same while its
  turns are live, so a retry pays for the same summarizer call and most likely gets the
  same summary. A window that holds only saved turns or turns that read as high
  answers `reason: "nothing-to-compact"`.
- **`conversation.delete`** (admin) tombstones one thread's turns, thread row and
  embeddings; `confirm_thread_id` must equal `thread_id`. A turn another agent also
  logged in the thread keeps its embedding while that agent holds a live copy with the
  same text that search indexes (a user or assistant turn, or a summary), since the
  two copies share one embedding row; the row takes that copy's agent, role, run and
  user, so the deleting agent's own search drops it (compaction does the same). A row
  of exactly 240 characters (an earlier build's cut, or a turn of that length) is kept
  only for a copy whose first 2,048 characters match its agent's turn, and is
  tombstoned when no message row is left to check that against. A
  delete also finishes rows of the thread whose own agent no longer holds a live message
  for the turn: one it deleted or compacted (earlier builds left some), or one the
  retention TTL expired. For a
  person's or a namespace's data everywhere, use
  [`erasure.request`](./memory/retention-and-erasure.md).

Tombstoned turns are filtered from every read but `erasure.export`, which returns them
unless asked with `include_tombstoned: false`. They are removed from disk only if the
operator set a table TTL (`LOOM_RETENTION_CONVERSATIONS_DAYS`, 0 = keep forever). That
TTL covers `loom.conversations` only: a turn's embedding row, which keeps up to 2,048
characters of its text, has no TTL. Once its message has expired, a whole-thread
delete in its thread passes it to a live copy of the turn or tombstones it; an erasure
request tombstones it, and removes it only on a `namespace_list` erasure with `purge`.
`erasure.export` does not return embedding rows, so the excerpt of a turn the TTL
expired is erased with the subject but is not in their export.

### Thread titles

The `conversation_titler` worker gives untitled threads (built by appends with no
`conversation.create`) a short LLM-written title, which the dashboard's Conversations
page leads with. It covers namespaces that record conversations, threads active in the
last `LOOM_CONVERSATION_TITLER_WINDOW_HOURS` (168) with at least
`LOOM_CONVERSATION_TITLER_MIN_TURNS` (4) turns, at most
`LOOM_CONVERSATION_TITLER_MAX_THREADS_PER_PASS` (10) per pass. It is on by default
(`LOOM_CONVERSATION_TITLER_ENABLED`) and does nothing with the fake LLM.

## Capturing sessions from clients

| Client             | How turns reach Loom                                                                                                                                                                                                                                                    |
| ------------------ | ----------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| Claude Code plugin | The Stop hook (`hooks/loom-conversation.mjs`) appends each new turn from the session transcript, `msg_id` = the transcript uuid; `LOOM_CAPTURE_CONVERSATIONS=false` turns it off. It caches the namespace's recording verdict and skips appends while recording is off. |
| Codex plugin       | The same capture, see [Codex](./integrations/codex.md).                                                                                                                                                                                                                 |
| Cursor             | `integrations/cursor/cursor-session-hook.sh`, run by Cursor's after-conversation hook, hands the transcript to `loom ingest-transcript`.                                                                                                                                |
| Other agents       | `integrations/generic/ingest.sh` on the exported transcript.                                                                                                                                                                                                            |
| Any MCP client     | The model calls `loom_conversation_append` (in the `core` toolset).                                                                                                                                                                                                     |
| Your own code      | `conversation.append`, or the Python SDK's `Session`, below.                                                                                                                                                                                                            |

`loom ingest-transcript --namespace NS --file session.md` reads a Markdown file with
alternating `# user` / `# assistant` sections, or a JSON array of `{role, content}`,
and appends each turn (`--dry-run` counts them; `--thread-id` sets a stable id).

The plugins also hand the session's turns to `memory.set_from_messages(defer=true)` at
session end, so facts are extracted whether or not the transcript is recorded, and
record tool actions as episodic observations through `observe.record` /
`observe.crystallize` (see [extraction](./memory/extraction.md)). The hook-by-hook
wiring is in [Claude Code](./integrations/claude-code.md).

## Session briefs

A session brief is the resumable state of one line of work: goal, status, decisions, dead ends, what is done, open
questions, next steps, key files, constraints and pointers. It is read after a compaction, the next day, in another
client or by a teammate. It is never embedded or returned by `memory.search`. Any agent that can read the namespace
can resume it.

| Operation              | Role   | Does                                                                                                                   |
| ---------------------- | ------ | ---------------------------------------------------------------------------------------------------------------------- |
| `session.brief_save`   | writer | Revise (or start) a brief from `events` (what happened since the last save), from `sections` the agent wrote, or both. |
| `session.brief_get`    | viewer | Load a brief by `brief_key`, else the newest one a session wrote (`from_session`), else the newest for a `project`.    |
| `session.brief_list`   | viewer | The namespace's briefs, newest first, one line each (`mine_only` for the caller's).                                    |
| `session.brief_delete` | writer | Delete a brief when its writer asks for it to be forgotten.                                                            |

**Addressing.** An empty `brief_key` becomes `auto/<project>/<branch>`, so every session
on the same line of work revises the same brief, or `session/<session_id>` when neither
is known. Keys match `^[A-Za-z0-9][A-Za-z0-9_.:/@+-]{0,159}$`. A token bounded to one
user (or session) stores its briefs as `~<user>~<key>` (or `~<user>~<session>~<key>`) and
sees them as `<key>`; a namespace-wide token lists them with that prefix and can get or
delete them by it. A bounded token can't send a `~` key.

**Revising.** A save with `events` (kinds `user`, `assistant`, `edit`, `command`,
`error`, `read`, `search`, `tool`, `note`, `summary`; up to 4,000 per call) is condensed
server-side. With `LOOM_SESSION_BRIEF_LLM` on (default) the model named by
`LOOM_SESSION_BRIEF_LLM_MODEL` revises the brief once per window of
`LOOM_SESSION_BRIEF_WINDOW_CHARS` characters, at most `LOOM_SESSION_BRIEF_MAX_WINDOWS`
windows per save; without that model's key the deployment's LLM writes it, and with no
LLM the deterministic builder writes goal, status, files, errors and pointers (no
decisions or dead ends). `author` in the response says `llm`, `deterministic` or
`model` (the calling agent wrote the sections). Under an operator's
[extraction policy](memory/extraction.md#operator-extraction-policy) the builder never
runs and no client summary is taken from the events. A window whose model call failed
is skipped while the rest are revised. A save of events alone with no model to call (none
configured, `use_llm: false`, or the org's model calls switched off before its first
window was revised) writes nothing and answers `changed: false`, `reason: "not-revised"`;
an org switched off after some windows were revised keeps those windows and revises no
more. A save of events alone whose every model call failed, was held by an unread switch
or did not parse is refused with 503 for the client to send again. A save that also
carries the agent's `sections` writes them over the brief as it was in both cases, and
takes nothing from the events.
State sections are replaced on each revision; decisions, dead ends and done items
accumulate. Every revision is kept in `loom.session_brief_revisions`; `revision=N` on
`brief_get` reads one. A save that repeats the previous save's session and
`event_cursor` is a retry and writes nothing; `if_revision` makes a save
compare-and-set (409 on a mismatch).

**Restoring is the person's choice.** The coding plugins save briefs at compaction and
session end with zero model tokens on the client, and the session-start primer only
names the saved briefs. Loading one happens when the person asks, through the
`loom-session` skill or the briefs section of the dashboard's Conversations page.
`brief_get(full=true)` appends the client's own last compaction summary verbatim below
the brief. Briefs never load on their own: only the person knows which line of work they are resuming.

## Example

The Python SDK's `Session` binds these ops to one `(user, session)` scope: `append`
writes turns to the transcript and, every `flush_every` turns, to
`memory.set_from_messages`; `resume` is `context.pack` for the scope plus the transcript
tail.

```python
from loom import Agent

async with Agent("dev", "local", "support-bot", base_url="http://localhost:7777") as bot:
    s = bot.session("thread-42", user_id="u1")
    resumed = await s.resume(query="resume the billing migration")
    await s.append(
        {"role": "user", "content": "We moved invoices to the new ledger."},
        {"role": "assistant", "content": "Noted; I'll update the export job."},
    )
    print(s.transcript_stored)  # False when the namespace does not record
    await s.close()  # flushes the buffered turns to extraction

    await bot.briefs.save(
        project="billing",
        branch="ledger-move",
        sections={
            "goal": "Move invoices to the new ledger",
            "next_steps": ["Update the export job"],
        },
    )
```

Extraction on `close()` needs a real LLM provider to find facts; with the stock fake
provider the turns are still recorded.

## Settings

| Setting                                                                     | Effect                                                                                                                                                                    |
| --------------------------------------------------------------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `LOOM_CONVERSATION_SEARCH_MIN_SIMILARITY`                                   | The cosine floor of `conversation.search` ([Knowledge → Conversations](../reference/configuration.md#conversations)).                                                     |
| `LOOM_CONVERSATION_SEARCH_EXCERPT_CHARS`                                    | How much of a turn a search hit or a saved-message listing shows, live ([Knowledge → Conversations](../reference/configuration.md#conversations)).                        |
| `LOOM_CONVERSATION_TITLER_*`                                                | The titler worker's switch, cadence, window and per-pass cap ([Knowledge → Conversations](../reference/configuration.md#conversations)).                                  |
| `LOOM_RETENTION_CONVERSATIONS_DAYS`, `LOOM_TIERING_CONVERSATIONS_WARM_DAYS` | Opt-in TTL and warm-volume move for transcripts ([Retention](../reference/configuration.md#retention), [storage tiering](../reference/configuration.md#storage-tiering)). |
| `LOOM_SESSION_BRIEF_LLM`, `…_LLM_MODEL`, `…_WINDOW_CHARS`, `…_MAX_WINDOWS`  | How briefs are condensed ([Context](../reference/configuration.md#context)).                                                                                              |
| `LOOM_MCP_SESSION_RUN_IDLE_SECONDS`                                         | Closes an MCP session's implicit run after this long without a call ([Mcp](../reference/configuration.md#mcp)).                                                           |

## Limits

- **"I appended but nothing is there."** Recording is off for the namespace; the append
  answered `stored: false`. Turn it on with `conversation.set_logging`.
- **Transcripts are not mined.** Recorded turns never become memories on their own.
- **`conversation.search` is vector-first.** Exact wording that the embedding does not
  capture may rank low; `memory.recall` does lexical and time-window reads over the
  turns extraction read.
- **Briefs are bounded.** A rendered brief stays under about 12,000 characters; each
  list section has a cap (for example 20 decisions, 12 dead ends, 8 next steps), so old
  items fall off.
- **A brief is not a lock.** Two sessions on the same branch revise the same brief;
  pass `if_revision` when that must not interleave.

## Related

- [Extraction](./memory/extraction.md) — turning a transcript into memories.
- [Working memory and profiles](./memory/working-memory-and-profiles.md) — the "where
  was I" scratchpad block the plugins also write.
- [Runs](./runs.md) — the run ledger a session's writes and observations join.
- [Unified search](./unified-search.md) — the conversation section of `loom_search`.
- [Trace and resume](../guides/trace-and-resume.md) — resuming a long run.
- [Memory architecture](../architecture/memory-architecture.md) — the conversation and
  brief tables from the inside.
