# Tracker record contract

The `tracker.*` ops, and the tracker legs of `code.*` and `loom_search`, read a team's
issues, pull requests and incidents live from the team's own ClickHouse with a read-only
credential. Loom does not copy the record. Every statement is written against one logical
model, the **contract**: the tables and columns below.

A team connects its schema in one of two ways:

| Mode    | How                                                                                                                                        |
| ------- | ------------------------------------------------------------------------------------------------------------------------------------------ |
| Views   | The team creates views with the contract's table and column names. Loom reads them as they are.                                            |
| Mapping | Loom lists the tables the credential can read and proposes a mapping. The confirmed mapping is saved as data; nothing is created remotely. |

A mapping is only a projection in front of the contract, so the same statements, tests and
privacy predicates run for every source. The contract as code is
`server/tracker/domain/contract.py` (`REQUIRED`, `OPTIONAL`, `FEATURES`, `ALIASES`), the
mapping is `server/tracker/domain/mapping.py`, and the columns read per table are
`COLUMNS_READ` in `server/tracker/domain/columns.py`.

## Adding a tracker source

1. **Create a read-only ClickHouse user for Loom** with `readonly = 2`. `readonly = 1`
   refuses the per-query settings Loom sends with every read, so no read would run
   (`CREDENTIAL_READONLY_MODE`).

   ```sql
   CREATE USER loom_ro IDENTIFIED BY '…' SETTINGS readonly = 2;
   GRANT SELECT ON system.columns TO loom_ro;
   -- views mode: the views only
   GRANT SELECT ON loom_tracker.* TO loom_ro;
   -- mapping mode instead: each mapped table
   GRANT SELECT ON tracker_db.tickets TO loom_ro;
   ```

2. **Views mode only: create the views.** A database (say `loom_tracker`) with
   `github_issues`, `github_prs` and `edges`, plus any optional tables you have data for.
   Create them `DEFINER = CURRENT_USER SQL SECURITY DEFINER`, so `loom_ro` needs `SELECT`
   on the views and nothing on the tables behind them. A plain view reads with the
   reader's own rights, and every read would fail with `ACCESS_DENIED`; the registration
   check catches this and names the fix. A source outside ClickHouse can sit behind a
   `PostgreSQL`, `MySQL` or `S3` engine table. `tracker.contract` returns every table and
   column with what it holds and a blank view template per table, including a
   `loom_uuid5(name)` SQL function for the item `id`.

3. **Register the source.** Any signed-in member of the org can, from the dashboard's
   _Add a tracker_ flow ([tracker overview](overview.md#adding-a-tracker-from-the-dashboard))
   or with the ops below; the registrant becomes the first `admin` of the tracker
   namespaces. Before anything is written Loom checks that the host resolves to a public
   address (`HOST_REFUSED`), that the credential is read-only (`CREDENTIAL_NOT_READONLY`,
   `CREDENTIAL_READONLY_MODE`), that it can connect (`SOURCE_UNREACHABLE`) and that the
   schema or mapping meets the contract (`CONTRACT_FAILED`). Read-only means every access
   type `SHOW GRANTS` and `SHOW GRANTS FINAL` list, the granted roles' types included,
   is `SELECT`, a `SHOW` of databases, tables, columns or dictionaries, `dictGet` or
   `CREATE TEMPORARY TABLE`, and nothing is granted `WITH GRANT OPTION` or
   `WITH ADMIN OPTION`, on a nested role either; any other type is refused. Every role
   granted to the user must be a default role, so the check sees its whole chain. A
   server that does not accept `SHOW GRANTS FINAL` is refused (`SOURCE_UNREACHABLE`). With
   `dry_run: true` a failing schema comes back as the report instead, with a
   `CREATE OR REPLACE VIEW` draft per failing table.

   | Op                           | Does                                                                                                                                                                   |
   | ---------------------------- | ---------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
   | `tracker.contract`           | The contract and view templates, before any connection.                                                                                                                |
   | `tracker.source.inspect`     | Connects and lists every readable table with its columns, plus a proposed mapping. Writes nothing.                                                                     |
   | `tracker.source.add`         | `name`, `host`, `port` (8443), `secure` (true), `database`, `user`, `password`, `namespaces`, optional `mapping`; `dry_run: true` runs every check and writes nothing. |
   | `tracker.source.set_mapping` | Replace a registered source's mapping (`dry_run` too); `mapping: null` returns it to views mode.                                                                       |
   | `tracker.source.check`       | Re-run the contract check now.                                                                                                                                         |
   | `tracker.source.rotate`      | Replace the credential.                                                                                                                                                |
   | `tracker.source.remove`      | Retire it and its namespaces (`confirm`).                                                                                                                              |
   | `tracker.source.list`        | Every source of the org: contract verdict, breaker state, namespaces and the caller's standing in each. No credential is ever returned.                                |

   Each entry of `namespaces` is `{namespace, repos, restricted, join_policy}`: `repos` are
   the `Org/repo` prefixes of `source_id` the namespace may read (a repository not listed is
   never read); `restricted` makes it a private record; `join_policy` is `open` (default) or
   `approval` for a standard namespace. Self-service registration needs `LOOM_TRACKER_ORG`
   and is on by default (`LOOM_TRACKER_SELF_SERVICE_ENABLED`, at most
   `LOOM_TRACKER_SELF_SERVICE_MAX_SOURCES` per org, default 10;
   `LOOM_TRACKER_SELF_SERVICE_ALLOW_PRIVATE_HOSTS=true` for a local development
   ClickHouse). The password is stored encrypted and never returned. `inspect` by
   name needs `admin` on one of the source's namespaces; `set_mapping`, `rotate` and
   `remove` need it on every one, since each changes what every namespace of the source
   reads. A standard namespace is decided by the gate at `admin`, and a restricted one
   by the restricted gate's rules except that the token need not name it; registering
   the source is not enough once the registrant's grant is revoked
   ([restricted namespaces](../../architecture/security.md#restricted-namespaces)). A
   dashboard session of anyone but a platform admin is a `writer` token, so it cannot
   change or retire a source with a private (restricted) namespace: that takes a token
   with the `admin` role, which an operator mints with `loom mint-token --roles admin`.
   The page shows those buttons disabled with the reason; the server decides.

   An operator can configure the same source instead ([configuration](#configuration)); a
   configured entry wins over a registered one of the same name and is changed only in
   configuration (`SOURCE_FROM_CONFIG`).

4. **Loom creates the namespaces' directory rows**: `kind = tracker`, private,
   `classification = restricted` for a restricted namespace, `join_policy` `open` (or
   `approval`) for a standard one and `approval` for a restricted one.

5. **Readers join.** `namespace.join` with `role: "viewer"` on an open tracker namespace
   admits at once, service accounts included. A restricted one waits for its admin, and the
   reader's token must name the namespace
   ([identities and namespaces](../../concepts/identities-and-namespaces.md)).

6. **Link the tracker to a repository** so agents working in that checkout read it:
   `code.repo.set_source` with `tracker_namespaces` (repository `admin`), the repository
   card in the dashboard, or, for the operator's own repositories only,
   `LOOM_TRACKER_CODE_NAMESPACE_BINDINGS_JSON`. A repository may link several trackers in
   order; the first is the primary, whose numbers are the repository's issue and PR numbers
   ([linking a tracker](overview.md#linking-a-tracker-to-a-repository)).
   `tracker.binding` reports what a code namespace resolves to for the caller.

## Configuration

The operator's form of the same entry:

```bash
LOOM_TRACKER_ORG=acme
LOOM_TRACKER_SOURCES_JSON='{
  "sources": {
    "widgets": {"host": "abc.clickhouse.cloud", "port": 8443, "secure": true, "database": "loom_tracker"}
  },
  "namespaces": {
    "tracker-widgets":         {"source": "widgets", "repos": ["acme/widgets"]},
    "tracker-widgets-private": {"source": "widgets", "repos": ["acme/widgets-private"], "restricted": true}
  }
}'
LOOM_TRACKER_CREDENTIALS_JSON='{"widgets": {"user": "loom_ro", "password": "…"}}'
LOOM_TRACKER_CODE_NAMESPACE_BINDINGS_JSON='{"code-widgets*": ["tracker-widgets", "tracker-helpdesk"]}'
```

Credentials sit in their own variable, apart from the map, so the map can be shown and
diffed without the secret. A binding value is one tracker namespace or an ordered list,
primary first; keys are globs over the code namespace names of the operator's
repositories (a self-service repository is linked by its maintainer). A maintainer's
`code.repo.set_source` links replace the map for that repository. Tracker namespaces
belong to `LOOM_TRACKER_ORG`; with it empty no tracker namespace serves.

### Source entry options

A source entry may also carry:

- `"mapping"`: the saved mapping ([mapping mode](#mapping-mode)); absent means views mode.
- `"checks": {"from": "<table or table function>", "repo": "Org/repo"}`: where the
  tracker's CI results live, read by `tracker.flaky` and `tracker.test_signal`.
  Resolution: this entry, else the source's own `checks` table, else
  `LOOM_TRACKER_CHECKS_FROM` / `LOOM_TRACKER_CHECKS_REPO` (both empty by default: no CI
  checks). Name a table function rather than a view over it: a view's filter runs on the
  remote side after the scan. With no `repo`, a namespace with a single repository uses
  that one.
- `"bisect": {"code_prefixes": ["src/"]}`: which paths count as source code for bisect;
  absent means any path not under docs, tests, CI, examples or benchmarks. A repository
  with no tracker still bisects over Loom's own PR history (the answer then says
  `candidates_from: "code_history"`).
- `"columns": {"<table>": {"<contract column>": "<your column>"}}`: contract columns your
  tables hold under another name, for example
  `{"github_issues": {"triage_bisect_outcome": "bisect_result"}}`. The live column is
  checked like the one it serves; only plain identifiers are accepted.

Rules:

- A namespace naming an unknown source does not serve (one error line at boot, never a
  guess).
- Each source has its own connection, circuit breaker and time budget, so one team's slow
  service never trips another's breaker.
- Registered sources (`loom.qa_sources`) are re-read every
  `LOOM_TRACKER_SOURCES_REFRESH_SECONDS` (60); a write in the same process refreshes at
  once.

Every tracker setting is in the [configuration reference](../../reference/configuration.md#tracker).

## Public and private

A tracker namespace is either **standard** or **restricted**, enforced by Loom statement
by statement:

- A standard namespace's every statement carries `is_private = 0`, and the tables that
  hold only private data (`core_incidents`, `support_cases`, `slack_threads`) are refused
  before any read.
- A restricted namespace lifts that predicate, reads only its own `repos`, and is gated
  by the restricted-namespace rules: a token that names it and a direct grant.

Three layouts work:

- one view set with an `is_private` column, read by both namespaces (a row policy on
  `is_private` for the read-only user adds a check on the ClickHouse side);
- two view sets with two users, one holding only public rows, registered as two sources;
- a restricted namespace alone.

## The contract

Types are compared by **family**: `String`, `LowCardinality(String)`, `Nullable(String)`,
`FixedString` and `Enum` are one; `DateTime`, `DateTime64` and `Date` are one; the `UInt*`
and `Int*` types and `Bool` are one; `Array(x)` compares on `x`. A view over another store
may differ in codec and nullability, never in shape. The view templates write each family
as its widest type (`UInt64`, `DateTime64(3)`, …).

### Required tables

A source without these does not serve.

**`github_issues`**: one row per issue.

| Column                                                                                       | Family        | Meaning                                                                                                                 |
| -------------------------------------------------------------------------------------------- | ------------- | ----------------------------------------------------------------------------------------------------------------------- |
| `id`                                                                                         | UUID          | `uuid5(NAMESPACE_DNS, 'github_issue:' \|\| source_id)` (`github_issue_private:` for private rows); the key `edges` uses |
| `source_id`                                                                                  | String        | `Org/repo#number`; the repository must be one the tracker namespace lists                                               |
| `number`                                                                                     | UInt          | the issue number                                                                                                        |
| `url`, `title`, `body`, `author`                                                             | String        | a link a person can open; the title every search reads; the text the duplicate check reads; who opened it               |
| `labels`, `assignees`                                                                        | Array(String) |                                                                                                                         |
| `primary_component`, `severity_level`                                                        | String        | the team's taxonomy; `''` when none                                                                                     |
| `state_reason`                                                                               | String        | `completed`, `not_planned`, `duplicate` or `''`                                                                         |
| `affected_versions`, `fix_versions`                                                          | Array(String) | versions the issue affects; versions that carry the fix                                                                 |
| `is_flaky_test`, `is_crash_ci`, `is_performance`                                             | UInt          | 0/1 flags; `0` when unknown                                                                                             |
| `has_test`                                                                                   | UInt          | a linked test exists; NULL when unknown                                                                                 |
| `error_code`                                                                                 | UInt          | the server error code the report names; `0` when none                                                                   |
| `is_private`                                                                                 | UInt          | `1` for a private-repository row                                                                                        |
| `created_at`, `updated_at`, `closed_at`                                                      | DateTime      | `closed_at` is epoch 0 while open                                                                                       |
| `triage_bisect_outcome`, `triage_introduced_commit`, `triage_component`, `triage_confidence` | String        | a triage bot's verdict; served as `''` when the tracker has none                                                        |
| `triage_reproduces_on`, `triage_clean_on`                                                    | Array(String) | likewise, served as `[]`                                                                                                |

**`github_prs`**: one row per pull request.

| Column                                                                                       | Family        | Meaning                                                                    |
| -------------------------------------------------------------------------------------------- | ------------- | -------------------------------------------------------------------------- |
| `id`                                                                                         | UUID          | `uuid5(NAMESPACE_DNS, 'github_pr:' \|\| source_id)` (`github_pr_private:`) |
| `source_id`, `number`, `url`, `title`, `body`, `author`                                      | as above      |                                                                            |
| `pr_number`                                                                                  | UInt          | equal to `number`                                                          |
| `primary_component`, `severity_level`, `is_private`, `created_at`, `updated_at`, `closed_at` | as above      |                                                                            |
| `state`                                                                                      | String        | `open`, `closed` or `merged`                                               |
| `is_merged`, `has_test`, `review_requests_test`                                              | UInt          |                                                                            |
| `test_types`, `reviewers`, `src_components`                                                  | Array(String) |                                                                            |
| `ci_result`, `changelog_category`, `minor_version`, `base_branch`                            | String        |                                                                            |
| `is_backport_pr`, `is_cherrypick_pr`, `is_sync_pr`                                           | UInt          | derivative PRs, left out of activity reads                                 |
| `upstream_pr_number`                                                                         | UInt          | the PR a backport repeats; `0` when none                                   |
| `additions`, `deletions`, `time_to_merge_days`                                               | UInt          |                                                                            |

**`edges`**: links between items.

| Column                 | Family   | Meaning                                                                                                   |
| ---------------------- | -------- | --------------------------------------------------------------------------------------------------------- |
| `from_id`, `to_id`     | UUID     | item ids as above                                                                                         |
| `from_type`, `to_type` | String   | `github_issue`, `github_pr`, `github_issue_private`, `github_pr_private`, `core_incident`, `support_case` |
| `rel_type`             | String   | `fixes`, `related_to`, `duplicate_of`, `caused_by`, `backport_of`, `reverts`, …                           |
| `created_at`           | DateTime |                                                                                                           |

Loom derives an item's `id` locally and joins `edges` on it, so a view must produce exactly
that value. ClickHouse has no `uuid5()`; the `loom_uuid5` function `tracker.contract`
returns is the RFC 4122 version-5 construction in SQL, equal to Python's `uuid.uuid5`.

### Optional tables

An absent table makes the ops that need it answer `not_provided`; a present table missing
one of its minimum columns (`OPTIONAL` in `contract.py`) is reported `incomplete` and
treated as absent.

| Table                                        | Minimum columns                                                                                                                                               |
| -------------------------------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `core_incidents`                             | `id`, `source_id`, `title`, `body`, `primary_component`, `severity_level`, `stack_functions Array(String)` (stack-frame matching), `error_code`, `created_at` |
| `support_cases`                              | `id`, `source_id`, `title`                                                                                                                                    |
| `slack_threads`                              | `source_id`, `title`, `body`                                                                                                                                  |
| `component_risk_snapshot`                    | `computed_at`, `component`                                                                                                                                    |
| `release_items`                              | `release_version`, `issue_number`                                                                                                                             |
| `release_health_snapshot`                    | `computed_at`, `release_version`                                                                                                                              |
| `version_history`, `version_history_private` | `commit_sha`, `version`                                                                                                                                       |
| `checks`                                     | `check_name`, `test_name`                                                                                                                                     |
| `cloud_instances`                            | `instance_id`                                                                                                                                                 |
| `rel_instance_mentions`                      | `from_id`, `instance_id`                                                                                                                                      |
| `slack_incidents`                            | `incident_id`                                                                                                                                                 |

`core_incidents`, `support_cases` and `slack_threads` are read only through a restricted
namespace.

### Feature columns

Optional columns on required tables, reported present or absent. A feature column the
source lacks reads its alias, or else a default (see [Aliases](#aliases)): the statements
that read it answer as for a row without it, and the feature stays absent.

| Feature      | Columns                                             | Unlocks                                                                   |
| ------------ | --------------------------------------------------- | ------------------------------------------------------------------------- |
| `bisect`     | `github_prs.diff`, `github_prs.affected_file_paths` | bisect candidates and culprit scans                                       |
| `merge_time` | `github_prs.merged_at`                              | merge-time facts                                                          |
| `vector`     | `github_issues.embedding`, `github_prs.embedding`   | reported only; `tracker.similar` uses the embeddings Loom computes itself |

### Aliases

A source that lacks a contract column still meets the contract when `ALIASES` in
`contract.py` names an accepted expression for it over columns the source does have. The
check verifies the alias's own columns by family and reports it as `aliased`; statements
keep reading the contract's name, because the table reference is replaced by a projection
that adds the alias (the query analyzer prunes it to the columns a statement reads). An
alias with no columns of its own is a **default** (`defaulted`): every row reads `''`, `0`,
`[]` or `NULL`, and the report says so, so a default is never mistaken for data.

| Table                                            | Column                                                                                       | Accepted forms, in order                                                   |
| ------------------------------------------------ | -------------------------------------------------------------------------------------------- | -------------------------------------------------------------------------- |
| `github_issues`                                  | `severity_level`                                                                             | `severity`                                                                 |
| `github_issues`                                  | `has_test`                                                                                   | `toUInt8(metadata.has_test.:Bool)` (a JSON `metadata` column), else `NULL` |
| `github_issues`                                  | `triage_bisect_outcome`, `triage_introduced_commit`, `triage_component`, `triage_confidence` | `''`                                                                       |
| `github_issues`                                  | `triage_reproduces_on`, `triage_clean_on`                                                    | `[]`                                                                       |
| `github_prs`                                     | `has_test`                                                                                   | `toUInt8(metadata.has_test.:Bool)`, else `toUInt8(tests_added > 0)`        |
| `github_prs`                                     | `upstream_pr_number`                                                                         | `ifNull(toUInt32(metadata.original_pr_number.:Int64), 0)`, else `0`        |
| `github_prs`                                     | `review_requests_test`                                                                       | `NULL`                                                                     |
| `github_prs`                                     | `test_types`, `src_components`                                                               | `[]`                                                                       |
| `github_prs`                                     | `merged_at`                                                                                  | `if(is_merged = 1, closed_at, toDateTime64(0, 3))`                         |
| `github_prs`, `core_incidents`                   | `severity_level`                                                                             | `''`                                                                       |
| `qa_bugs` (the answer route's cross-source view) | `severity_level`, `has_test`                                                                 | `severity`; `toUInt8(metadata.has_test.:Bool)`, else `NULL`                |

A rename the contract does not list is still a missing column. Declare it in the source
entry's `columns` map ([source entry options](#source-entry-options)), or add an `Alias`
in `contract.py` to accept another spelling for every source.

## Mapping mode

A mapping says, for each logical table, which of the team's tables it reads and, for each
logical column, an expression over that table's columns:

```json
{
  "version": 1,
  "tables": {
    "github_issues": {
      "source": "tracker_db.tickets",
      "final": true,
      "columns": {
        "source_id": { "concat": [{ "lit": "acme/widgets#" }, "ticket_no"] },
        "number": "ticket_no",
        "title": "summary",
        "body": "description",
        "created_at": "opened_at",
        "state_reason": { "lower": "resolution" }
      }
    }
  }
}
```

- **Expressions are data, never SQL**: a column name the live table has (or
  `{"col": …}`), `{"lit": …}`, or one of `concat`, `coalesce`, `eq`, `in`, `has`, `cast`
  (`[expr, family]`), `lower`, `digits` (the trailing number of a key: `PROJ-1234` →
  `1234`). Loom renders them with identifiers back-quoted and literals quoted, converts
  each to the family the statements read, and runs them with the read-only credential.
- `source` is `db.table`, or `table` in the tracker's database. `final: true` reads the
  table with `FINAL` (proposed for Replacing, Collapsing, Aggregating and Summing
  engines).
- Loom derives each item's `id` from the mapped `source_id`, and an edge's `from_id` /
  `to_id` from the mapped `from_ref` / `to_ref` (`Org/repo#number`). They are never mapped.
- Each mapped table needs its core columns: `github_issues` `source_id`, `number`,
  `title`, `created_at`; `github_prs` `source_id`, `pr_number`, `title`, `created_at`;
  `edges` `from_ref`, `to_ref`, `from_type`, `to_type`, `rel_type`; `core_incidents`
  `source_id`, `title`, `created_at`. A mapping must map `github_issues`. Unmapped columns
  take a default or are derived (a PR's `state` from its merged and closed fields), and
  the report lists them; a logical table the mapping does not name reads as an empty
  table.
- The report's capabilities (`items`, `duplicate_check`, `duplicate_calibration`,
  `pull_requests`, `links`, `severity_board`, `bisect`, `incidents`, `ci_checks`) say what
  the mapped data lets Loom answer.

At read time each logical table reference is replaced by the mapped projection, so the
same statements, tests and privacy predicates run for every source.

## Checking a source

```bash
loom tracker-contract-check                           # every configured source
loom tracker-contract-check --source widgets          # one source
loom tracker-contract-check --namespace tracker-widgets
loom tracker-contract-check --json
```

The server runs the same check on the first tracker read of each process: it reads the
schema through the source's own credential (`system.columns`, or `DESCRIBE` per table when
that is hidden) and caches the verdict for `LOOM_TRACKER_CONTRACT_TTL_S` (3,600 s), a
failed one for 60 s so a fixed view is picked up within a minute. A source that fails
answers every read `tracker_unavailable` naming what is missing, never a partial answer.
`tracker.source.check` runs the check on demand.

## Related

- [Tracker overview](overview.md): what the ops answer, linking, the dashboard flow
- [History and PRs](../code-index/history-and-prs.md): the code-plane ops that read a tracker
- [Configuration reference](../../reference/configuration.md#tracker)
- [Operations reference](../../reference/operations.md#tracker)
