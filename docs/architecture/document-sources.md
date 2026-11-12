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
| Two sources that reach one file: the second is `skipped`.                                              | The address is held by every source that reaches it, and retired when none does ([holds](#one-document-many-holders)).                                                                                          |
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

A source holds at most 300 items (a wire contract, not a setting). A listing asks the
store about at most 50 parent folders per query (not a setting), so a source of many
folders lists in several queries; a recursive folder's subfolders are walked on every
full listing, and a file in a new subfolder arrives with the next one. A probe checks a
bounded sample: every item while there are at most 20, otherwise the 20 items added
last, and the answer says `sampled: true` with the number checked. Only a refused item
of the sample refuses `add` or `update`; the next sync turn reports any other as `item
<n>: http_404` and goes on with the rest.

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

### One document, many holders

One document per address per namespace, as today. `loom.knowledge_source_holds` records,
per address of the namespace the document lives in, each source that holds it: the sync
writes a hold when one of the source's items lists the file and drops it on positive
evidence (today's retirement rule) that none of its items reaches the file. A document
is retired when its last hold is dropped this way. A source that lists a file another
source already wrote at the listed revision writes its hold and fetches nothing; it then
reads the document again, and when another source's cleanup retired it in between,
fetches and writes it as a changed file. A retirement that lands after that re-read is
caught by the holder's next turn, mirrored or not: every turn first reads the source's
live holds against the stored copies (one read of the holds and of the live documents at
those addresses, by address and not by the source's tag, under `sequential_reads()`) and
puts a held address with no stored copy at the front of its content queue, so a held
address stays retired until that source's next content phase. Every write of a mirrored
address with no stored copy, on either path, also writes the readers rows of the
connected or crawl accounts that listed it, and its access row when the list was read. A
document's `source:<name>` tags are its holders' names: whenever a turn finds a held
address whose document's `source:*` tags differ from its holders (a set of holders that
changed, or a rewrite a crash cut short), the sync rewrites the tags with
`knowledge.update_metadata`, which fetches nothing. A document another writer (a person,
an agent) holds is never overwritten or retired by any source, as today. A retirement
reads the address's holds, access row and readers rows under `sequential_reads()`, as
today's retirement reads its writer check, and each source writes its dropped hold
before that read, so of two sources dropping their holds at once at least one sees no
hold left; it retires only when no hold is left, then writes `deleted` the address's one
access row and every readers row it read. Each `deleted` row carries the `version` of
the row it replaces, and a readers row is written at a `version` of its write time, so a
readers row that lands later is strictly newer and wins. An access row's `version` is
its list's read time, so one written from a read older than the row the retirement
replaced loses to the `deleted` row and waits for the next read of the list; the heal
therefore writes the access row only from a list read in that same turn.

`remove` drops the source's holds. With `delete_documents`, a document left with no hold
is retired; without it, such a document stays as a kept document, as today
(`SOURCE_DOCUMENTS_KEPT`). A mirrored source's documents are always retired
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

A mirrored source's documents are never kept past it: `remove` retires every one no
other live source holds (`delete_documents` is implied), and so do a disconnect, a SCIM
deactivation, the owner check and every retirement of a namespace's sources
(`namespace.delete`, `collection.delete`, an erasure, `code.repo.remove`). The trigger
is an open cleanup row ([below](#who-the-store-says-may-read)), whatever the source
row's `pending_delete` or `access` columns say, so an N-1 worker that clears
`pending_delete` after counting no document in the namespace stops nothing. The
retention pass reads `loom.knowledge_mirrors` and applies the namespace's
`retention_days` to its mirror. `count_documents`, the name checks of `add` and the
sync's retirement read the mirror for a mirrored source.

A namespace source and a mirrored source that reach the same file hold two documents,
one in the namespace and one in its mirror. The namespace copy is readable by every
reader, because an admin registered a source that makes it so; search answers the
address once.

### Who the store says may read

Four tables under the mirror say who may read, each written as new versions with a
`deleted` flag (no mutation):

- `loom.knowledge_document_access`, one row per mirrored address: the file's permission
  list as of the newest permissions read that succeeded, by any account of any holder:
  `emails` (people the store grants any role, lower-cased), `groups` (group emails),
  `domains` (a domain permission the store marks discoverable), `anyone` (a public
  permission the store marks discoverable), `read_at` (when the list was read) and
  `changed_at` (the read time at which the list last differed from the one before it). A
  permission list belongs to the file, so the newest read replaces the older one, and
  the row's `version` is its read time, so a turn that read earlier but writes later
  never replaces a newer list. Every turn that reads the list writes the row, changed or
  not, once per address however many accounts list the file: freshness is the row's own
  `read_at`, with no state shared between sources that a stopped source could leave
  behind. That costs one small row per mirrored file per turn (a mirror of 10,000 files
  synced every 15 minutes writes about a million a day, which `ReplacingMergeTree`
  merges away); the implementing pull request measures it.
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
  start, the earliest moment it can have seen a file.
- `loom.knowledge_group_members`, one row per source and group: the members of a group
  the crawl's directory read listed, and `read_at`. A group is stored once, not on every
  file shared with it.

A stopped source's rows go on one trigger, whichever path stopped it: an open row of
`loom.knowledge_source_cleanups`, written by every stop N makes and by the anti-join
below. Every stop writes it under the source lease, with the source's id, name,
namespace and mirror, and `retire_documents`: true for a mirrored source, and the
`remove`'s `delete_documents` for a `namespace` source (`false` by default). Because the
row is keyed by the source's id, a source added later under the same name, which
replaces the stopped row in the registry, cannot hide it.

A pass cleans at most 100 open cleanup rows, oldest first, each under that source's own
turn lease (`knowledge-source-sync:<org>/<namespace>/<name>`, as today's retirement of a
removed source runs), leaving it for the next pass when the lease is busy; a turn still
in flight finishes, or stops at its next write's check, before the cleanup starts. Each
batch of hold, access, readers, listing and group-members writes first checks the
source's registration (`require_current`, one read per batch), and an address's access
row is written only after its hold, so a turn that starts after the cleanup writes
nothing either, and no such row lands after it. For each it writes `deleted` the
source's readers, listing and group-members rows, then drops its holds wherever they
live (the namespace or its mirror). With `retire_documents`, a document left with no
hold is retired; without it, such a document stays as a kept document, as `remove`
without `delete_documents` keeps one today. It then marks the cleanup done. So no row of
a stopped source can admit anyone, and none names an account in any form other than its
pseudonym, even for a source that never reached a file.

The anti-join finds what no stop of N recorded: a source N-1 stopped (its row inactive,
with its `source_id`), and one whose name an N-1 `add` has since reused (its row gone
from the registry under `FINAL`). It reads, with `FINAL` and live rows only (`held`, not
`deleted`), the distinct `source_id`s (with the source `name` those rows carry) of the
holds, listing and group-members tables, maps a row under a mirror back to its namespace
(one read of the org's `loom.knowledge_mirrors` rows, which are few), and uses that
namespace and the name for the turn lease's key and the registry re-check; the cleanup
row records the namespace and the mirror. It does these reads under
`sequential_reads()`, and takes each one that no active registry row carries and no
cleanup row, open or done, already names, at most 100 a pass. For each it reads again,
under the source lease, that no active registry row carries the `source_id` (skipping it
if one does, so a replica's lag never retires a live source), and only then opens its
cleanup; a readers row with no live listing row of its source admits nobody under rule 3
and goes with its source's cleanup. A cleanup it opens takes `retire_documents` from
what the stop asked: true in a mirror, and for a `namespace` source the inactive
registry row's `pending_delete` (an N-1 `remove(delete_documents=true)` sets it), or
false when no registry row is left to say. The implementing pull request measures the
anti-join's cost per pass. A source whose registry row a purge deleted went with its
namespace's erasure, mirror included.

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
`permissions.list` with `supportsAllDrives`. When that is refused too, the turn keeps
the file's last permission list and only the readers rows prove access past its
`read_at`. For `linear` the list is the union of the members of every team the project
belongs to. A `crawl` source expands, through its directory read, the groups its
`subjects` name, and only those, within its cap of 2000 people; a group on a file's list
that no crawl of the mirror expanded admits nobody, and the people named directly still
read. A `connected` source reads no directory and expands no group itself.

A reader sees a mirrored document when rule 1 holds, and rule 2 or rule 3:

1. The reader holds `viewer` on the source's namespace (the ordinary gate).
2. The file's permission list has `read_at` within
   `LOOM_KNOWLEDGE_SOURCE_ACL_STALE_SECONDS` (default 3600) of now, and one of the
   reader's verified emails is in its `emails`, or in the members of one of its `groups`
   from an equally fresh members row, or that email's domain is in its `domains`, or its
   `anyone` is set.
3. A readers row matches the reader (one of the reader's own active connections for a
   connected source, one of its verified emails for a crawl), and the listing row of
   that readers row's own source and account says its last complete listing started
   within the stale bound and after the file's permission list last changed
   (`changed_at`), or no list could be read.

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
together, and a file whose list it alone reads keeps its older `read_at`. A large
mirrored source therefore needs a stale bound longer than the time to list it all;
`inspect` and `loom doctor` report the oldest listing start and the oldest `read_at`
each source has. The cost per turn is the listing pages (1000 files each), one
permissions read per file the listing leaves it out for, one access row per listed file
whose list was read, one directory read and one members row per group, and readers
writes only for a file an account starts or stops listing. The implementing pull request
measures the writes of a crawl over 2000 people and puts the figures here.

Each mirror leg first computes the addresses the reader may see: one read of the
mirror's four tables with `FINAL`, newest row per key, filtered by `has` and `hasAny`
over the reader's emails and connection accounts, then the chunk search, outline or list
restricted to those addresses before its `LIMIT`, so a page is never cut short by the
check. Any skip index on those tables is read only under
`use_skip_indexes_if_final_exact_mode=1`, so a granule skipped for its newest version
cannot leave an older one that names the reader. No admitted set is cached. The
implementing pull request measures the leg (rows read, memory, p50) on a mirror of a
million documents and puts the figures here. A leg that cannot read the tables shows no
mirrored document and marks the answer `degraded: mirror_unread`.

A stopped sync hides every mirrored document once the stale bound passes.

### Threat model

| Threat                                                                   | Boundary                                                                                                                                                                                                                                | Enforced by                                                            |
| ------------------------------------------------------------------------ | --------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- | ---------------------------------------------------------------------- |
| A connected person leaks a private file into a team namespace            | Every connected source is mirrored; its documents live in the mirror, and a teammate sees one only when the store names them. `access` cannot change.                                                                                   | `add` and `update`; the mirror gate; `test_document_source_leakage.py` |
| A crawl's registrant reads crawled people's private files                | No rule admits a source's registrant or owner as such; a registrant reads a file only when the store names them or their own account lists it.                                                                                          | the access check                                                       |
| An admin points someone's connected account at files they did not choose | Only a connected source's owner adds its items or changes `include`.                                                                                                                                                                    | `update`                                                               |
| A link-only share becomes a search hit for everyone                      | A permission the store marks not discoverable admits nobody.                                                                                                                                                                            | the access check                                                       |
| A person stops reading a file and Loom keeps showing it (stale ACL)      | Every turn of a mirrored source lists every file's permissions; the file's list is the newest read, so a removal applies at the next read by any account; lists older than the stale bound admit nobody.                                | the sync's mirrored listing; the access check                          |
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
  and holders. To a viewer who is neither the owner nor an admin, a `connected` source
  shows its kind, its owner as "a connected account", its item count and its last sync's
  status and counts, not its items, `config`, `created_by` or the error text, which can
  name an item. Python and TypeScript SDKs are regenerated.
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
| `loom.knowledge_source_holds`      | `(org, namespace, source_uri, source_id)`          | `name`, `held`, `version`; `namespace` is where the document lives (the mirror for a mirrored source)                                                                                                       |
| `loom.knowledge_document_access`   | `(org, namespace, source_uri)`                     | `emails`, `groups`, `domains`, `anyone`, `read_at`, `changed_at`, `deleted`, `version`; `namespace` is the mirror                                                                                           |
| `loom.knowledge_document_readers`  | `(org, namespace, source_uri, source_id, account)` | `deleted`, `version`; `namespace` is the mirror; `account` is the UUIDv5 pseudonym of `<kind>:<account id>` or of the lower-cased email                                                                     |
| `loom.knowledge_source_listings`   | `(org, namespace, source_id, account)`             | `name`, `started_at`, `deleted`, `version`; `account` is the pseudonym, or `shared:<source id>`; the start of the account's last complete listing; `namespace` is the mirror                                |
| `loom.knowledge_group_members`     | `(org, namespace, source_id, group)`               | `name`, `members`, `read_at`, `deleted`, `version`; `namespace` is the mirror                                                                                                                               |
| `loom.knowledge_source_cleanups`   | `(org, namespace, source_id)`                      | `name`, `mirror`, `retire_documents`, `done`, `version`; a stopped source whose holds and rows wait to be cleaned                                                                                           |
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
- `loom.knowledge_source_holds`, `loom.knowledge_source_cleanups` and the four access
  tables are keyed by namespace and erased with it. `loom.knowledge_document_access` and
  `loom.knowledge_group_members` hold people's emails copied from the store's permission
  lists and directory, people who are not Loom users among them; their rows are written
  `deleted` with their document or when their source stops or no longer reads that
  person, and otherwise mirror the store's current lists, so an erasure by `user_id`
  does not select them; the crawl rows that name an erased person go as the next bullet
  says.
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
  that person's listing held are retired. A crawl's documents are the org's, so a
  document someone else's listing still holds stays. Disconnect and SCIM deactivation
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
  inactive row with its new columns reset; N retires its documents by its holds, which
  N-1 does not write.
- **The new modes wait for N everywhere.** `add` and `update` answer 503 `OVERLOADED`
  (`retry_after_s`) for a source stored with `kind` empty while any live process of any
  deployment on the ClickHouse service (`loom.instances`,
  `read_live(all_deployments=True)`, as rule 25 counts) reports a schema version below
  the migration that adds the columns, or no heartbeat has been read, as rule 24 does
  for namespace guides ([rolling upgrades](rolling-upgrades.md)).
- **Rollback.** N-1 reads no mirror, but its gate admits a platform or org admin's
  wildcard token that names one, and nothing on N-1 ages the access rows. So the
  rollback procedure ([upgrades](../operations/upgrades.md)) requires, before any N-1
  replica starts, stopping every mirrored source and erasing the mirrors (`loom
knowledge mirror erase --all`, a server CLI command that reads
  `loom.knowledge_mirrors` and runs a platform-admin erasure of each mirror); the
  implementing pull request adds the step.
- **Holds and tags.** N-1 does not read holds: it treats a document carrying its
  source's tag as its own, overwrites it at a new revision (the same text) and may
  retire it on its own source's evidence though another source holds it. N's next turn
  of the other holder writes it again. The cost is a document missing for up to one
  interval.
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
- A second source that reaches a held file holds it, where it was counted `skipped`.
- The gate of `add`, `update`, `check`, `sync_now` and `remove` takes `writer`, with
  `admin` checked by the handler for shared and crawl sources; `rotate` is refused for a
  connected source.

## Settings

| Setting                                              | Default | Meaning                                                                               |
| ---------------------------------------------------- | ------- | ------------------------------------------------------------------------------------- |
| `LOOM_KNOWLEDGE_SOURCE_ACL_STALE_SECONDS`            | 3600    | a permission list, group members row or account listing older than this admits nobody |
| `LOOM_KNOWLEDGE_CONNECTED_SOURCES_MAX_PER_PERSON`    | 8       | a person's connected sources in one namespace                                         |
| `LOOM_KNOWLEDGE_CONNECTED_SOURCES_MAX_PER_NAMESPACE` | 64      | a namespace's connected sources                                                       |
| `LOOM_CONNECT_GDRIVE_CLIENT_ID`, `_SECRET`           | unset   | the shared-folder store's OAuth client; unset, those connections are off              |
| `LOOM_CONNECT_NOTION_CLIENT_ID`, `_SECRET`           | unset   | the page store's public integration                                                   |
| `LOOM_CONNECT_LINEAR_CLIENT_ID`, `_SECRET`           | unset   | the project tracker's OAuth application                                               |

The redirect URI to register with each store is
`<LOOM_PUBLIC_BASE_URL>/v1/connect/<kind>/callback`.

## Rejected

- **A permission column on `loom.documents`, filtered by every read.** A missed path
  would show the document to every reader.
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
  listing row's new start, with the sync killed between them; a stopped crawl, with or
  without holds, leaving no listing or group-members row, also when its name is added
  again before the cleanup pass; a hold of a source N-1 removed, with and without a
  re-add under its name, cleaned by the anti-join; a cleaned source opened by no later
  anti-join; an anti-join running while N's own cleanup of the same source is open,
  which keeps that cleanup's `retire_documents`; an N-1 `remove(delete_documents=true)`
  of a `namespace` source retiring its documents; a turn in flight when its source is
  removed, and a turn of a pass that read it active before the remove, leaving no hold,
  readers, listing or group-members row after the cleanup finishes; a cleanup skipped on
  a busy lease and run on a later pass; a hold written while another source's cleanup
  retires the document, which fetches it again and its owner still reads it, and a
  retirement that lands after the re-read, healed by the holder's next turn, for a
  mirrored and for a `namespace` holder, whose owner and listed readers both read it
  again; a readers row written between a retirement's read and its write surviving it,
  and an access row from an older read waiting for the next; two holders dropping one
  address at once on lagging replicas, which still retires it; a sync killed between a
  hold write and its tag rewrite, whose next turn fetches nothing and restores the tag;
  an anti-join over a mirrored source N-1 removed, whose lease key and re-check use the
  source's namespace; no access row left by a turn that starts after a cleanup; a
  `remove` without `delete_documents` on a `namespace` source keeping its documents
  through the cleanup; a source whose registry row a lagging replica does not show yet
  left alone; a person dropped from a crawl's expansion leaving no readers or listing
  row; a directory read that fails mid-group, or returns over the cap, dropping nobody;
  a user erasure leaving no row, of any version, keyed by the person's pseudonyms;
  `erasure.request` refusing an `acl-` scope for an org admin and a namespace admin and
  admitting a platform admin; a user erasure leaving no address of the person in
  `loom.knowledge_sources`, nor a crawl readers or listing row naming them;
  `erasure.request` and `erasure.export` refusing an `acl-` scope with auth off; the
  mirror cache after an erasure followed by a new mirrored `add`; a connected `notion`
  page shown to its owner only; a consent callback without the starting browser's
  cookie, or in another subject's session, storing nothing; an admin's `update` adding
  items to someone else's connected source refused; scope refusals on the connect
  callback and a crawl's probe; today's `check`, `rotate`, `remove` and turn over a row
  N wrote with `kind` empty.
- Integration: `tests/integration/test_document_source_leakage.py`, beside
  `test_cross_principal_leakage.py`, seeds mirrored documents with canary text and reads
  every knowledge read op, `knowledge.reads`, `knowledge.query` and
  `knowledge.source.inspect` as a namespace viewer the list does not name, as one whose
  permission was removed, as a group member after removal, after the stale bound, as a
  service account, through a link-only share, as a crawl's registrant, as another
  namespace and as another org; any canary fails.
- Integration: a `connected` source is mirrored wherever it writes; a SCIM deactivation
  and a disconnect stop the person's sources and retire their mirrored documents.
- Integration: two sources holding one address; removing an item retires only what no
  other item reaches; `namespace.delete` and `collection.delete` retire the namespace's
  sources and clear their credentials.

## Related

- [Knowledge: document sources](../features/knowledge.md#document-sources)
- [Document extraction](document-extraction.md)
- [Security](security.md)
- [Sharing knowledge](sharing.md)
- [Identities and namespaces](../concepts/identities-and-namespaces.md)
- [Rolling upgrades](rolling-upgrades.md)
