<!-- i18n: source=docs/benchmarks/methodology.md blob=0e0708fc624ae94fe8f285a72bac00e00924c567 model=claude-sonnet-5-5 -->
> 本页由机器从[英文原文](https://github.com/ClickHouse/loom/blob/main/docs/benchmarks/methodology.md)翻译，内容以英文原文为准。
> Machine-translated from the [English page](https://github.com/ClickHouse/loom/blob/main/docs/benchmarks/methodology.md), which is authoritative.

<a id="benchmark-methodology"></a>

# 基准测试方法

本页介绍各测试工具（harness）计算什么、如何根据噪声判断差异（delta）、什么情况下两组数字可以比较，以及这些数字在哪些方面存在偏差。相关规则都在代码中：`bench/_gate.py`（共用的门限）、`bench/bootstrap.py`、`bench/judge_calibration.py`、`bench/bench_namespaces.py` 和 `bench/failure_taxonomy.py`（[源码入口](#source-entry-points)）。

<a id="recall-and-answer-accuracy-are-separate-axes"></a>

## 召回率与答案准确率是两个独立的指标

**Recall@K** 衡量相关记忆是否出现在前 K 个结果中。**答案准确率**衡量合成的答案是否正确。其中一个指标可能已经达到饱和，而另一个仍然很低。`longmemeval.py` 将 recall@K 和经评判的准确率作为两个独立的指标输出；`coding_agent_qa.py` 将 R@5 / R@10 / MRR 与经评判的问答准确率分开输出。仅使用 embedder、没有 reader 也没有 judge 得出的头条数字是召回率数字。不要把它与准确率数字放在一起比较。

<a id="task-categories"></a>

## 任务类别

每个测试框架都保留其数据来源的类别标签，因此结果可以按类别查看。

| Harness               | 类别                                                                                                                                                            |
| --------------------- | --------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `locomo_qa.py`        | 1 多跳，2 时间，3 开放域，4 单跳，5 对抗。`overall_comparable` 涵盖 1–4（即已发布的列）；`overall_all` 将 5 按弃答计分。 |
| `longmemeval.py`      | 数据集的 `question_type`（`--question-type`）；`--stale-check` 会标出返回了已被取代的值的、错误的知识更新答案。                    |
| `memoryagentbench.py` | 准确检索、测试时学习、长程理解、冲突消解。                                                                          |
| `beam.py`             | 十项记忆能力，由数据集的评分标准评判器打分；事件排序额外计入 Kendall τ_b × F1。                                                             |
| `membench.py`         | 事实层面和反思层面的参与场景与观察场景。                                                                                       |
| `code_intel_qa.py`    | 行的 `type`：`symbol_def`、`callers`、`conceptual`、`hub`、`history`、`test_set`、`abstain_probe`；另有 `lane` 和 `facets`。                                     |
| `coding_agent_qa.py`  | Bug 修复落地、基础设施修复、重构位置、不稳定测试、性能泄漏、schema 迁移、多会话因果、时间、团队约定。                        |
| `op_planning.py`      | 每个任务的 `class`：`instruction_type` × `structure`。                                                                                                          |

<a id="ground-truth"></a>

## 基准真值

| 类型                  | 位置                                                                                                 | 构建方式                                                                                                                     | 弱点                                           |
| --------------------- | ---------------------------------------------------------------------------------------------------- | ---------------------------------------------------------------------------------------------------------------------------- | ---------------------------------------------- |
| 子串黄金键            | `bench/datasets/synthetic_memory_qa.json`, `extraction_regressions.json`, `graph_regressions.json`   | 手工编写；只要返回的 `memory_key` 或摘录中包含任一黄金字符串即算命中（不区分大小写，`_` 视为空格）                           | 有意放宽；冗长的摘录可能误命中                 |
| 已发布的黄金答案      | 第三方数据集（[数据集](https://github.com/ClickHouse/loom/blob/main/docs/benchmarks/running.md#datasets)） | 数据集自带的答案和证据标注                                                                                                   | 沿袭数据集的标注错误和饱和问题                 |
| 手工标注的查询 → 键对 | `retrieval_recall.py`, `plane_ablation.py`, `hard_distractors.py` `--dataset`                        | 由使用者针对现有语料库标注查询                                                                                               | 数据集较小；标注不进入代码仓库                 |
| 按使用情况标注        | `relied_on_set.py` → `bench/datasets/relied_on/`                                                     | 一次搜索返回的键，在短时间窗口内被 agent 随后依赖（`used` / `used_cited` 事件）                                              | 按时间而非 id 关联；偏向当前排序已经呈现的结果 |
| 从已合并的 PR 中挖掘  | `bench/fixtures/code_intel_mined_*.jsonl`, `code_intel_history.jsonl`, `code_plane_ablation.py mine` | 查询 = issue 或 PR 标题；黄金 = 被修改的 `src/` 文件。文本中出现黄金路径文件名主干的行会被丢弃，并报告其占比               | 索引包含每一个修复：绝对值只是上界，应看成对差值 |
| 模型编写、模型验证    | `bench/fixtures/code_intel_oss.jsonl`                                                                | 第三方语料库，其查询和路径由模型编写，并由模型评判器检查                                                                     | 未命中可能是标注的问题；`secondary` 路径从不计分 |

结构化代码黄金（`symbol_def`、`callers`）是精确的，但编写时依据的是对上游仓库的了解。每次全新回填之后，先不设门槛运行一次，查看不匹配项并修正黄金：错误的黄金是语料库的缺陷。

评判器校准会自行构建探针（`build_probes`）：把某一项的黄金改写成读者作答的形式，即为已知正确的探针；取同一类别中另一项的黄金，即为已知错误的探针。对抗性条目不计入。

<a id="judges"></a>

## 评判模型

读取模型（reader）和评判模型（judge）属于测量工具的一部分，不是被测系统。

- **按测试工具固定。** `longmemeval.py` 和 `coding_agent_qa.py` 使用 `gpt-4o-mini` 读取，使用 `gpt-4o-2024-08-06`（LongMemEval 的评判快照）评判。`locomo_qa.py` 使用 `gpt-4.1-mini` 读取，使用 `gpt-4.1` 评判。`BENCH_READER_MODEL` / `BENCH_JUDGE_MODEL` 可以覆盖这些设置，并会输出可比性警告；`OPENAI_BASE_URL` 可指向兼容的网关。
- **评判 Loom 的回答。** `longmemeval.py --answerer loom-ask` 和 `locomo_qa.py --reader ask` 评判 `memory.ask`；`--ask-arms` 在同一次导入上比较不同的 `memory.ask` 配置。
- **评分标准是文件。** `bench/rubrics/entailment.md` 是 `dialogue_consistency.py` 加载的 NLI 提示词。`beam.py` 使用数据集自带的评分标准评判模型。
- **一致性。** `locomo_qa.py --second-judge-model` 会对抽样结果重新评分，并报告一致率和 Cohen's kappa。

词法分数仅用于与已发表的词法分数表并列对照。`longmemeval.py --lexical-scores` 会在**简化后**的回答上增加 token F1、BLEU-1 和 ROUGE-L（`--simplify-model`，默认使用评判模型；否则使用带标注的启发式方法）。不做简化时，这些分数衡量的是回答的冗长程度：错误的日期得分可能高于正确的句子（[参考值](https://github.com/ClickHouse/loom/blob/main/docs/benchmarks/results.md#lexical-scoring-reference-values)）。它们永远不能取代评判模型。

<a id="metrics"></a>

## 指标

| 指标                       | 定义                                                                                                                                                        | 计算位置                                         |
| -------------------------- | ----------------------------------------------------------------------------------------------------------------------------------------------------------- | ------------------------------------------------ |
| 召回率@K                   | 前 K 个结果中包含标准答案命中的问题所占比例                                                                                                                 | `quality.py` `summarise`、`retrieval_recall.py`  |
| MRR                        | 首个标准答案命中的 1 / 排名的平均值（无命中时为 0）                                                                                                         | 同上                                             |
| rank@1                     | 标准答案排名第一的问题所占比例                                                                                                                              | 同上                                             |
| 精确率@K                   | 返回结果中标准答案命中的比例，取平均                                                                                                                        | `quality.py`                                     |
| `best_cos_rank`            | 余弦相似度最高的结果的排名；若与第 1 名有差距，说明融合排序把较弱的匹配排在了前面                                                                           | `retrieval_recall.py`                            |
| exact@1、caller-set F1     | 代码结构层面的精确度                                                                                                                                        | `code_intel_qa.py`                               |
| 弃答精确率 / 召回率        | 召回率 = 弃答的探测题数 / 探测题总数；精确率 = 弃答的探测题数 / 全部弃答数                                                                                  | `code_intel_qa.py`                               |
| `placebo@k`                | 对每个问题，从系统在所有概念类问题中返回的路径池里（按种子）随机抽取同样数量的路径。低于该值的召回率反映的是候选池覆盖率，而不是排序质量                    | `code_intel_qa.py`                               |
| 每个回答的 token 数        | 每个已回答问题的 `tokens_returned` 的平均值 / p90                                                                                                           | `code_intel_qa.py`、验收基准测试                 |
| 评判准确率                 | 多次重复中多数标签为 CORRECT 的比例                                                                                                                         | `_gate.py`                                       |
| 翻转率、不一致率           | 翻转率：同一组内重复评判的条目中标签不一致的比例；不一致率：两组之间多数标签不同的配对所占比例                                                              | `_gate.py` `flip_rate`、`GateResult.discordance` |
| 失败阶段                   | `extraction`、`graph_resolution`、`retrieval_ranking`、`compression_loss`、`reader`、`not_verbatim`                                                         | `failure_taxonomy.py`                            |

失败阶段按每个未命中的条目计算，并按流水线顺序判定：未被召回的为 `retrieval_ranking`；标准答案出现在已检索的摘录中但回答错误的为 `reader`；标准答案只出现在已检索记忆背后的原始证据中的为 `compression_loss`；标准答案在任何位置都没有逐字出现的为 `not_verbatim`。`lme_diagnose.py` 会导出带有这些标签的 LongMemEval 未命中条目。在检索不变的情况下出现 `reader` 回退，责任在 reader。

请求失败不计入比率，也不会被算作未命中（`code_intel_qa.py` 会打印 `errors(excluded)`）。`quality.py` 遇到任何 HTTP 失败都会中止。

<a id="baselines"></a>

## 基线

Loom 在相同的评测方法和相同的摄取数据上，与多种技术以及自身的各个对照组进行比较。

| 基线                                                              | 位置                                                                               |
| ----------------------------------------------------------------- | ---------------------------------------------------------------------------------- |
| 仅词法检索、词法加查询扩展、仅向量检索                            | `make bench-code-legs`：每一种都是与默认混合检索配对的 B 组                        |
| 仅摘要记忆                                                        | `msc_dmr.py` 的 `digest` 组，与 `ask`、`search`、`search_recall`、`search_reader` 并列 |
| 原文存储与提取事实                                                | `memoryarena/` 和 `memorydata/` 的 `verbatim` / `extract` 模式                     |
| 长上下文、BM25、普通稠密检索                                      | `memoryarena/memory_server.py` 中的各组                                            |
| 关闭插件的编码 agent                                              | `code_plane_ablation.py` 的 A 组                                                   |
| 批量读取文件                                                      | `code_shunt_tokens.py` 的 "cold" 组                                                |
| 暴力最近邻搜索                                                    | `vector_tenant_recall.py`                                                          |
| 单一强模型或单一低成本合成模型                                    | `route_replay.py` 在冻结的检索结果上运行的 strong、cheap 和 cascade 组             |
| 源系统的原生排序                                                  | `clickgap_migration_validate.py`：导出数据中的 `recall_count` 顺序                 |
| 按平面消融                                                        | `plane_ablation.py` 的 `execution_planes` 掩码                                     |

<a id="statistics"></a>

## 统计

<a id="paired-clustered-bootstrap"></a>

### 配对聚类 bootstrap

排序变更是否上线，取决于配对差值的**下界**，而不是均值。`paired_bootstrap` 接收逐项差值（B 减 A），对整个聚类进行有放回重采样（10,000 次重采样，`seed=7`），并取第 2.5 和第 97.5 百分位数。只有当下界大于零时，才算有提升。围绕同一机制的条目会一起变化，因此测试框架会传入一个聚类键：一段对话、一种问题类型，对代码而言则是路径的前两段（`cluster_of_path`）。聚类太少会使区间过窄；请结合下界查看 `clusters`。`code_plane_ablation.py` 还会对跨 seed 的各任务均值做配对 Wilcoxon 符号秩检验。

<a id="repeats-and-the-majority-label"></a>

### 重复运行与多数标签

即使 temperature 为 0，reader 的输出也会变化，而且合成过程中的检索会重新分解查询。`--repeats N` 会在同一次检索结果上对每个条目提问并评判 N 次，并保留**多数**标签。平局算 WRONG；只有每次运行都出错时，条目才算 ERROR。门禁运行使用三次重复（`GATE_REPEATS`）。对各次重复的 `top_keys` 计算 `identical_share`，可以看出标签翻转是由 reader 还是由检索造成的。重复运行可以降低标签噪声（[模拟](https://github.com/ClickHouse/loom/blob/main/docs/benchmarks/results.md#majority-voting-on-simulated-reader-noise)）；只有增加条目数才能降低抽样噪声。

<a id="the-verdict"></a>

### 判定结果

`paired_gate` 对各组的多数标签运行 bootstrap：

| 判定         | 条件                                                                                                                                       |
| ------------ | ------------------------------------------------------------------------------------------------------------------------------------------ |
| `pass`       | 95% 下界超过 `--gate-min-delta`（默认 0；负值表示非劣效性门禁）                                                                            |
| `fail`       | 95% 上界低于阈值                                                                                                                           |
| `unresolved` | 区间跨越阈值。若处于零假设带（`--gate-band`，默认 `DEFAULT_BAND_PP` = 4.0 pp）之内，这就是噪声下限；若在其之外，则说明运行的统计功效不足 |

在任一组的重复运行中不稳定的条目，也会作为仅含稳定条目的配对单独报告。

**测量你自己的带宽。**默认值来自一个测试框架和一个 reader。请将同一配置分别作为 A 组和 B 组，以 `--repeats 3 --gate` 运行，并在真实对比中把结果中较宽的一侧（`band_from_null`）作为 `--gate-band` 传入。

<a id="how-wide-the-interval-is"></a>

### 区间有多宽

宽度取决于两组结果不一致的配对条目数量，而不仅仅取决于 n。下表各行是对固定差值运行 `paired_bootstrap` 的结果（+1 = B 对而 A 错，−1 相反，0 = 一致）：

| n                    | 胜 / 平 / 负                     | 平均差值   | 95% 区间          | 解读                                           |
| -------------------- | -------------------------------- | ---------- | ----------------- | ---------------------------------------------- |
| 60                   | 1 / 59 / 0                       | +1.7 pp    | +0.0 .. +5.0 pp   | 仅一道题；下界未超过零                         |
| 30                   | 2 / 26 / 2                       | 0          | −13.3 .. +13.3 pp | 同配置配对：13 个点的波动在区间之内            |
| 60                   | 5 / 50 / 5                       | 0          | −10.0 .. +10.0 pp |                                                |
| 164                  | 13 / 138 / 13                    | 0          | −6.1 .. +6.1 pp   |                                                |
| 500                  | 40 / 420 / 40                    | 0          | −3.6 .. +3.6 pp   |                                                |
| 60                   | 6 / 52 / 2                       | +6.7 pp    | −1.7 .. +16.7 pp  | 一个“7 个点的提升”，但结论未定                 |
| 60                   | 8 / 50 / 2                       | +10.0 pp   | +0.0 .. +20.0 pp  | 下界刚好到零：不算通过                         |
| 500                  | 40 / 440 / 20                    | +4.0 pp    | +1.0 .. +7.0 pp   | 提升较小，但因 n 更大而通过                    |
| 60，6 个聚类，每个 10 | 8 / 50 / 2，胜出集中在两个聚类   | +10.0 pp   | +0.0 .. +30.0 pp  | 相关的条目会使区间变宽                         |

```bash
.venv/bin/python - <<'EOF'
from bench.bootstrap import paired_bootstrap
def row(w, t, l, clusters=None):
    d = [1.0] * w + [-1.0] * l + [0.0] * t
    print(paired_bootstrap(d, clusters).line(unit="pp", scale=100))
row(1, 59, 0); row(2, 26, 2); row(5, 50, 5); row(13, 138, 13); row(40, 420, 40)
row(6, 52, 2); row(8, 50, 2); row(40, 440, 20)
row(8, 50, 2, [f"c{i // 10}" for i in range(60)])
EOF
```

<a id="set-sizes"></a>

### 数据集规模

小数据集无法区分相近的各组。

| 数据集                                 | 规模                         | 单个条目相当于                    |
| -------------------------------------- | ---------------------------- | --------------------------------- |
| `synthetic_memory_qa.json`             | 15 个问题                    | 6.7 个点：只能发现性能崩溃        |
| `code_intel_qa.jsonl` 概念类子集       | 60 个问题中的 36 个          | 2.8 个点                          |
| 算法门禁 fixture                       | 19 个抽取用例，2 个图用例    | 冒烟测试                          |
| `code.ask` 缓存配对                    | 42 个释义，43 个不同         | 超过 2 个点                       |

<a id="judge-calibration"></a>

### 评判器校准

`bench/judge_calibration.py` 会在探针上测量评判器的真阳性率和假阳性率。观测到的准确率为 `TPR·p + FPR·(1−p)`，因此配对差值会按 Youden 指数 `J = TPR − FPR` 缩小。使用 `--judge-calibration <json>` 时，门禁会报告 `Δ/J`，并按校准集自身的方差（delta 方法）拓宽区间。`J ≤ 0` 的评判器不携带任何信息；输入会原样通过。

<a id="when-two-numbers-compare"></a>

## 两个数字何时可比

以下各项必须全部一致。只要有一项不同，就把这次比较标为不可比。

| 必须一致 | 原因 |
| ------------------------- | ------------------------------------------------------------------------------------------- |
| 评测轴 | Recall@K 与经裁判评分的准确率是不同的指标 |
| 阅读器与裁判 | 更强的阅读器会改变偏重推理的类别；不同的裁判会改变标注结果 |
| 数据集划分与切片 | LongMemEval oracle 与 S，LoCoMo 类别 1–4 与 1–5，单个 lane 与整个文件 |
| 搜索模式 | 未固定时的默认值是 `LOOM_MEMORY_SEARCH_DEFAULT_MODE`；其他模式属于消融实验 |
| Embedder 与服务端构建 | 更换 embedder 会改变整个相似度尺度；`quality.py` 的 `.meta.json` 会同时记录二者 |
| 索引修订（代码） | `code_intel_qa.py` 会给出提供答案的 `as_of.commit` 以及过期数量 |
| 计分方式 | 带 `--credit` 的运行会强化它读取到的内容 |
| 准确率分母 | 把弃答排除在外，会使弃答更多的一组看起来更好 |

**弃答。** 经裁判评分的门槛把弃答算作错误。`route_replay.py` 同时报告两个值：`accuracy` = correct / (correct + wrong)，`accuracy_abstain_wrong` = correct / (correct + wrong + abstained)，后者即门槛所用的分母。出错的请求不计入这两个值。引用结论所依据的那一个，并写明是哪一个。

**上界与安慰剂。** 挖掘得到的代码 lane 运行在包含所有修复的索引上（每个消融任务都有 `fix_in_index`）：应引用成对的差值，而不是绝对值。召回率若不能超过 `placebo@5` / `placebo@10`，说明它只是候选池的覆盖范围。很小的数据集会让安慰剂基线异常强。如果大多数组的得分都接近上限，就无法区分它们；请查看各类别的结果以及仅含稳定项的成对比较。

<a id="fake-providers-make-scores-meaningless"></a>

## 伪造 provider 会让分数失去意义

checkout 中的 `.env` 运行 `LOOM_EMBEDDING_MODEL=fake-ngram-3072`，CI 运行 `fake-ngram-1024`，两者都使用 `LOOM_LLM_PROVIDER=fake`（已发布的技术栈使用 `local` 进行 embedding，并保留 fake LLM）。fake embedder（`server/embedding/fake.py`）把字符三元组哈希到若干桶中，因此相似度反映的是拼写，而不是语义。fake LLM（`server/llm/fake.py`）根据锚点短语返回固定结构的 JSON，所以流水线可以运行，但不会提取任何真实内容。在它们之下：

- 召回率衡量的是问题与已存储文本之间的字符串重合度；
- 提取基准衡量的是 fake 的输出，而不是 prompt；
- 相似度门限使用其标量默认值，因为自适应校准会跳过 fake embedder。

基于 fake embedding 的绿色门限比没有门限更糟：它报告了一种从未被测量过的安全性。因此 `make bench-quality-gate` 和 `make bench-code-intel` 不在 CI 中运行（要强制执行它们需要付费 provider 的密钥），CI 只运行纯评分代码（`pytest bench`）和门限函数的单元测试（`quality.gate`：边界值通过，0/0 失败）。在基准测试服务器上设置 `LOOM_REQUIRE_REAL_PROVIDERS=true`，这样当使用 fake 或没有密钥的 provider 时，`/v1/health` 会报告 `degraded`，让你在花费一次运行之前就能发现问题。

<a id="reproducibility-controls"></a>

## 可复现性控制

| 控制项                     | 机制                                                                                                                                                                               |
| -------------------------- | ---------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| 确定性重采样               | `paired_bootstrap` 和 `paired_gate` 在给定 `seed` 时是确定性的；gate 会记录 `resamples` 和 `seed`                                                                                  |
| 确定性样本                 | 样本参数接受 seed（`musique.py --seed`、`judge_calibration.py`、mined-lane 样本）                                                                                                  |
| 记录服务端检测信息         | `quality.py` 会写入 `.meta.json`，其中的 `embedding_model`、`embedding_dim`、`llm_model`、`llm_provider`、`build_commit` 来自 `/v1/health`；`code_intel_qa.py` 会给出 `as_of.commit` |
| 固定范围                   | 搜索 harness 发送 `namespace_scope: "this"`，因此其他可读命名空间的行不会进入窗口。临时探测也必须这样做                                                                             |
| 评估读取                   | `measurement_fields()` 发送 `auto_touch: false` / `reinforce: false`；`--credit` 可选择开启；对于之前读取过的命名空间，请传入 `reinforcement_weight: 0` 或使用新的行                |
| 全新命名空间               | 每个条目或每段对话使用一个临时命名空间，结束时擦除                                                                                                                                 |
| 预热                       | `search_latency.py --warm 1` 会丢弃每个查询的第一次调用，因此 embedding 缓存未命中不会计入检索                                                                                     |
| 令牌有效期                 | 签发一个有效期长于运行时间的令牌（[运行](https://github.com/ClickHouse/loom/blob/main/docs/benchmarks/running.md#2-mint-a-token-that-outlives-the-run)）                           |

<a id="bench-namespaces"></a>

### Bench 命名空间

写入数据的 harness 会创建临时命名空间（`lme-…`、`locomo-…`、`mab-…`、`qabench-…`；新的 harness 使用 `bench-`）。`bench/bench_namespaces.py`：

- 在每个命名空间首次写入之前注册它，设置 `traffic_class = "bench"`、`kind = "bench"` 和 `capture_generation_io = True`（默认）；
- 在 `erase_on_exit` 中运行任务，当运行结束、失败或被中断时，它会通过一次清除式擦除来擦除所有已创建的名称。请求体设置 `external: false`，因此不会触及任何外部后端；
- 使用 `--keep-namespaces` 时，会保留这些命名空间以便之后重新评分（`--namespaces-json`），并将它们注册为 `kind = "memory"`，使空闲回收器不会处理它们。

擦除需要组织管理员令牌。被拒绝时，harness 会记录 `loom namespaces-gc` 命令。`namespace_gc` worker 会在 `LOOM_NAMESPACE_GC_IDLE_HOURS` 之后清除空闲的 `kind = bench` 命名空间，且创建的名称以 `LOOM_NAMESPACE_BENCH_PREFIXES` 中的某个前缀开头，因此中止的运行也能被找到。

<a id="http-policy"></a>

### HTTP 策略

Harness 通过 `bench/_http.py` 发送请求（`post` / `apost` 返回响应，`post_json` / `apost_json` 返回响应体）：

| 响应                                  | 策略                                                                                           |
| ------------------------------------- | ---------------------------------------------------------------------------------------------- |
| 429                                   | 等待 `Retry-After` 指定的时间（缺省为 1 s，限制在 0.2–60 s 之间）后重试；等待不占用 5xx 重试预算 |
| 5xx                                   | 以 `0.5 × 2^n` s 的退避重试，直到已发送 `attempts` 次请求                                      |
| 任何其他 ≥ 400 的状态，或最后一次 429 / 5xx | 抛出 `httpx.HTTPStatusError`，附带响应体的开头部分                                             |
| 传输错误                              | 向上传播；需要应对重启的 harness 会保留自己的循环                                              |

<a id="sources-of-bias"></a>

## 偏差来源

| 来源                     | 影响                                                                 | 代码中的缓解措施                                                     |
| ------------------------ | -------------------------------------------------------------------- | -------------------------------------------------------------------- |
| 作者编写的数据集         | 调优系统的人编写了合成数据集，并在其上选定了阈值                     | 仅将其作为冒烟检查；排名结论基于公开数据集或按使用情况标注的数据集   |
| 宽松的子串标准答案       | 冗长的摘录可能碰巧包含某个标准答案词                                 | 在召回率之外同时报告 MRR、rank@1 和 precision@K                      |
| 上界型代码通道           | 索引中已经包含了所有修复                                             | 页眉警告、`fix_in_index`；只引用成对差值                             |
| 模型编写的标准答案       | 第三方代码搜索标签由模型编写并检查                                   | `secondary` 路径从不计分；只做成对解读                               |
| 按使用情况标注           | 被依赖的数据集只包含当前排名所呈现的内容                             | 在 bootstrap 界限上做成对扫描；仅作为多个来源之一                    |
| 评审宽松                 | LLM 评审会以一定比例放过切题但错误的答案                             | `judge_calibration.py` 的 TPR/FPR、`--second-judge-model`            |
| 评审与读取模型漂移       | 固定名称背后的提供方版本更新会改变标签                               | 数据集指定快照时使用固定快照；两组同时运行                           |
| 自我强化                 | 计入读取会强化下一轮的排名                                           | 默认使用评估读取                                                     |
| 被排除的错误             | 如果失败集中在困难条目上，比值会上升                                 | 打印错误计数；`quality.py` 在任何失败时中止                          |
| agent 基准中的泄漏       | agent 通过网络或 git 历史找到修复                                    | 消融实验的排除台账会丢弃此类单元，失败即关闭                         |
| 饱和                     | 大多数系统都能轻松完成的类别无法区分各组                             | 按类别给出结果；仅稳定项的配对                                       |
| 测试框架一致性           | 外部测试平台用自己的模板包装每个方法的每条查询                       | 适配器保持这种一致性                                                 |

<a id="source-entry-points"></a>

## 源码入口

- `bench/_gate.py`：`paired_gate`、`verdict_for`、`majority_label`、`flip_rate`、`JudgeCalibration`、`measurement_fields`、`add_gate_arguments`。
- `bench/bootstrap.py`：`paired_bootstrap`、`cluster_of_path`。
- `bench/judge_calibration.py`：`build_probes`。
- `bench/bench_namespaces.py`：`register_body`、`erase_on_exit`、`erase_on_exit_sync`。
- `bench/failure_taxonomy.py`：`classify_answer_failure`。
- `bench/answer_scoring.py`：`token_f1`、`bleu1`、`rouge_l`、`cohen_kappa`。
- 测试：`tests/unit/test_bench_gate.py`、`test_bench_bootstrap.py`、`test_bench_judge_calibration.py`、`test_bench_namespaces.py`、`test_bench_failure_taxonomy.py`、`test_answer_scoring.py`、`test_bench_gate_help_renders.py`。

<a id="related"></a>

## 相关内容

- [运行](https://github.com/ClickHouse/loom/blob/main/docs/benchmarks/running.md)和[结果](https://github.com/ClickHouse/loom/blob/main/docs/benchmarks/results.md)。
- [检索与排序](https://github.com/ClickHouse/loom/blob/main/docs/concepts/retrieval-and-ranking.md)：哪些因素会影响排序。
