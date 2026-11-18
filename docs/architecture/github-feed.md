# GitHub tracker feed

The GitHub feed maintains a team's tracker record in the team's ClickHouse database.
Loom's serving processes read that record through the existing
[read-only tracker source](../features/tracker/record-contract.md). The feed does not
put issue bodies, comments or pull requests in Loom's deployment tables.

## Ownership and credentials

An operator runs the feed as a bounded CLI job, scheduled by the team's job runner.
Before reading the registered endpoint or opening the record, the CLI checks its
actual deployment's storage authority and whether its cell holds the feed organization.
The external connection repeats that deployment gate; it never treats the record's
metadata as deployment authority. The job closes both clients and the deployment's
Postgres pool on exit.
Initialization creates feed tables and contract views in an empty, dedicated database;
it refuses existing objects. The operator then registers that database as a tracker
source with a separate read-only credential. Configuration and sync require that
registered endpoint and its repository allowlist. A separately supplied writer
credential can write only the team's feed database; the reader credential is never
upgraded or reused for writes.

The source credential is a read-only fine-grained GitHub token or App installation
token restricted to the configured repositories: Metadata, Issues and Pull requests,
all read-only. Token permissions are operator-attested configuration: a successful
GET, token prefix or endpoint-permission header cannot certify absence of write grants.
An unidentified host token is not eligible for a read-only live probe. The token is
sealed with the existing secret-root mechanism in the team's
feed configuration table. No token appears in arguments, output or contract views.
The CLI receives the database writer password through a named environment variable;
it does not persist that password. GitHub requests use GET on the fixed
`https://api.github.com` origin, refuse redirects and construct paths from validated
repository names and numeric item ids. They never follow response URLs.

The team owns the copied record, its backup retention and erasure. To erase copied
content, the team stops its scheduled writer and removes the affected feed rows,
comments and checkpoints before restarting with an exclusion or removing the source
repository. Deleting a Loom namespace retires Loom's derived data and grants; it
does not delete the team's record. This is the same boundary as any registered tracker
database. The record-writer credential cannot address Loom's own database; copied
content is never written there. Only the existing deployment-store lease is local.

## Record and progress

Raw items and comments are versioned in the team's database with the identity tuple,
process actor and imported source class. Contract views expose the latest item version,
contract UUIDs and effective visibility. Issue and pull-request comments are
kept separately and included in the corresponding item's searchable body. Unsupported
triage fields use the contract's typed defaults; edges are empty until evidence exists.
Contract `updated_at` is the latest persisted publication timestamp across the item
and its comments, so late imports and comment edits advance incremental tracker reads.
Publication versions use whole milliseconds to match tracker cursors. Under the record's
writer lease, each content batch receives the later of the record clock and one millisecond
beyond every persisted content version for its organization, across all repositories.
This ordering also covers namespaces that read multiple repositories through one cursor.
Repository control writes likewise advance beyond the persisted version for their exact
organization and repository, even when the receiving clock moves backward. Visibility
revocation, credential rotation and cursor checkpoints therefore replace the prior control
row rather than losing to its timestamp.
Raw upstream timestamps remain separate and drive API cursors. No model or embedding
call is required. Repository identities retain the registered allowlist's spelling,
including when the API returns different capitalization.

Owner, repository revision and publication watermark reads request
`select_sequential_consistency = 1`; the writer lease alone does not make a replica
current. Feed writers require SharedMergeTree storage or a single self-hosted server,
following the [read-your-writes guarantees](clickhouse.md#read-your-writes).
The setting alone does not cover acknowledged writes on self-hosted replicated tables.
Writer validation accepts only `ReplacingMergeTree` and `SharedReplacingMergeTree`
for the five feed tables in the connected record database. Replicated, distributed
and unrecognized table engines are refused before the job can publish.

Each repository has independent item, issue-comment and review-comment cursors. The
[issues endpoint](https://docs.github.com/en/rest/issues/issues#list-repository-issues)
lists issues and pull requests together; pull requests are hydrated through their
numeric endpoint. The
[issue comments endpoint](https://docs.github.com/en/rest/issues/comments#list-issue-comments-for-a-repository)
and [review comments endpoint](https://docs.github.com/en/rest/pulls/comments#list-review-comments-in-a-repository)
have separate updated-time scans. Each uses ascending `updated_at`, bounded pages,
response bytes and request time. Cursor advancement follows acknowledged writes only,
through a complete timestamp boundary. Requests lower the saved timestamp by one second
because `since` is exclusive. A full page budget saves the earliest timestamp on its
final page; the next turn replays that boundary. The budget is 2 to 100 pages: a preceding
page permits progress while the final page retains an incomplete timestamp boundary.
If one timestamp fills the entire configured page budget,
the job reports a capacity error and keeps the checkpoint rather than skipping rows or
claiming progress. The operator can raise the bounded page budget for that repository.
Replayed rows replace the same identity. The API is a changing collection, not a snapshot;
overlapping scans reduce pagination races without claiming transactional upstream reads.

Every turn verifies repository identity and visibility before copying content. An
unavailable, moved or mismatched repository first hides that repository's contract rows
and then refuses the turn. Contract views also require a recent successful visibility
observation, expiring after a bounded interval if the scheduler stops. Each item and
comment publication stores the observation's privacy classification in the same row as
its content. Views use the most restrictive classification from the repository, item
and stored comments for both `is_private` and the contract UUID. A replica that learns
new content before repository metadata therefore cannot label a private publication
public. The standard tracker's existing predicate excludes those rows. Restricted
namespaces retain their live direct-grant check. Visibility is a polling observation,
not an instant permission mirror.
A validated identity mismatch or private observation can hide the current record even
when another turn changed its revision; this preserves current credentials and cursors.
Generic failures from an older credential retain the revision check. Stale public
observations cannot widen access.

When a repository becomes public, configuration or sync resets its existing stream
cursors before publishing public metadata. Bounded scans replay the items and comments
and stamp each with the public observation. An item remains restricted until its own
row and its stored comments have been replayed. Repeated scheduled turns continue a
backlog under the same page and timestamp-boundary limits. Deleted upstream rows cannot
be replayed; the team must erase those retained copies to clear their private
classification.

Incremental API lists do not report deleted issues or comments. The team handles those
erasures explicitly; the feed does not claim deletion detection. Configuration, sealed
credentials and cursors stay with the team's record, so CLI restarts do not depend on
local files. Initialization binds the record to one Loom deployment's database UUID;
configuration and sync refuse any other deployment. Concurrent jobs in that deployment
use one publication lease keyed by the record database UUID, independent of source
aliases and repository renames. Provider fetches happen outside that lease; initialization,
configuration and publication share it so concurrent first-time identity observations
cannot bypass serialization. An uncertain database write retains its claim through the bounded
server send window. Record erasure requires the team to stop all writers first.

## Validation and rollout

Fakes cover pagination ties, partial responses, rate limits, retries, visibility changes,
redirects and credential redaction. A private ClickHouse verifies the contract, current
views, replay and interrupted cursor publication. A read-only token, when available,
checks a bounded public-repository slice; a write-capable CLI token is never substituted.
The feed adds no Loom wire operation or automatic worker. Existing deployments and
registered trackers are unchanged until an operator initializes and schedules a feed.

## Source entry points

- [CLI](../../server/cli/tracker_feed.py): `register_tracker_feed` and `_run` select the
  owned record and its separate writer credential; `_validate_writer` checks grants
  and supported table engines.
- [Feed application](../../server/tracker/app/github_feed.py): `initialize`, `configure`,
  `sync` and `_publish` enforce ownership, stamp publication privacy and fence writes.
- [Record store](../../server/tracker/store/github_feed.py): `statements` defines the
  contract views; `repository`, `put_repository` and `put_items` read publication
  state consistently.
- [API client](../../server/clients/github_feed.py): `pages` bounds upstream scans;
  [domain rules](../../server/tracker/domain/github_feed.py) validate identities and
  choose replay boundaries through `next_cursor`.

## Invariants

- Copied content stays in the team's registered record, with team-owned erasure.
- Repository identity, visibility and revision checks precede content publication;
  control reads include acknowledged record writes on the supported storage topology.
- Content and its privacy classification share one raw row version. Contract privacy
  and UUIDs include every stored component's classification, even when tables lag
  independently.
- A private-to-public transition resets cursors before public publication, so retries
  replay retained content without dropping its private classification prematurely.
- The record-wide writer lease covers publication and checkpoint writes. An uncertain
  send retains its claim until it settles; a cursor advances only after acknowledged writes.
- Whole-millisecond content versions increase across every repository in the record,
  so combined item/comment bodies remain visible to existing tracker cursors.
