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
person's own place (a `col-me-…` collection, a `personal-…` or `u-…` namespace, a
namespace claimed at sign-up) holds no source (`PERSONAL_NAMESPACE`): a `user_id`
erasure erases what the person wrote, and a source's documents are the sync worker's.
The sync skips a source whose namespace is inactive.

A source ends with its namespace: `code.repo.remove`, `namespace.delete`,
`collection.delete` and a namespace erasure each retire the namespace's sources under
its source lease, clearing their credentials, as `code.repo.remove` does today, so no
source outlives its namespace with a sealed credential or writes back what an erasure
took.

Sync state stays where [storage layout](storage-layout.md#cursors) keeps a source's sync
state (`loom.code_index_state` today, Postgres `cursors` from wave 3), keyed by the
source's namespace, which the namespace's erasure already covers: by name for a
`namespace` source, as today, and by source id for a mirrored one, so a later source of
the same name resumes none of an earlier one's listing.

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
retires, and lands its listing row, so its copies drain back under the bound and no list
goes stale; `inspect` and `loom doctor` show `too_many_documents` with the count
meanwhile. The read of a source's stored copies goes in pages by `source_uri`, so it
never cuts one off. A listing asks the store about at most 50 parent folders per query
(not a setting), so a source of many folders lists in several queries; a recursive
folder's subfolders are walked on every full listing, and a file in a new subfolder
arrives with the next one. A probe checks a bounded sample: every item while there are
at most 20, otherwise the 20 items added last, and the answer says `sampled: true` with
the number checked. Only a refused item of the sample refuses `add` or `update`; the
next sync turn reports any other as `item <n>: http_404` and goes on with the rest.

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
`owner` email and a crawl's `subjects` as keyed hashes (below). Removing every item is
refused (`BAD_CONFIG`); `remove` ends a source. Changing `access` is a new source. For a
`crawl`, `update` also takes `subjects` (the new list, probed like added items) and
`directory_subject`. Every `update` and `rotate` of a crawl needs the same roles as
`add`, since its credential reads every listed person's drive: a namespace admin without
an org-wide role cannot change whose drives it reads, what it reads of them (items,
`include`) or the key it reads with.

### One document, one owner

One document per address per namespace, as today, owned by one source: the source whose
`source:<name>` tag it carries, written by the sync worker (today's ownership check).
Only the owner rewrites the document when its file changes and retires it on positive
evidence (today's retirement rule), and a document another writer (a person, an agent)
wrote is never overwritten or retired by any source, as today. A mirrored document also
carries `source-id:<id>`, the id of the source that wrote it, and in a mirror that tag,
not the name, decides ownership: a later source of the same name owns none of an earlier
one's documents, and the cleanup of a stopped mirrored source retires by it. Only N
writes a mirror, so every mirror document carries the tag. How a stopped source's
documents and rows are found and cleaned when several sources, builds or names meet is
the implementing pull request's to specify.

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

So a file that two sources reach is written once and read through one document, with the
owner's list: in a mirror, a file whose list neither source can read admits only the
people the owner's listing found, so the other source's reader does not see it while the
first owns it, which fails closed. Between the owner's retirement and the other source's
next listing of the file, the document is missing; those gaps are the cost of keeping
one owner per address instead of recording every source that reaches it.

`remove` with `delete_documents` retires the source's documents, as today, and without
it they stay as kept documents, which `add` of the same name refuses to take over
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
with the owner's account; its owner or an admin of the namespace may `check`,
`sync_now`, `remove` it and remove items. `rotate` stays `admin` (for a crawl, also an
org-wide role) and is refused for a `connected` source, whose credential is its owner's
connection. Owners are compared as canonical subjects (`server/auth/aliases.py`
`canonical_subject`), so a person's other spellings are the same owner.

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
  source's namespace is stopped (written inactive under the source lease, `status:
owner_inactive`) and goes as a removed source does, so a missed hook fails closed.

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
anyone connecting. Every crawl's expansion skips the people an erasure excluded
([erasure](#erasure-and-audit)).

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
checks the list and fails closed; and every sync turn reads the lists again. Everything
else in this section follows from those four.

A deployment with auth off (`LOOM_AUTH_REQUIRED=false`) has no reader identity to check.
It refuses to add a mirrored source (`AUTH_REQUIRED`), and when auth is switched off
after mirrors exist, the gate still refuses every mirror name, the mirror legs answer
nothing, and the sync skips mirrored sources.

### The mirror

A mirrored document is not stored in the source's namespace but in that namespace's
mirror: a namespace named `acl-<24 random hex>`, made under the namespace's source lease
with its first mirrored source and recorded in `loom.knowledge_mirrors` (org, namespace,
mirror). The gate admits no token to a name of that form (`acl-[0-9a-f]{24}`):
`check_authorized` and `CachedGate` refuse the name before any other rule, the auth-off
return included, and `namespace.register` and `restore` refuse the form.
`erasure.request` refuses an `acl-` name in its scope for every caller but a platform
admin with auth on, and `erasure.export` for every caller; with auth off both refuse it
for everyone. So no token searches, reads or exports a mirror, and the threat model's
rows hold for every token. A mirror has no directory row and appears in no namespace
listing or usage view; a namespace name keeps one mirror for life, so a name deleted and
registered again inherits it, and a name whose mirror still holds rows counts as in use
(`namespace_census.in_use`).

A knowledge read taught the mirror adds a leg over the namespace's mirror, with the
check below. Every other read path reads the namespace and finds nothing of the mirror.
So a new op, a context-pack leg or an export that nobody taught the check shows no
mirrored document, and neither does an N-1 replica. A permission column on
`loom.documents` read by every path would make the opposite the default;
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
  read by `knowledge.query` without the check.
- **Reads recorded under the mirror:** a mirror leg's `agent_events` row is filed under
  the mirror's namespace, so `knowledge.reads` on the namespace names no mirrored
  address, and `sql_v1_agent_events`, `sql_v1_usage_daily` (replaced by a migration),
  the analytics export's usage entity and the insights agents and cost reads leave
  `acl-` names out; a mirror's spend is charged to its namespace, under that namespace's
  `daily_cost_cap_usd`.
- **No text in a trace:** a generation whose context holds a mirrored chunk captures no
  prompt or completion text, whatever the namespace's `capture_generation_io` says.
- **No id reaches a mirror:** the id-addressed checks (`row_reach`, `row_in_scope`,
  `id_namespace.namespace_for_id`) address memory rows, which no mirror holds, so their
  auth-off shortcut opens nothing of one.

A mirrored source's documents are never kept past it: `remove` retires every one it owns
(`delete_documents` is implied), and so do a disconnect, a SCIM deactivation, the owner
check and every retirement of a namespace's sources. In a mirror, ownership and every
per-source document read go by the `source-id:<id>` tag, not by name. The retention
overrides give each mirror its namespace's `retention_days` and `warm_days`.

A namespace source and a mirrored source that reach the same file own two documents, one
in the namespace and one in its mirror. The namespace copy is readable by every reader,
because an admin registered a source that makes it so; search answers the address once.

### The permission list

Each mirrored document carries its permission list in an `access_json` column of its row
(`loom.documents`; under the Postgres authority it rides in the head's `row` jsonb, with
no Postgres migration), written by the turn of the source that owns the document:

- `emails`: the people the store grants any role, lower-cased, and, for a file whose
  list the source cannot read (a view-only file, a page store that shows an integration
  no permissions), the people whose own account listed the file in that turn: the owner
  of a connected source, named by the owner subject's verified directory emails at that
  turn (never the connection's store email), and the person a crawl listed it as. A
  `shared` mirrored source's service account is no reader, so its unreadable lists are
  `unread`.
- `groups`: group emails, which a crawl's directory read expands into
  `loom.knowledge_group_members` (one row per source and group, with its members and
  `read_at`). A connected source reads no directory and expands no group.
- `domains` and `anyone`: a domain or public permission the store marks discoverable. A
  link-only share (one the store marks not discoverable) is left out: it lets someone
  who holds the link open the file, not find it.
- `unread`: no current list is known.

For `gdrive` the list is the file's `permissions`, read with the listing, or with
`permissions.list` when the listing leaves it out; for `linear`, the members of every
team the project belongs to. The list lives and dies with its document: a retirement's
tombstone carries no list, and no read op returns the column.

### Who may read

A reader sees a mirrored document when all of these hold:

1. The reader holds `viewer` on the source's namespace (the ordinary gate).
2. The document's owning source (by its `source-id` tag) has a last complete listing
   that started within `LOOM_KNOWLEDGE_SOURCE_ACL_STALE_SECONDS` (default 3600) of now,
   read from its row in `loom.knowledge_source_listings`, and the list is not `unread`.
3. One of the reader's verified emails is in the list's `emails`, or in the members of
   one of its `groups` from an equally fresh members row of the document's owning source
   (by its `source-id` tag), or that email's domain is in its `domains`, or its `anyone`
   is set.

The reader's verified emails are its subject's directory emails that
`VERIFIED_EMAIL_SQL` admits (an SSO account's or a linked one's,
`server/core/grant_sql.py`), and nothing else: a subject that merely spells an address
is not one, and neither is a connection's store email. A domain matches only through
them. A delegated token (an agent's, the assistant's: one carrying `AuthInfo.creator`)
reads with its creator's emails. Every other token, a service account that inherits its
creator's grants, a bot and an end-user token included, has none, so it sees only
`anyone` documents.

The check fails closed. A stale listing, an `unread` list, an unexpanded or stale group,
a link-only permission and a permission type the sync does not know admit nobody. A leg
that cannot read the lists, the members rows or the listing row shows no mirrored
document and marks the answer `degraded: mirror_unread`. Each leg reads the listing row
first and then the members rows and the lists, under `sequential_reads()`, so a lagging
replica cannot pair a fresh listing row with an older list. It computes the addresses
the reader may see before its `LIMIT`, so a page is never cut short by the check, and
caches no admitted set. Under the Postgres authority each leg judges the addresses it is
about to answer by their heads, so a narrowing or a retirement committed and not yet
published already applies, and a missing head drops the address; a leg that cannot read
the heads shows no mirrored document. The implementing pull request measures the leg on
a mirror of a million documents under both storage authorities and puts the figures
here.

### Every turn reads the lists again

A mirrored source lists every file on every turn, metadata only (id, revision,
permissions): the store does not change a file's modification time when its sharing
changes, so an incremental listing would miss a permission removed. A listing that reads
a list unlike the stored one rewrites the document's row with the same content and the
new list, as `knowledge.update_metadata` without `reindex` does, so no chunk or
embedding is rebuilt; an unchanged list costs no write. A stored document the complete
listing no longer has is rewritten `unread`, then retired on positive evidence, as
today. The listing row's start lands only once every changed list and every such
`unread` rewrite of that listing is written, so a row is never newer than the lists it
vouches for. These rewrites are outside the pass's document cap, bounded only by the
listings' share of the deadline. Content is fetched only for a file whose revision
changed, and a content write carries the list of the latest listing that read the file,
so a fetch the pass bound deferred never writes an older list; a listing row lands only
once every document written during that listing carries a list it read.

A pass runs the listings first, rotating across the mirrored sources, and gives them at
most a third of the pass deadline (`knowledge_source_sync_max_pass_seconds`); a listing
that does not finish goes on from its cursor in the next pass, and until it completes
the source's listing row keeps its older start, so a large source needs a stale bound
longer than the time to list it all. `inspect` and `loom doctor` report each source's
oldest listing start. A stopped sync hides every mirrored document once the stale bound
passes. The implementing pull request measures the writes of a crawl over 2000 people
and puts the figures here.

How lists from several accounts of one source, a person a crawl stops reading, a stopped
source's rows, and restores of either store are handled is the implementing pull
request's to specify against these four parts; each must leave no list or row that
admits someone the store no longer names.

### Threat model

| Threat                                                                   | Boundary                                                                                                                                                                                                       | Enforced by                                                            |
| ------------------------------------------------------------------------ | -------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- | ---------------------------------------------------------------------- |
| A connected person leaks a private file into a team namespace            | Every connected source is mirrored; its documents live in the mirror, and a teammate sees one only when the store names them. `access` cannot change.                                                          | `add` and `update`; the mirror gate; `test_document_source_leakage.py` |
| A crawl's registrant reads crawled people's private files                | No rule admits a source's registrant or owner as such; a registrant reads a file only when the store names them or their own account lists it.                                                                 | the access check                                                       |
| An admin points someone's connected account at files they did not choose | Only a connected source's owner adds its items or changes `include`.                                                                                                                                           | `update`                                                               |
| A link-only share becomes a search hit for everyone                      | A permission the store marks not discoverable admits nobody.                                                                                                                                                   | the access check                                                       |
| A person stops reading a file and Loom keeps showing it (stale ACL)      | Every turn of a mirrored source reads every file's list and rewrites a changed one; a source whose last complete listing is older than the stale bound admits nobody.                                          | the sync's mirrored listing; the access check                          |
| A person leaves a group and keeps reading through it (group churn)       | Groups are read on every turn into a members row with its `read_at`; a stale or unread group admits nobody.                                                                                                    | the access check                                                       |
| Auth is switched off after mirrors exist                                 | The `acl-` refusal runs before the auth-off return; mirror legs answer nothing and the sync skips mirrored sources while auth is off.                                                                          | `check_authorized`; the mirror legs                                    |
| A person leaves the org and their connected sources keep syncing         | SCIM deactivation and disconnect stop the sources and clear the credential, then revoke it at the store; each turn's owner check skips a source whose owner is revoked, deactivated or no longer a writer.     | `server/auth/scim.py`; the sync's turn                                 |
| A crawl reads more than its registrant meant, or can write               | An org-wide role to register, update or rotate; the probe requires the store to refuse a write scope; subjects and scopes in the audit row.                                                                    | the crawl probe; `loom.audit_log`                                      |
| An id-addressed read confirms a mirrored document exists                 | `get`, `outline`, `download` and the inspects answer `found=false` or leave out an address the reader may not see.                                                                                             | the mirror legs                                                        |
| A new read path shows mirrored documents to everyone                     | Mirrored documents live in a namespace no grant opens and no listing names; a path that does not read the mirror shows none.                                                                                   | `check_authorized` (`acl-[0-9a-f]{24}`)                                |
| An older build shows mirrored documents                                  | N-1 reads no mirror; its gate does not refuse a mirror's name, so no N-1 serves while one holds rows: the new modes wait for N everywhere, and a rollback first erases the mirrors with every replica stopped. | the random name; the rollback procedure                                |
| A mirrored document's text reaches every reader through a derived row    | Decision mining and dataset extraction skip mirrored documents; a generation over a mirrored chunk captures no text.                                                                                           | the sync; generation capture                                           |
| A store account is linked into another person's Loom account, either way | The connection is stored only from the browser that started it, with that subject's own dashboard session and the `state` cookie. A connection's email is never a reader identity.                             | the connect routes                                                     |
| A caller injects store query syntax through a filter                     | Filters are fields with patterns, escaped and built by the server; no raw query is taken.                                                                                                                      | `server/clients/gdrive.py`                                             |
| A stolen connected credential changes the store                          | Only read scopes are accepted; the credential is sealed and never returned.                                                                                                                                    | the connect callback; `server/core/secrets_at_rest.py`                 |

Out of scope: a reader who already saw a document and kept its text, their own agent's
or conversation's record of an answer included, which is that reader's write; the store
itself reporting a wrong permission list.

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
  mirrored source shows its kind, its owner as "a connected account" for a connected
  one, its item count and its last sync's status, not its items, `config`, `created_by`,
  its document counts or the error text, which can name an item. Python and TypeScript
  SDKs are regenerated.
- **CLI.** `loom knowledge source add|update|list --namespace <ns> --link <url>...`
  (`--credential-file`, `--connected`), `loom connect gdrive|notion|linear`, `loom
connection list|disconnect`, `loom knowledge mirror erase --all` ([restores and
  rollback](#rolling-upgrades)), and `loom doctor`'s line for the oldest listing start
  of each mirrored source.
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

| Table                              | Key                                  | Holds                                                                                                                                                                                                    |
| ---------------------------------- | ------------------------------------ | -------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `loom.knowledge_sources` (columns) | `(org, namespace, name)`             | `source_kind`, `credential_mode` (default `shared`), `access` (default `namespace`), `owner_subject`, `items_json`, `options_json` (a crawl's `directory_subject` and `subjects`), `secret_enc`          |
| `loom.knowledge_mirrors`           | `(org, namespace)`                   | `mirror`, `created_at`                                                                                                                                                                                   |
| `loom.documents` (column)          | as today                             | `access_json`: a mirrored document's permission list (`emails`, `groups`, `domains`, `anyone`, `unread`); empty elsewhere; under the Postgres authority it rides in the head's `row` jsonb, no migration |
| `loom.knowledge_source_listings`   | `(org, namespace, source_id)`        | `name`, `started_at` (the start of the source's last complete listing), `version`; `namespace` is the mirror                                                                                             |
| `loom.knowledge_group_members`     | `(org, namespace, source_id, group)` | `name`, `members`, `read_at`, `deleted`, `version`; `namespace` is the mirror                                                                                                                            |
| `loom.connected_accounts`          | `(org, subject, kind)`               | `account_id`, `account_email`, `scopes`, `credential_enc`, `active`, `connected_at`, `disconnected_at`, `version`                                                                                        |

A source N-1 can operate on (`shared`, `access: namespace`, with items today's config
can express) keeps today's columns as they are: `kind`, its credential in
`credential_enc` and its items in `config_json`, which N writes alongside `items_json`.
Every other source (mirrored, `connected`, `crawl`, or with an item today's config
cannot express) is stored with `kind` empty and its kind in `source_kind`, its
credential in `secret_enc` (purpose `knowledge_sources.secret`, which `loom secrets
reseal` covers) and its items in `items_json`, with `credential_enc` empty and
`config_json` `{}`. N reads `source_kind` first and `kind` when it is empty, and
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
  back what the erasure took. A namespace erasure covers the namespace's mirror as one
  more name of the same erasure, through its resume, replay and re-verify. A mirror's
  rows are deleted in either mode: no token reads or restores an `acl-` name.
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
- **Rollback and restores.** N-1 reads no mirror, but its gate admits a wildcard admin
  token that names one, and nothing on N-1 ages the lists. So the rollback procedure
  ([upgrades](../operations/upgrades.md)) stops every replica, stops every mirrored
  source and erases every mirror (`loom knowledge mirror erase --all`) before any N-1
  replica starts. A restore of either store runs the same command before the deployment
  starts, since a restored copy cannot be trusted to say who may read; owners connect
  and add their sources again. The implementing pull request adds both steps and
  specifies the command.
- **Ownership.** One owner per address, found by its tag, is today's model, so N-1 and N
  agree on whose a document is.
- **`meet`.** N-1 still syncs a stored `meet` source on its turns; N skips it.
- **Erasure.** N-1's erasure misses the mirror and the new tables (rule 14): run again
  with N every erasure completed while an N-1 replica served.

Each implementing pull request adds its rule to [rolling upgrades](rolling-upgrades.md),
and pins this section's N-1 behaviour with a test that runs today's `check`, `rotate`,
`remove` and sync turn over a row N wrote with `kind` empty.

## Breaks

- The source ops no longer answer `NOT_A_CODE_NAMESPACE`; `add` answers
  `NAMESPACE_INACTIVE` for an inactive namespace and `PERSONAL_NAMESPACE` for a person's
  own place.
- `recording` sources lose the `meet` provider.
- A source whose stored documents plus new files pass
  `LOOM_KNOWLEDGE_SOURCE_MAX_DOCUMENTS` (default 50,000) writes no new document and
  reports `too_many_documents`, where today it syncs on with a truncated comparison.
- The gate of `add`, `update`, `check`, `sync_now` and `remove` takes `writer`, with
  `admin` checked by the handler for shared and crawl sources; `rotate` is refused for a
  connected source.

## Settings

| Setting                                              | Default | Meaning                                                                             |
| ---------------------------------------------------- | ------- | ----------------------------------------------------------------------------------- |
| `LOOM_KNOWLEDGE_SOURCE_ACL_STALE_SECONDS`            | 3600    | a source's listing or group members row older than this admits nobody               |
| `LOOM_KNOWLEDGE_SOURCE_MAX_DOCUMENTS`                | 50000   | a source's stored documents plus new files; past it a turn holds back new documents |
| `LOOM_KNOWLEDGE_CONNECTED_SOURCES_MAX_PER_PERSON`    | 8       | a person's connected sources in one namespace                                       |
| `LOOM_KNOWLEDGE_CONNECTED_SOURCES_MAX_PER_NAMESPACE` | 64      | a namespace's connected sources                                                     |
| `LOOM_CONNECT_GDRIVE_CLIENT_ID`, `_SECRET`           | unset   | the shared-folder store's OAuth client; unset, those connections are off            |
| `LOOM_CONNECT_NOTION_CLIENT_ID`, `_SECRET`           | unset   | the page store's public integration                                                 |
| `LOOM_CONNECT_LINEAR_CLIENT_ID`, `_SECRET`           | unset   | the project tracker's OAuth application                                             |

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
  token reads or exports a mirror, and `erasure.request` takes a mirror's name only from
  a platform admin, with auth on.
- Every `crawl` and `connected` source is mirrored.
- A stale, unexpanded or link-only permission, an `unread` list and a permission type
  the sync does not know admit nobody.
- Every turn of a mirrored source reads every file's list again.
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

Each implementing pull request lists its own; these pin the model:

- Unit:
  - The link parser on every link form and every unreadable one; item validation; the
    query builder escaping every field.
  - The access check on fresh and stale listings, `unread`, group, stale group, domain,
    link-only, anyone and unknown permissions, and for a delegated token, a bot and an
    end-user token.
  - The `acl-` gate in `check_authorized` and `CachedGate`, with auth on and off, and
    for a platform admin on a read op; `namespace.register` and `restore` refusing the
    form; `erasure.request` and `erasure.export` refusing an `acl-` scope with auth on
    and off.
  - A document missing from a complete listing admitting nobody once its listing row
    lands; an email-shaped subject with no verified directory email, and an inheriting
    service account, seeing no `emails` document; a group matched only through the
    owning source's members row.
  - A leg on a lagging replica that sees a fresh listing row and an older list admitting
    nobody; a fetch the pass bound deferred writing no list older than the listing row
    that lands after it; a connected source's unreadable-list entry admitting the owner
    through their directory email, never the connection's store email; a generation over
    a mirrored chunk capturing no text; the insights agents and cost reads leaving
    `acl-` names out.
  - `row_reach`, `row_in_scope` and `namespace_for_id` opening nothing of a mirror with
    auth off; a mirrored source's document counts hidden from a viewer who is neither
    its owner nor an admin.
  - A turn that finds a narrowed list rewriting the row without rebuilding a chunk, and
    one that finds the same list writing nothing; a listing row landing only after its
    changed lists.
  - A leg that cannot read the lists, the members rows, the listing row or (under the
    Postgres authority) the heads showing no mirrored document, marked `degraded:
mirror_unread`.
  - The mixed-version refusal, and N-1's `check`, `rotate`, `remove` and sync turn over
    a row N wrote with `kind` empty.
- Integration: `tests/integration/test_document_source_leakage.py`, beside
  `test_cross_principal_leakage.py`: for every leg with the check and every id-addressed
  op, a reader the list leaves out sees no mirrored document, its title, its count or
  its existence, under both storage authorities; a reader the list names sees it; a
  reader removed from the store's list stops seeing it after the next listing.
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
