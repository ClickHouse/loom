# Live connected-source search

`search.query(live=true)` adds an ephemeral section from the asker's own connected
Drive and Notion registrations. The default is false. Indexed retrieval, ranking,
withholding and paging keep their existing paths; provider results join only the
final response. No provider result becomes an indexed hit or an ingestion request.
Registered Linear document projects use the same live section.

## Authority and publication

The leg requires authenticated operation, mirrored sources enabled, the caller's
existing session-protection promise, a bound request work bag and an active
required request tail. It marks the logical request and HTTP owner before private
account discovery. Account and provider work runs under private diagnostics. A
mark classifies the whole response, including an attempted read that times out;
it does not establish a client protection promise.

The canonical requester selects only active connected registrations in the
search's authorized content namespaces. Shared ingestion credentials and another
person's registrations are excluded. The connected-account resolver checks the
selected source identifier and retains its account generation, owner, corpus and
requester. Provider authorization uses its checked access object, without exposing
credential material to response assembly.

Each selected hit retains its original authority and native corpus membership.
Both are checked before assembly and through the existing request-tail publication
barrier after accounting. Revocation, source replacement, moved items or expired
evidence cannot authorize replacement results. A failed final publication check
refuses the response before its private bytes are sent. These are current read
checks, not an atomic lock on a remote document.

## Work bounds and coverage

One five-second deadline covers account discovery, credential checkout, native
search and final assembly. The leg considers at most three eligible registrations
in namespace/name order and returns at most ten hits in total. Each provider
response is capped at the smaller of 256 KiB and the configured source response
limit. There are no retries or pagination.

The `knowledge_live_search_*` settings can reduce the deadline, registration and
hit counts, response bytes, source item count and Notion ancestor count. Their
defaults are these ceilings; configuration cannot increase them.

Drive uses native full-text search constrained to registered folders, shared
drives or filters. At most four source items are searched. Folder searches cover
direct children even when a registration permits recursive ingestion; explicit
file items have no native file-id search predicate and do not widen the search.
Notion uses native title search and accepts pages only after checking registered
page or database membership, following at most three ancestors. Incomplete
coverage is not evidence that a document does not exist.

Linear uses native document search filtered to at most four registered project
identifiers. It excludes archived documents and comment matches, requests one
bounded page, and checks each document's returned project, archive and trash
state. Final membership checks fetch that document's current state without
replacing the retained title or URL. A missing project identifier never widens
the request to the workspace. Ingestion project slugs alone do not authorize a
live search; the registration must contain a project UUID.

Live search runs only on the first page, without tags, for queries of at most
1,000 characters. It ignores memory retrieval recipes. An unavailable or timed-out
leg contributes no live section and leaves indexed sections unchanged. Cancellation
or failure of indexed search cancels and joins its owned live task.

## Content boundary

The separate `live` section contains bounded identifiers, titles, URLs, provider
names and optional excerpts, with `live=true` on the section and each hit.
`bodies=none` omits excerpts. HTTP, SDK and MCP responses retain the existing
server-owned mirrored classification and protected-client handling.

Provider data does not enter indexed rankers, search traces, page tokens, served
memory keys, gap contributions, learnings, snapshots, offloads or model prompts.
A live answer suppresses the unanswered-query contribution explicitly. Existing
numeric private request accounting remains required; it contains no live result
identifier, URL, title or content. The operation remains a read, so response replay
storage is not enabled. This path creates no table, stored content or erasure
substrate.

Connected-account authority and deployment protection follow
[document sources](document-sources.md). The user-visible response contract is in
[unified search](../features/unified-search.md).
