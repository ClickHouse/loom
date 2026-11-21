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
Replacement of a completed whole-page unit is unsupported. Exact whole-page reuse remains
possible, but the API does not resolve every changed-page mapping.

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
- `scripts/i18n/chunks.py`: protected ranges, contextual source cuts and parsed table cells.
- `scripts/i18n/context.py`: source spelling declarations and residual label projection.
- `scripts/i18n/requirements.in`: translation runtime dependencies.
