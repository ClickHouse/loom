<!-- i18n: source=docs/guides/performance-tuning.md blob=285ede1bf84d35f39aa2de165f838e15263c115e model=claude-sonnet-5-5 -->
> 本页由机器从[英文原文](https://github.com/ClickHouse/loom/blob/main/docs/guides/performance-tuning.md)翻译，内容以英文原文为准。
> Machine-translated from the [English page](https://github.com/ClickHouse/loom/blob/main/docs/guides/performance-tuning.md), which is authoritative.

<a id="tune-looms-performance"></a>

# 调优 Loom 的性能

本页上的每一项调优手段都是一个设置或一个请求字段。[配置参考](https://github.com/ClickHouse/loom/blob/main/docs/reference/configuration.md)给出了每个设置的默认值和含义。对于经过实测的权衡取舍，本页会给出用于测量的命令。

<a id="measure-first"></a>

## 先测量

| 问题                                               | 命令                                                                                  | 需要                                                 |
| -------------------------------------------------- | ------------------------------------------------------------------------------------- | ---------------------------------------------------- |
| 并发为 N 时，单个 op 的 p50 / p95 / p99 和 RPS     | `make bench OP=memory.search C=5 D=60` (`scripts/ops/benchmark.py`)                   | 一个运行中的服务器；写入会进入 `bench-…` 命名空间    |
| 排序或 embedder 的改动是否降低了召回率？           | `make bench-quick` (一个公开长记忆基准的子集，带答案评判)                             | 真实的 provider、`OPENAI_API_KEY`                    |
| 与已记录基线对比的召回率                           | `make bench-quality-gate MIN_RECALL=…`                                                | 真实的 provider                                      |
| 搜索模式 × rerank × 候选池宽度                     | `make bench-matrix` (写入 `bench/MATRIX_RESULTS.md`)                                  | 一个运行中的服务器                                   |
| 提取和图提取的回归                                 | `make bench-algorithm-gate`                                                           | 一个运行中的服务器                                   |
| 代码平面的符号、调用方、召回率和 token 成本        | `make bench-code-intel`, `make bench-code-legs`                                       | 一个真实的 embedder                                  |
| 部署后发生了哪些变化                               | `loom deploy-delta` (切换前后两侧的 ledger 和 ClickHouse 语句形态)                    | `var/deployed.json`                                  |

> **警告：** 用 fake embedder 测得的召回率数字不能反映真实的检索效果。质量门禁因此没有放进 CI。请针对使用你将要部署的 provider 的服务器运行它们，并在任何 embedder 变更之后重新建立基线。

对于单个慢请求，在调整任何设置之前，先查看它的 `Server-Timing` 头（`app` 与 `ch`）及其 `system.query_log` 行（[调试](https://github.com/ClickHouse/loom/blob/main/docs/guides/debugging.md)）。整个部署中每个操作的开销，可以通过对 `log_comment` 的一次查询得到：

```sql
SELECT splitByChar(' ', log_comment)[1] AS source,
       count() AS queries, sum(read_bytes) AS bytes, sum(query_duration_ms) AS ms
FROM system.query_log
WHERE type = 'QueryFinish' AND event_time > now() - INTERVAL 1 HOUR AND log_comment != ''
GROUP BY source ORDER BY ms DESC LIMIT 20;
```

切换后立即产生的基准测试流量读取的是冷缓存；在将本次部署的延迟与上一个构建的延迟做比较之前，请先等待。

<a id="ranking-and-thresholds"></a>

## 排序与阈值

`memory.search` 先对第一阶段的候选池排序，再应用排序项和门限（[检索与排序](../concepts/retrieval-and-ranking.md)）。大多数排序项可以按部署设置，也可以按请求覆盖，因此可以先在一个调用方上对改动做 A/B 测试。

| 调节项                             | 设置（请求字段）                                                                                                                                                        | 权衡                                                                                                       |
| ---------------------------------- | ----------------------------------------------------------------------------------------------------------------------------------------------------------------------- | ---------------------------------------------------------------------------------------------------------- |
| 相似度门限                         | `LOOM_ADAPTIVE_THRESHOLDS`（默认开启）                                                                                                                                  | 开启：门限在启动时根据 embedder 自身的余弦分布推导。关闭：由你自行设置下面的标量门限。                     |
| 标量门限（回退）                   | `LOOM_MEMORY_SEARCH_MIN_SIMILARITY`、`LOOM_MEMORY_SEARCH_ABSTAIN_THRESHOLD`、`LOOM_MEMORY_SEARCH_HYDE_FALLBACK_THRESHOLD`、`LOOM_MEMORY_SEARCH_QUERY_REWRITE_THRESHOLD` | 针对 `text-embedding-3-small` 调优；用于伪 embedder、探测失败或关闭自适应的情况。                          |
| 低于下限的命中                     | `LOOM_MEMORY_SEARCH_FLOOR_MODE`（`floor_mode`）                                                                                                                         | `readmit` 补满 `top_k` 并标记 `below_floor`；`ids_only` 用空正文补满；`drop` 返回更少的结果。              |
| 候选池                             | `LOOM_MEMORY_SEARCH_POOL_MULTIPLIER_STANDARD`、`_RERANK`、`_DEEP`，上限为 `LOOM_MEMORY_SEARCH_MAX_POOL_TOP_K`                                                           | 更宽的候选池会提高召回率和重排的输入量；代价是读取的向量行数和重排 token 增加。                            |
| 跳过重排                           | `LOOM_LLM_RERANK_SKIP_ABOVE`（自适应门限没有探测结果时的回退）                                                                                                          | 当最高的向量命中已经足够强时，跳过重排调用。                                                               |
| 回退级联                           | `LOOM_MEMORY_SEARCH_FALLBACK_MAX_ROUNDS`、`_MAX_WALL_MS`、`_MAX_LLM_CALLS`、`_MAX_VECTOR_QUERIES`                                                                       | 限制较弱的首轮结果可以花费的重试次数（查询改写、HyDE）。                                                   |
| 强化                               | `LOOM_MEMORY_SEARCH_REINFORCEMENT_WEIGHT`（`reinforcement_weight`）、`_HALF_LIFE_DAYS`                                                                                  | 召回和正面结果对一行的提升幅度；0 表示禁用。上限低于相关性。                                               |
| 来源                               | `LOOM_MEMORY_SEARCH_TRUST_WEIGHT`（默认 0）                                                                                                                             | 一个有界的排序项，偏向有认证的来源。                                                                       |
| 时效性                             | `LOOM_MEMORY_SEARCH_RECENCY_POOL_WEIGHT`（`recency_pool_weight`）                                                                                                       | 对带有时效意图的查询，让候选池向较新的行重新排序；0 表示关闭。                                             |
| 代码形态查询的词法分支             | `rrf_adaptive_lexical`（请求）、`LOOM_MEMORY_RRF_CODE_SHAPE_LO` / `_HI`                                                                                                 | 在 RRF 下，对路径形态或代码形态的查询降低词法分支的权重。                                                  |
| 默认模式                           | `LOOM_MEMORY_SEARCH_DEFAULT_MODE`（`search_mode`）                                                                                                                      | 调用方既未指定模式也未指定 recipe 时使用。                                                                 |
| 每个 key 的重复项                  | `LOOM_MEMORY_SEARCH_PER_KEY_CAP`                                                                                                                                        | 一个 key 在一页中最多可以贡献的行数。                                                                      |

> **警告：** 在一个 embedder 上调好的阈值，换到下一个 embedder 上就不适用。当 `LOOM_ADAPTIVE_THRESHOLDS=false`，或使用伪 embedder 时，每次更换 embedder 后都要重新跑基准测试（`make bench-quick`、`make bench-quality-gate`）。`calibration.get` 会显示某个命名空间当前生效的门限来自哪个层级。

<a id="vector-reads"></a>

## 向量读取

向量列的类型为 `Array(BFloat16)`；在 `enable_quantized_codec` 可以开启的环境中，migrator 会以编码方式创建这些列（[ClickHouse](../architecture/clickhouse.md#gated-clickhouse-settings)）。

| 调节项                     | 设置                                                                                              | 取舍                                                                                                                                                                                                                                                                                                                                                                   |
| -------------------------- | ------------------------------------------------------------------------------------------------- | ---------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| 优先扫描编码               | `LOOM_VECTOR_SEARCH_QUANTIZED_CODES`（默认开启），`LOOM_MEMORY_SEARCH_FACT_CODES_FIRST`（`auto`） | 读取量化编码，然后以完整精度重新打分。`auto` 会在 `memory_embeddings` 存在 patch parts 时将其关闭，因为此时重新打分会被跳过。                                                                                                                                                                                                                                           |
| 编码过量获取               | `LOOM_VECTOR_SEARCH_QUANTIZED_FETCH_MULTIPLIER`，`LOOM_MEMORY_SEARCH_FACT_CODES_FETCH_MULTIPLIER` | 重新打分之前按编码距离保留的行数。值越低开销越小，但排在前面的结果发生变化的情况越多。                                                                                                                                                                                                                                                                                 |
| 每条 fact 语句的线程数     | `LOOM_MEMORY_SEARCH_FACT_LEG_MAX_THREADS`（0 = 不固定）                                           | 固定线程数可以限制每条语句的 CPU 和峰值内存，代价是增加一些延迟。                                                                                                                                                                                                                                                                                                      |
| 批量写入的相近记忆读取     | `LOOM_MEMORY_BULK_PEER_READ`（默认 `batched`；`per_fact`）                                        | `per_fact` 与 `memory.set` 一样，以优先扫描编码的方式读取每条批量 fact 的冲突记忆和值更新的相近记忆：适用于大型命名空间，在这类命名空间中，批量的相近记忆语句会失败并报 `MEMORY_LIMIT_EXCEEDED`（服务器日志中为 `batched conflict candidates failed`），而当某条记录的逐 fact 回退也失败时，该记录会在未经检查的情况下写入。距离相同时，相近记忆的结果可能不同。                    |

要将现有的表迁移到某种编码或布局，请使用 `scripts/ops/vector_codec_rebuild.py`（孪生表、一致性与召回率检查、`EXCHANGE`；[步骤](https://github.com/ClickHouse/loom/blob/main/docs/operations/deployment.md#recommended-clickhouse-settings)），切勿在迁移中使用 `ALTER`。

<a id="clickhouse-connections-and-deadlines"></a>

## ClickHouse 连接与超时

| 调节项 | 设置 | 权衡 |
| ------------------------- | ----------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- | -------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| 连接池大小 | `LOOM_CH_POOL_MAXSIZE`（默认每个进程 64） | 按预期的请求并发数，加上 worker 数，再加上批量插入在等待期间占用的连接数来设置；驱动自身的默认值在批量写入下会被耗尽。 |
| 批量插入 | `LOOM_CH_BATCHED_INSERTS`（默认开启）、`LOOM_CH_BATCHED_INSERT_WAIT_MIN_MS`（10）、`LOOM_CH_BATCHED_INSERT_WAIT_MAX_MS`（50）、`LOOM_CH_BATCHED_INSERT_BACKGROUND_WAIT_MAX_MS`（200） | 并发的插入共用一个 part，每次插入最多增加一个窗口时长的延迟；单独的插入只等待最短时间；每个等待中的插入都会占用一个池中的连接，因此连接池需随写入速率增长（[批量插入](../architecture/clickhouse.md#batched-inserts)）。 |
| 连接池等待 | `LOOM_CH_POOL_WAIT_MS`（默认 500） | 超过该时间后，请求会快速失败并返回 503 `OVERLOADED`，而不是排队；0 表示不设限。 |
| Keep-alive | `LOOM_CH_KEEPALIVE_TIMEOUT`（默认 5 s） | 必须小于负载均衡器的空闲断开时间，这样 Loom 会先关闭池中的连接。 |
| 语句超时 | `LOOM_CH_TIMEOUT_READ_S`、`LOOM_CH_TIMEOUT_WRITE_S`、`LOOM_CH_TIMEOUT_DDL_S` | 作为 `max_execution_time` 以及释放连接池槽位的客户端限制生效；0 表示该类别不设限。 |
| 按流量类别设置优先级 | `LOOM_CH_SETTINGS_BY_TRAFFIC_CLASS`（JSON） | 为每个类别（`production`、`interactive`、`bench`、`background`）设置 `priority` 和 `max_threads`，使基准测试和 worker 让位于生产读取。 |
| Read-your-writes | `LOOM_CH_READ_YOUR_WRITES`、`LOOM_CH_READ_YOUR_WRITES_WINDOW_S` | 写入之后的读取保持顺序一致；在复制服务上有额外开销，在单节点上无效果。 |
| 操作预算 | `LOOM_LATENCY_BUDGET_MULTIPLIER` | 缩放每个操作的 `budget_p99_ms`；到 ClickHouse 的广域网跳转需要约 20。预算只会触发 `budget.violated` 事件，不会中断请求。 |

Web 容量等于每个容器的 `LOOM_WEB_WORKERS` 乘以容器数；速率限制器会用其在 `loom.instances` 中统计到的存活副本数来划分上限，且不会少于 `LOOM_REPLICA_COUNT`（[副本](https://github.com/ClickHouse/loom/blob/main/docs/operations/replicas.md#rate-limits)）。

<a id="providers"></a>

## 服务提供方

| 调节项                       | 设置                                                                                                                              | 取舍                                                                                                                                                         |
| ---------------------------- | --------------------------------------------------------------------------------------------------------------------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------ |
| 查询嵌入时限                 | `LOOM_MEMORY_SEARCH_EMBED_BOUND_S`, `LOOM_MEMORY_SEARCH_EMBED_BOUND_RETRIES`                                                      | 对查询嵌入的单次尝试设置时限；重试用尽后，搜索返回 503 `EMBED_TIMEOUT`。                                                                                     |
| 查询拼写                     | `LOOM_MEMORY_SEARCH_QUERY_SPELLING_MAX_WORDS`                                                                                     | 词数不超过该值的查询会在嵌入的同时读取一次命名空间，以语料库中的拼写嵌入其小写词：对 40k 行的命名空间需要 100–300 ms，结果复用一分钟；`0` = 关闭。          |
| 查询嵌入缓存                 | `LOOM_EMBEDDING_CACHE_SIZE`, `LOOM_EMBEDDING_CACHE_TTL_S`, `LOOM_EMBEDDING_CACHE_SHARED`, `LOOM_EMBEDDING_CACHE_SHARED_MAX_CHARS` | 在共享的 `loom.embedding_cache` 之前设有进程内 LRU；重复的查询会跳过对提供方的调用。                                                                         |
| 按流量类别设置 LLM 响应缓存  | `LOOM_LLM_POLICY_BY_TRAFFIC_CLASS` (JSON)                                                                                         | 缓存 LLM 响应（`bench` 默认开启），并可禁止某个类别使用强层级或中层级。                                                                                      |
| 备用模型                     | `LOOM_LLM_FALLBACK_MODEL`                                                                                                         | 主模型在重试后仍不可用时由其应答，而不是让操作失败。                                                                                                         |
| 支出上限                     | `LOOM_DEFAULT_DAILY_COST_CAP_USD` (按命名空间设置 `daily_cost_cap_usd`)                                                           | 超过上限后，依赖 LLM 的 worker 会跳过该命名空间，`memory.ask` 以 `budget_capped` 弃答；普通的读写继续进行。                                                 |

<a id="embedder-choice"></a>

### 嵌入模型的选择

| 嵌入模型 (`LOOM_EMBEDDING_MODEL`)                            | 取舍                                                                       |
| ------------------------------------------------------------ | -------------------------------------------------------------------------- |
| `text-embedding-3-large`, `gemini-embedding-001`             | Loom 默认的 3072 维；质量和成本仍需在你的数据上实测。                      |
| `text-embedding-3-small`                                     | 较窄的 1536 维向量选项；更改维度需要重新嵌入。                             |
| `local`, `local:bge-small`, `local:bge-base`, `local:minilm` | 在进程内用 CPU 做嵌入，无需调用提供方，也不需要密钥；以质量换取延迟和成本。 |
| `fake-ngram-3072`                                            | 标准维度下的字符 n-gram，仅用于测试和接线；其召回数字没有意义。            |

更换嵌入模型会改变向量空间：将 `LOOM_EMBEDDING_DIM` 设为相应的值，运行 `loom reembed`，若开启了自适应阈值，则让启动探测重新校准。更换前后各运行一次 `make bench-quick`；`LOOM_LLM_MODEL` 同理，抽取提示词正是针对它校准的。

<a id="workers-and-batches"></a>

## Workers 与批处理

每个 worker 都有一个 `*_ENABLED` 开关和一个 `*_INTERVAL_SECONDS` 运行间隔，其中许多还有 `*_IDLE_INTERVAL_SECONDS`，用于在一轮处理没有发现任何内容后休眠（见 [workers 参考](https://github.com/ClickHouse/loom/blob/main/docs/reference/workers.md)）。间隔越短，后台任务的延迟越低，但扫描次数也越多；空闲间隔则让空队列保持低开销。

| Worker                | 调节项                                                                                                                                                                                                                               |
| --------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------ |
| `embedding_worker`    | `LOOM_EMBEDDING_WORKER_INTERVAL_SECONDS`、`_IDLE_INTERVAL_SECONDS`；`LOOM_EMBEDDING_WORKER_ORPHAN_FULL_SCAN_SECONDS` / `_ORPHAN_RECENT_SECONDS` 用于限定孤立记录 anti-join 的范围。                                                          |
| `extraction_queue`    | `LOOM_EXTRACTION_QUEUE_INTERVAL_SECONDS`、`_MAX_BATCHES_PER_PASS`、`_NAMESPACE_CONCURRENCY`、`_LEASE_SECONDS`；`LOOM_EXTRACTION_DEFER_AGENTS` 会对列出的 agent 推迟 `set_from_messages`，使请求先于提取返回。 |
| `memory_expiry_sweep` | `LOOM_MEMORY_EXPIRY_SWEEP_BATCH_SIZE`。                                                                                                                                                                                               |
| `task_lease_sweep`    | `LOOM_TASK_LEASE_SWEEP_INTERVAL_SECONDS`、`_IDLE_INTERVAL_SECONDS`。                                                                                                                                                                  |

Worker 运行在 `background` 流量类别中，因此 `LOOM_CH_SETTINGS_BY_TRAFFIC_CLASS` 也会限制它们在 ClickHouse 中的优先级和线程数。只运行一个 workers 进程：由 LLM 驱动的 worker 若有第二个副本，扫描次数和开销都会翻倍。代码平面的摄取 worker 和编译器层级 worker 有各自的调节项（见[代码索引运维](https://github.com/ClickHouse/loom/blob/main/docs/operations/code-index-operations.md)）。

<a id="client-side-levers"></a>

## 客户端优化手段

一些最大的收益来自请求本身：

- 请求更少的正文：`memory.search` 上的 `bodies` 和 `body_budget_tokens`，`context.pack` 和 `code.search` 上的 `token_budget`。
- 每个任务只加载一次上下文（`context.pack`），而不是每一步都执行一次搜索。
- 使用 `retrieval_budget` 为每次调用选择更低成本或更深入的搜索配置。
- 使用 `Idempotency-Key` 请求头重试幂等的 op（SDK 会自动生成该请求头）：重放请求时会返回已存储的响应，而不是再次执行该 op。

<a id="related"></a>

## 相关内容

- [故障与恢复](../architecture/failure-and-recovery.md)：容量以及需要扩展的部分
- [监控](https://github.com/ClickHouse/loom/blob/main/docs/operations/monitoring.md)
- [运行基准测试](https://github.com/ClickHouse/loom/blob/main/docs/benchmarks/running.md)
- [调试](https://github.com/ClickHouse/loom/blob/main/docs/guides/debugging.md)
