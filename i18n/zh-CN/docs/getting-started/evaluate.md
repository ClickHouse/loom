<!-- i18n: source=docs/getting-started/evaluate.md blob=882e7d370aa195573ce2e4029995fa1f32beadc9 model=claude-sonnet-5-5 -->
> 本页由机器从[英文原文](https://github.com/ClickHouse/loom/blob/main/docs/getting-started/evaluate.md)翻译，内容以英文原文为准。
> Machine-translated from the [English page](https://github.com/ClickHouse/loom/blob/main/docs/getting-started/evaluate.md), which is authoritative.

<a id="evaluate-loom"></a>

# 评估 Loom

评估时应围绕你需要做出的决策，而不是仓库中的每一个测试框架。本指南先从无需成本的产品冒烟测试开始，再进行针对具体工作负载的质量测试。只有在需要审计或复现已发布的结论时，才进入最后一步，即公开基准测试套件。

<a id="choose-an-evaluation"></a>

## 选择评估方式

| 决策                           | 数据                               | 提供方                                 | 典型输出                                       |
| ------------------------------ | ---------------------------------- | -------------------------------------- | ---------------------------------------------- |
| Loom 能否正确安装并连接？      | 四条测试事实和一份文档             | 自带的 fake provider 即可              | 事实在新会话中依然存在；控制台中出现来源、历史和审计记录 |
| 记忆对我们的 agent 是否有用？  | 25–50 个带有预期记忆键的真实问题   | 真实 embedder；用于提取或 `memory.ask` 的真实 LLM | recall@k、rank@1、弃答、回答准确率、延迟和成本 |
| 代码智能对我们的代码仓库是否有用？ | 有代表性的符号、调用方集合和类似 issue 的问题 | 真实 embedder；已建立索引的代码仓库    | 精确定义、调用方 F1、文件召回率、弃答、token 和延迟 |
| 我们能否复现已发布的结果？     | 指定的公开数据集和已提交的结果产物 | 固定版本的模型或文档中说明的替代模型   | 使用相同切片、reader、judge 和搜索模式的可比运行 |

不要使用 fake embedder（checkout 中的 `.env`）或 fake LLM 来判断质量：它们的作用是让整套服务在没有凭据的情况下也能启动，并使集成测试具有确定性。已发布的整套服务中的本地 embedder 会真实地进行检索，因此可以用它对搜索做初步评估；回答和提取的质量则应使用真实的 LLM 来判断。

<a id="before-you-run"></a>

## 运行之前

先写下这五件事：

1. **工作负载。** 该决策所涵盖的用户、代码仓库和问题类型。
2. **基线。** 没有 Loom 时，同一用户或 agent 的做法：完整上下文、grep、BM25、其他记忆系统，或不做检索。
3. **精度门槛。** 你能接受的最低召回率、回答准确率或任务成功率。质量优先于成本。
4. **经济性门槛。** 最大 p95 延迟、token 数、模型成本、索引时间和存储空间。
5. **环境记录。** Loom commit、ClickHouse 部署、提供方模型、模型参数、硬件、数据集版本和随机种子。

预先登记这些门槛，可以避免在运行之后，让某个方便的指标或数据切片变成最终结果。失败和弃答都要保持可见；请求错误应单独报告，不要悄悄地把它们算作检索未命中，也不要直接丢弃。

<a id="1-product-smoke-test"></a>

## 1. 产品冒烟测试

这个测试大约需要 15 分钟，用于验证连通性、持久化和可见性，不用于验证语义质量。

1. [启动本地服务栈](quickstart.md#1-boot-the-stack)，并确认 `/v1/health` 返回 `status: ok`。
2. [写入并搜索一条事实](quickstart.md#4-first-memory-over-http)。
3. [连接一个编码 agent](https://github.com/ClickHouse/loom/blob/main/docs/getting-started/connect-a-coding-agent.md)，告诉它一条需要长期保留的偏好，然后打开一个新会话并询问这条偏好。
4. 修正这条事实，然后确认控制台显示了它的来源和版本历史。
5. 使用一份文档运行 `loom demo`，并确认返回的分块引用了该文档（[文档冒烟测试](quickstart.md#optional-ask-a-question-about-a-document)）。

只有同时满足以下条件才算通过：新会话能够回忆起该事实，修正后的内容取代了当前答案，无关问题会弃答，并且控制台显示了写入者和历史记录。只要健康检查报告 `fake_embedding` 或 `fake_llm`，测试结果就不能说明生产环境中的召回效果。

<a id="2-evaluate-your-workload"></a>

## 2. 评估你的工作负载

配置[真实的提供方](https://github.com/ClickHouse/loom/blob/main/docs/operations/deployment.md#providers)，如果向量维度发生变化，请重置数据或重新生成 embedding，并从 `/v1/health` 记录解析出的模型。使用独立的命名空间，并让每个配对组使用相同的已索引数据。

<a id="memory-and-rag"></a>

### 记忆与 RAG

根据真实任务构建至少 25–50 个问题的集合，其中应包括：

- 直接事实、同义改写、多跳问题和有时效性的更新；
- 答案本应已被取代的问题；
- 看似合理但无法回答的问题，用于衡量错误回答和拒答情况；
- 近似重复的干扰项，它们只在关键取值上有所不同。

对每个可回答的记忆问题，在运行之前标注期望的 `memory_key`。对于文档 RAG，标注期望的文档和 chunk，查询 `knowledge.search`，并用相同的检索指标对这些标识符打分。分别衡量检索和合成：

- **检索：**recall@k、rank@1、MRR、误报率，以及拒答的精确率/召回率。
- **回答：**在固定的同一个 reader 和 judge 下的正确性，以及引用的正确性。
- **成本：**p50/p95 延迟、输入/输出 token、模型成本、构建成本和已存储的行数。

对于已有的记忆语料，请按以下结构保存一个 JSON 数据集（所附的测试工具针对 `memory.search`；对 `knowledge.search` 请使用等效的打分器）：

```json
{
  "org": "acme",
  "namespace": "support-eval",
  "questions": [{ "query": "When can we deploy?", "gold_keys": ["deploy_window"] }]
}
```

然后在不改动语料的情况下运行检索：

```bash
export PYTHONPATH=$PWD LOOM_API_KEY=<token>
.venv/bin/python bench/retrieval_recall.py \
  --dataset /path/to/retrieval_qa.json --base-url http://localhost:7777 \
  --top-k 10 --out /tmp/loom-retrieval.csv
```

只能根据你预先登记的门槛来设置 `--min-recall` 和 `--min-mrr`。对相同的问题使用 `memory.ask` 或你的生产 reader 来衡量回答准确率；召回率与回答准确率是不同的度量。

<a id="code-intelligence"></a>

### 代码智能

为一个与你的生产仓库相似的仓库建立索引。不要只从 Loom 调优时使用的那个仓库开始。标注：

- 定义，包含期望的文件和限定符号；
- 调用方，包含完整的期望调用方集合；
- 概念性问题和 issue 形式的问题，以及前 5 和前 10 中可接受的文件；
- 应当拒答的仓库外问题；
- 影响分析查询所期望得到的测试或依赖方。

针对已建立索引的仓库运行代码 agent 验收报告：

```bash
export PYTHONPATH=$PWD
.venv/bin/python bench/acceptance/code_agent.py \
  --base-url http://localhost:7777 --token-file /path/to/token \
  --namespace <code-namespace>
```

内置的测试夹具针对 ClickHouse，适合用来检查部署。若要做产品决策，请替换或扩充为你自己标注的问题，并将同样的 agent 任务与仅使用普通仓库工具的情况进行比较。首先报告任务成功率，然后是轮次、token、成本和耗时。工具 schema 的开销取决于客户端，因此请记录它是否延迟加载 MCP 工具定义。

<a id="keep-the-comparison-fair"></a>

### 保持比较公平

- 在配对组中使用相同的模型、提示词、数据快照和问题顺序。
- 将抽取/索引构建成本与单个问题的成本分开，然后说明需要多少个问题才能摊销构建成本。
- 关闭评估读取带来的学习（`auto_touch: false`、`reinforce: false`），避免早先的查询改善后面的查询。
- 报告样本量、各类别的结果和不确定性。不要把小规模的冒烟测试切片提升为一般性结论。
- 保存每个问题的输出，包括错误和未命中，而不仅仅是汇总分数。

<a id="3-audit-or-reproduce-published-results"></a>

## 3. 审计或复现已发布的结果

README 只是一份计分卡，不是证据账本。对于每一个已发布的数字：

1. 打开[基准测试结果](https://github.com/ClickHouse/loom/blob/main/docs/benchmarks/results.md)中对应的章节。
2. 检查其在 [`bench/results/`](https://github.com/ClickHouse/loom/blob/main/bench/results) 下已提交的产物。
3. 确认数据集切片、Loom 提交、模型、reader、judge、搜索模式和种子。
4. 运行结果章节中给出的完整复现命令。
5. 参照[方法论](../benchmarks/methodology.md)解读差异，包括 judge 校准、bootstrap 区间和已知偏差。

并非所有第三方数据集都由本仓库再分发。其来源和许可证列在[数据集](https://github.com/ClickHouse/loom/blob/main/docs/benchmarks/running.md#datasets)下。完整的公开运行可能需要数小时，并产生服务提供方的费用；请先从文档中的冒烟切片开始，再在发布对比结果之前运行完整的协议。

<a id="what-to-publish"></a>

## 发布哪些内容

只有当结果具有代表性、一行即可看懂，并且有已提交的产物和复现命令作为支撑时，才将其发布到 README 中。完整的类别表、模型、提示词、不确定性、成本、失败分析和局限性，请放入[结果台账](https://github.com/ClickHouse/loom/blob/main/docs/benchmarks/results.md)。稳定的评估规则放入[方法论](../benchmarks/methodology.md)，命令和数据集获取方式放入[运行指南](https://github.com/ClickHouse/loom/blob/main/docs/benchmarks/running.md)。

每一项公开声明都应说明：

- 任务和样本量；
- 指标，以及它衡量的是检索、回答质量还是端到端成功率；
- 基线和配对差值（如果存在）；
- 模型和评判器，或者说明未使用 LLM 评判器；
- 不确定性，或者单个问题变化所对应的粒度；
- 分别列出构建成本和查询成本；
- 已提交的产物和确切的复现命令；
- 重要的负面发现，以及该结果不适用于哪些情形。

不要把所有可用的数字都发布在 README 中。过长的成绩表会掩盖产品层面的决策，容易引发无效的跨基准比较，也让过时的声明难以被发现。README 应当引导读者找到证据，而证据本身应放在文档中。

<a id="next"></a>

## 下一步

- [基准测试覆盖范围](../benchmarks/coverage.md)：哪些内容已有公开测量结果，哪些只有测试工具，哪些尚无公开证据。
- [基准测试概览](../benchmarks/overview.md)：根据所测量的信号选择测试工具。
- [运行基准测试](https://github.com/ClickHouse/loom/blob/main/docs/benchmarks/running.md)：提供方、token、命令和数据集。
- [基准测试方法](../benchmarks/methodology.md)：比较规则与局限性。
- [基准测试结果](https://github.com/ClickHouse/loom/blob/main/docs/benchmarks/results.md)：已提交的测量结果和产物。
