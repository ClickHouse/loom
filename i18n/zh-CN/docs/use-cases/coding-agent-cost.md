<!-- i18n: source=docs/use-cases/coding-agent-cost.md blob=d7a120fc7dc895117d934de1f2dc5ffd14bca474 model=claude-sonnet-5-5 -->
> 本页由机器从[英文原文](https://github.com/ClickHouse/loom/blob/main/docs/use-cases/coding-agent-cost.md)翻译，内容以英文原文为准。
> Machine-translated from the [English page](https://github.com/ClickHouse/loom/blob/main/docs/use-cases/coding-agent-cost.md), which is authoritative.

<a id="lower-model-cost-for-coding-agents"></a>

# 降低编码 agent 的模型成本

借助 Loom 插件和代码索引，Claude Code 在回答有关 ClickHouse 代码库的问题时，在全部三组测得的问题集上，模型成本比不使用时低 6% 到 21%。在 24 个按答案评分的问题上节省了 20%（答案质量为 0.84，对比 0.78），在 36 个概念性问题上节省了 21%，在 24 个多跳问题上节省了 6%；在多跳问题中，将一个 bug 追溯到其在发布分支上的修复，两种方式的成本相同。

<a id="what-was-measured"></a>

## 测量内容

两组测试都在同一份代码检出上运行 Claude Code（`claude-sonnet-5`）：一组是普通会话，另一组使用 Loom 插件和 `coding` 工具集。成本是整个会话的模型费用，不包括 Loom 自身在服务端的模型调用、托管和索引费用。该对比衡量的是插件的整体效果，并未将代码索引与记忆分开评估。结果仅适用于所测试的工作负载。使用 Loom 时，一次提问在时钟时间上耗时更长：插件对每条提示的搜索以及 MCP 往返调用都会增加延迟。单次提问的成本如何与多次运行之间的噪声进行比较，请见结果页面。
[With and without Loom](https://github.com/ClickHouse/loom/blob/main/docs/benchmarks/results.md#with-and-without-loom) 包含所有测试集、测试协议和产物，[evaluate Loom](https://github.com/ClickHouse/loom/blob/main/docs/getting-started/evaluate.md) 则介绍如何在你自己的代码仓库上进行测量。

关于由模型读取记忆而非读取完整聊天历史的情况，请参阅
[agent memory](agent-memory.md#measured)。

<a id="how-the-plugin-is-designed-to-save"></a>

## 插件的节省设计

- **用索引答案代替读取文件。** `code.symbol`、`code.callers` 和 `code.search` 会返回能够回答结构问题的那几行代码。[代码索引](code-intelligence.md)
- **用已保存的上下文代替重复探索。** 会话中保存的决策和发现，会在下一次会话开始时加载。[共享记忆](agent-memory.md)

Loom 还会按操作记录嵌入（embedding）和 LLM 调用的花费。
[成本与用量](https://github.com/ClickHouse/loom/blob/main/docs/features/observability/cost-and-usage.md)
