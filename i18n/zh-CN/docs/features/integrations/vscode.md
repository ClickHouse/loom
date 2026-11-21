<!-- i18n: source=docs/features/integrations/vscode.md blob=3cffbda831d94714b443b5f9d0a7ffe59bbc0ca5 model=claude-sonnet-5-5 -->
> 本页由机器从[英文原文](https://github.com/ClickHouse/loom/blob/main/docs/features/integrations/vscode.md)翻译，内容以英文原文为准。
> Machine-translated from the [English page](https://github.com/ClickHouse/loom/blob/main/docs/features/integrations/vscode.md), which is authoritative.

<a id="vs-code"></a>

# VS Code

VS Code 的 agent 模式（GitHub Copilot）通过 streamable HTTP 以远程 MCP 服务器的方式连接 Loom。Loom 没有 VS Code 扩展：`loom_*` 工具就是集成本身，不提供会话捕获或代码分流；这些功能由 [Claude Code](https://github.com/ClickHouse/loom/blob/main/docs/features/integrations/claude-code.md) 和 [Codex](https://github.com/ClickHouse/loom/blob/main/docs/features/integrations/codex.md) 插件提供。

<a id="local-single-user-server"></a>

## 本地单用户服务器

同一台机器上的单用户服务器接受不带令牌的 MCP 调用。
[将 Loom 添加到 VS Code](https://vscode.dev/redirect?url=vscode%3Amcp%2Finstall%3F%257B%2522name%2522%253A%2522loom%2522%252C%2522type%2522%253A%2522http%2522%252C%2522url%2522%253A%2522http%253A%252F%252Flocalhost%253A7777%252Fmcp%2522%257D) ·
[将 Loom 添加到 VS Code Insiders](https://insiders.vscode.dev/redirect?url=vscode-insiders%3Amcp%2Finstall%3F%257B%2522name%2522%253A%2522loom%2522%252C%2522type%2522%253A%2522http%2522%252C%2522url%2522%253A%2522http%253A%252F%252Flocalhost%253A7777%252Fmcp%2522%257D) 会在用户配置文件的 `mcp.json` 中添加一个 `servers.loom` 条目，其中的 `type` 和 `url` 与控制台及 `loom install --client vscode` 提供给 VS Code 的一致。链接的载荷经 URL 编码后放入 `vscode:mcp/install?<payload>`，内容如下：

```json
{ "name": "loom", "type": "http", "url": "http://localhost:7777/mcp" }
```

这些链接会打开 VS Code 的安装提示。在终端中，`code --add-mcp '{"name":"loom","type":"http","url":"http://localhost:7777/mcp"}'` 的效果相同。使用代码索引的编码会话需要在 `url` 后加上 `?toolset=coding`（参见[选择工具集](https://github.com/ClickHouse/loom/blob/main/docs/features/integrations/mcp-clients.md#choosing-a-toolset)）。

<a id="any-other-server"></a>

## 其他服务器

写入带有令牌的条目：

```bash
loom install --client vscode --server-url https://loom.example.com --token "$LOOM_API_KEY" --toolset coding
```

该命令会把一个 `servers.loom` 条目（`type: "http"`、`url` 和 `Authorization` 请求头）合并到用户配置文件的 `mcp.json` 中，不改动 `settings.json`。如果不想让令牌出现在文件里，可以手动编写条目，使用 VS Code 首次提示输入并保存的 input：

```json
{
  "servers": {
    "loom": {
      "type": "http",
      "url": "https://loom.example.com/mcp?toolset=coding",
      "headers": { "Authorization": "Bearer ${input:loom-token}" }
    }
  },
  "inputs": [
    { "id": "loom-token", "type": "promptString", "password": true, "description": "Loom token" }
  ]
}
```

打开 agent 模式的聊天，确认列出了 `loom_*` 工具；遇到 `401` 时请参见[检查连接](https://github.com/ClickHouse/loom/blob/main/docs/features/integrations/mcp-clients.md#checking-the-connection)。

<a id="agent-instructions"></a>

## agent 指令

把 [agent 指令](https://github.com/ClickHouse/loom/blob/main/docs/features/integrations/agent-instructions.md)块添加到 `.github/copilot-instructions.md`，让 agent 知道何时调用这些工具；如果仓库的 `AGENTS.md` 已包含该块，则无需添加。该块列出的是 `coding` 工具集中的工具：请在 `url` 上使用 `?toolset=coding` 连接；否则，该块中提到但工具列表里没有的工具，按块中所说通过 `loom_op` 运行。

<a id="related"></a>

## 相关内容

- [连接 MCP 客户端](https://github.com/ClickHouse/loom/blob/main/docs/features/integrations/mcp-clients.md)：`loom install`、工具集以及其他编辑器。
- [MCP 参考](https://github.com/ClickHouse/loom/blob/main/docs/reference/mcp.md)：端点、认证模式和工具列表。
- [集成](https://github.com/ClickHouse/loom/blob/main/docs/features/integrations/index.md)：Loom 可连接的所有内容。
