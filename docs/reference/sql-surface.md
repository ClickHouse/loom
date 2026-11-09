# SQL surface

The SQL surface is a set of read-only ClickHouse views over a tenant's memories, memory
history, conversations and request ledger. An org's own SQL client, BI tool or
warehouse job reads them with a ClickHouse user that `loom sql-access` makes. That user
reads the views and nothing else, and each view shows only the orgs and namespaces the
user was granted.

## Version and compatibility

Each view's name carries its version: `sql_v1_memories` is version 1. Column names, types
and meanings are a public contract:

- A `v1` view may gain columns. It never loses or renames a column, and a column never
  changes its type or meaning.
- A change that would break that rule ships as a `sql_v2_*` view beside `v1`. Both serve
  for at least one release, and the release notes name the `v1` view that is going.
- Select columns by name: `SELECT *` picks up added columns.

## Views

Every view is in Loom's database (`loom` unless `LOOM_CH_DATABASE` names another) and
carries the identity columns `org` and `namespace`. `sql_v1_memories`, `sql_v1_memory_history`
and `sql_v1_agent_events` also carry `agent`, `user_id` and `session_id`;
`sql_v1_conversations` carries `agent` and `user_id`; `sql_v1_usage_daily` carries `agent`.
A join can match any part of the [identity tuple](../concepts/identities-and-namespaces.md)
that both sides carry.

### `sql_v1_memories`

One row per memory (identity and key): its current version. A deleted or erased memory is
absent, and so is a rejected one. Other statuses are listed with their `status`:
`memory.list` hides `duplicate`, `superseded` and `proposed` rows unless asked, so filter
on `status = 'active'` for what a default listing shows. A memory past its `expires_at` is
listed with its value until the expiry sweep acts on it, as `memory.list` lists it; add
`(expires_at IS NULL OR expires_at > now())` for what `memory.search` serves.

| Column                                                             | Meaning                                                                                            |
| ------------------------------------------------------------------ | -------------------------------------------------------------------------------------------------- |
| `org`, `namespace`, `agent`, `user_id`, `session_id`, `run_id`     | who the memory belongs to and the run that wrote it (`-` for none)                                 |
| `memory_key`, `memory_id`, `memory_type`                           | the key, the memory's id (one version with `version`), its type (`fact`, `decision`, ...)          |
| `value`                                                            | the value as `memory.get` serves it, or NULL when withheld                                         |
| `value_withheld`                                                   | `''` when `value` is served, `sensitive` for a `high` row, `redacted` for a redacted version       |
| `content_sha256`                                                   | the SHA-256 of the value, NULL whenever `value` is: a short secret's hash can be guessed           |
| `version`, `status`, `confidence`, `pinned`, `tags`                | as on `memory.get`                                                                                 |
| `sensitivity`, `source_class`, `actor`                             | the label, the provenance class, the token subject that wrote it                                   |
| `created_at`, `updated_at`, `expires_at`, `valid_at`, `invalid_at` | when it was written and last changed, when it expires, when the fact became and stopped being true |

### `sql_v1_memory_history`

One row per version of every memory, the version of a deletion included (`status =
'tombstoned'`, `tombstoned_at` set). A rejected version is absent. The columns are those of
`sql_v1_memories` without `expires_at`, `valid_at` and `invalid_at`, plus `tombstoned_at`.
`value` and `content_sha256` are NULL for a redacted version and for every version of a key
that was ever labelled `high`: the version before the sensitivity sweep labelled a row holds
the same value unlabelled.

### `sql_v1_conversations`

One row per stored conversation turn of each user: `org`, `namespace`, `agent`, `user_id`,
`thread_id`, `msg_id`, `role`, `content`, `tokens`, `run_id` and `ts` (the turn's latest
write). A turn deleted or compacted away (`tombstoned_at` set) is absent, and an erased one
is gone with its row. A turn carries no sensitivity label, so `content` is served as written,
as `conversation.get` serves a thread's turns; the cross-thread listings' check for text that
reads as `high` runs in Loom, not in SQL.

### `sql_v1_agent_events`

The request ledger: one row per op call or worker pass, `kind` `''` for a request.
`ts`, `org`, `namespace`, `agent`, `user_id`, `session_id`, `run_id`, `operation`, `kind`,
`request_id`, `trace_id`, `latency_ms`, `success`, `error_code`, `status_class`,
`memory_key`, `input_tokens`, `output_tokens`, `embedding_tokens`, `llm_calls`,
`upstream_cost_usd` (model spend in USD), `bytes_written`, `model`, `consumer`, `subject`
(the token subject that called) and `transport`. Request arguments are not in it. An erased
end user's rows read `user_id` `<erased>`.

It holds the 31 whole UTC days before today and today so far; each statement reads its own
`now()`. The window length and its alignment to UTC days are part of the `v1` contract.

### `sql_v1_usage_daily`

`sql_v1_agent_events` summed per UTC `day`, `org`, `namespace`, `agent`, `operation` and
`kind`, over the same window: `ops`, `errors`, `input_tokens`, `output_tokens`,
`embedding_tokens`, `llm_calls`, `upstream_cost_usd`, `bytes_written`, `latency_sum_ms` (with
`ops`, a mean across rows), and `latency_p50_ms` and `latency_p95_ms`, approximate and per
row: they do not combine across rows. Every `day` but today's is a whole day.

### What a read costs

A view runs the caller's filter on the rows it returns, so a filter on `ts`, `day`,
`thread_id` or `memory_key` narrows the answer, not what is read. Every read of
`sql_v1_memories`, `sql_v1_memory_history` and `sql_v1_conversations` reads the grant's whole
scope: the history sorts every version in it, and the conversations view groups every turn
in it, content included. These grow with what the scope stores, and a wide scope can exceed
the read caps below. Each read of a ledger view reads the grant's whole scope over the
window: at most from the start of the month the window opens in, usually close to the
window itself. A wider grant (`*` namespaces, `--all`) reads more. The SQL user's read caps
apply to that read (`LOOM_CH_ANALYSIS_MAX_ROWS_TO_READ`, `_MAX_EXECUTION_S`,
`_MAX_MEMORY_BYTES`, `_MAX_THREADS`), so a busy scope can exceed them: narrow the grant, or
raise those settings, which `insights.query` shares, and run `loom sql-access grant` again,
which writes them into the SQL users' profile. Events older than the window are not on the
surface; the [analytics export](../operations/analytics-export.md) writes daily usage totals,
under hashed names, to the operator's bucket. `loom.agent_events` itself has no TTL unless
`LOOM_RETENTION_AGENT_EVENTS_DAYS` sets one.

## What the views never show

The memory views apply the rules of Loom's own listings
([sensitivity](../architecture/security.md#sensitivity)); the conversation view serves a
turn as `conversation.get` does, above. None of them shows:

- the value of a `high` row and its hash, in the current view and in every version of its
  key in the history;
- a redacted version's value and its hash;
- a rejected memory, a deleted or erased memory, and a deleted conversation turn;
- any row of a namespace classified `restricted` in `loom.namespaces` or `loom.code_repos`,
  whatever the grant, from the moment it is classified: Loom audits each read of a
  [restricted namespace](../architecture/security.md#restricted-namespaces), which a view
  cannot;
- tables outside the five views: tokens, grants, secrets, the audit log, documents,
  embeddings and request arguments;
- the `system` tables that show other users' statements, such as `system.query_log`,
  `system.mutations` and `system.processes`. ClickHouse refuses them to a user without a
  grant only with `access_control_improvements.select_from_system_db_requires_grant` on (the
  default), so `loom sql-access grant` logs in as each user it sets a password for and
  drops it when `system.processes` answers. In Loom's database, `system.tables` lists
  only the five views.

The memory and conversation views read the tables the
[erasure saga](../features/memory/retention-and-erasure.md) deletes from and its residue
census counts, so an erased user's or namespace's rows leave them with the tables. The
ledger views keep their rows, which hold no content: a user erasure rewrites the user's
`user_id` to `<erased>`, as it does in `loom.agent_events`.

## Freshness

The views read ClickHouse at query time; nothing is copied. Postgres holds live memory
([storage authority](../architecture/storage-authority.md)), and `loom.memory` and
`loom.memory_versions` are projections published after each commit, in a flush every
`LOOM_PG_PUBLISH_INTERVAL_MS` (250 ms by default). A committed memory write is in
`sql_v1_memories` and `sql_v1_memory_history` once its flush lands, normally within about
one interval; a delivery that a stopped writer left is published by the delivery worker,
which can take longer. The views wait for nothing: unlike `memory.search`, which answers
`PROJECTION_BEHIND` past `LOOM_PG_PROJECTION_WAIT_MS`, a view serves what ClickHouse holds.
Conversations and the request ledger are written to ClickHouse directly.

## Access

A SQL user of the surface is a ClickHouse user with `SELECT` on the five views and no other
grant, on the settings profile `loom_sql_surface`:

- `readonly = 1`: a statement changes no setting but `final`, `join_use_nulls`,
  `group_by_use_nulls` and `transform_null_in`, which leave every view's rows as they are and
  which clients send on their own (`pg_clickhouse` among them). Any other setting could cut
  a view's filter short: an overflow mode, `additional_table_filters`, PREWHERE under
  `FINAL`.
- values the profile holds whatever the server's defaults are: `apply_deleted_mask = 1`
  and `apply_patch_parts = 1` (a removed row stays hidden), `use_query_cache = 0` and
  `use_query_condition_cache = 0` (both key on the statement's text, which every user
  shares), `use_skip_indexes_if_final_exact_mode = 1`,
  `do_not_merge_across_partitions_select_final = 0` and
  `optimize_move_to_prewhere_if_final = 0` (`FINAL` reads each key's latest version),
  every overflow mode `'throw'` (read and timeout with their `_leaf` forms, set,
  transfer, join, sort, distinct, group by and result),
  `send_logs_level = 'none'`, no introspection functions, and `query_plan_filter_push_down = 0`
  and `enable_optimize_predicate_expression = 0`, so a caller's filter runs on what a view
  returns and never on another tenant's rows (a filter that raises an error on a value
  would quote it);
- `insights.query`'s ceilings, except its result caps, since a SQL client reads whole
  result sets: `LOOM_CH_ANALYSIS_MAX_EXECUTION_S`, `_MAX_ROWS_TO_READ`, `_MAX_MEMORY_BYTES`
  and `_MAX_THREADS`, and `_MAX_CONCURRENT` statements at a time.

Each view runs with its definer's privileges (`SQL SECURITY DEFINER`, the user that ran
the migration) and admits only the rows whose `(org, namespace)` the table
`loom.sql_access` grants to `currentUser()`, the user that sent the statement. A user with
no granted row reads nothing, and so does every other ClickHouse user, Loom's own
included.

```bash
loom sql-access grant --user acme_bi --org acme --namespaces support,research
loom sql-access grant --user acme_bi --org acme --namespaces '*'   # every namespace of acme
loom sql-access grant --user ops_bi --all                          # the whole deployment
loom sql-access grant --user acme_bi --org acme --namespaces support --rotate
loom sql-access list
loom sql-access revoke --user acme_bi
```

- `grant` creates the user when there is none and prints its password once; Loom keeps no
  copy. `--org` and `--all` exclude each other, and `--all` takes no `--namespaces`. Run
  again, it sets the user's scope to exactly what it names: namespaces it no longer names
  stop showing once the new rows reach the replica that serves the read; `revoke` ends the
  login at once. `--rotate` prints a new password. When a step after the password
  fails, `grant` says so; run it again with `--rotate`.
- `--all` is for a deployment whose data belongs to one org. On a shared
  deployment grant each tenant its own org.
- `grant` refuses a scope that names a restricted namespace, and a wildcard that would
  reach one only the code repo configuration classifies: the views cannot read that
  configuration, so run the command with the server's. A namespace restricted there after
  a grant that reaches it is in that grant's views: `list` names each such grant, to
  replace with the other namespaces by name, and exits 2.
- `revoke` takes the scope rows away first, then drops the user. Until then a granted
  namespace counts as in use, so its name is not handed to a new owner (sign-up, claims).
- The command runs as `LOOM_CH_USER`, which needs access management: `CREATE USER`,
  `ALTER USER`, `DROP USER`, `CREATE SETTINGS PROFILE` and `ALTER SETTINGS PROFILE`,
  `SELECT ... WITH GRANT OPTION` on the five views, and `SHOW USERS` with reads of
  `system.users` and `system.settings_profile_elements`. It refuses a name already taken
  by a ClickHouse user that is not on the `loom_sql_surface` profile, and Loom's own users
  (`LOOM_CH_USER`, the reader user, `loom_analyst`, `default`).
- Drop a user with `revoke`, not with `DROP USER`: its scope rows stay granted until
  `revoke` writes them again, and a later user of the same name with `SELECT` on the views
  would read them.

## Querying

```sql
SELECT memory_key, value, updated_at
FROM loom.sql_v1_memories
WHERE namespace = 'support' AND status = 'active'
ORDER BY updated_at DESC
LIMIT 20;

SELECT day, sum(upstream_cost_usd) AS spend
FROM loom.sql_v1_usage_daily
WHERE day >= toDate(now(), 'UTC') - 30
GROUP BY day ORDER BY day;
```
