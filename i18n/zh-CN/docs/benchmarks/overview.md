<!-- i18n: source=docs/benchmarks/overview.md blob=f3e3a30fcb2c5a330a968c35eb380c8c210ad12f model=claude-sonnet-5-5 -->
> 本页由机器从[英文原文](https://github.com/ClickHouse/loom/blob/main/docs/benchmarks/overview.md)翻译，内容以英文原文为准。
> Machine-translated from the [English page](https://github.com/ClickHouse/loom/blob/main/docs/benchmarks/overview.md), which is authoritative.

<a id="benchmarks-overview"></a>

# 基准测试概览

排序参数、相似度阈值、缓存阈值和提取提示词（prompt）所改变的行为，单元测试无法判断。每一项都由 `bench/` 下的测试框架（harness）决定，这些框架通过 HTTP 对运行中的服务器和真实的提供方执行测试。本部分介绍[方法论](https://github.com/ClickHouse/loom/blob/main/docs/benchmarks/methodology.md)、套件的[运行方式](https://github.com/ClickHouse/loom/blob/main/docs/benchmarks/running.md)及其数据来源、[覆盖矩阵](https://github.com/ClickHouse/loom/blob/main/docs/benchmarks/coverage.md)、仓库可复现的[结果](https://github.com/ClickHouse/loom/blob/main/docs/benchmarks/results.md)、如何[添加用例](https://github.com/ClickHouse/loom/blob/main/docs/benchmarks/adding-a-case.md)，以及[技能评测](https://github.com/ClickHouse/loom/blob/main/docs/benchmarks/skill-eval.md)。

[`bench/README.md`](https://github.com/ClickHouse/loom/blob/main/bench/README.md) 列出了所有测试框架。每个脚本的模块文档字符串就是它的手册：包含协议、参数和运行命令。

<a id="what-the-suite-measures"></a>

## 测试套件衡量什么

测试工具（harness）是独立的脚本：argparse 加一个 HTTP 客户端，没有共享的 fixture，也没有测试运行器。

| 领域 | 问题 | 主要 harness |
| ------------------------ | ---------------------------------------------------------------------------------------------- | -------------------------------------------------------------------------------------------------------------------------------- |
| 检索召回 | 正确的记忆是否进入了前 K 个结果？ | `quality.py`, `retrieval_recall.py`, `k_curve.py`, `plane_ablation.py`, `hard_distractors.py` |
| 回答准确率 | 在固定的 reader 和 judge 下，基于检索到的记忆生成的回答是否正确？ | `longmemeval.py`, `locomo_qa.py`, `memoryagentbench.py`, `membench.py`, `beam.py`, `musique.py`, `msc_dmr.py`, … |
| 写入端质量 | 提取是否只写入了对话轮次中所说的内容，且没有多写？ | `extraction_eval.py`, `graph_eval.py`, `memory_integrity.py`, `supersession_audit.py` |
| 时间与修订 | 被取代的事实是否在其时间区间结束后被隐藏，并在区间内保持可见？ | `fama_forgetting.py`, `mutation_sequence.py`, `fact_consolidation.py`, `pi_llm.py` |
| agent 与操作 | agent 是否选对了 op，并让存储保持正确的形态？ | `op_planning.py`, `skill_eval.py`, `op_trajectories.py`, `procedure_replay.py` |
| 成本 | 一次回答或一次会话的成本是多少？ | `ask_cost_rollup.py`, `session_ab.py`, `route_replay.py`, `ch_cost_by_op.py` |
| 安全与隐私 | 删除是否覆盖每一个层级；仅靠提示词的攻击者能否提取或污染记忆？ | `erasure_compliance.py`, `memory_extraction_redteam.py`, `memory_poison.py`, `sensitivity_leakage.py` |
| 代码索引 | 代码索引能否找到定义、调用方和概念性答案，并在其余情况下拒绝作答？ | `code_intel_qa.py`, `code_plane_ablation.py`, `code_shunt_tokens.py`, `code_ask_cache_threshold/` |
| 文档解析 | 提取器能否读取文档的文本、标题、表格和页面，且不丢失任何内容？ | `docparse/`（离线） |
| 跟踪器 | `tracker.similar` 能否找到某个 issue 被关闭时所指向的较早的重复项？ | `tracker_similar_bench.py` |
| 延迟与索引健康 | 搜索时间花在哪里；向量索引返回的结果是否与暴力搜索一致？ | `search_latency.py`, `vector_tenant_recall.py`, `ann_final_staleness.py`, `vector_index_vs_exact.py`, `scripts/ops/benchmark.py` |
| 写入负载 | 持续的写入速率会带来多少延迟和多少 ClickHouse parts？ | `write_rate.py` |
| 规模 | 随着命名空间、组织和记忆存储键的增长，记忆最先触及哪个上限？ | `scale_load.py` |
| 消费方验收 | 某一类部署能否以可接受的成本得到精确的回答？ | `acceptance/{code_agent,memory_app,plugin,harness}.py` |

[`bench/memoryarena/`](https://github.com/ClickHouse/loom/blob/main/bench/memoryarena/README.md)（一个对相互依赖任务的后续会话进行评分的 agent 训练场）和 [`bench/memorydata/`](https://github.com/ClickHouse/loom/blob/main/bench/memorydata/README.md)（一个让 Loom 与其他方法并行运行的测试平台）是接入了 Loom 的外部 harness。它们的 README 就是各自的运行指南。

如果你是在评估 Loom，而不是修改它的排序代码，请从[评估 Loom](https://github.com/ClickHouse/loom/blob/main/docs/getting-started/evaluate.md)开始。它把 harness 清单整理成一条简短的决策路径，涵盖产品接入、你自己的记忆工作负载、代码智能以及已发布结果的复现。

<a id="entry-points"></a>

## 入口

| 入口                         | 运行内容                                                                   | 前提条件                                                          |
| ---------------------------- | -------------------------------------------------------------------------- | ----------------------------------------------------------------- |
| `make bench-quality-gate`    | 在已提交的合成数据集上运行 `quality.py`；召回率低于 0.80 时退出码为 1      | 带有真实 embedder 和抽取 LLM 的服务器                             |
| `make bench-algorithm-gate`  | 在回归测试夹具上运行 `extraction_eval.py` 和 `graph_eval.py`               | 带有真实抽取 LLM 的服务器                                         |
| `make bench-quick`           | 使用固定的 reader 和 judge，在 30 个打乱顺序的条目上运行 `longmemeval.py`  | 已下载的 LongMemEval 文件、`LOOM_API_KEY`、`OPENAI_API_KEY`       |
| `make bench-matrix`          | `matrix.py`：在 LongMemEval 上遍历搜索模式 × 重排 × 候选池宽度             | 同 `bench-quick`                                                  |
| `make bench-code-intel-dry`  | `code_intel_qa.py --dry`：在完美的合成检索结果上运行评分器                 | 无（离线）                                                        |
| `make bench-code-intel`      | 按上线下限运行 `code_intel_qa.py`                                          | 带有已索引 ClickHouse 仓库和真实 embedder 的服务器                |
| `make bench-code-legs`       | 用 `code_intel_qa.py --compare-extra` 对三个检索分支做消融，每组配对比较   | 同 `bench-code-intel`                                             |
| `make bench-clickgap-replay` | `clickgap_code_replay.py`：回放 PR 评审机器人的调用序列                    | 该使用方 PR 的回放 JSONL 文件                                     |
| `make bench-clickgap-golden` | 在该使用方的 golden 夹具上运行 `clickgap_golden_replay.py`                 | `GOLDEN_DIR`，即不在本仓库中的私有夹具                            |
| `make bench`                 | `scripts/ops/benchmark.py`：对单个 op 施加并发负载，输出 p50/p95/p99 和 RPS | 一台服务器                                                        |
| `make test-unit`             | 同时运行 `pytest bench -q`：评分核心和数据集读取器                         | 无（离线）                                                        |

其他所有测试框架都以 `python bench/<name>.py` 的方式运行（[运行](https://github.com/ClickHouse/loom/blob/main/docs/benchmarks/running.md)）。

<a id="how-a-bench-differs-from-a-production-caller"></a>

## bench 与生产调用方的区别

bench 通过 HTTP 调用相同的操作，但有三点不同：

1. **评估读取。** 计入记忆强化的读取会强化下一轮排序所依据的行，因此 bench 会改变自身的结果。除非运行时传入 `--credit`，否则测试工具会在 `memory.search` 上发送 `auto_touch: false`，在 `memory.ask` 上发送 `reinforce: false`。
2. **一次性的 bench 命名空间。** 每个命名空间都以 `traffic_class` 和 `kind` 为 `bench` 注册：使用 bench 速率桶（默认为基础上限的一半）、bench 级别的 ClickHouse 优先级、单独的成本账本列，并在运行结束时被清除（[bench 命名空间](https://github.com/ClickHouse/loom/blob/main/docs/benchmarks/methodology.md#bench-namespaces)）。
3. **固定的测量工具。** reader 模型和 judge 模型按测试工具固定，不随 `LOOM_LLM_MODEL` 变化。可变的是服务器的提取模型、嵌入模型和各项设置。`--answerer loom-ask`（LongMemEval）或 `--reader ask`（LoCoMo）会对 `memory.ask` 本身进行评分。

服务器的默认值就是这些 bench 所选定的值。修改 `LOOM_LLM_MODEL` 或 `LOOM_EMBEDDING_MODEL` 的默认值时，应先运行 `make bench-quick`，而更换嵌入模型则需要[重新建立基线](https://github.com/ClickHouse/loom/blob/main/docs/benchmarks/running.md#re-baselining-after-an-embedder-change)。

<a id="coverage-and-what-it-leaves-out"></a>

## 覆盖范围与未涵盖的内容

- **CI 中没有质量信号。** CI 运行的是 fake provider，因此测试的是评分器，而不是检索（[原因](https://github.com/ClickHouse/loom/blob/main/docs/benchmarks/methodology.md#fake-providers-make-scores-meaningless)）。除非有人在真实部署上运行这些检查门槛，否则排序回归可能在无人察觉的情况下被合并。
- **已提交的测量结果很少。** [结果](https://github.com/ClickHouse/loom/blob/main/docs/benchmarks/results.md)列出了每一项已提交的测量，以及它在 `bench/results/` 下的产物、所用的测量工具和复现命令。针对你自己的数据和 provider 的数字，必须在你自己的部署上生成。
- **大多数测试工具需要仓库中没有的数据。** 第三方语料库需要下载（[数据集](https://github.com/ClickHouse/loom/blob/main/docs/benchmarks/running.md#datasets)）；回放、依赖集、tracker 标准答案以及基于 trace 的延迟测量，读取的都是某个部署的私有数据。
- **代码 fixture 特定于某个仓库。** ClickHouse 测试线需要 ClickHouse/ClickHouse 的索引，Langfuse 测试线需要该仓库的索引。它们的数字对其他代码库几乎没有参考意义。
- **没有与具名系统对比的排行榜。** 基于公开数据集的测试工具会报告 Loom 的数字，以及将其与已发表表格并列比较所需的列。该仓库只拿 Loom 与它自己的各个构建版本和对照组进行比较。
- **没有硬件记录或容量模型。** 没有任何测试工具记录主机信息。`scripts/ops/benchmark.py` 在一个并发度下测量一个 op；`search_latency.py` 按命名空间大小把一次 `memory.search` 拆分为多个阶段。两者都没有对真实的 agent 混合负载或多租户争用建模。
- **技能评测评估的是形态，而不是召回**（[技能评测](https://github.com/ClickHouse/loom/blob/main/docs/benchmarks/skill-eval.md#what-this-does-not-measure)）。

<a id="what-a-pass-does-not-prove"></a>

### 通过并不能证明什么

- **通过检查门槛只是一个下限。** 构建在某个 embedder 下，对一个小数据集达到了记录的阈值。这并不意味着在你的数据上召回就好。
- **召回不等于答案质量。** 一条记忆可能排在第一位，却仍被误读或已经过时。请查看经过评判的维度和失败所在的阶段。
- **答案准确率不等于 agent 的成功率。** 大多数测试工具一次只对一个问题评分。只有 `code_plane_ablation.py` 和 agent gym 会测量 agent 利用记忆做了什么。
- **安全基准测量的是已命名的攻击。** `memory_poison.py` 和 `memory_extraction_redteam.py` 只报告它们自身攻击形态下的结果。`erasure_compliance.py` 会扫描每个 `loom.*` 表中每个 String 类型的列，查找其 canary；外部后端的删除是否彻底，取决于它们自身的删除能力。

<a id="related"></a>

## 相关内容

- [检索与排序](https://github.com/ClickHouse/loom/blob/main/docs/concepts/retrieval-and-ranking.md)：检索基准测试所覆盖的内容。
- [代码索引](https://github.com/ClickHouse/loom/blob/main/docs/features/code-index/overview.md)：代码基准测试所覆盖的内容。
