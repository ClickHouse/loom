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
| `knowledge.search`          | viewer | Hybrid passage search (vector and BM25 fused).                                         |
| `knowledge.get`             | viewer | One document's full text and metadata by `source_uri`.                                 |
| `knowledge.list`            | viewer | Documents of a namespace, paged by an opaque cursor, filterable by tag and URI prefix. |
| `knowledge.outline`         | viewer | A document's table of contents, and optionally one section's assembled text.           |
| `knowledge.synthesize`      | viewer | Complete a task over whole documents (summaries, spread-out evidence) with the LLM.    |
| `knowledge.update_metadata` | writer | Edit `title`, `description` or `tags`; `reindex=true` rewrites the chunks' context.    |
| `knowledge.delete`          | writer | Supersede a document and its chunks.                                                   |
| `knowledge.bulk_delete`     | writer | The same for up to 500 `source_uris`.                                                  |
| `knowledge.reads`           | viewer | How often agents opened each document over the last `days` (1–30).                     |
| `knowledge.dedup_list`      | admin  | Documents (and, optionally, chunks) of the namespace with identical content hashes.    |
| `knowledge.dedup_resolve`   | admin  | Supersede one side of a duplicate pair.                                                |

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
and only the namespace's admins change that. The one kind served is `gdrive`, a shared
folder of docs, slides, decks and PDFs, read with a service account the folders are
shared to read-only; its credential is the account's JSON key. This build stores
sources, probes them and records sync and delete requests; no worker pulls documents
from them yet. A source's `sync` shows the last sync request (`requested_at`).

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
default). The store's API hosts are fixed in the server, so a registered source cannot
point the server at another host. The credential is sealed at rest under its own
purpose (`loom secrets reseal` covers it), held in memory only for a probe, never
returned by any op and never written to an audit row. `add` records the namespace,
name, kind, config and `dry_run`; `rotate` the namespace and name; `remove` and
`sync_now` their input (namespace, name, `confirm` and `delete_documents`, or `full`).
`check` is admin-only because its probe spends the source's rate limit.

A probe exchanges the key for a read-only token, checks every folder together (one
call each) and lists the included files of all folders in one call, the first 1000 at
most. It is bounded as a whole by `LOOM_KNOWLEDGE_SOURCE_PROBE_TIMEOUT_S` (default
20 s); past it `add` and `rotate` answer `SOURCE_UNREACHABLE` and `check` answers
`reachable: false`.

Every write of a namespace's sources (`add`, `rotate`, `remove`, `sync_now`, and the
retirement in `code.repo.remove`) is serialized by a lease, so a write's checks still
hold when it writes; a probe runs outside it. A write that finds the lease held past
a short wait answers `OVERLOADED` and can be retried; one whose lease store cannot
answer is refused, never run unlocked. The one writer outside the lease is
`loom secrets reseal`, which writes a row it read again at that row's version + 1, so
any later write wins over it. Versions come from the ClickHouse servers' clocks, so
writes stay ordered while those clocks differ by less than the time between them.

A removed source's row stays, inactive and with its credential cleared. With
`delete_documents` and documents to retire, the row also carries the request for the
sync worker. Removing a repository (`code.repo.remove`) retires the sources registered
on its namespace the same way and names them in `sources_deactivated`; the purge of
the repository's code index that follows deletes their sync state (it lives in
`loom.code_index_state`) and the namespace's documents, and leaves the inactive
source rows. A source can
still outlive its repository's trunk namespace (a repository retired by editing the
deployment's configuration, a branch that now outranks the trunk, or a repository
removed by a replica of the previous release, which does not retire sources): its row
stays active, keeps its sealed credential and counts toward the namespace's quota
until `knowledge.source.remove` retires it. `check`, `rotate` and `sync_now` refuse it
with `NOT_A_CODE_NAMESPACE`; `list`, `inspect` and `remove` still serve it, and a sync
worker must skip it.

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

| Code                    | When                                                                                                                                               |
| ----------------------- | -------------------------------------------------------------------------------------------------------------------------------------------------- |
| `NOT_A_CODE_NAMESPACE`  | `add`, `check`, `rotate` or `sync_now` names a namespace that is not a repository's trunk code namespace.                                          |
| `BAD_NAME`              | The name is not 2-32 lower-case letters, digits, `-` or `_`, starting with a letter.                                                               |
| `BAD_KIND`              | The kind is not one the server serves.                                                                                                             |
| `BAD_CONFIG`            | The config has an unknown key, a malformed folder id or an unknown `include` value.                                                                |
| `SOURCE_EXISTS`         | The namespace has an active source of that name.                                                                                                   |
| `SOURCE_QUOTA`          | The namespace has `LOOM_KNOWLEDGE_SOURCES_MAX_PER_NAMESPACE` active sources (default 8).                                                           |
| `NO_ROOT_SECRET`        | The deployment has no `LOOM_JWT_SECRET`, so a credential cannot be stored.                                                                         |
| `CREDENTIAL_REFUSED`    | The credential is not a service account key, or the store refused it; nothing is stored.                                                           |
| `SOURCE_UNREACHABLE`    | A folder is not shared with the account or is not a folder, the store failed or rate-limited the probe, or the probe timed out; nothing is stored. |
| `SOURCE_DELETE_PENDING` | `add` names a removed source whose documents are still waiting to be retired.                                                                      |
| `SOURCE_DOCUMENTS_KEPT` | `add` names a removed source whose kept documents are still live; delete them first.                                                               |
| `SOURCE_REPLACED`       | `rotate` or `sync_now`'s source was removed and added again during the call (409); act on the current source.                                      |
| `SOURCE_TAGS_LOCKED`    | A write that keeps the sync worker as a document's author changes its `source:*` tags.                                                             |
| `SOURCE_NOT_FOUND`      | No active source of that name in the namespace (404).                                                                                              |
| `CONFIRM_REQUIRED`      | `remove`'s `confirm` is not the source's name.                                                                                                     |

A probe's error names the HTTP status (`http_403`), never the store's response text.

## Searching

`knowledge.search` runs two legs per namespace and fuses them by reciprocal rank:

| Field               | Default    | Effect                                                                                                 |
| ------------------- | ---------- | ------------------------------------------------------------------------------------------------------ |
| `search_mode`       | `hybrid`   | `semantic` (vector only) or `lexical` (BM25 only) pins one leg.                                        |
| `lexical_weight`    | `1.0`      | The BM25 leg's weight in fusion. Lower it when the query's tokens are everywhere (paths, boilerplate). |
| `namespace_scope`   | `all_mine` | `this` searches only `namespace`; `all_mine` fans out over the caller's namespaces.                    |
| `uri_prefix`        | `[]`       | Up to 8 prefixes (`docs/guides/`); a primary-key range on `loom.chunks`, not a post-filter.            |
| `tags`              | `[]`       | Keep chunks carrying any of these tags.                                                                |
| `since_seconds`     | unset      | Only chunks ingested in the window.                                                                    |
| `collapse_sections` | `true`     | One hit per (document, section); the survivor reports `siblings_folded`.                               |
| `narrow_spans`      | `false`    | Also return the two-sentence `span` nearest the query, with its offsets and similarity.                |
| `top_k`             | `5`        | 1–100.                                                                                                 |
| `rerank`            | `""`       | Reranker for the fused head; `""` = the deployment default, `"none"` = off.                            |

Each hit carries `source_uri`, `chunk_idx`, `content`, `section` (the breadcrumb as
text) and `section_path`, `similarity`, `lexical_score`, `score` (the fused rank in
hybrid mode) and, on a fan-out, the `namespace` it came from.

**Reranking.** `rerank` names a reranker (`builtin:openai[:<model>]`, `builtin:cohere[:<model>]`
or an endpoint in `LOOM_RERANK_ENDPOINTS_JSON`; a URL is refused), or the deployment
sets `LOOM_KNOWLEDGE_SEARCH_RERANK_DEFAULT`, which the op and `loom_search`'s knowledge
section take; `memory.ask` and `context.pack` read knowledge without a rerank call. The
legs then fetch at least `LOOM_KNOWLEDGE_SEARCH_RERANK_TOP` hits (30), and the reranker
reads each one's context (title, description, breadcrumb; at most half the excerpt) and
text, `LOOM_KNOWLEDGE_SEARCH_RERANK_EXCERPT_CHARS` characters (800), and orders them
before sections collapse and the page is cut. `reranked_by` on the response names the
reranker that produced the order; it is empty when no reranker ran, it failed
(fail-open), it took longer than `LOOM_KNOWLEDGE_SEARCH_RERANK_TIMEOUT_S` (8 s) or it kept
the fused order. A model call the gateway holds or refuses fails
the search instead. An LLM-free read (`LOOM_READS_LLM_FREE`) reranks
only as its request names, never with `builtin:openai`. A rerank is one more model call
per search: `builtin:openai` adds about two seconds, so it is off unless asked for.

**Fan-out and private-first.** Under `all_mine` the namespaces are the caller's
memory-kind namespaces (`LOOM_MEMORY_SEARCH_FANOUT_KINDS`; code and tracker indexes are
reached by name or their own tools), up to `LOOM_MEMORY_SEARCH_FANOUT_MAX_NAMESPACES`
besides the addressed one. The addressed namespace and the private ones are searched
first, whatever the addressed one's visibility; public team namespaces are searched only
when no hit in that first pass reaches a similarity of 0.30.

**Collapse and narrowing.** Collapsing keeps the best window of a long section, leaving room for another section.
`narrow_spans` returns one sentence pair instead of a whole section, for one extra batched embed call.

A lexical leg needs corpus statistics; a namespace with none yet (or a failed read)
degrades to the vector leg alone. `loom lexical-stats-rebuild --org O --namespace N`
rebuilds them. A chunk with an empty context (one an older build wrote) is scored on
its text alone until `loom rechunk` rewrites its document; run
`loom lexical-stats-rebuild` after it, since the superseded chunks stay in the
document frequencies until then. Rechunk such documents before changing the
embedding model in Settings → Models & keys too: the provider rebuild embeds a
chunk's stored context and text, and an older chunk's empty context would drop the
context its vector was written with (and a titled document with no text, which has
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
  since following it would read something the answer did not come from. A bracket
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

The server takes text; it parses no file formats. `dev/loom_ingest.py` parses on the client and posts the text:

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

| Setting                                                                             | Effect                                                                                                                                                                              |
| ----------------------------------------------------------------------------------- | ----------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `LOOM_KNOWLEDGE_CHUNK_SIZE`, `…_CHUNK_STRIDE`                                       | How documents are split ([Knowledge → Chunking](../reference/configuration.md#chunking)). `loom rechunk --org O --namespace N --apply` re-chunks existing documents after a change. |
| `LOOM_MEMORY_SEARCH_FANOUT_KINDS`                                                   | Which namespace kinds an `all_mine` search spans ([MemorySearch](../reference/configuration.md#memorysearch)).                                                                      |
| `LOOM_MEMORY_SEARCH_FANOUT_MAX_NAMESPACES`                                          | How many of the caller's namespaces an `all_mine` search reads besides the addressed one ([MemorySearch](../reference/configuration.md#memorysearch)).                              |
| `LOOM_MCP_LOOM_SEARCH_KNOWLEDGE_MIN_SIMILARITY`                                     | The floor for knowledge hits in `loom_search` ([Mcp](../reference/configuration.md#mcp)).                                                                                           |
| `LOOM_MEMORY_ASK_KNOWLEDGE_TOP_K`, `LOOM_MEMORY_ASK_SYNTHESIZE_ROUTE_MAX_DOCS`      | Whether and how `memory.ask` reads knowledge ([MemoryAsk](../reference/configuration.md#memoryask)).                                                                                |
| `LOOM_KNOWLEDGE_SOURCES_MAX_PER_NAMESPACE`, `LOOM_KNOWLEDGE_SOURCE_PROBE_TIMEOUT_S` | How many document sources one repository namespace may register, and how long a source's probe may take ([Knowledge](../reference/configuration.md#knowledge)).                     |
| `LOOM_RETENTION_CHUNKS_DAYS`, `LOOM_RETENTION_DOCUMENTS_DAYS`                       | Opt-in table TTLs; 0 keeps rows forever ([Retention](../reference/configuration.md#retention)).                                                                                     |

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
