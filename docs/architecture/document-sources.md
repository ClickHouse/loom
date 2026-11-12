# Document sources

A document source pulls files from a document store (a shared-folder store, a page
store, a project tracker, a meeting store) into a namespace's documents and keeps them
in step, on any shared namespace or collection. This page is the design of sources of
many items given as pasted links, per-user connected accounts, an org-wide crawl, and
access that follows the store's own permissions. The caller's view of today's sources is
in [knowledge: document sources](../features/knowledge.md#document-sources); parsing is
in [document extraction](document-extraction.md).

## What changes

| Today                                                                                                  | With this design                                                                                                                                                                                                |
| ------------------------------------------------------------------------------------------------------ | --------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| A source holds 1 to 20 folders, projects, pages or databases, and no op changes its `config`.          | A source holds up to 300 items of mixed types, given as links or ids, and `knowledge.source.update` adds and removes them ([items](#items-and-links)).                                                          |
| One shared credential per source, typed by an admin.                                                   | A shared credential, a person's own connected account, or a delegated service account that reads many people's drives ([credentials](#credentials)).                                                            |
| Every reader of the namespace reads every synced document.                                             | A source's `access` is `namespace` or `mirrored`; a mirrored document is shown only to a reader the store says may read it ([access](#access-follows-the-source)).                                              |
| A `linear` source reads project documents only.                                                        | Issues and comments are a later design of their own ([issues](#project-tracker-issues)).                                                                                                                        |
| A `recording` source reads meeting-store transcripts (`meet`) or cloud-recording transcripts (`zoom`). | Meeting notes and transcripts that the meeting store saves to the shared-folder store come in through a `gdrive` source; `recording` keeps `zoom` only ([meetings](#meetings-through-the-shared-folder-store)). |

## Any namespace

A source can be registered on any active shared namespace or collection; who may
register which kind is in [credentials](#credentials). `add` on an inactive namespace
answers `NAMESPACE_INACTIVE`, and `add` and `update` answer `ERASURE_RUNNING` (409,
retryable) while an erasure that will still run names the namespace, so no source writes
into a mirror the erasure is clearing. A source on a repository's trunk code namespace
still tags each document `repo:<id>`, so the repository's `code.context` and
`loom_search` legs read them as today; on any other namespace, a branch namespace
included, it does not. A person's own place (a `col-me-…` collection, a `personal-…` or
`u-…` namespace, a namespace claimed at sign-up) holds no source (`PERSONAL_NAMESPACE`):
a `user_id` erasure erases what the person wrote, and a source's documents are the sync
worker's. The sync skips a source whose namespace is inactive; the cleanup of a stopped
source runs whatever its namespace's state.

A source ends with its namespace: `code.repo.remove`, `namespace.delete`,
`collection.delete` and a namespace erasure each retire the namespace's sources under
its source lease, clearing their credentials, as `code.repo.remove` does today, so no
source outlives its namespace with a sealed credential or writes back what an erasure
took.

A source's sync state is keyed by its namespace and name, as today, and a mirrored
source's by its mirror and source id, so the namespace's erasure covers it and a later
source of the same name resumes none of an earlier one's listing. It lives where
[storage layout](storage-layout.md#cursors) places it (`loom.code_index_state` today,
Postgres `cursors` from wave 3).

On the dashboard, the Library's Sources button opens the sources of the namespace or
collection in view; the repository card keeps its own.

## Items and links

A source's items are its things to read, of mixed types:

| Kind     | Item `type`        | Fields                                                                                                            | Reads                                                                                                                                               |
| -------- | ------------------ | ----------------------------------------------------------------------------------------------------------------- | --------------------------------------------------------------------------------------------------------------------------------------------------- |
| `gdrive` | `folder`           | `id`, `recursive` (default `false`)                                                                               | the folder's files; with `recursive`, every subfolder's too, to a depth of 8 and at most 500 folders per item (both not settings)                   |
| `gdrive` | `file`             | `id`                                                                                                              | one file of a kind `include` names                                                                                                                  |
| `gdrive` | `shared_drive`     | `id`                                                                                                              | every file of a shared drive                                                                                                                        |
| `gdrive` | `filter`           | any of `name_contains`, `kinds`, `owner` (an email), `modified_after` (a date), and optionally `in` (a folder id) | the files that match, as a query the server builds                                                                                                  |
| `gdrive` | `meet_recordings`  | none                                                                                                              | the meeting store's recordings folder of each account a `connected` or `crawl` source reads ([meetings](#meetings-through-the-shared-folder-store)) |
| `notion` | `page`, `database` | `id`                                                                                                              | as today's `page_ids` and `database_ids`                                                                                                            |
| `linear` | `project`          | `id`                                                                                                              | the project's documents, as today's `project_ids`                                                                                                   |

Every id keeps today's alphabet check, since an id goes into a store query or a request
path. A `filter` never takes a raw query string: each field has a pattern (a name
fragment of printable characters, an email, an ISO date, `kinds` from the `include`
table), quotes and backslashes are escaped, and `server/clients/gdrive.py` builds the
query, so a caller cannot widen a listing past what the fields say. `include` stays the
source-wide file-kind filter. `meet_recordings` is refused on a `shared` source, whose
service account has no recordings folder of its own.

A source holds at most 300 items (a wire contract, not a setting), and at most
`LOOM_KNOWLEDGE_SOURCE_MAX_DOCUMENTS` documents (default 50,000: today's bound on the
read of a source's stored copies, made a setting). The bound counts the source's stored
documents plus the new files a listing finds. A turn over it (a listing grown past it,
an item swap whose new files are listed before the old copies retire, or a lowered
setting) holds back new documents only: it finishes its listing, rewrites changed lists,
retires, and lands its listing row, so its copies drain back under the bound and no list
goes stale; `inspect` and `loom doctor` show `too_many_documents` meanwhile, with the
count, for a `namespace` source; for a mirrored source, its connected owner sees the
code without the count, and a platform admin sees both (exception 4). The read of a
source's stored copies goes in pages by `source_uri`, so it never cuts one off. A
listing asks the store about at most 50 parent folders per query (not a setting), so a
source of many folders lists in several queries; a recursive folder's subfolders are
walked on every full listing, and a file in a new subfolder arrives with the next one.

A probe checks a bounded sample: every item while there are at most 20, otherwise the 20
items added last, and the answer says `sampled: true` with the number checked, which is
shown wherever the item count is.

On a mirrored source, the probe and the status hold back what depends on what a person
can read:

- A mirrored source's probe answers reachability, the number of items checked and codes,
  never a count, a sample of titles or a summary that names a file, since it lists as
  the owner or as crawled people; which items were checked goes only to a connected
  source's owner and a platform admin.
- To anyone else, a mirrored source's probe, status and `sync_now` answer only
  source-level codes, whether the credential and its scopes work and `owner_inactive`,
  and depend only on source-level conditions, so a held-back code reads as a clean sync.
  `rate_limited`, which grows with how much a source lists, an item refusal, a per-file
  or per-item code (`item <n>: http_404`, `meet_recordings: not found`, `too_large`,
  `extraction_failed:<code>`), the oldest listing start and every last-sync time go only
  to the owner and a platform admin.
- A refused item of the sample refuses `add` or `update` only on a `namespace` source
  and on a connected source probed by its owner; on any other mirrored probe, a platform
  admin's included, items never refuse, and only the source-level codes do. The next
  sync turn reports a refused item as `item <n>: http_404`, to those who see per-item
  codes, and goes on with the rest.

### The link parser

`server/knowledge/domain/source_links.py` turns a pasted link into an item, with no
request to the store:

| Link                                                                                  | Item                                    |
| ------------------------------------------------------------------------------------- | --------------------------------------- |
| `https://drive.google.com/drive/folders/<id>`, with or without `u/<n>/`               | `gdrive` `folder`                       |
| `https://drive.google.com/file/d/<id>/…`, `…/open?id=<id>`                            | `gdrive` `file`                         |
| `https://docs.google.com/document/d/<id>/…`, `…/presentation/d/<id>/…`                | `gdrive` `file`                         |
| `https://www.notion.so/<slug>-<32 hex>`, `https://www.notion.so/<workspace>/<32 hex>` | `notion` `page` (`database` with `?v=`) |
| `https://linear.app/<workspace>/project/<slug>-<id>`                                  | `linear` `project`                      |

A shared drive's own link has the folder form; the probe reads the id and turns a
`folder` item that names a shared drive into a `shared_drive` one. A link of another
host, with no id, or to something the sync does not read (a spreadsheet, a single issue)
answers `unreadable` with the reason. The parser takes a list and answers each link, so
a paste of fifty links shows which three it could not read. It is
`knowledge.source.parse_links` (viewer; no store request, nothing written), used by the
dashboard's preview and by agents. A page link that turns out to be a database is
reported by the probe. A project link carries the project's short id, which the probe
resolves to the UUID the store's API takes.

### Wire shape

`knowledge.source.add` takes `items`, or `links` it parses (an unreadable link refuses
the call with each reason, `BAD_LINK`), and still takes `config`, which it turns into
items (`folder_ids` into `folder` items, and so on), so an N-1 client keeps working.
`list` and `inspect` answer `items`, and `config` in today's form when the items can be
written in it, else `{}`; a mirrored source answers `config` `{}` to everyone, and its
audit rows carry every field of its items, a filter's `owner` included, and a crawl's
`subjects`, as keyed hashes only (below).

`knowledge.source.update` takes `add_items`, `remove_items` (each also as links) and a
new `include`, probes what it adds with the stored credential (the same sample rule),
and writes the source at its next version under the namespace's source lease. Its audit
row, and `add`'s, names the namespace, the name and the items added and removed, with a
`filter`'s `owner` email, a crawl's `subjects` and every field of a mirrored source's
items as keyed hashes (below). Removing every item is refused (`BAD_CONFIG`); `remove`
ends a source. Changing `access` is a new source. For a `crawl`, `update` also takes
`subjects` (the new list, probed like added items) and `directory_subject`. Every
`update` and `rotate` of a crawl needs the same roles as `add`, since its credential
reads every listed person's drive: a namespace admin without an org-wide role cannot
change whose drives it reads, what it reads of them (items, `include`) or the key it
reads with.

### One document, one owner

In a namespace, one document per address, as today, owned by one source: the source
whose `source:<name>` tag it carries, written by the sync worker (today's ownership
check). Only the owner rewrites the document when its file changes and retires it on
positive evidence (today's retirement rule), and a document another writer (a person, an
agent) wrote is never overwritten or retired by any source, as today. A mirrored
document also carries `source-id:<id>`, the id of the source that wrote it, and in a
mirror that tag, not the name, decides ownership: a later source of the same name owns
none of an earlier one's documents, and the cleanup of a stopped mirrored source retires
by it. Only N writes a mirror, so every mirror document carries the tag. Inside a mirror
a document's `source_uri` is its address with the source id appended
(`<address>#<source id>`; the address is the part before the last `#`, and citations and
answers use it), so each mirrored source writes its own copy with its own list under the
table's key, heads and document machinery as they are, and no source's write or
retirement touches another's copy. The legs answer the address itself, dropping
duplicates after the check ([below](#the-mirror)), and a `get` or `download` of an
address answers the namespace copy when the reader may see it, else the newest mirror
copy the reader may see. Only the owning source's turn writes a mirror document's list;
any other writer of a mirror row (a retention or tier move, a sweep, a reindex) writes a
tombstone or `unread`, never a list it carried. Only those writers and the erasure write
rows under a mirror's name; no request does. How a stopped source's documents and rows
are found and cleaned when several sources, builds or names meet is the implementing
pull request's to specify.

In a namespace, a second source that lists a file at an address where a document is
counts it `skipped`, as today, and fetches no content. No source reads whether another
is live: an address is free exactly when no document is there. Once the owner's document
is retired (by today's [retirement rule](../features/knowledge.md#document-sources), its
`remove` with `delete_documents`), the next source that lists the file writes it as a
new file under its own tag at its next full listing
(`knowledge_source_sync_full_interval_seconds`). A kept document (one a `remove` without
`delete_documents` left) is skipped by every source until `knowledge.delete` removes it,
as today.

So a file that two `namespace` sources reach is written once and read through one
document. Between the owner's retirement and the other source's next listing of the
file, the document is missing; that gap is the cost of keeping one owner per address
there. A mirror pays duplicate chunks for a file two mirrored sources reach instead.

A mirrored predecessor never makes `add` of its name answer `SOURCE_DELETE_PENDING`,
since mirror ownership goes by source id, and it counts toward no source quota while it
waits; its pending retirement shows to a platform admin only. `remove` with
`delete_documents` retires the source's documents, as today, and without it they stay as
kept documents, which `add` of the same name refuses to take over
(`SOURCE_DOCUMENTS_KEPT`, as today). A mirrored source's documents are always retired
([below](#the-mirror)).

## Credentials

A source has a `credential_mode`:

| Mode        | Credential                                                                                                 | Registers it                                                               | Writes to                                            |
| ----------- | ---------------------------------------------------------------------------------------------------------- | -------------------------------------------------------------------------- | ---------------------------------------------------- |
| `shared`    | Today's: a service-account key, an integration token, an API key                                           | an `admin` of the namespace                                                | the namespace, or its mirror when `access: mirrored` |
| `connected` | The registering person's own connected account for the kind's store                                        | a person with `writer` on the namespace; the source is theirs              | that namespace's mirror                              |
| `crawl`     | A service account with domain-wide delegation that reads, as each listed person, what that person can read | an `admin` of the namespace who also holds `org_admin` or `platform_admin` | the namespace's mirror                               |

The gate of `add`, `update`, `check`, `sync_now` and `remove` drops from `admin` to
`writer`. The handler then requires `admin` for a `shared` or `crawl` source. On a
`connected` source only its owner adds items or changes `include`, since the items read
with the owner's account; its owner or an admin of the namespace may `check`, `sync_now`
and `remove` it, and only its owner, who sees them, removes its items. `rotate` stays
`admin` (for a crawl, also an org-wide role) and is refused for a `connected` source,
whose credential is its owner's connection. Owners are compared as canonical subjects
(`server/auth/aliases.py` `canonical_subject`), so a person's other spellings are the
same owner.

`LOOM_KNOWLEDGE_SOURCES_MAX_PER_NAMESPACE` (default 8) bounds a namespace's `shared` and
`crawl` sources, as today. `LOOM_KNOWLEDGE_CONNECTED_SOURCES_MAX_PER_PERSON` (default 8)
bounds a person's `connected` sources in one namespace, and
`LOOM_KNOWLEDGE_CONNECTED_SOURCES_MAX_PER_NAMESPACE` (default 64) all of a namespace's,
so writers cannot fill an admin's quota or the sync's passes. A crawl's credential reads
every listed person's drive, which is why it needs an org-wide role; a turn stops a
crawl whose registrant no longer holds `admin` on the namespace and an org-wide role, as
the owner check below stops a connected source.

### Connected accounts

A person connects their own account from the dashboard (Settings → Connected accounts)
or with `loom connect gdrive|notion|linear`. Connecting needs `LOOM_PUBLIC_BASE_URL`:
the redirect URI registered with the store is built from it, never from a request's
`Host`.

- The flow is bound to the browser that starts it, as SSO account linking binds its
  ticket (`_link_cookie` in `server/auth/oauth.py`). The dashboard's page calls
  `POST /v1/connect/<kind>/start` with its bearer token, which must carry a sign-in
  session (`sid`), which a service-account token lacks; the cookie below, not the token,
  is what ties the connection to one browser. The server keeps a single-use `state` in
  Keeper for 10 minutes, bound to the subject and to a random binding it sets in that
  browser as an HttpOnly, `SameSite=Lax` cookie, and answers the store's consent URL,
  which the page opens. The store redirects to `GET /v1/connect/<kind>/callback`, which
  stores the connection only when the request carries the cookie whose binding the
  `state` names; the subject is the one recorded at start. A consent URL sent to someone
  else attaches nothing: their browser has no such cookie. `loom connect` opens the
  dashboard's Connected accounts page for the kind, where the person signs in if they
  have not. PKCE where the store takes it; `include_granted_scopes=false`, so an earlier
  grant of the same client is not folded in.
- Scopes, the narrowest each store has:
  - `gdrive`: `https://www.googleapis.com/auth/drive.readonly`, `openid` and `email`
    (which the token answer spells `https://www.googleapis.com/auth/userinfo.email`).
    The Drive scope is a restricted one, so the operator registers an OAuth client that
    the store treats as internal to the org's workspace; a public client needs the
    store's verification first. `drive.file` with the store's file picker reads only
    files picked in a Loom page, never a folder's later files or the recordings folder,
    so it is not offered. The store's device grant does not issue Drive scopes.
  - `notion`: the public-integration OAuth, where the person picks the pages to share on
    the consent page. The store reports no scopes; the operator registers the
    integration with read-content capability only, and that registration is the bound.
  - `linear`: an OAuth application with the `read` scope.
- `GET /v1/connect/<kind>/callback` exchanges the code. Each granted scope is mapped to
  its canonical name, and a granted scope outside the kind's set refuses the connection.
  The callback stores in `loom.connected_accounts` the canonical subject, the kind, the
  store account's id and email (shown to the person, never used as their identity), the
  scopes, and the long-lived credential: the refresh token, or for a store that issues
  none (`notion`) its long-lived access token. That credential is sealed under the
  purpose `connected_accounts.credential`, which `loom secrets reseal` covers. A short
  access token is held in memory only.
- A connection belongs to the subject that made it, and only that subject's sources use
  it. `connection.list` and `connection.disconnect` act on the caller's own; an erasure
  of the person deletes its connections and retires their sources
  ([erasure](#erasure-and-audit)). An org admin sees in admin Users which kinds each
  person has connected, never a token or the store account's email.
- Connecting again with another store account replaces the connection and stops the
  sources that used the old one, as a disconnect does: their items were chosen for the
  old account.
- Every path that ends a connection (`connection.disconnect`, connecting again with
  another store account, a SCIM deactivation for every subject it revokes, an erasure of
  the person, a restore) ends it in one order: it unseals the credential into memory,
  clears the sealed credential and marks every source that uses it inactive (the part
  that fails closed), and only then revokes at the store with the token in memory, best
  effort, under a short timeout and outside any lease or claim, as SCIM revokes tokens
  past its leases; a revoke that hangs or fails leaves the credential cleared. The sync
  worker then retires those sources' mirrored documents. A turn also checks the owner: a
  source whose owner subject is revoked, deactivated, or no longer holds `writer` on the
  source's namespace is stopped (written inactive under the source lease,
  `status: owner_inactive`) and goes as a removed source does, so a missed hook fails
  closed.

A connected source reads the person's own files, the files and folders shared with them,
their recordings folder, and the pages and projects they can read. It reads no
directory, so it expands no group ([below](#who-may-read)).

### Org-wide crawl

A `crawl` source's credential is a service-account key whose client id the org's
workspace administrator granted domain-wide delegation for `drive.readonly`,
`admin.directory.group.member.readonly` and `admin.directory.user.readonly`. Its config
holds `directory_subject` (a workspace administrator the directory reads impersonate)
and `subjects`: people (emails) and groups (group emails), whose members the directory
read lists at each turn, at most 2000 people (not a setting). For each person the sync
exchanges the key for a token that impersonates them (the JWT bearer grant with `sub`)
and lists the source's items as them. A typical config is one `meet_recordings` item and
a `filter` with `kinds: [docs]`, over a group: every member's meeting notes, without
anyone connecting. Every list and members row the sync writes, a crawl's expansion
included, leaves out the people an erasure excluded ([erasure](#erasure-and-audit)).

The probe checks that the delegation grants no write on what the crawl reads: it asks
for each of the three scopes, which must be granted, and for
`https://www.googleapis.com/auth/drive`, `admin.directory.user` and
`admin.directory.group`, which the store must refuse; a credential the store grants any
of them is refused (`CREDENTIAL_REFUSED`). It cannot prove the delegation grants nothing
else (a mail scope, say), and the page says so to the admin who registers it. The audit
rows of `add`, `update` and `rotate` record the configured subjects as keyed hashes (an
HMAC under the root secret, as SCIM's audit rows hash a subject, so a list of the org's
addresses does not reverse them). Audit rows also record the scopes granted and the
number of members resolved: the audit trail outlives erasure and keeps no crawled
person's address.

## Access follows the source

A source's `access`:

- `namespace`: every reader of the namespace reads every document, as today. The only
  mode for a `shared` source of a store whose permissions the sync does not read
  (`notion`, `recording`), and the default for `shared` `gdrive` and `linear` sources.
- `mirrored`: a document is shown only to a reader the store says may read it. Required
  for every `crawl` and every `connected` source.

The model has four parts: mirrored documents live in a companion namespace no grant
opens; each document carries its permission list; every read through that namespace
checks the list and fails closed; and every complete listing reads the lists again. One
rule holds them together: nothing derived from a mirrored document (its address, title,
text, counts or existence) is written outside the mirror or answered to a caller. An op
that is not a checked leg (a source's probe, status or `remove`, an erasure's receipt)
therefore answers codes for what a mirror holds, never a count, title, file id or
address, and a code that depends on the mirror's rows (a residue failure) reads as the
same code for any cause. The exceptions are these:

1. A checked leg's answer to its reader, every field of which depends, of the mirror,
   only on the items that reader was admitted to, and otherwise only on the request and
   on whether the leg's reads succeeded.
2. The rows a marked request leaves, and every read of them (its own answer falls under
   exception 1, so a marked `memory.ask` still answers its `synth_*` fields to its
   caller):
   - Its spend (input, output and embedding tokens, and cost) depends only on what
     exception 1 lets the answer depend on: no model or embedding call runs, or is
     chosen, on a mirror candidate before the check. It counts in its namespace's totals
     and cap. Below a platform admin it shows only in the per-namespace-day total and in
     `cost.budget_status`'s live total, and every other `cost.summary` dimension leaves
     it out. The fold carries ops, those tokens and cost only, and leaves marked
     requests out of `errors` and the latency figures. A viewer can still difference a
     live or daily total around another person's marked request and so learn that
     request's spend; that residual is accepted.
   - Its work figures (statements, rows and bytes read, chunks injected, `llm_calls`,
     `response_tokens`, the stored duration), its `model` and the outcome fields a model
     call can change (`success`, `error_code`, `status_class`) go to a platform admin
     only, and the error-rate and model metrics leave marked requests out.
3. A connected source's per-item and per-file codes and `too_many_documents`, to its
   owner, and its listing age, to its owner as a code (fresh or stale).
4. A platform admin's view of a mirror's sources (every mirrored source's status, the
   probe's sampled items and their codes, and the oldest listing start), its sync and
   spend, and its erasure counts, as codes and counts, through `loom doctor`,
   `knowledge.source.inspect` and the erasure ops.

A deployment with auth off (`LOOM_AUTH_REQUIRED=false`) has no reader identity to check.
It refuses to add a mirrored source with `AUTH_REQUIRED`, which wins over
`MIRRORED_SOURCES_OFF`, the answer of every deployment until
`LOOM_KNOWLEDGE_MIRRORED_SOURCES_ENABLED` is set. That setting turned off again once
mirrors exist makes the mirror legs answer nothing and stops the sync apart from the
cleanup of a stopped source. Auth turned off once mirrors exist does the same, and the
gate still refuses every mirror name; the cleanup only deletes.

### The mirror

A mirrored document is not stored in the source's namespace but in that namespace's
mirror: a namespace named `acl-<24 random hex>`, made under the namespace's source lease
with its first mirrored source and recorded in `loom.knowledge_mirrors` (org, namespace,
mirror). The gate admits no token to a name of that form (`acl-[0-9a-f]{24}`):
`check_authorized` and `CachedGate` refuse the name before any other rule, the auth-off
return included, and `namespace.register` and `namespace.restore` refuse the form.
`erasure.request` refuses an `acl-` name in its scope for every caller but a platform
admin with auth on, and `erasure.export` for every caller; with auth off both refuse it
for everyone. So no token searches, reads or exports a mirror, and the threat model's
rows hold for every token. A mirror has no directory row and appears in no namespace
listing or usage view; a deleted name keeps its mirror and counts as in use
(`namespace_census.in_use`, with the same code as any other reason, whatever the mirror
holds) until a namespace erasure, or the mirror-erase command, deletes the
`loom.knowledge_mirrors` row. A namespace erasure deletes that row last, after the
mirror's rows (the table is classified as holding no memory content), so an erased name
can be registered again and gets a new mirror. The migration that enables the refusal
first looks for namespaces of the reserved form, registered or holding rows, and refuses
to run, naming them, if it finds any.

A knowledge read taught the mirror adds a leg over the namespace's mirror, with the
check below, while the namespace's source list holds an active mirrored source (a fact
that list shows every viewer); a mirror with no active mirrored source in that list runs
no leg. Every other read path reads the namespace and finds nothing of the mirror. So a
new op, a context-pack leg or an export that nobody taught the check shows no mirrored
document; an N-1 replica has no mirror leg, and the rollback procedure keeps it off a
mirror that holds rows. A permission column on `loom.documents` read by every path would
make the opposite the default; [sharing](sharing.md#alternatives-not-taken) rejected
that for collections, since one missed path leaks.

- **Legs with the check:** `knowledge.search`, `get`, `outline`, `list`, `download`,
  `synthesize`, `knowledge.inspect` (titles), `knowledge.source.inspect` (a mirrored
  source's documents), the `search.query` knowledge and repository-docs legs,
  `code.context`'s docs section, `context.pack`, `memory.stack`, `memory.ask` and the
  assistant (which calls the same handlers under its answer token, delegated from the
  caller's). A `get`, `outline` or `download` of a mirrored address the reader may not
  see answers `found=false`, as for an absent one.
- **Nothing to read:** decision mining and dataset extraction skip mirrored documents,
  since a decision row is a memory every reader of the namespace reads and a dataset is
  read by `knowledge.query` without the check.
- **A request that touches a mirror is marked.** Nothing a marked request writes outside
  the mirror depends on what the mirror holds, except as exception 2 allows: no cache
  shared across readers (the repository-docs listing cache, `memory.ask`'s precedents,
  the provider response and embedding caches) is read or written with text a mirror leg
  gave, and no field or row of its `agent_events`, trace, exposure, metric or span
  records, of recall credit (it writes none to `loom.memory_usage_events`), nor a
  generation's captured text, depends on the mirror. Every request that runs a mirror
  leg is marked, whether or not anything was admitted, since marking on admission would
  tell existence. The costs are accepted:
  - `knowledge.reads` names no address a marked request read, a namespace document's
    included;
  - every marked knowledge read drops out of `cost.summary` by `user_id`, `operation`
    and `agent`;
  - a marked request writes no recall credit or exposure row, so the usage signals
    (`recall_count`, `last_recalled_at`, the usage totals) leave out every marked read;
  - the assistant's support check reads a marked call as unknown, so a memory it cites
    from one gets the reason that the check could not run (`unchecked`), not
    `unsupported`;
  - the assistant's step list cannot say a marked tool call failed;
  - every session whose request runs a mirror leg is flagged, and a prompt-recall
    `context.pack` runs one, so while a namespace lists an active mirrored source every
    Loom hook of a session over it stops after its first recall, reads included, and a
    dashboard view of a flagged answer sends no `ui.vitals`
    ([the session rule](#the-session-rule)).

  The assistant front end's token record of a marked answer is a spend row under
  exception 2 that only the deployment's operator reads; the conversation itself lives
  with the caller (the dashboard's browser storage, or the message list an SDK or HTTP
  caller sends).

- **The flag.** The answer of every marked request carries `mirrored: true` as its first
  field or line, whether or not anything was admitted, for the same reason as the mark,
  and each admitted item carries it again, so a truncated copy still carries it. Every
  assistant answer on a namespace with an active mirrored source (the `assistant.ask` op
  and the chat route, `POST /v1/assistant/chat/completions`, which carries the flag in
  its stream's first event or its response's first field) is marked and flagged, since
  its answer is model text written from tool results and need not carry the field the
  tools answered. A mirror leg answers nothing to a client that does not declare it
  honours the flag. Loom's plugins and CLI declare it, and so do the dashboard, which
  shows the document, and the assistant, whose answer token declares the flag only when
  its caller's request did: a caller that does not declare gets no mirrored text through
  the assistant, and the assistant writes nothing from that turn into anything it
  shares. An SDK declares it only when the app built on it opts in.
- **The session rule** keeps a session that saw a mirrored answer from sending it on
  ([the session rule](#the-session-rule)).
- **The sync and the workers write nothing outside the mirror.** The mirrored sync's
  spend charges no namespace's cap, and
  `LOOM_KNOWLEDGE_SOURCE_MIRROR_DOCUMENTS_PER_PASS` bounds it. A mirrored source's sync
  state, the sync's audit rows, error text and per-file wait state are filed under the
  mirror's namespace and go with it when it is erased; whatever of a mirror leaves it (a
  source's status, a worker's pass notes, logs, spans and metrics, the extractor
  service's logs and traces) carries codes only (exceptions 3 and 4 apart).
- **No scope reaches a mirror.** Every statement a request runs that reads a table
  holding mirror rows other than by a namespace the caller names leaves out the
  `acl-[0-9a-f]{24}` form; the workers named here are the exception, since they must
  reach the mirror's rows. `resolve_org_scope`, ahead of its auth-off return,
  `every_namespace_scope` and `rollup_member_namespaces` (`server/auth/context.py`)
  leave it out for a request's caller; the sync, the erasure saga, the mirror-erase
  command (which finds mirrors by the `acl-` form in every store, not only by their
  registry rows, so a restore that loses a `loom.knowledge_mirrors` row orphans nothing)
  and the maintenance workers (reindex and embedding-model rebuilds, tier and retention
  moves, the blob and originals sweeps, which keep a mirror's rows referenced) reach a
  mirror through the stores, never the gate, under the rule of the bullet "the sync and
  the workers write nothing outside the mirror". Every `sql_v1_` view (each replaced by
  a migration), the analytics export's statements and the org-wide reads of
  `erasure.export` and of a `user_id` erasure's steps and census (their `Shown.hidden`
  always carries the form) leave it out too; a saga whose scope holds only `acl-` names
  is not found for anyone but a platform admin, and every scope a saga answers leaves
  `acl-` names out, so `audit.list`, the insights, usage and cost reads, the SQL surface
  and a person's export name no mirror. `row_reach` refuses the form before its auth-off
  return, so no id-addressed check or `audit.list` of a named namespace opens one.
- **Every `agent_events` read is folded and masked.** The analytics export and
  `sql_v1_usage_daily` fold a marked request's spend into a row per namespace and day
  with agent and operation `-`, and every read of `agent_events` rows for anyone below a
  platform admin, and every worker that derives rows a viewer reads from them, goes
  through the fold and the mask: `sql_v1_agent_events` masks every field of a marked row
  whose value can depend on an admitted item (`model`, the tokens, cost and work fields,
  `llm_calls`, and the outcome fields a model call can change: `success`, `error_code`,
  `status_class`), and `insights.query`'s analyst has a grant on a masked view only,
  never on the table, with the same signed scope policy on the view (the implementing
  pull request updates the isolation proof in [the assistant page](assistant.md)), which
  exception 2 accepts as a cost beside `cost.summary`'s.

A mirrored source's documents are never kept past it: `remove` retires every one it owns
(`delete_documents` is implied), and so do a disconnect, a SCIM deactivation, the owner
check and every retirement of a namespace's sources. Each of them marks the source
inactive (in the request, or in the turn for the owner check) and leaves the deleting to
the sync worker, so no request's work depends on how much the mirror holds. The worker
retires a stopped source's documents by its `source-id:` tag outside the content bound,
with a per-pass bound of its own, so a large removal does not stall other sources'
content. In a mirror, ownership and every per-source document read go by the
`source-id:<id>` tag, not by name. The retention overrides give each mirror its
namespace's `retention_days` and `warm_days`.

A namespace source and a mirrored source that reach the same file own two documents, one
in the namespace and one in its mirror. The namespace copy is readable by every reader,
because an admin registered a source that makes it so; every leg answers the address
once, dropping duplicates after the check. When both copies matched and the reader may
see the namespace copy, the hit is the namespace copy's, with its own chunks and scores
and no item flag. When only a mirror copy the reader is admitted to matched, the hit is
that copy's, with the item flag. Otherwise the mirror copy is dropped.

### The session rule

A session-content op records a session's text, or a figure derived from it, that a hook
or client sends without the agent choosing it; an op the agent calls with text it chose
(`memory.set`, `knowledge.add`, a task op) is the reader's own write. Each op declares
its class in the op registry (`server/operations/registry.py`), and an op both a hook
and an agent call is session content. They include `conversation.append` (which the
CLI's `ingest-transcript` calls), `conversation.save_message`, the `title` and
`metadata` of `conversation.create` and `conversation.update`,
`memory.set_from_messages`, `session.brief_save`, `observe.record`,
`observe.crystallize`, `working_memory.set`, `working_memory.append`, `run.begin`,
`run.complete`, `code.outcome`, `cost.record_turns` and `ui.vitals`, and every read a
hook sends with a session's prompt, files or transcript (`context.pack`, `code.search`,
`code.impact`, `memory.for_file`, `namespace.validate`). The rule decides; the
registry's classes and a test over what the shipped hooks send pin the full set.

Once a session has seen the flag, no Loom hook sends any op from it, reads included, and
no client sends a session-content op on its own; the calls the agent makes go on,
dual-use ops (`code.search`, `context.pack`) included. A session is also flagged when a
subagent or a child process it started saw the flag: the child records it in the session
state it shares with the parent, which the parent's hooks read, since the child's final
text, reworded, need not carry `mirrored: true`. A session resumed, forked or compacted
from a flagged one is flagged, and so is one started from a flagged session's process
(`claude -p` or `codex exec` run through its shell): the CLI and the hooks set an
inherited marker in the environment and read it, and keep the flag in the session's own
state beside what they keep for resume and fork. A worker an orchestrator starts outside
that process tree (through `tmux`, `ssh` or a job queue) inherits nothing; what its own
session records is the orchestrating agent's choice, out of scope ([threat
model](#threat-model)). The CLI's `ingest-transcript` refuses a transcript that carries
the flag, or whose session's state records it.

The refusal: from the first time `LOOM_KNOWLEDGE_MIRRORED_SOURCES_ENABLED` is set until
it is off again and the mirror-erase command has run, every session-content op refuses a
client that does not declare the flag, in every org, whatever mix of clients and tokens
a session runs; the declaration is the client's promise, carried by the transport and
never by an op argument a model fills in (an assistant answer token carries its caller's
declaration as a claim the bridge fixes at delegation), and a client that does not make
it (a third-party MCP client connected straight to Loom) is refused like any other. The
condition is the one row of `loom.knowledge_mirror_latch` or the process's own switch: a
process refuses while its switch is on, row or no row, and writes the row whenever it
reads the switch on and finds it absent, so neither a lost write nor a restore of a copy
without it lifts the refusal. Only the mirror-erase command deletes the row, last, once
no namespace of the `acl-` form holds rows in any store, and it refuses while any live
process reports the switch on. The check fails closed: a process that cannot read the
row refuses, and the first mirrored `add` waits until every live process reports the
switch on, as the schema check does. Since the refusal lifts only then, it says nothing
of when another org's mirrors went.

Mirrored sources are off for a deployment until an operator turns them on, after every
Loom client and hook that sends session content honours the flag. Each pull request that
adds a mirror leg, writer, client or hook lists what it writes and tests each against
these rules.

### The permission list

Each mirrored document carries its permission list in an `access_json` column of its row
(`loom.documents`; under the Postgres authority it rides in the head's `row` jsonb, with
no Postgres migration), written by the turn of the source that owns the document:

- `emails`: the people the store grants any role, lower-cased, and, by credential mode:
  - `connected`: the owner, whose account listed the file in that turn, entered as the
    source's `owner_subject` (an `owner` entry, never an email, and never the
    connection's store email), since the owner chose the item and can open it. When the
    store will not give the file's list, the list is the owner alone, not `unread`.
  - `crawl`: a person the crawl listed the file as, only when they own it or the
    permissions read as them name them, never from a read by an item or folder id or
    from a shared-with-me entry, which the store also gives a link-only share someone
    opened. An unreadable list holds the file's owner if a crawled person owns it, and
    is otherwise `unread`.
  - `shared`: nobody more; its service account is no reader, so its unreadable lists are
    `unread`.
- `groups`: group emails, which a crawl's directory read expands into
  `loom.knowledge_group_members` (one row per source and group, with its members and
  `read_at`). A connected or `shared` mirrored source reads no directory and expands no
  group, so a file shared with its readers only through a group shows only to the people
  whose own account listed it.
- `domains` and `anyone`: a domain or public permission the store marks discoverable. A
  link-only share (one the store marks not discoverable) is left out: it lets someone
  who holds the link open the file, not find it.
- `unread`: no current list is known; such a list carries no `emails`, `groups`,
  `domains` or `anyone`, and admits nobody.

For `gdrive` the list is the file's `permissions`, read with the listing, or with
`permissions.list` when the listing leaves it out; for `linear`, the active members of
every team the project belongs to, a member of unknown state admitting nobody; for a
connected `notion` source, the owner alone, so only the owner sees a page. The list
lives and dies with its document: a retirement's tombstone carries no list, and no read
op returns the column.

### Who may read

A reader sees a mirrored document when all of these hold:

1. The reader holds `viewer` on the source's namespace (the ordinary gate).
2. The document's owning source (the registry row whose `source_id` matches its
   `source-id` tag) is active and has a last complete listing that started within
   `LOOM_KNOWLEDGE_SOURCE_ACL_STALE_SECONDS` (default 3600) of now, read from its row in
   `loom.knowledge_source_listings`.
3. The person the token reads as (below) is the list's `owner` entry, compared through
   `canonical_subject`, or one of that person's verified emails is in the list's
   `emails`, or in the members of one of its `groups` from a members row of the
   document's owning source read within `LOOM_KNOWLEDGE_SOURCE_ACL_STALE_SECONDS` (by
   its `source-id` tag), or that email's domain is in its `domains`, or its `anyone` is
   set.

The reader's verified emails are its subject's directory emails that
`VERIFIED_EMAIL_SQL` admits (an SSO account's or a linked one's,
`server/core/grant_sql.py`), and nothing else: a subject that merely spells an address
is not one, and neither is a connection's store email. The predicate's first arm admits
any password-less row's `contact_email`; the implementing pull request confirms that
only an SSO sign-in or a link writes such a row's `contact_email`, or narrows the
predicate for this check. A domain matches only through the person's SSO account record,
whose identity provider vouches for that domain (the store's hosted-domain claim, or the
org's configured SSO domain), never a linked account's email or a bare verified address.
A token reads as one person, for both the `owner` entry and the emails, or as nobody:

- a person's own session token (a primary token whose own `sid` claim names a sign-in
  session, with no `creator` claim) reads as that person; a token minted for a subject
  without one reads as nobody;
- a token minted for one person alone that says so (a `reads_as_creator` claim, which
  only the person's own signed-in session can mint, through the dashboard's flow that
  connects their own agent, which the coding-agent plugins use; delegation never copies
  it) reads as its creator;
- the assistant's answer token, delegated from the caller's for that answer alone,
  carries a reads-as claim that the bridge sets at delegation to the person the caller's
  token reads as, or to nobody, and reads as that; the check never infers it from the
  token's `jti` or consumer, and no token delegated from it reads as anyone;
- every other token (a service account made for tools, CI or a team, a bot, an end-user
  token, whatever their `creator`) and a request sent on behalf of another person
  (`X-Loom-On-Behalf-Of`) reads as nobody, so it sees only `anyone` documents.

The check fails closed. A stale listing, an `unread` list, an unexpanded or stale group,
a link-only permission, a permission the store marks deleted or pending (its address may
now belong to someone else) and a permission type the sync does not know admit nobody. A
missing registry or listing row admits nobody. A leg whose per-request reads (the
statements it issues whatever matched, the reader's canonical subject and verified
emails among them) fail shows no mirrored document and marks the answer
`degraded: mirror_unread`; a read it issues only for candidates (the lists, the members
rows, the heads) that fails drops every mirrored result without the marker, so the
marker says nothing about what matched. Each leg reads the registry row and the listing
row first and then the members rows and the lists, under `sequential_reads()`, so a
lagging replica cannot pair a fresh listing row with an older list. It computes the
addresses the reader may see before its `LIMIT`, so a page is never cut short by the
check, and caches no admitted set. No score depends on a document the reader may not
see: a mirror's chunks add nothing to any corpus statistic (the lexical leg's document
counts, lengths and term frequencies), and a mirror leg scores its lexical hits with
fixed corpus constants, which depend on no document, so a namespace fed only by mirrored
sources still ranks by both legs. Under the Postgres authority each leg judges the
addresses it is about to answer by their heads, so a narrowing or a retirement committed
and not yet published already applies, and a missing head drops the address; a leg that
cannot read the heads shows no mirrored document. The implementing pull request measures
the leg on a mirror of a million documents under both storage authorities and puts the
figures here.

### Every complete listing reads the lists again

Every complete listing of a mirrored source reads every file, metadata only (id,
revision, permissions): the store does not change a file's modification time when its
sharing changes, so an incremental listing would miss a permission removed. A listing
that reads a list unlike the stored one rewrites the document's row with the same
content and the new list, as `knowledge.update_metadata` without `reindex` does, so no
chunk or embedding is rebuilt; an unchanged list costs no write. A stored document the
complete listing no longer has is rewritten `unread`, with every entry cleared, then
retired on positive evidence, as today. The listing row's start lands only once every
changed list and every such `unread` rewrite of that listing is written, so a row is
never newer than the lists it vouches for. These rewrites are outside the pass's
document cap, bounded only by the listings' share of the deadline. Content is fetched
only for a file whose revision changed, and a content write carries the list of the
latest listing that read the file, so a fetch the pass bound deferred never writes an
older list; a listing row lands only once every document written during that listing
carries a list it read.

A pass runs the listings first, rotating across the mirrored sources, and gives them at
most a third of the pass deadline (`knowledge_source_sync_max_pass_seconds`), while the
namespace sources' content starts at a fixed point of the pass whatever the listings
used, so their progress says nothing about a mirror's size; a listing that does not
finish goes on from its cursor in the next pass, and until it completes the source's
listing row keeps its older start, so a large source needs a stale bound longer than the
time to list it all. `inspect` and `loom doctor` report each source's oldest listing
start, a mirrored source's to a platform admin only (its connected owner sees fresh or
stale). A stopped sync hides every mirrored document once the stale bound passes. The
implementing pull request measures the writes of a crawl over 2000 people and puts the
figures here.

How lists from several accounts of one source, a person a crawl stops reading, a stopped
source's rows, and restores of either store are handled is the implementing pull
request's to specify against these four parts; each must leave no list or row that
admits someone the store no longer names.

### Threat model

| Threat                                                                            | Boundary                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                | Enforced by                                                                            |
| --------------------------------------------------------------------------------- | --------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- | -------------------------------------------------------------------------------------- |
| A connected person leaks a private file into a team namespace                     | Every connected source is mirrored; its documents live in the mirror, and a teammate sees one only when the store names them. `access` cannot change.                                                                                                                                                                                                                                                                                                                                                                                   | `add` and `update`; the mirror gate; `test_document_source_leakage.py`                 |
| A crawl's registrant reads crawled people's private files                         | No rule admits a source's registrant or owner as such; a registrant reads a file only when the store names them or their own connected account lists it.                                                                                                                                                                                                                                                                                                                                                                                | the access check                                                                       |
| An admin points someone's connected account at files they did not choose          | Only a connected source's owner adds its items or changes `include`.                                                                                                                                                                                                                                                                                                                                                                                                                                                                    | `update`                                                                               |
| A link-only share becomes a search hit for everyone                               | A permission the store marks not discoverable admits nobody.                                                                                                                                                                                                                                                                                                                                                                                                                                                                            | the access check                                                                       |
| A person stops reading a file and Loom keeps showing it (stale ACL)               | Every complete listing of a mirrored source reads every file's list and rewrites a changed one; a source whose last complete listing is older than the stale bound admits nobody.                                                                                                                                                                                                                                                                                                                                                       | the sync's mirrored listing; the access check                                          |
| A person leaves a group and keeps reading through it (group churn)                | Groups are read with every complete listing into a members row with its `read_at`; a stale or unread group admits nobody.                                                                                                                                                                                                                                                                                                                                                                                                               | the access check                                                                       |
| Auth is switched off after mirrors exist                                          | The `acl-` refusal runs before the auth-off return; mirror legs answer nothing and the sync skips mirrored sources while auth is off.                                                                                                                                                                                                                                                                                                                                                                                                   | `check_authorized`; the mirror legs                                                    |
| A person leaves the org and their connected sources keep syncing                  | SCIM deactivation and disconnect stop the sources and clear the credential, then revoke it at the store; each turn's owner check skips a source whose owner is revoked, deactivated or no longer a writer.                                                                                                                                                                                                                                                                                                                              | `server/auth/scim.py`; the sync's turn                                                 |
| A crawl reads more than its registrant meant, or can write                        | An org-wide role to register, update or rotate; the probe requires the store to refuse a write scope; subjects and scopes in the audit row.                                                                                                                                                                                                                                                                                                                                                                                             | the crawl probe; `loom.audit_log`                                                      |
| An id-addressed read confirms a mirrored document exists                          | `get`, `outline`, `download` and the inspects answer `found=false` or leave out an address the reader may not see.                                                                                                                                                                                                                                                                                                                                                                                                                      | the mirror legs                                                                        |
| A viewer or admin learns what a mirror holds from an op that is not a checked leg | A source's probe, status and `remove`, and an erasure's receipt, answer codes for a mirror; its counts go to a platform admin only.                                                                                                                                                                                                                                                                                                                                                                                                     | the source ops; the erasure handlers                                                   |
| A new read path shows mirrored documents to everyone                              | Mirrored documents live in a namespace no grant opens and no listing names; a path that does not read the mirror shows none, and no org-wide scope names an `acl-` namespace.                                                                                                                                                                                                                                                                                                                                                           | `check_authorized` (`acl-[0-9a-f]{24}`); the scope helpers ([the mirror](#the-mirror)) |
| An older build shows mirrored documents                                           | N-1 has no mirror leg, but its org-wide reads return `acl-` rows and so hand out a mirror's name, and its gate admits a wildcard admin token that names it. So no N-1 may serve while a mirror holds rows: the new modes wait for N everywhere, the rollback procedure erases the mirrors with every replica stopped first, and N reports an N-1 heartbeat while a mirror holds rows. An N-1 replica started without that procedure (a bare `helm rollback`) exposes mirror text to an org admin; that residual rests on the procedure. | the rollback procedure; `loom doctor`                                                  |
| A mirrored document's text reaches every reader through a derived row             | Decision mining and dataset extraction skip mirrored documents; a request that touches a mirror writes nothing derived from it outside the mirror.                                                                                                                                                                                                                                                                                                                                                                                      | the sync; the mark                                                                     |
| A store account is linked into another person's Loom account, either way          | The connection is stored only from the browser that started it, with that subject's own dashboard session and the `state` cookie. A connection's email is never a reader identity.                                                                                                                                                                                                                                                                                                                                                      | the connect routes                                                                     |
| A caller injects store query syntax through a filter                              | Filters are fields with patterns, escaped and built by the server; no raw query is taken.                                                                                                                                                                                                                                                                                                                                                                                                                                               | `server/clients/gdrive.py`                                                             |
| A stolen connected credential changes the store                                   | Only read scopes are accepted; the credential is sealed and never returned.                                                                                                                                                                                                                                                                                                                                                                                                                                                             | the connect callback; `server/core/secrets_at_rest.py`                                 |

Out of scope: response timing, and a resource limit (memory, rows read) that a mirror's
size can trip on a per-request statement, which like timing depends on how much the
mirror holds; how fast the shared pass gets to a connected owner's own source, which its
fresh or stale code and per-item status reflect; that a namespace erasure is still
retrying, which a namespace admin sees whatever its cause; a worker an orchestrating
agent starts outside a flagged session's process tree; a person who hands their own
`reads_as_creator` token to a team bot or a CI job, which then reads as them; a session
still open from before an operator lifted the refusal that saw mirrored text then; a
reader who already saw a document and kept its text in a record only they read, or one
their agent chose to write or sent in a call it chose (a follow-up `memory.search` whose
terms come from what it read, recorded in a trace log the namespace's viewers read),
which is that reader's write; that a person able to add a file where a mirrored source
lists can tell, by the file never appearing, that the source is over
`LOOM_KNOWLEDGE_SOURCE_MAX_DOCUMENTS`; the store itself reporting a wrong permission
list.

## Meetings through the shared-folder store

The meeting store saves a meeting's transcript and its notes as Docs in the organiser's
recordings folder of the shared-folder store, and shares them with the participants. A
`gdrive` source reads them like any file: a `meet_recordings` item, or a `folder` item
for that folder. `meet_recordings` finds the folder the meeting store made in each
account's own drive by its name, from a fixed table of the names the store gives it in
each language; when none is found, the turn reports `meet_recordings: not found`, naming
no account, and a `folder` item with the folder's link reads it instead.

The sync recognises a transcript Doc (one in a recordings folder whose exported text has
the store's transcript shape: a title line, then speaker turns under time stamps) and
writes it with `server/knowledge/domain/transcripts.py`'s timed sections,
`ingested_format: transcript`, as a `recording` source writes one, so a citation names
the minute. A meeting-notes Doc is written as any Doc. When the listing account can read
its permission list (the organiser's account, or a crawl listing as the organiser), the
list (the organiser and the invited participants) makes a mirrored source show each
meeting to the people invited; a participant's connected source cannot read it, so its
copy shows to that participant alone.

The `meet` provider of the `recording` kind is removed; `recording` keeps `zoom`, whose
cloud recordings do not land in the shared-folder store. This is a break: a stored
`meet` source is listed and inspected, refused by `check`, `rotate`, `sync_now` and
`update` (`BAD_CONFIG`, naming the `gdrive` route), and skipped by the sync with
`status: provider_removed`, which counts as no failure of the pass; its documents stay
until it is removed.

## Project-tracker issues

Feeding a project tracker's issues and comments into the tracker plane is a design of
its own, written in the pull request that builds it, after the items and access pull
requests. That design has to answer what this page found:

- The tracker plane reads a team's records live and copies none ([tracker
  plane](../features/tracker/overview.md)); issues with no team database would be kept
  by Loom, in its own tables, under the erasure census.
- The plane's statements filter by `repos` (and, in a standard namespace, by
  `is_private = 0`), never by org or namespace, so a Loom-kept table needs an (org,
  tracker namespace) predicate on every read.
- A source serves only with `github_issues`, `github_prs` and `edges` ([record
  contract](../features/tracker/record-contract.md)); the contract's `source_id` names a
  repository; and tracker namespaces belong to `LOOM_TRACKER_ORG` alone.
- An issue of a private team, or read through a person's connected account, is private
  (`is_private = 1`). The contract derives an item's id from `source_id`, which changes
  when an issue moves to another team; the design changes the contract to take the
  issue's own UUID, so a moved issue keeps its item.

## Surfaces

- **Ops.** `knowledge.source.add` takes `items` or `links`, `credential_mode` and, for
  `crawl`, `directory_subject` and `subjects`; `update`, `parse_links`,
  `connection.list` and `connection.disconnect` are new (a connection starts in the
  dashboard, `POST /v1/connect/<kind>/start`); `list` and `inspect` show items, modes
  and, through the check, the documents each owns. A mirrored source shows its document
  counts to nobody but a platform admin (exception 4), apart from what a checked leg
  lists to an admitted reader, and a connected source's items only to its owner (an
  admin can still remove the source). To a viewer who is neither the owner nor an admin,
  a mirrored source shows its kind, its owner as "a connected account" for a connected
  one, its item count and its last sync's status, not its items, `config` or
  `created_by`; its error text carries codes only, for every viewer. Python and
  TypeScript SDKs are regenerated.
- **CLI.** `loom knowledge source add|update|list --namespace <ns> --link <url>...`
  (`--credential-file`, `--connected`), `loom connect gdrive|notion|linear`,
  `loom connection list|disconnect`, `loom knowledge mirror erase --all` ([restores and
  rollback](#rolling-upgrades)), and `loom doctor`'s line for the oldest listing start
  of each mirrored source (for a platform admin; a connected owner sees fresh or stale).
- **Dashboard.** The Library's Sources section: paste many links, a preview of what each
  resolves to and which could not be read, Check, Add, edit items, the access mode, and
  each source's last sync (a mirrored source's status code only). Settings → Connected
  accounts: connect, see, disconnect. The repository card keeps its own sources.
- **Agents.** Every op reaches an agent through `loom_op`. The mentor reference gains a
  line: handed a store link and asked to bring it in, call `knowledge.source.update`
  with `links` on the caller's connected source for that store, or `add` one; with no
  connection, `connection.list` answers the dashboard link
  `<public base>/settings/connected-accounts?kind=<kind>` to hand back.

## Data model

ClickHouse, additive migrations:

| Table                              | Key                                                           | Holds                                                                                                                                                                                                             |
| ---------------------------------- | ------------------------------------------------------------- | ----------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `loom.knowledge_sources` (columns) | `(org, namespace, name)`                                      | `source_kind`, `credential_mode` (default `shared`), `access` (default `namespace`), `owner_subject`, `items_json`, `options_json` (a crawl's `directory_subject` and `subjects`), `secret_enc`                   |
| `loom.knowledge_mirrors`           | `(org, namespace)`                                            | `mirror`, `created_at`                                                                                                                                                                                            |
| `loom.knowledge_mirror_latch`      | one row                                                       | `set_at`: the deployment once had the switch on; the mirror-erase command deletes it last. It stays in ClickHouse and holds no memory content.                                                                    |
| `loom.documents` (column)          | as today; in a mirror `source_uri` is `<address>#<source id>` | `access_json`: a mirrored document's permission list (`emails`, `owner`, `groups`, `domains`, `anyone`, `unread`); empty elsewhere; under the Postgres authority it rides in the head's `row` jsonb, no migration |
| `loom.knowledge_source_listings`   | `(org, namespace, source_id)`                                 | `name`, `started_at` (the start of the source's last complete listing), `version`; `namespace` is the mirror                                                                                                      |
| `loom.knowledge_group_members`     | `(org, namespace, source_id, group)`                          | `name`, `members`, `read_at`, `deleted`, `version`; `namespace` is the mirror                                                                                                                                     |
| `loom.connected_accounts`          | `(org, subject, kind)`                                        | `account_id`, `account_email`, `scopes`, `credential_enc`, `active`, `connected_at`, `disconnected_at`, `version`                                                                                                 |

A source N-1 can operate on (`shared`, `access: namespace`, with items today's config
can express) keeps today's columns as they are: `kind`, its credential in
`credential_enc` and its items in `config_json`, which N writes alongside `items_json`.
Every other source (mirrored, `connected`, `crawl`, or with an item today's config
cannot express) is stored with `kind` empty and its kind in `source_kind`, its
credential in `secret_enc` (purpose `knowledge_sources.secret`, which
`loom secrets reseal` covers) and its items in `items_json`, with `credential_enc` empty
and `config_json` `{}`. N reads `source_kind` first and `kind` when it is empty, and
`items_json` first and `config_json` when it is empty ([rolling
upgrades](#rolling-upgrades)). The mirror's documents and chunks live in
`loom.documents` and `loom.chunks` under the mirror's namespace, so the document
machinery (heads under the Postgres authority, deliveries, originals, the blob sweep)
applies unchanged. Each new table is placed in [storage layout](storage-layout.md) by
its placement rule, in the pull request that adds it: `loom.knowledge_mirrors` and
`loom.connected_accounts` are registrations read by key, so they follow
`knowledge_sources` to Postgres in wave 3; the listing and members rows are read by the
mirror legs beside the lists in `loom.documents`, so they stay in ClickHouse. An
implementing pull request that needs another table (for a stopped source's cleanup, or a
crawl's exclusions) adds it with its placement and erasure classification.

## Erasure and audit

What every implementing pull request must hold; each specifies its steps, leases,
statements and census keys against these, and its tests pin each one:

- Every new table is classified in `server/erasure/store/census.py`, and every table
  that can hold a person's address or a document's text is covered by an erasure.
- A namespace erasure, `namespace.delete`, `collection.delete` and `code.repo.remove`
  retire the namespace's sources first, clearing their credentials, so no turn writes
  back what the erasure took. A `namespace` or `namespace_list` erasure covers each
  named namespace's mirror as one more name of the same erasure, through its resume,
  replay and re-verify. A mirror's rows are deleted in either mode: no token reads or
  restores an `acl-` name. A `user_id` erasure touches no mirror row (the sync writes
  them as `-`), and its census and export name none.
- A person's connected accounts, the sources they own with every mirrored copy those
  wrote, and their entries in every crawl are erased when an org admin asks; afterwards
  no source row, list or members row names them, and a crawl reads them again only once
  an org admin readmits them. The pull request that builds connected accounts and crawls
  designs that erasure.
- `erasure.request` refuses an `acl-` scope for every caller but a platform admin with
  auth on, and `erasure.export` refuses one for every caller (an export would read the
  mirror's chunks with no permission check); with auth off both refuse it for everyone.
- Audit rows: `add`, `update`, `rotate`, `remove`, `sync_now` as today, plus `update`'s
  item changes; `connect` and `disconnect` (subject, kind, scopes, never a credential);
  a crawl's subjects (as keyed hashes), granted scopes and member count; a SCIM-driven
  stop names the sources it stopped.

## Rolling upgrades

- **Clients.** A client built before `mirrored: true` declares nothing, so a mirror leg
  answers it nothing until it is upgraded, and [the refusal](#the-session-rule) refuses
  its session-content ops; a third-party MCP client is refused the same way. Mirrored
  sources stay off (`LOOM_KNOWLEDGE_MIRRORED_SOURCES_ENABLED`) until the operator has
  rolled out the plugins, CLI, SDKs and hooks that honour the flag.
- **N-1 cannot operate on a source it must not.** N-1 reads `kind`, unseals
  `credential_enc` and reads `config_json`, and knows none of the new columns. A source
  stored with `kind` empty is one N-1 cannot use: its `check`, `rotate` and sync turn
  fail before any request and retire nothing, and N-1 syncs only sources on a trunk
  namespace.
- **The new modes wait for N everywhere.** `add` and `update` answer 503 `OVERLOADED`
  (`retry_after_s`) for a source stored with `kind` empty while any live process of any
  deployment on the ClickHouse service reports a schema version below the migration that
  adds the columns, or no heartbeat has been read, as rule 24 does for namespace guides
  ([rolling upgrades](rolling-upgrades.md)).
- **Rollback and restores.** N-1 has no mirror leg, but its gate admits a wildcard admin
  token that names a mirror, its org-wide reads (insights, usage, cost, the analytics
  export) include `acl-` rows, and nothing on N-1 ages the lists. N reports the
  condition (`loom doctor`, and the schema heartbeat check above) when a process below
  the mirror migration heartbeats while `loom.knowledge_mirrors` holds a mirror, as a
  rolling rollback (a Helm rollback included) would start one. So the rollback procedure
  ([upgrades](../operations/upgrades.md)) stops every replica, stops every mirrored
  source and erases every mirror (`loom knowledge mirror erase --all`) before any N-1
  replica starts. A restore of either store runs the same command before the deployment
  starts, since a restored copy cannot be trusted to say who may read; owners connect
  and add their sources again. The implementing pull request adds both steps and
  specifies the command.
- **Ownership.** In a namespace, one owner per address, found by its tag, is today's
  model, so N-1 and N agree on whose a document is; N-1 has no mirror leg.
- **`meet`.** N-1 still syncs a stored `meet` source on its turns; N skips it.
- **Erasure.** N-1's erasure misses the mirror and the new tables (rule 14): run again
  with N every erasure completed while an N-1 replica served.

Each implementing pull request adds its rule to [rolling upgrades](rolling-upgrades.md),
and pins this section's N-1 behaviour with a test that runs today's `check`, `rotate`,
`remove` and sync turn over a row N wrote with `kind` empty.

## Breaks

- Mirrored sources need `LOOM_KNOWLEDGE_MIRRORED_SOURCES_ENABLED`, which an operator
  sets once every Loom client and hook honours `mirrored: true`.
- While [the refusal](#the-session-rule) holds, every session-content op, the hook-sent
  reads included (`context.pack`, `code.search`, `code.impact`, `memory.for_file`,
  `namespace.validate`), refuses a client that does not declare `mirrored: true` in
  every org: an SDK app that has not opted in, and a third-party MCP client, which
  cannot.
- `recording` sources lose the `meet` provider.
- A source whose stored documents plus new files pass
  `LOOM_KNOWLEDGE_SOURCE_MAX_DOCUMENTS` (default 50,000) writes no new document and
  reports `too_many_documents`, where today it syncs on with a truncated comparison.
- The gate of `add`, `update`, `check`, `sync_now` and `remove` takes `writer`, with
  `admin` checked by the handler for shared and crawl sources; `rotate` is refused for a
  connected source.

## Settings

| Setting                                              | Default | Meaning                                                                                                    |
| ---------------------------------------------------- | ------- | ---------------------------------------------------------------------------------------------------------- |
| `LOOM_KNOWLEDGE_SOURCE_ACL_STALE_SECONDS`            | 3600    | a source's listing or group members row older than this admits nobody                                      |
| `LOOM_KNOWLEDGE_SOURCE_MAX_DOCUMENTS`                | 50000   | a source's stored documents plus new files; past it a turn holds back new documents                        |
| `LOOM_KNOWLEDGE_CONNECTED_SOURCES_MAX_PER_PERSON`    | 8       | a person's connected sources in one namespace                                                              |
| `LOOM_KNOWLEDGE_CONNECTED_SOURCES_MAX_PER_NAMESPACE` | 64      | a namespace's connected sources                                                                            |
| `LOOM_KNOWLEDGE_MIRRORED_SOURCES_ENABLED`            | false   | mirrored sources may be added; set once every Loom client and hook honours `mirrored: true`                |
| `LOOM_KNOWLEDGE_SOURCE_MIRROR_DOCUMENTS_PER_PASS`    | 40      | documents a pass fetches, writes and retires for mirrored sources, apart from the namespace sources' bound |
| `LOOM_CONNECT_GDRIVE_CLIENT_ID`, `_SECRET`           | unset   | the shared-folder store's OAuth client; unset, those connections are off                                   |
| `LOOM_CONNECT_NOTION_CLIENT_ID`, `_SECRET`           | unset   | the page store's public integration                                                                        |
| `LOOM_CONNECT_LINEAR_CLIENT_ID`, `_SECRET`           | unset   | the project tracker's OAuth application                                                                    |

The redirect URI to register with each store is
`<LOOM_PUBLIC_BASE_URL>/v1/connect/<kind>/callback`.

## Rejected

- **Mirrored documents in the namespace itself, filtered by every read.** A missed path
  would show the document to every reader. The list sits on the document only inside the
  mirror, which no token opens.
- **A namespace per permission list.** A list changes with every share, which would move
  the document, its chunks and embeddings on each change.
- **A mirror name derived from the namespace.** Anyone could compute it, and a known
  name makes every error and refusal about it a probe.
- **A permission list per reading account within a source.** An account that read the
  list before a reader was removed would keep admitting them until its row aged out.
  Across sources each copy keeps its own list, bounded by the stale bound and dropped at
  its own source's next listing.
- **Resolving the store's groups through Loom's SCIM groups.** They hold a service's end
  users (`user:<id>`), not people's emails.
- **A separate meeting-store path.** The meeting store already saves transcripts and
  notes to the shared-folder store, with the participants' permissions.
- **`drive.file` with the picker.** It cannot read a folder's later files.
- **Incremental listings for mirrored sources.** A sharing change does not move a file's
  modification time.

## Invariants

- A mirrored document is read only through a leg that applies the permission check; no
  token reads or exports a mirror, and `erasure.request` takes a mirror's name only from
  a platform admin, with auth on.
- Every `crawl` and `connected` source is mirrored.
- A stale, unexpanded or link-only permission, an `unread` list and a permission type
  the sync does not know admit nobody.
- Every complete listing of a mirrored source reads every file's list again, and only a
  complete listing's start vouches for the lists.
- No caller-supplied host or query string reaches a store.
- A connected source stops on every path that ends its connection or its owner's
  standing ([connected accounts](#connected-accounts)), and its mirrored documents go
  with it.
- Nothing derived from a mirrored document leaves the mirror except as the four
  exceptions allow, and no mirrored source exists until
  `LOOM_KNOWLEDGE_MIRRORED_SOURCES_ENABLED` is set.
- N-1 never operates on a source it cannot honour: such a source is stored with `kind`
  empty.

## Source entry points

- `server/knowledge/domain/sources.py`: kinds, configs, items.
- `server/knowledge/domain/source_links.py`: the link parser.
- `server/knowledge/app/sources.py`: the ops.
- `server/knowledge/app/source_sync.py` and its kind modules: the sync.
- `server/knowledge/app/source_access.py`: the permission check and the mirror legs.
- `server/auth/connected_accounts.py`: the connect routes and their store.
- `server/clients/gdrive.py`, `notion.py`, `linear.py`, `recording.py`: the stores.

## Tests

Each implementing pull request lists its own; these pin the model:

- Unit:
  - Items, links and the sources' ops:
    - The link parser on every link form and every unreadable one.
    - Item validation.
    - The query builder escaping every field.
    - A non-owner admin's `list` and `inspect` of a connected source showing `config`
      `{}`.
    - A connected source's items, and its document counts beyond those a checked leg
      lists to that admin when admitted, hidden from an admin who is not its owner; a
      non-owner admin's `check` of a connected source answering no item ids.
    - A failed read of the reader's canonical subject or emails giving the same marker
      whether or not a candidate exists.
    - A mirrored source's probe (`check`, and the `add`, `update` and `rotate` probes,
      by a non-owner admin and a crawl registrant), status, `remove` answer and a
      namespace erasure's receipt carrying no count, title, file id or address of what
      the mirror holds.
    - A crawl `update` probe by any caller answering the same code whether or not the
      file exists.
    - A crawl's status carrying no `item <n>` code and no `meet_recordings: not found`.
    - A non-owner admin's `remove` of mirrored sources of 10 and 10,000 documents
      leaving the same request-side figures.
    - `add` of a removed mirrored source's name answering as for a fresh name.
    - A mirrored `add` during an open namespace erasure refused with `ERASURE_RUNNING`.
    - The mixed-version refusal, and N-1's `check`, `rotate`, `remove` and sync turn
      over a row N wrote with `kind` empty.
    - A crawl `update` by an `admin` with an org-wide role who is not the registrant,
      adding a `file` or `filter` item, accepted whether or not any crawled person can
      read it.
  - The mirror and its gate:
    - The `acl-` gate in `check_authorized` and `CachedGate`, with auth on and off, and
      for a platform admin on a read op.
    - `namespace.register` and `namespace.restore` refusing the form.
    - `erasure.request` and `erasure.export` refusing an `acl-` scope with auth on and
      off.
    - `row_reach` and `audit.list` refusing an `acl-` name with auth off.
    - `row_reach`, `row_in_scope` and `namespace_for_id` opening nothing of a mirror
      with auth off.
    - `erasure.list` and `erasure.status` treating an `acl-`-scoped saga as not found
      for an org admin.
    - `erasure.status` and `erasure.list` by a viewer, and a saga that fails on mirror
      residue, showing no mirror count.
    - `resolve_org_scope` with auth off, `every_namespace_scope`,
      `rollup_member_namespaces`, `sql_v1_agent_events` under a `*` grant and the
      analytics export leaving out every `acl-` name.
    - The insights agents and cost reads leaving `acl-` names out.
    - `audit.list` after a connected `add`, by an admin who is not the owner, showing no
      item id other than its keyed hash.
    - `erasure.export` and a `user_id` erasure's census naming no `acl-` row.
    - No row carrying a reader's `user_id` under an `acl-` name after an admitted
      reader's `get`, `search` and `ask`.
    - An erased namespace's name registered again with a new mirror.
    - N reporting a below-schema heartbeat while a mirror holds rows.
  - The list and who may read:
    - The access check on fresh and stale listings, `unread`, group, stale group,
      domain, link-only, anyone and unknown permissions, and for a delegated token, a
      bot and an end-user token.
    - An email-shaped subject with no verified directory email, and an inheriting
      service account, seeing no `emails` document.
    - A group matched only through the owning source's members row.
    - A connected source's unreadable-list entry admitting the owner through the list's
      `owner` entry, never an email or the connection's store email.
    - A crawl `file` item of a link-only file admitting no crawled person.
    - A verified email at a domain without the identity provider's domain claim refused
      a `domains` document.
    - A crawled person who opened a link-only file only through its link not admitted.
    - A deactivated team member admitted by no `linear` list.
    - Against an `emails` document: a person's own session token and a
      `reads_as_creator` agent token admitted, a Settings-made service-account token
      (with `creator`), a delegated child of a `reads_as_creator` token other than the
      assistant's answer token, and a request on behalf of another person not.
    - A permission the store marks deleted, whose address now belongs to another
      verified user, admitting nobody.
    - An answer token delegated from the listed person's session token admitted, an
      answer token delegated from a `reads_as_creator` token admitted as its creator,
      and a token delegated from an answer token refused.
    - An assistant answer token delegated from a Settings-made service-account token
      refused an `emails` document.
    - A token with consumer `loom-assistant` and an `assistant-answer-` id but no
      reads-as claim reading as nobody.
    - A caller of `assistant.ask`, `loom_assistant_ask` or the chat route that does not
      declare the flag getting no mirrored text; an `assistant.ask` and a chat-route
      answer on a namespace with an active mirrored source and no admitted item
      answering `mirrored: true`, the chat route in its stream's first event and,
      unstreamed, in its response's first field; a plugin session flagged once its
      `loom_assistant_ask` answer carries the flag; a marked `assistant.ask` and
      chat-route row folded and masked like a marked tool-call row.
    - A `knowledge.search`, `context.pack` and `memory.ask` on a namespace with an
      active mirrored source and no admitted item answering `mirrored: true` as the
      first field; the same reads after the last mirrored source is removed, and while
      the only one listed is inactive (its owner gone, its cleanup pending), answering
      no flag and running no mirror leg.
    - A marked `memory.ask` whose candidates are all hidden making the same model and
      embedding calls as one with no candidates.
    - A caller admitted to nothing seeing the same `cost.budget_status` change over 10
      and over 10,000 hidden matching documents.
    - A file another mirrored source holds, added by a second source, written as that
      source's own copy with its own list, so the adder sees what they would with no
      other source.
    - A hidden mirrored chunk holding the query term leaving an admitted hit's
      `lexical_score` and rank unchanged.
    - A namespace whose documents are all mirrored ranking by both legs.
  - The check fails closed:
    - A leg that cannot read the listing row or the owning source's registry row showing
      no mirrored document, marked `degraded: mirror_unread`, and one that cannot read
      the lists, the members rows or the heads dropping mirrored results without the
      marker.
    - The same `degraded` marker for a query that matches a hidden document and one that
      matches nothing while a mirror read fails.
    - A failed list, members or heads read with one hidden candidate answering exactly
      as a query with no candidate: no marker.
    - A leg on a lagging replica that sees a fresh listing row and an older list
      admitting nobody.
    - A document missing from a complete listing admitting nobody once its listing row
      lands.
    - A stopped source's documents admitting nobody before their retirement.
  - Every complete listing reads the lists again:
    - A turn that finds a narrowed list rewriting the row without rebuilding a chunk,
      and one that finds the same list writing nothing.
    - A listing row landing only after its changed lists.
    - A fetch the pass bound deferred writing no list older than the listing row that
      lands after it.
    - After a person's erasure, no source's next turn writing a list or members row that
      names them.
    - A namespace source's per-pass progress unchanged by a mirror's size.
    - A large removal leaving another mirrored source's content fetches unchanged.
    - A mirror's originals surviving a blob sweep and its chunks re-embedded after a
      model change.
  - The rule and its exceptions:
    - Two readers of one namespace, one admitted to a mirrored document, through the
      repository-docs listing cache and `memory.ask`'s precedent fast path: the other
      gets none of it.
    - A request a mirror leg answered keeping no argument or result-side field the leg
      shaped in its `agent_events` row.
    - A marked request's trace and exposure rows unchanged by a hidden mirrored hit.
    - A follow-up query a mirror leg shaped leaving the embedding cache untouched.
    - A generation over a mirrored chunk capturing no text.
    - The sync's audit rows, status, pass notes and log lines naming no mirrored file id
      or address.
    - No reader but a platform admin reading the work figures on a marked row.
    - A viewer's cost or insights read showing no per-request token spend of another
      person's marked request.
    - A viewer's `cost.summary` grouped by `user_id` and `operation` showing no single
      marked request's tokens.
    - `cost.budget_status` and `cost.summary` by a viewer reading the same before and
      after a mirrored sync pass.
    - A namespace-scoped SQL user reading `sql_v1_agent_events` and
      `sql_v1_usage_daily`, and the analytics export's `usage` rows, after another
      person's marked request, finding its spend and work only as the fold and the mask
      allow.
    - A mirrored source's document counts shown to nobody but a platform admin.
    - `knowledge.reads` after an admitted reader's `knowledge.get` of a mirrored address
      not naming it, nor a namespace document's address read in the same marked
      request.
    - The dashboard's `knowledge.get` and the assistant's `memory.ask` of an admitted
      mirrored document answered with `mirrored: true`.
    - A later turn of a session that saw `mirrored: true` not captured, and a client
      that does not declare the flag answered nothing by a mirror leg.
    - A capture hook recording no turn whose answer carries `mirrored: true`.
    - A plugin session over a collection with an active mirrored source and no admitted
      hit flagged all the same, its next turn not captured.
    - `loom` CLI output holding an admitted mirrored item, and a subagent's mirror read,
      not captured into the parent session.
    - A `loom` CLI run through a shell and a prompt-recall injection carrying
      `mirrored: true` stopping every hook of the session.
    - The CLI's `ingest-transcript` refusing a transcript that carries the flag.
    - A dashboard view of an answer carrying `mirrored: true`, with or without an
      admitted item, sending no `ui.vitals`.
    - A forked or resumed session not captured after its parent saw the flag.
    - One test over every op the registry classifies as session content, each refusing a
      client that does not declare the flag from the first time
      `LOOM_KNOWLEDGE_MIRRORED_SOURCES_ENABLED` is set until it is off again and the
      mirror-erase command has run, in every org, with the switch on and off; and a test
      that collects every op the shipped hooks, their shared library and the CLI send
      without the agent choosing the content and asserts each is classed as session
      content.
    - A session with clients holding tokens for two orgs, one reading a mirror,
      capturing nothing into the other.
    - The session-content refusal holding once the switch was set with no mirror yet,
      when the deployment row cannot be read and on a replica whose view lags it, and
      lifting only once the switch is off and the mirror-erase command has deleted the
      row.
    - A mirror-erase run with the switch still on refused, a restore that loses the
      deployment row, and a mirrored `add` after either, with the refusal still holding.
    - A viewer's `insights.query` over `agent_events` after another person's marked
      request finding its tokens, cost, `model` and outcome fields only as the mask
      allows, the caller's own rows included.
    - A caller admitted to nothing seeing no work figure on its own marked row over a
      mirror of 10 and of 10,000 hidden documents.
    - A checked leg's every answer field unchanged by a hidden mirrored document.
    - The mirror-erase command cut short after deleting mirrors but before the latch
      row, with the refusal still holding.
    - An assistant memory citation from a marked `memory.ask` returned `unchecked`.
    - A viewer's `cost.budget_status` read before and after another person's marked
      request differing only by that request's folded spend, as exception 2 accepts.
    - The folded per-namespace-day row's `errors` count unchanged by a marked request
      that fails in the model call.
    - The session-content refusal of a hook-sent `context.pack` from a client that does
      not declare the flag.
    - An admitted mirrored item in `context.pack` writing no recall credit or exposure
      row.
    - A later `ingest-transcript` of a session flagged only by inheritance refused from
      an unflagged shell.
    - A session started from a flagged session's process (`claude -p` through its shell)
      flagged through the inherited marker, and a hook's `code.search` and
      `context.pack` sending nothing after the flag.
    - The owner's `reads_as_creator` token admitted to the owner-only list of a
      connected source; an on-behalf request for the owner and a token minted for the
      owner's subject without `sid` refused.
    - A `claude -p` child that read a mirror flagging its parent session, whose hooks
      then send nothing.
    - A namespace copy winning a dedupe carrying only its own chunks and scores; a
      reader only the namespace copy admits keeping it; a reader whose query matches
      only the mirror copy's revision getting that hit with `mirrored: true`.
    - A restore that drops a `loom.knowledge_mirrors` row while `acl-` rows stay in the
      documents, chunks, heads and bucket, then `loom knowledge mirror erase --all`
      deleting every one of them before the latch.
    - A non-owner admin's `sync_now` on a mirrored source with a refused item answering
      as on a clean source.
- Integration: `tests/integration/test_document_source_leakage.py`, beside
  `test_cross_principal_leakage.py`: for every leg with the check and every id-addressed
  op, a reader the list leaves out sees no mirrored document, its title, its address,
  its count or its existence, through the legs, `knowledge.reads`, `audit.list`, the
  org-wide rollups or a source's status (an admin who is not the owner included), under
  both storage authorities; a reader the list names sees it; a reader removed from the
  store's list stops seeing it after the next listing of every source that holds a copy;
  one mirrored source's retirement leaving another source's copy of the same address
  readable; a `shared` mirrored source's status, read by a viewer no list names, showing
  no per-file code.
- Integration: a `connected` source is mirrored wherever it writes; a SCIM deactivation
  and a disconnect stop the person's sources and retire their mirrored documents;
  `namespace.delete` and `collection.delete` retire the namespace's sources and clear
  their credentials.

## Related

- [Knowledge: document sources](../features/knowledge.md#document-sources)
- [Document extraction](document-extraction.md)
- [Security](security.md)
- [Sharing knowledge](sharing.md)
- [Identities and namespaces](../concepts/identities-and-namespaces.md)
- [Rolling upgrades](rolling-upgrades.md)
