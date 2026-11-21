<!-- i18n: source=docs/guides/importing-clickgap-memory.md blob=5fff9080da2e5f36035250de4a491ae273d3484c model=claude-sonnet-5-5 -->
> 本页由机器从[英文原文](https://github.com/ClickHouse/loom/blob/main/docs/guides/importing-clickgap-memory.md)翻译，内容以英文原文为准。
> Machine-translated from the [English page](https://github.com/ClickHouse/loom/blob/main/docs/guides/importing-clickgap-memory.md), which is authoritative.

<a id="import-clickgap-memory"></a>

# 导入 ClickGap 记忆

`loom import-clickgap` 可将一种 SQLite 导出格式（ClickGap 的 `lessons` 存储）加载到 Loom 命名空间中；如果你没有这类导出文件，可以跳过本页。一条 lesson 是一条简短、已去重的评审记忆，带有类型化的实体、类别、kind、每行 TTL 和 `recall_count`。

有两个命令负责这项工作：

- `loom import-clickgap`：导入器（`server/cli/import_clickgap.py`），在 `loom` CLI 能够访问 ClickHouse 的环境中运行（服务器容器，或带有服务器 `.env` 的克隆仓库）。
- `python -m bench.clickgap_migration_validate`：验证器（`bench/clickgap_migration_validate.py`）。

<a id="what-the-importer-preserves"></a>

## 导入器保留的内容

| ClickGap                    | Loom                                                                    | 说明                                                                                                                                                                                                                                                                            |
| --------------------------- | ----------------------------------------------------------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `lessons.text`              | `loom.memory.value` + 已嵌入的 `content_excerpt`                        | 断言会被嵌入，推理过程不会                                                                                                                                                                                                                                                      |
| `lessons.reasoning`         | `value_json.reasoning`（其中 `.text` = 断言）                           | `get` 返回 `value_json`，因此断言与推理一同返回，二者不会被拼接在一起                                                                                                                                                                                                           |
| `lessons.kind`              | `memory_type`（大多为 1:1）**+** 标签 `kind:<kind>`                     | `summary → episodic`（不可搜索）：摘要仍可通过 `memory.get` / `memory.list` 和 `kind:summary` 标签访问，但不会在 `memory.search` 中参与排序。`kind:<kind>` 标签保留了原有的分类体系。参见 `_clickgap_memory_type` / `_CLICKGAP_TYPE_REMAP`。 |
| `lessons.source`            | `source_trace='clickgap://<class>/<source>'` + 标签 `source:<class>`    | 类别：pipeline / session / human / reviewer / eval                                                                                                                                                                                                                              |
| `lessons.created_at`        | `created_at`, `valid_at`, `temporal_anchor`                             | 原始记录时间，而非导入时间                                                                                                                                                                                                                                                      |
| `lessons.expires_at`        | `expires_at` **逐行原样保留**                                           | 绝不根据全局 TTL 重新计算                                                                                                                                                                                                                                                       |
| `lessons.recall_count`      | `recall_count` **和** `memory_embeddings.reinforcement_count`           | 参见[召回信号](#recall-signal)                                                                                                                                                                                                                                                  |
| `lessons.last_recalled_at`  | `last_recalled_at` **和** `last_reinforced_at`                          | 召回时近性维度                                                                                                                                                                                                                                                                  |
| `lessons.evidence_count`    | `evidence_count`                                                        |                                                                                                                                                                                                                                                                                 |
| `lesson_entities`           | `entities`（单独）+ `entity:<kind>:<text>` 标签 + `loom.entity_vectors` | 单独的 `entities` 数组不含类型，类型由标签携带                                                                                                                                                                                                                                  |
| `lesson_categories` + items | `loom.categories` + `loom.memory_categories`                            | 名称先做大小写折叠，再生成确定性的 `category_id`                                                                                                                                                                                                                                |
| `false_positive` lessons    | 标签 `polarity:negative`                                                | 抑制类（“不要再次提出”）记忆                                                                                                                                                                                                                                                    |
| 人工来源                    | 标签 `pii:human-attributed`                                             | 可按标签擦除的候选项                                                                                                                                                                                                                                                            |

**导入是幂等的。** `memory_key='lesson_<id>'` 和 UUIDv5 `memory_id`（由 `clickgap://<org>/<namespace>/lesson/<id>` 生成）都是确定性的，每次运行会标记一个新的 `version`。重新导入时，行会在 `ReplacingMergeTree(version)` 下被取代，行数保持不变。

<a id="1-dry-run"></a>

## 1. 试运行

不写入任何数据。试运行会检查每个时间戳是否都能解析，打印 kind → type 与 TTL 的交叉表以及类别数量，并探测 embedder，以免写入的行没有嵌入向量。

```bash
loom import-clickgap \
  --sqlite ~/Downloads/clickgap-memory-YYYYMMDD/memory.db \
  --org <org> --namespace clickgap --dry-run
```

确认交叉表与源数据一致，并且 embedder 可以连通。

<a id="2-import"></a>

## 2. 导入

使用**专用命名空间**（`clickgap`），这样回滚只需一次 `namespace.delete`。导出的数据不含向量，因此每一行在导入时都会使用当前的 `LOOM_EMBEDDING_MODEL` 生成嵌入。

```bash
loom import-clickgap \
  --sqlite .../memory.db --org <org> --namespace clickgap --agent clickgap
```

`--agent` 默认为 `clickgap`；`--batch-size` 设置每次 ClickHouse 插入的行数（默认 1000）。末尾的校验块会打印以下各项，如有不符则导致运行失败（退出码 1）：

- 存活的记忆行数与源数据中的 lesson 数量对比；
- 使用了错误模型的嵌入行数（必须为 0）；
- 永久（无 TTL）行的数量；
- 回读得到的最高 `recall_count`。

默认**不会**导入 `lesson_recalls`（每次召回的事件日志）：反规范化的 `recall_count` 列已经包含了排序信号。仅当你需要在 `loom.memory_usage_events` 中保留逐事件日志用于分析时，才传入 `--seed-recall-events`。

> **警告：** `--seed-recall-events` 是追加式的，不具备幂等性：只运行一次，并且导入到全新的命名空间。它会**丢弃 `pr_number`**，因为 Loom 的使用事件没有 PR 列。

<a id="recall-signal"></a>

## 召回信号

导入器会把 `recall_count` 镜像到 `memory_embeddings.reinforcement_count`，把 `last_recalled_at` 镜像到 `last_reinforced_at`。在 `memory.search` 中，这个计数会进入衰减强化排序项：`LOOM_MEMORY_SEARCH_REINFORCEMENT_WEIGHT` 决定该项的大小（0 表示禁用），`LOOM_MEMORY_SEARCH_REINFORCEMENT_HALF_LIFE_DAYS` 设置其衰减，请求也可以通过 `reinforcement_weight` / `reinforcement_half_life_days` 覆盖这两项（[配置](https://github.com/ClickHouse/loom/blob/main/docs/reference/configuration.md#memorysearch)）。

该项的上限低于相关性，因此 Loom **不会**复现 ClickGap 的 `recall_count DESC` 排序：它主要按相似度排序，并结合实体、时效和强化项（[检索与排序](../concepts/retrieval-and-ranking.md)）。如果你的工作负载依赖按召回频率排序，请在切换前验证最终的排序结果是否可以接受（验证器会报告召回顺序 τ）。

<a id="3-validate-before-cutover"></a>

## 3. 切换前验证

验证器使用 `server.auth.jwt.mint_token` 自行签发管理员令牌，因此需要设置 `LOOM_JWT_SECRET` 和显式的 `LOOM_BASE_URL`：没有默认目标。它的探测属于评估读取（`auto_touch: false`，以及 `bench/_gate.py` 的度量字段），因此一次运行不会在已导入的 lesson 上记录召回使用情况，重复运行也不会影响 τ 门槛所度量的强化信号。

> **警告：** 请将其指向可信的端点。探测查询就是 lesson 的断言；导入的数据不得通过第三方 embedder 离开你的基础设施。出于同样的原因，`--show-queries` 会打印被截断的探测文本，且默认关闭。

```bash
LOOM_BASE_URL=http://localhost:7777 LOOM_JWT_SECRET=... \
python -m bench.clickgap_migration_validate \
  --sqlite .../memory.db --org <org> --namespace clickgap --agent clickgap \
  --probes 200 --top-k 10 \
  --min-recall 0.90 --min-recallw 0.90 --min-tau 0.2
```

当门槛设置为大于 0 时，每个门槛未达标都会使运行以非零退出码失败（0 = 仅报告）：

| 门槛            | 度量内容                                                                                                                                                            |
| --------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `--min-recall`  | 已知条目 recall@k：探测所用的 lesson 本身是否在 Loom 的前 k 个结果中？按 kind 和按实体类型的细分可以发现单个切片的崩溃（例如类型重映射错误）。 |
| `--min-recallw` | 按召回加权的 recall@k：每个探测按其 `recall_count` 加权，因此漏掉热门 lesson 的代价更高。                                                              |
| `--min-tau`     | 召回顺序 τ：Loom 的排序与 `recall_count` 之间的 Kendall 一致性。这是一项诊断指标：Loom 并不按 `recall_count` 排序，因此不要期望得到很高的 τ。            |

失败的搜索会被报告并从比率中排除，绝不会被计为未命中，而且任何搜索失败都会使运行以非零退出码结束，无论是否设置了门槛。无法计算 τ 的运行会打印 `n/a`，并且未通过 `--min-tau`。

摘要（`summary → episodic`）不参与探测，因为它们在设计上不可搜索。`--seed`（默认 42）使探测样本可复现；`--concurrency`（默认 8）限制并发搜索的数量。

<a id="use-suppression-memories"></a>

## 使用抑制记忆

`false_positive` 类经验（“看起来像 bug，但不要再次提出”）构成抑制层。它们可以被搜索，并带有 `polarity:negative` 标签。检测器在决定是否提出一个候选发现时，会对该发现的文本运行带标签过滤的搜索：

```python
hits = await agent.memory.search(finding_text, tags=["polarity:negative"], top_k=5)
if hits and hits[0].similarity >= SUPPRESS_THRESHOLD:  # calibrate per embedder
    skip_or_downweight(finding)  # a known false positive
```

`tags` 过滤条件作用于每一路检索（向量、词法，以及按时间和置信度的回填），因此结果中只包含抑制记忆（见 `tests/unit/test_memory_search_service.py` 中的 `test_lexical_fallback_applies_caller_filters`）。一旦混入正向记忆，就可能掩盖真实的发现。

任何类别都可以通过其 `kind:<kind>` 标签用同样的方式过滤：`tags=["kind:principle"]`，或者用 `tags=["kind:false_positive", "kind:maintainer_preference"]` 构成多类别的屏蔽清单（`tags` 是 OR 过滤）。`memory.search` 的 `memory_type` 参数只接受粗粒度类型（`semantic`、`procedural`、`preference`、`reflection`、`decision`），因此完整的类别分类体系存放在标签上。

<a id="roll-back"></a>

## 回滚

- **完全回退：** 对 `clickgap` 执行 `namespace.delete`（需要管理员权限；通过 MCP 调用时使用 `loom_op`）。导入的目标是一个专用命名空间，因此不会删除其他任何内容。
- **擦除或群组清理：** 使用带命名空间范围的 `erasure.request`（由恢复 worker 设置检查点并恢复执行），或者针对维护者的删除请求，仅擦除 `pii:human-attributed` 行（[保留与擦除](https://github.com/ClickHouse/loom/blob/main/docs/features/memory/retention-and-erasure.md)）。
- **修正映射：** 重新运行导入器。确定性的键和更高的 `version` 会原地取代有问题的行。

<a id="consolidate-optional-after-validation"></a>

## 合并（可选，在验证之后）

如果要压缩实体集合近似重复的记忆，请在验证**之后**运行 `memory.consolidate`。`server/memory/app/consolidate.py` 中的防护逻辑会排除**已固定（pinned）**的行，并且绝不会跨 `memory_type` 或跨**极性（polarity）**合并，因此 `false_positive` 抑制记录不会被并入正向观察，原则（principle）也不会吸收观察（observation）。

请在命名空间上保持 `consolidation_enabled=false`（通过 `namespace.register` 设置），这样后台合并 worker 就不会对 ClickGap 已经汇总过的 `summary` 经验再合并一次。

<a id="related"></a>

## 相关内容

- [写入记忆](https://github.com/ClickHouse/loom/blob/main/docs/features/memory/writing.md)
- [检索与排序](../concepts/retrieval-and-ranking.md)
- [命名空间与共享](https://github.com/ClickHouse/loom/blob/main/docs/features/namespaces-and-sharing.md)
