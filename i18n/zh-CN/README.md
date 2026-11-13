<!-- i18n: source=README.md blob=e1db34acfd382a3c93ed63eca0125785ac13f76f model=claude-sonnet-5-5 -->
> 本页由机器从[英文原文](https://github.com/ClickHouse/loom/blob/main/README.md)翻译，内容以英文原文为准。
> Machine-translated from the [English page](https://github.com/ClickHouse/loom/blob/main/README.md), which is authoritative.

<h1 align="center">ClickHouse Loom</h1>

<p align="center">
  <b>为 AI agent 提供共享记忆、文档搜索和代码智能。</b><br>
  适用于 Claude Code、Codex、Cursor 以及你自己的 agent。
</p>

<p align="center">
  <a href="https://pypi.org/project/clickhouse-loom/"><img alt="PyPI 上的 Python SDK" src="https://img.shields.io/pypi/v/clickhouse-loom?label=python%20sdk"></a>
  <a href="https://www.npmjs.com/package/@clickhouse/loom"><img alt="npm 上的 TypeScript SDK" src="https://img.shields.io/npm/v/%40clickhouse%2Floom?label=typescript%20sdk"></a>
  <a href="https://github.com/ClickHouse/loom/blob/main/LICENSE"><img alt="许可证：Apache-2.0" src="https://img.shields.io/badge/license-Apache--2.0-blue"></a>
  <a href="https://glama.ai/mcp/servers/ClickHouse/loom"><img alt="MCP server 评分" src="https://glama.ai/mcp/servers/ClickHouse/loom/badges/score.svg"></a>
</p>

<p align="center">
  <!-- languages -->
  <a href="../../README.md">English</a> · 简体中文 · <a href="../ja/README.md">日本語</a> · <a href="../ko/README.md">한국어</a> · <a href="../pt-BR/README.md">Português (Brasil)</a> · <a href="../es/README.md">Español</a> · <a href="../ru/README.md">Русский</a>
  <!-- /languages -->
</p>

<p align="center">
  <a href="#what-loom-is-for">Loom 的用途</a> ·
  <a href="#measured-results">基准测试</a> ·
  <a href="#get-started">快速开始</a> ·
  <a href="#why-loom">功能</a> ·
  <a href="#integrations">集成</a> ·
  <a href="https://github.com/ClickHouse/loom/blob/main/docs/index.md">文档</a>
</p>

![Loom 控制台，显示共享的 agent 记忆、一条已固定的事实和被标记的冲突](https://github.com/ClickHouse/loom/raw/main/docs/images/dashboard-memories.png)

Loom 是一个开源、可自托管的 MCP server，为你的 agent 提供长期共享记忆：一个 agent 在会话中学到的内容，下一个 agent 或工具可以找到。

Agent 可以通过 MCP、HTTP、Python 或 TypeScript 连接。你可以使用 Docker 自托管，也可以[使用 Helm chart 部署在 Kubernetes 上](https://github.com/ClickHouse/loom/blob/main/docs/operations/kubernetes.md)；agent 上下文和索引数据存储在 [ClickHouse](docs/use-cases/self-hosted-clickhouse.md) 中，部署时还可以选择[在首次启动时使用 Postgres](https://github.com/ClickHouse/loom/blob/main/docs/operations/postgres.md) 来存放实时记忆和知识状态。控制台将记忆、代码仓库、任务和运行集中在一处。

<a id="what-loom-is-for"></a>

## Loom 的适用场景

- **在多个编码 agent 之间共享的长期记忆**，通过插件在会话开始时加载上下文，并在会话运行过程中捕获会话内容。
  [Agent memory](docs/use-cases/agent-memory.md) ·
  [连接编码 agent](https://github.com/ClickHouse/loom/blob/main/docs/getting-started/connect-a-coding-agent.md)
- **面向 agent 应用的记忆**，按用户划分，通过 Python 或 TypeScript SDK 使用。[你的第一个 agent](https://github.com/ClickHouse/loom/blob/main/docs/getting-started/first-agent.md)
- **多用户 MCP 记忆服务器**，支持命名空间、角色和令牌。
  [多用户模式](docs/getting-started/quickstart.md#start-in-multi-user-mode) ·
  [命名空间与共享](https://github.com/ClickHouse/loom/blob/main/docs/features/namespaces-and-sharing.md)
- **代码仓库的代码索引**，编码 agent 可以查询定义、调用方和变更影响。[代码索引](docs/use-cases/code-intelligence.md)
- **降低编码 agent 的模型成本**，见下方[实测结果](#measured-results)。
  [编码 agent 成本](docs/use-cases/coding-agent-cost.md)
- **带引用的文档搜索（RAG）**，包括由 extractor 服务转换的 PDF、`.docx` 和 `.pptx` 文件。[文档搜索](docs/use-cases/document-search.md)
- **多 agent 工作流的共享上下文**：共享记忆、任务收件箱和决策。[多 agent](docs/use-cases/multi-agent.md)
- **面向自主 agent 的记忆、召回、审计和擦除。**
  [自主 agent](docs/use-cases/autonomous-agents.md)
- **agent 工作记录**，包括可搜索的对话（开启录制时）、可用于恢复工作的会话简报、带有目标、结果以及（若已提供）模型成本的运行记录，以及 PR 和跟踪器历史。
  [运行记录与 PR 历史](docs/use-cases/runs-and-pr-history.md)

所有使用场景，以及何时其他工具更合适：[使用场景](docs/use-cases/index.md)。

Loom 不是用于单个 agent 短期记忆的进程内库，也不是 agent 框架。[Loom 不适用的情况](https://github.com/ClickHouse/loom/blob/main/docs/getting-started/overview.md#when-it-does-not-fit)

<a id="measured-results"></a>

## 测试结果

| 评测项                         | 使用 Loom | 不使用 Loom | 效率提升                           | 范围                               |
| ------------------------------ | --------- | ----------- | ---------------------------------- | ---------------------------------- |
| 编码 Agent 回答质量            | **0.84**  | 0.78        | 模型成本**降低 20%**               | 24 个问题 × 3 次配对运行           |
| 编码 Agent 文件定位            | **96.4%** | 91.4%       | 模型成本**降低 21%**               | 36 个问题 × 10 次配对运行          |
| 记忆与完整历史对比的回答准确率 | **94%**   | 90%         | Reader 提示词 token **减少 91%**   | 100 个配对的 LongMemEval-S 问题    |

两项编码评测均在 ClickHouse 代码库上使用 Claude Code（`claude-sonnet-5`）；
成本是整个会话的模型费用，不包括 Loom 服务端的模型调用、托管和索引费用。回答质量
衡量的是对已验证答案要点的覆盖程度，并对矛盾之处扣分。
文件定位统计的是在三个指定文件中是否包含正确的文件。

记忆一行比较的是检索到的记忆与完整对话历史，二者使用
相同的 `gpt-5` reader。token 节省量不包括记忆构建；94% 与 90% 的
准确率差异仅为描述性结果，并未经过统计学验证。

**另外，Loom 的 `memory.ask` 配置在全部 500 个 LongMemEval-S 问题上的回答准确率为 96.6%。** 这是一项产品评测，使用其自身配置的
回答模型，不同于上文固定 reader 的对比。

结果仅适用于所测试的工作负载。

[评测协议与可复现的产物](https://github.com/ClickHouse/loom/blob/main/docs/benchmarks/results.md) ·
[详细结果](#benchmark-details) ·
[在你的工作负载上评测](https://github.com/ClickHouse/loom/blob/main/docs/getting-started/evaluate.md)

<a id="get-started"></a>

## 快速开始

<a id="1-choose-your-docker-setup"></a>

### 1. 选择 Docker 部署方式

- **个人使用（默认）：** 无需登录，仅限 localhost 访问，使用 `dev/local` 命名空间。一个人可以连接多个 agent。
- **团队使用：** 提供需要认证的账户和命名空间访问控制。
  [首次启动时即进入多用户模式](docs/getting-started/quickstart.md#start-in-multi-user-mode)。

两种模式支持相同的 Loom 功能，包括已配置的 LLM 和 embedding。
单用户模式并不是功能缩减版。你也可以
[在不删除数据的情况下切换现有安装](docs/getting-started/quickstart.md#2-open-the-dashboard)；
并为新账户授予现有命名空间的访问权限。

无需安装任何东西即可试用，请在 codespace 中打开：

[![Open in GitHub Codespaces](https://github.com/codespaces/badge.svg)](https://codespaces.new/ClickHouse/loom?quickstart=1)
（[它运行什么以及谁可以访问](docs/getting-started/quickstart.md#try-it-in-github-codespaces)）

对于默认的个人安装，在已安装 Docker 的情况下：

```bash
curl -fsSL https://github.com/ClickHouse/loom/releases/latest/download/install.sh -o loom-install.sh && sh loom-install.sh && rm loom-install.sh
```

请在你的主目录下运行，或在
[只有你可以写入的其他目录](docs/getting-started/quickstart.md#fast-path-released-image-no-clone)中运行。
安装程序会启动 Loom 和 ClickHouse，应用迁移，并打印 dashboard 的 URL：
**http://localhost:7777/**。镜像包含 server、worker、dashboard，以及
Python、TypeScript 和 Go 编译器索引器；TypeScript 依赖会自动准备。
[快速入门与固定版本发布](docs/getting-started/quickstart.md)

安装位于 `~/.loom`（可用 `LOOM_INSTALL_DIR` 指定其他目录）：其中包括
Compose 文件，以及位于 `~/.loom/.env` 的启动设置。再次运行安装命令
会原地升级，并保留你的 `.env` 设置。

请让单用户模式保持仅限 localhost。如需共享访问，请参阅
[团队部署指南](https://github.com/ClickHouse/loom/blob/main/docs/operations/deployment.md#identity-modes)，包括 TLS 配置。

<a id="2-configure-models-and-api-keys"></a>

### 2. 配置模型和 API key

**默认安装无需 key 和网络即可启动。** 存储、dashboard、
结构化代码索引和语义搜索都可正常工作，使用的是镜像自带的一个小型本地 embedding 模型；
fake LLM 不会生成 AI 回答。

| 组件 | 实际使用时的配置 | 未配置时 |
| ---------- | -------------------------------------------------- | --------------------------------------------------------------------------- |
| Embeddings | 提供方、embedding 模型、维度和 API key。 | `local`（CPU 上的 bge-small）：支持语义检索，但效果不如托管模型。 |
| LLM | 提供方、模型和 API key。 | `fake-echo-1`：没有有意义的提取、摘要或回答。 |

打开 **Settings → Models & keys**，选择两个提供方和模型，输入它们的
key，然后点击 **Test & apply**。无需重启 Docker；更改 embedding
空间会重建现有索引。dashboard 中的 embedding 选项均为托管提供方：
若要保留 `local` 并添加 LLM，请改为在 `~/.loom/.env` 中设置 LLM 相关行
（[Models and keys](https://github.com/ClickHouse/loom/blob/main/docs/getting-started/models-and-keys.md#claude-for-the-llm)）。

**一个 OpenAI API key 即可同时满足两种角色。** Claude 需要另外配置 embedding 提供方。
支持 OpenAI、Gemini 和兼容 OpenAI 的 embedding 端点。你的 agent 的
CLI 登录或订阅不会提供 Loom 所需的提供方 key。

更喜欢用环境变量配置？编辑 `~/.loom/.env`，然后重新创建 server：

```bash
docker compose --project-directory "$HOME/.loom" up -d --force-recreate loom
```

仅提供 key 不会改变已固定的模型（本地 embedder 和 fake LLM）；还需要同时设置
提供方和模型。
dashboard 中保存的设置优先于 `.env`。有关各提供方的配置示例和健康检查，请参阅
[models and API keys](https://github.com/ClickHouse/loom/blob/main/docs/getting-started/models-and-keys.md)。

**其余所有设置都在一个文件中。** [`loom.example.yaml`](https://github.com/ClickHouse/loom/blob/main/loom.example.yaml) 按领域列出
每项设置、其默认值及作用：ClickHouse 连接、
超时和预算、速率限制、worker，以及用于开关功能的 `*_enabled` 开关。
可在 `~/.loom/.env` 中将任意设置写为 `LOOM_<NAME>` 并重新创建
server；挂载配置文件的部署则可以在该文件的副本中设置
（[设置的读取方式](https://github.com/ClickHouse/loom/blob/main/docs/operations/deployment.md#how-settings-are-read)）。标记为
`live` 的设置还可以在 Loom 运行时更改：使用 `loom settings set <name> <value>`，或 Admin → Deployment
settings。`loom settings list --changed` 会显示哪些设置与默认值不同，
以及每个值的来源（[运行时设置](https://github.com/ClickHouse/loom/blob/main/docs/operations/runtime-settings.md)）。

<a id="3-connect-your-agents"></a>

### 3. 连接你的 agent

在运行个人安装的机器上，插件无需 token：

```bash
# Claude Code
claude plugin marketplace add ClickHouse/loom
claude plugin install loom@loom

# Codex
codex plugin marketplace add ClickHouse/loom
codex plugin add loom@loom
```

启动新会话；首个会话会连接到 `http://localhost:7777`。在 Codex 中，请
交互式运行一次 `codex` 并批准插件的 hook；在此之前只有
工具可用。告诉 agent 一些值得保留的内容，然后在下一次会话中询问它。

对于运行个人安装的机器上的 Cursor，下方按钮会添加
[本地 MCP 条目](https://github.com/ClickHouse/loom/blob/main/docs/features/integrations/cursor.md#mcp-tools)，无需 token：

[![Add Loom to Cursor](https://cursor.com/deeplink/mcp-install-dark.svg)](https://cursor.com/en/install-mcp?name=loom&config=eyJ1cmwiOiJodHRwOi8vbG9jYWxob3N0Ojc3NzcvbWNwP3Rvb2xzZXQ9Y29kaW5nIn0%3D)

对于运行个人安装的机器上的 VS Code agent 模式，下方按钮会
添加[本地 MCP 条目](https://github.com/ClickHouse/loom/blob/main/docs/features/integrations/vscode.md#local-single-user-server)，
无需 token：

[![Add Loom to VS Code](https://img.shields.io/badge/VS_Code-Add_Loom-0098FF?logo=visualstudiocode&logoColor=white)](https://vscode.dev/redirect?url=vscode%3Amcp%2Finstall%3F%257B%2522name%2522%253A%2522loom%2522%252C%2522type%2522%253A%2522http%2522%252C%2522url%2522%253A%2522http%253A%252F%252Flocalhost%253A7777%252Fmcp%2522%257D)
[![Add Loom to VS Code Insiders](https://img.shields.io/badge/VS_Code_Insiders-Add_Loom-24bfa5?logo=visualstudiocode&logoColor=white)](https://insiders.vscode.dev/redirect?url=vscode-insiders%3Amcp%2Finstall%3F%257B%2522name%2522%253A%2522loom%2522%252C%2522type%2522%253A%2522http%2522%252C%2522url%2522%253A%2522http%253A%252F%252Flocalhost%253A7777%252Fmcp%2522%257D)

对于团队 server，请向 Claude Code 传入 `--config server_url=… --config token=…`，或
按照
[connect a coding agent](https://github.com/ClickHouse/loom/blob/main/docs/getting-started/connect-a-coding-agent.md) 中的说明将 Codex 指向它。
请在 **Settings → Tokens** 中创建 token。Cursor 和其他 MCP 客户端，以及
[SDK](https://github.com/ClickHouse/loom/blob/main/docs/getting-started/first-agent.md)，也都在该文档中介绍。

<a id="why-loom"></a>

## 为什么选择 Loom

- **可延续的记忆。** 在多个 agent 和会话之间共享决策与发现。每条事实都会记录其写入者,版本历史和冲突审查使修正可追溯,namespace 可以回滚到任意时间点。
  [Memory](https://github.com/ClickHouse/loom/blob/main/docs/concepts/memory.md)
- **有据可查的代码智能。** 查找定义、调用方、变更影响和相关测试;将代码与 commit、pull request 和 issue 关联。结果会标明其所属 revision、源码位置和完整程度。 [Code index](https://github.com/ClickHouse/loom/blob/main/docs/features/code-index/overview.md)
- **有依据的检索。** 搜索有权限访问的记忆、文档、对话和 tracker 记录。回答会引用来源;检索置信度低时可以放弃作答。
  [Retrieval](https://github.com/ClickHouse/loom/blob/main/docs/concepts/retrieval-and-ranking.md)
- **多 agent 交接。** 通过持久化的任务收件箱分配工作,支持租约、重试、审查规则和结果记录。工作流由你的 agent 执行,Loom 负责保留交接过程。 [Task queues](https://github.com/ClickHouse/loom/blob/main/docs/features/tasks-and-decisions.md)
- **对 agent 数据的控制。** 管理 namespace 访问权限、来源追溯、保留策略、审计记录和经过验证的删除。上下文仍可在 ClickHouse 中查询。
  [Security](https://github.com/ClickHouse/loom/blob/main/docs/architecture/security.md)

<a id="integrations"></a>

## 集成

| 集成                                                                                                                                                                                                                                                                                          | 连接内容                                                                      |
| --------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- | ----------------------------------------------------------------------------- |
| [Claude Code](https://github.com/ClickHouse/loom/blob/main/docs/features/integrations/claude-code.md)、[Codex](https://github.com/ClickHouse/loom/blob/main/docs/features/integrations/codex.md)、[Cursor](https://github.com/ClickHouse/loom/blob/main/docs/features/integrations/cursor.md) 和[其他 MCP 客户端](https://github.com/ClickHouse/loom/blob/main/docs/features/integrations/mcp-clients.md)（[所有集成](https://github.com/ClickHouse/loom/blob/main/docs/features/integrations/index.md)） | 在现有的 agent 工作流中提供记忆和代码智能。                 |
| [Python](https://github.com/ClickHouse/loom/blob/main/docs/reference/python-sdk.md)、[TypeScript](https://github.com/ClickHouse/loom/blob/main/docs/reference/typescript-sdk.md) 和 [HTTP](https://github.com/ClickHouse/loom/blob/main/docs/reference/operations.md)                                                                                                                                                         | 你自己的 agent 和应用程序。                                             |
| [Langfuse](https://github.com/ClickHouse/loom/blob/main/docs/features/integrations/langfuse.md)                                                                                                                                                                                                                                          | 请求追踪、模型成本、检索评分和 prompt 版本归因。 |
| [ClickStack / HyperDX](https://github.com/ClickHouse/loom/blob/main/docs/features/integrations/clickstack.md)                                                                                                                                                                                                                            | ClickHouse 中的 OpenTelemetry 追踪、日志和指标。                         |

Langfuse 和 ClickStack 是可选集成，不是 Docker 技术栈中捆绑的服务。

<a id="benchmark-details"></a>

## 基准测试详情

<a id="detailed-retrieval-reader-and-code-search-results"></a>

### 检索、阅读器和代码搜索的详细结果

| 评测                                                  | 结果                                                                                               |
| ----------------------------------------------------- | -------------------------------------------------------------------------------------------------- |
| LongMemEval-S 证据检索（500 个问题）                  | 前 30 条记忆中至少包含一个支持会话的问题占 **98.6%**。                                             |
| LongMemEval-S 固定 `gpt-5` 阅读器（500 个问题）       | 基于前 200 条记忆的回答准确率为 **92.0%**。                                                        |
| LoCoMo（全部 10 段对话）                              | 回答准确率 **89.6%**；对抗性问题的正确弃答率 **90.8%**。                                           |
| MemoryAgentBench 事实问题样本（500 个问题）           | SubEM **99.0%**；生成答案的 token 重叠 F1 为 **95.7%**。                                           |
| 概念性代码搜索（36 个问题）                           | 标准文件召回率：前 5 为 **91.2%**，前 10 为 **97.2%**；MRR 为 **0.91**。                           |
| SWE-bench Verified 文件定位（200 个 issue）           | 前 10 个结果中包含被修改文件的 issue 占比：**82%**，BM25 为 **57%**；前 5 为 **76.5%**。           |
| Tracker 重复项检测（104 对）                          | 已记录的较早重复项排在第一位的比例为 **87.5%**。                                                   |

这些评测衡量的是不同的任务，而不是一个综合得分。SWE-bench 衡量的是文件定位，而不是 issue 的解决。[结果台账](https://github.com/ClickHouse/loom/blob/main/docs/benchmarks/results.md)包含评分定义、小样本评测、较弱和负面的发现，以及完整的评测方案、模型和产物。

[方法论](https://github.com/ClickHouse/loom/blob/main/docs/benchmarks/methodology.md) ·
[覆盖范围与已知缺口](https://github.com/ClickHouse/loom/blob/main/docs/benchmarks/coverage.md) ·
[运行基准测试](https://github.com/ClickHouse/loom/blob/main/docs/benchmarks/running.md)

<a id="documentation-and-contributing"></a>

## 文档与贡献

如需了解共享 agent 上下文、代码智能和带权限控制的检索,请从[文档](https://github.com/ClickHouse/loom/blob/main/docs/index.md)开始。有关部署要求,请参阅[前提条件](https://github.com/ClickHouse/loom/blob/main/docs/getting-started/overview.md#prerequisites)。

[部署](https://github.com/ClickHouse/loom/blob/main/docs/operations/deployment.md) · [常见问题](https://github.com/ClickHouse/loom/blob/main/docs/faq.md) ·
[参与贡献](https://github.com/ClickHouse/loom/blob/main/CONTRIBUTING.md) · [安全问题报告](https://github.com/ClickHouse/loom/blob/main/SECURITY.md) ·
[`llms.txt`](https://github.com/ClickHouse/loom/blob/main/llms.txt)

Apache-2.0 · [许可证](https://github.com/ClickHouse/loom/blob/main/LICENSE) · [声明](https://github.com/ClickHouse/loom/blob/main/NOTICE)
