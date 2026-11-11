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
| A source lives only on a repository's trunk code namespace (`NOT_A_CODE_NAMESPACE`).                   | A source lives on any namespace whose admin registers it ([any namespace](#any-namespace)).                                                                                                                     |
| A source holds 1 to 20 folders, projects, pages or databases, and no op changes its `config`.          | A source holds up to 300 items of mixed types, given as links or ids, and `knowledge.source.update` adds, removes and changes them ([items](#items-and-links)).                                                 |
| One shared credential per source, typed by an admin.                                                   | A shared credential, a person's own connected account, or a delegated service account that reads many people's drives ([credentials](#credentials)).                                                            |
| Every reader of the namespace reads every synced document.                                             | A source's `access` is `namespace` or `mirrored`; a mirrored document is shown only to a reader the store's own permission list names ([access](#access-follows-the-source)).                                   |
| Two sources that reach one file: the second is `skipped`.                                              | The address is held by every source that reaches it, and retired when none does ([holds](#one-document-many-holders)).                                                                                          |
| A `linear` source reads project documents only.                                                        | Issues and their comments also reach the tracker plane ([issues](#project-tracker-issues)).                                                                                                                     |
| A `recording` source reads meeting-store transcripts (`meet`) or cloud-recording transcripts (`zoom`). | Meeting notes and transcripts that the meeting store saves to the shared-folder store come in through a `gdrive` source; `recording` keeps `zoom` only ([meetings](#meetings-through-the-shared-folder-store)). |

## Any namespace

A source can be registered on any namespace where the caller holds `admin`, including a
collection and the caller's own `personal-<slug>` namespace. The check
`is_trunk_namespace` and the code `NOT_A_CODE_NAMESPACE` go. A source on a repository's
trunk code namespace still tags each document `repo:<id>`, so the repository's
`code.context` and `loom_search` legs read them as today; on any other namespace it does
not. The sync skips a source whose namespace is inactive, as it does today, and a
repository's removal (`code.repo.remove`) still retires the sources on its trunk.

Sync state moves out of `loom.code_index_state` into `loom.knowledge_source_state`
([data model](#data-model)): a source's state is no longer part of a code index, so the
code index's purge no longer deletes it, and `knowledge.source.remove` and the
namespace's erasure do.

On the dashboard, the Library shows a Sources section for the namespace in view, beside
the repository card, which keeps its own.

## Items and links

A source's `config` holds a list of `items`, each one thing to read, of mixed types:

| Kind     | Item `type`              | Fields                                                                                                         | Reads                                                                                                                 |
| -------- | ------------------------ | -------------------------------------------------------------------------------------------------------------- | --------------------------------------------------------------------------------------------------------------------- |
| `gdrive` | `folder`                 | `id`, `recursive` (default `false`)                                                                            | the folder's files; with `recursive`, every subfolder's too, to a depth of 8 and at most 500 folders per item         |
| `gdrive` | `file`                   | `id`                                                                                                           | one file: a Doc, a Slides deck, a PDF, a `.pptx` or a `.docx`                                                         |
| `gdrive` | `shared_drive`           | `id`                                                                                                           | every file of a shared drive                                                                                          |
| `gdrive` | `filter`                 | any of `name_contains`, `kinds`, `owner` (an email), `modified_after` (a date), and optionally `in` (a folder) | the files that match, compiled to the store's query by the server                                                     |
| `gdrive` | `meet_recordings`        | none                                                                                                           | the "Meet Recordings" folder of each account the source reads ([meetings](#meetings-through-the-shared-folder-store)) |
| `notion` | `page`, `database`       | `id`                                                                                                           | as today's `page_ids` and `database_ids`                                                                              |
| `linear` | `project`                | `id`                                                                                                           | the project's documents, as today's `project_ids`                                                                     |
| `linear` | `team`, `project_issues` | `id`                                                                                                           | the team's or the project's issues and comments, into the tracker plane ([issues](#project-tracker-issues))           |

Every id keeps today's alphabet check, since an id goes into a store query or a request
path. A `filter` never takes a raw query string: each field has a pattern (a name
fragment of printable characters with quotes escaped, an email, an ISO date, `kinds` from
the `include` table), and `server/clients/gdrive.py` builds the query from them, so a
caller cannot widen a listing to files the item does not name. `include` stays as the
source-wide default for the file kinds of `folder`, `shared_drive` and `meet_recordings`
items.

A source holds at most 300 items (a wire contract, not a setting). A probe checks a
bounded sample: every item while there are at most 20, otherwise the first 20 items added
and the counts of the rest, and the answer says `sampled: true` with the number checked.
`add` refuses an item a full check would refuse only when it is in the sample; the first
sync turn reports any other as `item <n>: http_404` and goes on with the rest.

### The link parser

`server/knowledge/domain/source_links.py` turns a pasted link into an item, with no
request to the store:

| Link                                                                 | Item                                       |
| -------------------------------------------------------------------- | ------------------------------------------ |
| `https://drive.google.com/drive/folders/<id>` (any `u/<n>/` prefix)  | `gdrive` `folder`                          |
| `https://drive.google.com/drive/u/<n>/folders/<id>`                  | `gdrive` `folder`                          |
| `https://drive.google.com/file/d/<id>/…`, `…/open?id=<id>`           | `gdrive` `file`                            |
| `https://docs.google.com/document/d/<id>/…`, `…/presentation/d/<id>` | `gdrive` `file`                            |
| `https://drive.google.com/drive/shared-drives/<id>`                  | `gdrive` `shared_drive`                    |
| `https://www.notion.so/<slug>-<32 hex>`, `notion.so/<ws>/<32 hex>`   | `notion` `page` (or `database` with `?v=`) |
| `https://linear.app/<ws>/project/<slug>-<id>`                        | `linear` `project`                         |
| `https://linear.app/<ws>/team/<key>/…`                               | `linear` `team`                            |

The parser takes a list of links and answers each with its item or `unreadable` and why
(another host, no id, a link to a single Linear issue), so a paste of fifty links shows
which three it could not read. It is exposed as `knowledge.source.parse_links` (viewer;
no store request, nothing written) for the dashboard's preview and for agents. A Notion
slug link names a page unless it carries a view (`?v=`); a page that turns out to be a
database is reported by the probe and corrected by `update`. A Linear project link
carries the project's short id, which the probe resolves to the UUID the API takes.

### Changing a stored source

`knowledge.source.update` (admin) takes `add_items`, `remove_items`, a new `include` and a
new `access` (narrowing only, see below), probes what it adds with the stored credential
(the same sample rule), and writes the source at its next version under the namespace's
source lease. It is audited with the namespace, name and the items added and removed. The
rule "no op changes a stored source's `config`" goes.

Removing an item retires only the documents no other item of the source still reaches:
the next turn of the source lists every item that remains, and a document whose file is
in none of them, and that the store confirms is outside them (the positive-evidence rule
of today's sync), is retired. Removing every item is refused (`BAD_CONFIG`); `remove` is
the way to end a source.

### One document, many holders

One document per address per namespace, as today. A source holds an address while one
of its items reaches the file at that address. `loom.knowledge_source_holds` records, per
address, each source that holds it, and the sync writes a hold when it lists a file and
drops it when it has positive evidence that none of the source's items reaches the file.
A document is retired when no active source holds its address. Today's
"another source holds it: `skipped`" becomes: a second source that reaches a file writes
its hold and fetches nothing while the stored copy has the listed revision; whichever
holder's turn sees a new revision first writes it. A document another writer (a person,
an agent) holds is still never overwritten or retired by any source.

A document carries `source:<name>` for each source that held it when it was last
written, so `knowledge.search(tags=["source:<name>"])` still works; the holds table, not
the tags, decides retirement.

## Credentials

A source has a `credential_mode`:

| Mode        | Credential                                                                                                       | Who registers it               | Writes to                                                          |
| ----------- | ---------------------------------------------------------------------------------------------------------------- | ------------------------------ | ------------------------------------------------------------------ |
| `shared`    | Today's: a service-account key, an integration token, an API key                                                 | an admin of the namespace      | the namespace                                                      |
| `connected` | The registering person's own connected account for the kind's store                                              | any person with `writer` there | a namespace where they hold `writer`, by default `personal-<slug>` |
| `crawl`     | A service account with domain-wide delegation that reads, as each listed person, their own drive (`gdrive` only) | an admin of the namespace      | the namespace                                                      |

### Connected accounts

A person connects their own account with the store's OAuth from the dashboard (Settings →
Connected accounts) or `loom connect google|notion|linear`, which opens the same flow in
a browser (the device grant is not offered by these stores).

- `GET /v1/connect/<provider>/start` (a signed-in session) answers a redirect to the
  store's consent page with `state` (single use, kept in Keeper like Loom's own OAuth
  state, 10 minutes), PKCE where the store takes it, and the narrowest scopes:
  `https://www.googleapis.com/auth/drive.readonly` plus `openid email` (the account's
  email, verified by the store); Notion's public-integration OAuth, which grants the pages
  the person picks on the consent page, read content only; Linear's OAuth app with
  `read`. `drive.file` with the store's picker would read only the files picked in a
  Loom page, which covers single files but not a folder's later files or the "Meet
  Recordings" folder, so it is not offered.
- `GET /v1/connect/<provider>/callback` exchanges the code, checks the granted scopes
  (any write scope refuses the connection, as a `recording` credential is refused today),
  and stores `loom.connected_accounts`: the subject, the provider, the store account's
  verified email and id, the scopes, and the refresh token sealed under its own purpose
  (`connected_account`, covered by `loom secrets reseal`). No access token is stored.
- The connection belongs to the subject that made it. Only that subject's sources use it,
  and `connections.list` and `connections.disconnect` act on the caller's own only.
  An org admin sees, per person, which providers are connected (no token, no email of the
  store account) in admin Users.
- Disconnecting revokes the refresh token at the store (best effort), clears the sealed
  token, and stops every source that uses it (inactive, `credential_mode` kept, the
  documents left as `remove` without `delete_documents` leaves them). A SCIM
  deactivation of the person does the same for every connection of every subject the
  deactivation revokes, in the same request. The sync also checks the owner at each
  turn: a source whose owner subject is revoked, deactivated, or no longer holds `writer`
  on the source's namespace is skipped with `status: owner_inactive`, so a missed hook
  fails closed.

A connected source covers a person's My Drive, the files and folders shared with them,
their "Meet Recordings" folder, and the Notion pages and Linear teams they can read.

### Org-wide crawl

A `crawl` source's credential is a service-account key whose client id an org's
workspace administrator granted domain-wide delegation for `drive.readonly`,
`admin.directory.group.member.readonly` and `admin.directory.user.readonly`. Its config
names `subjects`: a list of people (emails) and groups (group emails, whose members are
read through the directory API at each full turn, at most 2000 people). For each person,
the sync exchanges the key for a token that impersonates them (the JWT bearer grant with
`sub`), and reads the source's items as that person. A typical config is one
`meet_recordings` item plus a `filter` (`kinds: [docs]`), over a group of the org: every
member's meeting notes, without anyone connecting.

Only an admin of the target namespace registers one, and the audit row of `add`,
`update` and `rotate` records the subjects, the group members resolved at the probe and
the scopes the token answer granted. A token answer with any scope beyond the three
refuses the credential. `access` is `mirrored` and cannot change.

## Access follows the source

A source's `access`:

- `namespace`: every reader of the namespace reads every document, as today. The only
  mode for `shared` sources of a store whose permissions the sync does not read
  (`notion`, `recording`), and the default for `shared` `gdrive` and `linear` sources.
- `mirrored`: each document keeps the store's permission list, and a read returns the
  document only to a reader the list names. Required for a `crawl` source, and for a
  `connected` source that writes anywhere but its owner's `personal-<slug>` namespace;
  the default for every `connected` source. A `connected` `notion` source can therefore
  write only to its owner's personal namespace, since the page store does not expose a
  page's permissions to an integration.

`update` may change `namespace` to `mirrored`, never back: widening a source's audience
is a new source, registered by an admin.

### Where a mirrored document lives

A mirrored document is not stored in the source's namespace. It is stored in the
namespace's mirror, a namespace of its own named `acl-<24 hex of sha256(org, namespace)>`,
which the gate admits for nobody: `check_authorized` refuses the `acl-` prefix before
any other rule, for every token but a platform admin's (for operator erasure and export),
as it refuses a deleted collection, and `namespace.register` and `restore` refuse the
prefix. The knowledge reads of a namespace, and only those taught to, add a leg over its
mirror with the permission check below.

So the default of every read path is to show no mirrored document. A read path that is
not taught the check (a new op, a context-pack leg, an export) reads the namespace and
finds nothing of the mirror, instead of everything; an N-1 replica, which knows no
mirror, shows none ([rolling upgrades](#rolling-upgrades)). A permission column on
`loom.documents` would make the opposite the default, and
[sharing](sharing.md#alternatives-not-taken) rejected that for collections because one
missed path leaks.

The legs that read the mirror, with the check: `knowledge.search`, `get`, `outline`,
`list`, `download`, `synthesize`, `inspect` (titles), the `search.query` knowledge and
repository-docs legs, `code.context`'s docs section, `context.pack`, `memory.stack`,
`memory.ask` and the assistant (which call the same handlers under a delegated token
that carries the person's subject). Decision mining does not run on a mirrored document,
since a decision row is a memory every reader of the namespace reads; `knowledge.reads`,
`dedup_list`, the tune sample and the dataset ops do not read the mirror. A
`get` or `download` of a mirrored address the reader may not see answers `found=false`,
exactly as an address that does not exist.

### The permission list

`loom.knowledge_document_access` holds, per mirrored address, the permission list as of
the sync turn that read it: `emails` (people with any role, lower-cased), `groups` (group
emails), `domains` (a domain permission), `anyone` (an anyone-with-link or public
permission), the turn's `read_at`, and the expanded `group_members` for the groups the
sync could resolve. For `gdrive` the list is the file's `permissions` (type, role,
`emailAddress`, `domain`), read with the listing of every turn; a shared drive's members
are part of each file's list. For `linear` it is the members of the team that owns the
project or issue. A permission whose type the sync does not know is kept as unresolvable.

A reader sees a mirrored document when all of these hold, and otherwise does not:

1. The reader holds `viewer` on the source's namespace (the ordinary gate).
2. `read_at` is within `LOOM_KNOWLEDGE_SOURCE_ACL_STALE_SECONDS` (default 3600) of now.
3. One of the reader's verified emails is in `emails` or `group_members`, or its domain is
   in `domains`, or `anyone` is set.

A reader's verified emails are the subject's email when `VERIFIED_EMAIL_SQL` admits it
(an SSO account or a linked one) and the emails of the SSO links that subject holds,
read once per request through the grant cache. A service account, a bot, an end-user
token and an unverified account have none, so they see only `anyone` documents. A
group the sync could not expand (a connected account cannot read a group's members, or
the directory call failed) admits nobody; the document stays visible to the people and
domains named directly.

Group membership comes from the directory API under a `crawl` source's delegation,
re-read at each full turn and cached for the source for the stale bound. Loom's SCIM
groups (`loom.principal_groups`) hold a service's end users (`user:<id>`), not signed-in
people's emails, so they cannot resolve a store's group; a deployment that wants group
permissions honoured for a `connected` source registers a `crawl` source credential for
the org, whose directory read the sync then uses for the org's groups.

The sync writes the permission list on every turn, for every listed file, whether or not
its content changed. A file whose list shrinks is hidden from the removed reader at the
next turn's write; a file the turn could not list keeps its old row, which ages out at
the stale bound. A document is therefore never shown on a list older than one sync
interval plus the stale bound, and a stopped sync hides every mirrored document after
the stale bound.

### Threat model

| Threat                                                                 | Boundary                                                                                                                                                                                                                           | Enforced by                                                                                    |
| ---------------------------------------------------------------------- | ---------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- | ---------------------------------------------------------------------------------------------- |
| A connected person leaks a private file into a team namespace          | A `connected` source writing anywhere but its owner's personal namespace is `mirrored`, and its documents live in the mirror; a teammate sees one only when the store's list names them. `update` cannot change it to `namespace`. | `knowledge.source.add`/`update` validation; the mirror gate; `test_document_source_leakage.py` |
| A person stops reading a file and Loom keeps showing it (stale ACL)    | The list is re-read on every turn; a row older than the stale bound shows the document to nobody; a stopped sync or an unlistable file ages out.                                                                                   | the access check (rule 2)                                                                      |
| A person leaves a group and keeps reading through it (group churn)     | Members are re-read at each full turn and cached no longer than the stale bound; an unexpandable group admits nobody.                                                                                                              | the access check (rule 3); the crawl's directory read                                          |
| A person leaves the org and their connected sources keep syncing       | SCIM deactivation and disconnect stop the sources and clear the token; the sync's owner check at each turn skips any source whose owner is revoked, deactivated or no longer a writer.                                             | `server/auth/scim.py`; the sync's turn                                                         |
| A crawl reads more than the admin meant                                | Read-only scopes checked on every token answer; subjects and resolved members recorded in the audit row; only a namespace admin registers it.                                                                                      | the crawl probe; `loom.audit_log`                                                              |
| An id-addressed read confirms a mirrored document exists               | `get`, `outline`, `download` and `inspect` answer `found=false` for an address the reader may not see, as for an absent one.                                                                                                       | the mirror legs                                                                                |
| A new read path or an older build shows mirrored documents to everyone | Mirrored documents live in a namespace no grant opens; a path that does not read the mirror shows none.                                                                                                                            | `check_authorized` (the `acl-` prefix)                                                         |
| A mirrored document's text reaches every reader through a derived row  | Decision mining skips mirrored documents; no mirrored chunk feeds a memory row.                                                                                                                                                    | the sync's mining step                                                                         |
| A caller injects store query syntax through a filter                   | Filters are fields with patterns, compiled by the server; no raw query is taken.                                                                                                                                                   | `server/clients/gdrive.py`                                                                     |
| A stolen connected-account token changes the store                     | Only read scopes are accepted; the token is sealed and never returned.                                                                                                                                                             | the connect callback; `server/core/secrets_at_rest.py`                                         |

Out of scope: a reader who already saw a document and kept its text; the store itself
reporting a wrong permission list.

## Meetings through the shared-folder store

The meeting store saves a meeting's transcript and its notes as Docs in the organiser's
"Meet Recordings" folder of the shared-folder store, and shares them with the
participants. A `gdrive` source reads them like any file: a `meet_recordings` item, or a
`folder` item for that folder.

The sync recognises a transcript Doc (a Doc in a "Meet Recordings" folder whose exported
text has the store's transcript shape: a title line, then speaker turns under time
stamps) and writes it with `server/knowledge/domain/transcripts.py`'s timed sections,
`ingested_format: transcript`, as a `recording` source writes one, so a citation names
the minute. A meeting-notes Doc is written as any Doc. Its permission list (the
organiser and the invited participants) makes a mirrored source show each meeting to the
people who were invited.

The `meet` provider of the `recording` kind is removed; `recording` keeps `zoom`, whose
cloud recordings do not land in the shared-folder store. This is a break: a stored
`recording` source with `provider: meet` is listed and inspected, refused by `check`,
`rotate`, `sync_now` and `update` (`BAD_CONFIG`, naming the `gdrive` route), and
skipped by the sync with `status: failed`, `error: provider removed`; its documents stay
until it is removed. [Rolling upgrades](#rolling-upgrades) covers the overlap.

## Project-tracker issues

A `linear` source's `team` and `project_issues` items feed issues and their comments into
the tracker plane, through a `shared` key or a `connected` account. The tracker plane
reads a team's records live from contract tables and stores none
([tracker plane](../features/tracker/overview.md)); for this feed Loom keeps the contract
tables itself, in the tracker database the org's tracker source points at, written by the
`knowledge_source_sync` worker on the source's turn:

- `github_issues` rows with `source_id` `<team key>-<number>`, `url` the issue link,
  `labels`, `assignees`, `state_reason` from the issue's state type (`completed`,
  `canceled` → `not_planned`, a duplicate → `duplicate`), `created_at`, `updated_at`,
  `closed_at`; `body` is the description followed by the comments, each as
  `<author>: <text>`.
- `edges` for `duplicate_of`, `related_to` and the issue's attachments that name a pull
  request (`fixes`).

The contract's `source_id` names a repository today; the issues pull request widens it
to take a tracker key, and keeps every repository-keyed read as it is. An item `id` is
`uuid5(NAMESPACE_DNS, 'linear_issue:' || source_id)`. A team's issues are an incremental
listing by `updatedAt` with the same stamp rules as documents. The tables are covered by
the erasure census, keyed by the tracker namespace. Delivered after the items and access
pull requests, in its own.

## Surfaces

- **Ops.** `knowledge.source.add` takes `items` (or a `links` list it parses),
  `credential_mode`, `access` and, for `crawl`, `subjects`; `update`, `parse_links`,
  `connections.list`, `connections.disconnect` are new; `list` and `inspect` show items,
  modes and holds. Python and TypeScript SDKs are regenerated.
- **CLI.** `loom knowledge source add|update|list --namespace <ns> --link <url>...`
  (`--credential-file`, `--connected`, `--access`), `loom connect google|notion|linear`,
  `loom connections list|disconnect`.
- **Dashboard.** The Library's Sources section: paste many links, a preview of what each
  resolves to and which could not be read, Check, Add, edit items, the access mode, and
  each source's last sync. Settings → Connected accounts: connect, see, disconnect. The
  repository card keeps its own sources.
- **Agents.** Every op reaches an agent through `loom_op`. The mentor reference gains a
  line: when handed a store link and asked to bring it in, call `knowledge.source.update`
  with `links` on the caller's connected-account source for that store (or `add` one);
  when the caller has no connection, `connections.list` answers the dashboard link
  `<public base>/settings/connected-accounts?provider=<p>` to hand back.

## Data model

ClickHouse, additive migrations:

| Table                              | Key                                       | Holds                                                                                                                              |
| ---------------------------------- | ----------------------------------------- | ---------------------------------------------------------------------------------------------------------------------------------- |
| `loom.knowledge_sources` (columns) | `(org, namespace, name)`                  | `credential_mode` (default `shared`), `access` (default `namespace`), `owner_subject` (a `connected` source's owner), `items_json` |
| `loom.knowledge_source_state`      | `(org, namespace, source_id)`             | the sync state now in `loom.code_index_state` (`knowledge_source:<name>`), moved on the first turn N runs                          |
| `loom.knowledge_source_holds`      | `(org, namespace, source_uri, source_id)` | `held`, `item`, `version`                                                                                                          |
| `loom.knowledge_document_access`   | `(org, namespace, source_uri)`            | `emails`, `groups`, `group_members`, `domains`, `anyone`, `unresolved`, `read_at`, `source_id`, `version`                          |
| `loom.connected_accounts`          | `(org, subject, provider)`                | `account_id`, `account_email`, `scopes`, `refresh_enc`, `active`, `connected_at`, `disconnected_at`, `version`                     |

`items_json` holds the items; `config_json` keeps today's form for a source N-1 can still
sync, so a source whose items are all of a type today's config can express is written in
both forms ([rolling upgrades](#rolling-upgrades)). The mirror's documents and chunks
live in `loom.documents` and `loom.chunks` under the mirror's namespace, so every
existing document machinery (heads under the Postgres authority, deliveries, originals,
the blob sweep) applies unchanged.

## Erasure and audit

- A namespace's erasure covers its mirror: the saga adds the mirror's name to its scope
  for every document, chunk, original and head step, and the census reads both.
- `loom.knowledge_source_holds`, `loom.knowledge_source_state` and
  `loom.knowledge_document_access` are keyed by namespace and erased with it (the access
  table holds emails: a person identifier). A user erasure deletes that person's
  `loom.connected_accounts` rows and removes their email from `emails` and
  `group_members` of every access row in the org, and stops their connected sources; the
  documents those sources wrote are the sync worker's (`user_id` `-`), as today, and go
  with the namespace's erasure or `knowledge.delete`.
- Every census entry is classified in `server/erasure/store/census.py`.
- Audit rows: `add`, `update`, `rotate`, `remove`, `sync_now` as today, plus `update`'s
  item changes; `connect` and `disconnect` (subject, provider, scopes, never a token);
  a `crawl` source's subjects and resolved members; a SCIM-driven stop names the
  sources it stopped.

## Rolling upgrades

- **The mirror.** N-1 knows no mirror and no `acl-` rule: its gate would admit an org
  admin's wildcard token to an `acl-` namespace by name. N creates no mirrored source
  until no N-1 replica serves: `add` and `update` refuse `access: mirrored` (`MIXED_VERSION`) while a
  live row of `loom.instances` reports a schema version below the migration that adds
  the mirror, as collections wait
  ([rule 19](rolling-upgrades.md)). N-1's reads of the source's namespace show no
  mirrored document.
- **Items.** N-1 syncs a source from `config_json`. A source whose items N-1 cannot
  express (a file, a shared drive, a filter, a recursive folder, `meet_recordings`, more
  than 20 items, a `connected` or `crawl` credential) is written with a `config_json`
  that N-1's parser refuses, so an N-1 sync turn ends `failed` for it and retires nothing
  until N's turn; the implementing pull request pins that with a test.
- **Holds.** N-1 does not write holds and still skips an address another source holds,
  which N reads as no hold of that source; N's next turn writes it.
- **State.** N reads the state from `loom.knowledge_source_state`, else from
  `loom.code_index_state`, and writes the new table; N-1 keeps writing the old row, which
  N reads when it is newer.
- **`meet`.** N-1 still syncs a stored `meet` source on its turns; N skips it.
- **Erasure.** N-1's erasure misses the mirror and the new tables ([rule 14](rolling-upgrades.md)):
  run again with N every erasure completed while an N-1 replica served.

Each implementing pull request adds its rule to [rolling upgrades](rolling-upgrades.md).

## Breaks

- `NOT_A_CODE_NAMESPACE` is no longer answered by the source ops.
- `recording` sources lose the `meet` provider.
- A second source reaching a held file no longer counts it `skipped`.
- Sync state leaves `loom.code_index_state`.

## Settings

| Setting                                    | Default | Meaning                                                                        |
| ------------------------------------------ | ------- | ------------------------------------------------------------------------------ |
| `LOOM_KNOWLEDGE_SOURCE_ACL_STALE_SECONDS`  | 3600    | a mirrored document's permission list older than this shows it to nobody       |
| `LOOM_CONNECT_GOOGLE_CLIENT_ID`, `_SECRET` | unset   | the OAuth client of the shared-folder store; unset, Google connections are off |
| `LOOM_CONNECT_NOTION_CLIENT_ID`, `_SECRET` | unset   | the page store's public integration                                            |
| `LOOM_CONNECT_LINEAR_CLIENT_ID`, `_SECRET` | unset   | the project tracker's OAuth application                                        |

The stale bound must exceed the idle sync interval; boot refuses one that does not. The
redirect URI to register with each store is `<LOOM_PUBLIC_BASE_URL>/v1/connect/<provider>/callback`.

## Rejected

- **A permission column on `loom.documents`, filtered by every read.** The default of a
  missed path would be to show the document to every reader.
- **A namespace per permission list.** A list changes with every share, which would move
  the document, its chunks and embeddings between namespaces on each change.
- **Resolving store groups through Loom's SCIM groups.** They hold a service's end users,
  not people's emails.
- **A separate meeting-store path for meetings.** The meeting store already saves
  transcripts and notes to the shared-folder store, with the participants' permissions;
  a second path would write each meeting twice.
- **`drive.file` with the picker.** It cannot read a folder's later files.

## Invariants

- A mirrored document is read only through a leg that applies the permission check; the
  mirror namespace admits no token but a platform admin's.
- A `connected` or `crawl` source writes to a namespace other than its owner's personal
  one only with `access: mirrored`.
- A stale, unresolvable or unknown permission admits nobody.
- No caller-supplied host or query string reaches a store.
- A connected account's sources stop when its owner disconnects, is deactivated, or
  loses `writer` on the namespace.

## Source entry points

- `server/knowledge/domain/sources.py`: kinds, configs, items.
- `server/knowledge/domain/source_links.py`: the link parser.
- `server/knowledge/app/sources.py`: the ops.
- `server/knowledge/app/source_sync.py` and its kind modules: the sync.
- `server/knowledge/app/source_access.py`: the permission check and the mirror legs.
- `server/auth/connected_accounts.py`: the connect routes and the store.
- `server/clients/gdrive.py`, `notion.py`, `linear.py`, `recording.py`: the stores.

## Tests

- Unit: the link parser on every link form; config and item validation; the query
  compiler escapes every field; the access check on fresh, stale, group, domain, anyone
  and unknown permissions.
- Integration: `tests/integration/test_document_source_leakage.py`, beside
  `test_cross_principal_leakage.py`, seeds mirrored documents with canary text and reads
  every knowledge read op as a namespace viewer not on the list, as one whose permission
  was removed, as a group member after removal, after the stale bound, as a service
  account, as another namespace and as another org; any canary fails.
- Integration: a `connected` source refused `access: namespace` outside the owner's
  personal namespace; a SCIM deactivation stops the person's sources.
- Integration: two sources holding one address; removing an item retires only what no
  other item reaches.

## Related

- [Knowledge: document sources](../features/knowledge.md#document-sources)
- [Document extraction](document-extraction.md)
- [Security](security.md)
- [Sharing knowledge](sharing.md)
- [Identities and namespaces](../concepts/identities-and-namespaces.md)
- [Rolling upgrades](rolling-upgrades.md)
