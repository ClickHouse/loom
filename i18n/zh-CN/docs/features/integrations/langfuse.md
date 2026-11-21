<!-- i18n: source=docs/features/integrations/langfuse.md blob=6a704ef0765498c439dcf20e7c5ac032a353ecb4 model=claude-sonnet-5-5 -->
> 本页由机器从[英文原文](https://github.com/ClickHouse/loom/blob/main/docs/features/integrations/langfuse.md)翻译，内容以英文原文为准。
> Machine-translated from the [English page](https://github.com/ClickHouse/loom/blob/main/docs/features/integrations/langfuse.md), which is authoritative.

<a id="langfuse"></a>

# Langfuse

需要设置 `LOOM_LANGFUSE_URL` 和 `LOOM_LANGFUSE_ADMIN_TOKEN`；两者都设置之前不会发送任何数据。设置完成后，OpenTelemetry span 树会通过 OTLP 转发到 Langfuse。Langfuse 是可选的，不属于 Loom 本地 Compose 栈的一部分。

span 导出和数据删除已在自托管的 **Langfuse 3.227.0** 和 **Langfuse 4.76.0** 上测试。Langfuse Cloud 运行的是 Langfuse 4。本地部署 Langfuse 还需要运行 PostgreSQL、ClickHouse、Redis/Valkey 和兼容 S3 的对象存储，因此规模远大于只有两个服务的 Loom 配置。

Loom 在 ClickHouse 中保存自己的遥测数据（[遥测](https://github.com/ClickHouse/loom/blob/main/docs/features/observability/telemetry.md)）。Langfuse 则为每个请求提供一条 trace，其中包含嵌套的模型调用、每次生成的成本、可过滤的评分以及提示词版本。Loom 在其中承担四种角色：

| 角色        | Loom 的作用                                                    | 需要                          |
| ----------- | -------------------------------------------------------------- | ----------------------------- |
| Trace 写入  | 将每个请求的 OTel span 树及其生成记录通过 OTLP 转发            | `pk:sk` 令牌，已启用 OTel     |
| 评分写入    | 在每次搜索和提问的 trace 上写入检索质量评分                    | `pk:sk` 令牌                  |
| 提示词关联  | 根据已提交的 lockfile，为每次生成标记其使用的 Langfuse 提示词名称和版本 | lockfile；无需运行时调用      |
| 删除后端    | 删除已被擦除的用户或命名空间的 trace                           | URL 和令牌（任意形式）        |

Loom **不**在提示词获取路径上。自行在 Langfuse 中管理提示词的 agent 应使用 Langfuse SDK 的 `get_prompt` 来获取。

<a id="setup"></a>

## 设置

```bash
LOOM_LANGFUSE_URL=https://langfuse.example.com
LOOM_LANGFUSE_ADMIN_TOKEN=pk-lf-...:sk-lf-...
```

该令牌是一个 `public_key:secret_key` 键值对，以 HTTP Basic 方式发送。单个 bearer 令牌（不含 `:`）仍可用于删除数据，但会禁用 trace 和 score 的发送；服务器会记录一条说明此情况的警告日志。

可通过 `GET /v1/health?include=langfuse` 检查结果：它会报告本地导出器的状态，并在 `scores` 和 `retrieval_judge` 下报告已发送、失败或被丢弃（队列已满、熔断器打开）的 score 数量。这不是对远程 Langfuse 就绪状态的探测。当两项设置都存在时，启动日志中的 `mode` 令牌会包含 `langfuse`。

<a id="traces"></a>

## 追踪

Loom 在 ClickHouse exporter 旁边附加了第二个 span processor，并把 OpenTelemetry 的 span 树转发到 Langfuse 的 OTLP 端点。追踪保留了真实的嵌套关系（HTTP span、处理器、每次 generation），同时带有会话 id 和每次 generation 的成本。一次聊天调用是一个 `GENERATION` 观测，到达模型提供方的嵌入调用是一个 `EMBEDDING`；由 Loom 自身缓存返回的嵌入则是普通 span。请求在解析出调用方之后打开的每个 span，都带有该请求的用户 id、会话 id、`org:` 和 `namespace:` 标签以及追踪名称，因为 Langfuse 4 是按观测读取这些信息，而不是从追踪根节点读取。这些键同样会出现在 ClickStack 的 span 中。每个搜索阶段（`search.<stage>`）是一个 `retriever` 观测，这是 Langfuse 的 agent 图视图所使用的类型之一。禁用 OTel（`LOOM_OTEL_ENABLED=false`）后，不会有任何追踪发送到 Langfuse。

| 设置                                                                          | 默认值                                              | 作用                                                                                       |
| ----------------------------------------------------------------------------- | --------------------------------------------------- | ------------------------------------------------------------------------------------------ |
| `LOOM_LANGFUSE_SAMPLE_RATE`                                                   | `1.0`                                               | 转发的追踪比例，按追踪 id 采样，因此一条追踪会完整到达                                     |
| `LOOM_LANGFUSE_SPAN_DENYLIST`                                                 | `ch.query,ch.insert,GET /v1/health,POST /v1/health` | 永不转发的 span 名称前缀（基础设施 span 应放在 ClickStack 中）                             |
| `LOOM_LANGFUSE_CIRCUIT_FAILURE_THRESHOLD`, `LOOM_LANGFUSE_CIRCUIT_COOLDOWN_S` | `5`, `30`                                           | 熔断器：连续 N 次评分 POST 失败后，在冷却期内丢弃评分，之后发送一次探测请求               |

Langfuse 从不位于 Loom 的请求路径上。熔断器避免评分 POST 的失败演变成重试压力；OTLP 批处理器自己维护投递队列，因此 span 导出失败**不会**触发熔断。所以 `loom.langfuse.breaker` 指标（0 表示关闭，1 表示半开，2 表示打开）反映的是评分发送的状态，而不是远端是否可用。worker 的开销追踪命名为 `worker.<name>`，并带有该 worker 的 org 和命名空间。作为追踪根节点的嵌入 span（嵌入回填）和 worker 轮次的根节点永远不会被转发；请求路径上的嵌入嵌套在其处理器 span 之下，会被保留。

> **提示：** span 数量主要由 worker 的嵌入产生。在生产环境中配置 Langfuse 之前，先调低 `LOOM_LANGFUSE_SAMPLE_RATE`。

<a id="what-text-leaves-loom"></a>

## 哪些文本会离开 Loom

有多少用户内容会到达 Langfuse：

- **请求和响应 payload。** 从不发送：span 不携带请求体或响应体。
- **Loom 自身模型调用的 prompt 和 completion 文本**，按 prompt 所包含的内容区分：

  | `LOOM_LANGFUSE_CAPTURE_IO_LEVEL` | 记录内容                                                                                                              |
  | -------------------------------- | --------------------------------------------------------------------------------------------------------------------- |
  | `off`（默认）                    | 无                                                                                                                    |
  | `query`                          | 仅记录 prompt 为用户自己的搜索文本的环节（HyDE、查询改写、enrichment 等）                                             |
  | `all`                            | 还包括携带检索到的记忆或对话轮次的环节（rerank、iterative judge、extraction、session summary 等）                     |

  命名空间的 `capture_generation_io`（通过 `namespace.register` 设置）会覆盖该级别：`false` 表示无论服务器如何设置都为 off，`true` 表示 `all`，未设置则继承服务器设置。每个字段的长度上限为 `LOOM_LANGFUSE_GENERATION_IO_MAX_CHARS`（2000），并带有明确的截断标记。同一级别也控制 `loom.search_trace_log.query`。

token 数量、成本、延迟、质量评分和 prompt 版本链接不包含用户内容，因此从不受此限制。

<a id="scores"></a>

## 评分

`server/observability/langfuse_scores.py` 会把检索质量评分发布到每次搜索的 trace 上：`retrieval_confidence`、`top_similarity`、`abstained` 和 `abstain_reason`。`code.ask` 还会增加 `code_ask_cache_hit` 和 `code_ask_coverage`。当 `LOOM_RETRIEVAL_JUDGE_SAMPLE_RATE` 大于 0 时（默认值为 0；每次被抽样的搜索都会产生一次模型调用的开销），一个独立的 LLM 评审会写入 `retrieval_relevance` 评分，该评分与检索不共享任何状态。

评分不会占用请求路径：系统使用一个有界队列并在后台消费，当 Langfuse 响应缓慢时，队列会优先丢弃最旧的评分，并统计丢弃的数量。只有在其 trace 已被转发时（使用相同的采样门限）才会发布评分，并且评分受评分发布断路器的约束。

<a id="prompt-versions"></a>

## 提示词版本

Loom 的系统提示词是 Python 常量。`scripts/codegen/push_langfuse_prompts.py` 会把它们注册到 Langfuse，并写入 `server/observability/langfuse_prompts.lock.json`，以 `sha256(prompt)[:12]` 作为键。运行时，`server/observability/genai.py` 会在该锁文件中查找所发送的提示词，并为这次生成标注 Langfuse 的提示词名称和整数版本，无需调用 Langfuse。已编辑但未推送的提示词不会被关联，而不会被关联到错误的版本。

```bash
python scripts/codegen/push_langfuse_prompts.py          # plan against the lockfile, offline
python scripts/codegen/push_langfuse_prompts.py --write  # push, then write the lockfile
```

`--write` 需要同时配置两项 Langfuse 设置。文本已是最新版本的提示词不会被再次推送。

`POST /v1/admin/prompts.evaluate`（平台管理员）会根据某个提示词版本对应 trace 的评分标准分数，对照最低数量和阈值，返回是否应当提升该版本的结论。每次关联到提示词版本的生成都会为其 trace 打上 `prompt:<name>:<version>` 标签，评估时会读取带有该标签的最多 200 条 trace 及其分数，Langfuse 3 和 4 均适用。该接口从不向 Langfuse 写入数据；未配置 Langfuse 时返回 501 `INTEGRATION_NOT_CONFIGURED`，无法读取 Langfuse 时返回 502 `BACKEND_UNAVAILABLE`。

<a id="erasure"></a>

## 删除（Erasure）

`erasure.request` 把 Langfuse 后端（`server/erasure/app/backends.py` 中的 `LangfuseBackend`）作为其 saga 的一个步骤来运行：

- **用户范围**：按 `userId` 和 `org:<org>` 标签过滤，找到该用户的 trace，然后用 `DELETE /api/public/traces` 分批删除。删除一个 trace 会同时删除它的 observation 和 score。org 过滤很重要，因为每个组织都写入密钥对所对应的同一个 Langfuse 项目，而 `user_id` 只在一个组织内唯一。
- **命名空间范围**：删除同时带有 `namespace:<ns>` 和 `org:<org>` 的 trace。

查找方式取决于服务器的主版本，主版本从 `GET /api/public/health` 读取：Langfuse 3 列出 trace（`GET /api/public/traces`）；Langfuse 4 不再提供该列表，而是列出 observation（`GET /api/public/v2/observations`）并收集它们的 trace id。Langfuse 4 以异步方式删除，因此已删除的 trace 可能还会保持可见数分钟。服务器版本在每个删除步骤中只读取一次。

在从 Langfuse 3 升级而来的部署上，Langfuse 4 的查找只能找到 observation 列表中已有的内容，除此之外删除不会报告任何其他内容。Langfuse 3.227.0 存储的 trace 在其中（已通过将其原地升级到 4.76.0 测试）：Loom 会发送 `x-langfuse-ingestion-version: 4` 请求头，该版本会把它们写到 Langfuse 4 读取的位置。对于更早的 Langfuse 3 版本所存储的 trace，请在升级之后、依赖删除功能之前，运行 Langfuse 的历史数据回填（historic backfill）。

该步骤采用失败即关闭的策略。健康检查的响应中没有版本、出现 5xx、超时，或删除端点返回 404（较旧的自托管构建，或只转发 GET 和 POST 的代理）时，都会抛出异常，saga 会把该请求记录为 `failed`，并由 `erasure_recovery` worker 重试。重新运行是幂等的，因为删除一个已删除的 trace 也会成功；在 Langfuse 4 上，它可能会再次找到并删除仍在等待删除的 trace。未配置 Langfuse 时，由一个桩（stub）后端记录意图并报告 0。`LOOM_EXTERNAL_ERASURE_TIMEOUT_S`（30）限定每次调用的时长。参见[保留与删除](https://github.com/ClickHouse/loom/blob/main/docs/features/memory/retention-and-erasure.md)。

<a id="joining-a-request"></a>

## 关联请求

Loom 在 `X-Request-Id` 中返回的请求 id 会出现在 Langfuse trace 元数据中，位于 `loom.agent_events.request_id` 以及 `loom.request_id` span 属性上，因此可以从 Langfuse 追溯到 Loom 的账本中的同一个请求。参见[跨系统关联请求](https://github.com/ClickHouse/loom/blob/main/docs/features/integrations/index.md#joining-a-request-across-systems)。

<a id="related"></a>

## 相关内容

- [遥测](https://github.com/ClickHouse/loom/blob/main/docs/features/observability/telemetry.md)：同一批 span 在 ClickHouse 一侧的呈现。
- [ClickStack](https://github.com/ClickHouse/loom/blob/main/docs/features/integrations/clickstack.md)。
- [配置参考](https://github.com/ClickHouse/loom/blob/main/docs/reference/configuration.md#observability)：包含所有 `LOOM_LANGFUSE_*` 设置。
