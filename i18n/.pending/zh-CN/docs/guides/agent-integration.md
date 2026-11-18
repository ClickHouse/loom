<!-- i18n: source=docs/guides/agent-integration.md blob=456e73fdf0db8acbbcc8465c94067cbbf581e556 model=claude-sonnet-5-5 -->
> 本页由机器从[英文原文](https://github.com/ClickHouse/loom/blob/main/docs/guides/agent-integration.md)翻译，内容以英文原文为准。
> Machine-translated from the [English page](https://github.com/ClickHouse/loom/blob/main/docs/guides/agent-integration.md), which is authoritative.

<a id="integrate-loom-into-an-agent"></a>

# 将 Loom 集成到 agent 中

本指南介绍 agent 向 Loom 发送什么内容、如何读取返回的结果：客户端接口、身份、写入什么以及如何读取。阅读前请先了解[记忆](../concepts/memory.md)和[身份与命名空间](../concepts/identities-and-namespaces.md)。带有已发布插件的编码 agent（[Claude Code](../features/integrations/claude-code.md)、[Codex](../features/integrations/codex.md)）可以从插件中获得其中大部分内容。

同样的规则，若是写给 agent 而不是写给你的，就是 `loom` Agent Skill（`skills/loom/`）。没有插件的 agent 可以从服务器读取它：实验性的 [`mentor.guide`](https://github.com/ClickHouse/loom/blob/main/docs/reference/operations/mentor.md#mentorguide) 会返回循环流程、每种状态应写入何处以及如何读取结果（`part: "skill"`），或者返回所有 plane 及其 op（`part: "reference"`）。请将其放入 agent 的系统提示词中：

```python
system_prompt = "You triage support tickets.\n\n" + await agent.guide()
```

通过 MCP 时，引导文本会指明该调用；使用 Agent-Skills 的客户端可以通过 `npx skills add ClickHouse/loom --skill loom` 安装它。

<a id="1-choose-the-client-surface"></a>

## 1. 选择客户端接入方式

| 接入方式                                                     | 适用场景                                                                                        | 你能得到什么                                                                                                                                                                       |
| ------------------------------------------------------------ | ----------------------------------------------------------------------------------------------- | ---------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| **MCP** (`/mcp`)                                             | agent 是已支持 MCP 的 LLM 客户端（Claude Code、Codex、Cursor、桌面应用）。 | 每个 `?toolset=` 对应一份精选的工具列表，工具描述面向模型编写，身份默认值取自连接。由模型决定何时调用。                            |
| **Python SDK** (`clickhouse-loom`, `from loom import Agent`) | agent 是你自己的 Python 进程。                                                           | 每个平面一个带类型的客户端，支持重试和幂等键，提供用于模型工具循环的 `agent.tools()`、运行作用域，以及框架适配器（LangGraph `LoomStore`、Strands `LoomMemoryStore`）。 |
| **TypeScript SDK** (`@clickhouse/loom`)                      | agent 是 Node 服务或浏览器端服务。                                                    | `Datapack` / `Agent`，根据生成的 op 表提供类型的 `agent.call(op, body)`，以及 `AgentTools`、`Session`。                                                                           |
| **HTTP** (`POST /v1/<op>`)                                   | 其他任何语言，或一次性脚本。                                                        | 支持所有 op，JSON 请求体相同；重试和 `Idempotency-Key` 请求头需要你自己处理。                                                                                      |

这四种方式都由同一个注册表提供相同的 op。选择的依据是由谁决定调用：由模型决定时用 MCP，由你的代码决定时用 SDK。许多 agent 两者并用：开始时用 SDK 加载上下文，结束时用 SDK 记录本次运行，而模型自行选择要记住或查询的内容时，则使用 MCP 工具。通过 MCP 时，用 `?toolset=` 选择工具列表（[MCP 工具集](https://github.com/ClickHouse/loom/blob/main/docs/reference/mcp.md#toolsets)）；列表中未包含的 op 仍可通过 `loom_op` 调用。

<a id="2-choose-the-identity"></a>

## 2. 选择身份

每一行都带有身份元组 `(org, namespace, agent, user_id, session_id)`，外加 `run_id`。令牌决定 `org` 和命名空间，其余字段随每次调用传入。

| 字段         | 设置为                                                | 为什么重要                                                                                      |
| ------------ | ----------------------------------------------------- | ----------------------------------------------------------------------------------------------- |
| `namespace`  | 每个 agent 一个（`agent-<name>`），或由一组 agent 共用一个 | **成员资格就是读取边界**：命名空间的读取者可以读取其中的每一行，无论是哪个 agent 写入的。       |
| `agent`      | 写入方 agent 的稳定名称                               | 用于标明作者，而非隔离。通过 MCP 时，默认使用客户端名称（`clientInfo.name`，经 slug 化处理）。 |
| `user_id`    | agent 所服务的终端用户（当它服务多个用户时）          | 使用以 `--read-scope own_user` 签发的令牌，一个用户的提示词无法取出其他用户的记忆。             |
| `session_id` | 对话或作业                                            | 限定工作记忆和 `own_session` 读取的范围；用于归组对话记录。                                     |
| `run_id`     | 任务的一次执行                                        | 将一次运行的写入、搜索、检查点和对话记录关联在一起（[运行](https://github.com/ClickHouse/loom/blob/main/docs/features/runs.md)）。 |

- **一个 agent、多个用户、一个令牌：** 用 `--read-scope own_user` 签发令牌（或固定 `--read-user-id`），并在每次调用中传入 `user_id`。缺少该 `user_id` 的限定范围读取会返回 400 `IDENTITY_INCOMPLETE`，不会被放宽。
- **多个 agent 共享知识：** 使用一个它们都能读取的共享命名空间，每个 agent 以自己的 `agent` 写入；或者使用一个可读取共享命名空间并写入自己命名空间的令牌（`ns_write`）。参见[命名空间访问](https://github.com/ClickHouse/loom/blob/main/docs/guides/tokens-and-grants.md#namespace-access)。
- **`agent` 中不要放入密钥或个人标识符：** 它会被写入每一条账本记录。

<a id="3-decide-what-to-write"></a>

## 3. 决定写入什么

先选择层面。持久记忆会被嵌入、排序、冲突检查并保留版本。工作记忆按 key 读取，从不参与排序。知识库存放分块后的文档。

| agent 拥有的内容…                                                      | 写入方式                                                                                      | 不要使用                              |
| ---------------------------------------------------------------------- | --------------------------------------------------------------------------------------------- | ------------------------------------- |
| 下周仍然成立、其他会话需要的事实（决策、约束、负责人、偏好）           | `memory.set`：每次调用一条事实，每条事实一个 key                                              | 工作记忆                              |
| 当前计划、未决假设、本次任务的检查清单                                 | `working_memory.set` / `append`，由 `context.pack` 加载                                       | `memory.set`                          |
| 值得记住的对话记录、邮件或讨论串                                       | `memory.set_from_messages`：服务器提取带短 key 的事实                                         | 一个巨大的 `memory.set`               |
| 文档（规格说明、运行手册、报告，以及任何超过约 2 KB 的内容）           | 以 Markdown 形式 `knowledge.upsert`，并在 `memory.set` 中用相同的 key 写一段指向它的说明      | 用一个 `memory.set` 存放全文          |
| 带有标识的事实表（按编号区分的 PR、按 id 区分的服务）                  | 先执行一次 `memory_schema.register`，再使用 `memory.record.write`                             | 每批数据一行记忆                      |
| 协调状态（租约、冻结标志、“进展到哪里了”）                             | 按 `block_key` 使用 `working_memory.set`                                                      | 循环调用 `memory.set` + `memory.get`  |
| 用于恢复的检查点                                                       | 工作记忆块，或对大型数据使用 `context.offload`                                                | 每一步写一条持久记忆                  |
| 之后要展示和搜索的对话                                                 | `conversation.create` / `conversation.append`                                                 | 记忆                                  |
| 推理过程、问候语、中间的工具输出                                       | 不写入                                                                                        | 任何方式                              |

[调查类 agent 的记忆](https://github.com/ClickHouse/loom/blob/main/docs/guides/investigation-agent-memory.md)把这张表应用到处理案件的 agent 上；[追踪与恢复](https://github.com/ClickHouse/loom/blob/main/docs/guides/trace-and-resume.md)介绍检查点。

**让每条事实的形态都值得后续会话信任：**

- **先写结论。** value 用纯文本陈述事实，随后再写来源行。只有 value 的开头部分会被嵌入（摘录在 1,200 个字符处截断）。
- **可读的 key**，格式为 `<scope>/<kind>/<id>`：例如 `sre/case/1234/evidence/dns-sidecar`，而不是哈希值。分类维度放在 `tags`（`case:1234`、`kind:evidence`）和 `entities` 中。
- **修订时重写同一个 key。** Loom 会保留各个版本（`memory.history`）并返回当前版本；不要创建 `deploy_window_v2` 这样的 key，也不要保留一条指向当前 key 的索引行。
- **说明来源：** 用 `source_resource_id` 标明事实所读取自的文件、URL 或文档。
- **不要把推断当作事实。** 应写成“根据 A 和 B 推断：C”，并将 `confidence` 设为低于 1.0。
- **写明具体日期**（用“on <YYYY-MM-DD>”，而不是“昨天”），并将 `valid_at` 设为做出观察的时间。
- **不要写入密钥。** 对于用户不希望被记住的某一类内容，用 `blacklist.add` 添加匹配模式：匹配的写入会被拒绝，并返回 422 `BLACKLISTED`。

用户的一句陈述（“我们周二部署”）就是触发写入的时机。不要先搜索；重复和冲突由服务器处理。

<a id="4-read-the-writes-answer"></a>

## 4. 读取写入的返回结果

`memory.set` 的返回是一张回执。请这样解读：

| 字段                                      | 含义                                                                                                                       |
| ----------------------------------------- | -------------------------------------------------------------------------------------------------------------------------- |
| `memory_id`, `version`, `content_sha256`  | 写入已持久化。没有这些字段就说明没有写入成功；绝不要在没有保存成功时告诉用户已经保存。                                     |
| `conflict_verdict: conflict`, `peers`     | 本次写入与已有事实冲突（两行都会被标记为 `conflicted`）。请展示两个值并询问哪个是当前有效的，不要静默覆盖。               |
| 409 `PINNED_CONFLICT`                     | 有人固定了已有事实。请如实报告，不要换一个新的 key 重试。                                                                  |
| `duplicate_of`, `merged_into_key`, `noop` | 该事实已经存在，没有存储任何新内容。                                                                                       |
| `advisories`                              | 形态警告：base64、双重编码的 JSON、值超过 `LOOM_MEMORY_VALUE_ADVISORY_CHARS`。                                             |

当两个写入方可能对同一个 key 竞争写入时，请传入 `if_match_sha256`（来自 `memory.get`）；过期的哈希会被拒绝，而不是覆盖现有内容。

<a id="5-read-with-one-call-per-question"></a>

## 5. 每个问题只调用一次来读取

1. **每个任务只加载一次上下文。** `context.pack` 返回一个按 token 预算裁剪的数据包：profile 部分、工作记忆、与 `query` 相关的记忆、流程，还可以选择性包含知识、对话和最近的运行（`include_knowledge`、`include_conversations`、`include_runs`）。插件在会话开始时调用它。

   ```python
   pack = await agent.context.pack("fix the flaky ingest test", token_budget=2000)
   system_prompt = base_prompt + "\n\n" + pack.rendered
   ```

2. **每个问题只搜索一次。** 通过 MCP，`loom_search` 一次调用即可覆盖记忆、profile、知识和对话。在 SDK 中，使用 `agent.memory.search(query)`，或使用 `agent.memory.ask(question)` 获取带引用的回答。只有当回答需要更多行（列表、计数）时，才使用 `next_page` 翻页。
3. **把 abstain 当作一种回答。** `abstain: true` 表示没有任何结果通过相似度门限；应回复“记忆中没有”，而不是返回最接近但相关性弱的结果。`abstain_code` 指明是哪一道门限（[abstain 代码](https://github.com/ClickHouse/loom/blob/main/docs/features/memory/search-and-ask.md)）。当 `pending_extraction` 大于 0 时，延迟提取尚未运行，因此空结果并不能证明记忆中不存在相关内容。
4. **尊重来源信息。** 命中结果的 `source_class` 说明内容是如何进入记忆的，`verified` 说明是否有有效的证明覆盖它。`memory.get` 返回 `verification`（`human`、`process`、内容在证明之后发生变化时为 `stale`，或 `none`）。默认情况下，`memory.ask` 在综合回答时会排除声明为 `external` 且未经证明的行。

```python
hits = await agent.memory.search("refund window for EU customers", top_k=5)
if hits.abstain:
    reply = "I don't have that in memory."
else:
    reply = hits[0].content_excerpt
```

<a id="6-record-runs"></a>

## 6. 记录运行

run 是任务的一次执行：包括它的目标、它召回并依赖了哪些记忆、写入了什么以及最终结果。记录下来的 run 可以让排序学会哪些记忆有帮助（[运行](https://github.com/ClickHouse/loom/blob/main/docs/features/runs.md)）。

```python
async with agent.run_scope(goal="Fix the flaky CI job") as run:
    answer = await agent.memory.ask("what broke CI last week?")
    run.used("ci/flaky/runner-image")
    run.final_response = "Pinned the runner image."
```

该作用域在进入时调用 `run.begin`，退出时调用 `run.complete`，状态为 `completed` 或 `failed`，其中 `memories_used` 包含其 `memory.ask` 答案所引用的 key，以及传给 `used()` 的 key。块内的写入会带上它的 `run_id`。不使用 SDK 时，请自行调用 `run.begin` 和 `run.complete(run_id, status=..., memories_used=[...])`。`memories_used` 是 agent 实际依赖的记忆，而不是展示给它的全部记忆；召回的 key 会单独跟踪。

插件之外的 agent 还应将完成后的对话记录交给 `memory.set_from_messages`（或关闭 `agent.session(...)`），以便服务器提取其中持久有效的内容。

<a id="7-follow-the-memory-skills-review"></a>

## 7. 遵循记忆 skill 的复查流程

这些插件附带一个 `loom-memory` skill（`integrations/claude-code-plugin/skills/loom-memory/`，已生成到 Codex 插件中），它把本指南的内容编码给模型使用。通过普通 MCP 使用的编码 agent 采用 [agent 指令](../features/integrations/agent-instructions.md)块，该块的最后一条记忆规则是这一复查流程的简短形式；若要执行完整的复查，请把下面的问题粘贴到该块之后。其他 agent 可以把这些问题复制到自己的指令中。在报告完成之前，只写下回答为“是”的内容：

1. 我是否做出或了解到某个决策、约束或负责人，且后续会话需要它？
2. 是否有现有记忆被发现有误或已过时？请更新它，key 保持不变。
3. 用户是否纠正了我？请更新该事实；如果用户否定了它，就用 `memory.delete` 删除，并且在该模式可能再次出现时用 `blacklist.add` 加入该模式。
4. 我是否产出了后续工作应当了解的产物（文件、PR、控制台）？
5. 我是否发现了某些不显而易见、且花费了大量精力的内容？
6. 我之前写下的推断现在是否已得到确认？请提高 `confidence`，并去掉“Inferred”前缀。

然后运行 `namespace.validate`，并处理那些指向你所写入的 key 的发现。只有在回执中包含 `memory_id` 和 `version` 时才说“已保存”，只有在有人或某个流程证明过时才说“已验证”，在检查无法运行时说“此处未验证”。其他随附的 skill 涵盖代码问题（`loom-code`，也位于 `skills/loom-code/`，适用于任何客户端）、整理命名空间（`loom-optimize`）、会话恢复（`loom-session`）以及客户端健康检查（`loom-status`）。

<a id="mistakes-to-avoid"></a>

## 需要避免的错误

`namespace.audit` 可以检测出下列每一种问题；[优化命名空间](https://github.com/ClickHouse/loom/blob/main/docs/guides/optimize-namespace.md)会把检测结果转化为修复方案。

| 检测项                                | 错误做法                                                                      | 正确做法                                                                                        |
| ------------------------------------- | ----------------------------------------------------------------------------- | ----------------------------------------------------------------------------------------------- |
| `double_encoded_values`               | `json.dumps(json.dumps(doc))`：嵌入的文本以转义引号开头。                     | 将文本或对象本身作为 `value` 发送。                                                             |
| `json_prefix_excerpts`                | JSON 对象超过嵌入截断长度，向量只由其前缀生成。                               | 先以纯文本形式写入结论；文档使用 `knowledge.upsert`。                                           |
| `hash_keys`                           | 用 sha 作为行的标识：不可读、无法分组，且每次修订都会变成新的键。             | 使用 `<scope>/<kind>/<id>`；修订时沿用同一个键。                                                |
| `exact_key_store`                     | 按精确键读取的次数远多于搜索：该命名空间被当成了对象存储。                    | 使用 `memory.search` 召回；租约和指针放入工作记忆。                                             |
| `coordination_as_memory`              | 租约、锁、冻结标志被存成记忆，并在循环中读取。                                | 使用 `working_memory.set` / `get`：不会被嵌入、排序或做冲突检查。                               |
| `hand_rolled_index_rows`              | 用一行记录列出其他键，因为 agent 找不到自己的行。                             | 使用 `tags` + `memory.list prefix=`；用 `memory.link supersedes` 串联各次修订。                 |
| `oversized_values`, `unembedded_rows` | 把文档当作记忆存储；超过嵌入器的上限后，值不会被嵌入。                        | 使用 `knowledge.upsert`，再加一行单段落的指针行。                                               |
| `episodic_blobs`, `version_churn`     | 把运行转储存为情景行，并在每一步重写。                                        | 大对象使用 `context.offload`；每次运行只存一条简短的情景行；修订结论，而不是大对象。            |

还有两种审计无法发现的问题：

- **每轮只存一个大对象。** 五条事实应当是五次 `memory.set` 调用，各用自己的键。
- **淘汰仍有在线 agent 读取的键。** 它的读取会返回 `found: false`，客户端可能退回到本地状态，导致两个存储不一致。请使用 `memory.move` 迁移（它会留下重定向），并在所有读取方都迁移完之前不要删除任何内容。

<a id="related"></a>

## 相关内容

- [无头机器人](https://github.com/ClickHouse/loom/blob/main/docs/guides/headless-bots.md)
- [写入记忆](https://github.com/ClickHouse/loom/blob/main/docs/features/memory/writing.md)
- [统一搜索](https://github.com/ClickHouse/loom/blob/main/docs/features/unified-search.md)
- [框架](https://github.com/ClickHouse/loom/blob/main/docs/features/integrations/frameworks.md)
- [MCP 客户端](https://github.com/ClickHouse/loom/blob/main/docs/features/integrations/mcp-clients.md)
