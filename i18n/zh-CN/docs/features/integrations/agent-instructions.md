<!-- i18n: source=docs/features/integrations/agent-instructions.md blob=66910fd5b55982ca9d0f8f554d606e94b2c0f60c model=claude-sonnet-5-5 -->
> 本页由机器从[英文原文](https://github.com/ClickHouse/loom/blob/main/docs/features/integrations/agent-instructions.md)翻译，内容以英文原文为准。
> Machine-translated from the [English page](https://github.com/ClickHouse/loom/blob/main/docs/features/integrations/agent-instructions.md), which is authoritative.

<a id="agent-instructions"></a>

# Agent 指令

在 agent 的指令文件中放入一小段文字，告诉它何时加载 Loom 的上下文、何时保存事实，以及何时使用代码索引，而不是用 grep 搜索或直接读取文件。本页只给出一次这段文字，以及各客户端读取它的文件；将其粘贴进去，按 `code.repos`（见下文）填写其中的两个命名空间，然后随代码仓库一起提交。

Claude Code 和 Codex 插件已经通过各自的 hook 和 skill 完成了这些工作（[Claude Code](https://github.com/ClickHouse/loom/blob/main/docs/features/integrations/claude-code.md#skills)、[Codex](https://github.com/ClickHouse/loom/blob/main/docs/features/integrations/codex.md#skills)）；这段文字适用于其他所有客户端，也适用于未使用插件而连接的 Claude Code 或 Codex。它引用的是 `coding` 工具集列出的工具，因此请通过 `?toolset=coding` 连接客户端：对其支持的客户端，使用 `loom install --client <name> --toolset coding`，否则在 URL 上加 `?toolset=coding`（[MCP 客户端](https://github.com/ClickHouse/loom/blob/main/docs/features/integrations/mcp-clients.md)）。

<a id="the-block"></a>

## 代码块

使用 [Cline 钩子](https://github.com/ClickHouse/loom/blob/main/integrations/cline/README.md)时，请在你的副本中，将该块里以 "Every task:" 开头的段落替换为 "Every task: make these two calls."，这样两次调用都仍然是必须的。Cline 只会在 hook 所针对的工具运行之后，才把 hook 的文本展示给模型，因此入门提示会在任务的第一次工具调用之后才到达，而某次编辑对应的文件块则在该编辑之后到达。

```markdown
## Loom: memory and code index

Loom is connected as an MCP server; its tools are named `loom_*`. A tool you do not see
listed runs through `loom_op` with the op's dotted name: on a connection without the
coding toolset, `loom_code_symbol` is `code.symbol` and `loom_working_memory_set` is
`working_memory.set`. `loom_op` with `{"op": "help", "query": "<words>"}` finds them.

Every task: make these two calls unless a Loom hook already put Loom's context in this
session (the Claude Code and Codex plugins do). Without one they are not optional.

- At the start of each task, before planning: `loom_context_pack` with the task as
  `query`. It returns the decisions, constraints and memories that bear on it.
- Before editing a file: `loom_op` with `op` set to `memory.for_file` and `arguments`
  holding `path` (the file's repo-relative path) and `extra_namespaces`
  (`["<team-namespace>"]`). Follow every constraint and decision it returns.

Memory

- To recall something mid-task, call `loom_search` with the topic, file or error. Treat
  `abstain` as "nothing relevant". A hit whose `code_state` is `changed`, `unresolved`
  or `missing` is about code that changed or went away since it was written: check the
  code before relying on it.
- When the user states a decision, preference, constraint or correction, save it with
  `loom_memory_set` right away: one plain-text fact per call under a readable key
  (`<scope>/<kind>/<id>`), dates as calendar dates. Never save a guess, your reasoning or
  the conversation.
- A rule the user states for the whole team about a file: `loom_memory_set` with
  `namespace` `<team-namespace>`, the file in `files` and the tag `constraint`.
- A fact that changed: `loom_memory_update` on the same key, in the namespace the row
  lives in, never a second copy. A team rule or decision someone else wrote is not yours
  to update: follow the refusal's `hint`.
- Your plan and checklist for this task: `loom_working_memory_set`, not `loom_memory_set`.
- Before reporting done, one pass over what should outlive the session.

Code

- This repository's code namespace is `<code-namespace>`; pass it as `namespace` on
  every `loom_code_*` call.
- Where X is defined: `loom_code_symbol`. Who calls X: `loom_code_callers`. A concept
  with no exact keyword: `loom_code_search`. An exact string: `loom_code_grep`. What is
  in a file: `loom_code_outline`, then read one span. What a change breaks:
  `loom_code_impact`.
- Grep your own uncommitted files and repositories the index does not cover. When a code
  tool answers `abstain: true`, grep instead of retrying with synonyms.
- A code answer's `lessons` are memories about its top files: follow them, and check the
  code of one whose `code_state` is `changed`, `unresolved` or `missing` before relying on
  it.
```

`code.repos` 会列出该令牌可以读取或可以申请加入的仓库，每个已索引的分支一行：

```bash
curl -s -X POST https://loom.example.com/v1/code.repos \
  -H "Authorization: Bearer $LOOM_API_KEY" -H 'Content-Type: application/json' \
  -d '{"repo": "<owner>/<name>", "fields": ["branch", "primary_namespace", "readable"]}'
```

将 `<code-namespace>` 替换为该文件副本所提交的分支所对应那一行的 `namespace`，将 `<team-namespace>` 替换为该行的 `primary_namespace`（即主干分支的命名空间，团队的文件规则和决策都存放在那里）；对于主干分支，这两者相同。如果某个仓库没有对应的行（未被索引，或 `code.repos` 返回代码智能已关闭），或者其行显示 `readable: false`（该令牌无法读取的仓库，列出是为了让你申请访问或接管它），则使用不含 `extra_namespaces`、团队规则那一行和 Code 部分的代码块。

<a id="where-each-client-reads-it"></a>

## 各客户端读取的位置

各处的区块内容相同（上文 Cline hooks 的一段改动除外），只有文件和文件头不同。这些客户端大多也会读取 `AGENTS.md`，因此在一个文件可以供多个客户端使用的情况下，只保留一份即可；如果某个客户端读取了两个带有该区块的文件，它会看到两次，这会多占用上下文，但不会改变行为。

| 客户端                                                         | 文件                                                                   | 区块前的文件头                                                               |
| -------------------------------------------------------------- | ---------------------------------------------------------------------- | ---------------------------------------------------------------------------- |
| 未使用插件的 Codex，以及任何读取 `AGENTS.md` 的 agent          | 仓库根目录下的 `AGENTS.md`                                             | 无                                                                           |
| Zed                                                            | 它找到的第一个指令文件（见下文）                                       | 无                                                                           |
| 未使用插件的 Claude Code                                       | 仓库根目录下的 `CLAUDE.md`（见下文）                                   | 无                                                                           |
| Cursor                                                         | `.cursor/rules/loom.mdc`                                               | `---`、`description: Loom memory and code index`、`alwaysApply: true`、`---` |
| Devin Desktop（Windsurf）                                      | `.devin/rules/loom.md`（Windsurf 构建版本中为 `.windsurf/rules/loom.md`） | `---`、`trigger: always_on`、`---`                                           |
| Cline                                                          | `.clinerules/loom.md`                                                  | 无                                                                           |
| VS Code (GitHub Copilot)                                       | `.github/copilot-instructions.md`                                      | 无                                                                           |

Zed 只读取它找到的第一个指令文件（在 `AGENTS.md` 之前还有 `.rules`、`.cursorrules` 等），所以应把区块放在那个文件中；如果仓库里没有这类文件，则放在 `AGENTS.md` 中。

Claude Code 不读取 `AGENTS.md`：如果仓库中已有一个，请把区块放在 `AGENTS.md` 中，并在 `CLAUDE.md` 中用 `@AGENTS.md` 这一行导入它。

每一行文件头都单独占一行。例如 Cursor 文件的开头如下：

```markdown
---
description: Loom memory and code index
alwaysApply: true
---

## Loom: memory and code index
```

已有的 `AGENTS.md`、`CLAUDE.md` 或 Copilot 文件应保留原有内容：把该区块作为其中的一个章节添加进去。

<a id="why-these-rules"></a>

## 为什么是这些规则

在没有 hook 的会话中，没有任何机制会触发召回，而列出的记忆工具只是众多选项之一，因此这个规则块把加载上下文变成每个任务的固定步骤，就像插件的 hook 所做的那样。

每一行都是插件所教内容的简短形式，或是 Loom 的返回结果所携带内容的简短形式：

- 在任务开始时和编辑之前加载上下文：对应插件的会话开始 hook 和文件上下文 hook，它们会调用 `context.pack` 和 `memory.for_file`；
- 随着事实的陈述，召回并保存这些事实：对应 [`loom-memory`](https://github.com/ClickHouse/loom/blob/main/integrations/claude-code-plugin/skills/loom-memory/SKILL.md) skill；
- 先用代码索引，再用 grep；索引无法回答时才用 grep：对应 [`loom-code`](https://github.com/ClickHouse/loom/blob/main/integrations/claude-code-plugin/skills/loom-code/SKILL.md) skill，插件的代码分流（code shunt）会强制执行这一点；
- 阅读代码查询结果所携带的记忆：即其 `lessons`（[与代码关联的记忆](https://github.com/ClickHouse/loom/blob/main/docs/concepts/memory.md#memories-linked-to-code)），插件会话在同样的返回结果中也会得到这些内容。

规则块的最后一条记忆规则代表任务结束时的回顾；若要把它展开说明，请将 [agent 集成指南](https://github.com/ClickHouse/loom/blob/main/docs/guides/agent-integration.md#7-follow-the-memory-skills-review)中的问题复制到规则块下方。skill 中包含规则块省略的细节：回执字段、文档与记忆的区别、停止规则。可以把 `loom` 和 `loom-code` skill 与规则块一起安装，以获得这些细节（[编码 agent](https://github.com/ClickHouse/loom/blob/main/docs/features/integrations/index.md#coding-agents)）；`loom` 涵盖记忆循环、写入形式、回执以及任务结束时的回顾。它们没有 hook，因此不能替代规则块中的两个调用。

<a id="related"></a>

## 相关内容

- [MCP 客户端](https://github.com/ClickHouse/loom/blob/main/docs/features/integrations/mcp-clients.md)：连接各个客户端。
- [选择工具集](https://github.com/ClickHouse/loom/blob/main/docs/features/integrations/mcp-clients.md#choosing-a-toolset)。
- [集成](https://github.com/ClickHouse/loom/blob/main/docs/features/integrations/index.md)。
