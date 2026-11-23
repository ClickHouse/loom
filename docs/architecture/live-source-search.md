# Live connected-source search

`search.query(live=true)` adds an ephemeral section from the asker's own approved
MCP source registrations in live mode. The default is false. The common
`live_search` path retains indexed retrieval, ranking, withholding and paging;
selected remote results join only the final response. Live retrieval never
imports a document or saves a memory automatically.

## Authority and publication

The leg requires authentication, mirrored sources enabled, the caller's supported
session-protection promise, a bound request work bag and an active required
request tail. It marks the logical request and HTTP owner before private account
discovery. Account and remote work runs under private diagnostics. A mark
classifies the whole response, including an attempted read that times out; it
does not establish client readiness.

The canonical requester selects only active, owner-bound MCP registrations in
live mode within the search's authorized namespaces. Another person's connection
and retained unsupported native sources are excluded. Current account generation,
owner standing, approved profile, source scope and destination authority remain
required throughout the read. A destination grant does not borrow an owner's
connection.

Each returned hit retains its exact selected resource identity and successful-read
receipt. Final checks rediscover the approved capability and re-read retained
resources, comparing their content digest and title. A changed source, connection,
selection or resource cannot authorize replacement text. The common request-tail
publication barrier retains these checks after required accounting and refuses
unchecked private bytes at delivery. These checks are current observations, not
an atomic lock on a remote document.

An optional live contribution that fails before selection is omitted, leaving
indexed sections available. Once a selected response frame retains private
contributions, a failed final publication check refuses that frame before its
private bytes are sent.

## Work bounds and coverage

One common deadline covers discovery, connection admission, mapped search,
selected reads and assembly. The existing `knowledge_live_search_*` settings
bound the deadline, eligible registration count, hit count and response bytes.
Trusted MCP profile bounds also constrain protocol calls, pages, content bytes
and work. Configuration grants no wider selection or additional operation.

A live search needs an approved declarative search mapping and a supported read
capability. It reads one bounded search page, intersects returned identities with
the saved selection, drops duplicates and reads each admitted resource before
returning content. A resource-only server can support direct selected reads
without offering mapped search. Browse and search windows are not complete
catalogs; there is no recursive expansion, inferred upstream ACL or deletion
claim. Missing hits never authorize content retirement.

Live search runs only on the first page, without tags, for queries of at most
1,000 characters. It ignores memory retrieval recipes. An unavailable or
timed-out optional leg contributes no live section. Cancellation or failure of
indexed search cancels and joins its owned live task. There is no native provider
execution or OAuth fallback.

## Content boundary

The separate `live` section contains bounded identifiers, titles, local source-copy
citations and optional excerpts, with `live=true` on the section and each hit.
Those citations identify the registration and exact selected resource; they are
not upstream URLs or fetch targets. `bodies=none` omits excerpts. HTTP, SDK and MCP
responses retain server-owned mirrored classification and protected-client
handling.

Remote data does not enter indexed rankers, search traces, page tokens, served
memory keys, gap contributions, learnings, snapshots, offloads or model prompts.
A live answer suppresses the unanswered-query contribution explicitly. Required
numeric private request accounting contains no result identity, citation, title
or content. The operation remains a read without response replay storage. This
path creates no persistent live-result cache, stored content or erasure substrate.
Remote hosts and participating clients retain their own requests and conversations
under their respective policies.

## Source entry points

- `server/knowledge/app/live_search.py`: common selection, deadline and publication.
- `server/knowledge/app/mcp_live.py`: approved mapped search, selected reads and
  retained resource checks.
- `server/knowledge/app/mcp_source_sessions.py`: checked connection sessions and
  observed schema refusal.

## Invariants

A live hit belongs to the current saved selection and is read before publication.
No model argument widens approval, account or selection. Final protected
publication applies to the entire selected response frame. Live lookup creates no
import, automatic memory write or deletion evidence.

Connection and deployment protection follow
[MCP document sources](mcp-document-sources.md) and the generic
[document-source boundary](document-sources.md). The user-visible response contract
is in [unified search](../features/unified-search.md). Synthetic protocol tests do
not qualify an external service or every client application.
