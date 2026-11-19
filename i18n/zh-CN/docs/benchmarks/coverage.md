<!-- i18n: source=docs/benchmarks/coverage.md blob=9fcb841d4e8d303e5db5edafbe9a2beeb448ef11 model=claude-sonnet-5-5 -->
> 本页由机器从[英文原文](https://github.com/ClickHouse/loom/blob/main/docs/benchmarks/coverage.md)翻译，内容以英文原文为准。
> Machine-translated from the [English page](https://github.com/ClickHouse/loom/blob/main/docs/benchmarks/coverage.md), which is authoritative.

<a id="benchmark-coverage"></a>

# 基准测试覆盖范围

本矩阵列出了 Loom 的记忆、知识和代码流程中，哪些部分已有可复现的公开测量结果。它区分了已测量的行为、可运行的测试框架，以及尚无公开质量证据的产品能力。

<a id="status"></a>

## 状态

| 状态                | 含义                                                                 |
| ------------------- | -------------------------------------------------------------------- |
| **已发布**          | 已提交结果工件、模型记录、协议、局限性说明和复现命令。               |
| **部分**            | 公开结果衡量了评估问题的一部分，但未覆盖完整流程或具有代表性的规模。 |
| **仅有测试框架**    | 存在可运行的协议，但没有已提交的真实 provider 测量结果来支持公开的性能结论。 |
| **无公开测量**      | 仓库中没有针对该问题的完整公开协议和工件。可能存在产品文档。         |

单元测试、dry run 或 fake-provider 检查都不算作已发布的质量结果。
[结果台账](https://github.com/ClickHouse/loom/blob/main/docs/benchmarks/results.md)是已发布数字的来源；[测试框架清单](https://github.com/ClickHouse/loom/blob/main/bench/README.md)是可运行协议的来源。

<a id="full-flow-matrix"></a>

## 全流程矩阵

| 层面 | 评测问题 | 所需指标 | 状态 | 公开证据或下一步所需结果 |
| ------------------------- | ----------------------------------------------------------------------------------------------------------------------------------------------------------- | --------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- | ------------------------- | ----------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| 记忆构建 | 提取是否写入有依据的事实，并避免无依据的断言？ | 证据覆盖率、提取精确率、虚假记忆率、token 数和构建时间 | **仅有测试框架** | 在公开对话集上运行 `memory_integrity.py` 和 `extraction_eval.py`；提交每一条提取出的事实和评判结论。 |
| 记忆检索 | 正确的记忆是否排在靠前位置？ | recall@k、rank@1、MRR 和干扰项侵入率 | **已发布** | [结果台账](https://github.com/ClickHouse/loom/blob/main/docs/benchmarks/results.md)中的 LongMemEval-S、LoCoMo、MuSiQue 和 MemoryAgentBench 产物。 |
| 记忆回答 | 最终回答是否正确使用了检索到的证据？ | 回答准确率、引用正确率和各类别的失败情况 | **已发布** | LongMemEval-S、LoCoMo、MemBench、MemoryAgentBench 和 PerLTQA。 |
| 未知问题 | Loom 在证据薄弱时是否会拒答，而不是勉强作答？ | 拒答精确率/召回率、错误回答率和可回答问题的召回率 | **部分完成** | 已发布 LoCoMo 对抗性问题和十个代码拒答探测；仍需要更大规模、经过校准的记忆拒答数据集。 |
| 更正与时间 | 当前值是否会取代过时的事实，同时历史查询仍然正确？ | 当前值准确率、过时回答率、时间准确率和取代精确率 | **仅有测试框架** | 在同一个公开语料上发布 `pi_llm.py`、`forget_switch_eval.py`、`fama_forgetting.py` 和 `supersession_audit.py` 的结果。 |
| 跨工具连续性 | 一个客户端写入的事实能否在之后的会话中帮助另一个客户端？ | 跨客户端召回率、来源出处、捕获丢失和延迟 | **仅有测试框架** | `cross_tool_validate.py` 目前只有六条查询；请发布更大的 Claude Code、Codex 和 Cursor 矩阵，并附上原始捕获数据。 |
| 记忆效率 | 检索是否节省了上下文、模型开销和轮次？ | 任务成功率、提示/输出 token 数、模型成本和构建盈亏平衡点 | **已发布** | 成对的 LongMemEval 完整历史对比结果显示 reader token 减少 91%，并给出了构建盈亏平衡点。 |
| 知识检索 | 文档 RAG 能否找到并引用正确的段落？ | 分块 recall@k、回答准确率、引用精确率、拒答、延迟和 token 数 | **部分完成** | [设计决策](https://github.com/ClickHouse/loom/blob/main/docs/benchmarks/results.md#design-decisions-from-design-documents)中的第 2 组实验，在由五份文档组成、使用单一评判模型的私有数据集上，衡量 `loom_search` 知识命中的回答准确率（A 和 D 相对标准答案的精确率与召回率）、引用精确率与召回率、拒答以及注入的 token 数；仍需要分块 recall@k、延迟，以及公开数据集（使用固定 reader 的 `narrativeqa.py`，包含困难的不可回答和引用用例）。 |
| 源格式 | 哪些输入能得到等价的可搜索文本？ | 按格式统计的提取成功率、文本/表格保真度、OCR 质量和失败率 | **部分完成** | [文档解析](https://github.com/ClickHouse/loom/blob/main/docs/benchmarks/results.md#document-parsing)在已提交的语料上，以版面模型为基线，衡量 PDF、DOCX 和 PPTX 的文本、表格和定位信息保真度、OCR 质量以及静默丢失；仍需要 CSV、XLSX、Parquet、HTML 及其他文本格式的结果，以及一个获得许可的真实语料。 |
| 代码概念搜索 | 以 issue 形式描述的文本能否找到对应实现？ | 文件 hit@k、全部标准文件 hit@k、MRR、拒答、token 数和延迟 | **已发布** | ClickHouse 概念性问题，以及 200 个实例的 SWE-bench Verified 文件定位样本；这是检索评测，不是官方的补丁加测试得分。 |
| 代码结构 | 定义和调用方是否完整？ | 定义 exact@1、调用方精确率/召回率/F1 和完整性标签 | **已发布** | 针对 ClickHouse 的十个定义问题和四个调用方问题。调用方数据集太小，不适合做接近结果之间的比较。 |
| 代码语言可移植性 | 质量能否在不同语言和仓库形态之间迁移？ | 按语言和仓库统计的相同检索与结构指标 | **仅有测试框架** | [语言表](https://github.com/ClickHouse/loom/blob/main/docs/features/code-index/overview.md#language-support)记录了解析器层级；`make bench-code-portability-dry` 只检查评分。请针对实际索引运行公开的测试数据。 |
| 代码 agent 成果 | agent 是否完成了任务，而不只是找到文件？ | 解决率、测试通过数、补丁质量、轮次、token 数、成本和时间 | **部分完成** | 成对的 ClickHouse 运行会将 agent 的回答与经过验证的参考答案（解释、影响、历史）对照评分，并报告成本、轮次和时间；它们不编辑代码，也不运行测试。仍需要一个控制了数据泄漏的补丁加测试基准。 |
| 设计决策 | 从设计文档中提供的决策，能否避免 agent 重新提出已被否决的方案？它对“为什么”和功能问题的回答是否正确、有引用且是最新的？ | 避免被否决方案、约束满足、论据与功能事实的精确率/召回率、引用精确率/召回率、过时陈述和失败的拒答、注入的 token 数、提取精确率 | **部分完成** | [设计决策](https://github.com/ClickHouse/loom/blob/main/docs/benchmarks/results.md#design-decisions-from-design-documents)基于由五份文档组成的私有数据集和单一评判模型；仍需要评判模型相对人工标注的准确率以及提取精确率。 |
| 索引构建 | 冷启动索引的成本是多少？ | 按仓库规模统计的耗时、嵌入/模型 token 数、ClickHouse 存储和 worker 峰值资源 | **部分完成** | SWE-bench 运行报告了 11 次回填加 188 次增量共消耗 137.5 million 嵌入 token、约 10.6 小时，但没有规模曲线，也没有硬件记录。 |
| 索引新鲜度 | 一次提交多久后可被搜索到，回答何时会过时？ | 提交到查询的延迟、增量成本、过时回答率和中断后的恢复 | **仅有测试框架** | 已有阶段延迟和过时性测试框架；请发布冷启动、增量和修复运行的结果，并附上仓库规模和硬件信息。 |
| 搜索容量 | 随着行数、仓库、租户和并发的增长，会发生什么？ | 混合工作负载下的 p50/p95/p99、吞吐量、ClickHouse 读取量、CPU 和内存 | **部分** | 已发布单服务器延迟，[大规模记忆](https://github.com/ClickHouse/loom/blob/main/docs/benchmarks/results.md#memory-at-scale)记录了硬件、多租户形态（10,000 个命名空间上的 Zipf 分布）以及记忆在同一台共享主机上的上限。目前没有云端运行结果、跨规模的容量曲线，也没有具有代表性的混合 agent 负载。 |
| 缓存正确性 | 重复的问题能否避免合成，同时不复用错误的答案？ | 在所选阈值下的命中率、节省的延迟和 token、误共享率 | **已发布** | 阈值扫描和 30 天的生产环境汇总数据均已公开。重复请求占观测到的调用的 3%。 |
| 跟踪器质量 | 重复检测能否找到较早的 issue，且误报率可接受？ | 正例命中率、判定精确率、负例误报率和延迟 | **已发布** | 104 个公开的 ClickHouse 重复 issue 和 100 个负例。其他跟踪器形态尚未覆盖。 |
| 安全与隐私 | 正常使用或对抗性使用是否会泄露、污染或保留已擦除的数据？ | 提取率、污染成功率、良性误报、泄露和残留 | **仅有测试框架** | 已有红队、敏感度、污染和擦除测试框架；但没有已提交的真实提供商结果可支持公开的安全率。 |
| 可靠性 | 被中断的写入、索引或擦除能否无损恢复？ | 故障注入后的恢复成功率、重复工作、不可用时间和残留 | **无公开测量** | 单元测试和集成测试覆盖了各项机制，但没有公开的工作负载级故障注入结果。 |
| 总运营成本 | 在计入模型、ClickHouse 和索引构建开销之后，Loom 的成本是多少？ | 构建和查询的模型开销、ClickHouse 计算/存储、worker 资源和盈亏平衡点 | **部分** | 已发布模型 token 和部分配对成本，[大规模记忆](https://github.com/ClickHouse/loom/blob/main/docs/benchmarks/results.md#memory-at-scale)按标价给出了每百万条记忆的 ClickHouse 存储成本和每百万次调用的 CPU 成本；Postgres、合并和闲置容量尚未纳入总成本曲线。 |

<a id="what-belongs-in-the-readme"></a>

## README 中应包含的内容

README 包含多项公开基准测试，并按其衡量的评测结果分组。只有当本矩阵中标注为 **Published**、样本量足以支撑解读，并且能用一句话清楚说明该结果在哪些情况下不适用时，相应的行才应放入 README。

不要把标注为 **Harness only** 或 **No public measurement** 的行移入 README 的性能声明，请改为链接到本矩阵。测试框架（harness）只能证明 Loom 可以被测量，并不能证明 Loom 表现良好。

<a id="publication-backlog"></a>

## 待发布事项

下列每一项都对应上方评测矩阵中的评测问题：

- **写入完整性与更正：**在同一个公开对话语料库上评测提取精度、错误记忆率、取代（supersession）率和过期答案率。
- **知识 RAG：**评测文本文档上的检索、回答和引用质量，随后再针对 PDF、CSV 和结构化文本，使用有授权的格式保真语料库。
- **端到端代码 agent 成效：**在控制数据泄漏的前提下评测补丁生成和测试通过率，并与未使用 Loom 的同一 agent 做配对对比。
- **实时代码可移植性：**使用真实的模型提供商，在现有的多语言、SQL/shell 和开源（OSS）测试集上进行测量。
- **索引成本与新鲜度：**按仓库规模给出冷启动和增量索引曲线，并记录硬件、ClickHouse 存储和 worker 资源。
- **容量与可靠性：**在多个租户之间运行记忆与代码混合负载，随后进行 worker、ClickHouse 和网络故障注入。
- **安全与隐私：**单独发布针对已命名攻击的结果，不要将其合并为一个笼统的安全评分。

每项新结果都遵循[添加结果](https://github.com/ClickHouse/loom/blob/main/docs/benchmarks/results.md#adding-a-result)的要求：提交原始产物，记录所用的每个模型和服务器构建版本，说明数据集许可证，附上完整命令，报告失败和负面发现，并补充不确定性或单个问题变动所带来的影响幅度。

<a id="related"></a>

## 相关内容

- [评估 Loom](https://github.com/ClickHouse/loom/blob/main/docs/getting-started/evaluate.md)：选择产品测试或工作负载测试。
- [方法论](https://github.com/ClickHouse/loom/blob/main/docs/benchmarks/methodology.md)：两个数字何时可以比较，以及它们在哪些方面存在偏差。
- [运行](https://github.com/ClickHouse/loom/blob/main/docs/benchmarks/running.md)：提供方、数据集和命令。
- [结果](https://github.com/ClickHouse/loom/blob/main/docs/benchmarks/results.md)：可根据已提交的文件复现的测量结果。
