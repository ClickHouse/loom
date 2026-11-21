<!-- i18n: source=docs/features/observability/telemetry.md blob=6ecde6689a67135feecc80bca8d455821803fdca model=claude-sonnet-5-5 -->
> 本页由机器从[英文原文](https://github.com/ClickHouse/loom/blob/main/docs/features/observability/telemetry.md)翻译，内容以英文原文为准。
> Machine-translated from the [English page](https://github.com/ClickHouse/loom/blob/main/docs/features/observability/telemetry.md), which is authoritative.

<a id="telemetry"></a>

# 遥测

Loom 通过三个层面报告自身的运行情况：

| 层面                         | 存放位置                                                                    | 回答的问题                                            |
| ---------------------------- | --------------------------------------------------------------------------- | ----------------------------------------------------- |
| 账本                         | `loom.agent_events`，每次操作或 worker 执行对应一行                         | 谁调用了什么、耗时多久、成本多少                      |
| OpenTelemetry                | `default.otel_traces`、`otel_logs`、`otel_metrics_*`（或 OTLP collector）   | 一次请求内部的时间花在哪里；趋势与告警                |
| ClickHouse 查询归因          | `system.query_log.log_comment`                                              | 哪个操作或 worker 让 ClickHouse 执行了这些工作        |

三者共用同一个请求 id。[审计日志](https://github.com/ClickHouse/loom/blob/main/docs/features/observability/audit.md)是另一份记录，用于记录谁修改了什么，并具备防篡改能力。

<a id="the-ledger"></a>

## 账本

`loom.agent_events` 每个操作一行：`operation`、身份列（`org`、`namespace`、`agent`、`user_id`、`session_id`、`run_id`）、`request_id`、`trace_id`、`latency_ms`、`success`、`error_code`、token 计数、`upstream_cost_usd`、`model`、`llm_calls`、`consumer`、`traffic_class`、`subject`、`build_commit`、`instance`、`instance_label`，以及一个 `args` 映射，其中保存经过限长和脱敏的请求与输出字段。worker 的各个处理阶段写入 `kind='worker'` 的行。[成本与用量](https://github.com/ClickHouse/loom/blob/main/docs/features/observability/cost-and-usage.md)基于此表构建。

每条请求行还带有服务级别列：

- `transport`：`http` 或 `mcp`。
- `slo_class`：`code`、`search`、`admin`；其他非只读的 op 为 `write`，否则为 `read`。
- `status_class`：`ok`；4xx 拒绝，或被重试的幂等键仍在运行时为 `client`；其余情况为 `server`。
- `budget_ms`：该请求所适用的延迟预算。预算为 0（用于强制产生违规的覆盖值）会使每个成功的请求都算作慢请求。

未指定 org 或命名空间的调用，会记录在闸门放行它时所针对的 org 和命名空间之下（代码 op 的默认值是部署的代码命名空间），因此这一行归该租户所有：它的 SLI、成本汇总、`subject_last_seen` 以及历史策略的计数都会在那里看到它。未指定 org 且在闸门之前被拒绝的调用，记录在令牌所属的 org 之下（org 级令牌或关闭认证时为 `-`），因此 `cost.summary`、已连接 agent 视图和 `subject_last_seen` 也会把它计入该 org。

计入服务端错误的情况：

- 因无法评估历史策略而被拒绝的写入。
- 因检查所需的某次读取失败而做出的拒绝（授权、subject 的别名、服务账号或委托令牌的创建者、命名空间分类）。如果请求自身的答案因此被拒绝或隐藏（按 id 查找返回 `found=False`、处理器自身检查返回的 403 或 404、整个 org 级汇总被清空），即使在传输层上成功，也算 `server`，此时 `error_code` 为空。
- 不计入的情况：答案本身完整，只是少了某个命名空间。例如 Loom 自行添加的命名空间（代码链接、grounding、仓库文档、trunk、为调用方未指明条目的代码答案补充信息的 tracker）、调用方未限定范围的列表或默认选择所省略的条目，或发布者本已被放行的任务上的所有者检查。这些情况下请求仍为 `ok`。

调用被放行之前的拒绝：

- MCP 会为分发前被拒绝的调用写一行（令牌不能使用的 org 或命名空间、不对 MCP 开放的 op、只读连接上的写入），与 HTTP 对其拒绝的处理一致。此时调用方尚未绑定，所以该行带有令牌的 org（或调用指明的 org，若令牌可在其中操作）、subject 和 consumer，其余为 `-`，且没有参数。
- 在两种传输上，指明了令牌无权操作的 org 的拒绝，都记录在令牌所属的 org 中，而不是所指明的 org。
- 在调用被分类之前记录的拒绝，带有令牌的 `traffic_class` 声明，没有声明则为 ''。
- 在同一个 MCP 批次内，调用方在处理器运行前的拒绝，按操作、代码、org 和状态类别各记录一次；服务端故障最多记录十次。未写入的行计入 `loom.ledger.batch_repeats`（调用方的拒绝）或 `loom.ledger.dropped{reason="batch_repeat"}`（服务端故障的，即丢失的服务端错误）。幂等拒绝（在限流器之后）和处理器的失败总是有各自的行。

一个可刷新的物化视图每分钟把账本汇总进 `loom_telemetry.sli_minute`（请求数、服务端和客户端错误、被限流的请求（属于客户端错误）、超出预算的成功请求、延迟分位数，保留 400 天；延迟是处理器的耗时，也就是预算所针对的那段，不含其前面的各道闸门），供 SLO 控制台和告警规则读取，无需扫描账本。它从 ClickHouse 接收每一行的时间（`ingested_at`）继续，而不是该行的 `ts`，因此刷写被延迟的行仍然只会被计数一次，并计入其 `ts` 所在的那一分钟；一行在 ClickHouse 接收后至少 70 秒（最多约 130 秒）才会被汇总，这为它的插入留出了提交的时间。一次刷新写入一个 part，因此刷新失败时，重试既不会重复汇总，也不会遗漏。若某个账本 part 的提交时间晚于 ClickHouse 给其行打时间戳后的 70 秒，它就永远不会被汇总；`loom.sli.rollup_missed_rows` 统计最近一天内这类行的数量，重新汇总该rollup（[ClickHouse](../../architecture/clickhouse.md)）即可恢复它们。没有 SLO 类别的行（`/v1/me/*` 路由，以及未指明任何已注册 op 的 MCP 调用，如 `initialize`）不在汇总之内。同一分钟在合并之前可能以多行形式出现，因此读取方应按其键分组，计数用 `sum()`，延迟用 `quantilesTDigestMerge(0.5, 0.95, 0.99)(latency)`。每次刷新还会写入一行，所有键为空、所有计数为 0，下一次刷新从这里继续：读取方应过滤 `slo_class != ''`。若要排除测试流量，过滤 `traffic_class NOT IN
('bench', 'background')`：在调用被分类之前记录的拒绝，其类别为空。

发送器是批量的，从不使请求失败；崩溃可能丢失尚未刷出的队列。

每个写入行的进程都要标明其启动者。compose 文件设置 `LOOM_DEPLOYMENT=1`，并给服务打上 `deploy:web`、`deploy:workers` 或 `deploy:dev` 标签。任何其他进程（`make server`、测试服务器）必须设置 `LOOM_INSTANCE_LABEL=<who>:<what>:<port>`，否则拒绝启动；`loom serve` 默认为 `cli:serve:<port>`。这样可以让该部署的行与同一 ClickHouse 上的其他内容区分开。

<a id="traces-and-logs"></a>

## 追踪与日志

- **追踪。** span 写入 `default.otel_traces`（或 OTLP 端点），并带有 `loom.org`、`loom.namespace`、`loom.agent`、`loom.operation`、`loom.request_id` 和 `enduser.id` 标签。健康检查轮询不会产生 span（`LOOM_OTEL_EXCLUDED_URLS`，默认 `/v1/health`）。每个 HTTP 响应都带有 `X-Request-Id`（若客户端发送了由 1 到 64 个字母、数字、`.`、`_` 或 `-` 组成的值，则沿用客户端的值）和 `X-Trace-Id`。span 不会携带查询文本。
- **模型调用。** 每次 LLM 和 embedding 调用都是独立的 span，名为 `llm.<stage>` 或 `embed.provider`，并带有 OpenTelemetry GenAI 属性：`gen_ai.provider.name`、`gen_ai.operation.name`（`chat` 或 `embeddings`）、`gen_ai.request.model`、`gen_ai.usage.input_tokens` 和 `output_tokens`，provider 提示缓存的份额记在 `gen_ai.usage.cache_read.input_tokens` 和 `cache_creation.input_tokens` 中（两者都包含在 `input_tokens` 内），以及根据 Loom 价格表计算的 `gen_ai.usage.cost`。发起调用的环节记为 `loom.llm.stage`。由 Loom 自身 embedding 缓存提供的 token 记为 `loom.embed.cached_tokens`，与计费的输入分开。HyperDX 的 LLM 视图和 Langfuse 会按原样读取这些键。
- **日志。** 使用 `docker logs loom-server` 和 `docker logs loom-workers`，以及导出到 `default.otel_logs` 的 OTel 日志。日志管道可使用 `LOOM_LOG_FORMAT=json`，用 `LOOM_LOG_LEVEL` 控制详细程度。启动日志会列出解析出的 provider 和每个已应用的迁移；supervisor 会记录每次失败的轮次以及 `DEGRADED` 行。
- **Langfuse。** 设置 `LOOM_LANGFUSE_URL` 和 `LOOM_LANGFUSE_ADMIN_TOKEN` 后，span 树还会作为第二个 span 处理器转发到 Langfuse，经过采样和过滤。参见 [Langfuse](../integrations/langfuse.md)。

`init_otel` 在 pytest 下不会启动，因此测试不会写入部署的可观测性表。`LOOM_OTEL_ENABLED=false` 会关闭数据输出。

<a id="exporters"></a>

### 导出器

| 设置                                              | 默认值        | 作用                                                                                                                                                                   |
| ------------------------------------------------- | ------------- | ---------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `LOOM_OTEL_EXPORTER`                              | `""`          | `ch` 直接写入 Loom 自带 ClickHouse 服务上的 `default.otel_*` 表；`otlp` 发送到收集器；为空时，若设置了端点则为 `otlp`，否则为 `ch` |
| `LOOM_OTEL_EXPORTER_OTLP_ENDPOINT`                | `""`          | OTLP/HTTP 收集器的基础 URL（会追加 `/v1/traces` 和 `/v1/metrics`）                                                                                                     |
| `LOOM_OTEL_EXPORTER_OTLP_HEADERS`                 | `""`          | 以逗号分隔的 `K=V` 请求头，例如 `Authorization=Bearer <token>`                                                                                                         |
| `LOOM_OTEL_SERVICE_NAME`, `LOOM_OTEL_ENVIRONMENT` | `loom`, `dev` | 每个 span、日志和指标上的 `service.name` 和 `deployment.environment.name`                                                                                              |
| `LOOM_OTEL_METRICS_EXPORT_INTERVAL_S`             | `60`          | 指标导出间隔；span 保持自己的 3 秒批处理延迟                                                                                                                           |
| `LOOM_OTEL_METRICS_DELTA_TEMPORALITY`             | `false`       | 计数器和直方图使用增量而非累积值；默认的控制台读取累积行，因此两者应同时切换                                                                                           |
| `LOOM_OTEL_GAUGE_HEARTBEAT_S`                     | `0`           | 仅限 `ch` 导出器：仅在值变化时或经过这么多秒后才写入一个 gauge 点；0 = 每次导出都写（gauge 图块会用 0 填补空缺）。OTLP 会导出每个点                                      |
| `LOOM_OTEL_CENSUS_SCOPE`                          | `leader`      | 由谁执行全表普查：仅持有 supervisor 租约的进程，或每个进程（开销大）                                                                                                   |

`ch` 导出器写入 ClickStack/HyperDX 读取的表，因此默认的技术栈无需收集器即可使用控制台。参见 [ClickStack](../integrations/clickstack.md) 和 `deploy/hyperdx_dashboards.md`。

<a id="metrics"></a>

## 指标

指标每隔 `LOOM_OTEL_METRICS_EXPORT_INTERVAL_S` 导出一次。使用 `ch` exporter 时，指标写入 `default.otel_metrics_sum`、`default.otel_metrics_histogram` 和 `default.otel_metrics_gauge`；使用 `otlp` 时，指标发送到 collector 的 `/v1/metrics`。控制台定义位于 `deploy/dashboards/` 和 `deploy/hyperdx_dashboards.md`。

标签来自封闭集合（操作、命名空间、有界枚举）。键、用户 id、会话 id 和查询文本从不作为标签；`tests/unit/test_domain_metrics.py` 会强制检查这一点。模型标签（`loom.llm.fallback`、`loom.llm.response_cache`）的取值是价格表中列出的模型，或部署的 `LOOM_LLM_MODEL` / `LOOM_LLM_FALLBACK_MODEL`，其他任何模型都记为 `unlisted`，因为请求可以指定模型。

需要扫描表的 gauge 由持有 workers 租约的实例计算并缓存：完整性不变量和 embed 积压每隔 `LOOM_OTEL_INVARIANTS_PROBE_S`（3600 秒）计算一次，保留期普查每隔 `LOOM_OTEL_RETENTION_CENSUS_S`（3600 秒）一次，代码平面计数每隔 `LOOM_OTEL_CODE_COUNTS_CACHE_S`（3600 秒）一次。

<a id="requests-and-cost"></a>

### 请求与成本

| 指标                            | 类型      | 含义                                                                                                                                                                                                                                                                                                                  |
| ------------------------------- | --------- | --------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `loom.requests`                 | counter   | 已处理的操作数，按操作、命名空间、是否成功以及 `loom.error_code`（失败时的错误码，成功时为空）区分                                                                                                                                                                                                                     |
| `loom.request.duration`         | histogram | 操作的墙钟耗时，单位 ms                                                                                                                                                                                                                                                                                               |
| `loom.cost.usd`                 | counter   | 归属于某个操作的上游提供方花费                                                                                                                                                                                                                                                                                        |
| `loom.tokens`                   | counter   | 按 `kind`（`embed`、`llm_input`、`llm_output`）统计的 token                                                                                                                                                                                                                                                           |
| `loom.ops.paid_errors`          | counter   | 已经产生上游花费的失败请求                                                                                                                                                                                                                                                                                            |
| `loom.llm.fallback`             | counter   | 由 `LOOM_LLM_FALLBACK_MODEL` 应答的 LLM 调用                                                                                                                                                                                                                                                                          |
| `loom.llm.response_cache`       | counter   | 按结果统计的精确提示词 LLM 缓存查找                                                                                                                                                                                                                                                                                   |
| `loom.memory.build.tokens`      | counter   | 构建记忆所花费的 LLM token，按阶段和方向区分                                                                                                                                                                                                                                                                          |
| `loom.memory.extraction_yield`  | histogram | 每写入一条记忆所用的提示词 token，按 `messages` 范围区分                                                                                                                                                                                                                                                              |
| `loom.extract.cost_per_row_usd` | histogram | 一次提取每写入一行记忆的 USD 成本                                                                                                                                                                                                                                                                                     |
| `loom.auth.refusals`            | counter   | 因其 bearer 令牌而被拒绝的请求（没有令牌、验证失败的令牌，或暂时无法检查的令牌；也包括由 ClickHouse 判定已吊销的令牌发起的签发），按 `reason`（`missing`、`malformed`、`expired`、`revoked`、`invalid`、`unavailable`）和 `loom.transport`（`http`、`mcp`，或用于提供方令牌缺失、格式错误或错误的 SCIM 请求的 `scim`）区分；这些都不会进入 SLI 汇总 |

<a id="agent-trusts"></a>

### Agent 信任

使用 `--agent-claim` 注册的工作负载信任，会在每个 agent 首次交换时为其创建一个命名空间，数量上限为 `--max-agents`（[机器人与 CI](https://github.com/ClickHouse/loom/blob/main/docs/operations/sso.md#bots-and-ci)）。`loom.trust` 是该信任的名称，只有运维人员可以注册。

| 指标                                | 类型    | 含义                                                                                                                                                 |
| ----------------------------------- | ------- | ---------------------------------------------------------------------------------------------------------------------------------------------------- |
| `loom.auth.agents_provisioned`      | counter | 某个信任下 agent 的首次交换，按 `loom.org`、`loom.trust` 和 `outcome` 区分：`made`（已创建其命名空间）或 `at_cap`（被拒绝：该信任已创建 `--max-agents` 个） |
| `loom.auth.trust_agents`            | gauge   | 活跃信任下仍存活的 agent（已释放的不计入），与上限的计数方式一致；由持有 workers 租约的实例每五分钟计算一次                                          |
| `loom.auth.trust_agents_used_ratio` | gauge   | `loom.auth.trust_agents` 与该信任的 `--max-agents` 之比；达到 1 时，每个新 agent 都会被拒绝                                                          |

<a id="retrieval-quality"></a>

### 检索质量

这些指标用于发现“速度很快但结果错误”的搜索，这是 HTTP 指标看不到的。

| 指标                                    | 类型      | 含义                                                                                      |
| --------------------------------------- | --------- | ----------------------------------------------------------------------------------------- |
| `loom.search.results`                   | histogram | 每次搜索的结果数；向 0 漂移意味着悄无声息的故障                                           |
| `loom.search.top_similarity`            | histogram | 最佳命中的余弦相似度；最具诊断价值的检索信号（更换 embedder 后需重新建立基线）            |
| `loom.search.confidence`                | histogram | 响应中的 `retrieval_confidence`                                                           |
| `loom.search.abstain`                   | counter   | 选择放弃作答的搜索，按 `reason` 区分                                                      |
| `loom.search.plane_hits`                | counter   | 每个检索平面的命中数                                                                      |
| `loom.search.assist`                    | counter   | 启用的可选辅助（HyDE、rerank、迭代式评判），按 `kind` 区分                                |
| `loom.search.judged_relevance`          | histogram | 独立 LLM 评判者对抽样搜索的评分（`LOOM_RETRIEVAL_JUDGE_SAMPLE_RATE`，默认 0）             |
| `loom.search.judge_verdict`             | counter   | 评判者的裁决，按类别统计                                                                  |
| `loom.search.bm25_stats_failures`       | counter   | 失败后放行的 BM25 语料统计读取（词法部分未加权运行），按平面和错误区分                    |
| `loom.memory.search.enumeration_shaped` | counter   | 措辞上要求枚举某一类已存储记录的搜索；这是一个信号，而不是拒绝                            |

<a id="memory-writes"></a>

### 记忆写入

| 指标                                           | 类型    | 含义                                                                                                          |
| ---------------------------------------------- | ------- | ------------------------------------------------------------------------------------------------------------- |
| `loom.memory.writes`                           | counter | 写入结果：created、updated、merged、duplicate、conflicted、rejected、blacklisted、too_large 等                |
| `loom.memory.drops`                            | counter | 摄取管线丢弃或截断的条目，按 `stage` 和 `reason` 区分（行记录在 `loom.memory_drops` 中）                      |
| `loom.memory.attribute_tagged`                 | counter | 写入时被标记为 `attribute:<class>` 的行                                                                       |
| `loom.policy.decisions`                        | counter | 历史策略闸门的决策                                                                                            |
| `loom.procedure.runs`, `loom.procedure.evolve` | counter | 按结果统计的 procedure 运行；`procedure.evolve` 的结果                                                        |
| `loom.memory.agent_labels_24h`                 | gauge   | 过去 24 小时内 agent 写入的使用标签，按命名空间统计（前 `LOOM_OTEL_AGENT_LABELS_MAX_NAMESPACES` 个，其余记为 `other`） |
| `loom.memory.agent_labels_namespaces_at_pace`  | gauge   | 每个组织中 agent 标签达到每月 1,000 条的日均速度的命名空间数                                                  |

`loom drops --namespace NS [--hours 24]` 会按阶段和原因输出丢弃计数。彻底失败的提取不算丢弃：`set_from_messages` 返回 502 `EXTRACTION_FAILED`，部分失败则出现在响应的 `pipeline_errors` 中。

<a id="backlogs-and-process-state"></a>

### 积压与进程状态

水位非零并且能够逐步消化，属于正常情况。

| 指标                                                                                                                         | 类型      | 含义                                                                                                                                       |
| ---------------------------------------------------------------------------------------------------------------------------- | --------- | ------------------------------------------------------------------------------------------------------------------------------------------ |
| `loom.embed.pending`                                                                                                         | gauge     | 等待生成向量的行（`embedding_dim = 0`）；尚不可搜索的记忆                                                                                  |
| `loom.vectors.total`                                                                                                         | gauge     | 已存在的已嵌入向量                                                                                                                         |
| `loom.vector_index.searches_total`, `adds_total`, `reads_indexed_ratio`                                                      | gauge     | 被探测副本上的向量索引使用情况：已处理的搜索与已付出的构建成本之比，以及近期向量读取中打开了索引的比例                                     |
| `loom.process.open_fds`, `loom.process.fd_limit` | gauge | 每个进程（`loom.pid`）的已打开描述符数和软限制；曲线持续上升说明存在泄漏 |
| `loom.host.disk.free_bytes`, `loom.host.disk.total_bytes` | gauge | 存放 `var/` 的磁盘的可用字节数和总字节数 |
| `loom.ch.mutations_pending` | gauge | 尚未完成的 ClickHouse mutation 数 |
| `loom.ch.table.active_parts`, `wide_parts`, `patch_parts`, `bytes_on_disk`, `rows_per_mark` | gauge | 按表（`loom.table`）统计，针对参与维护的表 |
| `loom.memory_embeddings.patch_parts` | gauge | `loom.memory_embeddings` 上的 patch part 数；非零时会关闭量化编码读取 |
| `loom.ch.view_refresh.scheduled`, `loom.ch.view_refresh.age_s` | gauge | 按可刷新视图（`loom.view`，位于 `loom` 中的只写视图名，否则带数据库名：`loom_telemetry.sli_minute_refresh`）统计：已调度且无错误时为 1；以及距上次成功的秒数 |
| `loom.sli.rollup_lag_s` | gauge | 最近一次存储探测（每五分钟一次，由 workers-lease 持有者执行）时，`loom_telemetry.sli_minute` 的标记落后于当前时间的秒数：正常时约为 70 到 130；更大说明汇总正在追赶，其最新的几分钟读取为空 |
| `loom.sli.rollup_missed_rows` | gauge | 最近一天内的账本请求行中，其插入在汇总标记越过它们之后才提交的行数，因此 `sli_minute` 永远不会统计它们（存储探测，每五分钟一次，由 workers-lease 持有者执行）；大于 0 时应告警并重新折叠汇总 |
| `loom.audit.checkpoint_lag_windows` | gauge | 尚未封入检查点链的每小时审计窗口数（[审计日志](https://github.com/ClickHouse/loom/blob/main/docs/features/observability/audit.md#checkpoint-lag)） |
| `loom.langfuse.breaker` | gauge | Langfuse 评分熔断器：0 为闭合，1 为半开，2 为打开（评分被丢弃；span 导出不受影响） |
| `loom.health.problems` | gauge | 此进程上每个未解决的 `/v1/health` 问题计为 1，按 `code` 和 `severity` 区分（`provider_failing` 另有 `provider`），但 `/v1/health` 通过 ClickHouse 读取发现的问题（`otel_erasure_index`）除外；已解决的问题会停止上报，因此告警应读取至少为 `LOOM_OTEL_GAUGE_HEARTBEAT_S` 加一个 `LOOM_OTEL_METRICS_EXPORT_INTERVAL_S` 的窗口（即两个没有心跳的间隔）：使用 `ch` 导出器并有心跳时，未变化的值会在每次心跳之后的第一次导出时写入 |
| `loom.ledger.dropped` | counter | 此进程自启动以来丢失的账本行数，按 `reason` 区分（`queue_full`、`flush_failed` 和 `batch_repeat`：MCP 批处理在其十行之外没有写入的服务器故障）；SLI 会相应少计同样的数量 |
| `loom.ledger.batch_repeats` | counter | MCP 批处理没有再次写入的调用方拒绝行（每个批次只记账一次）；这是调用方所致，不属于丢失 |
| `loom.worker.passes` | counter | 按 `worker` 和 `outcome` 统计的轮次数 |
| `loom.worker.pass_duration_ms` | histogram | 单轮的实际耗时 |
| `loom.worker.consecutive_failures` | gauge | 每个 worker 当前的连续失败次数；达到 3 时告警 |
| `loom.worker.partial_failures` | gauge | 上一轮容忍下来的失败数 |
| `loom.retention.active_rows`, `never_recalled_ratio`, `low_information_rows`, `sensitive_without_expiry`, `stale_candidates` | gauge | 按组织统计的记忆整洁度计数；Loom 不会按时间删除任何内容 |

<a id="postgres-publication-pipeline"></a>

### Postgres 发布管道

在 `LOOM_STORAGE_AUTHORITY=postgres` 下：一次写入需要多久才能被搜索到，以及 ClickHouse 还欠下哪些数据（无论哪种 authority，`loom.ch.inserts` 都会计数）（[管道 SLO](https://github.com/ClickHouse/loom/blob/main/docs/operations/monitoring.md#pipeline-slos)）。

| 指标 | 类型 | 含义 |
| ---------------------------------------- | --------- | ---------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `loom.pg.search_wait_ms` | histogram | 读取在屏障处等待 ClickHouse 持有该命名空间写入的时间，按 `loom.operation`（请求调用的 op）、`outcome`（`passed`，或在到达截止时间时为 `deadline`，截止时间即 `LOOM_PG_PROJECTION_WAIT_MS` 或可重入的一轮所剩的时间：该 op 返回 503 `PROJECTION_BEHIND`，这是一份可以跳过投影读取继续的报告）和 `loom.traffic_class` 区分；仅统计发现其落后的读取 |
| `loom.pg.projection_lag_ms` | histogram | 按 Postgres 的时钟，从某个修订的提交事务开始，到其投递清除了最后一个搜索目标为止，按 `path`（flush、redelivered）区分：文档已落地，或重新投递发现该修订已不再提供服务（已被编辑删除，或其键已更新）并在未发送的情况下将其结清 |
| `loom.pg.backlog` | gauge | 按 `source`（memory、audit_outbox、knowledge）和 `state`（pending、parked）统计的待投递数，每项最多计到 `LOOM_PG_BACKLOG_COUNT_CAP`；memory 的 pending 项是搜索所等待的那些 |
| `loom.pg.oldest_pending_age_s` | gauge | 按 `source` 统计的最旧待投递项的年龄：memory 的从其提交事务开始算起，knowledge 的从其来源的最近一次提交算起，均在按租约排序的前 `LOOM_PG_BACKLOG_COUNT_CAP` 项中取；audit outbox 的为逾期秒数，覆盖所有已逾期的行 |
| `loom.pg.backlog_reading_age_s` | gauge | 自各来源的积压被读取以来的秒数，仅在读数新鲜时上报；两个导出周期内没有新读数时，积压相关的 gauge 会停止上报 |
| `loom.ch.inserts`, `loom.ch.insert_rows` | counter | 记忆写入路径对记忆行和搜索文档的插入，以及审计写入器的插入，及其行数，按 `table` 区分；`memory` 和 `memory_embeddings` 的插入是已提交的，每次一个 part，而 `audit_log` 的插入默认是批量的（`LOOM_CH_BATCHED_INSERTS`） |

积压由投递 worker 在每一轮的开头读取，并由运行它们的进程导出（默认是 workers 子进程，否则是 workers-lease 持有者）。多个进程可能同时导出（`LOOM_BACKGROUND_WORKERS=on`、租约交接），每个进程导出的都是整个部署的全部积压：聚合这些 gauge 时应使用 `max`，不要用 `sum`。

<a id="integrity-invariants"></a>

### 完整性不变量

每一项都应始终为 0。非零值表示已经发生的缺陷，且不会自行消退。

| 指标 | 非零值表示 | 修复 |
| --------------------------------------------------------------- | ------------------------------------------------------------------------------- | ----------------------------------------------- |
| `loom.integrity.divergent_entities` | 某条记忆在 `loom.memory` 与 `loom.memory_embeddings` 中的实体不一致 | 排查写入路径 |
| `loom.integrity.orphan_vectors` | 已嵌入的向量，其记忆行已不存在 | `orphan_vector_reconcile` worker |
| `loom.integrity.orphan_entity_vectors` | 实体向量，其记忆行已不存在 | `orphan_vector_reconcile` worker |
| `loom.integrity.orphan_graph_namespaces` | 有实体边但没有有效记忆或知识的命名空间 | `loom erasure-re-verify --orphan-graph --erase` |
| `loom.integrity.live_ns_unregistered`, `live_rows_unregistered` | 位于没有 `loom.namespaces` 行的命名空间中的有效记忆 | 注册该命名空间 |
| `loom.integrity.dim0_with_vector` | 有向量但 `embedding_dim = 0` 的行，搜索不可见 | `loom integrity-repair --apply` |
| `loom.integrity.tombstone_status_drift` | 设置了 `tombstoned_at` 但状态为其他值的行 | 由 `loom integrity-repair` 列出 |
| `loom.integrity.active_past_valid_to` | 已超过自身 `valid_to` 的活动行 | 由 `loom integrity-repair` 列出 |
| `loom.integrity.high_with_embedding` | 仍有有效嵌入行的高敏感度行 | `loom sensitivity-sweep --apply` |

<a id="code-plane"></a>

### 代码平面

按命名空间统计。

| 指标 | 类型 | 含义 |
| ----------------------------------------------------------------------- | ------------------ | --------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `loom.code.index_lag_commits` | gauge | 索引水位线与分支末端之间相差的提交数；表示索引过期的信号 |
| `loom.code.index_age_seconds` | gauge | 水位线上次推进至今的秒数 |
| `loom.code.stale_files` | gauge | 已索引路径中，其分块早于文件在 head 处内容的路径数 |
| `loom.code.scip_lag_commits`, `scip_age_seconds`, `scip_py_age_seconds` | gauge | 各编译器层级（`tier` 属性）落后于快速层级的程度 |
| `loom.code.tier_lag_seconds` | gauge | 某个层级（`fast`、`clang`、`python` 等）尚未处理到的最旧提交的年龄 |
| `loom.code.tier_failing` | gauge | `code_tier_failing` 健康问题所报告的各层级连续失败的次数，按 `tier`、错误 `kind` 以及该问题评定的层级 `severity` 区分 |
| `loom.code.tier_lag_commits` | gauge | 上述各层级落后于已拉取末端的提交数（下限值：按该条目最后发布的状态统计），按 `tier` 区分；仅在落后量已知时上报 |
| `loom.code.pr_cursor_age_seconds` | gauge | PR 历史同步落后于 GitHub 的程度 |
| `loom.code.files_errored`, `loom.code.embed_backlog` | gauge | 索引出错的文件数；等待生成向量的分块数 |
| `loom.code.symbols` | gauge | 按 `tier`（`treesitter`、`scip`）统计的有效符号数；数量下降说明应用出错 |
| `loom.code.edges_unqualified`, `loom.code.edges_low_confidence` | gauge | 仅按名称绑定的解析器层级调用边；可与 `loom.code.symbols` 对比 |
| `loom.code.grep.wrong_abstain_24h` | gauge | `code.grep` 对语料中存在的字面量放弃返回结果的次数；前 8 个调用方加 `other`（每 `LOOM_OTEL_CODE_GREP_REPLAY_CACHE_S` 重新统计一次；0 = 关闭） |
| `loom.code.repos_config_errors` | gauge | `LOOM_CODE_REPOS_JSON` 中无法使用的条目数（0 = 解析无误） |
| `loom.code.outcome`, `loom.code.shunt`, `loom.code.shunt_tokens_saved` | counter | 客户端上报的结果；coding agent 的分流决策，以及被挡在上下文之外的 token |
| `loom.code.ask_cache`, `ask_cache_saved_ms`, `ask_cache_saved_usd` | counter | `code.ask` 答案缓存的结果以及命中所节省的量 |
| `loom.tracker.sections`, `loom.tracker.section_ms` | 计数器、直方图 | 按模板和状态统计的 `tracker.answer` 分节；读取耗时 |

关于滞后指标对调用方意味着什么，参见[代码索引新鲜度](../code-index/freshness.md)。

<a id="suggested-alerts"></a>

### 建议的告警

| 条件 | 原因 |
| ------------------------------------------------------------------------------------------------------------------------ | ---------------------------------------------------------------------------------------------------------------------------------------------------- |
| `loom.search.top_similarity` 均值低于基线持续 30 分钟 | 检索质量下降（嵌入模型或索引发生变更） |
| `loom.embed.pending` 高于 100 持续 15 分钟 | 新记忆无法被搜索到 |
| 任何 `loom.integrity.*` 高于 0 | 出现了数据缺陷 |
| `loom.worker.consecutive_failures` ≥ 3 | worker 陷入失败循环 |
| `loom.process.open_fds / loom.process.fd_limit` 高于 0.5 | 描述符泄漏 |
| `loom.host.disk.free_bytes / loom.host.disk.total_bytes` 低于 0.10 | 数据磁盘即将写满 |
| `loom.langfuse.breaker` = 2 | 发往 Langfuse 的评分正在被丢弃 |
| `loom.audit.checkpoint_lag_windows` 持续上升数小时 | 审计封存器已停滞 |
| `loom.code.index_lag_commits` 高于每轮提交预算持续 15 分钟 | 代码摄取正在落后 |
| `loom.code.tier_lag_seconds` 高于 1800 持续 10 分钟 | 某个编译器层级卡住（即 `deploy/dashboards/code_plane.json` 中的“code tier lag”告警；阈值应设置得高于最慢层级的正常周期） |
| 每个 trust 的 `loom.auth.agents_provisioned{outcome=made}` 高于其控制平面的正常速率（可从 10 分钟内 50 起步） | 某个 trust 命名新 agent 的速度超过其集群的增长速度：可能是签发密钥泄漏，或控制平面失控 |
| `loom.auth.trust_agents_used_ratio` 高于 0.8 | 该 trust 接近 `--max-agents` |
| `loom.auth.agents_provisioned{outcome=at_cap}` 在 10 分钟内增量高于 0 | 该 trust 的新 agent 被拒绝 |

在 HyperDX 中，空结果会被当作 0，因此最小样本数保护必须返回一个处于健康范围内的值，而不是不返回任何行。

起步查询：

```sql
-- mean top similarity per namespace, last hour
SELECT Attributes['loom.namespace'] AS ns,
       round(anyLast(Sum) / greatest(anyLast(Count), 1), 3) AS mean_top_similarity
FROM default.otel_metrics_histogram
WHERE MetricName = 'loom.search.top_similarity' AND TimeUnix > now() - INTERVAL 1 HOUR
GROUP BY ns;

-- every gauge's latest value
SELECT MetricName, anyLast(Value) AS latest
FROM default.otel_metrics_gauge
WHERE TimeUnix > now() - INTERVAL 5 MINUTE
GROUP BY MetricName ORDER BY MetricName;
```

<a id="query-attribution-in-clickhouse"></a>

## ClickHouse 中的查询归因

每条 ClickHouse 查询都带有一个 `log_comment` 设置，用来标明发起者：请求使用 `<operation>
<request_id>`，worker 的一次处理使用 `worker.<name>`，CLI 命令使用 `cli.<command>`，其余情况使用 `untagged:<module>.<function>`。标签表、`query_id` 关联以及语句摘要见 [ClickHouse：查询归因](../../architecture/clickhouse.md#query-attribution)。

```sql
-- one request end to end
SELECT * FROM loom.agent_events WHERE request_id = '<id>';
SELECT query_duration_ms, read_bytes, log_comment
FROM system.query_log
WHERE type = 'QueryFinish' AND log_comment LIKE '% <id>%';
```

<a id="not-telemetry-the-observe-operations"></a>

## 不是遥测：`observe.*` 操作

`observe.record` 和 `observe.crystallize` 属于记忆平面。它们用于捕获 agent 的工具调用：

- `observe.record` 将一次工具调用（工具名称、输入、输出、文件路径、错误）存为一条情节性的、不可搜索的记忆行，键为 `obs:<session_id>:<sha8>`，不调用 LLM，也不做同步嵌入。如果工具调用来自 Loom 自身或 harness 的元工具，则不存储内容（`skipped_reason: "self_referential"`），内容匹配黑名单模式的调用也一样（`skipped_reason: "blacklisted"`）。
- `observe.crystallize` 是会话结束时的刷新操作：它把该会话中待处理的观察标记为可供提取。它只负责入队，不运行 LLM。

提炼观察需要 `observe_extract` worker（默认关闭，由 `LOOM_OBSERVE_EXTRACT_ENABLED` 控制）。关闭时，观察会被存储，但不会被提取。

编码 agent 插件如何使用这些操作，参见[对话与会话](../conversations-and-sessions.md)。

`ui.vitals` 为控制台提供服务：它把某个页面的 web vitals 记录在自己的账本行上。

<a id="related"></a>

## 相关内容

- [监控](https://github.com/ClickHouse/loom/blob/main/docs/operations/monitoring.md)：健康检查、控制台以及运维人员的日常视图。
- [故障排查](https://github.com/ClickHouse/loom/blob/main/docs/operations/troubleshooting.md)。
- [成本与用量](https://github.com/ClickHouse/loom/blob/main/docs/features/observability/cost-and-usage.md)和[审计日志](https://github.com/ClickHouse/loom/blob/main/docs/features/observability/audit.md)。
- [ClickStack](../integrations/clickstack.md)和 [Langfuse](../integrations/langfuse.md)。
- [Workers 参考](https://github.com/ClickHouse/loom/blob/main/docs/reference/workers.md)和[配置参考](https://github.com/ClickHouse/loom/blob/main/docs/reference/configuration.md#observability)。
