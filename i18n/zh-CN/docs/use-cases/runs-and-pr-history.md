<!-- i18n: source=docs/use-cases/runs-and-pr-history.md blob=8a6ae75f4719bf82572f8fbb0496949da479d685 model=claude-sonnet-5-5 -->
> 本页由机器从[英文原文](https://github.com/ClickHouse/loom/blob/main/docs/use-cases/runs-and-pr-history.md)翻译，内容以英文原文为准。
> Machine-translated from the [English page](https://github.com/ClickHouse/loom/blob/main/docs/use-cases/runs-and-pr-history.md), which is authoritative.

<a id="agent-runs-traces-and-pr-history"></a>

# Agent runs、traces 和 PR 历史

Loom 会把编码 agent 所做的工作记录为 run 和可搜索的会话对话,并将其与你的代码历史关联起来。每个 run 都带有目标和结果;当 Claude Code 插件上报会话的 turn 用量时(默认开启),还会带有模型成本。历史记录涵盖已合并的 pull request、review 评论、GitHub issue,以及在配置了 tracker 来源时的事故(incident)和 CI 运行。对话记录、PR 历史和 tracker 默认都是关闭的,需要手动开启。开启之后,agent 或开发者就可以查询之前尝试过什么、做出过哪些决定,以及是哪次变更引入了回归。

<a id="what-you-get"></a>

## 你将获得

- **Runs。** `run.begin` 和 `run.complete` 会记录任务的目标、状态、agent 所依赖的记忆以及它写入的内容。[Runs](https://github.com/ClickHouse/loom/blob/main/docs/features/runs.md)
- **对话与会话简报。** 已记录的会话可被搜索，会话简报则让下一次会话能够接着继续。在命名空间所有者通过 `conversation.set_logging(enabled=true)` 开启之前，记录功能处于关闭状态。[对话与会话](https://github.com/ClickHouse/loom/blob/main/docs/features/conversations-and-sessions.md) ·
  [追踪并恢复长时间运行的 agent 任务](https://github.com/ClickHouse/loom/blob/main/docs/guides/trace-and-resume.md)
- **PR 历史。** 包括符号、路径或行的历史、评审简报、提交 PR 前的重复检查以及 bisect 候选项，详见[代码索引页面](code-intelligence.md)。
- **Tracker 记录。** 重复 issue 检查、issue 与 PR 的关联以及不稳定测试，均从团队自己的 ClickHouse 中实时读取。需要 `LOOM_TRACKER_ORG` 和已注册的 tracker 源。[Tracker plane](https://github.com/ClickHouse/loom/blob/main/docs/features/tracker/overview.md)
- **追踪与成本。** Loom 会输出 OpenTelemetry 数据，并按操作记录自身的模型开销；Langfuse 和 ClickStack 为可选的接收端。
  [遥测](https://github.com/ClickHouse/loom/blob/main/docs/features/observability/telemetry.md) ·
  [成本与用量](https://github.com/ClickHouse/loom/blob/main/docs/features/observability/cost-and-usage.md)

<a id="measured"></a>

## 实测结果

在公开的 ClickHouse issue 上,`tracker.duplicate_check` 对 104 个已记录的重复项中的 91 个(0.875)把较早的 issue 排在了第一位,并且把 100 个没有已记录关联的 issue 中的 2 个判定为重复。该结果只来自一个 tracker,而且它的 issue(包含堆栈跟踪、错误码,重复项的关联也一致)比许多 tracker 的更容易处理。
[公开 ClickHouse issue 上的重复检查](https://github.com/ClickHouse/loom/blob/main/docs/benchmarks/results.md#duplicate-check-on-public-clickhouse-issues)

<a id="start"></a>

## 开始

[连接编码 agent](https://github.com/ClickHouse/loom/blob/main/docs/getting-started/connect-a-coding-agent.md) 会安装插件，在开启记录后由这些插件捕获会话；[代码索引](code-intelligence.md)需要先添加一个仓库，并设置 `LOOM_CODE_INGEST_ENABLED=true`。

本页中的部分 op 在[状态页](https://github.com/ClickHouse/loom/blob/main/docs/reference/status.md)中被标记为实验性。
