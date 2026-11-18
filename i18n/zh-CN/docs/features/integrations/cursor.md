<!-- i18n: source=docs/features/integrations/cursor.md blob=ba85fce4ce13c17118f863ce5f4a67693523e037 model=claude-sonnet-5-5 -->
> 本页由机器从[英文原文](https://github.com/ClickHouse/loom/blob/main/docs/features/integrations/cursor.md)翻译，内容以英文原文为准。
> Machine-translated from the [English page](https://github.com/ClickHouse/loom/blob/main/docs/features/integrations/cursor.md), which is authoritative.

<a id="cursor"></a>

# Cursor

Cursor 通过三部分接入 Loom：

| 组成部分 | 提供的内容 | 方式 |
| ------------------ | -------------------------------------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------- |
| MCP 工具 | Cursor 的 agent 中的 `loom_*` 工具 | [插件](#plugin)、适用于本地单用户服务器的[安装链接](#mcp-tools)，或 `loom install --client cursor`（[MCP 客户端](https://github.com/ClickHouse/loom/blob/main/docs/features/integrations/mcp-clients.md)） |
| Skills | `loom` 和 `loom-code` skills | [插件](#plugin)，或 `npx skills add ClickHouse/loom --skill loom loom-code` |
| 对话记录捕获 | 每次对话的各轮内容写入 `conversation.append` | `sessionEnd` hook `integrations/cursor/cursor-session-hook.sh` |

Cursor 插件包含 MCP 条目以及 `loom` 和 `loom-code` skills，不含任何 hook：没有代码分流，也不捕获工具调用、运行或工作记忆；[Claude Code](https://github.com/ClickHouse/loom/blob/main/docs/features/integrations/claude-code.md) 和 [Codex](https://github.com/ClickHouse/loom/blob/main/docs/features/integrations/codex.md) 插件则具备这些功能。

<a id="plugin"></a>

## 插件

仓库根目录下的 `.cursor-plugin/` 使该仓库成为一个 Cursor 插件市场，其中只有一个插件 `clickhouse-loom`：包含 `loom` MCP 服务器和根目录下的 `skills/`。该插件未在 Cursor 的市场中上架。它的本地单用户连接和只读记忆搜索已在 Cursor 中测试过。在 Teams 或 Enterprise 套餐上，团队管理员可在 **Dashboard → Plugins & MCPs → Add Marketplace → Import from Repo** 下添加，地址为 `https://github.com/ClickHouse/loom`。

该插件只有一个变量：**Loom MCP URL**。团队管理员在 Cursor 控制台的 **Plugins & MCPs** 下为所有成员设置它。

| 服务器                                     | Loom MCP URL                                             | 登录方式            |
| ------------------------------------------ | -------------------------------------------------------- | ------------------- |
| 单用户，在每个成员的机器上                 | `http://localhost:7777/mcp?toolset=coding`（默认值）     | 无                  |
| 设置了 `LOOM_OAUTH_ENABLED=true` 的团队服务器 | `https://<your server>/mcp`                              | 浏览器，每人各自登录 |

开启 OAuth 的服务器使用不带参数的 URL，即其受保护资源元数据所指明的资源；默认工具集之外的工具通过 `loom_op` 运行。该条目不发送 `Authorization` 头，因此未开启 OAuth 的团队服务器会返回 401。这样的团队应跳过该插件：使用[下文](#mcp-tools)的 `loom install --client cursor` 写入带令牌的条目，并用 `npx skills add` 添加 skills。
[通过浏览器登录的客户端](https://github.com/ClickHouse/loom/blob/main/docs/features/integrations/mcp-clients.md#clients-that-sign-in-by-browser)中有 OAuth 的详细说明。

<a id="mcp-tools"></a>

## MCP 工具

同一台机器上的单用户服务器无需令牌即可接收 MCP 调用。[将 Loom 添加到 Cursor](https://cursor.com/en/install-mcp?name=loom&config=eyJ1cmwiOiJodHRwOi8vbG9jYWxob3N0Ojc3NzcvbWNwP3Rvb2xzZXQ9Y29kaW5nIn0%3D) 会在 `mcpServers.loom` 下添加此条目，使用控制台为 Cursor 提供的 `coding` 工具集。链接中的 `config` 就是该条目的 base64 编码：

```json
{ "url": "http://localhost:7777/mcp?toolset=coding" }
```

该条目指向默认端口 7777；如果使用其他端口，请编辑 `~/.cursor/mcp.json` 中的 `url`。对于其他任何服务器，请连同令牌一起写入该条目：

```bash
loom install --client cursor --server-url https://loom.example.com --token "$LOOM_API_KEY"
```

这会把一个 `mcpServers.loom` 条目（`url` 加上 `Authorization` 请求头）合并到 `~/.cursor/mcp.json` 中。之后请重启 Cursor。工具集、agent 标签以及各客户端的文件对照表见 [MCP 客户端](https://github.com/ClickHouse/loom/blob/main/docs/features/integrations/mcp-clients.md#wire-a-client-with-loom-install)。

然后从 [agent 指令](https://github.com/ClickHouse/loom/blob/main/docs/features/integrations/agent-instructions.md) 中添加 `.cursor/rules/loom.mdc`，让 agent 知道何时检索记忆、保存记忆以及使用代码索引，除非仓库的 `AGENTS.md` 已经包含该内容块。

<a id="transcript-capture"></a>

## 对话记录捕获

你需要自行注册一个 hook。`cursor-session-hook.sh` 会把已结束对话的记录交给 `loom ingest-transcript`，后者将每一轮对话发送到 `conversation.append`。只有命名空间开启了对话日志时，这些轮次才会被存储。

<a id="install"></a>

### 安装

1. 从 Loom 代码仓库检出目录安装 `loom` CLI（`pip install -e .`），或从 git 安装（`pip install "git+https://github.com/ClickHouse/loom"`），并确保 `python3` 在 `PATH` 中。
2. 将 hook 复制到 `~/.cursor/hooks/cursor-session-hook.sh`，并赋予可执行权限。
3. 在 `~/.cursor/hooks.json` 中注册它。用户级文件中的路径相对于 `~/.cursor/`；项目内的 `.cursor/hooks.json` 则从项目根目录解析路径。

   ```json
   {
     "version": 1,
     "hooks": {
       "sessionEnd": [{ "command": "./hooks/cursor-session-hook.sh", "timeout": 150 }]
     }
   }
   ```

4. 为 hook 提供环境变量。从 Dock 启动的应用看不到你的 shell 配置文件，因此请把令牌放在 `~/.loom/token` 中（`loom mint-token --save` 会写入该文件），其余变量在 `hooks.json` 所指向的包装脚本中设置：

   ```bash
   #!/usr/bin/env bash
   export LOOM_BASE_URL=https://loom.example.com LOOM_NAMESPACE=my-namespace
   exec "$HOME/.cursor/hooks/cursor-session-hook.sh" "$@"
   ```

5. 为该命名空间开启对话日志（控制台“对话”页面上的 **Record conversations** 开关，或使用 `conversation.set_logging`）。在此之前，服务器会接受这些轮次，但不会存储任何内容。

| 变量                         | 默认值                  | 含义                                                                                            |
| ---------------------------- | ----------------------- | ----------------------------------------------------------------------------------------------- |
| `LOOM_NAMESPACE`             | 必填                    | 对话线程写入的命名空间                                                                          |
| `LOOM_API_KEY`               | `~/.loom/token`         | 令牌；CLI 从环境变量中读取，因此它不会出现在命令行上                                            |
| `LOOM_BASE_URL`              | `http://localhost:7777` | Loom 服务器                                                                                     |
| `LOOM_AGENT`                 | `cursor`                | 对话行上的 `agent`                                                                              |
| `LOOM_BIN`                   | `loom`                  | 要运行的 CLI                                                                                    |
| `LOOM_CAPTURE_CONVERSATIONS` | `true`                  | 设为 `false` 会使 hook 不执行任何操作                                                           |
| `LOOM_INGEST_MAX_BYTES`      | `16777216`              | 超过该大小的记录会被跳过，而不是被截断                                                          |
| `LOOM_INGEST_TIMEOUT`        | `120`                   | CLI 允许运行的秒数（需要 `timeout` 或 `gtimeout`）；请保持小于 `hooks.json` 中的超时时间        |

<a id="what-the-hook-reads"></a>

### hook 读取的内容

Cursor 会将 hook 作为进程启动，并向其 stdin 写入一个 JSON 对象。hook 只读取其中两个字段，其余忽略：

| 字段              | 用途                                                                             |
| ----------------- | -------------------------------------------------------------------------------- |
| `transcript_path` | 记录文件；关闭记录功能时为 `null`，此时不会发送任何内容                          |
| `conversation_id` | 线程 id `cursor-<conversation_id>`（仅允许 `[A-Za-z0-9._:-]`，最长 128 个字符）  |

环境变量 `CURSOR_TRANSCRIPT_PATH` 是路径的后备来源。如果没有可用的 `conversation_id`，线程 id 为 `cursor-` 加上记录路径的哈希值。请注册 `sessionEnd`；`stop` 也可以使用，但它会在每个 agent 轮次之后重新发送整份记录。

hook 读取 Cursor 写在 `~/.cursor/projects/<project>/agent-transcripts/` 下的 JSONL：每行一个对象，包含 `role` 和 `message.content`（字符串或块列表）。由 `{role, content}` 组成的 `.json` 数组，或带有 `# user` / `# assistant` 小节的 `.md` 文件，会被原样传递。其他任何文件都会被跳过，并在 stderr 上输出一条消息。

<a id="capture-boundary"></a>

### 捕获范围

- **会发送：** `user` 和 `assistant` 行的文本，按文件顺序发送。只发送文本块：`tool_use` 块、工具结果、思考内容以及其他所有角色，都会在本机被丢弃。
- **不会发送：** hook 输入中的 `user_email`、`workspace_roots` 或模型字段。写入者就是令牌所对应的身份；这些行只带有 `agent`，不包含任何来自 Cursor 账户的信息。
- **不会脱敏：** 轮次文本按原样发送。请不要在要捕获的对话中出现密钥，或者保持该命名空间的日志关闭。

<a id="behaviour"></a>

### 行为

- **从不阻塞 Cursor。** 在 hook 模式下，所有结果都以退出码 0 退出，并在 stdout 上输出 `{}`；CLI 的输出写入 stderr。
- **Loom 不可达时是安全的。** hook 会记录一行日志并以 0 退出，不会重试。下一次运行会重新发送记录，而 `conversation.append` 会跳过已经写入的轮次（[轮次 id](https://github.com/ClickHouse/loom/blob/main/docs/features/integrations/index.md#transcript-capture-hooks)）。
- **有界。** hook 输入最多读取 1 MiB，记录最多读取 `LOOM_INGEST_MAX_BYTES`，CLI 最多运行 `LOOM_INGEST_TIMEOUT` 秒。
- **安静。** 诊断信息是 stderr 上以 `loom-cursor-hook:` 为前缀的一行，不会包含任何记录文本或令牌。
- **手动模式。** `cursor-session-hook.sh <transcript>` 会摄取单个文件，并以 CLI 的退出码退出（`2` 表示配置错误，`3` 表示记录被跳过，其余情况为 CLI 自身的退出码）。

<a id="limits"></a>

## 限制

- 存储的轮次只是一份记录，不是提取来源：不会从中挖掘任何内容。若要从对话中写入记忆，请将其轮次发送到 `memory.set_from_messages`。
- 转录格式属于 Cursor，Cursor 未公开其文档；格式发生变化时，stderr 上会出现 "no user or assistant text in the transcript"。

<a id="related"></a>

## 相关内容

- [集成](https://github.com/ClickHouse/loom/blob/main/docs/features/integrations/index.md#transcript-capture-hooks)：其他对话记录模板。
- [MCP 客户端](https://github.com/ClickHouse/loom/blob/main/docs/features/integrations/mcp-clients.md)。
- [对话与会话](https://github.com/ClickHouse/loom/blob/main/docs/features/conversations-and-sessions.md)。
- [源码 README](https://github.com/ClickHouse/loom/blob/main/integrations/cursor/README.md)。
