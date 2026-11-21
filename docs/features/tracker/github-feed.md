# Maintain a GitHub tracker record

`loom tracker-feed` copies repository issues, pull requests, issue comments and inline
review comments into a team's dedicated ClickHouse database. Loom reads the resulting
views through a [read-only tracker source](record-contract.md). The team owns the copied
content, backups and erasure. The job makes no model or embedding calls.

## Prepare the record

Use an empty database with a stable database UUID, separate from the Loom deployment
database. Give a dedicated writer permission to create tables and views and to read and
insert rows in that database. Keep its credentials separate from the tracker reader.

```sql
CREATE DATABASE team_feed;
CREATE USER feed_writer IDENTIFIED BY '…';
GRANT CREATE TABLE, CREATE VIEW, SELECT, INSERT ON team_feed.* TO feed_writer;
GRANT SELECT ON system.columns TO feed_writer;
GRANT SELECT ON system.databases TO feed_writer;
GRANT SELECT ON system.tables TO feed_writer;
CREATE USER feed_reader IDENTIFIED BY '…' SETTINGS readonly = 2;
GRANT SELECT ON system.columns TO feed_reader;
```

Supply the writer password as `LOOM_TRACKER_FEED_WRITER_PASSWORD` through the scheduler's
secret facility. Run the CLI with the same Loom deployment configuration used by the
serving process, including its ClickHouse connection and secret root.
Set `LOOM_TRACKER_ORG` to the organization being initialized. The CLI verifies direct
writer grants before use and refuses roles, grant options and authority outside this
database, apart from those three system metadata tables.

```bash
loom tracker-feed init --org acme --host record.example --database team_feed \
  --writer-user feed_writer
```

Initialization creates `github_issues`, `github_prs` and `edges` as definer views.
Grant the reader access only to those views:

```sql
GRANT SELECT ON team_feed.github_issues TO feed_reader;
GRANT SELECT ON team_feed.github_prs TO feed_reader;
GRANT SELECT ON team_feed.edges TO feed_reader;
```

[Register the tracker source](record-contract.md#adding-a-tracker-source) in views mode
using `feed_reader`. Include each repository in the source's namespace allowlist and
use restricted namespaces for private records. Set the deployment's `LOOM_TRACKER_ORG`
to the same organization. Source registration keeps its existing host, credential and
contract checks. HTTP and private addresses require the development private-host
setting; production connections use HTTPS.

## Configure and schedule a repository

Create a read-only fine-grained token or App installation token restricted to the
repositories being copied. It needs Metadata, Issues and Pull requests read permissions.
`--read-only-token` is the operator's permission attestation; the CLI cannot certify the
absence of write grants from a successful request or token prefix.

Supply that token as `LOOM_TRACKER_FEED_GITHUB_TOKEN` through the secret facility, then
configure one allowlisted repository:

```bash
loom tracker-feed configure --org acme --source widgets --repo acme/widgets \
  --writer-user feed_writer --read-only-token --max-pages 5
loom tracker-feed sync --org acme --source widgets --repo acme/widgets \
  --writer-user feed_writer
```

Configuration seals the token in the team's feed database. Sync needs the writer
password and deployment secret root; it does not need the token environment variable.
Copied repository identities keep the spelling in the registered source's allowlist,
so namespace filters match even if API capitalization differs.
App installation tokens [expire after one hour](https://docs.github.com/en/apps/creating-github-apps/authenticating-with-a-github-app/authenticating-as-a-github-app-installation).
An external secret process must refresh the installation token and rerun `configure`
before expiry. The feed does not issue or refresh installation tokens.
Use `--writer-password-env` or `--token-env` to select another credential variable.
During a root-secret rotation, successful metadata writes reseal credentials opened by
`LOOM_JWT_SECRET_PREVIOUS`. Complete a successful turn before retiring the previous root.

Schedule `sync` every five minutes or more frequently. Visibility observations expire
after 15 minutes, hiding the repository if checks stop succeeding. Each job has a
120-second limit and reads at most `--max-pages` pages per stream, with 100 items per
page and a budget of 2 to 100 pages. The final page's timestamp boundary is replayed;
the minimum leaves a preceding page from which to make progress.
A capacity error leaves the cursor unchanged; raise
the bounded page budget or shorten the repository's backlog before retrying. Counts
on standard output report processed rows, including replayed rows.

## Visibility and erasure

Every stream verifies the repository's immutable identity and current visibility.
Failed checks hide its views. Each item and comment keeps the observation's privacy
classification with its content. The views use the most restrictive classification
from the repository, item and stored comments, including when those tables reach a
replica at different times. This is a polling observation with a bounded expiry,
rather than an instant permission mirror. Normal tracker privacy and direct grant
checks still apply.

When a repository becomes public, configuration or sync resets its cursors and
scheduled turns replay all streams within the same page budget. An item becomes public
after its row and stored comments have been refreshed under public observations.
Deleted upstream rows require erasure of the retained copies; replay cannot refresh them.

Bodies include up to 32,768 item characters and the 64 most recently updated comments,
with up to 2,048 characters per comment. Review requests and test evidence remain
unknown when the source does not provide that evidence.
The views' `updated_at` tracks item and comment publication in the record, so a comment
imported after its item remains visible to incremental tracker reads.

Incremental lists do not report deletions. To erase copied content, stop every scheduled
writer, erase the affected raw rows, comments and repository configuration or cursors,
and exclude the repository before restarting. Dropping a Loom namespace removes Loom's
derived data; it does not erase the team's database or backups. Initialization binds a
record to one deployment and organization. Use a separate record for another deployment.

See the [feed architecture](../../architecture/github-feed.md) for cursor boundaries,
concurrency and ownership.
