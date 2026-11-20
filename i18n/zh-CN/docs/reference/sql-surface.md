<!-- i18n: source=docs/reference/sql-surface.md blob=cbe1b629ce7bff1c19c5d51c3a461f058ad2fd2c model=claude-sonnet-5-5 -->
> 本页由机器从[英文原文](https://github.com/ClickHouse/loom/blob/main/docs/reference/sql-surface.md)翻译，内容以英文原文为准。
> Machine-translated from the [English page](https://github.com/ClickHouse/loom/blob/main/docs/reference/sql-surface.md), which is authoritative.

<a id="sql-surface"></a>

# SQL 接口

SQL 接口是一组只读的 ClickHouse 视图，覆盖租户的记忆、记忆历史、对话和请求账本。组织自己的 SQL 客户端、BI 工具或数据仓库作业，使用由 `loom sql-access` 创建的 ClickHouse 用户来读取这些视图。该用户只能读取这些视图，不能访问其他内容，并且每个视图只显示该用户被授予权限的组织和命名空间。

<a id="version-and-compatibility"></a>

## 版本与兼容性

每个视图的名称都带有版本号：`sql_v1_memories` 是版本 1。列名、类型和含义是公开的约定：

- `v1` 视图可以新增列，但不会删除或重命名列，列的类型和含义也不会改变。
- 会破坏这条规则的变更，会以 `sql_v2_*` 视图的形式与 `v1` 并存发布。两者至少会同时提供一个版本，发布说明会指明即将移除的 `v1` 视图。
- 请按名称选择列：`SELECT *` 会包含新增的列。

<a id="views"></a>

## 视图

每个视图都在 Loom 的数据库中（默认为 `loom`，除非 `LOOM_CH_DATABASE` 指定了其他数据库），并带有标识列 `org` 和 `namespace`。`sql_v1_memories`、`sql_v1_memory_history` 和 `sql_v1_agent_events` 还带有 `agent`、`user_id` 和 `session_id`；`sql_v1_conversations` 带有 `agent` 和 `user_id`；`sql_v1_usage_daily` 带有 `agent`。联接可以匹配双方都带有的[标识元组](../concepts/identities-and-namespaces.md)中的任意部分。

<a id="sql_v1_memories"></a>

### `sql_v1_memories`

每条记忆（标识加键）一行：即它的当前版本。已删除或已擦除的记忆不会出现，被拒绝的记忆也不会出现。其他状态会连同其 `status` 一起列出：除非特别要求，`memory.list` 会隐藏 `duplicate`、`superseded` 和 `proposed` 行，因此若要得到默认列表所显示的内容，请按 `status = 'active'` 过滤。超过 `expires_at` 的记忆在过期清理处理它之前，仍会带着其值列出，与 `memory.list` 的列出方式相同；若要得到 `memory.search` 所返回的内容，请加上 `(expires_at IS NULL OR expires_at > now())`。

| 列                                                                 | 含义                                                                                               |
| ------------------------------------------------------------------ | -------------------------------------------------------------------------------------------------- |
| `org`, `namespace`, `agent`, `user_id`, `session_id`, `run_id`     | 该记忆所属的对象以及写入它的运行（无则为 `-`）                                                     |
| `memory_key`, `memory_id`, `memory_type`                           | 键、记忆的 id（与 `version` 合起来对应一个版本）、类型（`fact`、`decision` 等）                    |
| `value`                                                            | `memory.get` 所返回的值，被隐去时为 NULL                                                           |
| `value_withheld`                                                   | 返回 `value` 时为 `''`，`high` 行为 `sensitive`，已脱敏的版本为 `redacted`                         |
| `content_sha256`                                                   | 值的 SHA-256，只要 `value` 为 NULL 它也为 NULL：较短密钥的哈希可以被猜出                           |
| `version`, `status`, `confidence`, `pinned`, `tags`                | 与 `memory.get` 相同                                                                               |
| `sensitivity`, `source_class`, `actor`                             | 标签、来源类别、写入它的令牌主体                                                                   |
| `created_at`, `updated_at`, `expires_at`, `valid_at`, `invalid_at` | 写入时间和最近更改时间、过期时间、该事实开始成立和不再成立的时间                                   |

<a id="sql_v1_memory_history"></a>

### `sql_v1_memory_history`

每条记忆的每个版本一行，包括删除所产生的版本（`status =
'tombstoned'`，并设置了 `tombstoned_at`）。被拒绝的版本不会出现。列与 `sql_v1_memories` 相同，但去掉了 `expires_at`、`valid_at` 和 `invalid_at`，并增加了 `tombstoned_at`。已脱敏的版本，以及曾被标为 `high` 的键的每个版本，其 `value` 和 `content_sha256` 都为 NULL：在敏感度清理给某行打标签之前的版本，保存的是同一个未加标签的值。

<a id="sql_v1_conversations"></a>

### `sql_v1_conversations`

每个用户已存储的每个对话轮次一行：`org`、`namespace`、`agent`、`user_id`、`thread_id`、`msg_id`、`role`、`content`、`tokens`、`run_id` 和 `ts`（该轮次最近一次写入）。已删除或已被压缩掉的轮次（设置了 `tombstoned_at`）不会出现，已擦除的轮次连同其行一起消失。轮次不带敏感度标签，因此 `content` 按写入时的原样返回，与 `conversation.get` 返回某个线程的轮次的方式相同；跨线程列表对读起来属于 `high` 的文本所做的检查，是在 Loom 中运行的，而不是在 SQL 中。

<a id="sql_v1_agent_events"></a>

### `sql_v1_agent_events`

请求账本：每次 op 调用或 worker 一次处理一行，请求的 `kind` 为 `''`。
列有 `ts`、`org`、`namespace`、`agent`、`user_id`、`session_id`、`run_id`、`operation`、`kind`、`request_id`、`trace_id`、`latency_ms`、`success`、`error_code`、`status_class`、`memory_key`、`input_tokens`、`output_tokens`、`embedding_tokens`、`llm_calls`、`upstream_cost_usd`（模型花费，单位为美元）、`bytes_written`、`model`、`consumer`、`subject`（发起调用的令牌主体）和 `transport`。其中不含请求参数。已擦除的终端用户的行，其 `user_id` 显示为 `<erased>`。

它保存今天之前完整的 31 个 UTC 日以及今天至今的数据；每条语句读取自己的 `now()`。窗口长度及其与 UTC 日的对齐方式是 `v1` 契约的一部分。

<a id="sql_v1_usage_daily"></a>

### `sql_v1_usage_daily`

在同一窗口内，把 `sql_v1_agent_events` 按 UTC 的 `day`、`org`、`namespace`、`agent`、`operation` 和 `kind` 汇总：`ops`、`errors`、`input_tokens`、`output_tokens`、`embedding_tokens`、`llm_calls`、`upstream_cost_usd`、`bytes_written`、`latency_sum_ms`（与 `ops` 配合可求跨行的均值），以及 `latency_p50_ms` 和 `latency_p95_ms`，二者都是近似值且按行计算：它们不能跨行合并。除今天之外，每个 `day` 都是完整的一天。

<a id="what-a-read-costs"></a>

### 读取的开销

视图会对其返回的行应用调用方的过滤条件，因此对 `ts`、`day`、`thread_id` 或 `memory_key` 的过滤只会缩小结果，而不会减少读取量。对 `sql_v1_memories`、`sql_v1_memory_history` 和 `sql_v1_conversations` 的每次读取，都会读取授权的整个范围：历史视图要对其中每个版本排序，对话视图要对其中每个轮次（包括内容）分组。这些开销随范围内存储的数据增长，范围过宽时可能超出下面的读取上限。对账本视图的每次读取，都会读取授权的整个范围在窗口内的数据：最早从窗口开始的那个月的月初算起，通常与窗口本身相差不大。更宽的授权（`*` 命名空间、`--all`）会读取更多。SQL 用户的读取上限（`LOOM_CH_ANALYSIS_MAX_ROWS_TO_READ`、`_MAX_EXECUTION_S`、`_MAX_MEMORY_BYTES`、`_MAX_THREADS`）适用于这次读取，因此繁忙的范围可能超出这些上限：请缩小授权，或调高这些设置（`insights.query` 共用它们），然后再次运行 `loom sql-access grant`，它会把这些设置写入 SQL 用户的配置档。早于窗口的事件不在该接口上；[分析导出](https://github.com/ClickHouse/loom/blob/main/docs/operations/analytics-export.md)会把每日用量合计以哈希后的名称写入运维方的存储桶。除非 `LOOM_RETENTION_AGENT_EVENTS_DAYS` 设置了保留期，否则 `loom.agent_events` 本身没有 TTL。

<a id="what-the-views-never-show"></a>

## 视图从不显示的内容

记忆视图沿用 Loom 自身列表的规则（[敏感度](https://github.com/ClickHouse/loom/blob/main/docs/architecture/security.md#sensitivity)）；对话视图按上文 `conversation.get` 的方式提供一个轮次。以下内容它们都不会显示：

- `high` 行的值及其哈希，无论在当前视图中，还是在历史记录里该键的每个版本中；
- 已脱敏版本的值及其哈希；
- 被拒绝的记忆、已删除或已擦除的记忆，以及已删除的对话轮次；
- 在 `loom.namespaces` 或 `loom.code_repos` 中被归类为 `restricted` 的命名空间的任何行，无论授予何种权限，自归类之时起即生效：Loom 会审计对[受限命名空间](https://github.com/ClickHouse/loom/blob/main/docs/architecture/security.md#restricted-namespaces)的每次读取，而视图无法做到这一点；
- 镜像（名称为 `acl-` 加 24 位十六进制数字的命名空间）的任何行，无论授予何种权限：其中每个文档只会显示给其存储的权限列表所允许的读取者（[文档来源](https://github.com/ClickHouse/loom/blob/main/docs/architecture/document-sources.md#the-mirror)），而视图无法检查这一点；
- 这五个视图之外的表：令牌、授权、密钥、审计日志、文档、嵌入和请求参数；
- 会显示其他用户语句的 `system` 表，例如 `system.query_log`、`system.mutations` 和 `system.processes`。只有开启 `access_control_improvements.select_from_system_db_requires_grant`（默认开启）时，ClickHouse 才会对没有授权的用户拒绝访问这些表，因此 `loom sql-access grant` 会以它设置了密码的每个用户登录，并在 `system.processes` 有响应时删除该用户。在 Loom 的数据库中，`system.tables` 只列出这五个视图。

记忆视图和对话视图读取的表，正是[擦除 saga](https://github.com/ClickHouse/loom/blob/main/docs/features/memory/retention-and-erasure.md) 要从中删除数据、其残留普查要统计的表，因此被擦除的用户或命名空间的行会随这些表一起消失。账本视图保留其行，这些行不含内容：用户擦除会把该用户的 `user_id` 改写为 `<erased>`，与 `loom.agent_events` 中的做法相同。

<a id="freshness"></a>

## 数据新鲜度

这些视图在查询时读取 ClickHouse，不复制任何数据。Postgres 保存实时记忆（[存储权威](../architecture/storage-authority.md)），`loom.memory` 和 `loom.memory_versions` 是每次提交之后发布的投影，每隔 `LOOM_PG_PUBLISH_INTERVAL_MS`（默认 250 ms）刷新一次。已提交的记忆写入在其刷新完成后会出现在 `sql_v1_memories` 和 `sql_v1_memory_history` 中，通常在大约一个间隔之内；如果某次投递因写入方停止而被遗留，则由投递 worker 负责发布，这可能需要更长时间。这些视图不会等待任何内容：`memory.search` 在超过 `LOOM_PG_PROJECTION_WAIT_MS` 后会返回 `PROJECTION_BEHIND`，而视图则直接返回 ClickHouse 中已有的数据。对话和请求账本则直接写入 ClickHouse。

<a id="access"></a>

## 访问

该接口的 SQL 用户是一个 ClickHouse 用户，仅对这五个视图拥有 `SELECT` 权限，没有其他授权，并使用设置配置文件 `loom_sql_surface`：

- `readonly = 1`：语句只能修改 `final`、`join_use_nulls`、`group_by_use_nulls` 和 `transform_null_in` 这几项设置，它们不会改变任何视图返回的行，客户端也会自行发送它们（`pg_clickhouse` 即是其一）。任何其他设置都可能绕过视图的过滤：溢出模式、`additional_table_filters`、`FINAL` 下的 PREWHERE。
- 无论服务器的默认值如何，配置文件都固定以下值：`apply_deleted_mask = 1` 和 `apply_patch_parts = 1`（已删除的行保持隐藏），`use_query_cache = 0` 和 `use_query_condition_cache = 0`（二者都以语句文本为键，而所有用户共用同一文本），`use_skip_indexes_if_final_exact_mode = 1`、`do_not_merge_across_partitions_select_final = 0` 和 `optimize_move_to_prewhere_if_final = 0`（`FINAL` 读取每个键的最新版本），所有溢出模式均为 `'throw'`（read 和 timeout 及其 `_leaf` 形式，以及 set、transfer、join、sort、distinct、group by 和 result），`send_logs_level = 'none'`，不提供内省函数，并且 `query_plan_filter_push_down = 0` 和 `enable_optimize_predicate_expression = 0`，因此调用方的过滤条件只作用于视图返回的结果，绝不会作用于其他租户的行（对某个值报错的过滤条件会把该值引用出来）；
- 沿用 `insights.query` 的上限，但不包括其结果上限，因为 SQL 客户端会读取完整的结果集：`LOOM_CH_ANALYSIS_MAX_EXECUTION_S`、`_MAX_ROWS_TO_READ`、`_MAX_MEMORY_BYTES` 和 `_MAX_THREADS`，以及同时执行的语句数 `_MAX_CONCURRENT`。

每个视图以其定义者的权限运行（`SQL SECURITY DEFINER`，即执行迁移的用户），并且只放行 `(org, namespace)` 已由表 `loom.sql_access` 授予 `currentUser()`（即发送该语句的用户）的行。没有被授予任何行的用户什么也读不到，其他所有 ClickHouse 用户也一样，包括 Loom 自己的用户。

```bash
loom sql-access grant --user acme_bi --org acme --namespaces support,research
loom sql-access grant --user acme_bi --org acme --namespaces '*'   # every namespace of acme
loom sql-access grant --user ops_bi --all                          # the whole deployment
loom sql-access grant --user acme_bi --org acme --namespaces support --rotate
loom sql-access list
loom sql-access revoke --user acme_bi
```

- `grant` 在用户不存在时创建该用户，并只打印一次密码；Loom 不保留副本。`--org` 与 `--all` 互斥，`--all` 不接受 `--namespaces`。再次运行时，会把该用户的范围设置为恰好是命令中指定的内容：不再指定的命名空间，会在新的行到达提供读取的副本后停止显示；`revoke` 会立即终止登录。`--rotate` 会打印新密码。当密码之后的某一步失败时，`grant` 会给出提示；请加上 `--rotate` 再运行一次。
- `--all` 适用于数据只属于一个 org 的部署。在共享部署上，应为每个租户分别授予各自的 org。
- `grant` 会拒绝包含受限命名空间的范围，也会拒绝可能涵盖受限命名空间的通配符，而该命名空间仅由代码仓库配置标记为受限：视图读不到该配置，因此请使用服务器的配置运行该命令。若某个命名空间在授权之后才被设为受限，而该授权涵盖它，则它仍在该授权的视图中：`list` 会列出每个这样的授权，请改为按名称指定其他命名空间，并以退出码 2 退出。
- `revoke` 先删除范围行，再删除用户。授权中指定的命名空间在 `revoke` 删除该行之前视为在用：其名称不会交给新的所有者，对它的首次写入会将其注册为无所有者。`*` 授权不指定任何命名空间：它读取该 org 的所有命名空间，包括之后创建的，因此绝不要对注册用户共享的 org（`LOOM_SELF_SIGNUP_ORG`）授予这种权限。
- 该命令以 `LOOM_CH_USER` 身份运行，它需要访问管理权限：`CREATE USER`、`ALTER USER`、`DROP USER`、`CREATE SETTINGS PROFILE` 和 `ALTER SETTINGS PROFILE`，对五个视图的 `SELECT ... WITH GRANT OPTION`，以及 `SHOW USERS` 和对 `system.users` 与 `system.settings_profile_elements` 的读取权限。它会拒绝已被某个不使用 `loom_sql_surface` 配置文件的 ClickHouse 用户占用的名称，也会拒绝 Loom 自己的用户（`LOOM_CH_USER`、读取用户、`loom_analyst`、`default`）。
- 请使用 `revoke` 删除用户，不要用 `DROP USER`：在 `revoke` 重写之前，其范围行仍保持授权状态，之后若有同名且对这些视图拥有 `SELECT` 权限的用户，就能读到这些行。

<a id="querying"></a>

## 查询

在组织自己的 Postgres 中查询，请参阅[从 Postgres 查询 Loom](https://github.com/ClickHouse/loom/blob/main/docs/guides/query-from-postgres.md)。

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
