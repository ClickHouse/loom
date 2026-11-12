# Document sources

A document source pulls files from a document store (a shared-folder store, a page
store, a project tracker, a meeting store) into a namespace's documents and keeps them
in step. This page is the design of sources on any namespace, sources of many items
given as pasted links, per-user connected accounts, an org-wide crawl, and access that
follows the store's own permissions. The caller's view of today's sources is in
[knowledge: document sources](../features/knowledge.md#document-sources); parsing is in
[document extraction](document-extraction.md).

## What changes

| Today                                                                                                  | With this design                                                                                                                                                                                                |
| ------------------------------------------------------------------------------------------------------ | --------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| A source lives only on a repository's trunk code namespace (`NOT_A_CODE_NAMESPACE`).                   | A source lives on any namespace ([any namespace](#any-namespace)).                                                                                                                                              |
| A source holds 1 to 20 folders, projects, pages or databases, and no op changes its `config`.          | A source holds up to 300 items of mixed types, given as links or ids, and `knowledge.source.update` adds and removes them ([items](#items-and-links)).                                                          |
| One shared credential per source, typed by an admin.                                                   | A shared credential, a person's own connected account, or a delegated service account that reads many people's drives ([credentials](#credentials)).                                                            |
| Every reader of the namespace reads every synced document.                                             | A source's `access` is `namespace` or `mirrored`; a mirrored document is shown only to a reader the store says may read it ([access](#access-follows-the-source)).                                              |
| A `linear` source reads project documents only.                                                        | Issues and comments are a later design of their own ([issues](#project-tracker-issues)).                                                                                                                        |
| A `recording` source reads meeting-store transcripts (`meet`) or cloud-recording transcripts (`zoom`). | Meeting notes and transcripts that the meeting store saves to the shared-folder store come in through a `gdrive` source; `recording` keeps `zoom` only ([meetings](#meetings-through-the-shared-folder-store)). |

## Any namespace

A source can be registered on any active shared namespace or collection; who may
register which kind is in [credentials](#credentials). The refusal
`NOT_A_CODE_NAMESPACE` goes; `add` on an inactive namespace answers
`NAMESPACE_INACTIVE`. A source on a repository's trunk code namespace still tags each
document `repo:<id>`, so the repository's `code.context` and `loom_search` legs read
them as today; on any other namespace, a branch namespace included, it does not. A
person's own collection and `personal-` namespace hold no source
(`PERSONAL_COLLECTION`): a user erasure erases what the person wrote, and a source's
documents are the sync worker's. The sync skips a source whose namespace is inactive.

A source ends with its namespace: `code.repo.remove`, `namespace.delete`,
`collection.delete` and a namespace erasure each retire the namespace's sources under
its source lease, clearing their credentials, as `code.repo.remove` does today, so no
source outlives its namespace with a sealed credential or writes back what an erasure
took.

Sync state stays in `loom.code_index_state` under `knowledge_source:<name>`, keyed by
the source's namespace, which the namespace's erasure already covers.

On the dashboard, the Library shows a Sources section for the namespace or collection in
view; the repository card keeps its own.

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
writes its deleted readers rows, retires, and lands its listing row, so its copies drain
back under the bound and no list goes stale; `inspect` and `loom doctor` show
`too_many_documents` with the count meanwhile. The read of a source's stored copies goes
in pages by `source_uri`, so it never cuts one off. A listing asks the store about at
most 50 parent folders per query (not a setting), so a source of many folders lists in
several queries; a recursive folder's subfolders are walked on every full listing, and a
file in a new subfolder arrives with the next one. A probe checks a bounded sample:
every item while there are at most 20, otherwise the 20 items added last, and the answer
says `sampled: true` with the number checked. Only a refused item of the sample refuses
`add` or `update`; the next sync turn reports any other as `item <n>: http_404` and goes
on with the rest.

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
written in it, else `{}`.

`knowledge.source.update` takes `add_items`, `remove_items` (each also as links) and a
new `include`, probes what it adds with the stored credential (the same sample rule),
and writes the source at its next version under the namespace's source lease. Its audit
row names the namespace, the name and the items added and removed, with a `filter`'s
`owner` email and a crawl's `subjects` and `readmit` emails as keyed hashes (below).
Removing every item is refused (`BAD_CONFIG`); `remove` ends a source. Changing `access`
is a new source. For a `crawl`, `update` also takes `subjects` (the new list, probed
like added items) and `readmit` (emails whose pseudonyms leave `excluded`, for a person
who comes back), under the same roles as `add`.

### One document, one owner

One document per address per namespace, as today, owned by one source: the source whose
`source:<name>` tag it carries, written by the sync worker (today's ownership check).
Only the owner rewrites the document when its file changes and retires it on positive
evidence (today's retirement rule), and a document another writer (a person, an agent)
wrote is never overwritten or retired by any source, as today. A mirrored document also
carries `source-id:<id>`, the id of the source that wrote it, and in a mirror that tag,
not the name, decides ownership: a later source of the same name owns none of an earlier
one's documents, and the cleanup of a stopped mirrored source retires by it
([below](#who-the-store-says-may-read)). Only N writes a mirror, so every mirror
document carries the tag.

A second source that lists a file at an address where a document is counts it `skipped`,
as today, and fetches no content. No source reads whether another is live: an address is
free exactly when no document is there. Once the owner's document is retired (by today's
[retirement rule](../features/knowledge.md#document-sources), its `remove` with
`delete_documents`, or any stop of a mirrored source), the next source that lists the
file writes it as a new file under its own tag: a mirrored source at its next complete
listing, a `namespace` source at its next full listing
(`knowledge_source_sync_full_interval_seconds`). A kept document (one a `remove` without
`delete_documents` left) is skipped by every source until `knowledge.delete` removes it,
as today.

So a file that two sources reach is written once and read through one document. Between
the owner's retirement and the other source's next listing of the file, the document is
missing; that gap is the cost of keeping one owner per address instead of recording
every source that reaches it.

`remove` with `delete_documents` retires the source's documents, as today, and without
it they stay as kept documents, which `add` of the same name refuses to take over
(`SOURCE_DOCUMENTS_KEPT`, as today). A mirrored source's documents are always retired
([below](#where-a-mirrored-document-lives)).

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
with the owner's account; its owner or an admin of the namespace may `check`,
`sync_now`, `remove` it and remove items. `rotate` stays `admin` and is refused for a
`connected` source, whose credential is its owner's connection. Owners are compared as
canonical subjects (`server/auth/aliases.py` `canonical_subject`), so a person's other
spellings are the same owner.

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
  ticket (`_link_cookie` in `server/auth/oauth.py`). The dashboard's page calls `POST
/v1/connect/<kind>/start` with its bearer token, which must carry a sign-in session
  (`sid`), which a service-account token lacks; the cookie below, not the token, is what
  ties the connection to one browser. The server keeps a single-use `state` in Keeper
  for 10 minutes, bound to the subject and to a random binding it sets in that browser
  as an HttpOnly, `SameSite=Lax` cookie, and answers the store's consent URL, which the
  page opens. The store redirects to `GET /v1/connect/<kind>/callback`, which stores the
  connection only when the request carries the cookie whose binding the `state` names;
  the subject is the one recorded at start. A consent URL sent to someone else attaches
  nothing: their browser has no such cookie. `loom connect` opens the dashboard's
  Connected accounts page for the kind, where the person signs in if they have not. PKCE
  where the store takes it; `include_granted_scopes=false`, so an earlier grant of the
  same client is not folded in.
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
  it. `connection.list` and `connection.disconnect` act on the caller's own; a user
  erasure deletes its connections and retires their sources
  ([erasure](#erasure-and-audit)). An org admin sees in admin Users which kinds each
  person has connected, never a token or the store account's email.
- Connecting again with another store account replaces the connection and stops the
  sources that used the old one, as a disconnect does: their items were chosen for the
  old account.
- `connection.disconnect`, and a SCIM deactivation for every subject it revokes, clear
  the sealed credential and mark every source that uses it inactive, in the request. The
  sync worker then revokes the credential at the store (best effort) and retires those
  sources' mirrored documents. A turn also checks the owner: a source whose owner
  subject is revoked, deactivated, or no longer holds `writer` on the source's namespace
  is stopped (written inactive under the source lease, `status: owner_inactive`) and
  goes as a removed source does, so a missed hook fails closed.

A connected source reads the person's own files, the files and folders shared with them,
their recordings folder, and the pages and projects they can read. It reads no
directory, so it expands no group ([below](#who-the-store-says-may-read)).

### Org-wide crawl

A `crawl` source's credential is a service-account key whose client id the org's
workspace administrator granted domain-wide delegation for `drive.readonly`,
`admin.directory.group.member.readonly` and `admin.directory.user.readonly`. Its config
holds `directory_subject` (a workspace administrator the directory reads impersonate),
`excluded` (people a user erasure took out, each as a UUIDv5 of the lower-cased email,
as SCIM keys a user: a pseudonym the directory read compares, not the address) and
`subjects`: people (emails) and groups (group emails), whose members the directory read
lists at each turn, at most 2000 people (not a setting). For each person the sync
exchanges the key for a token that impersonates them (the JWT bearer grant with `sub`)
and lists the source's items as them. A typical config is one `meet_recordings` item and
a `filter` with `kinds: [docs]`, over a group: every member's meeting notes, without
anyone connecting.

The probe checks that the delegation grants no write on what the crawl reads: it asks
for each of the three scopes, which must be granted, and for
`https://www.googleapis.com/auth/drive`, `admin.directory.user` and
`admin.directory.group`, which the store must refuse; a credential the store grants any
of them is refused (`CREDENTIAL_REFUSED`). It cannot prove the delegation grants nothing
else (a mail scope, say), and the page says so to the admin who registers it. The audit
rows of `add`, `update` and `rotate` record the configured subjects as keyed hashes (an
HMAC under the root secret, as SCIM's audit rows hash a subject, so a list of the org's
addresses does not reverse them). `excluded` and the readers and listing `account` keys
are unkeyed UUIDv5s instead, for the reason SCIM's ids are: they must still match after
the root secret is rotated, which an HMAC would not; the audit hash never has to match
anything later. Audit rows also record the scopes granted and the number of members
resolved: the audit trail outlives erasure and keeps no crawled person's address.

## Access follows the source

A source's `access`:

- `namespace`: every reader of the namespace reads every document, as today. The only
  mode for a `shared` source of a store whose permissions the sync does not read
  (`notion`, `recording`), and the default for `shared` `gdrive` and `linear` sources.
- `mirrored`: a document is shown only to a reader the store says may read it. Required
  for every `crawl` and every `connected` source. A `connected` `notion` source, whose
  store shows an integration no page's permissions, therefore shows each page only to
  its owner, through the readers rows of the owner's own listings (rule 3 below).

A deployment with auth off (`LOOM_AUTH_REQUIRED=false`) has no reader identity to check.
It refuses to add a mirrored source (`AUTH_REQUIRED`), and when auth is switched off
after mirrors exist, the gate still refuses every mirror name (the `acl-` rule runs
before `check_authorized`'s return for a request with no auth), the mirror legs answer
nothing, and the sync skips mirrored sources.

### Where a mirrored document lives

A mirrored document is not stored in the source's namespace but in that namespace's
mirror: a namespace named `acl-<24 random hex>`, made under the namespace's source lease
(read sequentially, so two first mirrored adds make one) with its first mirrored source
and recorded in `loom.knowledge_mirrors` (org, namespace, mirror), after the namespace
is checked active under that lease. The gate admits no token to a name of that form
(`acl-[0-9a-f]{24}`): `check_authorized` and `CachedGate` refuse the name before any
other rule, the auth-off return included, and `namespace.register` and `restore` refuse
the form. `erasure.request` names its scope in `scope_type` and `scope_value`, not
through the gate, so `handle_erasure_request` refuses an `acl-` name as a `namespace`
scope or inside a `namespace_list` for every caller but a platform admin, an org admin
included. So no token searches, reads or exports a mirror; `erasure.export` of a
namespace or of a `user_id` leaves mirrors out, and the threat model's rows hold for
every token. The erasure saga's own steps read the mirror below the gate, as they read
every table. A boot warning lists any existing namespace of that form, as it does for
`col-` names. A mirror has no directory row and appears in no namespace listing or usage
view. Its row in `loom.knowledge_mirrors` holds no personal data and is never deleted,
an erasure's included, so a namespace name keeps one mirror name for life: a name
deleted and registered again inherits the mirror, and `loom.knowledge_mirrors` joins the
names `namespace_census.in_use` checks: it looks the name's mirror up and runs the
census tables' branches over the mirror's name too, so a name whose mirror still holds
rows counts as in use, as its own documents would. A namespace's mirror is looked up
through a per-process cache: a found mirror never changes, and an absent one is asked
again after 30 s (not a setting), so a new mirror's documents show at most that late.
With auth off, the id-addressed checks (`row_reach`, `row_in_scope`) admit every
request, but they address memory rows, which no mirror holds.

A knowledge read taught the mirror adds a leg over the namespace's mirror, with the
check below. Every other read path reads the namespace and finds nothing of the mirror.
So a new op, a context-pack leg or an export that nobody taught the check shows no
mirrored document, and neither does an N-1 replica. A permission column on
`loom.documents` would make the opposite the default;
[sharing](sharing.md#alternatives-not-taken) rejected that for collections, since one
missed path leaks.

- **Legs with the check:** `knowledge.search`, `get`, `outline`, `list`, `download`,
  `synthesize`, `knowledge.inspect` (titles), `knowledge.source.inspect` (a mirrored
  source's documents), the `search.query` knowledge and repository-docs legs,
  `code.context`'s docs section, `context.pack`, `memory.stack`, `memory.ask` and the
  assistant (which calls the same handlers under a token that carries the person's
  subject). A `get`, `outline` or `download` of a mirrored address the reader may not
  see answers `found=false`, as for an absent one.
- **Nothing to read:** decision mining and dataset extraction skip mirrored documents,
  since a decision row is a memory every reader of the namespace reads and a dataset is
  read by `knowledge.query` without the check; so `knowledge.query` and the dataset ops
  find none. `dedup_list` and the tune sample read no mirror.
- **Reads recorded under the mirror:** a mirror leg's `agent_events` row is filed under
  the mirror's namespace, so `knowledge.reads` on the namespace names no mirrored
  address, and the SQL surface's ledger and usage views (`sql_v1_agent_events`,
  `sql_v1_usage_daily`) and the insights usage reads leave `acl-` names out.

A mirrored source's documents are never kept past it: `remove` retires every one it owns
(`delete_documents` is implied), and so do a disconnect, a SCIM deactivation, the owner
check and every retirement of a namespace's sources (`namespace.delete`,
`collection.delete`, an erasure, `code.repo.remove`). The trigger is an open cleanup row
([below](#who-the-store-says-may-read)), whatever the source row's `pending_delete` or
`access` columns say, so an N-1 worker that clears `pending_delete` after counting no
document in the namespace stops nothing. A `namespace` source's documents go as today:
`remove` with `delete_documents` sets `pending_delete`, and the retirement pass both
builds run retires them by tag in the namespace. The retention pass reads
`loom.knowledge_mirrors` and applies the namespace's `retention_days` to its mirror.
Every `add` runs today's name checks on the namespace (`SOURCE_DELETE_PENDING`,
`SOURCE_DOCUMENTS_KEPT`), mirrored or not, so a mirrored source never replaces the row
of a `namespace` source whose documents wait to be retired or were kept; the mirror
needs no name check, since a mirrored document is never kept and ownership there goes by
source id. In a mirror, every per-source document read and check (`count_documents`,
`source_documents`, `source_revisions`, the ownership check `_ours` that gates each
write and retirement) goes by `source-id:<id>`, not by name.

A namespace source and a mirrored source that reach the same file own two documents, one
in the namespace and one in its mirror. The namespace copy is readable by every reader,
because an admin registered a source that makes it so; search answers the address once.

### Who the store says may read

A mirrored document's own row and three tables under the mirror say who may read; the
tables are written as new versions with a `deleted` flag; only a user erasure deletes
rows ([below](#erasure-and-audit)):

- The document's permission list, in an `access_json` column of the document row
  (`loom.documents`, and the knowledge head under the Postgres authority), as of the
  newest read by the source that owns the document: `emails` (people the store grants
  any role, lower-cased), `groups` (group emails), `domains` (a domain permission the
  store marks discoverable), `anyone` (a public permission the store marks
  discoverable), `changed_at` (when the list last differed from the one before it, a
  change into `unread` included; an `unread` list refused again changes nothing) and
  `unread` (no current list is known: no account that lists the file could read it, or
  the source stopped listing the file and its retirement is not confirmed yet). A new
  document is written with the list its fetch reads with the file's metadata. A listing
  that reads a list unlike the stored one rewrites the document's row with the same
  content and the new list, as `knowledge.update_metadata` without `reindex` does today,
  so no chunk or embedding is rebuilt; an unchanged list costs no write. The sync's
  comparison read changes from today's `(source_uri, version)` match against
  `loom.chunks` to a match on the document's `revision` (`loom.documents.revision`
  against the chunks' `chunk_metadata` `revision`), which the rewrite keeps, so the next
  turn finds the copy current and fetches nothing; a chunk written before chunks
  recorded a revision (`revision` 0) still matches by `(source_uri, version)`, so no
  existing copy is fetched again. Under the Postgres authority the comparison starts to
  read each copy's live chunks (their `revision` and `doc_version`) from `loom.chunks`
  and judges them with `serves()` against the head, which records `chunks_revision`, and
  it compares the list against the head's ([reads under the Postgres
  authority](#reads-under-the-postgres-authority)). The list lives and dies with its
  document: a retirement, an erasure or the mirror's retention removes it with the row,
  and no read op returns the column. Only the owning source's turn writes it, under its
  turn lease, so the document's own version order is the list's.
- `loom.knowledge_document_readers`, one row per mirrored address, source and reading
  account: the connection (its kind and store account id) for a connected source, or the
  person a crawl listed it as. The `account` column holds a pseudonym of that account,
  never an address: a UUIDv5 of `<kind>:<account id>`, or of the person's lower-cased
  email (the form `excluded` uses), which a reader's own connections and verified emails
  are mapped to before the check compares them. Listing a file proves that account reads
  it. It is written when the account's listing finds no live readers row for that
  address, source and account (its first listing of the file, or the first after a
  retirement deleted the row), and written `deleted` when a complete listing by that
  account no longer has it, so an unchanged file costs no write. A `shared` mirrored
  source writes none: its service account's listing proves no person reads anything.
- `loom.knowledge_source_listings`, one row per source and reading account, keyed by the
  same pseudonym (and `shared:<source id>` for a `shared` source): when that account's
  last complete listing started. A listing that spans several passes counts from its
  start, the earliest moment it can have seen a file. A complete listing has read the
  list of every file it listed (each address once per turn, by whichever account lists
  it first) and rewritten every changed one before its row lands, so the oldest start
  among a source's listing rows is when its documents' lists were last confirmed. A file
  is dropped when no account's latest complete listing of the source has it; the listing
  whose completion drops it lands its row only once the file is retired or its list
  marked `unread`, as the deleted readers rows land first, and a retirement cut short by
  the pass bound keeps that listing's older start. So no older list outlives the
  listing.
- `loom.knowledge_group_members`, one row per source and group: the members of a group
  the crawl's directory read listed, and `read_at`. A group is stored once, not on every
  file shared with it.

A stopped mirrored source's rows and documents go on one trigger, whichever path stopped
it: an open row of `loom.knowledge_source_cleanups`, written by every stop N makes and
by the anti-join below. Every stop writes it under the source lease, with the source's
id, name, namespace and mirror. Because the row is keyed by the source's id, a source
added later under the same name, which replaces the stopped row in the registry, cannot
hide it. A `namespace` source writes no readers, listing or group-members row and needs
no cleanup row.

A pass cleans at most 100 open cleanup rows, oldest first, each under that source's own
turn lease (`knowledge-source-sync:<org>/<namespace>/<name>`, as today's retirement of a
removed source runs), leaving it for the next pass when the lease is busy; a turn still
in flight finishes, or stops at its next write's check, before the cleanup starts. Each
batch of document, readers, listing and group-members writes first checks the source's
registration (`require_current`, one read per batch), so a turn that starts after the
cleanup writes nothing either, and no such row lands after it. For each it writes
`deleted` the source's readers, listing and group-members rows, then retires the
mirror's documents carrying its `source-id:<id>` tag, re-checking that tag on each
document before its retirement, so a document another source wrote is never retired. It
then marks the cleanup done; a run cut short is run again from its open row and finds
the documents still tagged. So no row of a stopped source can admit anyone, and none
names an account in any form other than its pseudonym, even for a source that never
reached a file.

The anti-join finds what no stop of N recorded: a mirrored source N-1 stopped (its row
inactive, with its `source_id`), and one whose name an N-1 `add` has since reused (its
row gone from the registry under `FINAL`). It reads, with `FINAL` and live rows only
(not `deleted`), the distinct `source_id`s of the readers, listing and group-members
tables and of the mirror documents' `source-id:<id>` tags (a mirrored source stopped
during its first listing has readers rows and perhaps documents, but no listing row
yet), maps each back to the namespace whose mirror holds it (one read of the org's
`loom.knowledge_mirrors` rows, which are few) and to its name (the readers, listing and
group-members rows carry it, and a document its `source:<name>` tag), and uses that
namespace and the name for the turn lease's key. It does these reads under
`sequential_reads()`, and takes each id that no active registry row carries and no
cleanup row, open or done, already names, at most 100 a pass. For each it reads again,
under the source lease, that no active registry row carries the `source_id` (skipping it
if one does, so a replica's lag never retires a live source), and only then opens its
cleanup; until it runs, a readers row with no live listing row of its source admits
nobody under rule 3. N-1 writes no mirror document and no row of these tables, so these
reads see every mirrored source. The implementing pull request measures the anti-join's
cost per pass. A source whose registry row a purge deleted went with its namespace's
erasure, mirror included.

A person a crawl no longer reads (gone from a group it expands, taken out of `subjects`,
or excluded) is treated the same way, on positive evidence only, as today's sync retires
a file: the turn whose complete, successful directory read no longer lists them writes
`deleted` their readers and listing rows and drops their cursor, and a document only
their rows held counts as no longer reached by the source. A directory read that fails
or is cut short keeps the previous expansion and drops nobody, and an expansion past the
cap of 2000 people fails the turn and drops nobody.

When an account's complete listing ends, the sync first writes `deleted` the readers
rows of the files it no longer had, and writes the listing row's new start only once
those have landed. A listing row is never newer than the readers rows it vouches for, so
a crash between the two leaves the older start, which proves less, not more.

A link-only permission (an anyone-with-link or domain-with-link share, which the store
marks not discoverable) admits nobody: it lets someone who holds the link open the file,
not find it in a search. For `gdrive` the list is the file's `permissions` (type, role,
`emailAddress`, `domain`, `allowFileDiscovery`), read with the listing when the reading
account may share the file. When it may not (a view-only file), or for a file in a
shared drive, where the listing leaves `permissions` out, the turn reads
`permissions.list` with `supportsAllDrives`. When that is refused too, the same turn
reads it as another account of the source that lists the file, and marks the list
`unread` when none can, before the listing row of the account that first listed it
lands; then only the readers rows prove access. For `linear` the list is the union of
the members of every team the project belongs to. A `crawl` source expands, through its
directory read, the groups its `subjects` name, and only those, within its cap of 2000
people; a group on a file's list that no crawl of the mirror expanded admits nobody, and
the people named directly still read. A `connected` source reads no directory and
expands no group itself.

A reader sees a mirrored document when rule 1 holds, and rule 2 or rule 3:

1. The reader holds `viewer` on the source's namespace (the ordinary gate).
2. The document's list is not `unread`, the oldest start among its source's listing rows
   is within `LOOM_KNOWLEDGE_SOURCE_ACL_STALE_SECONDS` (default 3600) of now, and one of
   the reader's verified emails is in its `emails`, or in the members of one of its
   `groups` from an equally fresh members row, or that email's domain is in its
   `domains`, or its `anyone` is set.
3. A readers row matches the reader (one of the reader's own active connections for a
   connected source, one of its verified emails for a crawl), and the listing row of
   that readers row's own source and account says its last complete listing started
   within the stale bound and after the document's list last changed (`changed_at`),
   which a change into `unread` moves too, so a reader the last list left out stays out
   until their own account lists the file again.

The newest evidence wins: Alice's and Bob's connected sources that both reach a
view-only file each prove their own account, and each of them reads it; a re-read that
finds the same list voids no listing, and a person a changed list leaves out keeps no
access through a listing older than the change.

The reader's verified emails are its subject's directory email when `VERIFIED_EMAIL_SQL`
admits it (an SSO account or a linked one) and the emails of the subject's SSO links; a
domain matches only through them. A connection's store email is not one of them: a
connection admits only through the readers rows of its own listings (rule 3). A
delegated token (an agent's, the assistant's: a service account with a `creator`) reads
with its creator's emails and connections. Every other service account, bot and end-user
token has none, so it sees only `anyone` documents. A deployment without SSO admits
through connections alone.

Mirrored sources list every file on every turn, metadata only (id, revision,
permissions). They are full listings because the store does not change a file's
modification time when its sharing changes, so an incremental listing would miss a
permission removed. A pass runs those listings first, rotating across the mirrored
sources from where the previous pass's listings stopped, by a listings position of its
own: a `loom.code_index_state` row under org and namespace `-` and the key
`knowledge_source_listings`, holding the `org/namespace/name` of the next source to list
(no personal data; an N-1 pass never reads it, so its own resume cursor on
`loom.worker_status` is untouched). Listings take at most a third of the pass deadline
(`knowledge_source_sync_max_pass_seconds`) and sit outside the pass's document bound,
which counts fetches and writes of content. Content keeps today's start bound of three
quarters of the deadline from the pass start, so when listings use their third, content
has five twelfths instead of three quarters; decision mining keeps its tail. Its give-up
rule (`_FULL_READ_SHARE`, a document given up at once when it ran out of time with three
quarters of the read time) assumed the sync before it rarely takes a quarter; with
listings first it would never fire, so the implementing pull request measures that share
against the read time left after the listings. That slower drain of a content backlog is
the accepted cost of keeping every mirrored source's permissions fresh. Content is still
fetched only for a file whose revision changed: a listing records each file's revision
against the stored copy's, and the files it finds changed wait in the source's sync
state for the content phase of this or a later pass. Groups are expanded on every turn,
once per group per turn. A listing that does not finish in a pass keeps one cursor per
account in the source's sync state, keyed by the account's pseudonym (the item it
reached and the page token), and goes on from it in the next pass; until it completes,
that account's listing row keeps its older start, so every file it alone proves ages
together, and so does every list of the source. A large mirrored source therefore needs
a stale bound longer than the time to list it all; `inspect` and `loom doctor` report
the oldest listing start each source has. The cost per turn is the listing pages (1000
files each), one permissions read per file the listing leaves it out for, one document
write per file whose list changed, one directory read and one members row per group, and
readers writes only for a file an account starts or stops listing. The implementing pull
request measures the writes of a crawl over 2000 people and puts the figures here.

Each mirror leg first computes the addresses the reader may see: one read of the
mirror's lists from `loom.documents` and of its three tables with `FINAL`, newest row
per key: the lists that name the reader through `emails`, `domains`, `anyone` or a group
the reader's group-members rows hold, the readers rows by the reader's accounts and the
group-members rows by `members` (`has` and `hasAny`), and the listing rows unfiltered,
since rule 2 takes the oldest `started_at` over every account of the source; then the
chunk search, outline or list restricted to those addresses before its `LIMIT`, so a
page is never cut short by the check, except for an address the head check below drops.
Any skip index on those tables is read only under
`use_skip_indexes_if_final_exact_mode=1`, so a granule skipped for its newest version
cannot leave an older one that names the reader. No admitted set is cached. The
implementing pull request measures the leg (rows read, memory, p50) on a mirror of a
million documents, under both storage authorities, and puts the figures here. A leg that
cannot read the tables shows no mirrored document and marks the answer `degraded:
mirror_unread`.

#### Reads under the Postgres authority

The admitted set comes from `loom.documents`, and a commit there can lag the head. So
every leg with the check then judges each address it is about to answer by its head: a
deleted head drops the address, and otherwise rules 2 and 3 are evaluated again with the
head's `emails`, `groups`, `domains`, `anyone`, `unread` and `changed_at`, against the
same readers, listing and group-members rows. A leg that already reads the head (through
`pg_serving`, or as `knowledge.get` and `knowledge.inspect` serve it) uses that head;
any other gains one head read per page of addresses, which the cost measurement above
covers. A mirror address always has a head, since only N writes mirrors and every write
under this authority commits one; an address found without one is dropped. A leg that
cannot read the heads shows no mirrored document (`degraded: mirror_unread`). So a
narrowing or a retirement committed and not yet published, or held by a parked delivery,
already applies; a widening applies once published.

A stopped sync hides every mirrored document once the stale bound passes.

### Threat model

| Threat                                                                   | Boundary                                                                                                                                                                                                                                | Enforced by                                                            |
| ------------------------------------------------------------------------ | --------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- | ---------------------------------------------------------------------- |
| A connected person leaks a private file into a team namespace            | Every connected source is mirrored; its documents live in the mirror, and a teammate sees one only when the store names them. `access` cannot change.                                                                                   | `add` and `update`; the mirror gate; `test_document_source_leakage.py` |
| A crawl's registrant reads crawled people's private files                | No rule admits a source's registrant or owner as such; a registrant reads a file only when the store names them or their own account lists it.                                                                                          | the access check                                                       |
| An admin points someone's connected account at files they did not choose | Only a connected source's owner adds its items or changes `include`.                                                                                                                                                                    | `update`                                                               |
| A link-only share becomes a search hit for everyone                      | A permission the store marks not discoverable admits nobody.                                                                                                                                                                            | the access check                                                       |
| A person stops reading a file and Loom keeps showing it (stale ACL)      | Every turn of a mirrored source lists every file's permissions and rewrites a changed list, so a removal applies at the next listing; a source whose listings are older than the stale bound admits nobody through a list.              | the sync's mirrored listing; the access check                          |
| A person leaves a group and keeps reading through it (group churn)       | Groups are read on every turn into a members row of their own with its `read_at`; a stale or unread group admits nobody.                                                                                                                | the access check                                                       |
| Auth is switched off after mirrors exist                                 | The `acl-` refusal runs before the auth-off return; mirror legs answer nothing and the sync skips mirrored sources while auth is off.                                                                                                   | `check_authorized`; the mirror legs                                    |
| A person leaves the org and their connected sources keep syncing         | SCIM deactivation and disconnect stop the sources and clear the credential; each turn's owner check skips a source whose owner is revoked, deactivated or no longer a writer.                                                           | `server/auth/scim.py`; the sync's turn                                 |
| A crawl reads more than its registrant meant, or can write               | An org-wide role to register; the probe requires the store to refuse a write scope; subjects and scopes in the audit row.                                                                                                               | the crawl probe; `loom.audit_log`                                      |
| An id-addressed read confirms a mirrored document exists                 | `get`, `outline`, `download` and the inspects answer `found=false` or leave out an address the reader may not see; `knowledge.reads` files mirrored reads under the mirror.                                                             | the mirror legs                                                        |
| A new read path shows mirrored documents to everyone                     | Mirrored documents live in a namespace no grant opens and no listing names; a path that does not read the mirror shows none.                                                                                                            | `check_authorized` (`acl-[0-9a-f]{24}`)                                |
| An older build shows mirrored documents                                  | N-1 reads no mirror; its gate does not refuse a mirror's name, so the name is random and in no listing, and a rollback first erases the mirrors.                                                                                        | the random name; the rollback procedure                                |
| A mirrored document's text reaches every reader through a derived row    | Decision mining and dataset extraction skip mirrored documents.                                                                                                                                                                         | the sync                                                               |
| A store account is linked into another person's Loom account, either way | The connection is stored only from the browser that started it, with that subject's own dashboard session and the `state` cookie; a consent URL sent to someone else attaches nothing. A connection's email is never a reader identity. | the connect routes                                                     |
| A caller injects store query syntax through a filter                     | Filters are fields with patterns, escaped and built by the server; no raw query is taken.                                                                                                                                               | `server/clients/gdrive.py`                                             |
| A stolen connected credential changes the store                          | Only read scopes are accepted; the credential is sealed and never returned.                                                                                                                                                             | the connect callback; `server/core/secrets_at_rest.py`                 |

Out of scope: a reader who already saw a document and kept its text; the store itself
reporting a wrong permission list.

## Meetings through the shared-folder store

The meeting store saves a meeting's transcript and its notes as Docs in the organiser's
recordings folder of the shared-folder store, and shares them with the participants. A
`gdrive` source reads them like any file: a `meet_recordings` item, or a `folder` item
for that folder. `meet_recordings` finds the folder the meeting store made in each
account's own drive by its name, from a fixed table of the names the store gives it in
each language; when none is found, the turn reports `meet_recordings: not found` for
that account, and a `folder` item with the folder's link reads it instead.

The sync recognises a transcript Doc (one in a recordings folder whose exported text has
the store's transcript shape: a title line, then speaker turns under time stamps) and
writes it with `server/knowledge/domain/transcripts.py`'s timed sections,
`ingested_format: transcript`, as a `recording` source writes one, so a citation names
the minute. A meeting-notes Doc is written as any Doc. Its permission list (the
organiser and the invited participants) makes a mirrored source show each meeting to the
people invited.

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
- The plane's statements filter by `repos` (and, in a standard namespace, by `is_private
= 0`), never by org or namespace, so a Loom-kept table needs an (org, tracker
  namespace) predicate on every read.
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
  and the documents each owns. To a viewer who is neither the owner nor an admin, a
  `connected` source shows its kind, its owner as "a connected account", its item count
  and its last sync's status and counts, not its items, `config`, `created_by` or the
  error text, which can name an item. Python and TypeScript SDKs are regenerated.
- **CLI.** `loom knowledge source add|update|list --namespace <ns> --link <url>...`
  (`--credential-file`, `--connected`), `loom connect gdrive|notion|linear`, `loom
connection list|disconnect`, `loom knowledge mirror erase --all` (the rollback step),
  and `loom doctor`'s line for the oldest access read of each mirrored source.
- **Dashboard.** The Library's Sources section: paste many links, a preview of what each
  resolves to and which could not be read, Check, Add, edit items, the access mode, and
  each source's last sync. Settings → Connected accounts: connect, see, disconnect. The
  repository card keeps its own sources.
- **Agents.** Every op reaches an agent through `loom_op`. The mentor reference gains a
  line: handed a store link and asked to bring it in, call `knowledge.source.update`
  with `links` on the caller's connected source for that store, or `add` one; with no
  connection, `connection.list` answers the dashboard link `<public
base>/settings/connected-accounts?kind=<kind>` to hand back.

## Data model

ClickHouse, additive migrations:

| Table                              | Key                                                | Holds                                                                                                                                                                                                       |
| ---------------------------------- | -------------------------------------------------- | ----------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `loom.knowledge_sources` (columns) | `(org, namespace, name)`                           | `source_kind`, `credential_mode` (default `shared`), `access` (default `namespace`), `owner_subject`, `items_json`, `options_json` (a crawl's `directory_subject`, `subjects` and `excluded`), `secret_enc` |
| `loom.knowledge_mirrors`           | `(org, namespace)`                                 | `mirror`, `created_at`                                                                                                                                                                                      |
| `loom.documents` (column)          | as today                                           | `access_json`: a mirrored document's permission list (`emails`, `groups`, `domains`, `anyone`, `changed_at`, `unread`); empty elsewhere; the knowledge head under the Postgres authority gains it too       |
| `loom.knowledge_document_readers`  | `(org, namespace, source_uri, source_id, account)` | `name`, `deleted`, `version`; `namespace` is the mirror; `account` is the UUIDv5 pseudonym of `<kind>:<account id>` or of the lower-cased email                                                             |
| `loom.knowledge_source_listings`   | `(org, namespace, source_id, account)`             | `name`, `started_at`, `deleted`, `version`; `account` is the pseudonym, or `shared:<source id>`; the start of the account's last complete listing; `namespace` is the mirror                                |
| `loom.knowledge_group_members`     | `(org, namespace, source_id, group)`               | `name`, `members`, `read_at`, `deleted`, `version`; `namespace` is the mirror                                                                                                                               |
| `loom.knowledge_source_cleanups`   | `(org, namespace, source_id)`                      | `name`, `mirror`, `done`, `version`; a stopped mirrored source whose documents and rows wait to be cleaned                                                                                                  |
| `loom.connected_accounts`          | `(org, subject, kind)`                             | `account_id`, `account_email`, `scopes`, `credential_enc`, `active`, `connected_at`, `disconnected_at`, `version`                                                                                           |

A source N-1 can operate on (`shared`, `access: namespace`, with items today's config
can express) keeps today's columns as they are: `kind`, its credential in
`credential_enc` and its items in `config_json`, which N writes alongside `items_json`.
Every other source (mirrored, `connected`, `crawl`, or with an item today's config
cannot express) is stored with `kind` empty and its kind in `source_kind`, its
credential in `secret_enc` (purpose `knowledge_sources.secret`, which `loom secrets
reseal` covers) and its items in `items_json`, with `credential_enc` empty and
`config_json` `{}`. `remove`, the retirement of a namespace's sources and a disconnect
clear `secret_enc` as they clear `credential_enc`. N reads `source_kind` first and
`kind` when it is empty, and `items_json` first and `config_json` when it is empty
([rolling upgrades](#rolling-upgrades)). `loom.knowledge_sources` joins the tables whose
version writes carry the columns a build does not know (rule 2), so a later release's
columns survive N's writes. The mirror's documents and chunks live in `loom.documents`
and `loom.chunks` under the mirror's namespace, so the document machinery (heads under
the Postgres authority, deliveries, originals, the blob sweep) applies unchanged.

## Erasure and audit

- A namespace's erasure covers its mirror. Its first step (`knowledge_sources.retire`)
  retires the namespace's sources under the source lease; the erasure does not make the
  namespace inactive, so a source can still be added after it, and the step therefore
  runs again on every resume, never skipped as completed. The saga then reads the
  namespace's mirror from `loom.knowledge_mirrors`, which is never deleted, and every
  namespace-keyed step and census entry runs over the mirror's name too, so a step added
  later covers it by the same rule. A census that finds a live source or a mirrored
  document added after those steps reopens them (`steps_to_reopen`).
- `loom.knowledge_sources` moves from `NO_MEMORY_CONTENT` into the namespace steps: it
  holds a connected source's owner, a crawl's subjects and a filter's `owner` email, so
  `knowledge_sources.retire` clears a retired row's credentials, owner, options and
  items, and a purging erasure deletes its rows.
- `loom.knowledge_source_cleanups` and the three access tables are keyed by namespace
  and erased with it, and a document's list goes with its row. The lists and
  `loom.knowledge_group_members` hold people's emails copied from the store's permission
  lists and directory, people who are not Loom users among them; a list goes with its
  document and a members row is written `deleted` when its source stops or no longer
  reads that person, and otherwise mirror the store's current lists, so an erasure by
  `user_id` does not select them; the crawl rows that name an erased person go as the
  next bullet says.
- A user erasure (`user_id`) reaches the connections of every subject that spelling maps
  to (`server/auth/aliases.py` `spellings`, `user:<id>` included). Its step reads those
  connections first, then deletes outright (a `DELETE`, every version, as the user steps
  delete a grant row of the user) the readers and listing rows keyed by their
  pseudonyms, retires their connected sources with their mirrored documents, and deletes
  the `loom.connected_accounts` rows last. It also takes the person's verified emails
  out of every crawl of the org: out of `subjects`, and into the crawl's `excluded` list
  as pseudonyms, which group expansion skips, so no address of the person stays in
  `loom.knowledge_sources` and the census counts none. It deletes outright every crawl
  readers and listing row keyed by the pseudonym of one of the person's verified emails,
  since no listing by them runs again to do it, and rewrites each crawl's sync state
  without that person's cursor; those `DELETE`s are in `_EXPECTED_MUTATIONS`, and the
  census counts these tables without `FINAL` or the `deleted` filter. Documents only
  that person's listing reached are retired. A crawl's documents are the org's, so a
  document someone else's listing still reaches stays. Disconnect and SCIM deactivation
  clear the credential.
- Every new table is classified in `server/erasure/store/census.py`.
- `erasure.request` and `erasure.export` refuse an `acl-` scope in every auth mode:
  their handlers check it before the auth-on checks, since with auth off those do not
  run. `erasure.export` of a `user_id` gains a connections section (kind, store account
  email, scopes, connection times; never a credential).
- Audit rows: `add`, `update`, `rotate`, `remove`, `sync_now` as today, plus `update`'s
  item changes; `connect` and `disconnect` (subject, kind, scopes, never a credential);
  a crawl's subjects, granted scopes and member count; a SCIM-driven stop names the
  sources it stopped.

## Rolling upgrades

- **N-1 cannot operate on a source it must not.** N-1 reads `kind`, unseals
  `credential_enc` and reads `config_json`; it knows none of the new columns, and its
  writes of a row (`rotate`, `remove`, `code.repo.remove`'s retirement, the clearing of
  `pending_delete`) reset them. A source stored with `kind` empty is one N-1 cannot use.
  Off a trunk namespace N-1 refuses its `check` and `rotate` with `NOT_A_CODE_NAMESPACE`
  before unsealing its credential; on a trunk its `check` unseals the empty credential
  and answers `credential_invalid`, its `rotate` finds no probe for the kind and fails
  with a server error before writing (as for an unknown kind, rule 28), and its sync
  turn reads the empty credential and ends `credential_invalid` before any request,
  retiring nothing. N-1 also syncs only sources on a trunk namespace (its `_usable`). On
  a trunk namespace, an N-1 turn over such a source writes `status: credential_invalid`
  into its sync state and counts as a failed turn, as rule 28 says for a new kind, until
  N's next turn writes the real status. N-1's `remove` of such a source leaves an
  inactive row with its new columns reset: for a mirrored source the anti-join opens its
  cleanup, and a `namespace` source's documents go through `pending_delete`, as today.
- **The new modes wait for N everywhere.** `add` and `update` answer 503 `OVERLOADED`
  (`retry_after_s`) for a source stored with `kind` empty while any live process of any
  deployment on the ClickHouse service (`loom.instances`,
  `read_live(all_deployments=True)`, as rule 25 counts) reports a schema version below
  the migration that adds the columns, or no heartbeat has been read, as rule 24 does
  for namespace guides ([rolling upgrades](rolling-upgrades.md)).
- **Rollback.** N-1 reads no mirror, but its gate admits a platform or org admin's
  wildcard token that names one, and nothing on N-1 ages the lists. So the rollback
  procedure ([upgrades](../operations/upgrades.md)) requires, before any N-1 replica
  starts, stopping every mirrored source and erasing the mirrors (`loom knowledge mirror
erase --all`, a server CLI command that reads `loom.knowledge_mirrors` and runs a
  platform-admin erasure of each mirror); the implementing pull request adds the step.
- **Ownership.** One owner per address, found by its tag, is today's model, so N-1 and N
  agree on whose a document is. Both write a retired address again at their next listing
  of the file.
- **State.** Sync state stays in `loom.code_index_state`; N-1 ignores the per-account
  cursors N keeps in it and the listings position row (its own turns merge into the
  state and leave them).
- **`meet`.** N-1 still syncs a stored `meet` source on its turns; N skips it.
- **Erasure.** N-1's erasure misses the mirror and the new tables (rule 14): run again
  with N every erasure completed while an N-1 replica served.

Each implementing pull request adds its rule to [rolling upgrades](rolling-upgrades.md),
and pins this section's N-1 behaviour with a test that runs today's `check`, `rotate`,
`remove` and sync turn over a row N wrote with `kind` empty.

## Breaks

- The source ops no longer answer `NOT_A_CODE_NAMESPACE`; `add` answers
  `NAMESPACE_INACTIVE` for an inactive namespace.
- `recording` sources lose the `meet` provider.
- A source whose stored documents plus new files pass
  `LOOM_KNOWLEDGE_SOURCE_MAX_DOCUMENTS` (default 50,000) writes no new document and
  reports `too_many_documents`, where today it syncs on with a truncated comparison.
- The gate of `add`, `update`, `check`, `sync_now` and `remove` takes `writer`, with
  `admin` checked by the handler for shared and crawl sources; `rotate` is refused for a
  connected source.

## Settings

| Setting                                              | Default | Meaning                                                                              |
| ---------------------------------------------------- | ------- | ------------------------------------------------------------------------------------ |
| `LOOM_KNOWLEDGE_SOURCE_ACL_STALE_SECONDS`            | 3600    | a source's lists, group members row or account listing older than this admits nobody |
| `LOOM_KNOWLEDGE_SOURCE_MAX_DOCUMENTS`                | 50000   | a source's stored documents plus new files; past it a turn holds back new documents  |
| `LOOM_KNOWLEDGE_CONNECTED_SOURCES_MAX_PER_PERSON`    | 8       | a person's connected sources in one namespace                                        |
| `LOOM_KNOWLEDGE_CONNECTED_SOURCES_MAX_PER_NAMESPACE` | 64      | a namespace's connected sources                                                      |
| `LOOM_CONNECT_GDRIVE_CLIENT_ID`, `_SECRET`           | unset   | the shared-folder store's OAuth client; unset, those connections are off             |
| `LOOM_CONNECT_NOTION_CLIENT_ID`, `_SECRET`           | unset   | the page store's public integration                                                  |
| `LOOM_CONNECT_LINEAR_CLIENT_ID`, `_SECRET`           | unset   | the project tracker's OAuth application                                              |

The redirect URI to register with each store is
`<LOOM_PUBLIC_BASE_URL>/v1/connect/<kind>/callback`.

## Rejected

- **Mirrored documents in the namespace itself, filtered by every read.** A missed path
  would show the document to every reader. The list sits on the document only inside the
  mirror, which no token opens.
- **A namespace per permission list.** A list changes with every share, which would move
  the document, its chunks and embeddings on each change.
- **A mirror name derived from the namespace.** Anyone could compute it, and an N-1 gate
  admits a wildcard admin token to a computed name.
- **A permission list per reading account.** An account that read the list before a
  reader was removed would keep admitting them until its row aged out.
- **Resolving the store's groups through Loom's SCIM groups.** They hold a service's end
  users (`user:<id>`), not people's emails.
- **A separate meeting-store path.** The meeting store already saves transcripts and
  notes to the shared-folder store, with the participants' permissions.
- **`drive.file` with the picker.** It cannot read a folder's later files.
- **Incremental listings for mirrored sources.** A sharing change does not move a file's
  modification time.

## Invariants

- A mirrored document is read only through a leg that applies the permission check; no
  token reads or exports a mirror, and only a platform admin's `erasure.request` names
  one.
- Every `crawl` and `connected` source is mirrored.
- A stale, unexpanded or link-only permission, and a permission type the sync does not
  know, admit nobody; a file with no readable list admits only through the readers rows
  of the accounts that listed it (rule 3).
- No caller-supplied host or query string reaches a store.
- A connected source stops when its owner disconnects, is deactivated, or loses `writer`
  on the namespace, and its mirrored documents go with it.
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

- Unit: the link parser on every link form and every unreadable one; item validation;
  the query builder escapes every field; the access check on fresh, stale, group,
  domain, link-only, anyone, readers and unknown permissions, on two accounts' readers
  rows for one address, and on a reader removed by the newest list while an older
  account's listing is younger; the `acl-` gate in `check_authorized` and `CachedGate`,
  with auth on and off, and for a platform admin on a read op; the mixed-version
  refusal; the erasure's retire step running again on resume and its census reopening
  the steps for a source added mid-saga; the deleted readers rows landing before a
  listing row's new start, with the sync killed between them; a stopped crawl leaving no
  listing or group-members row, also when an N-1 `add` reuses its name for a `namespace`
  source before the cleanup pass; a source N-1 removed, with and without a re-add under
  its name, cleaned by the anti-join; a cleaned source opened by no later anti-join; an
  anti-join running while N's own cleanup of the same source is open, which opens no
  second one; an N-1 `remove(delete_documents=true)` of a `namespace` source retiring
  its documents through `pending_delete`; a turn in flight when its source is removed,
  and a turn of a pass that read it active before the remove, leaving no readers,
  listing or group-members row after the cleanup finishes; a cleanup skipped on a busy
  lease and run on a later pass; a second source's turn counting a file another source's
  document holds `skipped`, neither fetching nor rewriting it; that file written by the
  second source after the owner's document is retired, at its next complete listing when
  it is mirrored and its next full listing when it is a `namespace` source; a kept
  document skipped by every source; a stopped mirrored crawl whose name a `namespace`
  source reused before its cleanup ran, whose cleanup retires only the mirror's
  documents of its own id; a mirrored source stopped before its first listing completed,
  found by the anti-join through its documents' `source-id` tag; a changed list
  rewriting the document's row with its content and chunks unchanged, and the next turn
  fetching and embedding nothing; an unchanged list writing nothing; a list read
  refused, and a file the source stopped listing whose retirement fails, marking it
  `unread`, after which a readers row from a listing older than the mark admits nobody;
  a retirement cut by the pass bound leaving the older listing start; under the Postgres
  authority, on every leg with the check, a narrowed list and a retirement each
  committed but not yet published, or held by a parked delivery, already applying, a
  mirror address with no head dropped, and a head read that fails showing no mirrored
  document; a rule-2 check that admits nobody through the list when one of the source's
  accounts has a stale listing row and the reader's own is fresh; a document admitted
  only through `groups`, `domains` or `anyone` passing the admitted-set read; a head
  moved to `unread` and not yet published admitting nobody through a readers row older
  than its `changed_at`, and a connected `notion` page, admitted by rule 3 alone, still
  shown; a source over `LOOM_KNOWLEDGE_SOURCE_MAX_DOCUMENTS` after an item swap writing
  no new document, rewriting a narrowed list, landing its listing row, retiring, and
  draining back under it; a file only one account lists whose permissions read is
  refused marked `unread` before that account's listing row lands, its old list
  admitting nobody; a chunk with `revision` 0 matching by version, with no refetch after
  the upgrade; a crawl where one account drops a file another still lists, neither
  retiring nor marking it, and the dropping account's row not held back; a file whose
  first listing account's permissions read is refused and whose next account's succeeds,
  not marked `unread`; an `unread` list refused again writing nothing and moving no
  `changed_at`; a cleanup killed mid-retirement, whose rerun finds the documents still
  tagged and retires them; an anti-join over a mirrored source N-1 removed, whose lease
  key and re-check use the source's namespace; no readers, listing or group-members row
  left by a turn that starts after a cleanup; a `remove` without `delete_documents` on a
  `namespace` source keeping its documents, also when a same-named mirrored source's
  cleanup runs; a connected source N-1 removed during its first listing leaving no
  readers row after the anti-join's cleanup; a new document written with the list its
  fetch read; no read op returning `access_json`; a mirrored `add` over a `namespace`
  source of the same name removed with `delete_documents`, refused with
  `SOURCE_DELETE_PENDING`, and over its kept documents with `SOURCE_DOCUMENTS_KEPT`; a
  mirrored `add` reusing the name of a source N stopped while its cleanup is open, the
  two sharing the turn lease, the cleanup retiring only documents of the old id and the
  new source writing each freed address at its next complete listing; a mirrored `add`
  reusing the name of a stopped mirrored source whose documents are still in the mirror,
  before the anti-join runs, owning none of them and counting them `skipped`; a source
  whose registry row a lagging replica does not show yet left alone; a person dropped
  from a crawl's expansion leaving no readers or listing row; a directory read that
  fails mid-group, or returns over the cap, dropping nobody; a user erasure leaving no
  row, of any version, keyed by the person's pseudonyms; `erasure.request` refusing an
  `acl-` scope for an org admin and a namespace admin and admitting a platform admin; a
  user erasure leaving no address of the person in `loom.knowledge_sources`, nor a crawl
  readers or listing row naming them; `erasure.request` and `erasure.export` refusing an
  `acl-` scope with auth off; the mirror cache after an erasure followed by a new
  mirrored `add`; a connected `notion` page shown to its owner only; a consent callback
  without the starting browser's cookie, or in another subject's session, storing
  nothing; an admin's `update` adding items to someone else's connected source refused;
  scope refusals on the connect callback and a crawl's probe; today's `check`, `rotate`,
  `remove` and turn over a row N wrote with `kind` empty.
- Integration: `tests/integration/test_document_source_leakage.py`, beside
  `test_cross_principal_leakage.py`, seeds mirrored documents with canary text and reads
  every knowledge read op, `knowledge.reads`, `knowledge.query` and
  `knowledge.source.inspect` as a namespace viewer the list does not name, as one whose
  permission was removed, as a group member after removal, after the stale bound, as a
  service account, through a link-only share, as a crawl's registrant, as another
  namespace and as another org; any canary fails.
- Integration: a `connected` source is mirrored wherever it writes; a SCIM deactivation
  and a disconnect stop the person's sources and retire their mirrored documents.
- Integration: two sources reaching one address, one owning it and the other counting it
  `skipped` until the owner's document is retired; removing an item retires only what no
  other item reaches; `namespace.delete` and `collection.delete` retire the namespace's
  sources and clear their credentials.

## Related

- [Knowledge: document sources](../features/knowledge.md#document-sources)
- [Document extraction](document-extraction.md)
- [Security](security.md)
- [Sharing knowledge](sharing.md)
- [Identities and namespaces](../concepts/identities-and-namespaces.md)
- [Rolling upgrades](rolling-upgrades.md)
