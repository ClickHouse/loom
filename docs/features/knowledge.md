# Knowledge documents

A knowledge document is text an agent looks things up in: a runbook, a specification, a README. It is stored whole
in `loom.documents`, split into sections and windows in `loom.chunks`, and each chunk is embedded for search. A memory
is one claim under a key that Loom judges against its peers; a document is searched, outlined and cited by passage.
Put the document in knowledge and a short claim pointing at it in memory. A memory value over a couple of kilobytes
belongs here ([writing memories](./memory/writing.md)).

## What Loom does with a document

1. **Stores the text and, optionally, the original file.** `knowledge.add` takes
   `content` as a string plus a `source_uri`, which is the document's stable identity
   and the thing an agent cites. `original_bytes_b64` (with `original_filename` and
   `blob_content_type`) keeps the uploaded file beside the extracted text: a catalog row
   in `loom.document_blobs` addressed by its SHA-256, with the bytes in that row or in a
   bucket ([where originals are stored](#where-originals-are-stored)), so a person can
   download exactly what they uploaded.
2. **Chunks it on its structure.** The text is split on its headings and windowed
   inside each section (`LOOM_KNOWLEDGE_CHUNK_SIZE` characters, stepping by
   `LOOM_KNOWLEDGE_CHUNK_STRIDE`, so consecutive windows overlap). A document with no
   headings is windowed over its whole text. Headings follow CommonMark's block
   rules: `#` lines and a line over an underline of three or more `=` or `-` (a
   shorter one is text here), plus one-line `<h1>` to `<h6>`; a list item, a
   thematic break and any line inside a fenced code block or an HTML block are
   never headings.
3. **Indexes each chunk with its context.** A chunk's context is the title, the
   `description` and its heading breadcrumb ("Deploy > Rollback > Cloud"), stored
   with the chunk. The vector is computed over the context prepended to the chunk
   text, and the lexical leg scores the context's words with the text's. The stored
   chunk text stays unchanged, so snippets read naturally, while both legs know where
   the passage sits: a "Refunds" section of the "Lantern handbook" matches a query
   naming Lantern, though the section never does.
4. **Serves it through search, outline and whole-document reads**, described below.

A fixed window can cut a procedure in half and embed the second half without its heading. Chunking on headings keeps
a section the unit of retrieval, and the breadcrumb tells a hit where it came from. Chunk sizes are uneven as a result.

## Operations

All operations are in the [knowledge group](../reference/operations.md#knowledge) of
the operations reference.

| Operation                   | Role   | Does                                                                                   |
| --------------------------- | ------ | -------------------------------------------------------------------------------------- |
| `knowledge.add`             | writer | Store one document and its chunks.                                                     |
| `knowledge.bulk_add`        | writer | Store up to 500 documents in one call; the same per-document behaviour as `add`.       |
| `knowledge.upsert`          | writer | Add or replace the document at a `source_uri`; earlier chunks are superseded.          |
| `knowledge.search`          | viewer | Hybrid passage search (vector, BM25 and exact keys fused).                             |
| `knowledge.get`             | viewer | One document's full text and metadata by `source_uri`.                                 |
| `knowledge.list`            | viewer | Documents of a namespace, paged by an opaque cursor, filterable by tag and URI prefix. |
| `knowledge.outline`         | viewer | A document's table of contents, and optionally one section's assembled text.           |
| `knowledge.synthesize`      | viewer | Complete a task over whole documents (summaries, spread-out evidence) with the LLM.    |
| `knowledge.update_metadata` | writer | Edit `title`, `description` or `tags`; `reindex=true` rewrites the chunks' context.    |
| `knowledge.delete`          | writer | Supersede a document and its chunks.                                                   |
| `knowledge.bulk_delete`     | writer | The same for up to 500 `source_uris`.                                                  |
| `knowledge.reads`           | viewer | How often agents opened each document over the last `days` (1–30).                     |
| `knowledge.inspect`         | viewer | A report on the namespace's documents ([below](#inspecting-a-namespaces-documents)).   |
| `knowledge.dedup_list`      | admin  | Documents (and, optionally, chunks) of the namespace with identical content hashes.    |
| `knowledge.dedup_resolve`   | admin  | Supersede one side of a duplicate pair.                                                |
| `knowledge.ingest`          | writer | Extract an uploaded file and publish it as a document revision (a job).                |
| `knowledge.ingest_status`   | viewer | A job's state, stage, coverage, warnings, failure and published revision.              |
| `knowledge.ingest_cancel`   | writer | Stop a job before it publishes.                                                        |

`knowledge.search` and `knowledge.reads` are hidden from MCP: neither is listed, and
`loom_op` refuses them too. An agent reaches the knowledge plane through
[`loom_search`](./unified-search.md), whose knowledge section is a `knowledge.search`;
both ops are callable over HTTP and the SDKs. A document's original file is served by `GET /v1/knowledge.download`
(`org`, `namespace`, `source_uri`, optional `inline`), a plain URL rather than a JSON
op so a browser download works; a document with no stored original downloads as
Markdown built from its text, and the `X-Loom-Download-Source` header says `blob` or
`text`.

## Document sources

A repository's maintainers register the document sources Loom pulls the repository's
documents from. A source lives on the repository's trunk code namespace, the one its
documents live in, so whoever reads the repository sees where its documents come from,
and only the namespace's admins change that. Four kinds are served: `gdrive`, a shared
folder of docs, slides, decks and PDFs, read with a service account the folders are
shared to read-only, whose credential is the account's JSON key; `linear`, the
documents of a project tracker's projects, read with a read-only API key of an account
that sees the projects; `notion`, a page store's pages and databases, read with an
integration that has read content access and that the pages and databases are shared
with, whose credential is the integration's token; and `recording`, the transcripts of
a meeting store's recordings ([recordings](#a-recording-source)). The
`knowledge_source_sync` worker pulls each source's files into the repository's
documents ([syncing](#syncing-a-source)); `list` says whether this deployment runs it
(`enabled`, `LOOM_KNOWLEDGE_SOURCE_SYNC_ENABLED`, off by default).

On the dashboard, an opened repository on the Repositories page shows its sources under
Docs: each source's kind, last sync and counts, to anyone who reads the repository. The
namespace's admins also get Add a source (a dry-run check first; Add sends only the form
that passed), Check, Sync now, Rotate credential and Retire. The page adds `gdrive`
sources; a `linear` or `recording` source is added with `knowledge.source.add` and then
shows and is managed there like any other. The key is typed into a password field and is never shown
again.

| Operation                                                                                   | Role   | Does                                                                                                       |
| ------------------------------------------------------------------------------------------- | ------ | ---------------------------------------------------------------------------------------------------------- |
| [`knowledge.source.list`](../reference/operations/knowledge.md#knowledgesourcelist)         | viewer | The namespace's sources, each with its config and last sync request.                                       |
| [`knowledge.source.inspect`](../reference/operations/knowledge.md#knowledgesourceinspect)   | viewer | One source, its last sync request and the 200 documents the sync worker most recently wrote for it.        |
| [`knowledge.source.add`](../reference/operations/knowledge.md#knowledgesourceadd)           | admin  | Probe the credential, then store the source; `dry_run` only probes.                                        |
| [`knowledge.source.check`](../reference/operations/knowledge.md#knowledgesourcecheck)       | admin  | Probe a stored source with its own credential now; nothing is written.                                     |
| [`knowledge.source.rotate`](../reference/operations/knowledge.md#knowledgesourcerotate)     | admin  | Probe a new credential, then replace the stored one.                                                       |
| [`knowledge.source.remove`](../reference/operations/knowledge.md#knowledgesourceremove)     | admin  | Retire a source (`confirm` equals the name); `delete_documents` records a request to retire its documents. |
| [`knowledge.source.sync_now`](../reference/operations/knowledge.md#knowledgesourcesync_now) | admin  | Record a request for a sync pass over this source (`full`: list every document).                           |

A source's `config` holds ids and filters, never a host or a URL: `gdrive` takes
`folder_ids` (1 to 20) and `include` (any of `docs`, `slides`, `pdf`, `pptx`; all by
default); `linear` takes `project_ids` (1 to 20 project UUIDs); `notion` takes
`page_ids` and `database_ids`, 1 to 20 ids in all, each 32 hex digits with or without
dashes (stored dashed, lower case); `recording` takes `provider` (`meet` or `zoom`) and
`ids` (1 to 20 meeting space ids for `meet`, numeric meeting ids for `zoom`). The
store's API hosts are fixed in the server, so a registered source cannot point the
server at another host. The credential is sealed at rest under its own purpose
(`loom secrets reseal` covers it), held in memory only for a probe, never returned by
any op and never written to an audit row. `add` records the namespace, name, kind,
config and `dry_run`; `rotate` the namespace and name; `remove` and `sync_now` their
input (namespace, name, `confirm` and `delete_documents`, or `full`). `check` is
admin-only because its probe spends the source's rate limit.

A `gdrive` probe exchanges the key for a read-only token, checks every folder together
(one call each) and lists the included files of all folders in one call, the first 1000
at most. A `linear` probe reads the first page (50 documents) of every project together,
so a project the key's account cannot see refuses the probe. A `notion` probe reads each
listed page and database in turn (one in the trash refuses) and counts the listed pages
and the first 100 pages of each of up to three databases (read from each database's
first data source), so a probe of 20 ids stays well inside its bound at the store's
pace; `more` says the count leaves pages out. A `recording` probe exchanges its
credential, reads each id and counts the recordings it lists. Each is bounded as a whole
by `LOOM_KNOWLEDGE_SOURCE_PROBE_TIMEOUT_S` (default 20 s); past it `add` and `rotate`
answer `SOURCE_UNREACHABLE` and `check` answers `reachable: false`.

Every write of a namespace's sources (`add`, `rotate`, `remove`, `sync_now`, and the
retirement in `code.repo.remove`) is serialized by a lease, so a write's checks still
hold when it writes; a probe runs outside it. A write that finds the lease held past
a short wait answers `OVERLOADED` and can be retried; one whose lease store cannot
answer is refused, never run unlocked. The one writer outside the lease is
`loom secrets reseal`, which writes a row it read again at that row's version + 1, so
any later write wins over it. Versions come from the ClickHouse servers' clocks, so
writes stay ordered while those clocks differ by less than the time between them.

A removed source's row stays, inactive and with its credential cleared. With
`delete_documents`, the row also carries the request for the sync worker, even when no
document is counted yet (a sync turn running at that moment may still write one); the
worker clears it once none is left. Removing a repository (`code.repo.remove`) retires
the sources registered on its namespace the same way and names them in
`sources_deactivated`; the purge of the repository's code index that follows deletes
their sync state (it lives in `loom.code_index_state`) and the namespace's documents,
and leaves the inactive source rows. A source can still outlive its repository's trunk
namespace (a repository retired by editing the deployment's configuration, a branch that
now outranks the trunk, or a repository removed by a replica of the previous release,
which does not retire sources): its row stays active, keeps its sealed credential and
counts toward the namespace's quota until `knowledge.source.remove` retires it. `check`,
`rotate` and `sync_now` refuse it with `NOT_A_CODE_NAMESPACE`; `list`, `inspect` and
`remove` still serve it, and the sync worker skips it.

A source's documents are the live documents of its namespace that carry the tag
`source:<name>` and that the sync worker wrote (actor `worker:knowledge-source-sync`).
No token maps to that actor, so a document a caller tags `source:<name>` is never a
source's: it is not counted, listed or retired, and it never holds a name. The
`source:*` tags of a document the worker wrote belong to the worker: a tag edit
(`knowledge.update_metadata`) or a re-save of the same text (`knowledge.add`, `upsert`,
`bulk_add`) by anyone else, which keeps the worker as the document's author, may change
its other tags but not add or drop a `source:*` one (`SOURCE_TAGS_LOCKED`). A caller
tags its own documents freely.

A name is free for `add` only while no document of an earlier source of that name is
live, since a new source of the name would take them over. A removed source whose
documents wait to be retired refuses its name with `SOURCE_DELETE_PENDING` until they
are gone; one whose documents were kept refuses it with `SOURCE_DOCUMENTS_KEPT` until
they are deleted (`knowledge.delete`). A removed source with no documents frees its
name at once.

| Code                    | When                                                                                                                                                                                                                         |
| ----------------------- | ---------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `NOT_A_CODE_NAMESPACE`  | `add`, `check`, `rotate` or `sync_now` names a namespace that is not a repository's trunk code namespace.                                                                                                                    |
| `BAD_NAME`              | The name is not 2-32 lower-case letters, digits, `-` or `_`, starting with a letter.                                                                                                                                         |
| `BAD_KIND`              | The kind is not one the server serves.                                                                                                                                                                                       |
| `BAD_CONFIG`            | The config has an unknown key, a malformed folder, project, page, database, space or meeting id, an unknown `include` value or an unknown `provider`, or a `notion` config with no id or more than 20.                       |
| `SOURCE_EXISTS`         | The namespace has an active source of that name.                                                                                                                                                                             |
| `SOURCE_QUOTA`          | The namespace has `LOOM_KNOWLEDGE_SOURCES_MAX_PER_NAMESPACE` active sources (default 8).                                                                                                                                     |
| `NO_ROOT_SECRET`        | The deployment has no `LOOM_JWT_SECRET`, so a credential cannot be stored.                                                                                                                                                   |
| `CREDENTIAL_REFUSED`    | The credential is not of the kind's form, or the store refused it; nothing is stored.                                                                                                                                        |
| `SOURCE_UNREACHABLE`    | A folder, project, page, database, space or meeting is not visible to the account or integration, a page or database is in the trash, the store failed or rate-limited the probe, or the probe timed out; nothing is stored. |
| `SOURCE_DELETE_PENDING` | `add` names a removed source whose documents are still waiting to be retired.                                                                                                                                                |
| `SOURCE_DOCUMENTS_KEPT` | `add` names a removed source whose kept documents are still live; delete them first.                                                                                                                                         |
| `SOURCE_REPLACED`       | `rotate` or `sync_now`'s source was removed and added again during the call (409); act on the current source.                                                                                                                |
| `SOURCE_TAGS_LOCKED`    | A write that keeps the sync worker as a document's author changes its `source:*` tags.                                                                                                                                       |
| `SOURCE_NOT_FOUND`      | No active source of that name in the namespace (404).                                                                                                                                                                        |
| `CONFIRM_REQUIRED`      | `remove`'s `confirm` is not the source's name.                                                                                                                                                                               |

A probe's error names the HTTP status (`http_403`), never the store's response text.

### Syncing a source

Every `LOOM_KNOWLEDGE_SOURCE_SYNC_INTERVAL_SECONDS` (default 900) the worker first
retires the documents of removed sources a maintainer asked to delete, then gives each
active source a turn: sources a maintainer asked for with `sync_now` first, then the
rest from where the previous pass stopped. A source whose namespace is inactive or no
longer a repository's trunk is skipped. Each turn holds a lease of its own, so two
copies of the worker never sync one source at once.

A turn lists the source's files. Its first turn, a turn `sync_now(full=true)` asked for
and the first turn after `LOOM_KNOWLEDGE_SOURCE_SYNC_FULL_INTERVAL_SECONDS` (default a
day) since the last full listing list every file; the others list only the files changed
after the stored stamp: the newest modification time a complete turn saw, but never
later than a minute before that turn's listing started. A file added to a folder with an
older modification time (moved or copied in) is not changed after the stamp; it arrives
with the next full listing. A listed file whose stored copy has the same revision, and
whose chunks are live, is skipped without a fetch, so a write cut between its document
and its chunks is redone. A changed file is fetched and written with `knowledge.upsert`:

| File                  | Text                                                                                                                                                                                                                                                                                                                                                                                                      | Original                       |
| --------------------- | --------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- | ------------------------------ |
| A document (`docs`)   | Exported by the store as Markdown.                                                                                                                                                                                                                                                                                                                                                                        | none                           |
| A deck (`slides`)     | One `# Slide N: <title>` section per slide, so a chunk is a slide: its shapes' text (groups read through, a table one row per line), then `> Notes: …` from the speaker notes. A run of eight or more bare numbers is dropped. A slide whose text is under 120 characters beside a picture, chart or video ends with `> [image-only: N pictures, rationale not extracted]`.                               | none                           |
| A PDF, a `.pptx` file | Sent to the [extractor service](../architecture/document-extraction.md) its format is routed to; the artifact's headings become Markdown headings and its code and SQL blocks are fenced, and how much of the file it read goes in the metadata (`extraction_coverage`). With no extractor configured for the format, or one that does not declare it, the file is counted `unconverted` and not written. | the file's bytes, downloadable |

Each document's address (`source_uri`) is the link the store reports for the file,
without its query, so a `knowledge.synthesize` citation of it reads `<link>#<chunk>`.
Its title is the file's name, its description the first paragraph, its tags
`source-kind:design_doc`, the repository's `repo:<id>` and `source:<name>`, and its
metadata the source's kind and name, the file's id (`ingested_from`), its revision
(`source_revision`) and the conversion (`ingested_format`). Every document a sync writes
has the actor `worker:knowledge-source-sync` and the class `imported`. A live document
at the same address that another writer or another source holds is never overwritten or
retired; the file is counted `skipped` and nothing is fetched for it. The source is read
again before each write: one removed or replaced during its turn stops the turn, which
records nothing.

A document is retired only on positive evidence: its file is missing from a complete
full listing and the store, asked about that file, reports it gone, trashed or in none
of the source's folders (a listing paged while files change can miss one); its document
sits at an address that is no longer its file's; or a maintainer asked with
`remove(delete_documents=true)`. A listed file the store gives no https link for is not
written (`no_address`) and counts as missing, so its stored document stays while the
lookup still finds the file. A listing the store marks incomplete (`incompleteSearch`)
has its files written, but keeps the stamp, retires nothing and reports
`listing: incomplete search`. A folder the account no longer sees lists empty rather
than refused, so a full turn first checks every folder; while one fails, the turn ends
`failed`, still writes the files of the folders it sees, and retires nothing. A fetch
error or a rate limit retires nothing. Each retirement is a `knowledge.delete` with its
audit row, actor `worker:knowledge-source-sync`. Once a removed source has no documents
left, its delete request is cleared.

The stamp moves only when every listed file was handled. A turn cut short (a rate limit,
a failure, the pass bound) is listed again on the next pass, and the files it wrote are
skipped by revision. One pass fetches, writes, retires and mines at most
`LOOM_KNOWLEDGE_SOURCE_SYNC_DOCUMENTS_PER_PASS` documents (default 40) across all
sources, so a large folder's first sync spans several passes.

What fails for every file is the turn's: the token exchange, the listing, a `401` on any
request (`credential_invalid`), a rate limit, and an extractor whose capabilities cannot
be read. Such a turn keeps the stamp, so the next pass lists the same files again.
Trouble with one file is that file's:

- A file the store refuses on its own (a `403` or `404` on its export, download or deck
  read: an export over the store's limit, downloads turned off), one over
  `LOOM_KNOWLEDGE_SOURCE_SYNC_MAX_FILE_BYTES` (default 64 MiB), one the extractor
  refuses and one the write refuses are counted `unconverted` with their reason, and the
  turn goes on. Such a file is given up on at once, with the same retries as a file
  given up below, so it costs no fetch and none of the pass bound until then.
- Any other failure on one file (a `5xx` or a timeout on it, its extractor call, its
  conversion, its write) makes the file wait: the turn ends `failed` with the stamp
  kept, and the next pass tries the file again. After three passes on the same revision
  the file is counted `unconverted` instead and is given up on: it is tried again when
  it changes, once on a periodic full listing after each completed one (one more failure
  gives it up again), and on a full sync request (`sync_now(full=true)` or
  `loom knowledge sync --full`), which resets the waits once per request.

A `notion` source's turn follows the same rules, with pages for files. Its listing is
each listed page, the pages of each listed database, read through the database's data
sources (on an incremental turn, only the pages edited after the stamp), and each known
child page. Within each server process, the requests of one integration are paced to the
store's average of three a second; two integrations do not wait on each other, and a
rate limit (`429`) ends the turn, which the next pass retries. A page's revision is its
last edit time and its address the page URL the store reports, so a renamed page moves
to its new address and the document at the old one is retired. A page whose stored copy
has the listed revision is skipped only when that copy was read 90 seconds or more after
the revision; one read sooner is read and written again with its new read time, so an
edit made just before or after a read is not lost to an equal stamp or a clock skew, and
the page is skipped once its stored copy was read past the margin. The stamp trails the
listing by 150 seconds, the minute above plus the 90-second margin, so a database page
read within the margin is listed again by the next incremental turn. The rewrite embeds
the page again (the writer embeds every write), so a page read within 90 seconds of its
revision costs one more embedding, once in practice, since the next pass reads it past
the margin. A turn handles the pages that changed (no stored copy, another revision or
another address) before such re-reads, so a pass bound spent on them delays no edit. A
changed page's blocks are read, nested blocks included, at most 300 requests a page, and
turned into Markdown one to one: headings, paragraphs, bulleted and numbered lists,
to-dos (`- [x]`), quotes and callouts as `>` quotes, code fenced with its language,
tables as Markdown tables (the first row as the header row), equations as `$$` blocks,
dividers as `---`, bookmarks, embeds and link previews as links, toggles, columns and
synced blocks with their children inline, and a run of images or videos as
`> [image-only: N pictures, rationale not extracted]` followed by their captions. Left
out and counted by type in the document's metadata (`skipped_blocks`): a child database,
a file, a PDF, a link that is not an https address, a synced copy whose original is not
shared with the integration, and any type the converter does not map. `ingested_format`
is `blocks`, `read_at` is when the page was read, and `text_sha256` is an exact hash of
its title, Markdown and left-out blocks. The [mining](#mining-decisions) step keys a
page by its revision and that hash, so a copy written again with the same text is not
mined again, and one with other text at the same revision is.

A child page is a document of its own. The child pages a read page holds are kept in the
source's sync state, at most 200 per source (more are counted in their parent's
`skipped_blocks`), and each later turn reads every known child once for its revision; a
child found while a turn syncs its parent, and a grandchild found while it syncs the
child, are synced in that turn. A child page inside a column, a toggle, a list item or a
synced block belongs to the page whose blocks held it. A known child is forgotten in
three ways only: its page is read and no longer holds it; a complete full turn's lookup
finds it gone, in the trash or out from under the source (and retires its document); or
a complete full turn read it as not found or trashed and it holds no document. A failed
or cut turn forgets no child it could not read, and a child found while syncing whose
read fails (other than not found) leaves the turn failed, so it is read again next turn.
A page with no text (`no_text`) and one past the request bound or
`LOOM_KNOWLEDGE_SOURCE_SYNC_MAX_FILE_BYTES` (`too_large`) are given up on until they
change. A listed page or database the integration no longer sees, or a database in the
trash, fails the turn and retires nothing, while the pages it still sees are written. A
listed page or child page in the trash is left out of the listing, so the next full turn
retires it. A page is retired after a complete full listing lacks it and a lookup finds
it gone, in the trash, or under none of the listed pages, the listed databases and the
pages that listing had; a child moved out from under them, and its own children, are
retired this way. A database query the store marks incomplete is an incomplete listing
(`listing: incomplete result`).

### Mining decisions

After every source's turn, the same pass mines for decisions each of the source's
documents whose stored revision is not the one last mined for it, oldest first, with
the extraction `loom decisions extract --apply` runs
([extract decisions from a document](tasks-and-decisions.md#extract-decisions-from-a-document)).
The revision a document was mined at is recorded only once its extraction is done, so a
document written by a turn that failed or was stopped is mined on a later pass, as is
one synced while no extraction ran. The rows
go to the source's namespace, the repository's trunk code namespace, each citing its
document in `source_resource_id`, written through `decision.record` as agent
`loom-decisions` with the actor `worker:knowledge-source-sync` and the audit row the
transport would write.

Nobody has to confirm a row: an unpinned extracted row is served as it is, and a later
write of the document revises or retires the extractor's own rows. A pinned or attested
row, or one with a recorded outcome, is never rewritten: it is tagged `source-changed`
and counted `flagged_pinned`, and `loom decisions extract` without `--apply` prints the
body a maintainer may record instead.

The model calls bill the source's namespace (operation `worker.knowledge_source_sync`
in `loom.agent_events`). Before each document the pass reads the namespace's spend: one
whose spend on these calls in the last 24 hours reached
`LOOM_KNOWLEDGE_SOURCE_SYNC_CAP_SHARE` (default 0.25) of its daily cost cap, or the whole
cap, is left for a later pass; a cap that cannot be read counts as reached. An org whose
model calls are switched off, a namespace whose calls the inference gateway refuses or
asks to wait, for an embedding too (left for the time the gateway names), and every org
while the switch cannot be read are left the same way, with no try counted. A deployment
with no usable model mines nothing. A source left for a later pass shows why in
`extract_error`: `over_cap`, `switched_off`, `switch_unread`, `no_model`, `held`,
`held:<code>` or `pass_bound` (the pass had no time or document left for it).

Each decision's writes run whole with their audit row: a pass cancelled mid-write
finishes the write it is in, and shutdown waits for it. Before each write the run checks
that its pass is not stopped and that it still holds the source's lease; when either
ends, it stops there, keeps what it wrote, retires nothing, and the document is mined
again on a later pass.

A document whose extraction fails stays pending for three tries on the same revision,
then is counted `unextracted` and left until the sync writes it again. The reasons:
`not_run` (it could not run on the document, such as one with no chunks for the current
embedding model), `refused` (a read or a write it needed was refused for a reason of the
moment, such as a busy key, or failed) and `failed:<error>`. A write refused for a
reason every run would meet (a blacklisted decision, a pinned peer it contradicts, a
body the op does not take) is not a failure: the document is mined with its other
decisions. A document's model reads stop 30 seconds before the pass's deadline, with
nothing written (`out_of_time`), and it goes first among its source's documents on the
next pass; one that runs out of time with three quarters of a pass's read time or more
is given up on at once (`too_large_for_pass`): the worker does not mine a document that
large, and `loom decisions extract` runs it with no deadline. A document is not started
with less than a minute of read time left. A document retired, or held by another writer
since, is not mined.

A source's `sync` in `list` and `inspect` shows what its last turn did:

| Field                    | Meaning                                                                                                                                                                                                                                                                                                                                                                                                                                                                                       |
| ------------------------ | --------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `requested_at`           | A `sync_now` request no turn has finished yet.                                                                                                                                                                                                                                                                                                                                                                                                                                                |
| `last_synced_at`         | When the last turn ended.                                                                                                                                                                                                                                                                                                                                                                                                                                                                     |
| `status`                 | `ok`, `rate_limited` (the next pass retries), `credential_invalid` (the stored key no longer opens or is refused: rotate it) or `failed`.                                                                                                                                                                                                                                                                                                                                                     |
| `documents_seen`         | Files (or a project's documents, pages, or recordings) the turn listed.                                                                                                                                                                                                                                                                                                                                                                                                                       |
| `changed`, `removed`     | Documents it wrote and retired.                                                                                                                                                                                                                                                                                                                                                                                                                                                               |
| `unconverted`, `skipped` | Files it did not write: the store gave no https link, no extractor, refused or too large, a recording with no transcript yet, or given up after three passes on the same revision; another writer's document at the address.                                                                                                                                                                                                                                                                  |
| `unconverted_reasons`    | Why, by count: `no_address` (the store gave no https link), `no_extractor`, `too_large`, `http_<status>`, `unreachable`, `no_text`, `extraction_failed:<code>`, `extractor_unavailable`, `fetch_failed:<error>`, `write_refused:<code>`, `write_failed:<error>`, `no_transcript` (a recording with no finished transcript), `transcript_off_host`; for a project's documents also `not_found`, `graphql_<type>` and `no_document` or `no_data` (the answer lacked the document, or any data). |
| `error`                  | What failed and its HTTP status (`listing: http_500`), never the store's response text; for a turn that did not fail, why it is incomplete (`pass bound reached`, `listing: incomplete search`).                                                                                                                                                                                                                                                                                              |
| `pending_extract`        | Documents whose stored revision is not yet mined for decisions, as of the last pass.                                                                                                                                                                                                                                                                                                                                                                                                          |
| `last_extracted_at`      | When the last mining turn ended.                                                                                                                                                                                                                                                                                                                                                                                                                                                              |
| `extracted`              | Documents that turn mined, each with or without a decision.                                                                                                                                                                                                                                                                                                                                                                                                                                   |
| `flagged_pinned`         | Decision rows it tagged `source-changed` rather than rewriting them: pinned, attested, or with a recorded outcome.                                                                                                                                                                                                                                                                                                                                                                            |
| `unextracted`            | Documents it gave up on: three failed tries, or too large for a pass.                                                                                                                                                                                                                                                                                                                                                                                                                         |
| `extract_error`          | Why an extraction failed in the last mining turn (`not_run`, `refused`, `out_of_time`, `too_large_for_pass`, `failed:<error>`), or why documents wait (`over_cap`, `switched_off`, `switch_unread`, `no_model`, `held`, `held:<code>`, `pass_bound`).                                                                                                                                                                                                                                         |

### A recording source

A `recording` source reads transcripts, never audio or video, and nothing in Loom
transcribes. Neither store's listing says whether a recording has a transcript, so each
recording not stored yet costs one check. A recording without a finished transcript is
counted `unconverted` under `no_transcript`, not as an error: until a day after the
meeting it is checked again whenever a listing has it, at none of the pass bound, so
its transcript is written once the store has one; after that it is given up on, like a
refused file, and checked once more on each periodic full listing and on a full sync
request. A transcript whose download link is not on the store's own hosts is given up
on under `transcript_off_host`, and the token is never sent there.

| `provider` | `ids`               | Credential                                                                                                                                                                   | A recording                                                                                                                         |
| ---------- | ------------------- | ---------------------------------------------------------------------------------------------------------------------------------------------------------------------------- | ----------------------------------------------------------------------------------------------------------------------------------- |
| `meet`     | meeting space ids   | An OAuth `authorized_user` JSON file (`client_id`, `client_secret`, `refresh_token`) of a user who can see the spaces, granted the `meetings.space.readonly` scope only.     | One ended conference record of a space; its transcripts' entries, once every transcript's file is generated (`FILE_GENERATED`).     |
| `zoom`     | numeric meeting ids | A server-to-server app's JSON (`account_id`, `client_id`, `client_secret`), with the read-only meeting and recording scopes only (`meeting:read…`, `cloud_recording:read…`). | One ended instance of a meeting; the `TRANSCRIPT` file (WebVTT) of its cloud recording, downloaded from the store's own hosts only. |

The transcript becomes one `# mm:ss–mm:ss: <first words>` section per five minutes of
the meeting, timed from its start, so a chunk and its citation name the minute; inside a
section each speaker turn is a paragraph `<speaker>: <text>`. The document's address is
the store's API address of the recording, its title `Meeting <start time>` (`meet`) or
`Meeting <meeting id> <start time>` (`zoom`), its
`ingested_format` `transcript`, and its revision the meeting's end (`meet`) or the
instance's start (`zoom`): a stored transcript is not fetched again. An incremental
listing reaches a day behind the stamp, since a store finishes a transcript some time
after the meeting; the periodic full listing finds a later one. A `zoom` full listing
(and probe) reads the instances that started in the last 30 days, as `meet` keeps
conference records for 30 days, so the old instances of a long recurring meeting cost
no check; documents written from older instances stay. A space or meeting the
account no longer sees, or may not read, ends the turn `failed` with its status
(`space <id>: http_404` or `http_403`) and retires nothing, while the other ids'
recordings are written. A probe of such an id fails: `add` and `rotate` answer
`SOURCE_UNREACHABLE` naming it, and `check` answers `reachable: false` with that summary.

A credential whose token answer holds any scope beyond reads (for `meet`, anything but
`meetings.space.readonly`; for `zoom`, any scope that is not `<resource>:read…`), or
names no scope, is refused: `add` and `rotate` answer `CREDENTIAL_REFUSED`, `check`
answers `reachable: false` with that summary, and a turn ends `credential_invalid`, so a
stored credential can never change the store.

A recording its store no longer lists retires nothing: both stores delete recordings
and transcripts on their own retention schedule (a conference record after 30 days), so
absence is not the writer asking. A recording's document goes with
`remove(delete_documents=true)` or `knowledge.delete`; while the store still lists it, a
full listing writes it again.

Speaker names are text of the transcript like any other, under the same rules as every
document: the document is written as the sync worker with `user_id` `-`, so a
namespace's erasure removes it and an erasure scoped to one user does not; a speaker's
words are removed with `knowledge.delete` of the document (and at the store, or the next
full listing writes them again). Decision records mined from a transcript
(by the sync's [mining](#mining-decisions), or `loom decisions extract`) can quote a speaker too; deleting the transcript leaves them,
and `memory.delete` of each decision's row (which retires its record) or the
namespace's erasure removes them.

`loom knowledge sync --org O --namespace <trunk namespace> [--source NAME] [--full]` runs
one such pass over one namespace's sources now, in-process, and leaves a manual
`knowledge_source_sync` row in `loom.worker_status`. `--full` records a full sync
request, as `sync_now(full=true)` does; run again while that request is unfinished, it
goes on with it, so repeated runs work through a large folder rather than starting
over.

### Syncing a project's documents

A `linear` source's turn follows the rules above, with three differences. Every turn
reads each project's whole document list (ids, titles, stamps and links, 50 to a page,
every page), since a project's list is small; there is no incremental listing, and
`full` only grants given-up documents their retry. A changed document is fetched on its
own (its content is Markdown already, `ingested_format` `markdown`, with no original)
and is bounded by `LOOM_KNOWLEDGE_SOURCE_SYNC_MAX_FILE_BYTES`; its revision is its
`updatedAt` and its address its link without the query. The link carries the title, so
a renamed document is written at its new address and its old one retired.

A document is retired only when the list was read whole, every project on every page,
and the tracker, asked about that document, answers its own not-found error or reports
it trashed, archived or in none of the source's projects; an answer that only lacks the
document, or a lookup that fails, retires nothing. A project the key's account cannot
see, any other failure on a list page, an answer that carries a GraphQL error beside its
data and a page that promises another without a cursor leave the turn `failed` and
retire nothing, while the other projects' documents are still written. The API reports
most failures as GraphQL errors, often with HTTP 200 or 400, so a turn reads the errors'
type as well as the status: a `429` or an error typed `RATELIMITED` is `rate_limited`, a
`401` or an authentication error is `credential_invalid`, and either ends the turn. A
document the tracker no longer finds (`not_found`), one over the size bound and one it
refuses (`graphql_<type>` for a GraphQL error typed `FORBIDDEN`, `INVALID_INPUT`,
`FEATURE_NOT_ACCESSIBLE` or `USER_ERROR`; `http_<status>` for a `4xx` with no type)
are given up on their revision at once; any other GraphQL error, whatever its status, a
`5xx`, a timeout and an answer without the document (`no_document`) make it wait. List
pages are paced half a second apart.

## Searching

`knowledge.search` runs a vector leg and a BM25 leg per namespace, and in hybrid mode
an exact-key leg, and fuses them by reciprocal rank:

| Field               | Default    | Effect                                                                                                                                                                                                                                           |
| ------------------- | ---------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------ |
| `search_mode`       | `hybrid`   | `semantic` (vector only) or `lexical` (BM25 only) pins one leg.                                                                                                                                                                                  |
| `lexical_weight`    | `1.0`      | The BM25 leg's weight in fusion. Lower it when the query's tokens are everywhere (paths, boilerplate).                                                                                                                                           |
| `namespace_scope`   | `all_mine` | `this` searches only `namespace`; `all_mine` fans out over the caller's namespaces.                                                                                                                                                              |
| `uri_prefix`        | `[]`       | Up to 8 prefixes (`docs/guides/`); a primary-key range on `loom.chunks`, not a post-filter.                                                                                                                                                      |
| `tags`              | `[]`       | Keep chunks carrying any of these tags.                                                                                                                                                                                                          |
| `since_seconds`     | unset      | Only chunks ingested in the window.                                                                                                                                                                                                              |
| `formats`           | `[]`       | Only chunks of documents in these formats: an ingested file's `format` (`sql`, `toml`, `markdown`, ...), or `""` for every other document, a synced one included whatever `knowledge.inspect` names it (`knowledge.get` returns null for those). |
| `block_types`       | `[]`       | Only chunks of these block types (`sql_statement`, `toml_key`, `paragraph`, `mixed`, ...).                                                                                                                                                       |
| `collapse_sections` | `true`     | One hit per (document, section); the survivor reports `siblings_folded`.                                                                                                                                                                         |
| `narrow_spans`      | `false`    | Also return the two-sentence `span` nearest the query, with its offsets and similarity.                                                                                                                                                          |
| `top_k`             | `5`        | 1–100.                                                                                                                                                                                                                                           |
| `rerank`            | `""`       | Reranker for the fused head; `""` = the deployment default, `"none"` = off.                                                                                                                                                                      |

Each hit carries `source_uri`, `chunk_idx`, `content`, `section` (the breadcrumb as
text) and `section_path`, `similarity`, `lexical_score`, `score` (the fused rank in
hybrid mode) and, on a fan-out, the `namespace` it came from. A citation names what it
read: `revision` is the document revision the chunk was written as and `format` its
document's format. A chunk of an [ingested file](#ingesting-files) also carries its
`block_type` and its `locator` (its lines, a key path, a PDF's pages or a deck's
slides), so a caller opens the passage in the source.

**Exact keys.** An extracted file's chunks carry the names their blocks define or read
(the tables and columns a SQL statement creates and reads, a TOML key path). In hybrid
mode the query's identifiers are looked up in them, and the chunks that name the most
rank first in a third leg; a hit from it reports `keys_matched`. A term counts as an
identifier when it cannot be a plain word (an underscore, a dot, a `$` or a digit, or a
capital after a lowercase letter: `orders_daily`, `server.port`, `userId`; the acronyms
`ID` and `TTL` are not), or when it is the whole query, and a dash joins the segments of
a dotted path (`pool.max-size`) and nothing else; a plain word in a question (`status`,
`name`) is as often a column of many tables as the word the question means, and the
lexical leg matches it already. Identifiers that share their words (`orders_daily`,
`daily_orders`) read the same to BM25, and the exact-key leg tells them apart. A block
type filter leaves out text a caller extracted (it has none); a TOML table packs with
its keys, so its chunk's type is `mixed`. A chunk records the revision and format it was
written as; one written before they were recorded reads revision 0 and format `""` until
its document is written again.

**Reranking.** `rerank` names a reranker (`builtin:openai[:<model>]`,
`builtin:cohere[:<model>]` or an endpoint in `LOOM_RERANK_ENDPOINTS_JSON`; a URL is
refused), or the deployment sets `LOOM_KNOWLEDGE_SEARCH_RERANK_DEFAULT`, which the op and
`loom_search`'s knowledge section take; `memory.ask`, `context.pack` and `loom_search`'s
repository-docs leg read knowledge without a rerank call. The legs then fetch at least
`LOOM_KNOWLEDGE_SEARCH_RERANK_TOP` hits (30), and the reranker reads each one's context
(title, description, breadcrumb; at most half the excerpt) and text,
`LOOM_KNOWLEDGE_SEARCH_RERANK_EXCERPT_CHARS` characters (800), and orders them before
sections collapse and the page is cut. `reranked_by` on the response names the reranker
that produced the order; it is empty when no reranker ran, it failed (fail-open), it took
longer than `LOOM_KNOWLEDGE_SEARCH_RERANK_TIMEOUT_S` (8 s) or it kept the fused order. A
model call the gateway holds or refuses fails the search instead. An LLM-free read
(`LOOM_READS_LLM_FREE`) reranks only as its request names, never with `builtin:openai`.
A rerank is one more model call per search: `builtin:openai` adds about two seconds, so
it is off unless asked for.

**Fan-out and private-first.** Under `all_mine` the namespaces are the caller's
memory-kind namespaces (`LOOM_MEMORY_SEARCH_FANOUT_KINDS`; code and tracker indexes are
reached by name or their own tools), up to `LOOM_MEMORY_SEARCH_FANOUT_MAX_NAMESPACES`
besides the addressed one. The addressed namespace and the private ones are searched
first, whatever the addressed one's visibility; public team namespaces are searched only
when no hit in that first pass reaches a similarity of 0.30.

**Collapse and narrowing.** Collapsing keeps the best window of a long section, leaving room for another section.
`narrow_spans` returns one sentence pair instead of a whole section, for one extra batched embed call.

A lexical leg needs corpus statistics; a namespace with none yet (or a failed read)
degrades to the vector leg (and, in hybrid mode, the exact-key leg). `loom
lexical-stats-rebuild --org O --namespace N` rebuilds them. A chunk with an empty
context (one an older build wrote) is scored on its text alone until `loom rechunk`
rewrites its document; run `loom lexical-stats-rebuild` after it, since the superseded
chunks stay in the document frequencies until then. Rechunk such documents before
changing the embedding model in Settings → Models & keys too: the provider rebuild
embeds a chunk's stored context and text, and an older chunk's empty context would drop
the context its vector was written with (and a titled document with no text, which has
nothing else to embed, fails the rebuild until it is rechunked).

## Reading a document

Search finds a passage; three other reads cover the rest of the work with a long text:

- **`knowledge.outline`** returns the sections recorded at ingest (`path`, `title`,
  `depth`, `chunk_idxs`, `chars`). With `section_path` (a path from that list) it also
  returns `section_content`, the section's chunks in order, and `include_children`
  folds its subsections in. This is the reader's move with a long document: look at
  the outline, pick the section, read that. A document with no headings reports
  `structured=false`.
- **`knowledge.get`** returns the full stored text. `include_superseded=true` reads a
  deleted or replaced version.
- **`knowledge.synthesize`** answers a `task` over up to 50 whole documents: every
  live chunk is read in order in windows of `window_chars` that respect section
  boundaries, each window is noted by the LLM, and the notes are folded into one
  answer with `citations`. `max_windows` (default 64) caps the model calls; a corpus
  needing more sets `truncated`. A citation names chunks the passes read
  (`[source#idx]`, `[source#a-b]`, or a list of them separated by `,` or `;`); a
  citation of anything else (a chunk past `max_windows`, an index that does not exist,
  another document) is removed from `answer` and listed in `unsupported_citations`,
  since following it would read something the answer did not come from.
  `cited_chunks` lists each chunk the kept citations name, with the document
  `revision` the chunk was written as and, for an ingested file, its `locator`. A bracket
  holding anything else, or naming a source with a blank, comma or semicolon, is prose
  and stays as written (of nested brackets, the innermost is read). An answer that is
  empty (no letter or digit) as written or once those citations are removed abstains
  with `abstain_reason="empty_answer"`.
  It needs a real LLM provider.

`memory.ask` also reads knowledge: it runs one hybrid knowledge search over the same
namespace and appends the passages as a citable block (per request `knowledge=false`
opts out), and routes a summary-shaped question to `knowledge.synthesize` when the
knowledge search concentrates on a few documents. See
[search and ask](./memory/search-and-ask.md).

## Inspecting a namespace's documents

`knowledge.inspect` (viewer) reports what a namespace's documents are, which of their
versions search serves, what was extracted from them, and where their `knowledge.ingest`
jobs stand. It reads what is stored, never parses a document, never calls an extractor
and writes nothing. It is in the [assistant
toolset](../reference/mcp.md#the-assistant-toolset), and the dashboard's Assistant page
shows it when Documents is the subject inspected. `sample_limit` (0–100, default 20)
bounds the listed documents and every finding's samples.

- **`documents`**: live and deleted (`knowledge.delete`) documents by format (the row's
  `format`, which `knowledge.ingest` sets, else the `ingested_format` a source sync
  records in its metadata (`transcript`, `markdown`, `slides`, or the extractor's `pdf`
  or `pptx`), else the stored content type, else the extension of the original filename
  or the `source_uri` path) and by `source_uri` scheme, and the newest ingest. A
  document row holds only its current version, and its `revision` numbers it (1, 2,
  ...): `earlier_revisions` sums the revisions before each current one, and `unnumbered`
  counts rows no write numbered (`revision` 0). An earlier revision's text is known only
  through its retired chunks. `retired_versions` counts those chunk versions: the ones a
  later write or a delete replaced, and every `knowledge.ingest` attempt that inserted
  chunks and never published (failed, cancelled, or beaten by a later write), which no
  revision numbers.
- **`chunks`**: the live chunks per embedding model, how many carry a vector, and
  `searchable`, the ones under the server's model (`embedder_model`) with a vector:
  `knowledge.search` reads no others.
- **`listed`**: the newest live documents, each with its version, its `revision`, the
  `ingestion_id` of the `knowledge.ingest` job that published it (empty for a text
  write), its retired versions, its live chunks (searchable, unembedded, under another
  model, distinct versions, characters per chunk) and its decisions. `listed_complete`
  says whether the list holds every live document.
- **`decisions`**: the live decision rows whose `source_resource_id` cites a section of
  a document (`knowledge:<namespace>/<source_uri>#<section>`, as
  `loom decisions extract` writes it), per listed document and for the namespace. A
  citation of `doc#v2#Intro` counts for the document `doc#v2`, never for `doc`. Each
  stage is counted apart: `decided`, and `proposed` (a decision recorded with
  `stage="proposed"`, tagged `decision:proposed`). Within a stage, the rows are counted
  as confirmed (pinned or attested; for a proposal that backs its wording, not a
  choice), unconfirmed, tagged `source-changed`, and with a recorded outcome. Rows the
  extractor wrote into a repository's namespace are not counted.
- **`findings`**: one per check, in the shape of the [audit's
  findings](./namespaces-and-sharing.md#reading-a-namespaces-health), a clean check at
  `info` with count 0. `documents_without_chunks`, `unembedded_chunks` and
  `chunks_under_other_model` are `system_state`: the server's own state.
  `mixed_live_versions` is a `usage_heuristic`: live chunks of two versions of one
  document, which `knowledge.add` on an existing `source_uri` leaves and
  `knowledge.upsert` does not; a `knowledge.ingest` publish passes through it too,
  briefly, or until the job's next run when it could not confirm its insert. Each
  finding names the documents `knowledge.ingest` published apart (`ingested`,
  `ingested_samples`). A text document is repaired with `knowledge.upsert` or
  `loom rechunk --source-uri <uri>`; an ingested one with `knowledge.ingest` of its
  original (`GET /v1/knowledge.download` serves it), since a text write re-cuts its
  rendered text and clears its format, parser, coverage and ingestion. For
  `mixed_live_versions` on an ingested document, a job of the source still processing at
  its publish stage settles the state on its next run, so it is re-ingested only when no
  ingest of the source is open.
- **`ingestions`**: the namespace's `knowledge.ingest` jobs (their rows are kept 30
  days) by state, `stalled` for a processing job no worker has updated within its lease,
  when the oldest open job was queued, and the sources with a job and no live document (a
  first ingest, or one after a delete, queued or failed; not a source deleted after its
  last job ended), listed newest first in `without_document_listed`. `worker_enabled` and
  `extractor_configured` are this server's settings (`LOOM_KNOWLEDGE_INGEST_ENABLED`,
  `LOOM_DOCUMENT_EXTRACTOR_URL` or `LOOM_DOCUMENT_EXTRACTORS_JSON`); the jobs show what
  the deployment did.
- **`extraction`**: the live documents an extraction published, how many are incomplete,
  and per capability the artifact's `coverage.lost` names (`sql_structure`, `exact_keys`,
  ...), the documents that lost it.
- Each listed document also carries its **`extraction`** (the coverage's `unit`,
  `total`, `extracted`, `failed` and `lost`; null for a text write) and its **`jobs`**:
  how many its `source_uri` has, the `latest`, and the `current` one, the oldest open job
  when one is (the one a worker runs next) else the latest. `open_ingestion_id` names
  that open job, so a caller that did not start the ingest can pass it to
  `knowledge.ingest_status` or `knowledge.ingest_cancel`. A job carries its state, stage,
  attempts, `error_code`, its `failure_class`, and when it was queued, last changed,
  ended and is next due; its error message is not returned. The job rows hold no start
  time. A pending job with a cancel requested reads `cancelled`, the state
  `knowledge.ingest_status` answers, though its row stays pending until a worker takes
  it; its other fields are the row's. `replaced` says no job is open and the source's
  document row was written or deleted after the newest ended: a text write and a delete
  write no job row, so that job no longer says what search serves. A metadata edit
  without `reindex` keeps the row's `ingested_at` and replaces nothing; a reindex
  rewrites the document and counts as a write.
- **`ingest_findings`**: one per check, in the same shape, each sample a `source_uri`
  with its job's id in `ingestion_ids`, and `without_document` counting the sources with
  no live document. A source is judged by its current job, and a `replaced` one by none
  (`ingestions.replaced` counts them):
  - `ingest_backlog` (`system_state`): a job pending or processing whose last attempt
    did not fail, that a store or embedder outage put back (`failure_class` `retrying`),
    or that a worker is running again after any outage. A young backlog is the queue
    working: the worker runs one job per pass.
  - `extractor_unavailable` (`system_state`): no extractor takes the format
    (`format_unavailable`), or the one routed to it is unreachable, slow past its timeout
    or failing, whether the job failed or waits to retry, or answered an artifact that
    breaks the [extractor contract](../reference/extractor-contract.md)
    (`invalid_artifact`).
  - `ingest_missing_access` (`system_state`): the stored original could not be read
    (`original_gone`), or the extractor refused the server's credential (401, 403 or
    407).
  - `ingest_content_rejected` (`contract_violation`): the extractor refused the file, or
    the server refused what it made of it (`incomplete_extraction` without
    `allow_partial`, `no_text`, `too_large`). `resource_limit`: the extractor passed its
    own memory or output limit, or its artifact was over
    `LOOM_DOCUMENT_EXTRACTOR_MAX_ARTIFACT_BYTES`. The earlier revision, if any, still
    serves.
  - `ingest_failed_on_server` (`system_state`): the job failed on the server's side,
    neither the file's nor the extractor's fault: a store or embedder outage that used up
    `knowledge_ingest_max_attempts`, runs cut off before they finished (both
    `retries_exhausted`), or a server defect (`internal`).
  - `extraction_incomplete` (`usage_heuristic`): a live document published with less
    than its whole source, a unit failed or a capability lost.

  A revision conflict (a later write of the source landed first, or `expected_revision`
  did not hold) has no finding: it counts in `ingestions.failed`, and the revision that
  landed is what search serves. Each remedy names the op or setting that clears it.
  Coverage counts pages only when an extractor reports `pages` as its unit; no field
  records an OCR fallback.

- **`memory_findings`**: `namespace.audit`'s `oversized_values` and `episodic_blobs`,
  memory rows that may belong in documents, cited by code and count. `mentor.brief`
  reports the same shape for one agent as `documents_as_memories`.
- **`skipped`**: each section that could not be read, as `section: reason`. Its fields
  are null or empty, not zero.

Documents and their jobs are the namespace's under any read scope, as every `knowledge.*`
read serves them (`knowledge.ingest_status` answers any job of the namespace by id).
Under a token narrowed to one user or session, the decisions count only the rows inside
that scope, and `memory_findings` is skipped, since the audit reads every user's rows.

## Replacing, editing and deleting

| You want to                            | Use                                                     | What happens                                                                                          |
| -------------------------------------- | ------------------------------------------------------- | ----------------------------------------------------------------------------------------------------- |
| Replace a changed document             | `knowledge.upsert` (same `source_uri`)                  | New chunks are written; the previous version's chunks are marked superseded and kept for history.     |
| Change the title, tags or blurb        | `knowledge.update_metadata`                             | The document row changes; chunks keep the old context in both legs unless `reindex=true` rewrites it. |
| Rename a document                      | `knowledge.delete`, then `knowledge.add` at the new URI | `source_uri` is the identity agents cite, so it is not editable.                                      |
| Remove a document                      | `knowledge.delete`                                      | Chunks and the document row are superseded; searches stop returning them at once.                     |
| Erase a person's or a namespace's data | [`erasure.request`](./memory/retention-and-erasure.md)  | The erasure saga deletes documents, chunks and stored originals, and cascades to external systems.    |

`knowledge.add` does not retire an earlier version at the same `source_uri`; use
`knowledge.upsert` to replace one. `knowledge.upsert` also reports duplicates without
blocking: `duplicate_of_uri` names a live document under another URI with the same
title and content hash, and `duplicate_chunk_count` counts new chunks whose hash
already lives under another URI. The caller decides whether to keep both.
`knowledge.dedup_list` lists such clusters for a namespace, and `knowledge.dedup_resolve`
supersedes one side (`keep_canonical` supersedes the duplicate, `swap` the canonical);
it requires `confirm_duplicate_uri` equal to `duplicate_uri` so a mis-picked dropdown
cannot supersede the wrong document.

A delete is a soft delete: the rows stay, marked superseded, and `include_superseded`
reads them. No table TTL removes them unless an operator configured one
(`LOOM_RETENTION_CHUNKS_DAYS`, `LOOM_RETENTION_DOCUMENTS_DAYS`, both 0 by default). The
stored original stays until the `document_blob_gc` worker finds no live document naming
it: every `LOOM_DOCUMENT_BLOB_GC_INTERVAL_SECONDS` it removes, one namespace at a time,
the originals of deleted and re-uploaded documents and of documents a retention TTL
removed, so originals age out with their documents. It spares an original written in the
last `LOOM_DOCUMENT_BLOB_MIN_AGE_SECONDS` (3600): a concurrent upload of the same bytes
may be about to name it. A re-upload that fails after storing its bytes keeps the
document's current download: no write deletes the original a document names.

With a bucket, the min age must outlast a write from the start of its upload to its row
landing, since the store may date an object from the start of its upload. The settings
refuse a min age below this floor (906 seconds at the defaults). It is a minimum, not a
bound on every path a slow write can take (it does not count the client's reconnects or
its periodic schema check one by one), so keep the min age well above it, as the 3600
default does:

- 300 seconds for the upload: a write whose upload takes longer is refused with 503, and
  its new object, which no row names, is left to the bucket walk;
- the row insert with every retry, a describe of the table and the insert each run twice;
- a minute for reconnects.

```text
floor = 300 + 2 x (1 + LOOM_CH_REPLICA_BOUND_RETRIES)
            x (LOOM_CH_TIMEOUT_READ_S + LOOM_CH_TIMEOUT_WRITE_S + 2 x LOOM_CH_POOL_WAIT_MS / 1000)
        + 60
```

They also refuse an unbounded (0) `LOOM_CH_TIMEOUT_WRITE_S`, `LOOM_CH_TIMEOUT_READ_S` or
`LOOM_CH_POOL_WAIT_MS`, which no floor can outlast.

The floor assumes the walk's catalog read sees every acknowledged row, which holds on
one server and on SharedMergeTree. On a self-managed ReplicatedMergeTree cluster with more
than one replica, a sequential read waits only for inserts written with `insert_quorum`,
which Loom's do not use, so a replica behind a write could call its object unnamed: keep
the min age well above the floor there (the 3600 default leaves about 45 minutes), or send
every Loom process to one replica. `loom doctor` warns about this topology
(`replica_consistency`).

## Ingesting files

`knowledge.add` takes text the caller extracted. A file Loom should extract goes in two
steps, so the bytes never travel as base64 inside JSON and extraction never holds a
request open:

1. **`POST /v1/knowledge.upload?org=&namespace=&filename=`** with the file as the raw
   body (writer role; `sha256=` and `content_type=` optional). The bytes are stored as an
   original (where originals are stored, below) and the answer is a `201` with the
   scoped `upload_id`, `sha256`, `size`, the `format` its name and `content_type` give
   and `expires_at` (`LOOM_KNOWLEDGE_UPLOAD_TTL_SECONDS`, a day by default). A body over
   `LOOM_KNOWLEDGE_UPLOAD_MAX_BYTES` (64 MiB) is `413`, bytes that do not hash to the
   `sha256` sent are `422 CHECKSUM_MISMATCH`, and a legacy Office name (`.doc`, `.xls`,
   `.ppt`) is `415 UNSUPPORTED_FILE`, before anything is stored. The server does not
   read the bytes to type them: content that is another format than its name is refused
   at ingest (`format_mismatch`).
2. **`knowledge.ingest`** with that `upload_id` and the `source_uri` to publish under
   (title, description, tags, metadata as for `knowledge.add`). Only the caller who
   uploaded the file can ingest it. Options say what a file cannot: `format`,
   `encoding`, `sql_dialect`. `expected_revision` publishes only over that revision
   (0: no live document), and `allow_partial` publishes an extraction that lost part of
   its source instead of failing it. The answer is the job's `ingestion_id`, `pending`.

The `knowledge_ingest` worker then sends the original's bytes to the extractor service
its format is routed to, which returns the extraction artifact the server checks as
untrusted input ([document extraction](../architecture/document-extraction.md)); the
server parses nothing itself. It stores the artifact, cuts structural chunks, embeds
them (an input this namespace already embedded under the same model keeps its stored
vector) and publishes the next **revision** of the source: chunks first, then the
document row, then the earlier version's chunks retired. A document row carries its
`revision`, `format`, `parser` and `coverage`, which `knowledge.get` returns; each
chunk's `chunk_metadata` carries its `locator` (line span, char span, TOML key path, PDF
page or deck slide span) and block ids, and its `keys` the exact names it defines or
reads. Text writes (`knowledge.add`, `upsert`) number revisions too. Ingests of one
source run one at a time, in the order they were asked, so `expected_revision` guards an
ingest against another; a text write takes no such claim, so one that races a write of
the same source can reuse its revision number, and the later version wins (an ingest a
later write of the source overtook, a text write, a metadata edit or a delete, fails
`revision_conflict`).

`knowledge.ingest_status` follows a job: `pending`, `processing` (with its `stage`:
`parse`, `embed`, `publish`), then `ready`, `partial`, `failed` or `cancelled`, with the
coverage, the parser's warnings, the chunks and revision published, and `retrieval`
(`ready` once the document is searchable). A job's record is kept for 30 days after its
last change, an upload's for 7 days after it expires. A failed job names why in
`error_code`:

| `error_code`            | Means                                                                                                                                   |
| ----------------------- | --------------------------------------------------------------------------------------------------------------------------------------- |
| `format_unavailable`    | no configured extractor takes the format (none routed, it does not declare it, or it answers `format_unavailable`)                      |
| `unsupported_format`    | an `options.format` Loom does not know, or bytes of no format the extractor reads                                                       |
| `format_mismatch`       | the declared or named format is not what the bytes are                                                                                  |
| `corrupt`               | the file is damaged, or the extractor's parser fails on it                                                                              |
| `timeout`               | the extraction took longer than the extractor allows                                                                                    |
| `resource_limit`        | the extraction passed the extractor's memory or output limit, or the artifact the server's `LOOM_DOCUMENT_EXTRACTOR_MAX_ARTIFACT_BYTES` |
| `encoding`              | the text is not valid in its encoding; declare it in `options.encoding`                                                                 |
| `invalid_option`        | an unknown encoding or SQL dialect                                                                                                      |
| `incomplete_extraction` | coverage is incomplete and `allow_partial` was off                                                                                      |
| `no_text`               | the file holds no text to index                                                                                                         |
| `too_large`             | the original is over the extractor's `max_bytes`, or it cut more chunks than `LOOM_KNOWLEDGE_INGEST_MAX_CHUNKS`                         |
| `revision_conflict`     | the live revision is not `expected_revision`, or a later write of the source landed after this ingest began to publish                  |
| `original_gone`         | the upload's bytes are no longer stored                                                                                                 |
| `retries_exhausted`     | the extractor, a provider or a store failed `LOOM_KNOWLEDGE_INGEST_MAX_ATTEMPTS` times (`retrying` before)                              |
| `invalid_artifact`      | the extractor answered with an artifact that breaks the contract                                                                        |
| `refused`               | the extractor refused the source with a `4xx` that names no contract code                                                               |
| `internal`              | a defect; the log line has it                                                                                                           |

A transient failure (an extractor that cannot be reached, times out, or answers a `5xx`,
`408`, `429` or another status that is neither a result nor a refusal; a provider or a
store outage) puts the job back with a doubling delay (at most an hour); a source the
extractor refuses, or an artifact that breaks the contract, fails at once with its code. A
pass runs one due job, one source's jobs in order and namespaces in turn, and is cancelled
after `LOOM_KNOWLEDGE_INGEST_MAX_PASS_SECONDS` (15 minutes); a job cut off there, or by a
process that stopped, runs again from its start and counts the attempt. A job that fails,
or is cancelled, publishes nothing, so the earlier revision keeps serving.
`knowledge.ingest_cancel` stops a pending job at once and a running one before it
publishes; a job past its publish check finishes. The reference extractor reads Markdown,
HTML, plain text, SQL, TOML, PDF (its text layer: a scanned page needs OCR, so the job
needs `allow_partial`), DOCX and PPTX; another format fails `format_unavailable` until an
extractor that declares it is configured. `knowledge.update_metadata` with `reindex=true`
refuses an ingested document (its chunks come from the artifact): ingest it again to
re-index it. A chunk's context (the title, description and section breadcrumb both search
legs read) is filled as for `knowledge.add`.

## Where originals are stored

An original's catalog row in `loom.document_blobs` says where its bytes are. With
`LOOM_DOCUMENT_OBJECT_STORE=clickhouse` (the default) they are the row's `data`. With `s3`
(which needs boto3, the `aws` extra the Docker image installs) each upload is written as its
own object, `<LOOM_DOCUMENT_S3_PREFIX>/<org>/<namespace>/<sha256>/<write id>`, in
`LOOM_DOCUMENT_S3_BUCKET` of an S3-compatible store (`LOOM_DOCUMENT_S3_ENDPOINT_URL`,
`LOOM_DOCUMENT_S3_REGION`; credentials from the AWS credential chain), with the store's
SHA-256 check, before the row naming it is written. The org and namespace segments of a
key are percent-encoded. A row is read from wherever it says, so originals stored before
a switch keep downloading. A download over 4 MB streams from the bucket; an object
missing from the bucket answers `410 BLOB_GONE` and a bucket that fails answers
`503 BACKEND_UNAVAILABLE`, never an empty file. Each process runs at most 8 bucket
transfers (uploads, and downloads up to 4 MB) and 4 bucket streams at once; one more
waits up to `LOOM_CH_POOL_WAIT_MS` and then answers `503 OVERLOADED`.

The server's credentials need `s3:PutObject`, `s3:GetObject`, `s3:DeleteObject` and
`s3:ListBucket` on the bucket and prefix. Without `s3:ListBucket`, S3 answers a read of a
missing object with `403 AccessDenied` instead of `404 NoSuchKey`, so a gone original
answers `503` rather than `410`, and the sweep's walk and every erasure of the bucket fail.

The `document_blob_gc` sweep, with a bucket:

- deletes an unreferenced original's objects with its rows, and the record of who uploaded
  it only once they are gone (when the bucket refuses, the walk drops the record with the
  last object);
- keeps a row naming an object outside the configured bucket, and records it as the
  pass's failure. A pass reads a namespace from its first original, so after a bucket
  change that leaves 20,000 or more such rows first in a namespace, the rest of that
  namespace waits until the rows are fixed;
- walks the bucket, `LOOM_DOCUMENT_BLOB_GC_OBJECT_PAGES` (20) pages of 1,000 keys per
  pass from where the last pass stopped, and deletes an object no row names (one a
  re-upload replaced, once the replaced row is merged away, a write whose row never
  landed, one a failed delete kept) once it is older than
  `LOOM_DOCUMENT_BLOB_MIN_AGE_SECONDS`. One walk takes one pass per 20,000 keys (at the
  hourly default, a day per 480,000 objects).

The bucket and prefix are this deployment's alone: two deployments sharing one prefix
delete each other's originals. Erasure does not wait for the walk: it deletes the scope's
objects before their rows ([retention and erasure](./memory/retention-and-erasure.md)).

Switch to `s3` only when every replica runs a build that reads the row's location: an
older build reads the empty `data` column and answers the download `410 BLOB_GONE`. For
the same reason, do not roll back past this build while rows name objects: an older
build's erasure deletes those rows and cannot reach their objects, and its sweep never
walks the bucket. Keep the bucket and prefix fixed while any row names an object under
them; an erasure that finds a row naming an object elsewhere fails rather than leave the
bytes. The bucket needs its own backup, and a versioned bucket keeps erased originals until
a lifecycle rule expires their noncurrent versions ([backup and recovery](../operations/backup-and-recovery.md)).

## Getting files in: the ingest CLI

`knowledge.add` and `knowledge.upsert` take text: a caller sends the text it extracted.
A folder registered as a [document source](#document-sources) is pulled by the server
instead, with PDFs and `.pptx` files parsed by an extractor service. For a one-off push
of a local file, an `s3://` object or an `https://` page, `dev/loom_ingest.py` parses on
the client and posts the text:

```bash
make ingest FILE=docs/runbook.md TAGS=ops,runbooks      # wraps dev/loom_ingest.py
python dev/loom_ingest.py --namespace project-x --title "Q3 spec" spec.pdf
python dev/loom_ingest.py --upsert --source-uri 'github://acme/spec.md' spec.md
python dev/loom_ingest.py s3://acme-docs/runbooks/      # every object under the prefix
python dev/loom_ingest.py https://example.com/page.html --dry-run
```

| Source                                     | Handling                                                                                                     |
| ------------------------------------------ | ------------------------------------------------------------------------------------------------------------ |
| Markdown, text, JSON, CSV, YAML, RST, logs | Read as UTF-8, no parsing.                                                                                   |
| PDF                                        | Text extracted with `pypdf` (install it); the PDF outline becomes Markdown headings, so chunking follows it. |
| `s3://bucket/key` or `s3://bucket/prefix/` | One object, or one ingest per object under the prefix (boto3 credential chain).                              |
| `https://…`                                | Fetched with a 30 s timeout and a 10 MB cap; HTML, PDF, Markdown or text.                                    |

It reads the token from `--token`, `LOOM_API_KEY`, `~/.loom/config.json` or `~/.loom/token`, the
server from `--base-url` or `LOOM_BASE_URL`, and defaults `--namespace` to the token's first
namespace, `--title` to the file name and `--source-uri` to `file://<absolute path>`.
`--upsert` calls `knowledge.upsert` instead of `add`. Images, video, Parquet and Iceberg
are out of scope. `loom demo` ingests `AGENTS.md` and asks a question of it, which is
the shortest check that the knowledge plane works on a fresh stack.

## Example

Store a runbook, find a passage and read its section (Python SDK, stock stack):

```python
from loom import Agent

async with Agent("dev", "local", "docs-bot", base_url="http://localhost:7777") as agent:
    await agent.knowledge.add(
        open("runbook.md").read(),
        source_uri="file:///srv/runbook.md",
        title="Ops runbook",
        tags=["ops"],
    )
    hits = await agent.knowledge.search("how do I roll back a deploy", top_k=3)
    for h in hits:
        print(h.source_uri, h.section, round(h.similarity, 3))
    outline = await agent.knowledge.outline("file:///srv/runbook.md")
```

The same search over HTTP:

```bash
curl -s localhost:7777/v1/knowledge.search -H "Authorization: Bearer $LOOM_API_KEY" \
  -H 'content-type: application/json' \
  -d '{"org":"dev","namespace":"local","query":"roll back a deploy","namespace_scope":"this","narrow_spans":true}'
```

## Settings

| Setting                                                                                                                                                                            | Effect                                                                                                                                                                                                                                                                                |
| ---------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `LOOM_KNOWLEDGE_CHUNK_SIZE`, `…_CHUNK_STRIDE`                                                                                                                                      | How documents are split ([Knowledge → Chunking](../reference/configuration.md#chunking)). `loom rechunk --org O --namespace N --apply` re-chunks existing documents after a change.                                                                                                   |
| `LOOM_MEMORY_SEARCH_FANOUT_KINDS`                                                                                                                                                  | Which namespace kinds an `all_mine` search spans ([MemorySearch](../reference/configuration.md#memorysearch)).                                                                                                                                                                        |
| `LOOM_MEMORY_SEARCH_FANOUT_MAX_NAMESPACES`                                                                                                                                         | How many of the caller's namespaces an `all_mine` search reads besides the addressed one ([MemorySearch](../reference/configuration.md#memorysearch)).                                                                                                                                |
| `LOOM_MCP_LOOM_SEARCH_KNOWLEDGE_MIN_SIMILARITY`                                                                                                                                    | The floor for knowledge hits in `loom_search` ([Mcp](../reference/configuration.md#mcp)).                                                                                                                                                                                             |
| `LOOM_MEMORY_ASK_KNOWLEDGE_TOP_K`, `LOOM_MEMORY_ASK_SYNTHESIZE_ROUTE_MAX_DOCS`                                                                                                     | Whether and how `memory.ask` reads knowledge ([MemoryAsk](../reference/configuration.md#memoryask)).                                                                                                                                                                                  |
| `LOOM_KNOWLEDGE_SOURCES_MAX_PER_NAMESPACE`, `LOOM_KNOWLEDGE_SOURCE_PROBE_TIMEOUT_S`                                                                                                | How many document sources one repository namespace may register, and how long a source's probe may take ([Knowledge](../reference/configuration.md#knowledge)).                                                                                                                       |
| `LOOM_KNOWLEDGE_SOURCE_SYNC_ENABLED`, `…_INTERVAL_SECONDS`, `…_IDLE_INTERVAL_SECONDS`, `…_MAX_PASS_SECONDS`, `…_DOCUMENTS_PER_PASS`, `…_FULL_INTERVAL_SECONDS`, `…_MAX_FILE_BYTES` | Whether and how often the sync worker pulls sources, how long it sleeps when there are none, how long one pass may run, how many documents a pass handles, how often a source is listed whole and the largest file it ingests ([Knowledge](../reference/configuration.md#knowledge)). |
| `LOOM_RETENTION_CHUNKS_DAYS`, `LOOM_RETENTION_DOCUMENTS_DAYS`                                                                                                                      | Opt-in table TTLs; 0 keeps rows forever ([Retention](../reference/configuration.md#retention)).                                                                                                                                                                                       |

## Limits

- **Embedding happens on the write.** `knowledge.add` embeds every chunk before it
  returns, in one batched provider call, so a large document costs one embed call per
  chunk and a slow provider makes the add slow.
- **Search reads only the live embedding model's vectors.** After changing
  `LOOM_EMBEDDING_MODEL`, documents are invisible to vector search until re-embedded
  (`loom rechunk --apply` re-chunks and re-embeds them).
- **A cold namespace has no lexical leg** until its statistics exist; results are
  vector-only in the meantime.
- **`bulk_add` does not supersede.** It behaves like `add` per document; use `upsert`
  per document to replace.
- **Knowledge is not belief-revised.** Two documents can contradict each other; Loom
  flags identical content, not conflicting content. Claims that must be judged against
  each other belong in memory.

## Related

- [Unified search](./unified-search.md) — `loom_search`, which includes the knowledge
  section.
- [Search and ask](./memory/search-and-ask.md) — how `memory.ask` cites documents.
- [Retention and erasure](./memory/retention-and-erasure.md) — what erasure removes.
- [The dashboard](./dashboard.md) — the Library page lists, uploads and downloads
  documents.
- [Memory architecture](../architecture/memory-architecture.md) — the chunk and
  document tables from the inside.
