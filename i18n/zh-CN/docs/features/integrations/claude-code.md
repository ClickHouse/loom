<!-- i18n: source=docs/features/integrations/claude-code.md blob=390e8502444f4ea810aeb2237b84293184461fd8 model=claude-sonnet-5-5 -->
> 本页由机器从[英文原文](https://github.com/ClickHouse/loom/blob/main/docs/features/integrations/claude-code.md)翻译，内容以英文原文为准。
> Machine-translated from the [English page](https://github.com/ClickHouse/loom/blob/main/docs/features/integrations/claude-code.md), which is authoritative.

<a id="claude-code-plugin"></a>

# Claude Code 插件

Claude Code 的 `loom` 插件只需一次安装即可完成三件事：

- **通过 MCP 挂载 Loom 工具**：记忆、工作记忆、`context.pack`、代码索引、重复检查。
- **通过生命周期钩子捕获会话**：工具调用、对话轮次、运行记录、工作记忆笔记、会话简报以及会话中的事实。这些钩子不会调用模型，agent 也无需记住要做任何事。
- **在已建立 Loom 索引的仓库中，引导 agent 使用代码索引**。整文件读取和全树 grep 会被重定向到 `code.*` 操作；编辑文件前会提供该文件的规则，编辑后会提供其影响范围。

源码位于 `integrations/claude-code-plugin/`，是不含任何依赖的 Node ESM 脚本。Codex 版本运行相同的脚本，参见 [codex.md](https://github.com/ClickHouse/loom/blob/main/docs/features/integrations/codex.md)。

<a id="data-handling"></a>

## 数据处理

本节涵盖 Claude Code 插件和 Codex 插件，二者运行相同的脚本；Codex 接入除 token 用量 hook 之外的所有 hook。这些 hook 和插件的脚本只会把数据发送到插件所配置的 Loom 服务器；本节末尾提到的两个 skill 除外。插件会发送：

- agent 传给 Loom MCP 工具的所有内容：它保存的记忆、搜索查询和代码查询；
- 仓库的身份信息。每个 git remote 的所有者和名称会发送到 `code.repos`，以查找其代码索引。匹配到的 remote 的所有者和名称会随每条 observation、运行记录和会话简报一起发送；分支和 `HEAD` 提交随每份简报发送，绝对工作目录随运行记录发送。observation 和会话记忆会带有检出目录的名称和仓库的哈希值，observation 还会带有工作子目录；
- 提示词，用于为其获取召回的记忆和代码指针；
- 每次工具调用的输入、输出或错误（`observe.record`），以及读取或编辑的文件路径，用于代码分流、文件规则和影响范围分析；从被重定向的 grep 模式中取出的符号名称（`code.symbol`）；以及由 tracker 分流回答的 `gh` issue 搜索的搜索查询（`tracker.duplicate_check`），以及命令中所指仓库的所有者和名称（`code.repos`）；
- 对于已索引仓库中被代码分流重定向或放行的每次批量文件读取或 grep，发送工具名称、文件路径和行范围，以及对其避免进入或放入上下文的 token 数量的估计（`code.outcome`；`LOOM_SHUNT_REPORT=off` 可停止发送）；
- 聊天轮次（`conversation.append`），仅当命名空间开启了对话日志时，服务器才会存储；
- 会话的最后若干轮（最多 200 轮），发送到 `memory.set_from_messages`，无论日志设置如何，它都会把这些内容作为所提取事实的来源证据保留，同时还有会话中最多 80,000 个字符的 Loom 工具参数和结果（`LOOM_SESSION_MEMORY=off` 可停止发送）；
- 一条运行记录（以第一条提示词作为目标，加上最后一条回复）和一条工作记忆笔记（目标、git 状态、已编辑的文件、最后一条回复）；
- 每次压缩时和会话结束时的会话简报：客户端对整个上下文窗口的压缩摘要（最多 15,000 个字符），以及会话事件的日志，其中包括每次提问和回复、命令、读取或编辑的文件、搜索模式、网址或查询、子 agent 提示词、错误、待办列表、其他工具的名称，以及拉取请求和提交的标识符。当命名空间关闭对话日志时，服务器不会存储其中任何内容，无论是摘要还是日志（`LOOM_SESSION_BRIEF=off` 可停止发送）；
- token 用量和模型名称（仅 Claude Code）；
- 在已索引的检出中，每次编辑之后，发送该文件相对于 `HEAD` 的未提交差异的概要，到 `code.impact`（`LOOM_BLAST_RADIUS=off` 可停止发送）；
- 在已索引的检出中，于 Stop 时发送会话所更改文件的差异概要，或者在客户端未提供转录时发送整个未提交工作树的差异概要，到 `code.test_gate`（`LOOM_TEST_GATE=off` 可停止发送）。差异概要最多 200,000 个字符，内容为文件路径、各 hunk 的行范围，以及在代码文件中，每个 hunk 头部上下文里第一个 `(` 之前的最后一个标识符，通常是所在函数的名称。Git 从旧文件中 hunk 上方的某一行获取该上下文，同一差异中较早的 hunk 可能已更改了这一行；被更改行的其他任何部分都不会发送；
- 在没有凭据时，于第一次会话发送你的操作系统用户名和主机名，以请求自助注册令牌（除非服务器允许自助注册，否则会被拒绝；`LOOM_AUTO_SIGNUP=off` 可停止发送）；
- 在 agent 模式下，把工作负载的身份令牌和 `LOOM_CLIENT_ID` 发送到服务器的 `/oauth/token`，以换取 Loom 令牌。

[hook 的作用](#what-the-hooks-do)列出了这些请求背后的 hook。客户端的脱敏处理会从 hook 发送的提示词、工具调用、轮次、运行记录、笔记和简报中移除常见的凭据形态。这只是尽力而为，不作保证。MCP 工具参数和由 `loom-session` skill 保存的简报在发送时不经过脱敏。

服务器把这些数据保存在自己的数据库中（ClickHouse，以及运营方把记忆存放在 Postgres 时的 Postgres）。只有在运营方配置了 LLM 或 embedding 提供方时，它才会把文本传给对应提供方（[模型与密钥](https://github.com/ClickHouse/loom/blob/main/docs/getting-started/models-and-keys.md)）。删除数据的方法见[保留与删除](https://github.com/ClickHouse/loom/blob/main/docs/features/memory/retention-and-erasure.md)。授权允许读取某条已存储行的用户，以及服务器的运营方，都可以读取它。[命名空间与共享](https://github.com/ClickHouse/loom/blob/main/docs/features/namespaces-and-sharing.md)介绍了访问控制。

插件保存的令牌（安装令牌的副本或自助注册令牌，位于 `~/.loom/config.json`；在 agent 模式下，换取到的令牌位于 `~/.loom/agent-token-<hash of LOOM_CLIENT_ID>.json`）及其其他状态保存在 `~/.loom/` 中，权限为 0600，直到你将其删除。

它在系统临时目录中的文件可能包含提示词和回复文本，权限同样为 0600。投递进程在发送之前就已停止的排队写入会一直保留，直到你重放或删除它；`loom-status` 会统计这些写入并给出该目录的名称。被服务器拒绝或无法到达服务器的写入会被丢弃。上述按文件读取和 grep 记录的批次若发送失败，也会被丢弃。失败的聊天轮次会在下一次回复结束时重新发送，简报的事件则在下一次保存简报时重新发送。会话开始时，插件会尝试删除其他一天内未被改动的状态文件。它们可能保留更久，尤其是在没有新会话启动时。记录插件重定向或放行了哪些工具调用的日志（不含提示词或文件内容）会一直保留，直到你将其删除（`LOOM_SHUNT_LOG=off` 可停止记录）。

有两个 skill 会访问其他服务，且只在你要求时才会这样做。`loom-status` skill 可以通过 apt、dnf 或 yum 从 NodeSource 安装 Node.js，或通过 apk 或 Homebrew 安装，并且只有在你同意之后才会运行该安装程序。`improve-loom-for-my-repo` skill 会针对它报告的每一项发现，在 ClickHouse/loom 的 GitHub issue 中搜索，并在你批准之后，把其报告提交到那里，或者作为评论添加到已报告该问题的 issue 上。该 issue 是公开的，其中的报告与展示给你的内容一致：服务器版本、命名空间、已索引的提交、仓库被索引的比例，以及每项发现的复现步骤，包括其查询、符号和文件名。

<a id="requirements"></a>

## 要求

- 一个可访问的 Loom 服务器（[快速入门](../../getting-started/quickstart.md)）。
- Claude Code 2.1.139 或更新版本，这是第一个不经过 shell 运行 hook 的 `args` 的版本。更旧的版本会丢弃这些参数，并把 hook 的 payload 当作程序交给 `node` 运行：每个 hook 都会因 `SyntaxError` 而失败。
- Node.js 18 或更新版本，且 `node` 位于 Claude Code 启动时使用的 `PATH` 中。每个 hook 都是以脚本路径为参数的 `node`，由 Claude Code 不经过 shell 直接启动，MCP header 辅助程序是 `node "${CLAUDE_PLUGIN_ROOT}/bin/loom-mcp-headers.mjs"`。这个 `PATH` 是 Claude Code 自己的：从终端启动时就是该 shell 的搜索路径，但在 macOS 上从 Dock 或桌面启动器启动时只有 `/usr/bin:/bin:/usr/sbin:/sbin`，其中没有通过 Homebrew、nodejs.org 安装程序或版本管理器安装的 Node。此时请在 `~/.claude/settings.json` 的 `env` 块中把 `PATH` 设为完整的搜索路径，并把该 Node 所在目录放在最前面，例如 `"PATH": "/opt/homebrew/bin:/usr/bin:/bin:/usr/sbin:/sbin"`。`env` 块对每个 Claude Code 会话都生效，包括终端中的会话，因此需要写完整的路径，而不只是 Node 所在的目录。如果 hook 提示找不到 `node`，请运行 `loom-status` skill；它以只读方式进行诊断，并提议安装 Node，只有在你确认后才会运行安装程序。
- 支持的平台：macOS（Apple silicon、Intel）、Linux（x86_64、arm64；glibc 或 musl；包括 WSL 2）和 Windows（x86_64、arm64），有无 Git Bash 均可：hook 不需要 shell，header 辅助程序在 macOS 和 Linux 上通过 `sh` 运行，在 Windows 上通过 `cmd.exe` 运行。找不到 `node` 的 hook 会让会话继续运行；Claude Code 会将其显示为 hook 错误。没有 Git Bash 时，通过 Claude Code 的 PowerShell 工具运行的命令会被捕获，测试门控会读取其中涉及的路径，但代码分流只判断 `sh` 命令。
- 会话所写入的命名空间的 **writer** 令牌。viewer 令牌可以让工具读取，但服务器会拒绝每一次捕获。

<a id="install"></a>

## 安装

安装步骤见[连接编码 agent](https://github.com/ClickHouse/loom/blob/main/docs/getting-started/connect-a-coding-agent.md)。简而言之：

```bash
claude plugin marketplace add ClickHouse/loom        # or a path to a local clone
claude plugin install loom@loom \
  --config server_url=https://loom.example.com \
  --config token=<writer JWT>
```

请只安装此插件或 `loom-code-skill` 其中之一，不要同时安装；插件已自带 `loom-code` skill。使用 `claude plugin update loom@loom` 更新：hook 随插件发布，而不是随服务器发布。

| 选项          | 默认值                                         | 含义                                                                                  |
| ------------- | ---------------------------------------------- | ------------------------------------------------------------------------------------- |
| `server_url`  | `$LOOM_BASE_URL`，否则为 `http://localhost:7777` | Loom 基础 URL，末尾不带斜杠                                                           |
| `token`       | 空                                             | writer JWT，作为敏感值存储                                                            |
| `toolset`     | `coding`                                       | MCP 列表（`?toolset=`），例如 `coding+tracker`；所有 op 仍可按名称调用               |
| `local_first` | `false`                                        | `true` 会停用 code shunt（等同于 `LOOM_CODE_LOCAL_FIRST=on`）                         |

所有选项均为可选；Claude Code 只会询问一次，若回答为空，则回退到[下文的查找方式](#configuration)。

- **安装令牌。** MCP header helper 无法读取安装选项，因此会话的第一个 hook 会把令牌和 URL 复制到 `~/.loom/config.json`（0600）。仅当不存在其他令牌（`LOOM_API_KEY`、`config.json`、`~/.loom/token`）时才会这样做。
- **完全没有凭据。** 在设置了 `LOOM_SELF_SIGNUP=open` 的服务器上，第一次会话会为这台机器注册：令牌写入 `~/.loom/config.json`，认领码写入 `~/.loom/claim-<namespace>.txt`，primer 会说明如何认领该命名空间（只需一次，在 `/oauth/agent-claim` 页面，使用你自己的密码）。设置 `LOOM_AUTO_SIGNUP=off` 可禁用此行为。否则，primer 会说明如何获取令牌。
- **只保留一个 MCP 条目。** 不要再额外添加用户级的 `loom` 条目：否则每个工具都会被列出两次，而且那一份不带 code 作用域。

<a id="configuration"></a>

## 配置

**hook 如何找到服务器和身份信息。** 优先级依次为：环境变量、安装选项、`~/.loom/config.json`、令牌中的声明（claims）、默认值（`lib/loom-client.mjs`：`resolveServerUrl`、`resolveToken`、`resolveScope`）：

| 项目      | 查找顺序                                                                                                 |
| --------- | -------------------------------------------------------------------------------------------------------- |
| 服务器    | `LOOM_BASE_URL` → `server_url` 选项 → `config.json` 的 `url` → `http://localhost:7777`                   |
| 令牌      | `LOOM_API_KEY` → `token` 选项 → `config.json` 的 `token` → `~/.loom/token`（可通过 `LOOM_API_KEY_FILE` 更改位置） |
| org       | `LOOM_ORG` → `config.json` 的 `org` → 令牌的 `org` 声明 → `default`                                      |
| 命名空间  | `LOOM_NAMESPACE` → `config.json` 的 `namespace` → 令牌中第一个非通配符命名空间 → `coding`                |
| agent     | `LOOM_AGENT` → `config.json` 的 `agent` → `claude-code`                                                  |

对于远程服务器，hook 和 header helper 只需要一个文件（可通过 `LOOM_CONFIG_FILE` 更改位置）：

```json
{ "url": "https://loom.example.com", "token": "<writer JWT>", "namespace": "my-team" }
```

MCP 条目不会读取该文件：它的 URL 来自安装选项 `server_url`，其默认值为 `LOOM_BASE_URL`，否则为 localhost。也需要设置其中之一，否则该条目会把文件中的令牌发送到 localhost，primer 中会给出提示。

**项目范围。** 项目名取自 git 顶层目录的目录名（可用 `LOOM_PROJECT_NAME` 覆盖）。在默认的 `LOOM_PROJECT_SCOPE=tag` 下，所有项目都写入同一个命名空间，每一行带有一个 `project:<name>` 标签。设置 `LOOM_PROJECT_SCOPE=namespace` 后，则改为以项目名作为命名空间。每一行还带有 `repo:<id>`，它是规范化后的 `origin` URL 的哈希值（可用 `LOOM_REPO_REMOTE` 覆盖远程地址），因此目录名相同但属于不同仓库的两个克隆不会共享记忆。

<a id="the-mcp-entry-and-its-header-helper"></a>

## MCP 条目及其请求头辅助脚本

插件的 `.mcp.json` 指向 `<server_url>/mcp?toolset=<toolset>`，并通过 `bin/loom-mcp-headers.mjs` 获取请求头。Claude Code 在插件根目录中运行该辅助脚本，因此 URL 以 `#${CLAUDE_PROJECT_DIR}` 结尾：Claude Code 会把填入了项目目录的 URL 交给辅助脚本，而且不会把片段发送给服务器。辅助脚本会发送：

- `Authorization: Bearer <token>`，查找方式与钩子相同，只是不包括安装选项（见上文）。如果没有令牌，辅助脚本会执行与 SessionStart 钩子相同的首次会话注册，因为 Claude Code 连接 MCP 条目时，该钩子仍在运行。
- `X-Loom-Code-Scope: <org>/<namespace>`，即项目检出目录所对应的代码命名空间。服务器会把它应用到每个未指定命名空间的 `code.*` 调用上，因此模型无需再传入命名空间。`LOOM_MCP_CODE_SCOPE=off` 可将其禁用。

检出目录所属的仓库通过 `code.repos`，根据其远程仓库（先 `origin`，再 `upstream`，然后是其余的）来确定。如果某个 GitHub 复刻（fork）没有自己的索引，且你有权读取其父仓库的索引，则会解析为父仓库的索引。`LOOM_CODE_REPO=<owner>/<name>` 或仅仓库名（也可以是 `code_repo` 选项，或 `~/.loom/config.json` 中的 `code_repo`）是在远程仓库之后的备用候选：它用于不在任何 git 检出目录中的会话，或没有索引的检出目录，并为其提供该仓库的索引和作用域请求头。辅助脚本自身的查找有 2.5 s 的时间限制；未命中只会损失这个请求头。
服务器如何使用该作用域，请参阅[代码索引概览](https://github.com/ClickHouse/loom/blob/main/docs/features/code-index/overview.md)和 [MCP 参考](https://github.com/ClickHouse/loom/blob/main/docs/reference/mcp.md)。

<a id="what-the-hooks-do"></a>

## hooks 的作用

接线配置在 `hooks/hooks.json`；每个 hook 的说明见 `hooks/HOOKS.md`。

| 事件                                              | 脚本                        | 作用                                                                                                                                                                                                                                                                                                                            | 关闭开关                                                                                                    |
| ------------------------------------------------- | --------------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- | ----------------------------------------------------------------------------------------------------------- |
| SessionStart                                      | `loom-session-start.mjs`    | 一份带预算的 `context.pack` 引导内容（工作记忆笔记、置顶项、画像、记忆、决策、流程、最近的运行），已建索引的检出目录的代码地图，一行列出该项目已保存的会话简报，一行任务收件箱信息（`task.counts`），以及几行列出该令牌可读取的集合（`collection.list`） | `LOOM_INJECT_CONTEXT=false`, `LOOM_CODE_MAP=false`, `LOOM_TASK_INBOX_LINE=off`, `LOOM_COLLECTIONS_LINE=off` |
| UserPromptSubmit                                  | `loom-prompt-pack.mjs`      | 针对该提示词最多给出三条 `code.search` 指引，仅在索引不放弃作答时才给出                                                                                                                                                                                                                                                         | `LOOM_PROMPT_PACK=off`                                                                                      |
| UserPromptSubmit                                  | `loom-prompt-recall.mjs`    | 针对该提示词做一次有上限的 `context.pack` 召回（记忆、决策、流程），仅在记忆不放弃作答时才注入                                                                                                                                                                                                                                  | `LOOM_PROMPT_RECALL=first`（默认）/ `on` / `off`                                                            |
| UserPromptSubmit                                  | `loom-run.mjs`              | 开启本次会话的运行（`run.begin`，`run_id = cc-<session_id>`，目标为第一条提示词）                                                                                                                                                                                                                                               | `LOOM_RUN_LEDGER=off`                                                                                       |
| PreToolUse（Read、Grep、Bash）                    | `loom-code-shunt.mjs`       | 代码分流和跟踪器分流（[下一节](#the-code-shunt)）                                                                                                                                                                                                                                                                               | `LOOM_CODE_SHUNT=off`, `LOOM_TRACKER_SHUNT=off`                                                             |
| PreToolUse（Edit、Write、MultiEdit、NotebookEdit） | `loom-file-context.mjs`     | 即将被修改的文件在 Loom 中已知的信息（`memory.for_file`）：你的规则，以及仓库的共享规则和设计决策；每个文件每 30 分钟一次                                                                                                                                                                                                       | `LOOM_FILE_CONTEXT=off`                                                                                     |
| PostToolUse（Loom 工具）                          | `loom-mentor-hint.mjs`      | 把结果中的 `hint` 提升到上下文中，每个会话中每种 hint 代码一次                                                                                                                                                                                                                                                                  | `LOOM_MENTOR_HINTS=off`                                                                                     |
| PostToolUse（Edit、Write、MultiEdit）             | `loom-blast-radius.mjs`     | 来自 `code.impact` 的一行信息：调用方、触及该修改的测试、尚未改动的共同变更文件                                                                                                                                                                                                                                                 | `LOOM_BLAST_RADIUS=off`                                                                                     |
| PostToolUse、PostToolUseFailure（任意工具）       | `loom-capture.mjs`          | 每次工具调用记录一条情景观察（`observe.record`）；失败的调用类型为 `error`                                                                                                                                                                                                                                                      | 无                                                                                                          |
| Stop、SessionEnd                                  | `loom-conversation.mjs`     | 把新的对话轮次追加到对话平面（`conversation.append`）                                                                                                                                                                                                                                                                           | `LOOM_CAPTURE_CONVERSATIONS=false`                                                                          |
| Stop                                              | `loom-usage.mjs`            | 把本次会话的 token 用量提交到 `cost.record_turns`                                                                                                                                                                                                                                                                               | `LOOM_USAGE=off`                                                                                            |
| Stop                                              | `loom-test-gate.mjs`        | 对本次会话所做的修改，按 `code.test_gate` 给出的该修改应运行的测试，拦截一次“完成”                                                                                                                                                                                                                                              | `LOOM_TEST_GATE=off`                                                                                        |
| Stop                                              | `loom-validate-gate.mjs`    | 当 `namespace.validate` 在本次会话写入的记忆键上发现错误或警告时，拦截一次                                                                                                                                                                                                                                                      | `LOOM_VALIDATE_GATE=off`                                                                                    |
| Stop                                              | `loom-namespace-health.mjs` | 在第一次 Stop 时，如果 `namespace.audit` 发现命名空间存在严重误用，输出一行提示，指向 `loom-optimize` skill                                                                                                                                                                                                                      | `LOOM_NAMESPACE_HEALTH=off`                                                                                 |
| Stop、PreCompact                                  | `loom-working-memory.mjs`   | 刷新工作记忆笔记（`session:<project>`）：目标、git 状态、已编辑的文件、最后一条回复                                                                                                                                                                                                                                             | `LOOM_WORKING_MEMORY=off`                                                                                   |
| Stop、PreCompact、SessionEnd                      | `loom-capture.mjs`          | 固化本次会话（`observe.crystallize`）                                                                                                                                                                                                                                                                                           | 无                                                                                                          |
| PreCompact、SessionEnd                            | `loom-session-brief.mjs`    | 修订该分支的会话简报（`session.brief_save`，键为 `auto/<project>/<branch>`）                                                                                                                                                                                                                                                    | `LOOM_SESSION_BRIEF=off`                                                                                    |
| SessionEnd                                        | `loom-run.mjs`              | 关闭该运行（`run.complete`，`outcome: none`，以最后一条回复作为最终响应）                                                                                                                                                                                                                                                       | `LOOM_RUN_LEDGER=off`                                                                                       |
| SessionEnd                                        | `loom-session-memory.mjs`   | 把本次会话的轮次交给 `memory.set_from_messages(defer=true)`，由它提取事实并写入会话摘要                                                                                                                                                                                                                                         | `LOOM_SESSION_MEMORY=off`                                                                                   |

插件的 hooks 及其 MCP header 辅助脚本（负责把令牌加到 Loom 的 MCP 请求中的脚本）会在你的机器上运行 `node`、`git` 和 `grep`：`git` 读取检出目录的远程仓库、分支、`HEAD` 提交、状态和差异，`grep` 则针对插件重定向到代码索引的 grep 的模式，在未提交的文件中搜索。`loom-session` skill 的简报脚本还会运行 `git log`，以获取自上一份简报以来的提交。测试门禁（在回复结束时指出某项修改所需测试的 hook）不运行 Python：对于 pytest 测试，它会选择检出目录虚拟环境的解释器、当前激活的解释器（`VIRTUAL_ENV`），或 `PATH` 上的 `python3` 或 `python`，只检查该解释器的文件是否存在，而不运行它。

插件指明了其 hooks 所支持的最旧的 Loom 服务器版本（`lib/server-version.mjs` 中的 `MIN_SERVER_VERSION`）。SessionStart 会从它本来就要请求的 `GET /v1/health` 响应中读取服务器的 `version`，当该版本较旧时，输出一行 `loom:` 信息，写明两个版本并请求升级服务器。无论如何会话都会继续；如果服务器不可达、响应不是 2xx、服务器没有报告 `version`，或者版本不是 SemVer，则不输出任何内容。`loom-status` skill（`bin/loom-client-status.mjs`）会把同样的信息作为警告报告。最低版本会随着首个提供某个 hook 所需功能的发布而提高，因此在该 hook 合并之后、该版本发布之前，上一个版本的服务器不会收到警告。

**为什么运行以 `outcome: none` 关闭。** hook 只知道会话结束了，并不知道会话是否成功，也从不猜测 agent 依赖了哪些记忆。知道自身结果的 agent 会自己调用 `run.complete`。

**什么存储在哪里。**

- 只有当命名空间开启了对话日志时，才会存储对话轮次（新命名空间默认关闭；见 `conversation.set_logging`）。即使丢弃了某一轮，`conversation.append` 也会返回成功。参见[对话与会话](https://github.com/ClickHouse/loom/blob/main/docs/features/conversations-and-sessions.md)。
- 只有当服务器运行 `observe_extract` worker（`LOOM_OBSERVE_EXTRACT_ENABLED=true`，需要 LLM 和 embedder 凭据；参见[配置](https://github.com/ClickHouse/loom/blob/main/docs/reference/configuration.md#extraction)）时，原始观察才会变成持久记忆。
- 会话记忆与日志记录无关：`memory.set_from_messages` 会写入事实和摘要，并把所给的轮次保留为其来源证据。

**安全性。** hook 从不因网络而阻塞 agent。

- 写入是发出即不管；较慢的写入（对话、会话记忆、会话简报）交给独立的后台刷新进程（`hooks/loom-flush.mjs`），它会在 hook 结束后继续运行。
- 每个请求在 `/v1/health` 往返时间的 5 倍之后中止，限制在 3–8 秒之间，并按服务器缓存六小时。`LOOM_HOOK_TIMEOUT_MS` 和 `LOOM_HOOK_EXIT_MS` 可以覆盖该值。
- 错误会被吞掉。只有引导内容会等待响应正文；如果失败，会话会在没有它的情况下启动。没有令牌或没有服务器时，每个 hook 都以 0 退出。

**SessionEnd 预算。** Claude Code 在所有 SessionEnd hooks 之间共享约 1.5 秒。插件的五个条目各自声明 `timeout: 10`，以免最后一个被取消。

<a id="recalled-content-and-local-data"></a>

### 召回的内容与本地数据

引导内容、每条提示词的召回、已保存简报列表、编辑前注入的文件规则（`loom-file-context`），以及代码分流在回答 GitHub issue 搜索时引用的跟踪器条目，都可能带有其他人写入的内容：团队的文件规则存放在仓库的代码命名空间中，每个成员都可以写入，而任何能提交 issue 的人都能写入其标题。每一条都标注了其写入者（召回中为 `by=`，团队文件规则中为作者），插件会把这些内容都包在一个 `<loom-recalled-data>` 块中，并声明其内容是数据而不是指令。每个块的标签都带有一个 id，即 `<loom-recalled-data id="…">` 和 `</loom-recalled-data id="…">`，由该块的文本在保存于 `~/.loom/recall-key` 的密钥（只创建一次，权限 `0600`）下派生而来，说明中只声明该结束标签才会结束这个块：文本的写入者无法计算出 id，因此存储的文本无法写出该块的结尾；而相同的文本始终得到相同的 id，因此引导内容可以跨会话缓存。插件还会把文本内部的该标签名（包括常见的形近拼写）改写为 `[loom tag removed]`。只有调用者自己写的流程才会被注入（队友的流程仍会在显式调用 `procedure.match` 时给出回答）。Loom 无法控制 agent 随后会做什么：让工具权限独立于检索到的文本，是客户端的责任。

hooks 的临时文件（轮次日志、游标、暂存、刷新任务，以及代码分流的决策日志、待处理批次和拒绝计数）以 `0600` 创建，绝不通过符号链接或写入其他用户的文件，也绝不从被植入的符号链接、硬链接或其他用户的文件中读取。在 SessionEnd 时，会话记忆 hook 会删除其召回内容暂存；轮次日志会保留，留待之后某次会话启动时清理。保留期限以及 hooks 在发送前脱敏的内容，见[数据处理](#data-handling)。

<a id="tuning-knobs"></a>

### 调优参数

除表中的关闭开关之外：

| 变量                             | 默认值                       | 作用                                                    |
| ------------------------------ | ------------------------- | ----------------------------------------------------- |
| `LOOM_CONTEXT_TOKEN_BUDGET`    | `1200`                    | SessionStart 引导信息的 token 预算                            |
| `LOOM_CODE_MAP_TOKEN_BUDGET`   | `0`                       | 引导信息中代码地图的 token 预算；`0` 表示不包含代码地图                     |
| `LOOM_CONTEXT_RUNS`            | `false`                   | 在引导信息中包含最近的运行；`LOOM_CONTEXT_RUNS_LIMIT`（8）限制其数量      |
| `LOOM_PROMPT_RECALL_MIN_CONF`  | `0.5`                     | 提示召回注入任何内容所需的最低 `retrieval_confidence`                |
| `LOOM_PROMPT_RECALL_MAX_CHARS` | `1200`                    | 注入的召回内容的字符数上限                                         |
| `LOOM_PROMPT_PACK_TOP_K`       | `3`                       | 每条提示的指针数量（`LOOM_PROMPT_PACK=off` 会关闭该打包）            |
| `LOOM_FILE_CONTEXT_TTL_MIN`    | `30`                      | 再次获取同一文件上下文之前的间隔分钟数                                   |
| `LOOM_BLAST_DEPTH`             | `1`                       | 用于影响范围行的 `code.impact` 深度                             |
| `LOOM_TEST_GATE_DEPTH`         | `2`                       | 测试关卡跟踪的跳数（1–3）                                        |
| `LOOM_OBSERVE_DEDUPE_S`        | `60`                      | 在此时间窗口内重复的读取不会再次被观察；`0` 表示禁用                          |
| `LOOM_OBSERVE_SKIP_TOOLS`      | 内置列表                      | 以逗号分隔的工具名（支持 `*` 通配符），这些工具不产生观察记录                     |
| `LOOM_PLUGIN_SCRATCHPAD`       | 未设置                       | `1` 会将每一轮的一行结论追加到该运行的 `scratchpad` 工作记忆块中            |
| `LOOM_SESSION_BRIEF_KEY`       | `auto/<project>/<branch>` | 会话简报保存时使用的键                                           |

`integrations/claude-code-plugin/README.md` 记录了其余的配置项。

<a id="the-code-shunt"></a>

## 代码分流

在已建立索引的检出目录中，`loom-code-shunt.mjs` 会拒绝批量读取，并指明应改用的 op：标识符用 `code.symbol`，字面量或正则表达式用 `code.grep`，自然语言词语用 `code.search`。索引返回的是所在的代码单元及其调用方，而不是整个文件。

| 工具调用                                                                      | 拒绝条件                                                                              |
| ----------------------------------------------------------------------------- | ------------------------------------------------------------------------------------- |
| `Read` 读取整个文件                                                           | 该文件已建立索引、未被修改，且行数超过 `LOOM_SHUNT_MAX_LINES`（800）                  |
| 带 `offset` / `limit` 的 `Read`，或 `sed -n` / `head` / `tail` / `awk` 截取   | 读取范围超过 `LOOM_SHUNT_READ_CAP`（0 = 关闭）；每个会话中每个文件最多拒绝两次        |
| 对目录执行 `Grep`；Bash 中的 `rg`、`grep -r`、`cat`                           | 搜索范围位于已建立索引的目录树内                                                      |
| 在单个文件中对某一个符号名执行 grep                                           | 每个会话中每个符号一次，并给出确切的 `code.symbol` 调用                               |
| `gh issue list --search`、`gh search issues`                                  | 每个查询一次，由 `tracker.duplicate_check` 返回最接近的已报告条目作为答复             |

其他情况一律放行：有针对性的读取、单文件 grep、管道、你修改过或尚未提交的文件，以及未建立索引的仓库。在符号分流或 tracker 分流拒绝之后，同一条命令在下一次尝试时会被放行。

索引过时并不会关闭分流：未修改的文件按其被索引时的提交作答，拒绝提示中会写明该提交。如果想改为搜索工作树，请设置 `local_first` / `LOOM_CODE_LOCAL_FIRST=on`；此时分流停止生效，primer 也会跳过代码地图。

仓库检测结果会缓存十分钟。如果查询在 `LOOM_SHUNT_RESOLVE_TIMEOUT_MS`（8 秒）内没有返回，会在一分钟后重试，而不会被缓存为“未建立索引”。决策记录在 `$TMPDIR/loom-shunt-log-<hash of the uid>.log` 中（`LOOM_SHUNT_LOG=off` 可禁用），并以 `code.outcome` 行的形式上报给服务器（`LOOM_SHUNT_REPORT=off` 可禁用）。`LOOM_SHUNT_GREP=off` 和 `LOOM_SHUNT_BASH=off` 分别使 Grep 工具和 Bash 工具不受分流限制。

`claude --bare` 会跳过 hook，因此上述机制在该模式下都不会运行；这种情况由 [grep 垫片](grep-shim.md)处理。各项操作的说明见[代码索引操作](https://github.com/ClickHouse/loom/blob/main/docs/features/code-index/operations.md)。

<a id="skills"></a>

## 技能

| 技能                       | 加载时机                                                                 | 作用                                                                                                                                                                                                       |
| -------------------------- | ------------------------------------------------------------------------ | ---------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `loom-code`                | 即将在已建立索引的仓库中执行 grep、glob 或打开文件时                     | 说明用哪个 `code.*` op 来替代这种习惯性操作，以及如何解读它的返回结果                                                                                                                                      |
| `loom-memory`              | 陈述、决定或更正了某个事实时；一项较大的任务结束时                       | 区分什么是持久记忆、什么是临时草稿，如何写下后续会话可以信任的事实，以及任务结束时的回顾                                                                                                                   |
| `loom-mentor`              | 提示中出现了你不了解的 plane 或 op；"Loom 能做什么"                      | 提供"场景 → plane → op"的对应关系，以及每个提示代码的含义                                                                                                                                                  |
| `loom-optimize`            | "优化我的命名空间"、"记忆搜索效果不好"、出现命名空间健康状况提示行       | 执行 `namespace.audit`，制定由所有者批准的方案，再用 `memory.move` 并设置重定向（[指南](https://github.com/ClickHouse/loom/blob/main/docs/guides/optimize-namespace.md)）                                    |
| `loom-session`             | "保存这个会话"、"恢复"、"我在这个分支上做到哪了"                         | 列出已保存的摘要，恢复用户选定的那一份（摘要附带客户端的压缩总结；使用 `--transcript` 时还会附带每一轮记录的对话），或者保存一份；它不会在未被要求时加载摘要                                               |
| `loom-status`              | 工具提示未认证、缺少 primer、"Loom 在正常工作吗？"                       | 检查 Node 运行时（仅在你同意后才安装），运行客户端检查，并指出唯一需要的修复                                                                                                                               |
| `improve-loom-for-my-repo` | 代码工具未能命中的会话结束时                                             | 把会话中的未命中情况整理成可复现的问题发现，供维护者参考                                                                                                                                                   |

`loom-session` 运行 `bin/loom-brief.mjs`（`list`、`get [N | KEY] [--transcript]`、`save`）；其中没有任何操作会自行运行。

<a id="checking-it-works"></a>

## 检查是否正常工作

- 调用几次工具之后，控制台会显示该会话的观察记录；如果开启了日志记录，还会显示其对话内容。对 `cc-<session_id>` 执行 `run.get` 可以回放该次运行。
- `/loom:loom-status` 会运行插件中的 `bin/loom-client-status.mjs`，报告：它将要调用的服务器、令牌来自哪一种查找方式、`~/.loom/config.json` 是否指向了另一台服务器、令牌是否被接受并且能否写入该命名空间，以及从未到达服务器的捕获批次。它只需要插件本身，不依赖其他组件。
- `loom doctor --client`（运营方 CLI，可在源码检出目录或服务器镜像中使用）会额外检查 Claude Code 和 Codex 的接入配置。

<a id="troubleshooting"></a>

## 故障排查

先运行 `/loom:loom-status`，它会用一行文字指出原因。它通过 `sh` 执行检查：在没有 Git Bash 的 Windows 上，请改为运行 `node <plugin root>/bin/loom-client-status.mjs`。

| 现象                                                         | 原因                                               | 解决方法                                                                                                                                                                                                           |
| ------------------------------------------------------------ | -------------------------------------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------ |
| hook 报错提示找不到 `node`                                   | Claude Code 启动时的 `PATH` 中没有 `node`          | 在 settings.json 的 `env` 块中将 `PATH` 设置为完整的搜索路径，并把该 Node 所在目录放在最前面，例如 `"PATH": "/opt/homebrew/bin:/usr/bin:/bin:/usr/sbin:/sbin"`，或运行 `loom-status` skill；然后重启 |
| 每个 hook 报错都引用 `[stdin]:1` 和 `SyntaxError`            | 早于 2.1.139 的 Claude Code 会丢弃 `args`          | 更新 Claude Code                                                                                                                                                                                                   |
| hook 失败且没有捕获任何内容，但已安装 Node                   | 该 `PATH` 中的第一个 `node` 低于 18 版本           | 将 Node.js 18+ 放在该 `PATH` 的最前面，或运行 `loom-status` skill；然后重启                                                                                                                                        |
| MCP 服务器提示"需要认证"                                     | header helper 没有可用的 Node，或未找到令牌        | `loom-status` skill 会指出具体是哪一种                                                                                                                                                                             |
| 没有 primer，也没有捕获任何内容                              | 服务器无法访问，或未找到令牌                       | 运行 `loom-status`；检查 `~/.loom/config.json`                                                                                                                                                                     |
| 工具有响应，但捕获没有                                       | 该令牌在写入命名空间上只有查看者权限               | 创建一个具有写入权限的令牌                                                                                                                                                                                         |
| 对话页面为空                                                 | 该命名空间的对话日志已关闭                         | 使用控制台对话页面上的 **Record conversations** 开关，或调用 `conversation.set_logging`                                                                                                                            |
| 新版本中新增的 hook 从未触发                                 | 已安装的插件比服务器旧                             | `claude plugin update loom@loom`                                                                                                                                                                                   |
| 每个工具都列出两次                                           | 插件之外还有一个用户级别的 `loom` MCP 条目         | `claude mcp remove loom`                                                                                                                                                                                           |
| 在本地工作的仓库中读取被拒绝                                 | 该检出已被索引，且文件未被修改                     | `--config local_first=true` 或 `LOOM_CODE_LOCAL_FIRST=on`                                                                                                                                                          |
| 退出时出现 `Hook cancelled`                                  | SessionEnd hook 的运行时间超出了客户端的限额       | 更新插件（其条目声明了 `timeout: 10`）                                                                                                                                                                             |

<a id="other-clients"></a>

## 其他客户端

`hooks/hooks.generic.json` 将相同的生命周期事件映射到相同的脚本，适用于任何以 JSON 形式通过 stdin 接收 hook 载荷并运行命令的客户端；请将 `${LOOM_PLUGIN_DIR}` 替换为插件目录的绝对路径。这些脚本会探测多个常见的载荷字段名，并从第三个参数（`… PostToolUse codex`）或 `LOOM_CLIENT` 中读取客户端名称；未指定名称时，默认视为 Claude Code。对于只能导出对话记录的客户端，请参阅[对话记录捕获 hook](https://github.com/ClickHouse/loom/blob/main/docs/features/integrations/index.md#transcript-capture-hooks)。

<a id="related"></a>

## 相关内容

- [Codex 插件](https://github.com/ClickHouse/loom/blob/main/docs/features/integrations/codex.md)：相同的脚本，搭配 Codex 的接入配置。
- [MCP 客户端](https://github.com/ClickHouse/loom/blob/main/docs/features/integrations/mcp-clients.md)：不使用插件时的连接方式。
- [agent 集成指南](https://github.com/ClickHouse/loom/blob/main/docs/guides/agent-integration.md)。
- [操作参考](https://github.com/ClickHouse/loom/blob/main/docs/reference/operations.md)：hooks 调用的所有 op。
