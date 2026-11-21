<!-- i18n: source=docs/operations/upgrades.md blob=8caf8c789d2fc39ab89d9830d45a7e1229be2051 model=claude-sonnet-5-5 -->
> 本页由机器从[英文原文](https://github.com/ClickHouse/loom/blob/main/docs/operations/upgrades.md)翻译，内容以英文原文为准。
> Machine-translated from the [English page](https://github.com/ClickHouse/loom/blob/main/docs/operations/upgrades.md), which is authoritative.

<a id="upgrades"></a>

# 升级

升级会替换镜像（以及文档提取器的镜像，它不保存任何状态），并应用新版本自带的迁移。其他状态都不会移动：所有持久数据都在 ClickHouse 中（如果使用 `LOOM_DOCUMENT_OBJECT_STORE=s3` 写入原始文档，则还包括文档存储桶），代码镜像则原样挂载。本页面向运维人员。客户端如何看待未知字段和已移除字段，见 [HTTP API 参考](https://github.com/ClickHouse/loom/blob/main/docs/reference/http-api.md#unknown-and-removed-fields)。哪些版本会获得修复，以及哪个 SDK 可与哪个服务器配合使用，见[支持的版本](https://github.com/ClickHouse/loom/blob/main/docs/operations/supported-versions.md)。

<a id="how-migrations-run"></a>

## 迁移如何运行

迁移日志、编号、运行器指令以及编写迁移的规则见[迁移](../development/migrations.md)。运维人员需要了解以下内容：

- **启动时。** 服务器在报告就绪或启动 worker 之前，会先应用待执行的文件（`LOOM_BOOT_MIGRATE=true`，默认值）。任何容器中的每个进程都会获取 `loom._migration_lock` 中的 `KeeperMap` 锁，因此每个文件只会应用一次。没有 Keeper 的服务器会被拒绝启动。
- **provider 重建。** 启动时若有待执行的文件，进程会先注册为 provider 参与者，然后在 provider 配置锁（一个 Keeper 键，仅在该检查期间持有）下检查是否存在未完成的 provider 重建，若存在则拒绝迁移：`Finish the provider rebuild before applying schema migrations`。之后文件只在迁移锁下应用。期间发起的重新配置会等待，直到正在启动的进程确认接收，因此重建不会与迁移同时运行。
- **容器在迁移或重新配置时被终止**，只会在一段时间内阻塞其他容器。迁移锁在持有者停止续期（`ttl_s`）后会被接管。provider 锁（TTL 为 60 s，每 20 s 续期一次）在过期且其持有者的参与者行也已过期后会被接管：即 60 s 内没有心跳（每 10 s 一次），并且任何节点的 `system.processes` 中都没有带有其 id 标记的查询。同时启动的容器会等待 provider 锁最多 120 s，而不是直接失败。如果某个锁的持有者没有参与者行，在确认该进程已停止后，可用 `loom provider-lock clear --holder <holder>` 释放；当持有者仍在续期、发送心跳或有查询在运行时，该命令会拒绝执行。
- **Mutation。** 只有在文件的 mutation 全部完成后，该文件才会被记录。mutation 失败，或运行时间超过 `LOOM_MIGRATE_MUTATION_WAIT_S`（默认 600），都会使该文件失败并中止启动，日志中会指明该文件。
- **键变更。** 修改排序键或分区键的迁移会自行重建空表，而对有数据的表则会使启动失败。部署前请对该表运行 `scripts/ops/table_rebuild.py`；严格的 dry run 可以发现这种情况。
- **全新安装。** 全新安装与升级走同一条路径：服务器会重放账本中缺少的所有文件。在空数据库上，这意味着重放整个日志，需要几分钟。
- **账本中断。** 如果数据库的账本停在某个基线所取代的文件的中间，该数据库会被拒绝（`stranded`）：请先用该基线之前的构建对其进行迁移。
- **文件重新编号。** 账本中的某一行，其文件在仓库中已不存在，则为 `ledger-only`，`--strict` 遇到它会以 1 退出；但如果其记录的 SHA-256 与某个当前文件的相同，则例外：这是同一个文件换了新编号。运行器会将其报告为已重命名，若该文件仍待执行，则记录新名称而不再次运行，`--strict` 通过。如果文件在以旧名称运行之后又被编辑过，其 SHA-256 就不同，因此仍然是 ledger-only：其新名称会作为待执行文件运行，旧的行需要手动删除（[故障排查](https://github.com/ClickHouse/loom/blob/main/docs/operations/troubleshooting.md#migrations-pending-or-checksum-drift)）。在此之前，`/v1/health` 会在 `schema.ledger_only` 下列出该旧行。

CLI 可以手动运行同一个运行器：

```bash
loom migrate --status              # applied and pending; changes nothing
loom migrate --dry-run --strict    # pending files; exit 1 on ledger-only rows (not renames) or checksum drift
loom migrate                       # apply pending migrations
loom migrate --target N            # apply through file number N only
```

`make migrate`、`make migrate-status` 和 `make migrate-dry` 在代码检出目录中运行同一个模块。

<a id="upgrade-procedure"></a>

## 升级流程

1. 阅读该版本的 [GitHub 发布说明](https://github.com/ClickHouse/loom/releases)，了解运维步骤（新增设置、部署必须执行的回填）以及客户端可见的接口变更。
2. 在新构建的检出目录中，针对生产环境的 ClickHouse 运行 `loom migrate --dry-run --strict`。它会列出启动时将应用的迁移，并在账本出现问题时停止（[故障排查](https://github.com/ClickHouse/loom/blob/main/docs/operations/troubleshooting.md#migrations-pending-or-checksum-drift)）。如果该版本声明需要重建表，现在就对该表运行 `scripts/ops/table_rebuild.py`。使用 Postgres authority 的部署若要升级到把知识文档头保存在 Postgres 中的构建，现在要手动取消设置 `LOOM_RETENTION_DOCUMENTS_DAYS`：新构建在该变量已设置时拒绝启动，而且在该 authority 下，没有任何保留 TTL 会作用于 `loom.documents`，命名空间的 `retention_days` 也不会作用于 `loom.chunks`（[滚动升级](../architecture/rolling-upgrades.md)，规则 38）。若[单元](https://github.com/ClickHouse/loom/blob/main/docs/architecture/cells.md)要升级到会标记其会话并带有 org 触发器的构建（规则 47），须先把它的 `LOOM_PG_DSN` 直接指向 Postgres，绕过任何连接池（[Postgres](https://github.com/ClickHouse/loom/blob/main/docs/operations/postgres.md)）；若某个后续单元曾经准备过、之后通过取消设置 `LOOM_CELL` 撤回，则要先清除其状态（[故障排查](https://github.com/ClickHouse/loom/blob/main/docs/operations/troubleshooting.md#a-process-with-loom_cell-unset-is-gated)）：新构建会根据该状态对其进程设置准入。对于已经准备好的后续单元，新构建第一次执行迁移的启动会创建 org 触发器，因此仍在该处运行的旧 pod 在被替换之前，每次插入租户 org 的行都会被拒绝（对其调用方表现为未映射的错误）：请一步完成该单元的滚动发布。
3. 部署新镜像。第一个启动的容器会应用迁移；其他容器等待它完成，然后发现没有待处理的迁移。
4. 等待 `/v1/health` 报告 `status: ok` 且 `schema.pending` 为空，再把流量路由过去。[零停机双实例部署](https://github.com/ClickHouse/loom/blob/main/docs/operations/deployment.md#zero-downtime-pair)会替你完成第 2 到 4 步，但不包括第 2 步所说的取消设置：`redeploy.sh` 在预检中运行严格的 dry run，并且只在新颜色就绪后才切换。
5. 如果有回填，运行该版本的回填（[维护](https://github.com/ClickHouse/loom/blob/main/docs/operations/maintenance.md#backfills-and-repairs)）。
6. 当该版本提到插件变更时，更新客户端插件：hook 在客户端机器上运行，只能通过客户端的插件管理器更改（Claude Code 使用 `claude plugin update loom@loom`），不能通过服务端部署更改。
7. 最后一个旧副本停止后，重新验证滚动升级期间完成的擦除：`loom erasure-re-verify --completed-since <when step 3 began> --resume`。旧副本的 saga 不知道新构建新增的表（[滚动升级](../architecture/rolling-upgrades.md)，规则 14）。该命令只删除孤立的行链接：即每次擦除范围内 `entity_triple_rows` 和 `entity_edge_rows` 中，其记忆没有存活行、且自该次擦除的普查以来没有写入行的那些行。不带 `--resume` 时，它只统计这些行，若仍有剩余则以退出码 1 退出。擦除之后写入的数据会保留。
8. 只有在所有副本都运行新构建之后，才开启上一版构建无法支持的功能：`memory_extract_agent_learnings`，因为上一版构建不会擦除 `loom.learnings_supporters`（[滚动升级](../architecture/rolling-upgrades.md)，规则 23）；`LOOM_DOCUMENT_OBJECT_STORE=s3`，因为上一版构建读取 bucket 行的空 `data`，并以 `410 BLOB_GONE` 响应其下载（规则 9）；以及 `LOOM_PG_PAYLOAD_CLEAR_ENABLED`，要在新构建单独提供服务满一个清理期之后才能开启，因为上一版构建不会给迟到的投递打标记（规则 43）。
9. 如果部署现在或此前任何时候设置过 `LOOM_CODE_ASK_MEMORY_NAMESPACE`，并且从一个缓存的 `code.ask` 答案不记录所引用记忆的构建（早于 `086_code_answer_memory_scope.sql` 的构建）升级到会记录的构建（包括回滚后再次前滚）：待新构建单独提供服务、且没有提供方重建在运行时，用 `TRUNCATE TABLE <LOOM_CH_DATABASE>.code_answers` 清空答案缓存（默认是 `loom.code_answers`）。旧构建的答案和墓碑不记录任何记忆，而且它在滚动升级期间执行的擦除或脱敏并不知道这张表（[滚动升级](../architecture/rolling-upgrades.md)，规则 36），因此没有任何 saga 能找到其中的内容。重建会把它的表副本发布覆盖到原表上，所以跨越 truncate 运行的重建会把这些行带回来。truncate 会立即在复制表的每个副本上丢弃数据分区，无需等待 mutation，而且 loom 用户拥有该授权。每个被清除的问题在下次被询问时需要一次综合生成。
10. 升级到其用户擦除能够覆盖“由用户记忆汇总而成的合并摘要”所用数据的构建时（包括回滚后再次前滚）：待新构建单独提供服务后，对此前未请求 `purge` 的每次用户擦除重新发起请求（即 `loom.erasure_requests` 中的 `user_id` 行，保留 90 天），也包括新构建已完成的那些：旧构建的 saga 遗留了摘要的历史、chunk、向量、图行和共享副本（[滚动升级](../architecture/rolling-upgrades.md)，规则 37）。新的 saga 还会擦除该用户此后写入的内容：只对没有存活记忆行的用户，或只在确实需要时才发起请求。旧构建完成的用户 purge 同样留下了这些行，而之后的用户擦除找不到它们：该用户的键已不存在。旧构建执行过 `shared.tombstone` 之后的擦除，无论由哪个构建完成，还会留下该用户自己记忆的共享副本的搜索行。旧构建以 `agent="*"` 提升的共享副本，其来源 agent 记为 `*`，对其所复制的用户的任何擦除都可能找不到它。列出它们，并逐条审查，对包含已被擦除用户文本的副本使用 `memory.demote`：

    ```sql
    SELECT org, namespace, memory_key FROM loom.shared FINAL
    WHERE tombstoned_at IS NULL AND arrayExists(x -> splitByChar('/', x)[3] = '*', promoted_from)
    ```

11. 使用 Postgres authority 的部署若升级到把知识文档头保存在 Postgres 中的构建（`002_knowledge_documents.sql`），包括回滚后再次前滚：待新构建单独提供服务后，自新构建首次提供服务以来，凡在旧构建副本仍提供服务期间完成的擦除，都要重新运行一遍。旧构建的擦除不会删除任何文档头或投递：被擦除文档的文本会留在 `loom.knowledge_heads` 中，直到擦除再次运行，而它遗留的投递会被投递 worker 再次发布（[滚动升级](../architecture/rolling-upgrades.md)，规则 38）。请在重试已停放的投递之前完成此操作。
12. 使用 Postgres authority 的部署若升级到把知识摄取作业（`005_knowledge_jobs.sql`）或数据集目录（`007_knowledge_datasets.sql`）保存在 Postgres 中的构建，包括回滚后再次前滚：待新构建单独提供服务后，自新构建首次提供服务以来，凡在旧构建副本仍提供服务期间完成的擦除，都要重新运行一遍。旧构建的擦除不会从 `loom.knowledge_jobs` 删除任何作业，也不会从 `loom.knowledge_datasets` 删除任何目录行：新构建的 worker 会把这样的作业重新投影回 `loom.knowledge_ingestions` 并运行它，而目录行会一直保留，直到擦除再次运行（[滚动升级](../architecture/rolling-upgrades.md)，规则 40 和 42）。第 11 步的重新运行对文档头、作业和目录行同样有效；每次擦除运行一次就够了。同时，对旧构建在该时间窗口内答复的每个 `knowledge.delete` 和 `knowledge.bulk_delete` 也要重新发起：它只从 `loom.datasets` 中移除了这些 source 的数据集。
13. 升级到在任意命名空间上保存文档 source 的构建时（包括回滚后再次前滚）：待新构建单独提供服务后，清理旧构建的 `namespace.delete`、`collection.delete` 或命名空间擦除遗留为活动状态的 source（[滚动升级](../architecture/rolling-upgrades.md)，规则 44）。已删除命名空间上的 source：

    ```sql
    SELECT s.org, s.namespace, s.name
    FROM (SELECT org, namespace, name FROM loom.knowledge_sources FINAL WHERE active) AS s
    INNER JOIN (SELECT org, namespace FROM loom.namespaces FINAL WHERE NOT active) AS n
        ON s.org = n.org AND s.namespace = n.namespace
    ```

    以及在某次命名空间擦除完成之前注册、却没有被该擦除停用的 source（由旧构建运行的擦除，或在新构建的擦除运行期间由旧构建添加的 source）：

    ```sql
    SELECT s.org, s.namespace, s.name, e.id
    FROM (
        SELECT org, namespace, name, active, created_at, updated_at
        FROM loom.knowledge_sources FINAL
    ) AS s
    INNER JOIN (
        SELECT id, org, completed_at,
               arrayJoin(if(scope_type = 'namespace', [scope_value],
                            if(JSONType(scope_value) = 'Array',
                               JSONExtract(scope_value, 'Array(String)'),
                               JSONExtract(scope_value, 'namespaces', 'Array(String)')))) AS namespace
        FROM loom.erasure_requests FINAL
        WHERE scope_type IN ('namespace', 'namespace_list') AND status = 'completed'
    ) AS e ON s.org = e.org AND s.namespace = e.namespace
    WHERE s.created_at <= e.completed_at AND (s.active OR s.updated_at > e.completed_at)
    ```

    对于第一个查询，移除列出的每个 source（`knowledge.source.remove`），或用新构建再次删除其命名空间，这会停用它。对于第二个查询，用 `knowledge.source.remove` 并带上 `delete_documents=true` 移除列出的每个 source，这会停用它回写的文档（它也接受已被停用的 source，例如之后删除其命名空间所致），并用 `memory.delete` 删除从这些文档中挖掘出的决策行。新构建的命名空间擦除两件事都会做，但它也会擦除自第一次擦除以来写入该命名空间的所有内容；只对此后无人写入的命名空间才再次发起。擦除之后注册的 source 不会被列出：该擦除从未覆盖过它。第二个列表是只需处理一次的工作清单：按上述方式移除的 source 仍会被列出，因为它的移除行比该擦除更新。

    由旧构建启动、由新构建完成的命名空间擦除在这里不需要任何处理：它最后的普查会找到其 source 回写的文档和决策并重新打开相应步骤，因此 saga 只有在它们都消失之后才会完成。

14. 升级到其命名空间擦除会覆盖 `loom.shared` 的构建时（包括回滚后再次前滚）：待新构建单独提供服务后，清理旧构建的命名空间擦除遗留的已提升行（[滚动升级](../architecture/rolling-upgrades.md)，规则 45）。下面的查询列出账本（保留 90 天）中某次 `namespace` 或 `namespace_list` 擦除触及过、且仍有已提升行的每个命名空间，以及最近一次不带 purge 的擦除（`erased_at`）和带 purge 的擦除（`purged_at`）进行普查的时间。下面的语句处理在该时间点之前提升的行，因此在普查之后提升的行会保留，在普查所在那一秒内更早提升的行也可能保留，因为 `promoted_at` 只精确到整秒，无法区分；新构建的步骤在其自己运行的擦除中已处理过这类行。只有 `namespace_list` 才会 purge，而旧构建的 `namespace` 行可能带有被请求时的该标志。

    ```sql
    SELECT org, namespace,
           maxIf(counted_at, NOT purged) AS erased_at,
           maxIf(counted_at, purged) AS purged_at
    FROM (
        -- The census's time: a row promoted after it was never in the erasure's reach.
        SELECT org, ifNull(verified_at, completed_at) AS counted_at,
               scope_type = 'namespace_list' AND purge AS purged,
               arrayJoin(if(scope_type = 'namespace', [scope_value],
                            if(JSONType(scope_value) = 'Array',
                               JSONExtract(scope_value, 'Array(String)'),
                               JSONExtract(scope_value, 'namespaces', 'Array(String)')))) AS namespace
        FROM loom.erasure_requests FINAL
        WHERE scope_type IN ('namespace', 'namespace_list') AND status = 'completed'
    )
    -- Only a namespace that holds a promoted row has anything to clear.
    WHERE (org, namespace) IN (SELECT org, namespace FROM loom.shared)
    GROUP BY org, namespace
    ```

    对每一项，运行新构建的步骤所运行的语句，覆盖在该擦除普查之前提升的每个版本，包括被同一个键的后续提升从 `FINAL` 中隐藏的版本。若设置了 `purged_at`，删除它们：

    ```sql
    ALTER TABLE loom.shared DELETE
    WHERE org = '<org>' AND namespace = '<namespace>' AND promoted_at < '<purged_at>'
    SETTINGS mutations_sync = 2
    ```

    若设置了 `erased_at`，则对残留的存活行打墓碑：

    ```sql
    ALTER TABLE loom.shared UPDATE tombstoned_at = toDateTime('<the time you run it>')
    WHERE org = '<org>' AND namespace = '<namespace>' AND tombstoned_at IS NULL
      AND promoted_at < '<erased_at>'
    SETTINGS mutations_sync = 2
    ```

    该设置会让每条语句等待所有副本执行完成。时间写成字面量，与该步骤的绑定方式一致，因为复制表会拒绝调用 `now()` 这类非确定性函数的 mutation。在账本的 90 天之前请求的命名空间擦除不会被列出，它遗留的已提升行也无法与存活命名空间的行区分开；它们会保持旧构建留下的状态。

15. 升级到每个声明了 org 的 CLI 命令（代码索引命令、导入）都会检查其部署的构建时：无论哪种 authority，其 ClickHouse 用户都需要对 `loom.storage_authority` 拥有 `SELECT` 权限，并且当部署记录的是另一种 authority 时，命令会拒绝执行。在 Postgres authority 下，它还需要 `LOOM_PG_DSN`，并读取单元状态（在 `LOOM_CELL` 未设置时，使用其自己的会话）。设置了 `LOOM_CELL` 时，不为任何 org 写入、但会改变该单元存储的命令（`loom settings set`、`loom secrets reseal`、`loom code compact` 等）或撤销访问权限的命令，会以同样方式检查其部署，并在其自己的会话上读取该单元的 `first` 行。为它所连接的 Postgres 角色授予：
    - `LOOM_CELL` 未设置时：对 schema `loom` 的 `USAGE`，对 `loom.cell_first`、`loom.tenancy_mode` 和 `loom.authority` 的 `SELECT`，对 `loom.cell_self` 的 `SELECT (updated_at)`，以及对 `loom.tenancy` 的 `SELECT (since)`（列级授权使其接触不到指纹和 org），以及不小于 `LOOM_PG_POOL_MAX_SIZE` 乘以同时运行的此类命令数量的 `CONNECTION LIMIT`，因为每个命令都为其写入打开自己的连接池（读取状态时在达到上限时会短暂重试，然后拒绝）；
    - `LOOM_CELL` 已设置时：同上，但对 `loom.cell_self` 和 `loom.tenancy` 授予完整的 `SELECT`，并且为在第一个单元处于 `recording` 时记录新的 org，授予对 `loom.tenancy` 的 `INSERT` 和对 `loom.tenancy_mode` 的 `UPDATE (since)`；当该单元接入时，用 `LOOM_CELL_ROUTER_TOKEN` 撤销这两项。其文档 bucket 设置（`LOOM_DOCUMENT_S3_BUCKET`、`LOOM_DOCUMENT_S3_PREFIX`）与服务器的相同。

    仅持有列级授权的角色在已越过 `recording` 阶段的单元上执行的写入，会被作为权限错误（SQLSTATE 42501）拒绝，而不是 `LT001`。

16. 在首个 cell 执行 attach 之前，每个 CLI 和 CI 镜像都运行已读取该状态的构建，并且每个为组织写入数据的 CLI 都已设置 `LOOM_CELL`。没有 CLI 会发送心跳，因此 attach 无法检查这两项，较旧构建的 CLI 在 `enforced` 的 cell 上也不会被拒绝（[单元](https://github.com/ClickHouse/loom/blob/main/docs/architecture/cells.md#failure-modes)）。

17. 迁移到首个 cell 的进程定时从 router 读取其 cell 状态的构建：每个 router 副本（`loom router serve`）都要先于任何 cell 的进程运行此构建。已准备好的首个 cell 的每个进程，从其第一次读取 cell 状态起，就会请求 router 的状态路由，直到读到 `serving`；早期构建的 router 会在每次这类调用时读取目录：每个服务器进程大约每 `min(LOOM_INSTANCE_HEARTBEAT_SECONDS, LOOM_TENANCY_MODE_CACHE_S)` 读取一次，因此在默认值下，一千个 pod、每个 pod 八个 worker，每秒会产生 1,600 次读取；而此构建的 router 每个副本每 `LOOM_ROUTER_DIRECTORY_CACHE_S` 只读取一次来应答这些请求（[单元](https://github.com/ClickHouse/loom/blob/main/docs/architecture/cells.md#the-heartbeats-keys)）。

迁移应用期间，旧构建继续提供服务：每个迁移都是增量式的。让两个构建并行提供服务的规则见[滚动升级](../architecture/rolling-upgrades.md)。

<a id="rollback"></a>

## 回滚

重新部署上一个镜像；如果是成对部署，使用 `./redeploy.sh --rollback`。在回滚到没有 assistant-turn learnings 的构建之前，先关闭 `memory_extract_agent_learnings`，并删除 `loom.learnings_supporters` 的行以及 `loom.learnings_queue` 中的 `assistant_turn` 行（[提取](https://github.com/ClickHouse/loom/blob/main/docs/features/memory/extraction.md#rules-the-assistant-states-about-itself)）。上一个构建会把已提升的 bot 规则提供给命名空间中的每个 agent，而不只是它所属的 bot。要避免这种情况，请在回滚前用 `knowledge.delete` 删除带有 `source-kind:assistant_turn` 标签的文档；否则就接受更宽范围的提供。上一个构建在命名空间指南存在之后执行命名空间擦除时，会留下该命名空间的指南；等较新的构建重新提供服务后，再执行一次擦除（[滚动升级](../architecture/rolling-upgrades.md)，规则 24）。上一个构建执行命名空间擦除时，会让该命名空间已提升的行保持有效；等较新的构建重新提供服务后，升级步骤中的第 14 步会列出这些行（规则 45）。回滚到没有 bot 令牌上限的构建后，每个 bot 的令牌有效期都是一小时，该构建保存的 bot 会把 3600 作为其上限；等较新的构建重新提供服务后，再重新设置更短或更长的上限（[滚动升级](../architecture/rolling-upgrades.md)，规则 26）。上一个构建执行用户擦除时，会留下由该用户记忆的摘要所产生的内容；等较新的构建重新提供服务后，对每个未带 `purge` 执行的擦除重新发起请求，如步骤 10 所述；已完成的 purge 不会再被找到（规则 37）。

只要有任何目录行指向文档存储桶中的对象，就不要回滚到添加该存储桶的构建之前：更旧的构建对这些下载返回 `410 BLOB_GONE`，它的擦除会删除这些行却无法触及对应的对象，它的清扫也从不遍历该存储桶（[原始文件的存放位置](https://github.com/ClickHouse/loom/blob/main/docs/features/knowledge.md#where-originals-are-stored)）。

如果回滚到 `086_code_answer_memory_scope.sql` 之前的构建，并且有记忆命名空间绑定到 `code.ask`，请按步骤 9 清除答案缓存两次：一次在旧构建单独提供服务时，另一次在较新的构建重新提供服务后。该迁移已经记录在账本中，所以向前滚动时不会应用任何会触发第二次清除的内容。

回滚前先关闭 `LOOM_PG_PAYLOAD_CLEAR_ENABLED`，并在较新的构建重新单独提供服务满一个清除时间窗之后，才能再次开启；一旦执行过清除，只能用带有 `010_revision_delivered_at.sql` 的构建运行 `loom reconcile`，之后在该构建上启动部署，并且只有在较新的构建不再拥有针对已清除修订版本的可认领投递时才回滚，下面的查询会返回 0（规则 43）：

```sql
SELECT count(*) FROM loom.memory_deliveries d
JOIN loom.memory_revisions r USING (org, namespace, seq)
WHERE r.row IS NULL AND NOT d.settled AND d.lease_until <> 'infinity';
```

在结果为 0 之前，让较新的构建去投递它们。上一个构建从不认领已搁置的投递，所以已搁置的投递可以保留。上一个构建提供服务期间，重试会略过已清除的修订版本（[Postgres 运维](https://github.com/ClickHouse/loom/blob/main/docs/operations/postgres.md#what-the-boot-checks)），也不要运行 `loom reconcile`：它会把 reconcile 持有的锁交给上一个构建。

在把已准备好的 cell（带有 `loom.cell_first` 行的 cell）回滚到没有[单元](https://github.com/ClickHouse/loom/blob/main/docs/architecture/cells.md) org 触发器的构建之前，先停止较新构建的所有 pod，然后只运行[未设置 LOOM_CELL 的进程会被拦截](https://github.com/ClickHouse/loom/blob/main/docs/operations/troubleshooting.md#a-process-with-loom_cell-unset-is-gated)中的删除触发器部分，不要运行其中的状态事务（该 cell 保持已准备状态）；如果在回滚结束前有较新的 pod 带着迁移启动，就再运行一次：旧构建的会话不发送租户标记，所以一旦较新的构建创建了触发器和 `loom.tenancy` 守卫，它对该 cell 中没有租户行的 org 的每次写入都会被拒绝（在附加之前的后续 cell 上，则是每个租户 org），它为每个新放置的 org 写入租户行也会被拒绝，而它的其他写入还要承担触发器读取状态的开销。

没有需要撤销的迁移：上一个构建可以读写较新的 schema。它对 `loom.memory` 和其他 `ReplacingMergeTree` 记忆表的写入会保留它不认识的列（[滚动升级](../architecture/rolling-upgrades.md)）。

没有降级迁移。要回到更早的 schema，请恢复升级前做的备份（[备份与恢复](https://github.com/ClickHouse/loom/blob/main/docs/operations/backup-and-recovery.md)）。

<a id="downgrading-an-installer-install"></a>

### 降级安装器安装

每个安装器包都会指明构建时使用的 ClickHouse 版本，所以安装较早的 Loom 版本，也会在较新版本写入过的卷上运行较早的 ClickHouse，而它可能无法读取这些数据。若只回滚 Loom，请保留较新的 ClickHouse：在运行较早的安装器之前（它会立即启动整个栈），先在安装目录的 `.env` 中固定 ClickHouse 当前运行的镜像。下面的命令会打印应写入的那一行，并替换任何已有的 `LOOM_CLICKHOUSE_IMAGE` 行：

```bash
dir=${LOOM_INSTALL_DIR:-$HOME/.loom}
echo "LOOM_CLICKHOUSE_IMAGE=$(docker inspect -f '{{.Config.Image}}' \
  "$(docker compose --project-directory "$dir" ps -q clickhouse)")"
```

然后运行较早版本的安装器并指明其版本，否则较旧的安装器会拒绝覆盖较新的安装：

```bash
curl -fsSL https://github.com/ClickHouse/loom/releases/download/vX.Y.Z/install.sh -o loom-install.sh && LOOM_VERSION=X.Y.Z sh loom-install.sh && rm loom-install.sh
```

请在你的主目录中运行，或在[只有你能写入的其他目录](../getting-started/quickstart.md#fast-path-released-image-no-clone)中运行。

安装器会保留 `.env`，所以这个固定设置在之后的升级中也会让 ClickHouse 保持不变：在安装较新的 Loom 版本之前，删除 `LOOM_CLICKHOUSE_IMAGE` 这一行，并且不要在 shell 中导出 `LOOM_CLICKHOUSE_IMAGE`，因为安装器会把收到的值写回 `.env`。要同时回退两者，请恢复升级前做的备份。

<a id="what-stays-the-same-across-an-upgrade"></a>

## 升级前后保持不变的内容

| 升级前后                         | 保持不变的内容                                                                   | 保证机制                                  |
| -------------------------------- | -------------------------------------------------------------------------------- | ----------------------------------------- |
| 旧版本构建配合新 schema          | 继续读写它所知道的每一张表，因此回滚无需任何 schema 步骤                         | `tests/unit/test_migrations_additive.py`  |
| 已应用的迁移文件                 | 从不编辑或删除；修正内容以新文件提交                                             | 账本校验和；`--dry-run --strict`          |
| 客户端发送了服务器已移除的字段   | 照常处理；该字段会列在 `ignored_args` 中                                         | `server/transport/arg_check.py`           |
| 新旧版本构建同时提供服务         | 各自都能读取对方写入的内容，包括新增的列，但在发布完成后才启用的功能除外（第 8 步） | `make test-mixed-version`                 |

升级可能会更改或移除操作和字段。请通过能力目录（[HTTP API](https://github.com/ClickHouse/loom/blob/main/docs/reference/http-api.md#discovering-what-a-deployment-serves)）查看某个部署提供的内容，并使用 `scripts/codegen/wire_changelog.py <previous> <new>` 列出两个构建之间的变更（[代码生成](../development/code-generation.md#wire-changelog)）。

<a id="related"></a>

## 相关内容

- [部署](https://github.com/ClickHouse/loom/blob/main/docs/operations/deployment.md#zero-downtime-pair)：让升级变成一次切换的成对部署
- [迁移](../development/migrations.md)：新迁移需要遵循的规则
- [滚动升级](../architecture/rolling-upgrades.md)：构建版本 N-1 与 N 的规则
- [故障排查](https://github.com/ClickHouse/loom/blob/main/docs/operations/troubleshooting.md#migrations-pending-or-checksum-drift)
