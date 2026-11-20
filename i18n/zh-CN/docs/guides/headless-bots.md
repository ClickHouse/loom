<!-- i18n: source=docs/guides/headless-bots.md blob=7e255a8b2f49ab5ea38bb7947e4bde837a8a6323 model=claude-sonnet-5-5 -->
> 本页由机器从[英文原文](https://github.com/ClickHouse/loom/blob/main/docs/guides/headless-bots.md)翻译，内容以英文原文为准。
> Machine-translated from the [English page](https://github.com/ClickHouse/loom/blob/main/docs/guides/headless-bots.md), which is authoritative.

<a id="headless-bots"></a>

# 无头机器人

无头机器人在运行时没有人来粘贴令牌、发现静默失败，或者说“记住这个”。本指南涵盖机器人的身份、连接检查、三种运行时、机器人写入的内容，以及如何诊断一个不再有动静的机器人。

<a id="1-give-the-bot-an-identity"></a>

## 1. 为 bot 分配身份

`loom` CLI 随服务器镜像一同提供，也可以从克隆的仓库用 `pip install -e .` 安装。SDK 包（`clickhouse-loom`）不带 CLI。

**使用自助注册。** 在设置了 `LOOM_SELF_SIGNUP=open` 的服务器上，bot 可以自行注册：

```bash
loom init --agent nightly-triage --server-url https://<loom> --purpose "Files the nightly failures."
```

`loom init` 会执行以下操作：

1. 创建命名空间 `agent-nightly-triage`（`--purpose` 会作为其描述）。
2. 在该命名空间上授予服务账号主体 `writer` 角色。
3. 将令牌写入 `~/.loom/token`（权限为 0600），并将 URL、命名空间和组织写入 `~/.loom/config.json`（可用 `--token-path` 和 `--config-path` 更改路径）。
4. 输出一个认领码以及认领页面的地址（`/oauth/agent-claim`）。由一位用户在该页面输入认领码并设置自己的密码，之后即可使用命名空间名称和该密码，以管理员身份登录。认领码本身不能用于登录，也不能再次使用，第二次认领会被拒绝。bot 的令牌仍然有效。

`--ttl-days` 设置令牌的有效期（默认 365 天）。`--org` 用于指定自助注册组织之外的其他组织。

**在 CI 中**，请输出令牌而不是写入文件，并将其保存为密钥。使用 `--no-files` 时，输出的第一行就是令牌：

```bash
loom init --agent ci-reviewer --server-url https://<loom> --no-files | head -n 1 > token.txt
```

使用 `--json` 时，注册结果以 JSON 形式输出，其中不含令牌；使用 `--json --no-files` 时，令牌会包含在该 JSON 中，这是它唯一出现的位置。

**不使用自助注册时**，由运维人员签发令牌，或者由用户在 **Settings → Tokens** 下创建一个只能读取 bot 所用命名空间的令牌：

```bash
loom mint-token --subject ci-reviewer --org <org> --namespaces agent-ci --roles writer \
  --service-account --ttl-days 30 --save
```

有关角色、有效期和轮换，请参阅[令牌与授权](https://github.com/ClickHouse/loom/blob/main/docs/guides/tokens-and-grants.md)。

<a id="2-check-the-connection-before-the-first-run"></a>

## 2. 首次运行前检查连接

```bash
LOOM_BASE_URL=https://<loom> LOOM_API_KEY="$(cat token.txt)" loom doctor --client
```

`loom doctor --client` 会按照插件钩子和 SDK 的方式解析服务器和令牌，并说明每一项的来源。它会探测 `/v1/health` 和 `namespace.list_mine`，并检查该令牌是否可以写入 bot 的命名空间。每个问题都会输出一行 `FAIL`，并指明修复方法，同时命令的退出码为非零，因此该命令可以用作 CI 关卡：

```yaml
- run: loom doctor --client --server-url ${{ vars.LOOM_BASE_URL }} --token ${{ secrets.LOOM_API_KEY }}
```

`--json` 会以 JSON 格式输出检查结果和问题列表，供需要解析它们的流水线使用。

<a id="3-connect-the-runtime"></a>

## 3. 连接运行时

<a id="claude-agent-sdk-or-claude--p"></a>

### Claude Agent SDK 或 `claude -p`

通过文件把 Loom 挂载为 MCP 服务器，并且只允许使用它的工具。无头运行没有交互式 OAuth，因此 bearer 令牌通过请求头传递：

```bash
cat > loom.mcp.json <<EOF
{"mcpServers": {"loom": {"type": "http",
  "url": "https://<loom>/mcp?toolset=coding",
  "headers": {"Authorization": "Bearer $LOOM_API_KEY"}}}}
EOF
claude -p --mcp-config loom.mcp.json --strict-mcp-config \
  --allowedTools "mcp__loom__*" \
  --output-format stream-json --verbose \
  "Triage the failures in the last nightly run; record each root cause you find."
```

| 标志                            | 原因                                                                                                 |
| ------------------------------- | ---------------------------------------------------------------------------------------------------- |
| `--strict-mcp-config`           | 让 bot 只使用文件中指定的服务器，而不使用用户的配置。                                                |
| `--allowedTools "mcp__loom__*"` | 预先批准所有 Loom 工具，这样运行不会被权限提示阻塞。                                                 |
| `--output-format stream-json`   | 第一条消息是会话的 `system/init`（见下文）。`claude -p` 搭配它时需要 `--verbose`。                   |
| `--bare`                        | 跳过用户的 hook、插件和 `CLAUDE.md`，以获得一致的行为。                                              |

> **警告：** 请以连接状态作为准入条件。`system/init` 消息会列出 `mcp_servers[]`，其中每项都带有 `status`。只有当 `loom` 的状态为 `connected` 时才应信任该次运行；否则模型是在没有记忆的情况下运行的。

用 `?toolset=` 选择工具列表；各个工具集列在 [MCP 工具集](https://github.com/ClickHouse/loom/blob/main/docs/reference/mcp.md#toolsets)中。

如果运行 `claude -p` 的用户已安装 Loom 插件，它的 hook 也会运行（见 [Claude Code 插件](../features/integrations/claude-code.md)）。它的 mentor 提示只针对插件自带服务器的工具（`mcp__plugin_loom_loom__*`），而不针对 `--mcp-config` 中的 `loom` 条目。

<a id="codex-codex-exec"></a>

### Codex `codex exec`

```bash
export LOOM_API_KEY="$(cat token.txt)"
codex exec --json \
  -c 'mcp_servers.loom.url="https://<loom>/mcp?toolset=coding"' \
  -c 'mcp_servers.loom.bearer_token_env_var="LOOM_API_KEY"' \
  "Summarise what we decided about retries; record any decision that is missing."
```

Codex 从该条目指定的变量中读取 bearer 令牌，该变量取自 `codex` 启动时所在的环境。安装 Codex 插件后，它的 hook 在此处也会运行（见 [Codex 插件](https://github.com/ClickHouse/loom/blob/main/docs/features/integrations/codex.md)）。

<a id="the-sdk-in-the-bots-own-process"></a>

### 在 bot 自身进程中使用 SDK

如果 bot 是代码而不是编码 agent，可以跳过 MCP，直接调用这些操作：

```python
from loom import Agent

async with Agent(org="acme", namespace="agent-nightly-triage", agent="nightly-triage") as a:
    hits = await a.memory.search("flaky tests in the ingest suite", top_k=5)
    await a.memory.set(
        "flaky/ingest_retry_test",
        "Times out against the S3 mock.",
        tags=["failure", "ingest"],
    )
    ans = await a.memory.ask("which suites flaked this week?")
```

`Agent` 从 `base_url=` 或 `LOOM_BASE_URL` 读取服务器地址，并依次从 `api_key=`、`LOOM_API_KEY`、`~/.loom/token` 读取令牌。其他入口：

- `agent.tools()` 把 Loom 的动词（`loom_remember`、`loom_recall`、`loom_ask`、`loom_offload`、`loom_fetch`、`loom_note`）作为工具定义交给模型，并提供一个统一的 `execute()`。
- `agent.session(id)` 是 agent 框架所持有的单元：恢复、追加、刷新、关闭。
- LangGraph 图把 `LoomStore`（`loom.integrations.langgraph`）传给 `compile(store=...)`；Strands agent 使用 `LoomMemoryStore`（`loom.integrations.strands`）。
- [`examples/claude_agent_loop.py`](https://github.com/ClickHouse/loom/blob/main/examples/claude_agent_loop.py) 是一个使用 Anthropic SDK 的最小循环（需要 `ANTHROPIC_API_KEY`）。

参见 [Python SDK 参考](https://github.com/ClickHouse/loom/blob/main/docs/reference/python-sdk.md)和[框架集成](https://github.com/ClickHouse/loom/blob/main/docs/features/integrations/frameworks.md)。

<a id="4-decide-what-the-bot-writes"></a>

## 4. 决定 bot 写入什么

插件会自行捕获编码会话，而插件之外的 bot 需要自己决定写入什么。简而言之：记录决策和失败，而不是完整的对话记录；在任务开始前召回一次，而不是每一步都召回；将已完成运行的对话记录交给 `memory.set_from_messages`；并通过 `run.complete(memories_used=[...])` 报告该运行所依赖的内容。[Agent 集成](https://github.com/ClickHouse/loom/blob/main/docs/guides/agent-integration.md#3-decide-what-to-write)对每一项选择都有详细说明。

<a id="5-when-the-bot-is-quiet"></a>

## 5. bot 没有动静时

在 bot 所在的机器上运行 `loom doctor --client`。它会在单独的一行中报告每一种常见原因：

| 现象                      | 原因                                                                              |
| ------------------------- | --------------------------------------------------------------------------------- |
| 读写请求发往了另一台服务器 | `LOOM_BASE_URL` 与 `~/.loom/config.json` 不一致                                   |
| 每次调用都返回 401        | 令牌已过期或已被轮换                                                              |
| 每次写入都返回 403        | 该令牌在 bot 写入的命名空间中只是 `viewer`                                        |
| 捕获的内容始终没有出现    | 后台 flusher 没有成功投递：临时目录中滞留着 `loom-flush-*.json` 文件 |

<a id="related"></a>

## 相关内容

- [令牌与授权](https://github.com/ClickHouse/loom/blob/main/docs/guides/tokens-and-grants.md)
- [agent 集成](https://github.com/ClickHouse/loom/blob/main/docs/guides/agent-integration.md)
- [MCP 客户端](https://github.com/ClickHouse/loom/blob/main/docs/features/integrations/mcp-clients.md)
- [调试请求](https://github.com/ClickHouse/loom/blob/main/docs/guides/debugging.md)
