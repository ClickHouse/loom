<!-- i18n: source=docs/use-cases/memory-linked-to-code.md blob=fab4f6c6a23848dde1feef736969b204d07adfd8 model=claude-sonnet-5-5 -->
> 本页由机器从[英文原文](https://github.com/ClickHouse/loom/blob/main/docs/use-cases/memory-linked-to-code.md)翻译，内容以英文原文为准。
> Machine-translated from the [English page](https://github.com/ClickHouse/loom/blob/main/docs/use-cases/memory-linked-to-code.md), which is authoritative.

<a id="memory-linked-to-code-for-coding-agents"></a>

# 与代码关联的记忆，面向 coding agent

Loom 会把 coding agent 的记忆关联到其中提到的函数、类和文件，在重构后继续跟踪这些关联，并在记忆对应的代码发生变化时为其打上标记。默认情况下，如果一条记忆对应的代码已全部消失，搜索会将其排除，除非有人固定、锁定或验证过它；每次返回经验教训的读取，都会说明其代码何时发生变化或已经消失。该功能通过 MCP、HTTP 和各 SDK 运行，作用于自托管 Loom 所索引的仓库（即[代码索引](code-intelligence.md)，需设置 `LOOM_CODE_INGEST_ENABLED=true`），而维护这些关联的 `memory_code_links` worker 默认开启。

<a id="what-you-get"></a>

## 你能得到什么

- **经验教训附着在其所涉及的代码上。** 带有 `repo:<id>` 标签的记忆（Claude Code 和 Codex 插件会为其 hook 捕获的内容打上此标签），或写入仓库代码命名空间的记忆，会关联到以下对象：文本中用反引号标出的每个定义（函数、类或其他符号），或在文本任意位置以 `A::b` 或调用形式（`a.b()`、`name()`）出现、且恰好能解析到唯一位置的定义（不带限定的名称需要四个或更多字符）；记忆中列出的文件里、以纯 camelCase 或 snake_case 单词提到的定义；以及记忆中列出的、不含上述任何定义的每个文件。尚未进入主干的限定名称，如果在 `LOOM_MEMORY_CODE_LINKS_PENDING_DAYS`（14 天）内合并，则会建立关联。
  [与代码关联的记忆](https://github.com/ClickHouse/loom/blob/main/docs/concepts/memory.md#memories-linked-to-code)
- **关联随重构而变化。** 每次索引提交时，每条关联的状态为 `fresh`、`moved`、`changed`（并指明修改它的提交）、`unresolved` 或 `missing`。关联记录的是定义的 token，因此空白、注释和大多数格式化工具造成的改动不会改变其 `fresh` 状态。[关联状态](https://github.com/ClickHouse/loom/blob/main/docs/concepts/memory.md#memories-linked-to-code)
- **按你选择的方式处理过时的记忆。** 由命名空间的 [`code_change_policy`](https://github.com/ClickHouse/loom/blob/main/docs/reference/operations/namespace.md#namespaceregister) 决定：`retire_removed`（默认）会让代码已全部消失的记忆不再出现在搜索中，并标记已变化的代码；`flag` 只做标记；`revise` 同样将代码已消失的记忆排除在外，并让模型对照新旧代码检查已变化记忆的论断：仍然成立的会被重新锚定，被推翻的会得到一条修正建议。被人固定、锁定或验证过的记忆永远不会被排除。无论采用哪种策略，搜索通常会把代码已变化的记忆排在后续三条结果之后，而不是将其隐藏；被保留的记忆以及按名称找到的行则保持原有位置。Loom 从不改写记忆本身。[策略](https://github.com/ClickHouse/loom/blob/main/docs/concepts/memory.md#memories-linked-to-code)
- **agent 读取代码的任何地方都有经验教训。** `code.symbol` 会把关联到某个定义的记忆作为 `lessons` 返回；`code.search`、`code.callers`、`code.callees`、`code.impact`、`code.outline` 以及任务的 `code.context_pack` 会为其返回的文件附上相应的经验教训；每条经验教训都带有其关联状态。Claude Code 和 Codex 的 hook 会在 agent 编辑文件之前展示该文件的记忆（`memory.for_file`），Cline 的 hook 则在其读取或编辑文件时展示。[`code.symbol`](https://github.com/ClickHouse/loom/blob/main/docs/reference/operations/code.md#codesymbol) ·
  [Claude Code 插件](https://github.com/ClickHouse/loom/blob/main/docs/features/integrations/claude-code.md) ·
  [Codex 插件](https://github.com/ClickHouse/loom/blob/main/docs/features/integrations/codex.md) ·
  [Cline hook](https://github.com/ClickHouse/loom/blob/main/integrations/cline/README.md)（实验性）
- **与团队共享。** 经验教训会从仓库的代码命名空间以及调用中指明的命名空间读取（通过 MCP 时，默认读取调用者自己的命名空间），因此保存在仓库代码命名空间中的经验教训，会送达每个有权读取该命名空间的 agent（限定为其所属用户或会话的令牌读不到任何经验教训）。
  [与代码关联的记忆](https://github.com/ClickHouse/loom/blob/main/docs/concepts/memory.md#memories-linked-to-code)
- **分支读取各自的代码。** 在其他分支上，经验教训会对照该分支自身的定义进行检查，因此仍保留旧文本的发布分支会显示为 `fresh`（在设置了 `LOOM_GIT_HISTORY=none`、没有 git 镜像的主机上，经验教训携带的是主干的状态）。[与代码关联的记忆](https://github.com/ClickHouse/loom/blob/main/docs/concepts/memory.md#memories-linked-to-code)
- **文档与代码并列。** 来自注册在仓库代码命名空间（即其主干）上的来源的文档会带上该仓库的标签，因此针对该仓库的搜索会将这些文档与其代码一并读取（同步 worker 需要 `LOOM_KNOWLEDGE_SOURCE_SYNC_ENABLED`）。
  [文档来源](https://github.com/ClickHouse/loom/blob/main/docs/features/knowledge.md#document-sources)
- **一份审查清单。**
  [`namespace.validate`](https://github.com/ClickHouse/loom/blob/main/docs/reference/operations/namespace.md#namespacevalidate) 会列出代码已变化、无法再次找到或已经消失的记忆，控制台则显示每条记忆的关联及其状态。

![控制台中一条记忆的详情：其关联的代码，其中一个定义是最新的，另一个自某次提交后已发生变化](https://github.com/ClickHouse/loom/raw/main/docs/assets/screenshots/memory-linked-code.webp)

<a id="start"></a>

## 开始

[快速入门](../getting-started/quickstart.md)会启动 Loom，而[添加仓库](https://github.com/ClickHouse/loom/blob/main/docs/features/code-index/repositories.md)会为某个仓库开启代码索引。通过插件或 MCP 连接你的 coding agent（[连接 coding agent](https://github.com/ClickHouse/loom/blob/main/docs/getting-started/connect-a-coding-agent.md)）。保存一条经验教训，在其中用反引号或调用形式（`` `name()` ``）提到一个不少于四个字符的定义，或提到一个带限定的定义，例如 `` `Reader.run` ``。Claude Code 和 Codex 插件会为其 hook 捕获的内容打上 `repo:<id>` 标签，会话引导信息中会写明该 id（[如何得出](https://github.com/ClickHouse/loom/blob/main/docs/features/integrations/claude-code.md#configuration)）。agent 自己用 `loom_memory_set` 保存的经验教训，在带有该标签（`tags: ["repo:<id>"]`）或写入仓库代码命名空间时会建立关联；任何客户端都可以采用其中一种方式。通过 HTTP（或 Python SDK 中的 `agent.call`）时，保存在你自己命名空间中的经验教训，只有在调用的 `lesson_namespaces` 中指明该命名空间时才会被读取。每次关联处理会处理至少已写入 60 秒的内容，且每 60 秒运行一次，因此主干被索引之后，对该定义调用 `code.symbol` 大约两分钟内就会在 `lessons` 下返回这条经验教训。

本页中的部分 op 在[状态页](https://github.com/ClickHouse/loom/blob/main/docs/reference/status.md)上被标记为实验性。
