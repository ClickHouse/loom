<!-- i18n: source=docs/guides/optimize-namespace.md blob=bcf139a5e94cdfb8e192ed26d7bf748075392110 model=claude-sonnet-5-5 -->
> 本页由机器从[英文原文](https://github.com/ClickHouse/loom/blob/main/docs/guides/optimize-namespace.md)翻译，内容以英文原文为准。
> Machine-translated from the [English page](https://github.com/ClickHouse/loom/blob/main/docs/guides/optimize-namespace.md), which is authoritative.

<a id="optimize-a-namespace-from-its-audit"></a>

# 根据审计结果优化命名空间

如果写入方把 `memory.set` 当作对象存储来用（以哈希键保存 JSON blob，把租约存成记忆，把文档存成 claim），每张表都会保持一致，但召回却毫无用处。本流程把这样的命名空间重新整理为 [investigation agent 指南](https://github.com/ClickHouse/loom/blob/main/docs/guides/investigation-agent-memory.md)中描述的布局。每一次迁移都会留下重定向，因此仍在运行的写入方继续读取旧键时，依然能得到答案。

[Claude Code](../features/integrations/claude-code.md) 和 [Codex](../features/integrations/codex.md) 插件中的 `loom-optimize` skill 会执行相同的步骤。

<a id="the-two-operations"></a>

## 两个操作

**`namespace.audit`**（`POST /v1/namespace.audit`，MCP `loom_namespace_audit`，CLI `loom audit-namespace`）。只读。

| 输入               | 默认值          |
| ------------------ | --------------- |
| `org`, `namespace` | 必填            |
| `window_seconds`   | 604800（7 天）  |
| `sample_limit`     | 5               |

它返回 `summary`（`live_rows`、`live_bytes`、`excerpt_bytes`、`version_rows`、`version_bytes`、`unembedded_rows`、`gets`、`searches`、`sets`、`get_search_ratio`，以及 `inspector_calls`：控制台和助手工具集发起的 get 与 search，不计入上述计数；从控制台发起的写入会计入 `sets`）、`findings[]`（`code`、`severity`、`count`、`share`、`bytes`、`samples[]`、`detail`、`remedy`）、`skipped`（无法运行的规则：检查、摘要读取、证据读取；其余规则仍会返回结果）、`coverage`、`healthy`（“没有严重问题”），以及 `criticals` / `warnings` / `infos` 的计数。样本包含键、行 id、标识列和计数，从不包含值。每条 finding 还带有[证据字段](https://github.com/ClickHouse/loom/blob/main/docs/features/namespaces-and-sharing.md#reading-a-namespaces-health)；计划会读取 `kind`（使用情况的启发式判断不能证明存在误用）、`samples_complete` 和 `evidence[]`，即每个样本行当前的 `version` 和 `content_sha256`。

**`memory.move`**（`POST /v1/memory.move`，MCP `loom_memory_move`）。

| 输入                                                                         | 默认值                      |
| ---------------------------------------------------------------------------- | --------------------------- |
| `org`, `namespace`, `agent`, `from_key`, `to_key`                            | 必填                        |
| `value`, `memory_type`, `tags`, `entities`, `valid_at`, `source_resource_id` | 沿用源行的值                |
| `keep_redirect`                                                              | `true`                      |
| `if_match_sha256`                                                            | 无                          |
| `dry_run`                                                                    | `false`                     |

它返回 `{from_key, to_key, redirected, memory_id, version, content_sha256, advisories, dry_run}`；试运行（dry run）还会返回 `memory_type` 和 `value_preview`。移动之后：

- `memory.get(from_key)` 仍返回旧值，并附带 `canonical_key`；
- `memory.get(from_key, follow=true)` 返回新行；
- `memory.search` 和 `memory.list` 只显示新行。

<a id="1-audit"></a>

## 1. 审计

```bash
loom audit-namespace --org <org> --namespace <ns> --window-days 7 --samples 5
```

该 CLI 在服务器上运行（直接读取 ClickHouse）。它会输出一张表（code、severity、kind、count、share、first sample），使用 `--json` 时则输出 output model，并在命名空间不健康时以退出码 1 退出。在其他任何地方，请通过 HTTP 或 MCP 调用 `namespace.audit`。

先看 `summary`：

- `get_search_ratio` 达到数百，说明该命名空间被当作键值存储使用（对 procedure 的读取是有意按键读回的，因此不计入其中）；
- `unembedded_rows` 大于零，说明存在搜索永远找不到的行。

然后再看各项发现。当 `healthy` 为 true 且没有 `warning` 或 `critical` 时即可停止：`info` 级别的发现是写入方需要留意的提示，不需要返工。`system_state` 类发现（索引积压、缺少遥测数据）与部署有关，与写入方无关；当 `telemetry_unavailable` 为 warning 时，`exact_key_store` 和 `intentional_exact_key_reads` 会被列入 `skipped`，而不是视为通过，`coordination_as_memory` 则只运行其 key 段分支（`samples_complete: false`）。

控制台的命名空间审计面板显示的是同一份审计结果。当命名空间不健康时（出现一项 critical 发现，或在 `exact_key_store`、`double_encoded_values`、`unembedded_rows` 和 `coordination_as_memory` 中出现两项 warning），`context.pack` 会携带 `namespace_health` 块，因此 agent 在会话开始时就能看到它。

<a id="2-plan"></a>

## 2. 方案

每个发现项写一个小节。只有审计返回的代码才有对应的小节。

| 发现项                                                                  | 变更内容                                                                 | 做法                                                                                                                                                   |
| ----------------------------------------------------------------------- | ------------------------------------------------------------------------ | ------------------------------------------------------------------------------------------------------------------------------------------------------ |
| `double_encoded_values`, `json_prefix_excerpts`, `hash_keys`            | 该行迁移到可读的键，值以结论句开头                                       | 解码一次；先写结论句，再写来源行，其余内容写成简短的行；使用带 `value`、`tags`、`entities`、`valid_at`、`if_match_sha256` 的 `memory.move`               |
| `oversized_values`, `episodic_blobs`                                    | 正文变成文档或 blob；保留键的行只留一段话                                | 先用 `knowledge.upsert`（Markdown，`source_uri=loom://<org>/<ns>/<key>`）或 `context.offload`，再用 `memory.move` 迁移旧键，并以这段话作为 `value`      |
| `unembedded_rows`                                                       | 值被拆分或缩短，以便生成嵌入                                             | 同 `oversized_values`；`namespace.validate` 会在其 `unembedded` 行中列出这些记录                                                                       |
| `coordination_as_memory`                                                | 该块内容转入工作记忆；旧键仍可继续应答                                   | 先 `working_memory.set`，再用 `memory.move` 并设置 `keep_redirect=true`                                                                                |
| `hand_rolled_index_rows`                                                | 这些行保持可读并增加 `tags`；写入方不再产生它们                          | 只加标签（`memory.update_by_id`）；不迁移                                                                                                              |
| `exact_key_store`, `version_churn`                                      | 写入方需要修改                                                           | 把这些键列给负责人                                                                                                                                     |
| `telemetry_unavailable`, `index_backlog`, `intentional_exact_key_reads` | 无需改动：这是部署的状态，或是一种工作流                                 | 不做整改；对于 `telemetry_unavailable`，待账本覆盖该时间窗口后重新运行；对于停滞的 `index_backlog`，检查嵌入 worker                                      |

mentor（`mentor.brief`）还会指出另外三种属于数据变更的形态。它们按同样的规则整改，以提示中的 `evidence` 作为计数：

| Mentor 提示              | 变更内容                                                                                        | 做法                                                                                                |
| ------------------------ | ----------------------------------------------------------------------------------------------- | --------------------------------------------------------------------------------------------------- |
| `documents_as_memories`  | 同 `oversized_values`                                                                           | 先 `knowledge.upsert`，再用 `memory.move`，以这段话作为 `value`                                     |
| `checkpoints_as_memory`  | 运行转储变成 `context.offload` blob；由一条以运行 id 为键的简短 episodic 行取而代之             | `context.offload`，再用 `memory.move`，`value` 为一行账本记录，`memory_type=episodic`               |
| `records_for_identities` | 以 `<prefix>-<id>` 为键且带标量字段的行，变成一个 schema 加每行一条 record                      | 先 `memory_schema.register` 一次，每行 `memory.record.write`，再用 `memory.move` 迁移到指针行        |

每个小节需说明：数量和字节数，三个“之前 → 之后”的示例键（取自该发现项的 `samples`），带参数的完整调用，以及它**不会**做的事：

- 不使用 `memory.delete`；
- 不原地修改值：重写后的值只会写入新键；
- 不处理最近一小时内写入的行：写入方可能正在写入过程中。

把方案作为知识文档存放在 `loom://<org>/<ns>/_optimize/plan-<YYYYMMDD>`（用 `knowledge.upsert` 并指定该 `source_uri`），这样负责人的批准以及整改前后的数字就集中在一处。

<a id="3-get-approval"></a>

## 3. 获得批准

命名空间所有者需以书面形式批准。展示计划不等于计划已获批准。部分批准仅适用于所指明的章节。如果所有者的写入方正在运行（审计中该时间窗口内的 `sets` 会显示这一点），请告知对方；无论如何，最近一小时的行都不在范围内。

<a id="4-apply"></a>

## 4. 应用

如果该命名空间很重要，请先备份：将 `memory.list … fields=["value"]` 的结果导出到 JSONL 文件；使用 `namespace.snapshot`（可通过 HTTP 或 SDK 调用，MCP 不提供此操作）；或者在服务器上使用 `CREATE TABLE … AS loom.memory` 加 `INSERT SELECT`（参见[备份与恢复](https://github.com/ClickHouse/loom/blob/main/docs/operations/backup-and-recovery.md)）。

1. 对每个部分的前三次移动使用 `dry_run=true` 运行，并读取返回的 `to_key`、`redirected` 和 `advisories`。
2. 然后按 `created_at` 顺序分批移动，每批最多 50 条，这样各个修订会成为新键的多个版本。
3. 传入审计样本或最新一次 `memory.get` 得到的 `if_match_sha256`。返回 409 表示该行已被他人修改：请暂停该部分，重新读取后再确认。
4. 如果回复中没有 `memory_id`、`version` 和 `content_sha256`，说明这次移动没有发生，不要计入。

```bash
curl -s "$LOOM_BASE_URL/v1/memory.move" -H "Authorization: Bearer $LOOM_API_KEY" \
  -H 'Content-Type: application/json' -d '{
    "org": "acme", "namespace": "agent-sre", "agent": "sre-bot",
    "from_key": "3f9a0c1e5b7d2a4c6e8f0a1b3c5d7e9f",
    "to_key": "sre/case/c-42/evidence/e-7",
    "value": "Disk on ingest-3 filled at 02:10 UTC.\nSource: alert A-981.",
    "tags": ["case:c-42", "kind:evidence"],
    "dry_run": true}'
```

<a id="5-measure"></a>

## 5. 度量

在相同的时间窗口内再次运行审计，并将前后对比表追加到计划文档中（对同一个 `source_uri` 执行 `knowledge.upsert`）。请将[写入规则](https://github.com/ClickHouse/loom/blob/main/docs/guides/investigation-agent-memory.md#writer-rules)提供给负责人。

<a id="6-retire-nothing"></a>

## 6. 不要退役任何内容

只要所有者愿意，旧键就可以一直通过重定向读取。一旦写入方改用新键，所有者可以用 `memory.delete` 将旧键标记为墓碑（软删除：`memory.history` 仍会保留它们）。此流程从不执行该操作。

> **警告：** 如果退役仍有活跃写入方在读取的键，会使其视图分裂：它的 `memory.get` 会返回 `found: false`，客户端可能回退到本地状态，两个存储随之出现不一致。重定向的存在正是为了防止这种情况。

<a id="related"></a>

## 相关内容

- [调查类 agent 记忆](https://github.com/ClickHouse/loom/blob/main/docs/guides/investigation-agent-memory.md)：目标布局
- [Agent 集成](https://github.com/ClickHouse/loom/blob/main/docs/guides/agent-integration.md#mistakes-to-avoid)
- [历史与来源追溯](https://github.com/ClickHouse/loom/blob/main/docs/features/memory/history-and-provenance.md)
