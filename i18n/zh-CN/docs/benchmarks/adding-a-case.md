<!-- i18n: source=docs/benchmarks/adding-a-case.md blob=63bcbeab205c463f07cd6aa007c0f8d4eeabefab model=claude-sonnet-5-5 -->
> 本页由机器从[英文原文](https://github.com/ClickHouse/loom/blob/main/docs/benchmarks/adding-a-case.md)翻译，内容以英文原文为准。
> Machine-translated from the [English page](https://github.com/ClickHouse/loom/blob/main/docs/benchmarks/adding-a-case.md), which is authoritative.

<a id="adding-a-benchmark-case-dataset-or-suite"></a>

# 添加基准测试用例、数据集或套件

已提交的数据集规模较小（[数据集规模](https://github.com/ClickHouse/loom/blob/main/docs/benchmarks/methodology.md#set-sizes)），因此新增用例是改进套件成本最低的方式。请先阅读文件头部或 `_comment`：大多数文件都说明了其标准答案（gold）是如何构建的，以及新增的行必须满足哪些条件。

<a id="add-a-case-to-an-existing-set"></a>

## 向现有集合添加用例

<a id="memory-qa-benchdatasetssynthetic_memory_qajson"></a>

### 记忆 QA（`bench/datasets/synthetic_memory_qa.json`）

向某段对话追加一个问题，或追加一段对话：

```json
{
  "conversation_id": "06-billing",
  "conversation": [{ "role": "user", "content": "Our invoices go out on the 3rd of each month." }],
  "questions": [{ "question": "When do invoices go out?", "gold_keys": ["invoice", "3rd"] }]
}
```

只要返回的 `memory_key` 或摘录中包含某个 gold 字符串（忽略大小写，下划线视为空格），即算命中。请选择正确的记忆必然包含、而错误的记忆不太可能包含的 gold 词。保持内容为合成数据：不使用第三方文本，不涉及真实人物。

**它会改变基线。** 0.80 的门限阈值是基于此文件记录的。请在真实部署上于改动前后各运行一次门限检查，如果数值发生变化，则更新 `Makefile` 中的 `MIN_RECALL`。测试：`tests/unit/test_bench_quality_gate.py`、`bench/test_quality.py`。

<a id="extraction-and-graph-regressions"></a>

### 提取与图回归

- `bench/fixtures/extraction_regressions.json` 的每一行形如 `{id, messages, expected}`：expected 是必须出现在某条记忆的 key 或 value 中的短语。
- `graph_regressions.json` 的每一行形如 `{id, messages, expected_triples: [{subject, relation, object}], as_of}`。
- `_note` 字段用于说明失败类别，测试框架会忽略它。

`tests/unit/test_algorithm_gate_assets.py` 会检查每一行都有 messages 以及非空的期望值。请针对真实的提取 LLM 运行 `make bench-algorithm-gate`，以确认该用例通过。

<a id="code-plane-qa-benchfixturescode_intel_qajsonl"></a>

### 代码平面 QA（`bench/fixtures/code_intel_qa*.jsonl`）

每行一个 JSON 对象；以 `//` 开头的行为注释：

```json
{"id": "co037", "type": "conceptual", "query": "where are part merges scheduled", "gold": {"paths": ["src/Storages/MergeTree/MergeTreeBackgroundExecutor"]}}
{"id": "sd011", "type": "symbol_def", "query": "IColumn::insert", "gold": {"qualified_name": "DB::IColumn::insert", "path_prefix": "src/Columns/"}}
{"id": "ab011", "type": "abstain_probe", "query": "kubernetes ingress annotation for sticky sessions", "gold": {}}
```

各类型的 gold 格式见 `bench/code_intel_qa.py` 的 docstring（`callers` 使用 `expected_callers`，`hub` 使用 `path_prefix` 和 `symbols`）。路径是不含扩展名的前缀。可选的 `lane`、`facets` 和 `namespace` 用于对该行进行路由和切片。

- **在 gold 作为门限之前，先对照在线索引验证它**：先不设下限运行一次，查看不匹配项，再修正 gold。
- **abstain probe 必须针对仓库中不存在的代码。**
- **保持主文件的类型集合不变。** `bench/test_code_intel_qa.py`（`test_fixture_loads_and_validates`）断言 `code_intel_qa.jsonl` 至少有 30 行，且类型恰好为 `symbol_def`、`callers`、`conceptual`、`abstain_probe`。hub、history 和 test-set 的行放入各自的 lane 文件。
- `make bench-code-intel-dry` 必须仍然通过：在完美的合成检索下，每个 lane 都能达到其上限分数。
- 挖掘得到的 lane 应重新生成，而不是手工编辑：`code_intel_mine_prs.py` 和 `code_intel_ka_slice.py` 会写入文件及其头部计数。

<a id="coding-agent-lessons-benchfixturescoding_agent_qajson"></a>

### 编码 agent 经验教训（`bench/fixtures/coding_agent_qa.json`）

向 `lessons`（以 `memory_key` 为键的记忆行）和 `questions`（`question`、取自分类体系的 `question_type`、`gold_keys`，以及可选的 `fix_reuse`）中追加内容。每个 gold key 都必须对应一个真实存在的 lesson key，否则 `bench/test_coding_agent_qa.py`（`test_load_corpus_rejects_dangling_gold_key`）会失败。`--dry` 必须仍然通过。

<a id="op-planning-tasks-benchfixturesop_planning_tasksjsonl"></a>

### Op 规划任务（`bench/fixtures/op_planning_tasks.jsonl`）

每个任务包含 `id`、`class`（`instruction_type`、`structure`）、`prompt`、一个 `gold` 计划（工具、必需参数、字面参数）以及存储 `assertions`。当某个 gold 工具或参数在 MCP 接口上不再存在时，`tests/unit/test_op_planning_bench.py`（`test_every_gold_tool_and_required_arg_exists_on_the_surface`）会失败。其 op 已被废弃的任务应移入 `op_planning_tasks_retired.jsonl`，而不是直接删除。

<a id="skill-eval-tasks"></a>

### Skill-eval 任务

`bench/skill_eval/tasks/` 下的任务说明带有 front matter（`task`、`namespace`、`agent`、`traps`），正文中指明一个 `skill-eval-…` 命名空间，并在结尾说明该命名空间将被审计。相同的正文也是 `integrations/claude-code-plugin/evals/loom-memory/<task>/prompt.md` 处的一个 `claude plugin eval` 用例，除 front matter 外逐字节相同。`tests/unit/test_skill_eval.py` 会检查这三条规则并固定任务列表，因此新增任务时也要更新该测试。参见 [技能评测](https://github.com/ClickHouse/loom/blob/main/docs/benchmarks/skill-eval.md)。

<a id="wire-in-a-new-dataset"></a>

## 接入新数据集

1. **不要提交数据集。** 将其放在 `bench/datasets/` 下，或提供路径参数。如果数据集需要下载，请添加 `--fetch` 路径，或在文件缺失时打印下载命令，如 `longmemeval.py` 所做的那样。
2. **在 harness 的 docstring 中记录来源：** 出处、文件名、条目数量以及数据集声明的许可证。在 [数据集](https://github.com/ClickHouse/loom/blob/main/docs/benchmarks/running.md#datasets) 中添加一行。
3. **保留数据集自身的类别**作为一列，如果已发布的表格使用了可比较的子集，请标明该子集。
4. **公开基准测试的名称可以出现在 `bench/` 中**，但绝不能在 `server/` 下的文字说明里作为做出某项选择的理由（见 `tests/unit/test_public_copy.py` `BENCHMARK_NAMES`）。
5. 使用 `LOOM_NAMESPACE_BENCH_PREFIXES` 中的前缀创建临时命名空间（`server/settings/catalog.py`；新的 harness 使用 `bench-`），这样 `loom namespaces-gc` 和命名空间 GC 就能找到中止的运行所遗留的内容。

<a id="write-a-new-harness"></a>

## 编写新的 harness

每个 harness 都遵循以下结构：

| 规则 | 做法 |
| --------------------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| 独立脚本 | 使用 argparse，提供 `--base-url`、`--token`（默认取 `LOOM_API_KEY`）、`--out`；既可以用 `PYTHONPATH=$PWD python bench/<name>.py` 运行，也可以作为模块运行 |
| 共享的 HTTP 策略 | 通过 `bench/_http.py`（`post_json` / `apost_json`）发送请求：Bearer 令牌、遇到 429 时等待、有限次数的 5xx 重试、错误信息中带有响应体 |
| 文档字符串就是手册 | 说明它测量什么、不测量什么、数据集、成本，以及一条运行命令 |
| 纯粹的评分核心 | 评分函数放在实际运行路径之前，无需服务器即可导入，并带有单元测试（`bench/test_<name>.py` 或 `tests/unit/test_bench_<name>.py`） |
| `--dry` 或自检模式 | 在合成输入上运行评分器，不需要服务器，也没有花费 |
| 评估读取 | 先把 `bench/_gate.py` 中的 `measurement_fields("memory.search")` / `("memory.ask")` 合并到请求体中；提供 `--credit` |
| 一次性、已登记的命名空间 | 在第一次写入之前调用 `register_bench_namespace` + `remember_bench_namespace`，把工作放在 `erase_on_exit` 内，并提供 `--keep-namespaces` 标志（`bench/bench_namespaces.py`） |
| 固定范围 | 读取时发送 `namespace_scope: "this"` |
| 需要评判？使用共享的 gate | 用 `add_gate_arguments(parser)` 添加 `--repeats`、`--gate*`、`--credit`、`--judge-calibration`；用 `gate_from_args` 得出结论；固定 reader 和 judge 模型，并在被覆盖时给出警告。`tests/unit/test_bench_gate_help_renders.py` 说明了为什么帮助文本中单独的 `%` 会破坏 `--help` |
| 记录测量所用的工具 | 在数字旁记录 embedder、LLM、provider，以及来自 `/v1/health` 的 `build_commit`（参见 `quality.py` 的 `run_metadata`） |
| 将失败与未命中分开报告 | 统计并打印请求错误；不要把它们按未命中计分 |
| 配对比较 | 通过带聚类键的 `paired_bootstrap` 计算逐项差值；以下界作为发布依据 |

然后：

- 在 [`bench/README.md`](https://github.com/ClickHouse/loom/blob/main/bench/README.md) 的相应部分添加一条列表项；如果它回答的是常见问题，还要在其“When to run what”表中加一行；
- 只为有人会例行运行的 gate 添加 Makefile 目标，并把它的下限设为可覆盖的变量；
- 运行 `make format`（ruff 会格式化 `bench/`）、`pytest bench -q` 以及文案检查：

```bash
.venv/bin/python -m pytest tests/unit/test_public_copy.py tests/unit/test_bench_namespaces.py -q
```

文案检查会扫描 `bench/`：代码、注释或测试数据中不得出现产品名称、人名、内部主机或命名空间、论文引用或决策记录引用。

<a id="related"></a>

## 相关内容

- [方法论](https://github.com/ClickHouse/loom/blob/main/docs/benchmarks/methodology.md)：门禁和 bootstrap 的计算方式。
- [运行](https://github.com/ClickHouse/loom/blob/main/docs/benchmarks/running.md)：命令和输出。
- [测试](https://github.com/ClickHouse/loom/blob/main/docs/development/testing.md)：单元测试和集成测试套件。
