<!-- i18n: source=docs/features/entity-graph.md blob=f2344ba078b13ea0d97951af2212b5da3b9af8bb model=claude-sonnet-5-5 -->
> 本页由机器从[英文原文](https://github.com/ClickHouse/loom/blob/main/docs/features/entity-graph.md)翻译，内容以英文原文为准。
> Machine-translated from the [English page](https://github.com/ClickHouse/loom/blob/main/docs/features/entity-graph.md), which is authoritative.

<a id="entity-graph"></a>

# 实体图

Loom 在同一个 ClickHouse 数据库中，将实体图与记忆行并列保存。实体是节点。同一条记忆中提到的两个实体之间存在共现边；抽取过程还会将带类型的 `(subject, relation, object)` 三元组作为第二组边加入图中。该图是记忆的投影：每条边都可以追溯到生成它的行。可以用它来回答向量搜索无法回答的问题，例如“哪些内容与 billing service 相关联”。

实体社区默认关闭（`LOOM_ENTITY_COMMUNITIES_ENABLED`）。

<a id="what-the-graph-holds"></a>

## 图中保存的内容

| 表                                                                       | 保存内容                                                                                                                                |
| ------------------------------------------------------------------------ | --------------------------------------------------------------------------------------------------------------------------------------- |
| `loom.entity_edges` (+ `entity_edge_memories`, `entity_edge_rows`)       | 每条共现观察记录一行（`co_occur = 1`），读取时按实体对求和。包含每个实体对的来源键，以及指明该实体对的行和值。 |
| `loom.entity_triples` (+ `entity_triple_memories`, `entity_triple_rows`) | 带有 `confidence` 的类型化三元组，抽取模型记录为 `adapter_name`。包含每个三元组的来源键，以及陈述该三元组的行和值。 |
| `loom.entity_degree`                                                     | 每个实体的共现度总数（`SummingMergeTree`），枢纽规则会读取它。                                                                          |
| `loom.entity_canonical_map`                                              | 提及文本 → 规范实体。                                                                                                                   |
| `loom.entity_communities`                                                | 每个实体在各层级上的社区 id。                                                                                                           |
| `loom.entity_vectors`, `loom.entity_summaries`                           | 搜索中的实体分支所读取的嵌入。                                                                                                          |
| `loom.derived_facts`                                                     | 带单位和日期的数值事实，用于搜索中的计算型答案。                                                                                        |
| `memory.link` edges                                                      | 由 agent 或用户断言的边（`supports`、`depends_on`、`caused` 等），由图模式搜索读取。                                                    |

边和三元组带有 `valid_from` / `valid_to`（来自源记忆的 `valid_at` / `valid_to`）以及用于使其失效的 `invalid_at` 时间戳。带 `as_of` 的读取只对在该时刻有效的观察记录求和。

图存放在 ClickHouse 中，这样边与其所属的行在同一个请求中写入，由同一个 saga 擦除，并且无需网络往返即可与记忆进行关联查询。代价是：一次遍历由一系列有界的聚合查询组成，因此每次遍历都有上限。

<a id="how-the-graph-is-built"></a>

## 图的构建方式

记忆行最先写入，图中的行是它的投影。投影失败会记录到 `loom.memory_projection_failures` 并重试，但只有当该行仍保留投影失败时的那个值时才会重试。投影失败从不会导致写入失败。`high` 敏感级别的批量条目不投影任何内容；`high` 级别的 `memory.set` 只投影写入方声明的实体之间的边，从不投影其文本。

1. **实体。** 写入方在 `memory.set` 中传入 `entities`，或由 `memory.set_from_messages` 提取实体。`entity_extractor` worker（`LOOM_ENTITY_EXTRACT_ENABLED`，默认开启）会在写入路径之外，为未带实体写入的行打上标签；它需要真实的 LLM provider。实体文本在写入和读取时按相同方式规范化（去掉冠词和所有格，中心名词转为单数，第一人称映射为 `user`），因此 "The Billing Service's" 和 "billing service" 是同一个节点。`commit:<sha>` 或 `error:<name>` 形式的名称是标识符，规范化方式相同，但不会转为单数：`error:Timeouts`，或放在反引号或引号中的同一名称，对应的节点是 `error:timeouts`。冒号后紧跟空白或标记符号（`Error: the build failed`、``error:`Timeouts` ``）时，则视为普通文本。代词、感叹词、轻动词和两个字符的 token 会被丢弃；按原样书写或规范化之后（NFKC 会把 `ﬁ` 转为 `fi`）超过 256 个字符的文本也会被丢弃，写入时和在 `entity_filter` 中都是如此；点写入则会拒绝这样的事实主语或宾语，而不是丢弃。
2. **边。** 一条记忆的实体两两配对，每一对成为一行观察记录，每条记忆最多 100 对。
3. **三元组。** 提取出的事实的关系会成为三元组，每条记忆最多 8 个。动词规范化为 `lower_snake` 形式。`namespace.register_ontology`（管理员）可以注册首选的实体类型、动词和属性，并指定基数。集合之外的名称会产生 `ontology_warnings`；不会拒绝任何内容。
4. **取代会关闭三元组。** 当一行取代另一行时，旧行中新行未予证实的开放三元组会获得 `invalid_at`。对于 `one` 关系（雇主、所在地），较新的值保持有效，较旧的值在较新的值开始时关闭。如果在关闭过程执行期间有写入再次声明较旧的值，则它保持开放。
   - 各值按起始日期排序（未标注日期的值以到达时间为起始），再按精确到微秒的到达顺序排序，因此并发写入方会就同一个当前值达成一致。在同一次写入中，后声明的值更新。
   - 在同一次写入中，过去时态的无日期值（`lived_in`、`worked_at`）比有日期的值更旧，与顺序无关："user lived_in london" 与 10 月 1 日起的 Paris 并列时，Paris 保持有效。
   - 每个值以其最新的一行为准："London from Oct 15, Paris from Oct 14, London from Oct 13" 的结果是 London 保持有效。
   - 带有自身结束时间的值（"London until December"）绝不会被提前关闭。它会与较新的值并存有效，直到该结束时间。
   - 撤销（`memory.restore`）会使恢复的值从恢复时起成为当前值，或从事实自身较晚的起始时间起生效（"moving to Berlin on Dec 1"）。另一个值会关闭，但其行仍保持有效。日期更晚的值仍然优先，撤销之后声明的值也是如此。来回撤销不会增加权重。
   - 失败的关闭操作会按引发它的那次写入或撤销发生时的状态重放，因此之后声明的值仍保持有效。

   撤销如何重述其事实和边、如何为它们加权以及如何重放，见[记忆架构](https://github.com/ClickHouse/loom/blob/main/docs/architecture/memory-architecture.md#undo)。

点写入（`memory.set`、`memory.update`、`memory.update_by_id`）会写入调用方在 `relations` 中传入的三元组，最多 8 个，可通过 HTTP 以及 SDK 的类型化操作使用（不在 MCP 列表中）；它不做任何提取。自动提取在批量路径上运行：`memory.set_from_messages`，或提供关系的批量写入方。使用默认的 fake provider 时，只有你传入的实体会形成边。

<a id="operations"></a>

## 操作

| 操作                         | 角色   | 回答的问题                                                                                                    |
| ---------------------------- | ------ | ------------------------------------------------------------------------------------------------------------- |
| `facts.list`                 | viewer | 按 `subject`、`object`、`relation`（AND）或 `entity`（任一端）过滤的三元组。                                 |
| `facts.related`              | viewer | 与某个实体共现的实体，按强度从高到低排列，并指明种子位于哪一侧。                                              |
| `facts.traverse`             | viewer | 从种子出发 N 跳以内的邻域。                                                                                   |
| `facts.path`                 | viewer | 在 `max_hops` 以内，两个实体之间最短的边链。                                                                  |
| `facts.expire_by_adapter`    | admin  | 将某个提取器写入的三元组标记为墓碑。记忆保留。                                                                |
| `memory.search`（graph 模式）| viewer | `search_mode="graph"`：返回与查询中的实体相距一跳的命中（[搜索与问答](https://github.com/ClickHouse/loom/blob/main/docs/features/memory/search-and-ask.md)）。 |
| `memory.related`             | viewer | 与给定记忆共享实体的记忆。                                                                                    |
| `memory.timeline`            | viewer | 某个实体的记忆，按时间顺序排列（`entity=…`）。                                                                |
| `memory.backlinks`           | viewer | 与某一行共享实体的记忆。                                                                                      |
| `namespace.taxonomy`         | viewer | 命名空间中的顶层实体，以及已计算出的社区。                                                                    |

字段列表：[事实](https://github.com/ClickHouse/loom/blob/main/docs/reference/operations/facts.md)、[记忆](https://github.com/ClickHouse/loom/blob/main/docs/reference/operations/memory.md)。在 graph 模式之外，`entity_filter` 会提升与指定实体相关的行的排名，搜索的实体通道还能找到那些以其他措辞提到查询中实体的行（[检索与排序](https://github.com/ClickHouse/loom/blob/main/docs/concepts/retrieval-and-ranking.md)）。

<a id="factslist"></a>

### `facts.list`

不加过滤时，返回按 `co_occur` 排序的前几个三元组；`sort_by="recency"` 则按 `last_seen` 排序。

| 字段                    | 作用                                                                                                 |
| ----------------------- | ---------------------------------------------------------------------------------------------------- |
| `include_sources`       | 为每个三元组附加最多约十个来源记忆键。                                                               |
| `include_source_status` | 读取每个来源行；当三元组仍为开放状态、但其背后的所有行都已被取代时，将其标记为 `stale`。             |
| `provenance`            | `extracted`（两端都在源文本中）、`inferred`，或 `facet`（Loom 生成的导航边）。                       |
| `as_of`                 | 保留在该时刻有效的三元组。                                                                           |

<a id="factstraverse"></a>

### `facts.traverse`

遍历先裁剪再展开。每一跳把当前边界作为一次查询发出，在有效期窗口内按实体对汇总观测次数，丢弃已访问的实体，并为每个父节点保留最强的 `top_k_per_hop` 个实体对。第 h 跳的边界最多有 K^h 个实体。每个实体只在最先到达它的那一跳报告一次，并经由其最强的父节点，因此结果可以画成一棵树。

如果用一次递归查询先展开全部再裁剪，就会把整个邻域物化出来；在大型命名空间上，三跳遍历可能超出 ClickHouse 的内存限制。逐跳裁剪使开销受限于结果的大小。

| 字段                    | 默认值                                   | 作用                                                                                                           |
| ----------------------- | ---------------------------------------- | -------------------------------------------------------------------------------------------------------------- |
| `max_hops`              | 2（1–4）                                 | 遍历深度。                                                                                                     |
| `top_k_per_hop`         | 20（最大 100）                           | 每一跳为每个父节点保留的子节点数。                                                                             |
| `min_co_occur`          | 1                                        | 每个实体对的最小观测次数。                                                                                     |
| `max_nodes`             | 400（最大 5,000）                        | 硬性上限；达到时 `truncated=true`。                                                                            |
| `as_of`                 | 未设置                                   | 保留在该时刻有效的观测。                                                                                       |
| `labeled` / `relations` | false / 未设置                           | 遍历带类型的三元组（全部，或仅限这些动词）；节点带有 `via_relation`、`via_direction`、`via_provenance`。       |
| `hub_degree`            | 0（关闭）                                | 总度数不小于该值的节点为枢纽。                                                                                 |
| `hub_share`             | `LOOM_FACTS_TRAVERSE_HUB_SHARE`（0.01）  | 当到达某节点的边占其度数的比例不超过该值时，该节点为枢纽。                                                     |
| `rank_by`               | `LOOM_FACTS_TRAVERSE_RANK_BY`（`cosine`）| 每跳裁剪依据：`co_occur`、`specificity`（`via_co_occur / degree`）或 `cosine`（`via_co_occur / sqrt(degree)`）。 |

每个节点会报告 `hop`、`cumulative_weight`、`via_co_occur`、`path`（从种子出发的链）、`valid_from` / `valid_to`、`degree`，以及存在社区时的 `community_id`。节点先按跳数排序，再按权重排序。

**枢纽。** 泛化的实体（"user"、"pr"）会与所有内容共现。枢纽会被报告（`hub: true`，并列在 `hubs` 中），但不会被展开。种子始终会展开，因此要查看某个枢纽的邻域，可以把它作为种子。`hub_share` 是默认规则，因为它不依赖命名空间的度数尺度。`hub_degree` 则因命名空间而异；设置之前请先查看度数的长尾分布。

<a id="factspath"></a>

### `facts.path`

在相同的边上做有界的双向广度优先搜索，在中间相遇。`max_hops`（1–4，默认 3）限定总长度。结果包含 `found`、`path`，以及带有每个实体对的 `co_occur` 及有效期的 `edges`。`truncated=true` 表示结果可能不完整。`found=false` 且 `truncated=false` 表示在 `max_hops` 以内没有连接。

<a id="example"></a>

## 示例

```python
from loom import Agent

async with Agent("dev", "local", "sre-bot", base_url="http://localhost:7777") as agent:
    await agent.memory.set(
        "billing/depends_on",
        "The billing service reads invoices from the ledger database.",
        entities=["billing service", "ledger database"],
    )
    walk = await agent.facts.traverse("billing service", max_hops=2, top_k_per_hop=10)
    for node in walk["nodes"]:
        print(node["hop"], " > ".join(node["path"]), node["via_co_occur"])

    route = await agent.facts.path("billing service", "ledger outage")
    print(route["found"], route["path"], route["truncated"])
```

通过 HTTP，可以按图在过去某一时刻的状态进行遍历：

```bash
curl -s localhost:7777/v1/facts.traverse -H "Authorization: Bearer $LOOM_API_KEY" \
  -H 'content-type: application/json' \
  -d '{"org":"dev","namespace":"local","seed_entity":"billing service","max_hops":2,"as_of":"'"$AS_OF"'"}'
```

通过 MCP，调用 `loom_op`，并设置 `op="facts.traverse"`。

<a id="maintaining-the-graph"></a>

## 维护图谱

| 任务                | 方法                                                                                                           |
| ------------------- | -------------------------------------------------------------------------------------------------------------- |
| 回滚某个有问题的提取器产生的三元组 | `facts.expire_by_adapter(adapter_name=…, max_confidence=…)`。记忆会保留。                                |
| 导出图谱              | `loom entity-export --org O --namespace N --format graphml`（或 `jsonld`；`--all` 会包含已失效的边）。 |
| 将实体聚类为社区          | 设置 `LOOM_ENTITY_COMMUNITIES_ENABLED=true`（见下文）。                                                            |
| 查看图谱              | 控制台的 Graph 视图（[控制台](https://github.com/ClickHouse/loom/blob/main/docs/features/dashboard.md)）。                                                      |

`entity_communities` worker 会按命名空间运行标签传播，并将结果写入 `loom.entity_communities`，供 `namespace.taxonomy`、图模式扩展和 `community_id` 读取。它覆盖启用了整合的命名空间，以及在最近 `LOOM_ENTITY_COMMUNITIES_WALKED_DAYS`（14）天内被遍历过的命名空间。它会先丢弃度数最高的实体（`LOOM_ENTITY_COMMUNITIES_HUB_DROP_FRACTION`），当某个社区包含的实体超过 `LOOM_ENTITY_COMMUNITIES_GIANT_SHARE` 所占比例时，会将该丢弃比例加倍，这样通用 token 就不会把整个图谱合并成一个社区。

<a id="settings"></a>

## 设置

| 设置                                         | 章节                                                                                                                         |
| -------------------------------------------- | ---------------------------------------------------------------------------------------------------------------------------- |
| `LOOM_FACTS_TRAVERSE_HUB_SHARE`, `…_RANK_BY` | [命名空间健康度、知识图谱、社区](https://github.com/ClickHouse/loom/blob/main/docs/reference/configuration.md#namespace-health-knowledge-graph-communities) |
| `LOOM_ENTITY_COMMUNITIES_*`                  | 同一章节。                                                                                                                   |
| `LOOM_ENTITY_EXTRACT_*`                      | [整合](https://github.com/ClickHouse/loom/blob/main/docs/reference/configuration.md#consolidation)                                                                 |
| `LOOM_MEMORY_SEARCH_GRAPH_SEED`              | [记忆搜索](https://github.com/ClickHouse/loom/blob/main/docs/reference/configuration.md#memorysearch)                                                                   |

<a id="limits"></a>

## 限制

- **共现不等于关系。** 同一条记忆中出现的两个实体会被关联起来，无论句子本身表达的是什么。如果关系很重要，请使用类型化三元组（`labeled`、`relations`）或 `memory.link`。
- **图的质量取决于实体的质量。** 没有实体的行不会产生任何边，直到 `entity_extractor` worker 处理到它们。
- **遍历有范围限制。** 在断定两个实体没有连接之前，请先查看 `truncated`。
- **边可能滞后于被取代的行。** `facts.list(include_source_status=true)` 会显示仍处于未关闭状态的三元组。
- **有效性指有效时间。** `as_of` 读取的是事实成立的时间，而不是 Loom 得知该事实的时间。

<a id="related"></a>

## 相关内容

- [搜索与提问](https://github.com/ClickHouse/loom/blob/main/docs/features/memory/search-and-ask.md)：graph 模式。
- [写入记忆](https://github.com/ClickHouse/loom/blob/main/docs/features/memory/writing.md)：传入 `entities`。
- [提取](https://github.com/ClickHouse/loom/blob/main/docs/features/memory/extraction.md)：提取出的实体和三元组的来源。
- [记忆架构](https://github.com/ClickHouse/loom/blob/main/docs/architecture/memory-architecture.md)：投影写入器。
- [Workers 参考](https://github.com/ClickHouse/loom/blob/main/docs/reference/workers.md)。
