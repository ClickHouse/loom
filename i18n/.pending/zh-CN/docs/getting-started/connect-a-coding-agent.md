<!-- i18n: source=docs/getting-started/connect-a-coding-agent.md blob=4a72e3770686ed00295619940601d7f23baa7988 model=claude-sonnet-5-5 -->
> 本页由机器从[英文原文](https://github.com/ClickHouse/loom/blob/main/docs/getting-started/connect-a-coding-agent.md)翻译，内容以英文原文为准。
> Machine-translated from the [English page](https://github.com/ClickHouse/loom/blob/main/docs/getting-started/connect-a-coding-agent.md), which is authoritative.

<a id="shared-memory-for-claude-code-codex-and-cursor"></a>

# Claude Code、Codex 和 Cursor 的共享记忆

Loom 通过插件或 MCP 工具，为 Claude Code、Codex 和 Cursor 提供共享的长期记忆，这些插件或工具连接到同一台自托管服务器。给每位团队成员一个有权访问团队命名空间的令牌，他们的 agent 就能找到其他人保存的事实。安装下面的插件或 MCP 条目后，下一次会话就可以回忆起 agent 保存的内容。Claude Code 和 Codex 插件会向 Loom 发送哪些内容，请参阅[数据处理](../features/integrations/claude-code.md#data-handling)。对话记录默认关闭；命名空间所有者可以在控制台的 Conversations 页面上通过 **Record conversations** 开关将其打开，也可以使用 `conversation.set_logging`。如需自行运行服务器，请从[快速入门](quickstart.md)开始；如需让你的 agent 安装固定版本并验证其可用，请把最新版本的 `https://github.com/ClickHouse/loom/releases/latest/download/agent-setup.txt` 交给它（[使用你的编码 agent 设置 Loom](https://github.com/ClickHouse/loom/blob/main/docs/getting-started/agent-setup.md)）。

<a id="what-you-need"></a>

## 你需要准备什么

![设置 → 令牌：新建的令牌及其生成的 Claude Code 配置](https://github.com/ClickHouse/loom/raw/main/docs/images/dashboard-connect.png)

- **服务器 URL**，例如 `https://loom.example.com`，或者对于用 `loom up` 启动的堆栈，使用 `http://localhost:7777`。
- **你所在命名空间的 writer 令牌**，但连接同一台机器上的单用户服务器时除外：此时插件以及 [Cursor](../features/integrations/cursor.md#mcp-tools) 和 [VS Code](../features/integrations/vscode.md#local-single-user-server) 的安装链接无需令牌即可连接。可以在控制台的 `<server>/settings` → **令牌** 中创建，也可以请运维人员运行 `loom mint-token --subject you --org <org> --namespaces <namespace> --roles writer`。在开放自助注册的服务器上，Claude Code 插件可以在首次会话时自动为这台机器完成注册。参见[没有凭据时会发生什么](#no-token-yet)。

<a id="claude-code"></a>

<a id="how-do-i-give-claude-code-shared-memory"></a>

## 如何为 Claude Code 提供共享记忆？

使用服务器 URL 和你想共享的命名空间的 writer 令牌安装 Loom 插件。插件会在会话开始时加载相关记忆，并为 agent 提供保存和搜索事实的工具。

```bash
claude plugin marketplace add ClickHouse/loom
claude plugin install loom@loom \
  --config server_url=https://loom.example.com \
  --config token=<writer JWT>
```

两个 `--config` 值都是可选的；如果不提供，Claude Code 会询问一次。启动新会话后，Loom 的会话引导信息会出现在最前面，此后：

- hooks 会向 Loom 发送[数据处理](../features/integrations/claude-code.md#data-handling)中列出的内容；
- `loom-memory`、`loom-code`、`loom-session` 等 skills 会告诉 Claude 何时保存事实、何时搜索，以及如何使用代码索引；
- `loom_search`、`loom_memory_set` 和代码工具可作为 MCP 工具使用。

插件安装的全部内容及其配置方法，请参阅 [Claude Code](../features/integrations/claude-code.md) 页面。

<a id="codex"></a>

<a id="how-do-i-give-codex-shared-memory"></a>

## 如何为 Codex 提供共享记忆？

安装 Loom 插件，将它的 hooks 和 MCP 条目连接到同一个服务器，并使用有权访问共享命名空间的令牌。插件会在会话开始时加载记忆引导内容；它的 hooks 需要下文所述的批准。

```bash
codex plugin marketplace add ClickHouse/loom
codex plugin add loom@loom
```

然后以交互方式运行一次 `codex`，并[批准插件的 hooks](../features/integrations/codex.md#approve-the-hooks)；在此之前，工具可以使用，但不会捕获任何内容。

插件的 MCP 条目指向 `http://localhost:7777`。若要使用其他服务器，请将其 URL 和你的令牌写入 `~/.loom/config.json`，并在 `~/.codex/config.toml` 中覆盖 MCP 条目，具体做法见 [Codex：服务器](../features/integrations/codex.md#the-server)。

Codex 插件提供相同的 hooks 和 skills；差异见 [Codex](../features/integrations/codex.md) 页面。安装插件后，不要再运行 `codex mcp add loom`：同名条目会替换插件自带的条目。

<a id="cursor-and-other-mcp-clients"></a>

<a id="how-do-i-give-cursor-or-another-mcp-client-shared-memory"></a>

## 如何为 Cursor 或其他 MCP 客户端提供共享记忆？

使用共享命名空间的令牌将客户端连接到 Loom 的 MCP 端点，然后添加下方链接的 agent 指令。这些工具用于保存和召回事实；若要捕获会话转录，还需要客户端的 hook 集成。

在 Cursor Teams 或 Enterprise 套餐上，团队管理员可以导入 [Cursor 插件](../features/integrations/cursor.md#plugin)（尚未在 Cursor 中测试），它包含 MCP 条目和 Loom 的 skills。

如果 Cursor 或 VS Code 连接的是同一台机器上的单用户服务器，则不需要令牌：使用 [Cursor](../features/integrations/cursor.md#mcp-tools) 或 [VS Code](../features/integrations/vscode.md#local-single-user-server) 的安装链接。否则，在控制台中打开 **Settings → Tokens**，填写 **New token** 表单（如果已有令牌，请先点击 **New token**），然后点击 **Create token**，再在 **Connect** 下选择你的客户端（Cursor、VS Code、Cline、Windsurf、Zed 或 Claude Desktop）：对话框会显示其配置文件所需的条目，其中已包含令牌。对于其他任何 MCP 客户端，请从 [MCP 客户端](https://github.com/ClickHouse/loom/blob/main/docs/features/integrations/mcp-clients.md)复制条目。如果已安装 `loom` CLI（在仓库克隆目录中运行 `pip install -e .`），也可以用一条命令写入该条目：

```bash
loom install --client cursor --server-url https://loom.example.com --token "$LOOM_API_KEY"
```

之后请重启客户端。`--client` 还支持 `claude-desktop`、`vscode`、`cline`、`windsurf` 和 `zed`；如果不指定，`loom install` 会配置它找到的所有客户端。这些客户端可以使用这些工具；转录捕获需要 hook，[Cursor](../features/integrations/cursor.md) 自带 hook，其他客户端则使用[转录模板](https://github.com/ClickHouse/loom/blob/main/docs/features/integrations/index.md#transcript-capture-hooks)。[MCP 客户端](https://github.com/ClickHouse/loom/blob/main/docs/features/integrations/mcp-clients.md)介绍了工具集以及该命令写入的条目。然后将 [agent 指令](../features/integrations/agent-instructions.md)块添加到编码客户端的规则文件中，让 agent 知道何时调用这些工具；对于 Cursor 以外的客户端，请在条目的 URL 后添加 `?toolset=coding`（使用 `loom install` 时为 `--toolset coding`），以便列出该块中提到的工具。

<a id="check-that-it-works"></a>

## 检查是否正常工作

1. 在 Claude Code 中运行 `/loom:loom-status`。它会报告插件调用的服务器、令牌是否被接受并且能否写入该命名空间，以及任何从未到达服务器的已捕获批次。
2. 告诉 agent 一条需要长期保留的信息，例如“我们的部署窗口只在周二和周四”。在新的会话中提问“我们什么时候可以部署？”。答案来自 Loom。
3. 打开控制台（`<server>/`）。保存的事实就在那里；在对话记录开启期间，该会话的对话也在其中，你可以修正或删除其中任何一项。

如果你安装了 CLI，`loom doctor --client` 会额外检查本地的配置连接。

<a id="when-the-code-index-is-used"></a>

## 何时使用代码索引

只有当会话在已索引仓库的 git 检出目录中启动时，插件才会附加代码索引：

- 它会读取该检出目录的 remote（先是 `origin`，然后是 `upstream`，再是其他 remote），并在服务器已索引的仓库中查找。没有自己索引的 fork 会使用其父仓库的索引。
- 你需要拥有该仓库代码命名空间的读取权限。控制台的 **Repositories** 页面会显示你可以读取的内容，并允许你加入。
- 匹配成功后，会话中的代码问题会限定在该仓库内，对已索引路径的 grep 和整文件读取将由索引应答，内容以其最新已索引的提交为准。
- 没有匹配时（目录不在 git 中、仓库无人索引，或你无法读取），一切保持不变：agent 读取本地文件，记忆照常工作。

若仍要使用某个仓库的索引，请显式指定：`LOOM_CODE_REPO=<owner>/<name>`（例如 `ClickHouse/loom`，当你可读取的仓库中只有一个仓库有这个名称时，也可以直接写 `loom`）、Claude Code 插件的 `code_repo` 选项，或 `~/.loom/config.json` 中的 `"code_repo"`（Codex 会话读取的设置）。它只是后备方案：在任何 git 检出目录之外，或在其仓库没有索引的检出目录中（例如 URL 不同的镜像），primer 会指明该仓库的命名空间，且每个 `loom_code_*` 调用都默认使用它。在已索引的检出目录内，仍以该检出目录自己的索引为准；若要搜索其他仓库，请在调用中传入它的命名空间。`local_first`（`LOOM_CODE_LOCAL_FIRST=on`）会让 grep 和文件读取不使用索引，例如在你处理未提交的更改时。详情：[Claude Code](../features/integrations/claude-code.md#the-code-shunt)。

<a id="no-token-yet"></a>

## 尚无令牌

在以 `LOOM_SELF_SIGNUP=open` 启动的服务器上，第一个发现没有令牌的 Claude Code 会话会为这台机器完成注册。它会把令牌写入 `~/.loom/config.json`（权限为 0600），并把一个认领码写入 `~/.loom/claim-<namespace>.txt`，同时 primer 会说明如何认领该命名空间：认领码只能使用一次，在服务器的 `/oauth/agent-claim` 页面中使用，并设置一个你自己的密码。设置 `LOOM_AUTO_SIGNUP=off` 可以关闭这一行为。如果服务器没有开启自助注册，该会话将没有令牌，primer 会说明如何获取令牌。

<a id="next"></a>

## 后续步骤

- [你的第一个 agent](https://github.com/ClickHouse/loom/blob/main/docs/getting-started/first-agent.md)：使用 SDK 在你自己的代码中访问同样的记忆。
- [Agent 集成](https://github.com/ClickHouse/loom/blob/main/docs/guides/agent-integration.md)：保存什么、何时搜索，以及需要避免的错误。
- [代码索引](../features/code-index/overview.md)：向 Loom 询问服务器已建立索引的代码仓库。
- [故障排查](https://github.com/ClickHouse/loom/blob/main/docs/operations/troubleshooting.md)，从 `/loom:loom-status` 开始。
