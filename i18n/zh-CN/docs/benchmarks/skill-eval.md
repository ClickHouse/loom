<!-- i18n: source=docs/benchmarks/skill-eval.md blob=c855282f25bb802cf3b249aa01819ec8e21227b7 model=claude-sonnet-5-5 -->
> 本页由机器从[英文原文](https://github.com/ClickHouse/loom/blob/main/docs/benchmarks/skill-eval.md)翻译，内容以英文原文为准。
> Machine-translated from the [English page](https://github.com/ClickHouse/loom/blob/main/docs/benchmarks/skill-eval.md), which is authoritative.

<a id="skill-eval"></a>

# 技能评估

技能评估通过 agent 留下的命名空间来评估 `loom-memory` skill。该 skill 规定了各类内容的存放位置：一条结论是在可读的键下执行一次纯文本的 `memory.set`，文档放入知识库并附一行简短的指针记录，带有标识的事物表使用 records，租约放入工作记忆，修订则改写同一个键。评估会运行一项任务，用 `namespace.audit` 对命名空间评分，并将任何严重（critical）发现视为失败。

<a id="the-pieces"></a>

## 组成部分

| 组成部分          | 位置                                                 | 说明                                                                                                                                                                                                                                                                                                                                    |
| ----------------- | ---------------------------------------------------- | --------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| 任务简报          | `bench/skill_eval/tasks/*.md`                        | 三份容易诱发反模式的简报：一份告警调查，包含长日志、代码片段、表格、租约和需要保留的大型模型（`sre_alert_investigation`）；一批带结构化字段的 PR（`pr_review_batch`）；一份修订了两次的笔记（`research_note_revisions`）。每份简报都会指明自己的命名空间，并说明将接受审计。 |
| 运行器            | `bench/skill_eval.py`                                | 运行任务（`--runner claude-code`、`codex`、`manual`、`replay`），然后执行 `namespace.audit`（一小时窗口，5 个样本），再执行形态计数器，最后写出结果 JSON。                                                                                                                                                                              |
| 录制的 agent      | `bench/skill_eval/replays/{bad,good}_agent.jsonl`    | `{op, body}` 流。bad agent 会对值进行双重编码、使用哈希键，并轮询存储为记忆的租约；good agent 则遵循[调查 agent 记忆](https://github.com/ClickHouse/loom/blob/main/docs/guides/investigation-agent-memory.md)指南。                                                                                                                   |
| 插件评测套件      | `integrations/claude-code-plugin/evals/loom-memory/` | 与上述简报相同，作为 `claude plugin eval` 用例，依据工具调用轨迹，针对模拟的 Loom 服务器（`evals/mocks/loom/`）进行评分。                                                                                                                                                                                                              |
| 单元测试          | `tests/unit/test_skill_eval.py`                      | 让两份回放通过运行器，对接一个伪造的 Loom，其审计使用处理程序自身的分类器：bad 会以预期的错误码失败，good 则通过。同时固定任务列表和评测提示词的正文。                                                                                                                                                                                   |

每份简报的 front matter 都包含 `task`、`namespace`、`agent` 和 `traps`（它所诱发的反模式）。运行器会替换回放流中的 `$ORG`、`$NAMESPACE` 和 `$AGENT`，因此一份录制内容可以在任意组织中运行。

<a id="the-score"></a>

## 评分

`pass` 表示审计返回 **0 个 critical** 发现，且 **最多 1 个 warning**（`MAX_WARNINGS_TO_PASS`）。报告会列出每项发现的数量、占已检查行数的比例、字节数以及写入端的修复建议，随后是从行本身读取的形态计数器，因此评级并不只取决于审计：

| 计数器                   | 含义                                                                                  |
| ------------------------ | ------------------------------------------------------------------------------------- |
| `rows`                   | 有效的记忆行                                                                          |
| `readable_key_share`     | 最后一段不是长十六进制串、且不含 sha 长度片段的键（即审计的 `hash_keys` 规则）        |
| `plain_text_value_share` | 值为字符串且不是序列化 JSON 的行                                                      |
| `documents`              | 知识文档（`knowledge.list`）                                                          |
| `records`                | 类型化记录（按每个已注册的 schema 执行 `memory.record.query`）                        |
| `working_memory_blocks`  | 工作记忆块（`working_memory.render`）                                                 |

形态读取在审计**之后**执行，因此不会被计为 agent 的读取。令牌无法读取的计数器会列在 `shape.skipped` 下，而不是报告为 0。

<a id="run-it"></a>

## 运行

<a id="replay-no-model-is-called"></a>

### 回放（不调用模型）

任何 Loom 都可以，本地部署即可。

```bash
export LOOM_BASE_URL=http://localhost:7777 LOOM_API_KEY=<writer token> LOOM_ORG=<org>
.venv/bin/python bench/skill_eval.py --task sre_alert_investigation --runner replay \
  --replay bench/skill_eval/replays/bad_agent.jsonl            # exits 1: FAIL
.venv/bin/python bench/skill_eval.py --task sre_alert_investigation --runner replay \
  --replay bench/skill_eval/replays/good_agent.jsonl --baseline   # exits 0: PASS
```

每次运行请使用新的命名空间（`--namespace skill-eval-sre-$(date +%s)`）。审计窗口为一小时，因此复用命名空间会把上一次运行的读取记录也计入评分。

离线时，同样的检查可作为单元测试运行，无需服务器：

```bash
.venv/bin/python -m pytest tests/unit/test_skill_eval.py -q
```

<a id="a-live-agent-with-the-skill-loaded"></a>

### 加载 skill 的实时 agent

```bash
.venv/bin/python bench/skill_eval.py --task sre_alert_investigation --runner claude-code --baseline
```

这会消耗模型 token。它在仓库根目录下运行：

```bash
claude -p --plugin-dir integrations/claude-code-plugin \
  --mcp-config <generated file: loom at $LOOM_BASE_URL/mcp?toolset=all, Bearer $LOOM_API_KEY> \
  --strict-mcp-config --allowedTools "mcp__loom__*" "Skill" "Read" \
  --permission-mode bypassPermissions --output-format json --max-turns 60 \
  "<the task body>"
```

- `--plugin-dir` 从当前检出的代码加载 skill，因此本次运行衡量的是工作树中的 skill 文本。
- `toolset=all` 会列出 `memory_schema.register` 和 `memory.record.write`，默认的 `core` 列表不包含这两个。即使在 `all` 下也不会列出 `knowledge.upsert`（core 和 atomic 清单都不包含它），agent 通过 `loom_op` 调用它。
- 生成的 MCP 配置包含令牌，创建时权限为 `0600`，运行结束后删除。
- `--max-turns`（默认 60）限制运行的轮数。

`--runner codex` 使用相同的服务器配置运行 `codex exec`；已安装 Codex 插件时，由插件提供 skill。

<a id="any-other-agent"></a>

### 其他任意 agent

`--runner manual` 会打印任务说明。你可以针对其中指定的命名空间运行任意 agent，然后对它留下的内容评分：

```bash
.venv/bin/python bench/skill_eval.py --task pr_review_batch --runner manual
.venv/bin/python bench/skill_eval.py --task pr_review_batch --runner manual --audit-only
```

同样的 `--audit-only` 也可以对 `claude plugin eval` 使用真实服务器运行后写入的命名空间评分。

<a id="compare-with-a-baseline"></a>

### 与基线对比

修改 skill 后再次运行并对比：

```bash
.venv/bin/python bench/skill_eval.py --task sre_alert_investigation --runner claude-code \
  --compare bench/out/skill_eval_sre_alert_investigation_baseline.json
```

差异表中每个发现代码（finding code）占一行（严重程度和数量，修改前后各一列），每个形态计数器也占一行。实时 agent 的行为并非确定性的，因此评估 skill 的修改应通过多次运行来对比，而不是只运行一次。

<a id="output"></a>

### 输出

每次运行都会写入 `bench/out/skill_eval_<task>_<runner>_<ts>.json`（可用 `--out-dir` 更改位置）。`--baseline` 还会写入 `bench/out/skill_eval_<task>_baseline.json`，这是 `--compare` 读取的参照文件。`bench/out/` 已被 gitignore 忽略。

<a id="the-plugin-eval-suite"></a>

## 插件评测套件

```bash
claude plugin eval integrations/claude-code-plugin --trust-plugin --no-publish \
  --case 'loom-memory/*' --runs 3 --threshold 1.0 --json evals-result.json
```

该评测根据工具调用轨迹离线给每次运行打分：Loom MCP server 是一个返回固定回复的 mock（`evals/mocks/loom/`），因此不会写入任何数据。评分器只使用文档中记载的评分器类型：

- 对记忆写入使用 `max: 0` 的 `tool_used`，当写入的输入包含哈希键、JSON 字符串套 JSON 的值、大小超过约 2 KB 的值、租约键或锁键，或带版本号的修订键时即判定失败：这些形态在 `namespace.audit` 中被评为 critical 或 warning；
- 匹配 `loom-memory` 的 `tool_used: Skill`，即插件已触发的指示标志；
- 对工作记忆和按条目声明（claim）设置正数下限。

不使用 `llm` 评分器：对文字内容按评分细则进行阅读评判不能作为准入门槛。用例可以按名称通配符过滤（`--case sre_alert_investigation`）。`--ablation with-without` 是插件可解析时的默认选项，它会报告同一提示在没有插件的情况下触发相同评分器的频率。

该框架没有脚本评分器，因此命名空间级别的评分需要另一条命令。使用 `--mocks off --allow-real-servers --allow-tools "mcp__plugin_loom_loom__*"`，并在 `LOOM_API_KEY` 中提供具有写入权限的令牌，agent 会向该用例的命名空间写入数据，再由 `bench/skill_eval.py --task <case> --runner manual --audit-only` 对其评分。

<a id="reading-a-failure"></a>

## 解读失败

每条发现的补救方法都是写入端的修复。`loom-optimize` skill 会把同样的代码对应到对已有行的重新整理（[优化命名空间](https://github.com/ClickHouse/loom/blob/main/docs/guides/optimize-namespace.md)）。

| 发现                                       | 未被遵守的规则                                                |
| ------------------------------------------ | ------------------------------------------------------------- |
| `double_encoded_values`                    | 值只发送一次，切勿对字符串执行两次 `json.dumps`。             |
| `hash_keys`                                | 键应为可读的 `<scope>/<kind>/<id>`，而不是哈希。              |
| `coordination_as_memory`                   | 租约和锁应放在工作记忆中。                                    |
| `exact_key_store`                          | 把指针保存在工作记忆中，而不是按键轮询。                      |
| `oversized_values`, `json_prefix_excerpts` | 文档应放入知识库，并附一行简短的指针行。                      |

在 SRE 任务中，如果一次运行通过了审计但 `documents: 0`，说明大模型被放在了搜索无法触及的地方。请阅读 shape 行，而不要只看结论。

<a id="adding-a-task"></a>

## 添加任务

新的 brief 需要包含 front matter，在正文中指明一个 `skill-eval-…` 命名空间，并以说明该命名空间将被审计的结束语收尾；还需要一个正文逐字节一致的对应插件评测用例，并更新 `tests/unit/test_skill_eval.py` 所固定的任务列表。参见[添加用例](https://github.com/ClickHouse/loom/blob/main/docs/benchmarks/adding-a-case.md#skill-eval-tasks)。

<a id="what-this-does-not-measure"></a>

## 本文不衡量的内容

召回质量。agent 写入的声明能否回答下周的问题，是召回基准测试的任务（[概述](https://github.com/ClickHouse/loom/blob/main/docs/benchmarks/overview.md)）；本文评估的是那些会导致无法召回的形态。

<a id="related"></a>

## 相关内容

- [调查类 agent 记忆](https://github.com/ClickHouse/loom/blob/main/docs/guides/investigation-agent-memory.md)：良好回放所遵循的方案。
- [优化命名空间](https://github.com/ClickHouse/loom/blob/main/docs/guides/optimize-namespace.md)：重新处理审计标记的行。
- [Claude Code 插件](https://github.com/ClickHouse/loom/blob/main/docs/features/integrations/claude-code.md)和 [Codex](https://github.com/ClickHouse/loom/blob/main/docs/features/integrations/codex.md)。
- [`integrations/claude-code-plugin/evals/README.md`](https://github.com/ClickHouse/loom/blob/main/integrations/claude-code-plugin/evals/README.md)。
