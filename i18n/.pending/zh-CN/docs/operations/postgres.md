<!-- i18n: source=docs/operations/postgres.md blob=d087a158c4304a8400459a4a96b0f61030b0fabb model=claude-sonnet-5-5 -->
> 本页由机器从[英文原文](https://github.com/ClickHouse/loom/blob/main/docs/operations/postgres.md)翻译，内容以英文原文为准。
> Machine-translated from the [English page](https://github.com/ClickHouse/loom/blob/main/docs/operations/postgres.md), which is authoritative.

<a id="postgres-memory-authority"></a>

# Postgres 记忆权威

本指南介绍如何搭建一个 Loom 部署，其在线记忆和知识文档的头部版本保存在 Postgres 中（可以是 ClickHouse Managed Postgres，也可以是自托管的 Postgres），并说明在对外提供服务之前需要检查哪些内容。其内部工作原理见[存储权威](../architecture/storage-authority.md)。

权威存储在部署首次启动时一次性选定。已经保存了记忆的 ClickHouse 会继续使用 ClickHouse 权威；请在空的 ClickHouse 上启动 Postgres 部署。

<a id="requirements"></a>

## 要求

- Postgres 16 或更高版本。无需任何扩展。
- 一个数据库，以及拥有该数据库的角色（Loom 会在其中创建 `loom` schema）。
- 该角色可以运行 `pg_logical_emit_message`（默认情况下 public 即可运行；否则启动会被拒绝，并给出需要执行的 `GRANT`）。
- 对于 `LOOM_PG_DURABILITY=synchronous`（默认值）：至少有一个位于其他故障域的同步备库，这样已确认的写入在主库丢失后仍能保留。
- 如果该数据库还要为复制工具提供数据，则需要 `wal_level = logical`；Loom 本身不需要它。

<a id="settings"></a>

## 设置

| 设置                                             | 值                                                                                                                                             |
| ------------------------------------------------ | ---------------------------------------------------------------------------------------------------------------------------------------------- |
| `LOOM_STORAGE_AUTHORITY`                         | `postgres`                                                                                                                                     |
| `LOOM_PG_DSN`                                    | `postgres://loom:<password>@<host>:5432/<database>?sslmode=verify-full&sslrootcert=<CA file>`，直连端点，不能是事务级连接池 |
| `LOOM_PG_POOL_MIN_SIZE`, `LOOM_PG_POOL_MAX_SIZE` | 每个 Loom 进程的连接数（2 和 20）；按所有副本的所有进程来确定 `max_connections` 的大小                                             |
| `LOOM_PG_DURABILITY`                             | `synchronous`（没有同步备库就拒绝启动）或 `local`                                                                       |

请指定服务器的证书颁发机构，并要求 `sslmode=verify-full`：它会同时检查证书和主机名，因此链路上的任何节点都无法冒充该机构，也就无法读取记忆或密码。`require` 及更低的级别只加密，不检查对端身份，仅适用于本地开发服务器。

Loom 自己管理连接池。请让 `LOOM_PG_DSN` 绕过任何会为每个事务给客户端分配不同服务器连接的连接池（事务级连接池）：Loom 的迁移会持有会话级 advisory lock，其驱动会预处理语句，而这类连接池不会在事务之间保留这两者。[单元](https://github.com/ClickHouse/loom/blob/main/docs/architecture/cells.md)的 `LOOM_PG_DSN` 直接指向 Postgres，不经过任何模式的连接池：它的进程会用启动参数 `loom.tenancy_checked` 标记每个会话，会话级连接池会拒绝或丢弃该参数（此时，对于单元中没有对应行的 org，其 org 触发器会拒绝 Loom 自身的写入，而 `loom.tenancy` 守卫也会拒绝为每个新分配的 org 写入其租户行），`loom doctor` 会指出丢失该参数的会话。脚本和批量加载工具切勿在单元上设置 `session_replication_role = replica`：这会跳过 org 触发器和守卫。

部署中的每个副本及其 worker 进程都要设置相同的值。完整列表（包括准入和投递相关设置）见[配置参考](https://github.com/ClickHouse/loom/blob/main/docs/reference/configuration.md)。

<a id="clickhouse-managed-postgres"></a>

## ClickHouse 托管 Postgres

1. 创建带有**两个 HA standby** 的服务。只有一个 standby 时，服务采用异步复制，已确认的写入可能随主库一起丢失。
2. 创建数据库和角色，授予该角色 `pg_read_all_stats`，以便 Loom 能看到已连接的 standby，并从服务的连接详情中获取端口为 5432 的**直连**连接字符串，不要使用连接池的：该服务的连接池运行在事务模式下。
3. 设置[设置项](#settings)并启动 Loom。检查 `GET /v1/health?include=postgres` 是否报告 `postgres.standby` 为 `synchronous`：如果 `synchronous_standby_names` 为空，启动会被拒绝；如果已连接的 standby 少于其所列出的数量，则会发出警告（[启动时检查的内容](#what-the-boot-checks)）。

故障转移、standby 的放置以及旧主库的隔离均由该服务负责。在 Kubernetes 上，chart 的 `postgres` 值用于配置[设置项](#settings)（[ClickHouse Cloud 和托管 Postgres](https://github.com/ClickHouse/loom/blob/main/docs/operations/clickhouse-cloud.md)）。

<a id="self-hosted-postgres"></a>

## 自托管 Postgres

在 HA 管理器下运行 Postgres，该管理器需保持一个同步备库，在提升时隔离旧主库，并且只提升持有所有已确认提交的备库。请将其配置为严格同步模式：至少需要一个同步备库，当没有备库连接时，拒绝写入，而不是仅由主库确认。不严格的管理器会在备库掉线时清空 `synchronous_standby_names`，此后提交将仅由主库确认。

Loom 在启动时检查备库设置，并且在运行期间，`GET /v1/health?include=postgres` 会报告 `postgres.standby`：当提交所等待的备库（`FIRST n`、`ANY n` 或 `n (...)` 中的 `n`，否则为一个）都已连接时，值为 `synchronous`；当没有指定任何备库，因此仅由主库确认提交时，值为 `primary_only`；当已连接的备库数量不足，因此提交需等待备库连接时，值为 `waiting`；当无法读取状态时，值为 `null`，原因是 Postgres 没有响应，或该角色无法查看 `pg_stat_replication`。

将 `LOOM_PG_DSN` 指向管理器的主库端点（跟随主节点的服务或地址），不要指向某一台主机，这样提升后 Loom 会随之切换。

下面是一个带一个备库、没有管理器的主库，如 `dev/ci_start_postgres.sh` 为测试所启动的那样，其设置如下：

```text
synchronous_commit = on
synchronous_standby_names = 'FIRST 1 (loom_standby)'
```

<a id="what-the-boot-checks"></a>

## 启动时检查的内容

| 检查项 | 在以下情况下拒绝启动 |
| ---------- | ---------- |
| 连接 | `LOOM_PG_DSN` 为空，或 Postgres 没有响应 |
| 迁移 | `schemas/pg_migrations/` 中已应用的文件被修改，或此构建缺少的某个迁移早于最后一个已应用的迁移 |
| 持久性 | 设置 `LOOM_PG_DURABILITY=synchronous` 时，`synchronous_standby_names` 为空；无论取何值，该角色都不得无法运行 `pg_logical_emit_message` |
| 权威 | 满足以下任一情况即拒绝启动：部署已记录 ClickHouse 权威；此 ClickHouse 在该权威下保存着记忆；或此 Postgres 属于另一个部署 |
| 单元状态 | 首次读取单元状态行（`loom.cell_first`、`loom.tenancy_mode`、`loom.cell_self`、`loom.tenancy`）失败；若 Postgres 迁移尚未完成，进程会以受限且未就绪的状态启动，直到读取成功（[故障排查](https://github.com/ClickHouse/loom/blob/main/docs/operations/troubleshooting.md#a-process-with-loom_cell-unset-is-gated)） |

迁移等待表锁最多 5 秒，并会尝试五次；如果启动时记录了四次 `Postgres migration <file> waited too long for a lock`，随后以 `LockNotAvailableError` 失败，说明有长事务占用了该表：请结束该事务（`pg_stat_activity` 会显示它），然后重新启动。如果启动时发现另一个启动进程正在执行迁移，它会无限期等待，待迁移完成后再启动。

每个 Loom 连接都会自行设置 `synchronous_commit = on`，不论服务器默认值是什么。当已连接的备库数量少于提交所需等待的数量时，以及当其角色无法查看 `pg_stat_replication` 时，启动过程会给出警告。备库未连接期间，提交会等待它们，最长为 `LOOM_PG_STATEMENT_TIMEOUT_MS` 再加一秒，然后返回 503 `POSTGRES_UNAVAILABLE` 并附带 `outcome_unknown`：被取消的等待会让该提交只留在主库上。投递 worker 只有在备库已持有该提交之后，才会把它发布到 ClickHouse，因此期间发生的故障切换不会在 ClickHouse 中留下它的任何内容（[存储权威](../architecture/storage-authority.md#only-durable-revisions-are-published)）。发布者若发送另一个会话的提交，会在一个事务中再次读取它，该事务提交一条前缀为 `loom.durable` 的事务性逻辑消息，因此数据库的变更数据捕获消费者每进行一次这样的读取就会收到一条；启动时会拒绝无权写入这种消息的角色。

`GET /v1/health` 会报告 `storage_authority`，在使用 Postgres 时还会报告 `pg_ok`；带上 `?include=postgres` 时，还会返回一个 `postgres` 块，包含 `standby`、`parked_deliveries`、`parked_audits`、`parked_knowledge_deliveries` 和 `backlog`（各来源的待处理和已搁置投递数，计数上限为 `LOOM_PG_BACKLOG_COUNT_CAP`，超出则标记 `capped`，另有最旧一条的年龄及该读数的年龄，见[管道 SLO](https://github.com/ClickHouse/loom/blob/main/docs/operations/monitoring.md#pipeline-slos)），这些语句在默认响应中不会出现在存活探测里。当请求池上的查询在两秒内没有得到回答（包括所有池连接都繁忙的情况）时，`pg_ok` 为 false。`/v1/health/ready` 不会等待 Postgres：无法连接它的副本仍然会为不使用它的平面提供服务。对于记忆操作、每个从文档头部读取其当前行或提交该行的知识操作、搜索以及检查头部的读取操作（`knowledge.search`、`knowledge.outline`、`knowledge.synthesize`），以及在那里读取或提交数据集目录的数据集操作（`knowledge.dataset_get`、`knowledge.dataset_approve`、`knowledge.query`），它会返回 503 `POSTGRES_UNAVAILABLE`。仅读写 ClickHouse 的知识操作，例如 `knowledge.list` 和 `knowledge.dedup_resolve`，会继续提供服务。`knowledge.ingest`、`knowledge.ingest_status` 和 `knowledge.ingest_cancel` 也会返回 503，因为它们各自都在那里读取或提交其作业（`loom.knowledge_jobs`）；因故障而中止的运行会计为一次尝试，并在 Postgres 恢复响应后再次运行。

`parked_deliveries` 统计所有租约无限期的记忆投递：已在 Postgres 中提交、但 ClickHouse 拒绝了 `LOOM_PG_DELIVERY_MAX_ATTEMPTS` 次的修订；因负载已被清除而无内容可发送的被取代版本（见下文）；以及 `loom reconcile` 持有的投递（`last_error` 为 `held by loom reconcile`），直到它自行释放。其 `targets` 不含 1、2、4 中任何一项的投递，其记忆行已在 ClickHouse 中（若有搜索文档，搜索文档也在），其审计行在 ClickHouse 或 `loom.audit_outbox` 中，只剩下撤销任务：8，即已删除决策的记录，在被撤销之前 `decision.chain` 仍会返回它；16，即它所取代版本的向量，在被撤销之前搜索仍能找到它们（对于 `high` 行，其围栏在此期间会隐藏所服务模型的向量）。尝试次数用尽的投递不会再被重试；其在 `loom.memory_deliveries` 中的 `last_error` 记录了错误的类型及其 ClickHouse 代码、HTTP 状态或 Postgres SQLSTATE，而不是错误文本，因为文本可能引用了记忆内容。下面这条语句可重试一条投递（不适用于 reconcile 持有的投递，那些由 `loom reconcile` 自行释放）：

```sql
UPDATE loom.memory_deliveries SET attempts = 0, held = false, lease_until = now() WHERE …
```

在[负载清除](../architecture/storage-authority.md#payload-clearing)过程运行之后，不含 `010_revision_delivered_at.sql` 的构建无法发送负载已被清除的修订：当这样的构建在提供服务时，重试会排除这些修订，方法是在其 `WHERE` 中加入以下内容（每条投递一次主键探测）：

```sql
AND NOT EXISTS (SELECT 1 FROM loom.memory_revisions r
                WHERE r.org = memory_deliveries.org
                  AND r.namespace = memory_deliveries.namespace
                  AND r.seq = memory_deliveries.seq
                  AND r.row IS NULL)
```

结束 worker 进程的发布（例如因超大值导致内存不足），会在下一轮接管其声明时计为一次尝试（`last_error` 为 `stopped mid-publish`）：达到上限时该修订被搁置；或者，如果它只剩搜索文档未发送，则会得到一个由嵌入 worker 填充的存根。

超过 ClickHouse 语句期限的发布会被计为这些行自身的失败，因为 ClickHouse 仍有响应：在一次变慢导致健康的修订被搁置之后，使用上面带有 `WHERE attempts >= …` 的重试语句可以将它们全部重试。

`last_error` 为 `payload cleared, nothing to send` 的搁置投递，对应的是这样一个版本：其负载已被[负载清除](../architecture/storage-authority.md#payload-clearing)过程清除，且其键的后续版本已将它取代：ClickHouse 提供该键的头部，而该版本的内容已不存在（例如某次恢复跳过了[运维手册](https://github.com/ClickHouse/loom/blob/main/docs/operations/backup-and-recovery.md#the-postgres-journal-and-a-clickhouse-restore)）。它的审计行也不会再次发送：它保留在 `loom.memory_revisions` 中该修订的 `audit` 里。重试该投递不会成功；记录之后，请删除它的行：

```sql
DELETE FROM loom.memory_deliveries
WHERE lease_until = 'infinity' AND last_error = 'payload cleared, nothing to send';
```

知识投递 worker 一次发布一条投递，每次撤销各自独立：每条投递最多两次插入，每轮最多处理 `LOOM_PG_DELIVERY_BATCH_ROWS` 条投递，都在一个 `LOOM_PG_DELIVERY_LEASE_SECONDS` 租约之内，在有积压时，租约通常会先于这一轮处理结束。被租约到期截断的一轮会释放其余投递而不计入尝试次数，下一轮会接手它们。

`parked_knowledge_deliveries` 统计已在 Postgres 中提交、但其 `loom.documents` 行被 ClickHouse 拒绝了 `LOOM_PG_DELIVERY_MAX_ATTEMPTS` 次的知识文档：头部服务于 `knowledge.get` 和搜索，而搜索会排除旧版本的分块，这些分块在该投递的撤销运行之前，ClickHouse 仍视其为有效（[知识搜索](../architecture/storage-authority.md#knowledge-search)）。被搁置的删除，其撤销先于其行运行：`knowledge.get` 返回它已删除，搜索不返回它的任何分块，而 `knowledge.restore` 在来源的发布仍可能落地期间返回 503：直到该投递落地，之后还要再等插入的时限。原件清扫会保留被搁置的有效文档所指向的原件，除其写入者之外的用户的擦除也是如此。它在 `loom.knowledge_deliveries` 中的 `last_error` 记录了错误的类型和代码。在滚动升级期间被旧构建擦除的范围，其投递不会被重试：请先再次运行该擦除（[滚动升级](../architecture/rolling-upgrades.md)）。下面这条语句可重试一条投递：

```sql
UPDATE loom.knowledge_deliveries SET attempts = 0, lease_until = now() WHERE …
```

`parked_audits` 统计 `loom.audit_outbox` 中被 ClickHouse 同样拒绝了这么多次、连同其 `last_error` 一起留在那里的行：包括擦除的审计行，以及因被取代的持有者仍可能插入它们而被某次声明推迟的审计行。下面这条语句可重试一条：

```sql
UPDATE loom.audit_outbox SET attempts = 0, due_at = now() WHERE …
```

<a id="rolling-upgrades-and-rollback"></a>

## 滚动升级与回滚

每个副本都必须运行会记录 authority 的构建版本。使用 Postgres authority 的部署不能回滚到不含该功能的构建版本：这样的构建不会读取任何记录，并且会直接写入 `loom.memory`（[滚动升级](../architecture/rolling-upgrades.md)）。

<a id="backup"></a>

## 备份

使用云服务商或托管工具提供的功能备份 Postgres，使用 ClickHouse 自带的工具备份 ClickHouse。恢复时只恢复其中一个存储，随后运行 `loom reconcile`，它会在该存储中重新执行另一个存储在恢复点之后记录的内容（[恢复单个存储](https://github.com/ClickHouse/loom/blob/main/docs/operations/backup-and-recovery.md#restoring-one-store)）。来自其他部署的 Postgres 将拒绝启动。
