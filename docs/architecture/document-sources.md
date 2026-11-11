# Document sources

A document source pulls files from a document store (a shared-folder store, a page store, a
project tracker, a meeting store) into a namespace's documents and keeps them in step. This
page is the design of sources on any namespace, sources of many items given as pasted
links, per-user connected accounts, an org-wide crawl, and access that follows the store's
own permissions. The caller's view of today's sources is in
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
| A `linear` source reads project documents only.                                                        | Issues and their comments also reach the tracker plane ([issues](#project-tracker-issues)).                                                                                                                     |
| A `recording` source reads meeting-store transcripts (`meet`) or cloud-recording transcripts (`zoom`). | Meeting notes and transcripts that the meeting store saves to the shared-folder store come in through a `gdrive` source; `recording` keeps `zoom` only ([meetings](#meetings-through-the-shared-folder-store)). |

## Any namespace

A source can be registered on any active namespace, including a collection and the
caller's own `personal-<slug>` namespace; who may register which kind is in
[credentials](#credentials). The check `is_trunk_namespace` and the code
`NOT_A_CODE_NAMESPACE` go; `add` on an inactive namespace answers `NAMESPACE_INACTIVE`. A
source on a repository's trunk code namespace still tags each document `repo:<id>`, so
the repository's `code.context` and `loom_search` legs read them as today; on any other
namespace, a branch's included, it does not. The sync skips a source whose namespace is
inactive, and `code.repo.remove` still retires the sources on its trunk.

Sync state stays in `loom.code_index_state` under `knowledge_source:<name>`, keyed by the
source's namespace, which the namespace's erasure already covers.

On the dashboard, the Library shows a Sources section for the namespace or collection in
view; the repository card keeps its own.

## Items and links

A source's items are its things to read, of mixed types:

| Kind     | Item `type`              | Fields                                                                                                            | Reads                                                                                                                                               |
| -------- | ------------------------ | ----------------------------------------------------------------------------------------------------------------- | --------------------------------------------------------------------------------------------------------------------------------------------------- |
| `gdrive` | `folder`                 | `id`, `recursive` (default `false`)                                                                               | the folder's files; with `recursive`, every subfolder's too, to a depth of 8 and at most 500 folders per item                                       |
| `gdrive` | `file`                   | `id`                                                                                                              | one file of a kind `include` names                                                                                                                  |
| `gdrive` | `shared_drive`           | `id`                                                                                                              | every file of a shared drive                                                                                                                        |
| `gdrive` | `filter`                 | any of `name_contains`, `kinds`, `owner` (an email), `modified_after` (a date), and optionally `in` (a folder id) | the files that match, as a query the server builds                                                                                                  |
| `gdrive` | `meet_recordings`        | none                                                                                                              | the meeting store's recordings folder of each account a `connected` or `crawl` source reads ([meetings](#meetings-through-the-shared-folder-store)) |
| `notion` | `page`, `database`       | `id`                                                                                                              | as today's `page_ids` and `database_ids`                                                                                                            |
| `linear` | `project`                | `id`                                                                                                              | the project's documents, as today's `project_ids`                                                                                                   |
| `linear` | `team`, `project_issues` | `id`                                                                                                              | the team's or the project's issues and comments, for the tracker plane ([issues](#project-tracker-issues))                                          |

Every id keeps today's alphabet check, since an id goes into a store query or a request
path. A `filter` never takes a raw query string: each field has a pattern (a name
fragment of printable characters, an email, an ISO date, `kinds` from the `include`
table), quotes and backslashes are escaped, and `server/clients/gdrive.py` builds the
query, so a caller cannot widen a listing past what the fields say. `include` stays the
source-wide file-kind filter. `meet_recordings` is refused on a `shared` source, whose
service account has no recordings folder of its own.

A source holds at most 300 items (a wire contract, not a setting). A listing asks the
store about at most 50 parent folders per query, so a source of many folders lists in
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
| `https://linear.app/<workspace>/team/<key>/…`                                         | `linear` `team`                         |

A shared drive's own link has the folder form; the probe reads the id and turns a
`folder` item that names a shared drive into a `shared_drive` one. A link of another
host, with no id, or to something the sync does not read (a spreadsheet, a single
issue) answers `unreadable` with the reason. The parser takes a list and answers each
link, so a paste of fifty links shows which three it could not read. It is
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
row names the namespace, the name and the items added and removed. Removing every item
is refused (`BAD_CONFIG`); `remove` ends a source. Changing `access` is a new source.

### One document, many holders

One document per address per namespace, as today. `loom.knowledge_source_holds` records,
per address of the namespace the document lives in, each source that holds it: the sync
writes a hold when one of the source's items lists the file and drops it on positive
evidence (today's retirement rule) that none of its items reaches the file. A document
is retired when no active source holds its address. A source that lists a file another
source already wrote at the listed revision writes its hold and fetches nothing. A
document's `source:<name>` tags are the holders' names: when the set of holders changes
without a new revision, the sync rewrites the tags with `knowledge.update_metadata`,
which fetches nothing. A document another writer (a person, an agent) holds is never
overwritten or retired by any source, as today.

## Credentials

A source has a `credential_mode`:

| Mode        | Credential                                                                                                 | Registers it                                                               | Writes to                                                                          |
| ----------- | ---------------------------------------------------------------------------------------------------------- | -------------------------------------------------------------------------- | ---------------------------------------------------------------------------------- |
| `shared`    | Today's: a service-account key, an integration token, an API key                                           | an `admin` of the namespace                                                | the namespace                                                                      |
| `connected` | The registering person's own connected account for the kind's store                                        | a person with `writer` on the namespace; the source is theirs              | that namespace; `access: mirrored` unless it is the person's own `personal-<slug>` |
| `crawl`     | A service account with domain-wide delegation that reads, as each listed person, what that person can read | an `admin` of the namespace who also holds `org_admin` or `platform_admin` | the namespace, `access: mirrored`                                                  |

The source ops' gate drops from `admin` to `writer` for `add` and `update`. The handler
then requires `admin` for a `shared` or `crawl` source, and for a `connected` one that
the caller is its owner or an admin of the namespace. `check`, `sync_now`, `remove` and
`rotate` follow the same rule. A person holds at most `LOOM_KNOWLEDGE_SOURCES_MAX_PER_NAMESPACE`
connected sources per namespace, apart from the namespace's own quota for the other
modes, so writers cannot fill an admin's quota. A crawl's credential reads every listed
person's drive, which is why it needs an org-wide role and not only a namespace that its
creator administers.

### Connected accounts

A person connects their own account from the dashboard (Settings → Connected accounts)
or with `loom connect gdrive|notion|linear`, which opens the same flow in a browser.

- `GET /v1/connect/<kind>/start` (a signed-in session) answers a redirect to the store's
  consent page. `state` is single use, kept in Keeper like Loom's own OAuth state for 10
  minutes, and bound to the starting subject: a callback in another subject's session is
  refused, so nobody can attach their store account to someone else. PKCE where the
  store takes it.
- Scopes, the narrowest each store has:
  - `gdrive`: `https://www.googleapis.com/auth/drive.readonly` and `openid email`. The
    Drive scope is a restricted one, so the operator registers an OAuth client that the
    store treats as internal to the org's workspace; a public client needs the store's
    verification first. `drive.file` with the store's file picker reads only files
    picked in a Loom page, never a folder's later files or the recordings folder, so it
    is not offered. The store's device grant does not issue Drive scopes.
  - `notion`: the public-integration OAuth, where the person picks the pages to share on
    the consent page. The store reports no scopes; the operator registers the
    integration with read-content capability only, and that registration is the bound.
  - `linear`: an OAuth application with the `read` scope.
- `GET /v1/connect/<kind>/callback` exchanges the code. A token answer that names a scope
  beyond those refuses the connection. The callback stores in `loom.connected_accounts`
  the subject, the kind, the store account's id and verified email, the scopes, and the
  long-lived credential: the refresh token, or for a store that issues none (`notion`) its
  long-lived access token. That credential is sealed under the purpose
  `connected_accounts.credential`, which `loom secrets reseal` covers. A short access
  token is held in memory only.
- A connection belongs to the subject that made it, and only that subject's sources use
  it. `connection.list` and `connection.disconnect` act on the caller's own. An org admin
  sees in admin Users which kinds each person has connected, never a token or the store
  account's email.
- `connection.disconnect` revokes the credential at the store (best effort), clears the
  sealed copy, and stops every source that uses it (inactive, its mirrored documents
  retired, see [where mirrored documents live](#where-a-mirrored-document-lives)). A
  SCIM deactivation does the same for every connection of every subject it revokes, in
  the same request. A turn also checks the owner: a source whose owner subject is
  revoked, deactivated, or no longer holds `writer` on the source's namespace is skipped
  with `status: owner_inactive`, so a missed hook fails closed.

A connected source reads the person's own files, the files and folders shared with
them, their recordings folder, and the pages and teams they can read.

### Org-wide crawl

A `crawl` source's credential is a service-account key whose client id the org's
workspace administrator granted domain-wide delegation for `drive.readonly`,
`admin.directory.group.member.readonly` and `admin.directory.user.readonly`. Its config
holds `directory_subject` (a workspace administrator the directory reads impersonate)
and `subjects`: people (emails) and groups (group emails), whose members the directory
read lists at each turn, at most 2000 people. For each person the sync exchanges the key
for a token that impersonates them (the JWT bearer grant with `sub`) and lists the
source's items as them. A typical config is one `meet_recordings` item and a `filter`
with `kinds: [docs]`, over a group: every member's meeting notes, without anyone
connecting.

A turn reads people in order and keeps a per-person cursor in the sync state, so a crawl
too large for one pass (`LOOM_KNOWLEDGE_SOURCE_SYNC_MAX_PASS_SECONDS`) goes on from the
next person on the next pass. A person's files carry the time their listing was read
(below); a person not reached within the stale bound stops admitting readers through
that listing. A crawl over many people therefore needs a stale bound longer than the time
to read them all, and `inspect` reports the oldest person's read time.

The audit rows of `add`, `update` and `rotate` record the subjects, the members resolved
at the probe and the scopes the token answer granted. A token answer with any scope
beyond the three refuses the credential.

## Access follows the source

A source's `access`:

- `namespace`: every reader of the namespace reads every document, as today. The only
  mode for a `shared` source of a store whose permissions the sync does not read
  (`notion`, `recording`), and the default for `shared` `gdrive` and `linear` sources.
- `mirrored`: a document is shown only to a reader the store says may read it. Required
  for a `crawl` source and for a `connected` source writing anywhere but its owner's
  personal namespace. A `connected` `notion` source therefore writes only to its owner's
  personal namespace: the page store does not show an integration a page's permissions.

### Where a mirrored document lives

A mirrored document is not stored in the source's namespace but in that namespace's
mirror: a namespace named `acl-<24 random hex>`, made with the first mirrored source of
the namespace and recorded in `loom.knowledge_mirrors` (org, namespace, mirror). The gate
admits no token to a name of that form (`acl-[0-9a-f]{24}`) but a platform admin's, for
operator erasure and export: `check_authorized` and `CachedGate` refuse it before any
other rule, and `namespace.register` and `restore` refuse the form. A boot warning lists
any existing namespace of that form, as it does for `col-` names. A mirror has no
directory row and appears in no namespace listing.

The knowledge reads of a namespace that are taught to add a leg over its mirror, with the
check below. Every other read path reads the namespace and finds nothing of the mirror.
So a new op, a context-pack leg or an export that nobody taught the check shows no
mirrored document, and neither does an N-1 replica. A permission column on
`loom.documents` would make the opposite the default; [sharing](sharing.md#alternatives-not-taken)
rejected that for collections, since one missed path leaks.

The legs that read the mirror, each with the check: `knowledge.search`, `get`, `outline`,
`list`, `download`, `synthesize`, `inspect` (titles), the `search.query` knowledge and
repository-docs legs, `code.context`'s docs section, `context.pack`, `memory.stack`,
`memory.ask` and the assistant (which calls the same handlers under a token that carries
the person's subject). A `get`, `outline` or `download` of a mirrored address the reader
may not see answers `found=false`, as for an absent one. Decision mining skips mirrored
documents, since a decision row is a memory every reader of the namespace reads;
`knowledge.reads`, `dedup_list`, the tune sample and the dataset ops read no mirror.

A mirrored source's documents are never kept past it: `remove` retires every one no
other live source holds (`delete_documents` is implied), and so does a disconnect, a
SCIM deactivation and the owner check. The namespace's retention (`retention_days`)
applies to its mirror. `count_documents`, the name checks of `add` and the sync's
retirement read the mirror for a mirrored source.

A namespace source and a mirrored source that reach the same file hold two documents,
one in the namespace and one in its mirror. The namespace copy is readable by every
reader, because an admin registered a source that makes it so; search answers the
address once.

### Who the store says may read

`loom.knowledge_document_access` holds one row per mirrored address, keyed by the
mirror's namespace and the address, and written as a new version (no mutation) on every
turn that lists the file:

- `emails`: people the store grants any role, lower-cased; `groups`: group emails;
  `group_members`: the members of those groups the turn could resolve; `domains`: a
  domain permission the store marks discoverable; `anyone`: a public permission the store
  marks discoverable.
- `readers`: the store accounts that listed the file in this turn: the connected
  account's email, or each person a crawl listed it as.
- `read_at`: when the turn read it.

A link-only permission (an anyone-with-link or domain-with-link share, which the store
marks not discoverable) admits nobody: it lets someone who holds the link open the file,
not find it in a search. For `gdrive` the list is the file's `permissions` (type, role,
`emailAddress`, `domain`, `allowFileDiscovery`), read with the listing when the reading
account may share the file. When it may not (a view-only file), or for a file in a
shared drive, where the listing leaves `permissions` out, the turn reads
`permissions.list` with `supportsAllDrives`. When that is refused too, the list is empty
and only `readers` admit. For `linear` the list is the union of the members of every
team the project or issue belongs to.

Mirrored sources list every file on every turn, metadata only (id, revision,
permissions): the store does not change a file's modification time when its sharing
changes, so an incremental listing would miss a permission removed. Content is still
fetched only for a file whose revision changed. Groups are expanded on every turn, once
per group per turn. The cost per turn is the listing pages (1000 files each), one
permissions read per file whose list the listing leaves out, and one directory read per
group, paced like today's listing.

A reader sees a mirrored document only when all of these hold:

1. The reader holds `viewer` on the source's namespace (the ordinary gate).
2. `read_at` is within `LOOM_KNOWLEDGE_SOURCE_ACL_STALE_SECONDS` (default 3600) of now.
3. One of the reader's verified emails is in `emails`, `group_members` or `readers`, or
   its domain is in `domains`, or `anyone` is set, or the reader is the source's owner.

The reader's verified emails are its subject's directory email when `VERIFIED_EMAIL_SQL`
admits it (an SSO account or a linked one), the emails of the subject's SSO links, and
the store-account emails of the subject's own connections. A delegated token (an
agent's, the assistant's: a service account with a `creator`) reads with its creator's
emails. Every other service account, bot and end-user token has none, so it sees only
`anyone` documents. A deployment without SSO admits through the owner and connection
rules alone. A group the turn could not expand admits nobody, and the document stays
visible to the people named directly.

Each mirror leg computes the reader's admitted addresses first: one read of the mirror's
access rows (one per document) with the three rules as `hasAny` and `has` predicates
over the reader's emails, then the chunk search, outline or list restricted to those
addresses before its `LIMIT`, so a page is never cut short by the check. Its cost grows
with the mirror's document count, not with the request.

A file whose list shrinks is hidden from the removed reader at the next turn's write; a
file the turn could not list keeps its old row, which ages out at the stale bound. A
document is never shown on a list older than one sync interval plus the stale bound, and
a stopped sync hides every mirrored document once the stale bound passes. `loom doctor`
warns when the stale bound is not above the sync interval plus the pass bound
(`knowledge_source_sync_interval_seconds` + `knowledge_source_sync_max_pass_seconds`),
when mirrored documents would age out between turns.

### Threat model

| Threat                                                                 | Boundary                                                                                                                                                                                 | Enforced by                                                            |
| ---------------------------------------------------------------------- | ---------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- | ---------------------------------------------------------------------- |
| A connected person leaks a private file into a team namespace          | Outside its owner's personal namespace a connected source is mirrored; its documents live in the mirror, and a teammate sees one only when the store names them. `access` cannot change. | `add` and `update`; the mirror gate; `test_document_source_leakage.py` |
| A link-only share becomes a search hit for everyone                    | A permission the store marks not discoverable admits nobody.                                                                                                                             | the access check                                                       |
| A person stops reading a file and Loom keeps showing it (stale ACL)    | Every turn of a mirrored source lists every file's permissions; a row older than the stale bound admits nobody; a stopped sync or an unlistable file ages out.                           | the sync's mirrored listing; the access check (rule 2)                 |
| A person leaves a group and keeps reading through it (group churn)     | Groups are expanded on every turn and stored with the row, so they age with it; an unexpandable group admits nobody.                                                                     | the access check (rule 3)                                              |
| A person leaves the org and their connected sources keep syncing       | SCIM deactivation and disconnect stop the sources and clear the credential; each turn's owner check skips a source whose owner is revoked, deactivated or no longer a writer.            | `server/auth/scim.py`; the sync's turn                                 |
| A crawl reads more than its registrant meant                           | Read-only scopes checked on every token answer; an org-wide role to register; subjects and resolved members in the audit row.                                                            | the crawl probe; `loom.audit_log`                                      |
| An id-addressed read confirms a mirrored document exists               | `get`, `outline`, `download` and `inspect` answer `found=false` for an address the reader may not see.                                                                                   | the mirror legs                                                        |
| A new read path or an older build shows mirrored documents to everyone | Mirrored documents live in a namespace no grant opens and no listing names; a path that does not read the mirror shows none.                                                             | `check_authorized` (`acl-[0-9a-f]{24}`)                                |
| A mirrored document's text reaches every reader through a derived row  | Decision mining skips mirrored documents.                                                                                                                                                | the sync's mining step                                                 |
| Someone links their store account into another person's Loom account   | The connect `state` is bound to the starting subject.                                                                                                                                    | the connect callback                                                   |
| A caller injects store query syntax through a filter                   | Filters are fields with patterns, escaped and built by the server; no raw query is taken.                                                                                                | `server/clients/gdrive.py`                                             |
| A stolen connected credential changes the store                        | Only read scopes are accepted; the credential is sealed and never returned.                                                                                                              | the connect callback; `server/core/secrets_at_rest.py`                 |
| A private issue reaches every tracker reader                           | Issues from a connected account or a private team are `is_private = 1`, which a standard tracker namespace never reads.                                                                  | the tracker plane's private-row rule                                   |

Out of scope: a reader who already saw a document and kept its text; the store itself
reporting a wrong permission list.

## Meetings through the shared-folder store

The meeting store saves a meeting's transcript and its notes as Docs in the organiser's
recordings folder of the shared-folder store, and shares them with the participants. A
`gdrive` source reads them like any file: a `meet_recordings` item, or a `folder` item
for that folder. `meet_recordings` finds the folder the meeting store made in each
account's own drive by its name, from a fixed table of the names the store gives it in
each language; when none is found, the turn reports `meet_recordings: not found` for that
account, and a `folder` item with the folder's link reads it instead.

The sync recognises a transcript Doc (one in a recordings folder whose exported text has
the store's transcript shape: a title line, then speaker turns under time stamps) and
writes it with `server/knowledge/domain/transcripts.py`'s timed sections,
`ingested_format: transcript`, as a `recording` source writes one, so a citation names
the minute. A meeting-notes Doc is written as any Doc. Its permission list (the
organiser and the invited participants) makes a mirrored source show each meeting to
the people invited.

The `meet` provider of the `recording` kind is removed; `recording` keeps `zoom`, whose
cloud recordings do not land in the shared-folder store. This is a break: a stored
`meet` source is listed and inspected, refused by `check`, `rotate`, `sync_now` and
`update` (`BAD_CONFIG`, naming the `gdrive` route), and skipped by the sync with
`status: provider_removed`, which counts as no failure of the pass; its documents stay
until it is removed.

## Project-tracker issues

A `linear` source's `team` and `project_issues` items feed issues and their comments into
the tracker plane, through a `shared` key or a `connected` account. The tracker plane
reads a team's own records live and copies none
([tracker plane](../features/tracker/overview.md)); these issues have no team database
to read, so Loom keeps them itself, as the plane keeps its derived state:

- `loom.tracker_feed_items` holds rows in the contract's `github_issues` shape, keyed by
  (org, tracker namespace, `id`), with `id` `uuid5(NAMESPACE_DNS, 'linear_issue:' || <the
issue's UUID>)`, so an issue moved to another team keeps its item. `source_id` is the
  issue's current `<team key>-<number>`, `url` its link, `labels`, `assignees`,
  `state_reason` from the issue's state type (`completed`; `canceled` → `not_planned`;
  a duplicate → `duplicate`), the times, and `body` the description followed by each
  comment as `<author>: <text>`.
- `is_private` is 1 for an issue of a private team and for every issue a `connected`
  source reads, so only a restricted tracker namespace reads them
  ([public and private](../features/tracker/record-contract.md#public-and-private)).
- `loom.tracker_feed_edges` holds `duplicate_of`, `related_to` and `fixes` (an attachment
  that names a pull request).
- A tracker source of kind `feed` reads these tables in Loom's own ClickHouse for its
  tracker namespace. The contract's `source_id` names a repository today; the issues pull
  request widens it to take a tracker key and keeps every repository-keyed read as it
  is.

A team's issues are an incremental listing by `updatedAt` with today's stamp rules. Both
tables are keyed by the tracker namespace and in the erasure census. Delivered after the
items and access pull requests, in its own.

## Surfaces

- **Ops.** `knowledge.source.add` takes `items` or `links`, `credential_mode` and, for
  `crawl`, `directory_subject` and `subjects`; `update`, `parse_links`,
  `connection.list` and `connection.disconnect` are new; `list` and `inspect` show
  items, modes and holders. To a viewer who is neither the owner nor an admin, a
  `connected` source shows its kind, owner as "a connected account", item count and last
  sync, not its items. Python and TypeScript SDKs are regenerated.
- **CLI.** `loom knowledge source add|update|list --namespace <ns> --link <url>...`
  (`--credential-file`, `--connected`), `loom connect gdrive|notion|linear`,
  `loom connection list|disconnect`.
- **Dashboard.** The Library's Sources section: paste many links, a preview of what each
  resolves to and which could not be read, Check, Add, edit items, the access mode, and
  each source's last sync. Settings → Connected accounts: connect, see, disconnect. The
  repository card keeps its own sources.
- **Agents.** Every op reaches an agent through `loom_op`. The mentor reference gains a
  line: handed a store link and asked to bring it in, call `knowledge.source.update`
  with `links` on the caller's connected source for that store, or `add` one; with no
  connection, `connection.list` answers the dashboard link
  `<public base>/settings/connected-accounts?kind=<kind>` to hand back.

## Data model

ClickHouse, additive migrations:

| Table                               | Key                                                        | Holds                                                                                                                          |
| ----------------------------------- | ---------------------------------------------------------- | ------------------------------------------------------------------------------------------------------------------------------ |
| `loom.knowledge_sources` (columns)  | `(org, namespace, name)`                                   | `credential_mode` (default `shared`), `access` (default `namespace`), `owner_subject`, `items_json`, `secret_enc`              |
| `loom.knowledge_mirrors`            | `(org, namespace)`                                         | `mirror`, `created_at`                                                                                                         |
| `loom.knowledge_source_holds`       | `(org, namespace, source_uri, source_id)`                  | `held`, `version`; `namespace` is where the document lives (the mirror for a mirrored source)                                  |
| `loom.knowledge_document_access`    | `(org, namespace, source_uri)`                             | `emails`, `groups`, `group_members`, `domains`, `anyone`, `readers`, `read_at`, `source_id`, `version`; `namespace` the mirror |
| `loom.connected_accounts`           | `(org, subject, kind)`                                     | `account_id`, `account_email`, `scopes`, `credential_enc`, `active`, `connected_at`, `disconnected_at`, `version`              |
| `loom.tracker_feed_items`, `_edges` | `(org, namespace, id)`, `(org, namespace, from_id, to_id)` | the contract's columns                                                                                                         |

A source N-1 can sync keeps its credential in `credential_enc` and its items in
`config_json` as today, and N writes both forms. Every other source (mirrored,
`connected`, `crawl`, or with an item today's config cannot express) keeps its
credential in `secret_enc` (purpose `knowledge_sources.secret`) and its items in
`items_json`, with `credential_enc` empty and `config_json` `{"folder_ids": []}` (or the
kind's empty list) ([rolling upgrades](#rolling-upgrades)). The mirror's documents and
chunks live in `loom.documents` and `loom.chunks` under the mirror's namespace, so the
document machinery (heads under the Postgres authority, deliveries, originals, the blob
sweep) applies unchanged.

## Erasure and audit

- A namespace's erasure covers its mirror: every namespace-keyed step and census entry
  runs over the mirror's name too, so a step added later covers it by the same rule.
- `loom.knowledge_mirrors`, `loom.knowledge_source_holds`,
  `loom.knowledge_document_access` and the tracker feed tables are keyed by namespace and
  erased with it. The access table holds people's emails, copied from the store's
  permission list each turn, so it holds what the store holds and is rewritten from it;
  an erasure by `user_id` does not select it. `loom.connected_accounts` rows are deleted
  with the subject's account and cleared by disconnect and by SCIM deactivation.
- Every new table is classified in `server/erasure/store/census.py`.
- Audit rows: `add`, `update`, `rotate`, `remove`, `sync_now` as today, plus `update`'s
  item changes; `connect` and `disconnect` (subject, kind, scopes, never a credential);
  a crawl's subjects and resolved members; a SCIM-driven stop names the sources it
  stopped.

## Rolling upgrades

- **N-1 cannot sync a source it must not.** N-1 syncs only sources on a trunk namespace
  (its `_usable`), unseals `credential_enc` and reads `config_json`. A source N writes
  with `credential_enc` empty ends every N-1 turn `credential_invalid` before any
  request, and retires nothing. Its `config_json` of empty lists makes an N-1 `rotate`
  or `check` probe fail, so N-1 cannot give it a credential it would then sync. Pinned
  by a test that runs today's turn and probe over such a row.
- **The mirror.** `add` and `update` answer 503 `OVERLOADED` (`retry_after_s`) for a
  mirrored source while any live replica's schema version (`loom.instances`) is below
  the migration that adds the mirror, or no heartbeat has been read, as rule 24 does for
  namespace guides ([rolling upgrades](rolling-upgrades.md)). A rollback to N-1 after
  mirrors exist leaves them unread by every leg N-1 has, but N-1's gate admits a platform
  or org admin's wildcard token that names a mirror; a mirror's name is random and in no
  listing, and the rollback procedure stops mirrored sources and erases the mirrors
  first (`loom knowledge mirror erase --all`) when that is not acceptable.
- **Holds and tags.** N-1 does not read holds: it treats a document carrying its
  source's tag as its own, overwrites it at a new revision (the same text) and may retire
  it on its own source's evidence though another source holds it. N's next turn of the
  other holder writes it again. The cost is a document missing for up to one interval.
- **State.** Unchanged: `loom.code_index_state`.
- **`meet`.** N-1 still syncs a stored `meet` source on its turns; N skips it.
- **Erasure.** N-1's erasure misses the mirror and the new tables (rule 14): run again
  with N every erasure completed while an N-1 replica served.

Each implementing pull request adds its rule to [rolling upgrades](rolling-upgrades.md).

## Breaks

- The source ops no longer answer `NOT_A_CODE_NAMESPACE`; `add` answers
  `NAMESPACE_INACTIVE` for an inactive namespace.
- `recording` sources lose the `meet` provider.
- A second source that reaches a held file holds it, where it was counted `skipped`.
- `add` and `update` take `writer` at the gate, with `admin` checked by the handler for
  shared and crawl sources.

## Settings

| Setting                                    | Default | Meaning                                                                  |
| ------------------------------------------ | ------- | ------------------------------------------------------------------------ |
| `LOOM_KNOWLEDGE_SOURCE_ACL_STALE_SECONDS`  | 3600    | a mirrored document's list older than this admits nobody                 |
| `LOOM_CONNECT_GDRIVE_CLIENT_ID`, `_SECRET` | unset   | the shared-folder store's OAuth client; unset, those connections are off |
| `LOOM_CONNECT_NOTION_CLIENT_ID`, `_SECRET` | unset   | the page store's public integration                                      |
| `LOOM_CONNECT_LINEAR_CLIENT_ID`, `_SECRET` | unset   | the project tracker's OAuth application                                  |

The redirect URI to register with each store is
`<LOOM_PUBLIC_BASE_URL>/v1/connect/<kind>/callback`.

## Rejected

- **A permission column on `loom.documents`, filtered by every read.** A missed path
  would show the document to every reader.
- **A namespace per permission list.** A list changes with every share, which would move
  the document, its chunks and embeddings on each change.
- **A mirror name derived from the namespace.** Anyone could compute it, and an N-1 gate
  admits a wildcard admin token to a computed name.
- **Resolving the store's groups through Loom's SCIM groups.** They hold a service's end
  users (`user:<id>`), not people's emails.
- **A separate meeting-store path.** The meeting store already saves transcripts and
  notes to the shared-folder store, with the participants' permissions.
- **`drive.file` with the picker.** It cannot read a folder's later files.
- **Incremental listings for mirrored sources.** A sharing change does not move a file's
  modification time.

## Invariants

- A mirrored document is read only through a leg that applies the permission check; a
  mirror admits no token but a platform admin's.
- A `connected` or `crawl` source writes to a namespace other than its owner's personal
  one only with `access: mirrored`.
- A stale, unresolvable, link-only or unknown permission admits nobody.
- No caller-supplied host or query string reaches a store.
- A connected source stops when its owner disconnects, is deactivated, or loses `writer`
  on the namespace, and its mirrored documents go with it.

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
  domain, link-only, anyone, readers, owner and unknown permissions; the `acl-` gate in
  `check_authorized` and `CachedGate`; the mixed-version refusal; scope refusals on the
  connect callback and a crawl's token; today's turn and probe over a row N writes for
  a source N-1 must not sync.
- Integration: `tests/integration/test_document_source_leakage.py`, beside
  `test_cross_principal_leakage.py`, seeds mirrored documents with canary text and reads
  every knowledge read op as a namespace viewer the list does not name, as one whose
  permission was removed, as a group member after removal, after the stale bound, as a
  service account, through a link-only share, as another namespace and as another org;
  any canary fails.
- Integration: a `connected` source outside its owner's personal namespace is mirrored; a
  SCIM deactivation and a disconnect stop the person's sources and retire their
  mirrored documents.
- Integration: two sources holding one address; removing an item retires only what no
  other item reaches.

## Related

- [Knowledge: document sources](../features/knowledge.md#document-sources)
- [Document extraction](document-extraction.md)
- [Security](security.md)
- [Sharing knowledge](sharing.md)
- [Identities and namespaces](../concepts/identities-and-namespaces.md)
- [Rolling upgrades](rolling-upgrades.md)
