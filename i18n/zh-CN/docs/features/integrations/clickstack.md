<!-- i18n: source=docs/features/integrations/clickstack.md blob=008f7906fbb2d60f24b7f99be4e0ee44382e64f9 model=claude-sonnet-5-5 -->
> 本页由机器从[英文原文](https://github.com/ClickHouse/loom/blob/main/docs/features/integrations/clickstack.md)翻译，内容以英文原文为准。
> Machine-translated from the [English page](https://github.com/ClickHouse/loom/blob/main/docs/features/integrations/clickstack.md), which is authoritative.

<a id="clickstack-and-hyperdx"></a>

# ClickStack 与 HyperDX

Loom 始终输出 OpenTelemetry 数据；HyperDX 的数据源、控制台和擦除步骤需要另行配置。

Loom 为每个操作输出一个 span，并附带日志和指标。span 带有 `loom.operation`、`loom.org`、`loom.namespace`、`loom.agent`、`loom.request_id`，以及存在时的 `loom.user_id` / `enduser.id`。默认情况下，Loom 按采集器（collector）的表结构，把这些数据直接写入其自身 ClickHouse 服务上的 `default.otel_*` 表，因此 HyperDX 无需运行任何采集器即可读取。如果设置了 `LOOM_CLICKSTACK_DATABASE`，这些表会位于该数据库而不是 `default` 中，这里以及下文的每条查询都是如此。列布局见 `schemas/otel/clickstack_otel_tables.sql`；如果 ClickStack 更改了其表结构，该文件必须随之更新。

模型调用 span 带有 OpenTelemetry GenAI 属性（[遥测](https://github.com/ClickHouse/loom/blob/main/docs/features/observability/telemetry.md#traces-and-logs)），属性键与 HyperDX 的 LLM 视图读取提供方、操作、token 用量、提示缓存占比和成本时使用的键一致。

<a id="choose-the-export-path"></a>

## 选择导出路径

| `LOOM_OTEL_EXPORTER` | 行为                                                                                                                                      |
| -------------------- | ----------------------------------------------------------------------------------------------------------------------------------------- |
| `ch`                 | 写入同一服务上的 `default.otel_traces`、`otel_logs`、`otel_metrics_sum`、`otel_metrics_histogram` 和 `otel_metrics_gauge`                   |
| `otlp`               | 发送到 `LOOM_OTEL_EXPORTER_OTLP_ENDPOINT`（请求头在 `LOOM_OTEL_EXPORTER_OTLP_HEADERS` 中），用于 collector 或其他后端                       |
| 空（默认）           | 设置了 endpoint 时为 `otlp`，否则为 `ch`                                                                                                  |

在 `ch` 模式下，Loom 会先检查 `default.otel_traces` 是否存在；如果不存在，导出器会在输出一行日志后自动禁用。创建这些表：

```bash
clickhouse-client --multiquery < schemas/otel/clickstack_otel_tables.sql
```

`GET /v1/health?include=otel` 会显示实际生效的导出器。其余 OTel 设置（导出间隔、temporality、gauge 心跳、服务名称）请参阅 [遥测](https://github.com/ClickHouse/loom/blob/main/docs/features/observability/telemetry.md#exporters)。

<a id="point-hyperdx-at-the-tables"></a>

## 将 HyperDX 指向这些表

数据行已经在服务中，因此可以跳过 collector 和 ingestion key 的步骤。创建三个 HyperDX **source**：基于 `otel_traces` 的 traces，基于 `otel_logs` 的 logs，以及基于 `otel_metrics_*` 的 metrics。`deploy/clickstack_sources.sh` 会通过 HyperDX API 创建这些 source 并配置它们之间的关联（托管版 ClickStack 使用你的控制台会话 cookie `HDX_COOKIE` 进行身份验证）。[`deploy/hyperdx_dashboards.md`](https://github.com/ClickHouse/loom/blob/main/deploy/hyperdx_dashboards.md) 中列出了每个 source 的字段映射。

<a id="dashboards-and-alerts"></a>

## 控制台与告警

[`deploy/hyperdx_dashboards.md`](https://github.com/ClickHouse/loom/blob/main/deploy/hyperdx_dashboards.md) 是配置手册：

- **Loom — Memory Health**：检索质量趋势（`loom.search.top_similarity`）、按操作和传输方式划分的调用数与错误数、系统状态、开销与 token、按检索平面划分的延迟、写入结果，以及请求面板（速率、延迟、错误、最慢请求、按租户划分的流量、某个 trace 的日志）。
- **Loom — Code Plane**：索引滞后、层级滞后以及其他 `loom.code.*` 指标。JSON 定义位于 `deploy/dashboards/code_plane.json`。
- 引导顺序，以及已保存的搜索。

自行编写图块时有两个陷阱：

- **计数器是累计值。** 除非设置 `LOOM_OTEL_METRICS_DELTA_TEMPORALITY=true`，否则计数器的行是其累计总数；计算速率需要取相邻数据点之间的差值，而不是求和。修改该设置时，要同时修改控制台。
- **在 HyperDX 中，空结果会被读作 0。** 告警上的最小样本数保护必须返回一个处于正常范围内的值，而不是不返回任何行，否则告警会在没有数据时触发。

指标名称和建议的阈值见[指标目录](https://github.com/ClickHouse/loom/blob/main/docs/features/observability/telemetry.md#metrics)。

<a id="erasure"></a>

## 擦除

需要设置 `LOOM_CLICKSTACK_DATABASE`。ClickStack 没有用于删除用户数据的管理 API，因此后端（`server/erasure/app/backends.py` 中的 `ClickStackBackend`）会针对该数据库中的 OTel 表执行轻量级 `DELETE FROM` 语句：

| 表                                                                               | 搜索的属性列              |
| -------------------------------------------------------------------------------- | ------------------------- |
| `otel_logs`                                                                      | `LogAttributes`           |
| `otel_traces`                                                                    | `SpanAttributes`          |
| `otel_metrics_gauge`, `_sum`, `_histogram`, `_exponential_histogram`, `_summary` | `Attributes`              |
| `hyperdx_sessions`                                                               | `ResourceAttributes`      |

- **用户范围**匹配 `LOOM_CLICKSTACK_USER_ID_KEYS` 中的任意一个键（默认为 `user_id,user.id,enduser.id,loom.user_id,langfuse.user.id`），各键之间为 OR 关系，因为不同 SDK 对键名的约定并不一致。Loom 的请求 span 携带 `enduser.id`；其模型调用 span 携带 `loom.user_id` 和 `langfuse.user.id`，配置了 Langfuse 时，请求在解析出调用方之后打开的其他每个 span 也都携带这两个键。这些 span 同样携带 `loom.org`，因此下文的组织保护规则同样适用。
- **命名空间范围**匹配 `loom.namespace` 或 `service.namespace`。Loom 的导出器把 `service.namespace` 设为常量 `loom`，并在 `loom.namespace` 中携带租户；`service.namespace` 用于覆盖写入同一个 ClickStack 的其他发送方，其值 `loom` 永远不会被匹配。
- **组织保护。**带有*其他*组织 `loom.org` 标记的行绝不会被擦除。通过 `loom.namespace` 匹配到的行必须带有执行擦除的组织的 `loom.org`。Loom 的操作和领域指标（计数器和直方图）带有 `loom.namespace`，但不带组织，因此命名空间擦除不会处理它们：它们不包含记忆内容，但其 `loom.namespace` 标签就是命名空间名称，而个人命名空间的名称由用户的电子邮件派生。它们会一直保留，直到收集器表自身的 TTL（如果有）将其移除。没有 `loom.org` 标记、但被用户键或 `service.namespace`（其他发送方）匹配到的行会被擦除，因为它们可能包含该主体的数据。

轻量级删除只写入行存在掩码，因此这些行会立即从所有读取中消失，其占用的字节会在下一次合并时释放；而 `ALTER TABLE … DELETE` 会重写每个受影响的数据分片。对于属性 map 值上没有布隆过滤器的表（指标表都没有），该谓词会进行全表扫描；`loom doctor` 会指出这些表。

该步骤会在删除前统计行数，并报告该数量。表或数据库不存在时视为无需擦除；任何其他失败都会使该步骤以失败告终（fail closed），并由 `erasure_recovery` worker 重试。

在依赖默认值之前，请先检查你的数据实际携带了哪些用户 ID 键：

```sql
SELECT DISTINCT arrayJoin(mapKeys(LogAttributes))
FROM default.otel_logs WHERE TimestampTime > now() - INTERVAL 1 DAY
```

该步骤所属的 saga 请参见[保留与擦除](https://github.com/ClickHouse/loom/blob/main/docs/features/memory/retention-and-erasure.md)。

<a id="settings"></a>

## 设置

| 设置                               | 默认值                                                     | 作用                                         |
| ---------------------------------- | ---------------------------------------------------------- | -------------------------------------------- |
| `LOOM_OTEL_EXPORTER`               | `""`                                                       | `ch`、`otlp` 或自动选择                      |
| `LOOM_OTEL_EXPORTER_OTLP_ENDPOINT` | `""`                                                       | collector 的基础 URL                         |
| `LOOM_OTEL_EXPORTER_OTLP_HEADERS`  | `""`                                                       | 以逗号分隔的 `K=V` 请求头                    |
| `LOOM_OTEL_ENABLED`                | `true`                                                     | 总开关                                       |
| `LOOM_CLICKSTACK_DATABASE`         | `""`                                                       | OTel 表所在的数据库；启用后可执行擦除        |
| `LOOM_CLICKSTACK_USER_ID_KEYS`     | `user_id,user.id,enduser.id,loom.user_id,langfuse.user.id` | 按用户范围擦除时匹配的属性键                 |

<a id="related"></a>

## 相关内容

- [遥测](https://github.com/ClickHouse/loom/blob/main/docs/features/observability/telemetry.md)：输出的内容以及完整的指标目录。
- [监控](https://github.com/ClickHouse/loom/blob/main/docs/operations/monitoring.md)。
- [Langfuse](https://github.com/ClickHouse/loom/blob/main/docs/features/integrations/langfuse.md)：与模型调用追踪相同的 span。
