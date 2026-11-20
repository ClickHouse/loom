<!-- i18n: source=docs/guides/query-from-postgres.md blob=c49542f7fffa2ee2ced68d49d8ab0829854132f8 model=claude-sonnet-5-5 -->
> 本页由机器从[英文原文](https://github.com/ClickHouse/loom/blob/main/docs/guides/query-from-postgres.md)翻译，内容以英文原文为准。
> Machine-translated from the [English page](https://github.com/ClickHouse/loom/blob/main/docs/guides/query-from-postgres.md), which is authoritative.

<a id="query-loom-from-postgres"></a>

# 从 Postgres 查询 Loom

本指南从组织自己的 Postgres 中读取租户的记忆、对话和用量，这些数据可以与应用的表进行关联查询。它使用开源的 [`pg_clickhouse`](https://github.com/ClickHouse/pg_clickhouse) 扩展，该扩展把 ClickHouse 的表和视图导入为 Postgres 外部表，并将查询下推到 ClickHouse 执行。视图包含哪些内容、不包含哪些内容，见 [SQL 接口参考](https://github.com/ClickHouse/loom/blob/main/docs/reference/sql-surface.md)。

数据仍保存在 ClickHouse 中：来自 Postgres 的查询在查询时读取这些视图，适用的范围、数据扣留和新鲜度规则与其他任何 SQL 客户端相同。这里的 Postgres 是应用自己的，而不是 Loom 的[记忆权威库](https://github.com/ClickHouse/loom/blob/main/docs/operations/postgres.md)：扩展只负责读取，Loom 从不通过它写入，Loom 自己的数据库也不需要该扩展。请使用与 `LOOM_PG_DSN` 所指向的数据库不同的数据库，或者不同的服务。

CI 在 Postgres 16 和 18 上测试 `.github/workflows/ci.yml` 中 `LOOM_PGCH_VERSION` 所指定的版本（`tests/integration/test_pg_clickhouse_live_ch.py`）。

<a id="1-make-a-sql-user-for-the-tenant"></a>

## 1. 为租户创建 SQL 用户

在 Loom 一侧，以运维人员身份执行（[访问权限](https://github.com/ClickHouse/loom/blob/main/docs/reference/sql-surface.md#access)）：

```bash
loom sql-access grant --user acme_bi --org acme --namespaces support,research
```

该命令只会输出一次用户的密码。

<a id="2-install-the-extension"></a>

## 2. 安装扩展

该扩展包含在 `ghcr.io/clickhouse/pg_clickhouse:<postgres major>-<version>` 镜像中，也可以在 [PGXN 软件包库](https://pgxn.org/dist/pg_clickhouse/) 上获取，用于编译安装到自行管理的 Postgres（`pgxn install pg_clickhouse`）。托管的 Postgres 服务必须提供并允许使用该扩展。这里涉及三个角色：

- 本步骤使用的超级用户，因为该扩展未被标记为 trusted（在托管服务上，则是服务商允许其创建所允许扩展的角色）；
- 步骤 3 和 4 使用的 owner：具有 `CREATEROLE`、数据库上的 `CREATE` 以及外部数据封装器上的 `USAGE` 的角色，它创建 reader 角色，因此可以设置该角色的默认值；
- 步骤 5 使用的应用 reader 角色（此处为 `app_reader`），它需要步骤 3 中的用户映射以及步骤 4 末尾的授权。

以超级用户身份执行：

```sql
CREATE EXTENSION pg_clickhouse;
CREATE ROLE app_owner LOGIN CREATEROLE PASSWORD '<password>';
GRANT CREATE ON DATABASE app TO app_owner;
GRANT USAGE ON FOREIGN DATA WRAPPER clickhouse_fdw TO app_owner;
```

以 owner 身份执行：

```sql
CREATE ROLE app_reader LOGIN PASSWORD '<password>';
```

<a id="3-point-it-at-the-clickhouse-service"></a>

## 3. 指向 ClickHouse 服务

```sql
CREATE SERVER loom_ch FOREIGN DATA WRAPPER clickhouse_fdw
  OPTIONS (driver 'http', host 'abc123.us-east-1.aws.clickhouse.cloud', dbname 'loom');

-- The import in step 4 connects as the role that runs it; the queries in step 5 as theirs.
CREATE USER MAPPING FOR CURRENT_USER SERVER loom_ch
  OPTIONS (user 'acme_bi', password '<password from step 1>');
CREATE USER MAPPING FOR app_reader SERVER loom_ch
  OPTIONS (user 'acme_bi', password '<password from step 1>');
```

- `dbname` 是 Loom 的数据库（`LOOM_CH_DATABASE`，默认为 `loom`）。
- 对于 ClickHouse Cloud 主机，扩展的 `secure 'auto'` 默认值会启用 TLS，`http` 驱动的端口默认为 8443，其文档中有说明；这里的测试针对本地服务器使用纯 HTTP。对于自行管理的服务器，请设置 `port` 和 `secure 'on'`，使其与服务器的 TLS 端口一致。
- 只映射那些可以读取该租户数据的 Postgres 角色：用户映射中保存着 ClickHouse 密码，并且通过该 server 的每个查询都以该 SQL 用户的身份运行。

在角色上设置扩展的会话设置，使每个会话只发送该接口的 SQL 用户可以更改的那四项，别无其他：

```sql
ALTER ROLE app_reader SET pg_clickhouse.session_settings =
  'join_use_nulls 1, group_by_use_nulls 1, final 1, transform_null_in 0';
```

这些是扩展的默认值，在此显式写出，以免之后的版本在默认值中加入其他设置。如果会话设置了其他任何设置，会在 ClickHouse 中以 `READONLY` 失败：无论角色如何设置，SQL 用户的 profile 都会拒绝它（[访问权限](https://github.com/ClickHouse/loom/blob/main/docs/reference/sql-surface.md#access)）。

<a id="4-import-the-views"></a>

## 4. 导入视图

```sql
CREATE SCHEMA loom;
IMPORT FOREIGN SCHEMA loom
  LIMIT TO (sql_v1_memories, sql_v1_memory_history, sql_v1_conversations,
            sql_v1_agent_events, sql_v1_usage_daily)
  FROM SERVER loom_ch INTO loom;
```

第一个 `loom` 是 ClickHouse 数据库，第二个是 Postgres schema。用 `LIMIT TO` 指定视图名称，这样导入就固定在 `v1` 这一组，后续新增的 `sql_v2_*` 视图不会在未经请求的情况下被导入。然后让 reader 角色可以读取它们：

```sql
GRANT USAGE ON SCHEMA loom TO app_reader;
GRANT SELECT ON ALL TABLES IN SCHEMA loom TO app_reader;
```

`v1` 视图可能在 Loom 升级时新增列。外部表只保留导入时的列，因此要获取新增的列，请删除该 schema（以及基于其表创建的任何 Postgres 视图），然后重新执行导入和授权：`DROP SCHEMA loom CASCADE; CREATE SCHEMA loom;`。

<a id="5-query-and-join"></a>

## 5. 查询与关联

```sql
-- The facts Loom holds about each account on the support team
SELECT a.email, m.memory_key, m.value, m.updated_at
FROM accounts AS a
JOIN loom.sql_v1_memories AS m ON m.user_id = a.loom_user_id
WHERE a.team = 'support' AND m.status = 'active'
ORDER BY m.updated_at DESC;

-- Model spend per day for the last month
SELECT day, sum(upstream_cost_usd) AS spend
FROM loom.sql_v1_usage_daily
WHERE day >= (now() AT TIME ZONE 'UTC')::date - 30
GROUP BY day ORDER BY day;
```

Loom 扣留 `value` 时，它为 NULL；`value_withheld` 说明原因。

<a id="cost-and-scale"></a>

## 成本与规模

- `EXPLAIN (VERBOSE)` 会显示查询的哪一部分在 ClickHouse 中运行。外部表与本地表之间的关联在 Postgres 中执行，Postgres 会获取下推过滤之后剩余的每一行视图数据，因此请尽可能收窄对每个视图的过滤：按命名空间、按时间、按字面量 id 列表。
- 下推的过滤只会减少 ClickHouse 发送给 Postgres 的数据，而不会减少它读取的数据：每个视图读取授权的全部范围，账本类视图则读取其约 31 天的窗口（[读取的成本](https://github.com/ClickHouse/loom/blob/main/docs/reference/sql-surface.md#what-a-read-costs)）。
- 超出 SQL 用户上限（`LOOM_CH_ANALYSIS_MAX_EXECUTION_S`、`_MAX_ROWS_TO_READ`、`_MAX_MEMORY_BYTES`）的语句会在 ClickHouse 中直接失败，而不是变慢，Postgres 会返回其错误。
- 通过这些映射进行查询的每个 Postgres 会话都以同一个 SQL 用户登录，而该用户同一时间最多运行 `LOOM_CH_ANALYSIS_MAX_CONCURRENT` 条语句：请让应用的 reader 会话池大小低于该值，否则超出的语句会失败。

<a id="copies-and-erasure"></a>

## 副本与删除

外部表本身不保存任何数据：每次查询都读取 ClickHouse，因此 Loom 中的删除会立即对它们生效。在这个 Postgres 中生成的副本（从外部表执行 `CREATE TABLE … AS SELECT`、物化视图、转储）属于组织自己的数据，不在 Loom 的删除范围之内。

<a id="revoking"></a>

## 撤销

`loom sql-access revoke --user acme_bi` 会立即停止所有通过用户映射发起的查询：下一次查询将无法登录。然后，以 owner 身份执行 `DROP SERVER loom_ch CASCADE`，会删除两个用户映射和已导入的外部表。
