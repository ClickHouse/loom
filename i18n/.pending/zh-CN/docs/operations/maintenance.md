<!-- i18n: source=docs/operations/maintenance.md blob=5df99557fe7d3a4299c8ea5cc297b48e586731cc model=claude-sonnet-5-5 -->
> 本页由机器从[英文原文](https://github.com/ClickHouse/loom/blob/main/docs/operations/maintenance.md)翻译，内容以英文原文为准。
> Machine-translated from the [English page](https://github.com/ClickHouse/loom/blob/main/docs/operations/maintenance.md), which is authoritative.

<a id="maintenance"></a>

# 维护

这里的每条命令都是 `loom` 子命令，请在带有该部署环境变量的容器内运行（`docker exec loom-server loom …`，拆分部署时使用 `loom-workers`）。[CLI 参考](https://github.com/ClickHouse/loom/blob/main/docs/reference/cli.md)列出了所有标志。代码索引有单独的运维手册：[代码索引运维](https://github.com/ClickHouse/loom/blob/main/docs/operations/code-index-operations.md)。

<a id="before-you-run-a-write"></a>

## 执行写入之前

下面的大多数命令**默认都是试运行（dry run）**。先阅读输出，用 `--org` 和 `--namespace` 限定范围，然后再传入写入标志（`--apply`，擦除操作则使用 `--erase`；各表中列出了对应的标志及例外情况）。提交变更（`ALTER … UPDATE`）的命令会在 ClickHouse 实际应用该变更之前就返回，请通过 `system.mutations` 查看进度。

<a id="re-embed-and-re-extract"></a>

## 重新嵌入与重新提取

- **`loom reembed`** 会重新嵌入 `embedding_model` 与当前配置的 embedder 不一致的记忆、实体和对话向量。Loom 不会自行重新嵌入，因此请在修改 `LOOM_EMBEDDING_MODEL`（以及 `LOOM_EMBEDDING_DIM`）之后运行它。用 `--org` / `--namespace` 限定范围；`--dry-run` 只统计行数；`--delete-old` 在完成后删除旧模型的行；`--key-only` 只重建每个有效行的键轴向量，但导入键（`import:` 加摘要）除外，它没有键轴向量：没有键轴向量的行会被跳过，旧版本已嵌入的则会被清除。`--in-database <named collection>` 使用 ClickHouse 的 `aiEmbed`（需要 ClickHouse 26.7 或更高版本）在 ClickHouse 内部执行嵌入，每张表一条 `INSERT … SELECT`，而不是 Python 循环。如果设置了客户或身份请求头，或要求必须使用推理网关，该选项会被拒绝：ClickHouse 自己发出的调用不带客户或身份信息，也会跳过网关的主机检查。Python 路径按排序键顺序每次读取 `--batch-size` 行，并在每页之后输出一个 `resume:` 令牌；`--resume <token>` 从该处继续被中断的运行。对于文本被判定为高敏感度的对话轮次，Python 路径绝不会将其发送给 embedder：该行保留在旧模型上，搜索不会读取它，直到 `--delete-old` 将其删除。记忆行的向量遵循其敏感度标签。`--in-database` 无法检查文本，会同样重新嵌入这样的对话轮次；搜索仍会将其屏蔽（[敏感度](https://github.com/ClickHouse/loom/blob/main/docs/architecture/security.md#sensitivity)）。
  - 在每页插入之前，Python 路径会按键再次读取该页的行。自该页被读取以来已被删除、标记删除，或文本、agent 或用户已变化的行会被跳过。其他每一行都按第二次读取时的其他列写入。
  - 当被跳过的行是旧模型的行，且仍以其他文本或身份保存时，`--delete-old` 会保留其所属 org 的旧模型行留待下次运行，下次运行会重新嵌入该 org 的所有旧模型行（用 `--org` 限定范围）。`resume:` 令牌记录了到目前为止被保留的 org，运行会在删除之前列出它们。
  - 如果删除、脱敏或擦除发生在该次读取与插入之间，或其变更尚未作用到该行，该行可能仍以新向量保持有效。运行结束后请再次执行这类脱敏，并再次删除这类对话轮次或对话线程；孤立向量清理会将已删除记忆残留的向量标记删除，擦除的验证步骤会找到该行并重试。`--in-database` 在语句开始时对每张表只读取一次，因此请在没有脱敏或擦除操作时运行它。
- **其他宽度的模型。** 在 ClickHouse 26.8 或更高版本上，若可以开启 `enable_quantized_codec`，全新安装会对其向量列使用编码（`Quantized`）（[ClickHouse](../architecture/clickhouse.md#gated-clickhouse-settings)），而编码后的列只接受创建时的宽度的向量。要改用其他宽度（8 的倍数）的模型，请在“设置 → 模型与密钥”（Settings → Models & keys）中更改，这会以新宽度重建向量表；`loom reembed` 会拒绝表不接受的宽度，只修改 `LOOM_EMBEDDING_DIM` 会使每次向量写入都返回 503 `CODED_WIDTH_MISMATCH`，同时 `/v1/health` 报告 `coded_width_mismatch`。
- **`loom rechunk --org ORG --namespace NS`** 会根据存储的内容，通过 `knowledge.upsert` 重新处理每个有效文档，使文档采用当前的分块、分块上下文和向量（用 `--source-uri` 处理单个文档）。在加上 `--apply` 之前只是试运行。
- **`loom lexical-stats-rebuild --org ORG --namespace NS`** 根据有效行重新计算 BM25 文档频率。词频视图只能看到插入操作，因此取代、标记删除和批量删除会留下过时的词表，直到运行此命令为止（`--no-memory` / `--no-knowledge` 可跳过其中一侧）。它会立即写入。
- **`loom reextract --org ORG --namespace SRC --into DST`** 会把某个命名空间中已存储的 `set_from_messages` 对话记录，通过正在运行的服务器当前的提取器重放到另一个命名空间（`--token` 是 `DST` 的写入令牌，否则使用 `$LOOM_API_KEY`，再否则使用 `~/.loom/token`）。源命名空间只读，因此可以在切换读取方之前对比两个命名空间。`--dry-run` 列出会话；`--limit-sessions N` 重放一个样本。
- **`loom recalibrate`** 会重新探测 embedder，并立即重新计算自适应相似度阈值；否则 `recalibration` worker 每周执行一次。

重新嵌入需要手动进行，因为新的向量空间会改变每一个相似度分数。手动运行可以把切换、成本以及搜索质量的重新基线化放在运维人员的同一步操作中。

<a id="backfills-and-repairs"></a>

## 回填与修复

| 命令                          | 作用                                                                                                                                                      | 写入标志 |
| -------------------------------- | --------------------------------------------------------------------------------------------------------------------------------------------------------- | ---------- |
| `loom integrity-repair`          | 输出完整性指标及其对应的行；重新写出 `dim0_with_vector` 行，使搜索能够看到它们（漂移行和 `valid_to` 行仅列出）    | `--apply`  |
| `loom sensitivity-sweep`         | 统计敏感度检测器会为其打标签的、尚无标签的有效行；写入时会为这些行打上标签，并将 `high` 行的嵌入行标记为墓碑                        | `--apply`  |
| `loom reconcile-status`          | 重新写出状态与 `loom.memory` 不一致的 `memory_embeddings` 行                                                                                | `--apply`  |
| `loom demote-activity-narration` | 找出仅叙述工具活动、没有持久结果的已提取事实，并通过 `memory.apply` 将其降级或打标签；从不删除                 | `--apply`  |
| `loom namespaces-adopt`          | 为存在有效记忆但没有注册表行的命名空间补上所有者行                                                                                        | `--apply`  |
| `loom namespaces-gc`             | 按前缀查找空闲的基准测试命名空间（或使用 `--worker-pass` 运行一次 `namespace_gc` 处理）；提交对它们的擦除请求                                   | `--erase`  |
| `loom audit-namespace`           | 报告命名空间的使用情况（精确键读取与搜索、过大或被重复编码的值、版本变动）；只读；出现严重问题时退出码为 1 | 只读  |

这些修复所对应的完整性指标列在[监控](https://github.com/ClickHouse/loom/blob/main/docs/operations/monitoring.md#integrity-invariants)中。如果某个命名空间中的行形态有误（文档被存为记忆，租约被存为事实），请按照[优化命名空间](https://github.com/ClickHouse/loom/blob/main/docs/guides/optimize-namespace.md)中的指引逐步处理。

<a id="memory-ingest-drops"></a>

## 记忆写入丢弃

写入管道在每个丢弃、截断或放弃处理某条数据的位置都会记录一次丢弃（`server/memory/app/drops.py`，`record_drop`）。丢弃记录先在进程内缓冲，再刷新到 `loom.memory_drops`（保留 30 天），同时同步到计数器 `loom.memory.drops{stage, reason}`。记录丢弃从不抛出异常，也不会阻塞引发它的写入。

```bash
docker exec loom-server loom drops --namespace NS [--org ORG] [--hours 24]
```

该命令按 `stage` 和 `reason` 输出所选时间窗口内的计数，并给出每一组最后一次出现的时间；`SELECT * FROM loom.memory_drops WHERE namespace = '…' ORDER BY ts DESC` 可查看具体条目（`item` 保存键名或字段名，不会用作指标标签）。stage 包括 `extract.parse`、`extract.judge`、`extract.truncate`、`write`、`entity.extract`、`consolidate.merge`、`records.field`、`chunk.split` 和 `conflict.deferred`。

完全失败的提取不算丢弃：`set_from_messages` 会返回 502 `EXTRACTION_FAILED`，部分失败则出现在响应的 `pipeline_errors` 中。

<a id="entity-graph-maintenance"></a>

## 实体图维护

`loom.entity_edges` 的写入方会将词汇层面的噪声（代词、感叹词、轻动词、缩略形式的残片、两字符的 token）从图中过滤掉。

如果某个命名空间的图比其记忆存活得更久，可以用 `loom erasure-re-verify --org ORG --orphan-graph` 列出该命名空间，并用 `--erase` 清除。

<a id="erasure"></a>

## 擦除

| 调用                                                                            | 效果                                                                                                                                       | 适用场景                                                             |
| ------------------------------------------------------------------------------- | ------------------------------------------------------------------------------------------------------------------------------------------ | -------------------------------------------------------------------- |
| `memory.clear` (`ADMIN`)                                                        | 将命名空间中某个 `user_id` 或 `agent` 的记忆标记为墓碑；`confirm_subject` 必须重复范围值                                                   | 清理、测试重置；可通过 `memory.restore` 恢复                         |
| `erasure.request`，`scope_type` 为 `user_id`、`namespace` 或 `namespace_list` | 一个可恢复的 saga，遍历所有可能保存该主体内容的 Loom 表，然后处理已配置的外部后端（Langfuse、ClickStack）                                   | 擦除用户的数据（保留审计记录）；下线命名空间                         |

saga 会在 `loom.erasure_requests`（`ReplacingMergeTree`，读取 89 天内的行，90 天时删除）中为每一步设置检查点。完成各步骤后，它会对读取路径涉及的每张表执行残留普查；范围内仍有任何行，saga 即失败，最后一行包含 `residue_count`、`residue`、`verified_at` 和 `census`。无法确认删除的后端会使 saga 以失败关闭；`erasure_recovery` worker 会幂等地重试，直到 `max_attempts`，之后将其保持为 `failed`。`purge: true` 会硬删除而不是标记墓碑（用于一次性命名空间；没有恢复窗口）。`external: false` 会跳过外部后端，且仅接受用于 `namespace_list`。擦除承诺什么、不承诺什么，请参阅[保留与擦除](https://github.com/ClickHouse/loom/blob/main/docs/features/memory/retention-and-erasure.md)。

```sql
SELECT id, scope_type, scope_value, status, current_step, attempts, failure_reason, residue_count
FROM loom.erasure_requests FINAL
WHERE status != 'completed' ORDER BY requested_at DESC LIMIT 20;
```

`loom erasure-re-verify --org ORG` 会打印每个失败 saga 的最后失败原因，以及对其范围重新执行的普查，该操作为只读（用 `--id` 指定单个 saga）；用户 saga 的普查使用其请求时的 `purge`。`--resume` 会以同样的方式从检查点重新运行失败的 saga，不受恢复 worker 的尝试次数预算限制；如果某一步所负责的表在上次普查中发现仍有行，即使检查点将其列为已完成，该步骤也会再次运行。`--skip-external`（与 `--resume` 一起使用，仅限失败的 `namespace_list` saga；与 `--completed-since` 一起使用会被拒绝）会在外部后端持续失败时完成 Loom 表的处理。`--completed-since <ISO timestamp>` 会选取普查运行时间不早于该时间的已完成 saga，而不是失败的 saga（除非时间戳带有偏移量，否则按 UTC 处理）：用于上线之后，处理上一版本执行的擦除。对这些 saga，它只统计并在使用 `--resume` 时删除 saga 范围内的孤立行链接（即 `entity_triple_rows` 和 `entity_edge_rows` 中，其记忆没有存活行、且在该 saga 普查之后没有写入行的那些行）；它不会重新运行任何 saga 步骤，因此擦除之后写入的数据会保留，即使 `memory.delete` 已将其标记为墓碑。如果 saga 无法恢复，请再次发出相同的 `erasure.request`；该操作是幂等的。

<a id="clickstack-erasure-index"></a>

### ClickStack 擦除索引

ClickStack 环节会按属性值匹配并删除 OTel 行。ClickStack schema 为每张 OTel 表的属性 map 创建了 bloom-filter 索引，`schemas/otel/clickstack_otel_tables.sql` 创建这些表时也带有该索引。若 `otel_metrics_*` 表（由其他安装程序创建）没有 `mapValues(Attributes)` 索引，每次擦除都会对其进行全表扫描。`loom doctor` 和 `/v1/health` 会将缺失的索引报告为 `otel_erasure_index`。`CREATE TABLE IF NOT EXISTS` 不会为已有的表添加该索引，因此需要自行为每张表添加一次：

```sql
ALTER TABLE default.otel_metrics_histogram
  ADD INDEX IF NOT EXISTS idx_attr_value mapValues(Attributes) TYPE bloom_filter(0.01) GRANULARITY 1;
ALTER TABLE default.otel_metrics_histogram MATERIALIZE INDEX idx_attr_value;
-- repeat for otel_metrics_sum and otel_metrics_gauge (and the other metrics tables if populated)
```

在 ClickHouse Cloud 上，紧跟在 `ADD INDEX` 之后执行的 `MATERIALIZE INDEX` 可能什么也不会构建。请检查该表在 `system.data_skipping_indices` 中的 `data_compressed_bytes`，如果为 0，则重新执行 `MATERIALIZE`。

<a id="retention"></a>

## 保留策略

Loom 会保留每一行数据，直到写入方将其删除。默认情况下不会按时间移除任何数据；保留指标（`loom.retention.*`）只统计候选行。

- **按表。** 对 `audit_log`、`agent_events`、`memory`、`memory_embeddings`、`conversations`、`chunks` 和 `documents`，可设置 `LOOM_RETENTION_<TABLE>_DAYS=N`。
- **按命名空间。** 在命名空间注册（`namespace.register`）中设置 `retention_days`；受管表的 TTL 会变成一个覆盖所有设置了该值的命名空间的 `multiIf`。
- workers 的 leader 在启动时执行 `MODIFY TTL`。此后只有 `retention_reconcile` worker 会写入表的 TTL，因此策略变更会在 `LOOM_RETENTION_RECONCILE_INTERVAL_SECONDS` 内生效到各表，而一批集中注册也不会使同一张表被逐次重写。

热/温分层使用 ClickHouse 的存储策略：设置 `LOOM_TIERING_<TABLE>_WARM_DAYS=W`，并配合 `LOOM_TIERING_WARM_VOLUME_NAME`，其值为你的 `storage_policies` 中某个卷的名称。

少数运维表带有各自固定的 TTL，因为它们属于诊断数据，而不是记忆：`loom.memory_drops`（30 天）、`loom.worker_status`（30 天）和 `loom.erasure_requests`（90 天；其读取在 89 天时停止）。[表结构参考](https://github.com/ClickHouse/loom/blob/main/docs/reference/schema.md)列出了每张表的 TTL。

<a id="compaction-and-optimize"></a>

## 压缩与 OPTIMIZE

`loom.memory` 和 `loom.shared` 是 `ReplacingMergeTree(version)` 表：每次更新、墓碑标记和恢复都会写入一个新版本，后台合并会将这些版本折叠。读取方使用 `FINAL`，因此未折叠的版本只会增加读取时间，不影响正确性。`loom.memory` 上较大的 `count() / uniqExact(memory_key)` 比值表示存在大量未折叠的版本；在大规模擦除之后，可以等待合并赶上进度，或在非高峰时段运行 `OPTIMIZE TABLE loom.memory FINAL`。

代码索引的向量表有一个先测量再合并的命令：

```bash
docker exec loom-workers loom code compact                     # measure copies per key
docker exec loom-workers loom code compact --table all --apply # OPTIMIZE … FINAL where the ratio is reached
```

当代码向量表（默认为 `code_chunks_prefix`，也可以是 `code_chunks`、`all`，或 `LOOM_CODE_VECTOR_TABLES` 中的某张表）每个键的物理副本数达到 `--min-ratio`（默认为 `LOOM_CODE_COMPACT_MIN_RATIO`，即 1.5）时，该命令会对其运行 `OPTIMIZE … FINAL`，并等待合并完成（`--timeout-s`）。`code_chunks` 是代码密集型部署中最大的表，请在事先通知的维护窗口内压缩它。批量处理在写入 `LOOM_CODE_COMPACT_AFTER_BULK_ROWS` 行后会自行压缩。

**更改表的排序键、分区键或向量编解码器**需要重建，绝不能用 `ALTER`：`scripts/ops/table_rebuild.py` 和 `scripts/ops/vector_codec_rebuild.py` 会复制到一张孪生表，检查后用 `EXCHANGE TABLES` 交换，并保留旧表以供 `--rollback` 使用。两者都会打印执行计划，未加 `--yes` 时不会改动任何内容。排序键和分区键的步骤见[迁移](../development/migrations.md#changing-a-sort-key-or-a-partition-key)，向量编解码器的步骤见[推荐设置](https://github.com/ClickHouse/loom/blob/main/docs/operations/deployment.md#recommended-clickhouse-settings)。

`vector_codec_rebuild.py` 需要一个维护窗口，期间所有写入方和擦除操作都已停止，且所有进行中的写入都已排空。它从不修复在线表。复制之前，只要源表上仍有变更（mutation）在运行，或变更是在最近一分钟内创建的，它就会拒绝执行；这一分钟用于覆盖副本之间的时钟偏差，交换前的检查也会把它计算在内。请在最后一个变更完成一分钟后重新运行。使用 `--reuse` 时，它会统计自被复用的孪生表创建以来的每个变更，包括已完成的：这样的变更会使孪生表过期，此时重新运行应使用 `--restart --yes`，即重新复制。如果孪生表的指定列、粒度或排序键与本次运行将创建的孪生表不同（使用了另一个 `--method`、`--index-granularity` 或 `--chain`，或是另一个工具的孪生表），`--reuse` 同样会拒绝：检查只读取存储的向量和编码，发现不了这种差异。空表也会被重建，宽度取下面“宽度”一项给出的值（编码列自身的宽度，否则为 `LOOM_EMBEDDING_DIM`）；召回检查在空表上没有可探测的行。其 ClickHouse 用户需要该表上的 `SYSTEM FLUSH ASYNC INSERT QUEUE` 和 `SYSTEM SYNC REPLICA` 权限；在 `ReplicatedMergeTree` 上，还需要 `system.zookeeper` 和 `system.replication_queue` 上的 `SELECT`（`loom ch-setup` 不会授予），以及孪生表 `loom.<table>_rebuild` 上的 `SYSTEM SYNC REPLICA`（检查在每次读取它之前都会同步它），还有 `loom.<table>_pre_rebuild` 上的该权限，这是交换时创建的回滚副本，交换之后的检查以及 `--drop-pre-rebuild` 会同步它。预检会运行前两项一次，任一失败就会拒绝执行（退出码 3）并指出错误；其余几项只有在对应的表存在之后才能运行，孪生表同步被拒绝会使检查失败（退出码 5）。`--dry-run` 同样会运行前两项，因此会将排队的行落入表中，并在副本滞后时等待同步，除此之外不写入任何内容。检查失败（退出码 5）时，请停止写入和擦除，并使用 `--restart` 重新运行；例外情况是表中有行但没有可探测的真实向量：重新运行会以同样的方式失败，此时应删除孪生表并跳过该表，直到它包含 embedding 为止，如运行输出中的 `next` 所述。宽度被拒绝（退出码 3）时，需要按下文重新做 embedding。

在重建完成或被放弃，并且每个 `<table>_rebuild` 孪生表和 `<table>_pre_rebuild` 回滚副本都已移除之前，请保持写入方和擦除处于停止状态；交换成功后会在日志和摘要行的 `next` 中把这一点作为下一步打印出来。每个指明了命令的下一步都带有本次运行的 `--method`、`--chain`、`--index-granularity`、`--gate-fetch-multiplier`、`--probes`、`--require-patch-free`、`--cluster`、`--statement-timeout-s`，以及在不是默认值时的 `--abort-file`（孪生表的记录就在它旁边），因此可以照原样运行。验证或回滚之后，使用 `--drop-pre-rebuild --yes` 移除回滚副本；只有当对该表那一行执行 `--dry-run` 打印出以 `not swapped` 开头的下一步时（即保留下来供检查的孪生表，或从未被交换进来的遗留表），才可使用 `DROP TABLE loom.<table>_rebuild SYNC` 移除被放弃的孪生表。当它指明 `RENAME TABLE loom.<table>_rebuild TO loom.<table>_pre_rebuild`，或要求比较这两张表时，请先照做，不要删除任何东西：需要重命名意味着交换已执行但重命名没有，此时 `<table>_rebuild` 是旧表，在重命名执行之前，对该表那一行的每次运行都会以退出码 3 拒绝（包括带 `--yes`、`--reuse` 或 `--restart` 的同一行），`--drop-pre-rebuild` 也一样。工具是从两张表的每个向量列读取这一信息的，而不是根据运行的参数，因此换用另一个 `--method`、`--chain`、`--index-granularity` 或 `--columns` 重新运行同样会被拒绝：在带有编码向量列的在线表下面，一张无法确认为遗留表的孪生表，可能就是旧表。当在线表上每个已编码的列在孪生表上都是普通列时，工具会指出该执行的重命名。当有某列在孪生表上同样已编码（`--require-patch-free` 运行，或重建已编码的表）时，编解码器无法显示交换是否发生过：工具会拒绝，不重命名也不删除，并要求在手动删除任一张表之前先比较两张表的行。如果孪生表的排序键或分区键与在线表不同，它可能是 `scripts/ops/table_rebuild.py` 交换出来但尚未重命名的旧表，因为该工具使用同样的孪生表名称：每一行都会拒绝，不删除任何内容，并指明 `python -P -m scripts.ops.table_rebuild --table <table> --dry-run`，其报告会给出需要执行的重命名。从未被交换进来的孪生表是遗留表，无论重新运行指定了什么布局：`--restart --yes` 会重新复制，或者直接删除它。当孪生表的 UUID 和在线表的 UUID 与工具创建孪生表时记录在 `$LOOM_REBUILD_DIR/<table>_rebuild.uuid`（即 abort 文件所在目录）中的一致时，工具就知道该孪生表从未被交换进来，这适用于任何数据库引擎：任何一次交换都会让在线表的名称换上另一个 UUID，包括从另一个目录运行的 `--reuse` 交换，那会把记录遗留在原处。工具还会在自己的交换之前移除该记录，无法移除时会拒绝交换。没有匹配的记录时，如果孪生表的 `system.tables.metadata_modification_time` 晚于在线表的，工具就把它视为从未被交换进来；这在 Atomic 和 Replicated 数据库上成立，因为 `EXCHANGE TABLES` 会保留每张表自己的时间，而且交换会拒绝（退出码 3，孪生表保留）元数据时间晚于孪生表的在线表：在检查期间对在线表执行的 `ADD INDEX` 或 `MODIFY SETTING` 不会产生变更，但会让旧表在半途交换之后成为较新的一张。遇到这样的 ALTER 后，请使用 `--restart` 重新运行。时间相等（该列的精度为一秒）以及任何其他数据库引擎都无法说明问题，此时工具会把孪生表视为可能是旧表：每次都请从同一个 `LOOM_REBUILD_DIR` 运行该工具。`--drop-pre-rebuild` 会先再次比较两张表，所用的排空步骤与后置条件相同，两者都已处于目标布局时（对已在目标布局的表做 `--require-patch-free` 重建）也一样，只要出现[交换之后的检查发现的内容](#what-the-checks-after-a-swap-find)中的任何差异，它就会拒绝（退出码 3）并保留副本。当两者都在目标布局上时，`--rollback` 和 `--drop-pre-rebuild` 把元数据时间较晚的表当作新表，仅限 Atomic 和 Replicated 数据库：时间相等或其他任何引擎上，两者都会拒绝（退出码 3）并保留两张表，以便手动比较。擦除 saga 不涵盖这些副本。

- **宽度。** 普通列的目标宽度是 `LOOM_EMBEDDING_DIM`，因此请使用部署所用的环境变量运行该工具；编码列则自行声明宽度。`LOOM_EMBEDDING_DIM` 必须等于所服务模型的宽度：写入方使用服务器在启动时从模型探测到的宽度，过期的 `LOOM_EMBEDDING_DIM` 只会在服务器日志中产生一行 `embedding dim mismatch`。只要某个指定的普通列中含有另一种宽度的真实向量（并非全零），工具就会拒绝（退出码 3），`--dry-run` 也一样：复制会用零向量替换它们，而编码列会拒绝任何其他宽度的写入。消息会指出每种宽度和 `LOOM_EMBEDDING_DIM`。当所有真实向量只有同一种其他宽度，并且服务器日志指出它就是模型的宽度时，请把 `LOOM_EMBEDDING_DIM` 设为该宽度并重新运行；不要重新做 embedding。否则请重新做 embedding 到 `LOOM_EMBEDDING_DIM` 再重新运行：对 `memory_embeddings`、`entity_vectors` 和 `conversation_embeddings` 使用 `loom reembed --delete-old`，它只对 `embedding_model` 不同的行重新做 embedding，因此同一模型下的宽度变更要通过 Settings → Models & keys 处理；其他任何表，例如 `memory_chunks`、`code_chunks` 或 `code_summaries`，由它自己的写入方重新做 embedding。
- **检查（退出码 5）。** 一致性检查会比较每个键的最新版本（对没有版本列的表，则比较其行数）以及每一列的哈希，其中源表被重建的向量列按复制时写入的方式进行转换和填充。孪生表缺少的键，或其版本较旧的键，会逐键重新复制，若仍有键不同则检查失败。只有孪生表持有的键也会使检查失败：复制之后源表中的行被删除了，而交换会把它们带回来。召回检查只对真实向量排序，因为占位向量的余弦距离是 NaN。
- **共享同一个键和版本的行。** 在 `FINAL` 下，ReplacingMergeTree 会在一个键最新版本的各行中保留最后插入的那一行，而复制时并行的 `INSERT` 可能以另一种顺序插入它们，因此无论复制多少次，孪生表都可能保留不同的行（在以 `DateTime` 作版本的表上，同一秒内对一个键的两次写入就足以触发）。当某个键仍然不同时，检查会统计源表上这样的键，并在消息中指出它们。请在窗口内对源表运行 `OPTIMIZE TABLE loom.<table> FINAL`，这样每个键只剩一行，然后使用 `--restart` 重新运行。
- **交换之前。** 工具会刷新源表的异步插入队列（在复制引擎上还会运行 `SYSTEM SYNC REPLICA`，它只同步应答的那个副本；在 `ReplicatedMergeTree` 上使用 `LIGHTWEIGHT`，它等待拉取而不等待合并）。对于 `ReplicatedMergeTree`，整个运行过程中请直接连接到某一个副本。之后每次一致性和宽度读取，还会在同一条语句中读取：应答的副本是否已复制它所读各表的整个复制日志，并且其队列中除合并或变更之外没有其他条目。尚未拉取一条日志的副本，与日志为空的副本报告的日志指针同样为零，因此运行会读取 `system.zookeeper` 中日志的条目，只有日志没有条目时才接受零值；其用户需要 `system.zookeeper` 和 `system.replication_queue` 上的 `SELECT`，无法读取其中任一项的运行会像在落后的副本上读取一样被拒绝。在尚未追上的副本上读取，会在预检中拒绝运行（退出码 3），使交换前的检查失败（退出码 5），并使交换后的后置条件失败（退出码 7）。之后的每次检查读取以及最后的增量都使用 `select_sequential_consistency = 1`。在 `SharedMergeTree` 上，即使由另一个副本提供读取，这也能覆盖已确认的写入。在 `ReplicatedMergeTree` 上，该设置只覆盖 `insert_quorum_parallel = 0` 的仲裁插入，不能取代直接的副本连接。宽度和一致性检查会在以下情况使检查失败：删除、另一种宽度的写入、增量无法拾取的写入（版本低于孪生表的最高版本，或在没有版本列的表上，写入了孪生表已有的键下的一行），以及任何保持键和版本不变但改变了行的列的更改，包括被重建的向量，例如轻量级 `UPDATE` 或擦除墓碑，无论它的补丁部分是否仍在。任何其他插入都会被复制进来，交换继续进行：让写入方不进入的是维护窗口，而不是工具。检查还会在任何源表变更仍在运行或在复制开始之后创建时失败（使用 `--reuse` 时，则是自孪生表创建以来），并为副本时钟减去一分钟。
- **交换之后（退出码 7）。** 工具会再次运行一致性检查，以回滚副本作为源、新表作为孪生表，读取设置和副本检查与最后一次检查读取相同，并统计两张表自复制以来的变更。在 `ReplicatedMergeTree` 上，它会先对回滚副本运行 `SYSTEM SYNC REPLICA ... LIGHTWEIGHT`，该副本有自己的复制队列：在交换之前到达另一个副本的写入，在重命名之后属于该副本。使用 `LIGHTWEIGHT` 是因为交换会停止该副本的合并，完整同步会等待一个合并条目。合并不会改变任何一张表的行，因此通过。任何差异都会使后置条件失败并保留回滚副本；它打印的下一步见[交换之后的检查发现的内容](#what-the-checks-after-a-swap-find)。

`--statement-timeout-s`（默认 1800，须低于 7200，即客户端的读取超时）为宽度、一致性、变更、增量和召回读取设置上限，包括交换之后以及 `--drop-pre-rebuild` 中的读取，也适用于交换之前的每次增量和修复。读取表的行数、列和目录行这类小读取不受它限制；复制以及回滚的追赶复制（包括查找其行的读取）在各自 7000 秒的限制下运行。交换之后读取超时会使后置条件失败（退出码 7）并保留回滚副本，在 `--drop-pre-rebuild` 中超时则会保留该副本。每次宽度检查（预检、交换之前，以及交换之后对回滚副本的检查）会读取普通指定列的每个向量（在 ReplacingMergeTree 上使用 `FINAL`），每次一致性检查（复制或修复之后、交换之前和交换之后）会读取两张表的每一行，包括向量，因此请把超时设置得足以覆盖对该表的一次完整读取。

<a id="what-the-checks-after-a-swap-find"></a>

### 交换之后的检查发现的内容

交换之后的运行在下列每项发现上都会使其后置条件失败（退出码 7），`--drop-pre-rebuild` 则会拒绝（退出码 3），两者都会保留回滚副本。摘要行的 `next` 带有相应的步骤。

| 检查发现 | 打印的下一步 |
| ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------ | ----------------------------------------------------------------------------------------------------------------------------------------------------------- |
| 交换之后，只有回滚副本持有的更改：只存在于其中的键、其中较新的版本、其上未完成的 `ALTER UPDATE`，或设置了另一种宽度向量的 `UPDATE` | `--rollback --yes`，它会把旧表连同该更改一起放回去 |
| 交换之后，只有线上表持有的键，或线上表中的较新版本：交换后对新表的写入，或最后一次检查之后对旧表的删除 | 保留两张表，并手动检查差异 |
| 某个键在同一版本下内容不同：任一表中的轻量级 `UPDATE`（无论是否已合并），或在交换之前已完成的 `ALTER UPDATE` | 保留两张表，并手动检查差异 |
| 交换之后，自复制开始以来线上表上的变更（mutation），例如 `loom reembed --delete-old` 执行的轻量级 `DELETE` | 保留两张表，并手动检查差异 |
| 两张表的列名不一致 | 保留两张表，并手动检查差异 |
| 回滚之后，只有新布局副本持有的变更，或自其创建以来对它的变更（mutation） | 保留两张表，并手动检查差异 |
| 检查无法运行：drain 或 sync 失败、读取超时，或副本尚未追上 | 修复授权、直接连接到某一个副本，或调高 `--statement-timeout-s`，然后重新运行被拒绝的操作（`--drop-pre-rebuild` 或 `--rollback`） |

`--rollback` 只会把旧表缺少的行复制回去：对于带有版本列的表，复制版本高于旧表最高版本的行；对于没有版本列的 `chunks`，复制旧表缺少的键。线上表上的轻量级 `UPDATE` 或 `DELETE`，以及擦除墓碑（erasure tombstone），会保留其键和版本；写入的版本低于旧表最高版本的行也永远不会被复制回去，因此 `--rollback` 会丢失这类变更。它在交换之前先运行上述检查，对每一项下一步不是 `--rollback` 的发现，都会拒绝执行（退出码 3），且不交换任何内容。它会重新启动旧表的合并，因此回滚副本上未完成的 mutation 会在回滚之后生效，随后执行的 `--drop-pre-rebuild` 会把它涉及的键显示为同一版本下内容不同。回滚之后，新布局副本上未完成的 `ALTER UPDATE` 在 `FINAL` 下不会显示，这就是对它的 mutation 也要计入的原因。回滚成功后，摘要中会给出下一步：保持写入方和擦除处于停止状态，运行 `--drop-pre-rebuild --yes`，它会先检查两张表，再删除新布局副本。再次执行 `--rollback` 会被拒绝（退出码 3）并指出该步骤：线上表已不再是新布局。
线上表中的删除会使其键只留在回滚副本中，因此只有在线上表自复制开始以来没有 mutation 时，第一行才会指向 `--rollback`。

要手动检查差异，请保持写入方和擦除处于停止状态，列出两张表中持有内容不同的每个键，并带上两侧的版本列。`<version>` 是该表的 ReplacingMergeTree 版本列：`version`，在 `memory_chunks` 和 `entity_vectors` 上是 `created_at`，在 `conversation_embeddings` 上是 `ts`。`<key>` 是该表的排序键。`<copy_vectors>` 和 `<live_vectors>` 只列出重建所指定的向量列（`--columns`）。`<columns>` 是其他所有列，其中包括其他向量列，两侧的列名保持一致：工具按存储形式对它们做哈希。
对于重建所指定的列，在新布局一侧（交换后是线上表，回滚后是副本），列直接使用其列名。在旧布局一侧，则按重建的写法书写：
`if(length(<column>) = <dim>, CAST(<column> AS Array(BFloat16)), arrayResize(CAST([] AS Array(BFloat16)), <dim>, toBFloat16(0)))`，
其中 `<dim>` 是编解码器的宽度（见新布局 `SHOW CREATE TABLE` 中的 `Quantized(<method>, <dim>, 0)`），这样两种布局的哈希结果一致：

```sql
SELECT <key>, s.<version> AS copy_version, t.<version> AS live_version,
       s.__present AS in_copy, t.__present AS in_live
FROM (SELECT <key>, <version>, cityHash64(tuple(<columns>, <copy_vectors>)) AS __hash,
             1 AS __present FROM loom.<table>_pre_rebuild FINAL) AS s
FULL OUTER JOIN
     (SELECT <key>, <version>, cityHash64(tuple(<columns>, <live_vectors>)) AS __hash,
             1 AS __present FROM loom.<table> FINAL) AS t USING (<key>)
WHERE s.__hash != t.__hash
SETTINGS join_use_nulls = 0
```

对于没有版本列的表，去掉 `FINAL` 和 `<version>`，并在两侧分别读取 `count() AS n, sum(cityHash64(...)) AS __hash ... GROUP BY <key>`。在两张表中读取所列每个键的完整行，并检查 `system.mutations`，看是否有尚未生效的 mutation。然后：

- 当线上表已按写入方的本意持有所列每个键的变更，并且检查中计入的所有 mutation 都已在线上表生效时，用 `DROP TABLE loom.<table>_pre_rebuild SYNC` 删除副本。
- 否则，在窗口期内，把只有副本持有的每一行复制到线上表（`INSERT INTO loom.<table> (<all columns>) SELECT <all columns> FROM
loom.<table>_pre_rebuild FINAL WHERE (<key>) IN (...)`；在同一版本下，最后插入的行生效），再次运行读取，直到它只列出线上表应当保留的变更，然后删除副本。`<all columns>` 不包含 MATERIALIZED 和 ALIAS 列，INSERT 会拒绝这些列。交换之后线上表已编码，会拒绝空向量或其他宽度的向量，因此在 `SELECT` 中，重建所指定的每一列都要按重建的写法书写：
  `if(length(<column>) = <dim>, CAST(<column> AS Array(BFloat16)), arrayResize(CAST([] AS Array(BFloat16)), <dim>, toBFloat16(0))) AS <column>`。

手动删除会移除仅该表持有内容的唯一副本，因此必须在读取之后进行，绝不能在读取之前。

<a id="disk-pressure-hygiene"></a>

## 磁盘压力的日常维护

在 Loom 主机上：

- **限制 Docker 的 JSON 日志大小。** 每行都输出堆栈跟踪的容器会很快写满磁盘。在 `/etc/docker/daemon.json` 中设置 `log-opts`（例如 `max-size` 100m × `max-file` 5）。
- **使用 `docker builder prune` 和 `docker image prune` 清理构建缓存和悬空镜像。**
- **不要清理容器。** `docker container prune` 会删除已停止的容器，而在[零停机双实例部署](https://github.com/ClickHouse/loom/blob/main/docs/operations/deployment.md#zero-downtime-pair)中，已停止的那一组颜色正是你的回滚目标（[备份与恢复](https://github.com/ClickHouse/loom/blob/main/docs/operations/backup-and-recovery.md#the-rollback-colour)）。
- **留意代码镜像。** `var/code_repos` 为每个已建立索引的仓库保存一个裸克隆；停用某个仓库时，如果没有其他条目使用该镜像，并且镜像位于 `LOOM_CODE_REPOS_ROOT` 之下，就会删除该镜像；运维人员放在其他位置的镜像不会被动。
- **保持探测输出精简。** 磁盘写满时，需要写入临时文件的命令同样会失败；请将诊断信息写入内存支持的路径，例如 `/dev/shm`。

在 ClickHouse 中：

```sql
SELECT table, formatReadableSize(sum(bytes_on_disk)) AS size
FROM system.parts WHERE active AND database = 'loom'
GROUP BY table ORDER BY sum(bytes_on_disk) DESC LIMIT 10;

SELECT name, formatReadableSize(free_space) FROM system.disks;
```

大规模删除数据后，可以让合并操作折叠各个版本，压缩代码向量表，或者启用按表设置的保留策略。以症状为切入点的说明见[故障排查](https://github.com/ClickHouse/loom/blob/main/docs/operations/troubleshooting.md#the-host-or-clickhouse-runs-out-of-disk)。

<a id="related"></a>

## 相关内容

- [监控](https://github.com/ClickHouse/loom/blob/main/docs/operations/monitoring.md)：提示需要进行修复的各项指标
- [备份与恢复](https://github.com/ClickHouse/loom/blob/main/docs/operations/backup-and-recovery.md)
- [代码索引运维](https://github.com/ClickHouse/loom/blob/main/docs/operations/code-index-operations.md)
