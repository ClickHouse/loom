# Documentation translation requests

The translation generator splits prepared English text into bounded requests, then checks
the assembled translation before writing it. English remains the source of truth.

## Request planning

`english_requests` assigns heading anchors with one page-wide counter. It prefers safe
second-level heading cuts and packs oversized sections into source slices of at most
16 KiB. The slices reconstruct the anchored English exactly when joined with newlines.

The planner uses CommonMark token source ranges for list items, code blocks and table rows.
Fenced and indented code, inline code, complete HTML elements and comments remain together.
A table header stays with its delimiter. An indivisible oversized unit refuses before the
page's first model call. Cost estimation uses the same request list as translation.

The site's list extension accepts two-space continuations, including ordered items.
Its parser has no source maps, so an indentation scan protects those items alongside
the CommonMark ranges. Both dialects' items remain indivisible.

Some site-extension tables are indented code in CommonMark. The code range remains
indivisible, so an oversized block with that ambiguity refuses before provider admission.
Moving the table outside its extension container resolves the ambiguity in English.

## Original-unit reconciliation

`reconcile_completed_units` is a pure planning API for retaining completed requests across
English edits. It accepts historical and current `PreparedSource` containers and a complete
`OriginalUnit` manifest with its digest. Both containers include prepared links and
page-wide anchors. Every original range and source hash must match the historical
container; gaps between ranges contain exactly an empty string or LF. The manifest digest
binds this metadata, including each selected reply reference.

The caller authenticates the complete original plan and completion statuses against its
request ledger, cache and selected reply receipts. A missing cache file proves nothing.
An original unit with `reply_ref=None` explicitly attests no completed reply after uncertain
attempts are resolved. A completed reply with a confirmed fault keeps its reference and a
`UnitFault`; it cannot become unfinished work by omitting that reference. The caller also
supplies refusals for the entire locale/source pair across source versions.

The plan retains only unique, monotone, byte-identical whole completed units at parser-safe
current boundaries. A payload may own its final newline or leave one LF to assembly;
contiguous payloads remain contiguous. Each disposition carries its current `SourceContext`,
original provenance, fault identifiers and exact separators. Completed units can exceed
the request bound when reused without a provider call. Unfinished gaps and inserted English
use the bounded planner outside retained ranges. `chunks.split` accepts a `source_context`
so a headerless table slice uses the complete current table's row boundaries.

Dispositions distinguish reuse, same-input correction, changed input and unfinished work.
Source edits and faults affecting one original unit share one changed-input disposition.
Repeated candidates, overlapping or reordered ranges, unsafe boundaries, unknown faults
and pair refusals return conflicts. A changed completed unit among several unmatched
original units needs proven counterpart cuts; the planner does not guess a partition.
Changing a completed whole-page unit requires the raw source-origin proof below.
Exact whole-page reuse and same-input correction do not require that proof.

`SourceOrigins` optionally supplies the historical and current raw source text and
language. The caller authenticates those Git bytes and their association with the exact
paid requests, locale, model and prepared containers. The planner computes counterpart
ranges; it does not accept a table of asserted cuts. This path requires every original
unit to have a completed reference and leaves the no-evidence refusal unchanged.

Both prepared containers must reconstruct exactly through the maintained language-bar
placeholder, ordered source-associated link routes and generated heading anchors.
External links, fragments, image kinds and occurrence order cannot be substituted.
Historical raw lines that still occur in the current source must retain their order
and multiplicity. A line absent from the current source can change inside a whole
counterpart only when each endpoint's neighbouring lines remain present and have unique
monotone origins. Earliest and latest embeddings of retained lines must agree at every
endpoint. Replacing an endpoint line or inserting text at an interior unit or separator
boundary refuses; the planner does not assign that text to a neighbouring request.

A terminal append can extend the final original whole when both raw and prepared old
containers are exact proper prefixes of their current containers. The final original
must already own the last heading section and end at historical EOF. Only closed,
unindented, brace-free ordinary paragraphs under that heading may be appended. Added
headings, lists, tables, code blocks, HTML, reference definitions and ambiguous
site-extension or language-bar context refuse. Braces refuse even in literal prose
because site attributes can appear inline or without a colon.
Existing block content, every earlier cut and every original separator retain their
proof. This terminal ownership proof maps only the final end to current EOF, including
when historical EOF has repeated blank lines; it never relaxes an interior origin.
The enlarged whole is changed input with the same original reference and current USER
only. An already completed current input still requires its own origin and normal reuse
eligibility. No added request, new historical slot or old-assistant replay is inferred.
All whole counterparts and their original separators must cover the complete current
container. For this terminal extension, consumers must supply the authenticated raw
sources even when every old whole remains a substring of the current container.
Ambiguous origins, reordered retained lines, incomplete originals and unsafe current
parser cuts refuse. This proof is conservative: a replacement can still refuse when
its text also occurs elsewhere and prevents unique ordered correspondence.

Changed whole counterparts retain their original references and fault identifiers and
receive the `changed` disposition. Routing-only input changes also remain changed;
canonical link equivalence grants no free reuse or old-assistant replay. The existing
request-byte bound still applies to each whole changed or faulty counterpart. Raw origin
proof does not establish historical settlement, clear a refusal, approve a translation
or admit a provider call. Consumers retain the source evidence with their execution packet.

Both `chunks.split` and `reconcile_completed_units` accept `bounded_new_request_bytes`,
an integer from 1 to 16384, excluding booleans. The default preserves the 16 KiB cuts.
The bound applies to every prospective request, including changed-input and same-input
corrections. Reused completed units remain whole even above that bound. A changed or faulty
completed unit that cannot fit returns a conflict rather than being split.

A lower bound can refine unfinished work after a truncated reply. The caller authenticates
the settled request, source range, reply and usage, all completion and refusal statuses,
and subsequent ledger entries. Its execution packet binds the selected bound and exact
current plan, including counterpart ranges and retained completed references. A bound
alone does not establish this evidence, clear a hold or admit a call. A truncated response
prefix is not a completed unit, and smaller input does not guarantee a complete reply.

`translate_text` accepts trusted `initial_feedback` with a current source context for
changed-input constraints. `request_system` includes that exact feedback in the canonical
SYSTEM sent to the provider, so the actual request payload binds the constraints. Feedback
is limited to 4096 bytes of printable ASCII and LF; empty feedback preserves the default
SYSTEM. The first message contains only current English. No old-input assistant response
is replayed; a mechanical retry may use only the response returned for the current input.
Invalid feedback or a mismatched source context refuses before spending or a provider call.

A reconciliation plan does not resolve replies, admit provider calls or write output.
Consumers authenticate resolved replies and validate them against their current contexts,
then validate the complete assembled page. Content invariants and both table dialects still
apply. Publication also requires semantic and language-completeness review against current
English; structural success does not clear confirmed faults or refusals.

## Completed histories

`reconcile_completed_history` plans from multiple genuine source partitions without
reassigning a retained reply to a later container. A `RequestPartition` records the
complete actual planned ranges, hashes and empty or LF separators. It makes no
completion or absence claim. Each `CompletionOrigin` identifies one actual reply variant
and its whole slot in its own partition. A reused slot in a later execution creates no
additional origin. Reply references are opaque variant identities; a request key alone
is insufficient when it can identify different reply bytes.

The caller authenticates every partition and the complete same-pair catalog against
actual source, payload, default settings, reply and settlement evidence. It supplies
explicit reuse eligibility for every catalog entry and binds the full metadata digest.
The planner cannot establish that remote evidence or detect a self-consistent fabricated
catalog. Consumers must account for unselected variants, uncertainty, partial attempts,
refusals and security policy before execution. Nonselection clears no hold or charge.

Selected reply references supply no current offsets. Unique exact whole matches or the
maintained per-partition raw-origin proof determine their counterparts. Supplied origin
evidence must validate even when exact reuse is possible. Selected counterparts must
cover all current English in order without overlap, gaps or separator changes. This
completed-only API refuses unfinished gaps and never splits a completed request. The
single-history API retains its separate unfinished-work contract.

Same-input faults require the exact reply variant and produce correction; changed input
produces changed and cannot replay an old-input assistant. An eligible same-input
completion prevents correction spending. An already completed current input must select
its actual origin rather than relabel an older input as changed. All partitions, replies,
eligibility and supplied faults remain in the result, including unselected history.
Whole correction and changed requests obey the existing byte bound; unchanged reuse can
exceed it. Matching generation-aware consumers must authenticate each selected origin
against its own container and complete manifest; a plan alone admits no provider call.

## Assembled validation

The assembled page passes the existing content invariants and table checks before its
output directory or file is created. Table checks use both the documentation site's
Markdown parser and CommonMark with its table extension: their container rules differ.
They retain table groups, rows, cells, alignment and whether a cell is empty. Site list,
admonition and code extensions participate in the check.
Cell text is measured after serialization and raw HTML restoration so empty markup cannot
hide missing text and escaped literal tags remain text.

The parser packages are pinned in the translation requirements and development extra.
The site parser pin matches the documentation builder. A source-byte limit does not
guarantee that a translated reply fits the output-token limit. Truncated replies remain
failures, and structural checks do not establish translation quality or prose completeness.

## Source spelling contracts

Label validation distinguishes source spelling from ordinary descriptions. Source-specific
name declarations protect exact product and project spans. A filename label must match
a tracked repository file at its source link target; the conventional `README` label may omit the `.md` suffix.
Operation field declarations protect returned string vocabularies and one documented
field formula. A string-typed field whose whole description is quoted identifier
alternatives also declares literal values. Quotation, capitalization and filename-like
spelling alone grant no exemption.

These declarations require source evidence: package or project identities, explicit
terminology, real file targets, or the operation's wire contract. Changes to those source
declarations require checking the attestation and its focused regression cases. Ordinary
qualifiers around a protected span remain subject to label validation.

The existing site and CommonMark parsers supply table cells, including escaped and inline
code pipes. Each bounded request is checked in its complete English container, without
sending that surrounding text to the provider. Site-only cells without source maps are
checked at page assembly. Retries, assembly and stored-page checks share the same rule:
protected spans must retain their spelling, occurrence order and source label or field.
This is a finite source contract, not a language classifier or a semantic review.

## Invariants

- Every source slice sent to a provider stays within the bound; all source bytes and
  page-wide anchors survive assembly. Reused completed units retain their whole payloads.
- An indivisible unit refuses before provider admission.
- Provider settings, output-token limits, retries and cumulative financial checks govern
  every request independently of how the page is split.
- Output is written only after assembled validation succeeds.

## Source entry points

- `scripts/i18n/translate.py`: `english_requests`, `request_chars`, `translate_page`.
- `scripts/i18n/translate.py`: `request_system`, `translate_text` and initial feedback.
- `scripts/i18n/reconcile.py`: `reconcile_completed_units`, `OriginalUnit` and manifest binding.
- `scripts/i18n/completed_history.py`: `reconcile_completed_history` and actual reply origins.
- `scripts/i18n/origins.py`: `SourceOrigins` and complete source-origin correspondence.
- `scripts/i18n/chunks.py`: protected ranges, contextual source cuts and parsed table cells.
- `scripts/i18n/context.py`: source spelling declarations and residual label projection.
- `scripts/i18n/requirements.in`: translation runtime dependencies.
