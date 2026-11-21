<!-- i18n: source=docs/features/runs.md blob=7aa894eee29c65b040ebfbad315df18b437034ae model=claude-sonnet-5-5 -->
> 本页由机器从[英文原文](https://github.com/ClickHouse/loom/blob/main/docs/features/runs.md)翻译，内容以英文原文为准。
> Machine-translated from the [English page](https://github.com/ClickHouse/loom/blob/main/docs/features/runs.md), which is authoritative.

<a id="runs"></a>

# 运行（Runs）

run 是任务的一次执行，例如“处理 issue 1234”。`run.begin` 开启一个 run 并返回 `run_id`；agent 在写入操作（`memory.set`、`memory.update`、`observe.record`）中传入该 id；`run.complete` 结束这个 run，并附带状态、agent 所依赖的记忆，以及可选的反思。读取操作（`memory.search`、`memory.ask`）的请求体中带有 `run_id` 时，会把结果过滤为该 run 自己的行，因此读取改用标记的方式：通过 HTTP 时使用 `X-Loom-Run-Id` 请求头（Python SDK 中为 `run_scope`），通过 MCP 时使用会话自身的 run。成功的 run 所依赖的记忆下次排名会更靠前；失败的 run 所依赖的记忆则会失去之前获得的加成。

<a id="what-a-run-records"></a>

## 运行记录了什么

运行行存放在 `loom.agent_runs` 中。`run.get` 显示的大部分内容是在读取时从其他平面派生出来的：

| 字段                               | 存储或派生 | 来源                                                                                                                                                  |
| ---------------------------------- | ---------- | ----------------------------------------------------------------------------------------------------------------------------------------------------- |
| `goal`、`user_request`、`model` 等 | 存储       | `run.begin`                                                                                                                                           |
| `status`、`final_response`         | 存储       | `run.complete`（在此之前为 `active`）                                                                                                                 |
| `memories_used`                    | 存储       | `run.complete`：agent 自己声明的所依赖的内容                                                                                                          |
| `grade`、`grade_source` 等         | 存储       | `run.grade`，在运行关闭之后                                                                                                                           |
| `memories_written`                 | 派生       | `loom.memory` 中带有该运行 `run_id` 的行                                                                                                              |
| `recalled`、`recalled_keys`        | 派生       | `run_id` 下的搜索追踪日志，与 `run.complete(recalled_keys)` 取并集                                                                                    |
| `observations`                     | 派生       | 由 `observe.record` 写入、带有该 `run_id` 标记的情景行                                                                                                |
| `turns`、`cost_usd`                | 派生       | 运行期间会话在 `loom.agent_turn_usage` 中产生的轮次（由 Claude Code 插件写入，或由使用 `cost.record_turns` 的 agent 写入）                            |
| `budget_usd`、`budget_seconds`     | 存储       | `run.begin`（0 = 无预算）                                                                                                                             |
| `elapsed_seconds`、`over_budget`   | 派生       | 根据运行从开始到关闭（或当前）的时间（以服务器时钟为准）以及 `cost_usd`，与预算比较得出                                                               |

当 `LOOM_SEARCH_TRACE_LOG_ENABLED` 关闭且未发送 `recalled_keys` 时，`recalled` 为 `-1`。

<a id="budgets"></a>

## 预算

`run.begin` 接受可选的 `budget_usd`（模型花费）和 `budget_seconds`（运行允许持续的时间）。`run.get` 会将它们与 `cost_usd` 和 `elapsed_seconds` 比较，并在 `over_budget` 中列出该运行已超出的项（`cost`、`time`，或两者）。Loom 不运行 agent，因此预算只是给运行 agent 的一方的信号，而不是强制停止。

一次运行的花费，是其会话在该运行处于打开状态期间产生的各轮对话的花费，按每一轮自身的时间（`turn_at`，来自转录）计算，所以 `budget_usd` 需要 `session_id`（否则返回 422）。花费精确到小数点后 6 位，与 cost 相同，因此最小值为 $0.000001。时间按服务器时钟，从运行开始计到运行关闭，与客户端上报的 `duration_ms` 无关。请在运行的第一次 `run.begin` 中设置预算。

会话中第一次发现活动运行已超出预算的 `run.get`，还会附带 mentor 提示 `run_over_budget`，插件会将其作为建议交给模型。已关闭的运行仍会报告 `over_budget`，但不会收到提示。与所有 mentor 提示一样，它在每个会话中只提示一次；不发送会话的调用方（SDK、普通 HTTP）则在每个命名空间和每个 web 进程内共享一个会话，因此请根据 `over_budget` 做判断，而不是根据提示。

限制：

- **仅在两次提示之间检查。** Claude Code 插件在一次提示结束时才提交该提示的各轮对话（agent 也可以用 `cost.record_turns` 自行提交），因此成本预算只在两次提示之间检查，而单次无界面提示（`claude -p`）只有在结束之后才会显示其花费。即使这些轮次在 agent 自己的 `run.complete` 之后才到达，它们仍会被计入。一次调用的成本只有在返回之后才知道，所以一次运行在被发现之前，可能会超出 `budget_usd`，超出的量最多为最后一轮的成本。
- **客户端的时钟。** 该时间窗口将客户端的轮次时间与服务器的运行时间相比较；时钟相差几分钟，就会使运行边界处的轮次归属错误。一个轮次会保留其发送时所带的时间，因此重新发送的轮次仍留在它最初所属的运行中。没有带时间（或时间无法解析）的轮次，会采用其 `message_id` 被记录时的时间，如果是新的则采用当前时间；在第一次发送后的零点几秒内重新发送，仍可能被记为当前时间。`cost.record_turns` 的响应中包含 `turns_dated_ahead`，即它收到的时间比服务器时钟晚 5 秒以上的轮次数，因此自行提交轮次的调用方可以据此发现时钟偏快；Claude Code 插件提交轮次时不会读取该响应。
- **一个会话，多个运行。** 在同一会话中同时打开的多个运行看到的是相同的轮次：嵌套的运行会同时计入两者，而各自需要成本预算的并发 worker 则需要各自独立的会话。
- **未定价的模型。** 如果价格表中没有某一轮所用的模型，该轮的成本记为 0；`run.get` 会在 `unpriced_turns` 中统计这些轮次，它们不计入 `budget_usd`。
- **MCP。** MCP 工具只提供 `budget_seconds`，因为模型并不知道记录其各轮对话所用的会话 id。
- **重试。** 重新发送的 `run.begin` 会像任何重试一样替换该运行的行：未重新声明的会话和预算会保留，新的非零预算会替换旧预算，而 0 表示保留原值。新的 `budget_usd` 必须与其 `session_id` 一起发送（否则返回 422）；新的 `budget_seconds` 则不需要其他参数。如果重新发送的 begin 把运行移到另一个会话，则会丢弃其成本预算（它是为旧会话的轮次设置的），保留时间预算，并在其 `next` 中说明。这是移除预算的唯一方式。如果 `run.begin` 返回的 `run_id` 与发送的不同，请重新发送该 `run_id`，或使用相同的 `session_id`；否则重试会打开一个新的运行。

<a id="recalled-is-not-relied-on"></a>

## 召回不等于依赖

`run.complete` 携带两个列表：

- **`memories_used`**：agent 实际依赖的键。每个键都会产生一条 `used` 使用事件；结果为正面时再加一条 `evidence` 事件，结果为负面时再加一条 `harm` 事件。`outcome` 默认由 `status` 决定：`completed` 为正面，`failed` 为负面，`cancelled` 为无。
- **`recalled_keys`**：Loom 的工具在运行期间返回过的键（Claude Code 和 Codex 插件会从 `loom_search` 和 `memory.get` 的结果中暂存这些键，并在会话结束时发送）。它们只记录在 `run.get` 中，不会计入任何奖励。

一条被返回却被忽略的记录不会获得任何奖励；如果为每个搜索命中都计入奖励，就等于奖励了排名第一的内容。`usage_fold` worker 会把这些事件折算为强化项，[记忆搜索](https://github.com/ClickHouse/loom/blob/main/docs/features/memory/search-and-ask.md)按该强化项排序。`harm` 事件会收回已获得的提升，但下限为该记录写入时的基础值，因此一次糟糕的运行不会把某条记录压到其相关性之下，也不会删除它。

在 `memories_used` 中列出的 procedure 键，还会在 procedure 台账中新增一行（path 为空，`ok` 取自结果）；自行调用 `procedure.record_run` 的 agent 应当把该键从 `memories_used` 中去掉，以免这次运行被重复计数。参见[procedure 与 learning](https://github.com/ClickHouse/loom/blob/main/docs/features/memory/procedures-and-learnings.md)。

<a id="reflections"></a>

## 反思

`run.complete(reflection=...)` 会记录 `what_worked`、`what_failed`、`assumptions`、`lessons`、`improvements`（每项最多 20 条）、一个 `confidence`，以及该经验所依据的 `evidence` 键。它会作为一条类型为 `reflection` 的记忆存储，键为 `reflection:<run_id>`：

- 它会被嵌入向量化，因此之后的 run 可以通过 `memory.search(memory_type="reflection")` 找到它，但它不会出现在未加过滤的搜索和 `memory.ask` 中；
- `what_failed` 和 `lessons` 至少需要一个 `evidence` 键（否则返回 422）；
- 它会在 `expires_in_days` 之后过期（默认 90，`0` 表示永不过期），除非 `memory.verify` 或一次正面的结果为其续期；
- 以 `role`（planner、executor、critic，或你自己定义的名称）开启的 run，会给其反思打上 `role:<role>` 标签，因此相同角色的 agent 在调用 `memory.search(role=...)` 时会优先获取它。

<a id="what-happens-to-the-rows-a-run-wrote"></a>

## 运行写入的行会怎样

`run.complete` 上的 `retain` 决定该运行下写入的记忆的去留：

| `retain`         | 效果                                                                                                                                          |
| ---------------- | --------------------------------------------------------------------------------------------------------------------------------------------- |
| `keep`（默认）   | 不做任何更改。                                                                                                                                |
| `demote`         | 立即降低它们的置信度。                                                                                                                        |
| `expire`         | 设置在 `LOOM_MEMORY_RUN_COMPLETE_EXPIRE_DAYS` 之后（默认 7）到期，并附带 `on_expire=demote`；由 `memory_expiry_sweep` worker 执行。 |

无论采用哪种模式，已固定、已验证、已提升、已锁定的行，以及 episodic 和 reflection 行都会被保留，并且没有任何模式会将任何内容标记为删除。默认值来自 `LOOM_MEMORY_RUN_COMPLETE_RETAIN`；MCP 会话的隐式运行始终采用 keep，因为关闭它的传输层无法判断这项工作的好坏。

<a id="graded-runs"></a>

## 评分的运行

`status` 表示运行已结束；评分则表示其结果是否站得住脚。评分往往在几天后才到来，比如 CI 通过，或评审者拒绝了拉取请求。`run.grade` 接受 `grade`（`pass` 或 `fail`）、`source`（`maintainer`、`ci`、`reviewer` 等，自由文本）、`reason` 和 `ref`（URL、finding key、check run id），可指向某一个 `run_id`，也可指向某个 `session_id`（例如 `pr_1234`）下所有已关闭的运行。重新评分会替换原有结论；同一来源给出相同的结论则不产生任何变化。仍在进行中的运行会被跳过。

已评分运行的 `memories_used` 中的每条记忆，都会按该评分计数。`memory.proposals` 会在每条试用中的事实（即一行 `memory.set(proposed=true)`）旁显示这些计数。只有当命名空间所有者把 `proposal_policy.mode` 设为 `auto` 时，Loom 才会依据这些计数对提案做出决定；无论哪种模式，任何写入者都可以用 `memory.proposal_decide` 做出决定。参见[写入记忆](https://github.com/ClickHouse/loom/blob/main/docs/features/memory/writing.md)。

`run.list(grade="ungraded")` 会列出仍待评分的已关闭运行。

MCP 会话无需请求就会获得一个隐式运行：其第一次运行范围内的工具调用会开启该运行，`DELETE /mcp` 或 `mcp_session_run_sweep` worker 会将其关闭（参见 [MCP](https://github.com/ClickHouse/loom/blob/main/docs/reference/mcp.md)）。清扫任务会关闭满足以下条件的进行中运行：`environment='mcp'`，`metadata.implicit` 为 true，且 id 为 `mcp-` 加 40 个十六进制字符（只有传输层才会生成这种形状）。当其令牌的任何调用（若运行未记录令牌，则指任何人的调用）在 `LOOM_MCP_SESSION_RUN_IDLE_SECONDS` 内都没有触及该运行时，清扫任务才会关闭它。该运行位于调用者的默认命名空间中，因此其读取者所见的内容也留在该命名空间内：它的目标是标签 `mcp session (<agent>)`，绝不是某次调用的文本（一次读取的查询可能触及调用者可读的任意命名空间），而 `memories_written` 只列出该命名空间中的写入（其他写入仍带有其 `run_id`）。关闭运行不做任何评判：其结果为 `none`，无论服务器的默认保留模式如何，其记忆都会被保留，因此两个同时关闭它的操作（两台主机上的清扫任务，或一次 `DELETE`）最坏的情况也只是把同一条完成记录写入两次。

运行如何跨越多个 web 进程。其 id 是调用者所属组织、令牌的 subject 与 `Mcp-Session-Id` 的哈希，因此服务器的每个进程都会为该会话指向同一个运行，而其他令牌即使发送相同的会话 id 也无法指向它。进程在会话第一次被放行的运行范围内调用时注册该运行，并在后台写入开始行（若等待插入完成，会让该调用在 ClickHouse Cloud 上卡住数秒）；被闸门拒绝的调用不会写入运行行。两个进程都没找到行时，各自写入一条首次开始记录，较早的开始时间及其所有者胜出（首次开始记录的行版本从其开始时间起倒数；之后的每次写入都会取代它）。每个进程对缓存的运行，每分钟最多向账本核对一次，若期间 `DELETE` 或清扫任务已将其关闭，则重新打开它。`DELETE` 无论到达哪个进程，都按 id 关闭该运行；关闭时会统计该 id 下的每一次写入，不论由谁关闭。若 `DELETE` 在开始行落盘之前到达另一个进程，则没有可关闭的内容，随后开始行落盘，形成一个进行中的运行，待其空闲后由清扫任务关闭：短时间重试无法覆盖耗时数秒的开始写入，还会拖慢每个从未产生运行的会话的 `DELETE`。重新打开的运行保留其原始 `started_at`，因此一个被清扫任务关闭、之后又重新打开的运行，其报告的 `duration_ms` 从会话的第一次调用算起，间隔时间也计算在内。若核对时完全找不到行（因为擦除操作删除了它），则会开启一个从此刻开始的新运行，因此不会有早于擦除时间的运行重新出现。

<a id="operations"></a>

## 操作

| 操作           | 角色   | 作用                                                                                                                                                                                                                                                  |
| -------------- | ------ | ----------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `run.begin`    | writer | 开启一次运行：`goal`（必填）、`user_request`、`expected_outcome`、`run_id`（为空或 `-`：自动生成）、`session_id`、`role`、`model`、`metadata`。重复发送相同的 `run_id` 会替换该行；如果 id 的形式与 MCP 传输层相同（`mcp-` 加 40 位十六进制数字），则会为其分配一个新的 id。 |
| `run.complete` | writer | 关闭运行，仅可调用一次：`status`、`memories_used`、`recalled_keys`、`outcome`、`reflection`、`retain`。不具备幂等性：完成两次会追加两次使用事件。                                                                                                      |
| `run.get`      | viewer | 获取一次运行及其当前有效的衍生数据。                                                                                                                                                                                                                  |
| `run.list`     | viewer | 获取一页运行记录，可按 `agent`、`session_id`、`status`、`grade`、`q` 和 `memory_activity` 过滤（`with` 保留依赖过或写入过记忆的运行，`without` 保留没有记忆活动的运行；过滤先于 `limit` 生效）。                                                      |
| `run.grade`    | writer | 将已关闭的运行或某个会话的各次运行评为 `pass` / `fail`。                                                                                                                                                                                              |

`run.list` 通过 `order_by`（按开始时间、持续时间或所依赖的记忆数，各自可升序或降序；默认为 `started_desc`，即最新的在前）和 `offset`（最大 100,000）分页。`q` 保留目标、运行 id 或 agent 中包含该文本的运行，不区分大小写。`total` 统计所有符合过滤条件的运行；在第一页之后的满页上为 null，因为仅凭该页无法得出总数。

完整的字段列表见[操作参考](https://github.com/ClickHouse/loom/blob/main/docs/reference/operations.md#run)。`memory.outcome_run` 会为某次运行下召回的每条记忆记录一个 helped / hurt 结果，适用于批量评分而不逐个指定 key 的调用方。

<a id="example"></a>

## 示例

Python SDK 的 `run.scope` 在进入时开启 run，在退出时关闭 run。代码块内 `memory.ask` 的回答所引用的 key，加上传给 `used()` 的 key，会成为 `memories_used`；如果抛出异常，run 会以 `failed` 状态关闭（outcome 为负面），然后异常继续向外传播。代码块内的写入会带上该 run 的 id。

```python
from loom import Agent

async with Agent(org="acme", namespace="support", agent="triage-bot") as agent:
    async with agent.run.scope(goal="Answer ticket 4411: refund policy for annual plans") as run:
        answer = await agent.memory.ask("what is the refund policy for annual plans?")
        run.used("policy/refunds/annual")  # relied on beyond what the answer cited
        run.final_response = answer["answer"]

    replay = await agent.run.get(run.run_id)
    print(replay["run"]["status"], replay["memories_written"], replay["recalled"])

    # Later, once the customer confirmed the answer was right:
    await agent.run.grade("pass", run_id=run.run_id, source="support-lead")
```

通过 HTTP 调用时，生命周期由两次调用组成：

```bash
curl -sX POST "$LOOM_BASE_URL/v1/run.begin" -H "Authorization: Bearer $LOOM_API_KEY" \
  -H 'Content-Type: application/json' \
  -d '{"org":"acme","namespace":"support","agent":"triage-bot","goal":"Answer ticket 4411"}'
# → {"run_id":"…","status":"active","started_at":"…","next":"…"}

curl -sX POST "$LOOM_BASE_URL/v1/run.complete" -H "Authorization: Bearer $LOOM_API_KEY" \
  -H 'Content-Type: application/json' \
  -d '{"org":"acme","namespace":"support","run_id":"…","status":"completed",
       "memories_used":["policy/refunds/annual"]}'
```

`Agent(auto_run=True)`（或在客户端环境中设置 `LOOM_AUTO_RUN=on`）会在第一次调用记忆平面时延迟开启一个 run，让之后的每次调用都归入该 run，并在 `Agent` 关闭时将其完成，outcome 为 `none`，因此不会给任何内容加分或扣分。它默认关闭，因为在 run 下的写入会限定在该 run 范围内：对另一个 run 的行的重述会被存为 `duplicate`，而不是被合并。

<a id="settings"></a>

## 设置

| 设置                                                          | 作用                                                                                       |
| ------------------------------------------------------------- | ------------------------------------------------------------------------------------------ |
| `LOOM_MEMORY_RUN_COMPLETE_RETAIN`                             | 调用未指定 `retain` 模式时使用的模式（`keep`）。                                           |
| `LOOM_MEMORY_RUN_COMPLETE_EXPIRE_DAYS`                        | `retain=expire` 将过期时间设置在多久之后（7 天）。                                         |
| `LOOM_SEARCH_TRACE_LOG_ENABLED`                               | 按 `run_id` 记录每次搜索，`recalled` 即由此得出。由于会记录查询文本，默认关闭。            |
| `LOOM_USAGE_FOLD_ENABLED`                                     | `usage_fold` worker，将 used / evidence / harm 事件转化为排序依据。                        |
| `LOOM_MEMORY_SEARCH_REINFORCEMENT_WEIGHT`, `…_HALF_LIFE_DAYS` | 依赖程度在搜索中提升某一行排名的幅度和持续时间。                                           |

参见[配置：去重与过期](https://github.com/ClickHouse/loom/blob/main/docs/reference/configuration.md#dedup-and-expiry)、
[worker](https://github.com/ClickHouse/loom/blob/main/docs/reference/configuration.md#workers)和
[记忆搜索](https://github.com/ClickHouse/loom/blob/main/docs/reference/configuration.md#memorysearch)。

<a id="limits"></a>

## 限制

- `memories_used` 和 `recalled_keys` 各最多接受 500 个键；`goal` 最多 2,000 个字符。
- `memories_used` 中无法解析到有效行的键不会获得任何收益；响应中的 `used_applied` 统计的是成功解析的键的数量。
- `run.complete` 不是幂等的。重试完成操作会使使用事件被重复计数，因此只应完成一次。
- SDK 的 `run.scope` 以尽力而为的方式写入账本：`run.complete` 失败时不会掩盖代码块自身的异常，因此完成失败的运行会保持 `active` 状态。
- 当运行没有 `session_id`，或客户端不上报轮次用量时，`turns` 和 `cost_usd` 为 0。它们只统计运行处于打开状态期间的轮次。

<a id="related"></a>

## 相关内容

- [追踪并恢复长时间运行](https://github.com/ClickHouse/loom/blob/main/docs/guides/trace-and-resume.md) — 以 `run_id` 为键保存在工作记忆中的检查点，以及如何追踪一次运行做了什么。
- [搜索与提问](https://github.com/ClickHouse/loom/blob/main/docs/features/memory/search-and-ask.md) — 强化如何进入排序。
- [任务与决策](https://github.com/ClickHouse/loom/blob/main/docs/features/tasks-and-decisions.md) — `decision.outcome` 会为做出决策的运行所依赖的记忆记功。
- [记忆架构](https://github.com/ClickHouse/loom/blob/main/docs/architecture/memory-architecture.md) — 使用事件的折叠及其背后的表。
