<!-- i18n: source=docs/operations/runtime-settings.md blob=b519ead8aee373492c371073baaa1fe49501d971 model=claude-sonnet-5-5 -->
> 本页由机器从[英文原文](https://github.com/ClickHouse/loom/blob/main/docs/operations/runtime-settings.md)翻译，内容以英文原文为准。
> Machine-translated from the [English page](https://github.com/ClickHouse/loom/blob/main/docs/operations/runtime-settings.md), which is authoritative.

<a id="runtime-settings"></a>

# 运行时设置

每个设置都是在启动时读取的环境变量 `LOOM_<FIELD>`，也可以写入 YAML 设置文件（[配置参考](https://github.com/ClickHouse/loom/blob/main/docs/reference/configuration.md)）。参考文档中标记为 **live** 的设置，在 Loom 运行期间还可以接受整个部署范围的覆盖（包括检索与排序 `memory_search_*`；答案合成 `memory_ask_*`；提取、上下文和知识调优；以及 ClickHouse 插入批处理 `ch_batched_insert*`）。其他所有设置只在启动时读取一次，修改后需要重启才能生效。被校准取代的相似度阈值（`memory_search_min_similarity`、`memory_search_abstain_threshold`、`memory_search_hyde_fallback_threshold`、`memory_search_query_rewrite_threshold`）不是 live 设置：开启 `LOOM_ADAPTIVE_THRESHOLDS` 后，它们只在没有校准时才生效。

<a id="see-what-is-in-force"></a>

## 查看当前生效的设置

```bash
loom settings list --changed              # off its default, or overridden
loom settings list --prefix memory_ask_   # one area
loom settings list --json
```

每一行显示值、值的来源，以及它是否为 live 设置：

```text
memory_ask_recency_weight = 0.35  (override, live)
ch_host = "clickhouse"  (env)
```

| 来源 | 含义 |
| --------------- | ---------------------------------------------------------------------------------------- |
| `override`      | 通过 `loom settings set` 或 `/v1/admin/settings.set` 设置 |
| `dashboard`     | 在 Settings → Models & keys 中保存的模型配置 |
| `env`           | 进程环境变量 |
| `env_file`      | dotenv 文件（`LOOM_ENV_FILE`，默认 `.env`），包括被复制到环境变量中的情况 |
| `settings_file` | YAML 设置文件（`LOOM_SETTINGS_FILE`），包括由 `scripts/serve.sh` 导出的情况 |
| `inferred`      | 启动时根据其他设置推断得出（LLM provider、本地模型的维度） |
| `default`       | 内置默认值 |

密钥只显示其长度。

<a id="override-a-live-setting"></a>

## 覆盖 live 设置

```bash
loom settings set memory_ask_recency_weight 0.35
loom settings reset memory_ask_recency_weight
```

值按其对应环境变量的方式写入，并以同样的方式校验：超出范围的值，或不是 live 的设置，会被拒绝，且不会存储任何内容。覆盖值是 `loom.runtime_overrides` 中的一行。存储它的进程会立即应用；其他所有进程（包括 worker）会在 `LOOM_RUNTIME_OVERRIDES_REFRESH_SECONDS`（默认 10）内应用。覆盖值在重启后依然保留，并在被重置之前优先于环境变量和设置文件；重置会恢复各进程自己的值或默认值。对没有已存储覆盖值的设置执行 `reset` 会被拒绝，而对于后续版本已不再有的设置，则会移除其覆盖值。每次更改都会写入一条 `setting.changed` 审计记录，包含更改前后的值。

`--expect <value>`（HTTP 中为 `expected`）只有在重新读取已存储的覆盖值后，当前生效的值正是该值时才会进行更改，否则返回 `409 PRECONDITION_FAILED` 并附上它找到的值：这样，之后通过其他副本或 CLI 做出的更改不会在未被察觉的情况下被覆盖。同一时刻发出的两个更改仍可能同时通过。

<a id="see-and-undo-past-changes"></a>

## 查看并撤销过去的更改

```bash
loom settings history memory_ask_recency_weight
loom settings history --limit 20      # every setting
```

每一行是一次已记录的更改，最新的在前：时间、操作者（`cli:<user>` 或令牌主体）以及更改前后的值。如果更改前的值本身是覆盖值，它就是该部署的值：用 `loom settings set` 将其设回即可撤销。否则，它是执行更改的那个进程上的值（来自其环境变量或默认值），并标记为 `(undo: reset)`：用 `loom settings reset` 撤销，这会恢复各副本自己的值。如果更改前没有值（重置了此版本已不再有的设置），则无需撤销。

覆盖值是部署的默认值，因此命名空间或请求自身设置的值仍然优先于它：包括命名空间的 `retrieval_defaults`（`top_k`、`min_similarity`、`reinforcement_weight`，以及 `namespace.register` 接受的其他搜索参数）和请求自身的字段。

相同的操作也可以通过 HTTP 完成，需要平台管理员令牌。关闭认证时，只接受来自回环地址（`localhost`）的更改：否则网页可能通过 DNS rebinding 访问到它。

```bash
curl -s "$LOOM_BASE_URL/v1/admin/settings.list" -H "Authorization: Bearer $TOKEN" \
  -H 'content-type: application/json' -d '{"changed": true}'
curl -s "$LOOM_BASE_URL/v1/admin/settings.set" -H "Authorization: Bearer $TOKEN" \
  -H 'content-type: application/json' -d '{"name": "memory_ask_recency_weight", "value": 0.35}'
curl -s "$LOOM_BASE_URL/v1/admin/settings.set" -H "Authorization: Bearer $TOKEN" \
  -H 'content-type: application/json' -d '{"name": "memory_ask_recency_weight", "clear": true}'
```

`POST /v1/admin/settings.history` 接受 `{"name": "...", "limit": 50}`。Python SDK 提供 `agent.deployment.settings()`、`set_setting(name, value, expected=...)`、`reset_setting(name, expected=...)` 和 `setting_history(name)`。

<a id="during-a-rolling-upgrade"></a>

## 滚动升级期间

如果某个版本没有某项设置、不以 live 方式读取它，或其字段拒绝已存储的值，它会记录一次该已存储的覆盖值并忽略它；其他进程仍会继续应用。运行时设置出现之前的版本会不加提示地忽略已存储的覆盖值，因此在这样的版本仍在提供服务期间，覆盖值只在较新的副本上生效。排序设置会改变 `memory.search` 的返回结果：保留更改之前，请先用基准测试衡量（[运行基准测试](https://github.com/ClickHouse/loom/blob/main/docs/benchmarks/running.md)）。
