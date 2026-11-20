<!-- i18n: source=docs/development/migrations.md blob=bbea754e8750b1abe4dfb51af0636ead2246778b model=claude-sonnet-5-5 -->
> 本页由机器从[英文原文](https://github.com/ClickHouse/loom/blob/main/docs/development/migrations.md)翻译，内容以英文原文为准。
> Machine-translated from the [English page](https://github.com/ClickHouse/loom/blob/main/docs/development/migrations.md), which is authoritative.

<a id="migrations"></a>

# 迁移

本页列出编写 migration 的规则。部署如何应用 migration，以及升级和回滚的步骤，见 [升级](https://github.com/ClickHouse/loom/blob/main/docs/operations/upgrades.md#how-migrations-run)。[`schemas/README.md`](https://github.com/ClickHouse/loom/blob/main/schemas/README.md) 说明了 `schemas/` 目录的结构，并列出了运维工具。

<a id="the-journal-and-the-ledger"></a>

## 日志与账本

- schema 是 `schemas/migrations/` 中一组带编号的 SQL 文件构成的日志，由 `server/core/schema/migrate.py` 按数字前缀的顺序应用。`000_migrations.sql` 创建账本；`016_baseline.sql` 是起始 schema；之后的每个文件都是增量。
- 每个已应用的文件都会连同其内容的 SHA-256 记录在 `loom._migrations` 中。重新运行时只应用新文件。文件内容被修改称为**校验和漂移**（checksum drift），而仓库中已不存在的文件称为**仅存在于账本的行**（ledger-only row）；`loom migrate --dry-run --strict`（一个 CI 步骤，也是部署前的预检）在这两种情况下都会失败。文件仅被重命名而文本未变，则不算仅存在于账本：它的 SHA-256 能标识它，它会以新名称被记录下来，而不会再次执行。
- 每个文件都使用 `loom.<table>` 和 `loom_telemetry.<table>` 来指代表。运行器在发送每条语句时，会将它们分别映射到 `LOOM_CH_DATABASE` 和 `LOOM_CH_TELEMETRY_DATABASE`（`server/core/ch_names.py`），因此校验和是针对文件原文计算的。比较数据库时，应按值使用 `currentDatabase()` 或绑定的名称，而不能使用字面量 `'loom'`（`tests/unit/test_ch_names.py`）。如果某个文件修改了名称以 `_` 开头的表的键或 TTL，或者重命名、交换了这样的表，还要同时更新 [存储布局](https://github.com/ClickHouse/loom/blob/main/docs/architecture/storage-layout.md) 中对应的行，以及 `tests/unit/test_storage_layout_rows.py` 中的 reshape 防护。

<a id="numbering"></a>

## 编号

文件命名为 `NNN_description.sql`。运行器按整数前缀而不是按名称排序（`server/core/schema/journal.py`，`migration_order`），因此 `1030_x.sql` 会在 `220_y.sql` 之后运行。请取下一个未被占用的编号，并检查 `main` 和未合并的 pull request，确定后不要再更改：两个文件使用同一编号时仍会被应用，但顺序按后缀的字典序，这并不是任何人预期的顺序，而且通过 rebase 重命名其中一个文件不会重新应用任何内容。`tests/unit/test_migration_numeric_order.py` 会在出现重复编号时失败。

<a id="rules-for-writing-a-migration"></a>

## 编写迁移的规则

- **永远不要编辑或删除已合并的迁移。** 各部署的账本中保存着它的哈希。修正必须新建一个文件。
- **只增不减。** 可以添加表、列、索引、projection 和视图；可以删除 `server/`、`scripts/`、`bench/` 或 `dev/` 下的代码都没有引用的 skip index 或 projection（旧版本只会失去一项优化，不会丢失任何列）；可以更改 codec、默认值或表设置；可以放宽类型；可以扩展枚举。永远不要删除或重命名表或列，也不要交换表。`tests/unit/test_migrations_additive.py` 会强制执行这一点。对于视图和表，它只允许两种情况：当 `server/`、`scripts/`、`bench/` 或 `dev/` 下没有任何文件引用时，对插入时物化视图执行 `DROP VIEW IF EXISTS`，对已被取代的表执行 `DROP TABLE IF EXISTS`：无论是正在服务的版本，还是运维工具（它们针对生产服务运行），都不会读取它。收窄类型需要在评审中判断，因为正则表达式无法把它与放宽类型区分开。
- **分两个版本淘汰一列。** 先发布一个不再读取或写入该列的版本；等到没有任何正在服务的版本引用它之后，才能删除。
- **每个写入方都要带上新列。** 向 `loom.memory` 或 `loom.shared` 添加列时，必须在同一次变更中更新 `server/memory/store/rows.py` 中的行构造函数（`build_memory_version_row`、`build_embedding_row`），以及所有为它们提供数据的投影：这些表是使用 `FINAL` 读取的 `ReplacingMergeTree(version)` 表，缺少某列的版本行会让所有读取方悄悄读到该列的零值。新增的 `loom.memory` 列还必须在同一个迁移中加入 `loom.memory_versions`，并加入 `memory_versions_mv` 视图的 `SELECT`，否则历史记录就不会再记录该列（`tests/unit/test_memory_versions_schema.py` 会比较列清单）。
- **可能保存记忆内容的表** 还需要一个擦除 saga 步骤和一条残留普查行（`server/erasure/store/census.py`，`_CENSUS`）；如果新表不在任何清单中，`tests/unit/test_every_table_is_classified.py` 会失败（[添加表](https://github.com/ClickHouse/loom/blob/main/docs/development/adding-a-table.md)）。
- **与引擎无关的 DDL。** 将向量列声明为 `Array(BFloat16) CODEC(NONE)`；在可以启用 `Quantized` codec 的运行环境中，会以带 codec 的方式创建这些列（[受限设置](../architecture/clickhouse.md#gated-clickhouse-settings)）。使用普通的 `MergeTree` 引擎名称，ClickHouse Cloud 会将其作为 `SharedMergeTree` 运行。

**为什么只增不减。** 迁移运行之前正在服务的版本，在迁移之后仍必须能读写它所知道的每一张表。这样蓝绿回滚只需切换代理，而不必回滚 schema，也让旧的一侧在新的一侧于启动时应用迁移的同时继续提供服务。代价体现在这里：淘汰一列需要两个版本，而变更键时只能重建，不能使用 `ALTER`。

<a id="runner-directives"></a>

## Runner 指令

迁移文件可以携带 runner 会读取的注释指令：

| 指令                                | 作用                                                              |
| ----------------------------------- | ----------------------------------------------------------------- |
| `-- loom: tolerate-error <code>`    | 该文件可以容忍一个指定的服务器错误（例如只有部分服务器接受的某项设置） |
| `-- loom: rebuild-if-empty <table>` | 为 `<table>` 声明目标排序键或分区键（见下文）                     |
| `-- loom: baseline covers N`        | 该文件取代仓库中不再保留的、编号为 N 或更小的所有文件             |

**基线。** 基线的编号为 N，排在它所取代的最新文件之后。全新的数据库会执行它。若数据库的账本中已有文件 N，则只记录基线而不执行，并保留被取代的各行，严格模式会把这些行报告为已被取代，而不是错误。因此合并（squash）无需手工编辑任何账本，而合并之前的构建仍然能看到自己的全部文件都已应用。若数据库的账本中有部分被取代的文件但没有文件 N，说明迁移中途停止：`loom migrate` 会拒绝处理（`stranded`）并且不做任何更改，因为执行基线会跳过账本中缺少的文件，而记录基线又会声称这些文件的 schema 已存在。请用合并之前的构建应用缺失的文件，然后再次迁移。合并应在发布时进行，而不是每次变更都进行；合并时会根据 `schemas/schema.sql`（转储正文，不含所保留文件创建的对象）构建基线，并在全新的服务器、已完全迁移的账本和迁移到一半的账本上验证它。

runner 还会在应用时调整语句，而不改动文件及其校验和：在服务器禁止时，去掉实验性的 NLP 标志；在可以启用 `Quantized` 编解码器的地方，用它创建向量列，在无法启用 `allow_suspicious_codecs` 的地方，去掉链在其后的通用编解码器（`server/core/schema/vector_codec_ddl.py`）；以执行迁移的用户创建所有视图（`DEFINER = CURRENT_USER`：转储中写的是它自己的用户，而另一台服务器可能没有该用户）；并在记录文件之前，等待该文件发出的每个 mutation 完成（`LOOM_MIGRATE_MUTATION_WAIT_S`）。

**预检查。** 如果某个文件在数据中存在它自身无法察觉的内容时不得执行，就在 `server/core/schema/prechecks.py` 中注册一个检查，以去掉编号的文件名作为键，这样文件重新编号后仍保留该检查。runner 在执行文件之前运行检查，试运行也不例外；检查若拒绝，则运行停止并给出其消息，该文件的任何内容都不会执行。`097_mirror_names_reserved.sql` 就使用了一个检查：只要存在已注册的、属于保留镜像形式的命名空间，或者这样的命名空间中仍有活跃行，它就会拒绝执行。

<a id="changing-a-sort-key-or-a-partition-key"></a>

## 更改排序键或分区键

ClickHouse 不支持原地更改这两者，因此迁移只声明目标，表会被重建，而不是被修改：

```sql
-- loom: rebuild-if-empty code_test_facets
CREATE TABLE loom.code_test_facets ( ...every column, unchanged... ) ... ORDER BY (...);
```

这条语句是目标 DDL 的唯一副本。运行器（`server/core/schema/table_rebuild.py`）会把线上表的 `sorting_key` 和 `partition_key` 与它比较：一致则记录该文件；表为空（全新安装）则当场与具有目标 DDL 的孪生表交换；表中有数据则启动失败，并给出点名该工具的提示信息。启动过程从不复制数据行。在有数据的部署上，运维人员需要先运行该工具，一次处理一张表：

```bash
python -P -m scripts.ops.table_rebuild --table code_test_facets --dry-run
python -P -m scripts.ops.table_rebuild --table code_test_facets --yes
```

每个会产生变更的操作（单独的 `--yes`、`--rollback`、`--drop-pre-rebuild`）都会打印它所连接的 ClickHouse 及其计划，没有 `--yes` 则不会做任何更改。该工具以有界批次把每一行（不使用 FINAL）复制到 `loom.<t>_rebuild`，再复制期间新写入的 part，在 FINAL 下检查行数，并检查一批抽样键的最新版本，然后用 `EXCHANGE TABLES` 交换，并把旧表保留为 `loom.<t>_pre_rebuild`，供 `--rollback` 使用，直到执行 `--drop-pre-rebuild`。如果复制期间有 `ALTER` 更改了线上表，它会拒绝交换并保留孪生表：对于 ClickHouse 以 mutation 方式运行的更改（删除、重命名或更改列类型，删除索引，TTL），退出码为 6；对于只改变其元数据时间的更改（索引、设置、列的默认值或编码），退出码为 3。新增列会使复制失败（退出码 1，不交换），或者让运行在最后一轮验证之后以退出码 3 结束。重建不会沿用 `ALTER`：请在目标语句中重新声明它，然后用 `--restart` 重新运行。它也会拒绝处理没有任何迁移声明过的表。当已声明的表仍以旧布局保存着数据行时，`loom migrate --dry-run --strict` 会失败，因此部署预检能发现被跳过的步骤。

目标保留该表的各列（因此交换是增量式的），其排序键保留旧键的各列，只是重新排序：这个键同时也是 `ReplacingMergeTree` 合并所依据的键。包含其他列的键会合并其他行，并连同原因列在 `tests/unit/test_migrations_additive.py` 中。请在发布说明中把这次重建写成运维步骤（[发布流程](https://github.com/ClickHouse/loom/blob/main/docs/development/release-process.md#release-notes)）。

<a id="postgres-migrations"></a>

## Postgres 迁移

`schemas/pg_migrations/NNN_*.sql` 遵循相同的规则：按编号排列，只做增量变更，合并后不再编辑，并且每一条针对表的 `CREATE`、`ALTER` 或 `DROP` 都要写明 `loom.<table>`，唯一的例外是重命名后的新名称，因为 Postgres 在那里不接受 schema（见 `tests/unit/test_storage_layout_rows.py`）。启动时会在 Postgres advisory lock 下应用这些迁移，每个文件连同其账本记录在同一个事务中执行。迁移等待表锁最多 5 秒（`lock_timeout`，超时后重试），因此 `ALTER` 不会让该表的所有查询都排在一个长事务后面。如果账本中包含更新版本的文件，当前版本仍会正常提供服务，并给出点名这些文件的警告；如果账本中有当前版本不包含的文件，其编号等于或小于当前版本的最后一个文件，或者某个文件的校验和发生了变化，则拒绝启动。

每个文件都在一个事务中运行，所以迁移不会在大表上建立索引：建立索引会阻塞该表的写入，直到完成为止，期间每次提交都会返回 503。这样的索引需要在事务之外使用 `CREATE INDEX CONCURRENTLY`，而等待 advisory lock 的启动过程会与之死锁（等待中的会话持有一个快照，而索引构建要等它结束）：因此，新增这类文件的变更还要通过重试 `pg_try_advisory_lock` 来获取该锁。如果首个写入某张表的版本同时带有该文件，那么无论文件在何处运行，这张表都很小，其索引可以在该文件的事务中建立（`010_revision_delivered_at.sql`）。

<a id="schema-dump-and-check"></a>

## Schema 导出与检查

`schemas/schema.sql` 是应用所有迁移后导出的结果：可以通过阅读它来了解当前的结构，但不要手动编辑。修改迁移之后，请针对 CI 所测试版本的全新 ClickHouse（`clickhouse/clickhouse-server:latest`）重新生成它：

```bash
bash dev/ci_start_clickhouse.sh     # that image with Loom's server config
make migrate
make schema-dump                    # rewrites schemas/schema.sql only when the dump differs
make schema-check                   # exit 1 with the head of the diff; writes nothing
```

导出失败时，文件保持原样。使用其他版本的 ClickHouse、无法启用 `Quantized` 编解码器或其 `LZ4` 链的服务器，或者包含重建副本表（rebuild twins）的数据库，导出的结果都会不同，因此需要固定使用该容器。内部表（`_migrations`，以及名称以 `_` 或 `.` 开头的表）不会被导出。CI 的 `schema-check` 任务会在全新的服务器上重放所有迁移，运行严格的试运行（dry run），然后执行 `make schema-check`。

CI 跟随 `latest`，因此如果新发布的 ClickHouse 输出 DDL 的格式有所不同，即使 Loom 没有任何改动，`schema-check` 也会变红。请查看该任务打印的差异：如果只是格式变化，就运行上面的四条命令（它们会拉取新版本），并将 `schemas/schema.sql` 单独提交。如果是含义发生了变化（类型、默认值、引擎设置），这属于 ClickHouse 的行为变更，在覆盖导出之前应先调查；`LOOM_CLICKHOUSE_IMAGE=clickhouse/clickhouse-server:A.B.C.D` 会在最近一次能干净导出的版本上运行上面的命令，以便对比。

<a id="checklist"></a>

## 检查清单

1. 取下一个可用的编号；以增量方式编写文件。
2. 如果变更涉及行构建器、`loom.memory_versions`、擦除普查（erasure census）和分析器用例，请一并更新。
3. 针对本地 ClickHouse 运行 `make migrate`，然后运行 `make schema-dump`，并将 `schemas/schema.sql` 与迁移一起提交。
4. 运行 `make test-unit`（增量、编号和 schema 分类测试），并针对读取新结构的路径运行集成测试套件。
5. 记录所有需要运维人员执行的步骤（重建、回填），以便写入发布说明。

<a id="related"></a>

## 相关内容

- [升级](https://github.com/ClickHouse/loom/blob/main/docs/operations/upgrades.md)：runner 如何在部署上应用文件
- [ClickHouse](../architecture/clickhouse.md)
- [Schema 参考](https://github.com/ClickHouse/loom/blob/main/docs/reference/schema.md)
