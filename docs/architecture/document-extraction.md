# Document extraction

An uploaded original is parsed into one shape, the extraction artifact, by an extractor service: a separate process with
its own dependencies, never the Loom server. The server sends the original's bytes to the service its format is routed
to, reads the artifact back as untrusted input, and cuts structural chunks from its blocks, so a SQL statement, a TOML
key and a Markdown section are all cited by a locator into the source. Loom ships a reference extractor
(`extractor/loom_extractor/`) for Markdown, HTML, plain text, SQL, TOML, PDF, DOCX and PPTX; an operator can run another
behind the same [wire contract](../reference/extractor-contract.md). `knowledge.ingest` extracts an uploaded original
this way ([ingestion](#ingestion)), and the document-source sync calls it for the PDFs and `.pptx` files a registered
folder holds ([syncing a source](../features/knowledge.md#syncing-a-source)); `knowledge.add` takes text the caller
extracted.

## The service boundary

| Side              | Holds                                                                                                                                                                                    |
| ----------------- | ---------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| Loom server       | the contract (`server/knowledge/domain/extract/`), the checks on what an extractor returns, format routing, the structural chunker and the client (`server/knowledge/app/extractors.py`) |
| Extractor service | format detection, strict decoding and every parser; no Loom token, no database credential and no bucket access: it gets bytes and returns JSON                                           |

Parsing a file means running a parser over input an uploader controls. Keeping that out of the server's process keeps a
parser defect, a pathological input or a parser's dependencies away from the process that holds ClickHouse credentials,
signing keys and tenant data, and lets an operator swap in their own utility for a format.
`tests/unit/test_package_contracts.py` checks that no server module imports the extractor, sqlglot or pypdf, and that
the knowledge plane imports no parser library.

**Routing.** The server names the format from what the caller says (a declared format, else the file extension, else the
media type, else `text`) without reading the bytes, and sends the original to the service
`LOOM_DOCUMENT_EXTRACTORS_JSON` names for that format, or for its media type when the media type names that format, else
to `LOOM_DOCUMENT_EXTRACTOR_URL`. A format is extracted only when that service's `/v1/capabilities` declares it; the
declaration is read again after a minute. A `.doc`, `.xls` or `.ppt` name is refused before any call. A route key is a
format name or a media type that names one, matched without its parameters and case; any other key is refused when the
settings load.

**Fail closed.** A format with no configured service, a service that does not declare it, is unreachable, takes longer
than `LOOM_DOCUMENT_EXTRACTOR_TIMEOUT_S` for a whole call, refuses the server's token (`401`, `403`, `407`), answers
`408`, `429`, `format_unavailable`, a `5xx` or anything else that is neither `200` nor `4xx` (a redirect, a `204`)
raises `ExtractorUnavailable`, and the source can be tried again. A source the service refuses raises `ExtractionFailed`
with the contract's code (`format_mismatch`, `encoding`, `too_large`, `timeout`, `resource_limit`, ...), as does a
response that breaks the contract (`invalid_artifact`). There is no in-process fallback: with no extractor configured,
no original is extracted.

**Tokens.** The server sends each service the token `LOOM_DOCUMENT_EXTRACTOR_TOKENS_JSON` holds for its URL, else
`LOOM_DOCUMENT_EXTRACTOR_TOKEN`, so an operator's own service need not hold the token the reference extractor accepts.

**Untrusted output.** The server asks for a response uncompressed (`Accept-Encoding: identity`), treats a compressed one
as the service being unavailable, and reads an artifact up to `LOOM_DOCUMENT_EXTRACTOR_MAX_ARTIFACT_BYTES`, refusing a
longer one as `resource_limit`. Then `parse_artifact` checks it before anything is chunked, embedded or published: the
schema version, the format asked for, each block's type, a unique id and a parent that is an earlier block, at most
10,000 keys of 1,024 characters and 64 section-path entries of 500 characters per block (so the chunks cut from a long
block share its keys and per-chunk work stays linear), locator spans that are ranges, coverage counts within their
total, strings that are valid Unicode, numbers that are JSON (no `NaN` or `Infinity`; signed 64-bit integers (-2^63 to
2^63-1), other numbers within a double) and nesting depth in `attrs` and `options` (16; the reference TOML parser leaves
out the `attrs.value` of a value nested deeper than 8, whose text the block keeps). `complete` is recomputed from the
counts, failures and losses, and an artifact whose flag disagrees is refused. A field the contract does not name is
dropped. The server addresses the artifact by the SHA-256 of its own serialization, not the service's bytes.

**Isolation in the reference extractor.** Every `/v1` call carries `Authorization: Bearer <token>`; the service
refuses to start without a token of at least 16 characters. Each extraction runs in a child process, and before the
child reads the source:

| The child                                | How                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                               |
| ---------------------------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| has no environment                       | started with only `PYTHONPATH`, `PATH` and `LC_ALL`, so the token is not in it                                                                                                                                                                                                                                                                                                                                                                                                                                                                    |
| cannot read the service or another child | the service and every child are non-dumpable (`PR_SET_DUMPABLE`), so a process of the same user cannot read their environment, memory or open files through `/proc` or ptrace                                                                                                                                                                                                                                                                                                                                                                     |
| has bounded resources                    | address space (`LOOM_EXTRACTOR_MEMORY_MB`), CPU seconds, a file size of 0 (it can create empty files, not write to one), no core dump                                                                                                                                                                                                                                                                                                                                                                                                             |
| starts no process                        | `RLIMIT_NPROC` 0, so nothing a parser starts outlives the extraction; a child still running at a timeout, a disconnect or a cancel is killed with its process group before its slot is freed                                                                                                                                                                                                                                                                                                                                                      |
| opens no socket                          | a seccomp filter set after no-new-privileges makes `socket`, `socketpair` and `io_uring_setup` fail with EPERM, and `ptrace`, `process_vm_readv`, `process_vm_writev` and `pidfd_getfd` too (x86_64 and aarch64; the service refuses to start on another architecture), so it reaches no address, the Loom server on its network included. A concurrent sibling stays writable through `/proc/<pid>/mem` until it is non-dumpable, so with `LOOM_EXTRACTOR_CONCURRENCY` above 1 a taken-over child could still patch one before it filters itself |
| gains no privilege on exec               | `PR_SET_NO_NEW_PRIVS` in the service and in each child, so no setuid binary or file capability raises one                                                                                                                                                                                                                                                                                                                                                                                                                                         |
| loads only the package                   | runs in `/` with `-P`, so no current or script directory is on `sys.path`; the package and its dependencies are installed read-only to the service's user, as a file of its own on `sys.path` could change what every later child imports                                                                                                                                                                                                                                                                                                         |
| shows no request                         | its command line holds only its limits; the file's name and options come on stdin                                                                                                                                                                                                                                                                                                                                                                                                                                                                 |

The service kills a child at the wall-clock timeout, when its caller disconnects or when its output passes the artifact
bound, and reads a source only once one of its `LOOM_EXTRACTOR_CONCURRENCY` slots is free, so that bound covers the
originals held in memory too. The slots are one by default: a child is readable by other processes of its user from its
start until it makes itself non-dumpable, so a child a hostile source has taken over could open a concurrent child's
source in that moment. Scale with service replicas, not slots. The kernel does not hold root, or a process with
`CAP_SYS_RESOURCE` or `CAP_SYS_ADMIN`, to the process limit, so the service refuses to start as root or with either
capability permitted. A child is not denied reads of the file system in its process: the container it runs in is what
denies those. The service refuses to start on an architecture its socket filter does not cover. The settings are in
[`extractor/README.md`](../../extractor/README.md).

## The artifact

| Part       | Holds                                                                                                                                                                                                        |
| ---------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------ |
| `blocks`   | the source's structure in order: `heading`, `paragraph`, `list`, `code`, `sql_statement`, `toml_table`, `toml_key`, ...; each with its text, locator, section path and parent                                |
| `coverage` | the unit the format is made of (`lines`, `statements`, `keys`, `chars`, `pages`, `paragraphs`, `slides`), how many there are, how many extracted, each failed unit with its reason, and each capability lost |
| `warnings` | facts a reader should see, such as an ambiguous SQL dialect                                                                                                                                                  |
| `options`  | the overrides the parse ran with (`sql_dialect` for SQL only, `encoding` by its codec name)                                                                                                                  |
| `attrs`    | format facts (`dialect`, `dialect_source`, `dialect_candidates`; an HTML page's or a PDF's `title`; a PDF's `pages`, a deck's `slides`)                                                                      |

A block's `id` is `b<ordinal>`, stable within its artifact. `keys` are the exact names a reader can look the block up
by: the objects a SQL statement creates, writes and reads, and its columns (for a statement sqlglot cannot parse, every
name its tokens hold), every qualified name also by its last part (`events` for `analytics.events`), unquoted
(`my.table` for ``db.`my.table` ``; a quoted part keeps its dots); a TOML table's or key's full path and its index-free
form.

A locator sets only the fields that apply: `line_start`/`line_end` (1-based) on every text format,
`char_start`/`char_end` (offsets into the decoded text) on Markdown, plain text, SQL and the text blocks of an invalid
TOML file, `key_path` for TOML, and `page_start`/`page_end` (1-based) for a PDF's pages and a PPTX deck's slides. A
DOCX has no pages of its own (pagination is the renderer's), so its blocks carry no locator and a citation names their
section path. A Markdown heading's span covers its source line or lines, markup included, while its text is the cleaned
title, so a window of a long heading cites the heading's whole span. A chunk cut from several blocks spans their lines,
characters and pages. A format that needs another field (a cell range) adds it with its parser.

The artifact is serialized as sorted, compact JSON with a `schema_version` and addressed by the SHA-256 of those bytes.
A parser or option change produces a new artifact of the same source revision; an artifact is never edited. A reader
refuses a `schema_version` it does not know. `parser_version` names what a parse depends on: sqlglot's version for SQL,
pypdf's for PDF, and the Python version for HTML and TOML, whose parsers ship with it.

## Formats in the reference extractor

`detect_format` reads the bytes before the file name. A PDF, a DOCX, PPTX or XLSX container (by the parts inside the
zip) and a Parquet file are recognised by content; a name that claims a format the bytes are not is refused
(`format_mismatch`), and a legacy binary Office file is refused (`unsupported_format`), as is a `.doc`, `.xls` or `.ppt`
name unless a format is declared. Text is decoded strictly: UTF-8, UTF-16 or UTF-32 by its byte-order mark, or a
declared encoding, and a UTF-8 byte-order mark is dropped when the text is UTF-8, declared or not, as is the mark of a
declared byte-order codec (`utf-16-le`). An undecodable byte is an `encoding` failure naming its offset, and a NUL byte
in undeclared UTF-8 text (behind a byte-order mark or not) is binary; neither is replaced and indexed.

| Format             | Parser                                                                | Blocks                                                                       |
| ------------------ | --------------------------------------------------------------------- | ---------------------------------------------------------------------------- |
| Markdown           | a block parser after CommonMark's heading, fence and HTML-block rules | headings, paragraphs, lists, fenced code, front matter                       |
| Plain text         | a paragraph splitter                                                  | one paragraph per run of non-blank lines (a `#` line is text, not a heading) |
| HTML               | the standard library's parser                                         | headings, paragraphs, list items, table cells, quotes, definitions, `pre`    |
| SQL                | `sqlglot`, never executed                                             | one block per statement                                                      |
| TOML               | `tomllib` and a line scanner                                          | one block per table header and per key                                       |
| PDF                | `pypdf`, its text layer                                               | paragraphs by page; headings from the outline                                |
| DOCX               | the standard library's zip and XML readers                            | headings, paragraphs, lists, tables, footnotes and endnotes                  |
| PPTX               | the standard library's zip and XML readers                            | per slide: its title as a heading, each text shape, tables, speaker notes    |
| XLSX, CSV, Parquet | none in this build                                                    | none: detected and reported `format_unavailable`                             |

**Markdown.** Headings are ATX, setext and one-line `<hN>`. A paragraph over a setext underline (indented up to three
spaces) is one heading; a list item, a quote or a thematic break over `---` is not. A fenced block keeps its body's
lines without the fences; a fence closes on a bare run of its character at least as long, and its language is the info
string's first word, the rest kept as `info` (an empty block keeps it too). Inside a paragraph only a bullet or an item
numbered 1 starts a list, so a wrapped line that begins `2019.` stays prose. A thematic break (`***`, `- - -`) is
markup: it ends the paragraph or list above it and is no block's text. An HTML block is one paragraph block, as written,
and never a heading's title (CommonMark's start conditions): one opened by a comment, `<?`, `<![CDATA[`, `<!`
(`<!DOCTYPE html>`) or a raw-text tag (`<pre>`, `<script>`, `<style>`, `<textarea>`) ends on the line holding its end
marker (`-->`, `</pre>`), so the line after it can be a setext title; one opened by a block-level tag (`<p
align="center">`, `<div>`) or a line of only tags (`</div>`) ends at a blank line. Each but the tag-only line ends a
paragraph above it, and a tag-only line inside a paragraph is its text. An empty ATX heading (`# #`) is markup that ends
the paragraph above it, while one whose title cleans to nothing (`# <img src="logo.svg">`) stays a paragraph. Leading
YAML front matter (`key:` lines between `---` markers, a `#` at a line's start followed by a blank being a heading, not
a comment) is one block. Blocks carry line and char spans, and coverage counts non-blank lines, every one of which lands
in a block or is a fence or front-matter delimiter, a thematic break or an empty ATX heading (counted from the blocks'
spans, a guard against a parser change that drops a line).

These rules are the extractor's own (`extractor/loom_extractor/markdown.py`). The `knowledge.add` heading splitter,
which sections text a caller extracted, keeps its own rules and can section the same document differently.

A heading's title carries at most its first 500 characters into the section paths under it, a cut marked as the
contract bounds below say; the heading block keeps all of it and is windowed like any block longer than a chunk.

**HTML.** `blockquote`, `dd` and `dt` are paragraphs, and a `<p>` that opens a list item, and the `<p>`s after it, are
the item's text. A block whose end tag is left out closes at the next block, its container's end or the document's end
(a container inside a block leaves it open; `br`, `hr` and a container's start separate words), and an unclosed `head`
closes at the first body element or text. A block's line span runs from its first text line to its last. `script`,
`style`, `template`, `noscript`, `svg`, `title` and `head` are dropped from the blocks (the `<title>` text is the
artifact's `attrs.title`); coverage counts the visible characters outside them and how many the blocks hold, a guard of
the same kind. Neither counts what a block loses within itself, such as stripped inline tags. A hidden element left open
hides the rest of the document; the text inside it is counted as not read, with a failure naming the open elements.

**SQL statements.** A statement is split on the dialect's tokens, so a `;` in a string or comment does not end it. A
client's own command lines (`GO`, a lone `/`, `DELIMITER ;;`, backslash commands) are not SQL: a script that holds
them is split on its `;` tokens all the same, and a statement a command line joins is an `UNKNOWN` block that coverage
records as failed. A body the client sends whole (a procedure's `BEGIN … END`) is cut at its inner `;`: the head keeps
the `CREATE` and its name, each statement of the body is a block of its own with its own reads and writes, and the
closing fragment (`END`) is `UNKNOWN`; a T-SQL body with no `BEGIN … END` can read as complete. Splitting by a client's
own rules is not done; declare the dialect, and expect such scripts to report `sql_structure` lost. Its block holds its
kind (`CREATE MATERIALIZED VIEW` apart from `CREATE VIEW`), the object created, the objects written (a view's `TO`
table, the table an index is built on, what a `DROP` or `TRUNCATE` removes, a `MERGE` target), the tables read (a CTE is
not one), its columns and its comments (one after a `;` on its line belongs to that statement). Coverage counts
statements, not comment-only blocks, and names a failed one by its block id.

**SQL dialects.** A declared dialect is used as given, by its name alone; an unknown one, or one with sqlglot settings
after a comma, is `invalid_option`. Otherwise every candidate dialect reads a window of the script: the whole script
when it is at most 50,000 characters, else a prefix up to its 50th `;` within the first 50,000 characters, and a shorter
prefix when its tokenizer refuses one (a cut can land inside a quoted body). A dialect that tokenizes a longer window
ranks first, so a dialect that refuses the script cannot win on a shorter cut of it; then the larger share of its first
50 statements' text turned into syntax trees wins (a share, so a dialect that splits a quoted body into many small
statements does not win), the first of a tie by a fixed order (ClickHouse first). A tie between dialects that parse
anything is reported as `sql_dialect_ambiguous` with the tied candidates, and the script is split with the first of them
whose tokenizer reads it whole; when none of the dialects that read the most of the script parses anything, the first of
them is used with `dialect_source = "default"` and no candidates. Detection reads at most 50,000 characters per window
and at most about seven windows per dialect, so a script whose first statement is longer, or whose first quoted body
outruns every cut, may be detected wrongly: declare its dialect. A statement the chosen dialect cannot turn into a
syntax tree stays a searchable block of kind `UNKNOWN`, is listed in `coverage.failed` and sets `coverage.lost =
["sql_structure"]`; a script the tokenizer cannot split is one such block.

**TOML.** `tomllib` decides validity, types and values; the scanner beside it finds lines, comments and the original
value text, including values that span lines (multi-line strings, arrays, inline tables). A key block's text is `<full
path> = <value text>` with its comments, written so it reads on its own; its line span points into the source. The
comments above a table or key go into its text and its line span; a run of comments no table or key follows is a
paragraph of its own, and a comment inside a multi-line array stays in that key's text. An invalid file, one with an
integer outside signed 64 bits among them, is kept as paragraph blocks with `coverage.lost = ["toml_types"]` and the
reason, which names the line or the key path where it can. A key's `attrs.value` is the JSON value, except that a date
or time is its ISO text and an infinite or NaN float its text (`inf`, `nan`), whatever its `value_type`.

**PDF.** pypdf reads each page's text layer in its reading order; a page's text is split at blank lines, and a page with
none is one paragraph. The outline (the bookmarks) gives the headings: each entry, in outline order, is a heading block
on the page it points to, at the first line after the previous heading's that holds its title, or right after that
heading when no line does. A table is read as its text, row by row as the page lays it out. A page with no text that
draws an image (an image object, one in a form, or one inline in its content) is a failed unit: its text needs OCR,
which the reference extractor does not run, so a PDF with such a page is not complete and is published only with
`allow_partial`; a scan whose pages carry some text (a stamped number) is read as that text. A page pypdf cannot read is
a failed unit, not the file; a failed page's outline headings still open their sections. A character a font's map gives
no Unicode character (a lone surrogate) is read as U+FFFD, with a warning naming its page. A PDF that opens only with a
password is `unsupported_format`.

**DOCX and PPTX.** Both are zips of XML parts, read with the standard library: no third-party XML or image library runs
on a hostile file. A part must be stored or deflated (the methods OOXML writes) and declare at most 256 MiB
uncompressed; it is inflated only up to what it declares, so a zip bomb stops there. A part that declares a DOCTYPE (in
any encoding) is refused, so no entity is expanded. A part that cannot be read (damaged, over the bound, not XML, in an
encoding the XML parser does not read) fails the unit it belongs to: a slide, a slide's notes, the styles, the
footnotes; the document part itself fails the file as `corrupt`. A DOCX heading is a paragraph of a `Heading N` or
`Title` style, or one with an outline level of its own or along its style's `basedOn` chain, the nearest one set
deciding (level 9, body text, is no heading whatever the style); a numbered paragraph or a `List` style is a list item,
and a run of them is one list block. Inserted text is read and deleted text is not; a content control's or a custom XML
element's text is read in place, in the body, a table's rows and its cells, and an alternate-content choice once.
Headers, footers and comments are not the body and are not read; footnotes and endnotes follow the body (`attrs.note`).
An embedded document (`altChunk`) is a failed unit, as is a document of pictures (in its body or its tables) and no
text. A PPTX deck is read in its slide order (the presentation's slide list, not the parts' names): a slide's title is a
heading its other blocks sit under, each text shape (grouped ones in place, and a shape in alternate content through its
first choice) is a block, and the speaker notes are a block marked `attrs.notes`; a hidden slide (`show` false) is read
and marked `attrs.hidden`. A slide whose pictures, charts, diagrams or embedded objects carry all it shows, with no
text, is a failed unit. So is a slide id whose relationship names no slide part, and a slide nested deeper than the
reader walks. A table, in either format, is one block, a row per line with its cells joined by a pipe, and `attrs.table`
its `rows` and `columns`.

**Contract bounds.** Before it answers, the reference extractor fits every block to the contract's bounds: a key over
1,024 characters is left out and the first 10,000 kept, and the section path keeps its first 64 entries, each cut to 500
characters. A block it cut is marked (`attrs.keys_truncated`, `attrs.section_truncated`) and the artifact's
`coverage.lost` names what it lost (`exact_keys`, `section_path`); the block's text and locator still hold all of it.

## Structural chunks

`chunk_artifact` cuts chunks from blocks instead of fixed windows over flattened text:

- consecutive blocks under one parent (a heading's section, a TOML table with its keys) pack into one chunk up to the
  chunk size;
- a SQL statement is a chunk of its own, so a hit cites one statement;
- a block longer than the chunk size is windowed, and each window keeps its section and, for a block with char
  offsets, its own line and char span (an HTML window, or a valid TOML file's, cites its block's lines);
- heading text is not repeated in chunk text: the section path travels beside it; a heading with no text and no
  subsection under it is a chunk of its own.

Each chunk carries the ids of the blocks it was cut from, their type (`mixed` when they differ), the merged locator of
their span (a field such as `key_path` survives only when every block agrees) and the union of their keys, and records
the document revision and format it was written as.

## Ingestion

`knowledge.ingest` runs a job (`server/knowledge/app/ingest_run.py`, worker `knowledge_ingest`) over an original that
`POST /v1/knowledge.upload` stored. The upload names the format from the file name and media type and never reads the
bytes; the job reads the original and sends it to the extractor through the client above, checks the artifact, stores it
as an original of its own (content-addressed), cuts structural chunks with each chunk's context (`chunk_context`: the
title, description and section breadcrumb), embeds them, reusing the vector of an input this namespace already embedded
under the same model, and publishes the next revision of the source: it decides the revision (and checks an expected
one), inserts the chunks written as it, then, under the source's claim, refuses the publish when the source changed
since the revision was decided, writes the document row and retires the earlier version's chunks. The claim covers those
few statements, not the chunk insert, so its time limits hold for a source of any size. A source the extractor refuses
fails the job with its code; a format no configured extractor takes fails it with `format_unavailable`; an artifact that
breaks the contract fails it with `invalid_artifact`; an extractor that cannot be reached, times out or answers a status
that is neither a result nor a refusal puts the job back with a growing delay (at most an hour) until
`LOOM_KNOWLEDGE_INGEST_MAX_ATTEMPTS`. Nothing is published before every slow step has run, so a failed or cancelled job
leaves the earlier revision serving. The job row records the chunk version a publish inserts before it writes the
document row; when a run stops between the two, its retry, its failure or its next run retires those chunks. The
document row names the job that wrote it (`ingestion_id`, kept by a metadata edit and a delete) and a rerun looks for
any version row of the source that names it, so a text write since does not hide it, and a run that stopped mid-publish
(still `processing` at the publish stage, which is saved with its chunk version), when a later write of the source (a
text write, a metadata edit or a delete) has landed since and the job's row is not found (a merge may have folded it
away), fails `revision_conflict` rather than perhaps publish its older file over that write; a job a retry put back to
pending is not judged so. A document insert that failed transiently (its own timeout, or the source claim's deadline
cutting it off) may still land, so the run stops there as a crash would and the job stays `processing` at its publish
stage, its chunks live: the run leaves the job's lease to expire rather than release it, so no worker takes the job
until the lease's TTL has run out on the server's clock, past the insert's own bound (a carried send's start window, a
claimed statement's server-side limit and a commit margin), and by then the insert has landed or been stopped: the next
run finishes it published when its row landed (whatever was written after it: a delete or a text write stands), fails it
as overtaken as above, or retires the chunks; a rerun of a job that published finishes without publishing again or
retiring the chunks it published. A cancel is a row of its own (`loom.knowledge_ingest_cancels`), never a version of the
job's, which only the worker holding the job's lease writes; the worker reads it before the publish and when it takes
the job, and a job whose own publish is found ends published whatever cancel came after. A pass runs one job under
`LOOM_KNOWLEDGE_INGEST_MAX_PASS_SECONDS`, taking one source's jobs one at a time in the order they were asked (only the
oldest open job of a source is offered, a stale one before any later one), and namespaces in turn (the one served
longest ago first), and a run cut off at that deadline or by a stopped process counts toward the job's attempts.

## Dependencies

The reference extractor's parsers need `sqlglot` for SQL, `pypdf` (and `cryptography`, for a PDF encrypted with AES) for
PDF, and the standard library otherwise. They are installed with the service (`extractor/pyproject.toml`), and in a
development environment by the `extractor` extra; the server's image carries none of them. Its image
(`extractor/Dockerfile`, `ghcr.io/clickhouse/loom-extractor`) installs the package and its locked dependencies
(`extractor/uv.lock`) into a venv root owns and runs the service as a user that owns nothing in it, so no file on a
child's `sys.path` is writable to the child. The compose stack and the Helm chart run it apart from the server: Compose
on an internal network shared with the server alone, the chart behind a NetworkPolicy that admits only Loom's pods and
allows no egress, with no credential but its token; on either, an extraction makes no socket while it runs alone
(`LOOM_EXTRACTOR_CONCURRENCY` 1, the default) ([deployment](../operations/deployment.md#container-images),
[Kubernetes](../operations/kubernetes.md#document-extractor)).

## Invariants

- A text format's content lands in blocks; what a parser could not structure is recorded in `coverage` (failed units,
  lost capabilities), never dropped in silence. Only the HTML elements listed above are left out, by design.
- A block's locator points at the source text it came from.
- An artifact is addressed by the SHA-256 of its bytes and never edited; a parser change bumps that parser's
  `PARSER_VERSION`, so its output is a new artifact.
- Extraction never executes what it parses.
- The server parses no original: every format goes to an extractor service, and nothing it returns is used before
  `parse_artifact` accepts it.

## Source entry points

| What                       | Where                                                                                      |
| -------------------------- | ------------------------------------------------------------------------------------------ |
| The artifact               | `server/knowledge/domain/extract/artifact.py` (`Artifact`, `Block`, `Locator`, `Coverage`) |
| Checks on a response       | `server/knowledge/domain/extract/validate.py` (`parse_artifact`)                           |
| Format routing             | `server/core/document_formats.py` (`format_of`)                                            |
| Calls to extractors        | `server/knowledge/app/extractors.py` (`extract`, `available_formats`)                      |
| Structural chunks          | `server/knowledge/domain/extract/structural.py` (`chunk_artifact`)                         |
| The reference service      | `extractor/loom_extractor/service.py`, one extraction per `child.py` process               |
| Format detection, decoding | `extractor/loom_extractor/source.py` (`detect_format`, `decode`, `extract`)                |
| SQL                        | `extractor/loom_extractor/sql.py` (`extract_sql`, `detect_dialect`)                        |
| TOML                       | `extractor/loom_extractor/tomlfile.py`                                                     |
| Markdown, plain text, HTML | `extractor/loom_extractor/text.py`, heading rules in `markdown.py`                         |
