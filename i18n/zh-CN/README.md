<!-- i18n: source=README.md blob=46486c03a54d0fc37704491d37179cbb64ba871e model=claude-sonnet-5-5 -->
> 本页由机器从[英文原文](https://github.com/ClickHouse/loom/blob/main/README.md)翻译，内容以英文原文为准。
> Machine-translated from the [English page](https://github.com/ClickHouse/loom/blob/main/README.md), which is authoritative.

<h1 align="center">ClickHouse Loom</h1>

<p align="center">
  <b>为 AI agent 提供共享记忆、文档搜索和代码智能。</b><br>
  适用于 Claude Code、Codex、Cursor 以及你自己的 agent。
</p>

<p align="center">
  <a href="https://pypi.org/project/clickhouse-loom/"><img alt="Python SDK on PyPI" src="https://img.shields.io/pypi/v/clickhouse-loom?label=python%20sdk"></a>
  <a href="https://www.npmjs.com/package/@clickhouse/loom"><img alt="TypeScript SDK on npm" src="https://img.shields.io/npm/v/%40clickhouse%2Floom?label=typescript%20sdk"></a>
  <a href="https://github.com/ClickHouse/loom/blob/main/LICENSE"><img alt="License: Apache-2.0" src="https://img.shields.io/badge/license-Apache--2.0-blue"></a>
  <a href="https://glama.ai/mcp/servers/ClickHouse/loom"><img alt="MCP server score" src="https://glama.ai/mcp/servers/ClickHouse/loom/badges/score.svg"></a>
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

![Loom 控制台，显示共享的 agent 记忆、一条置顶的事实和被标记的冲突](https://github.com/ClickHouse/loom/raw/main/docs/images/dashboard-memories.png)

Loom 是一个开源、可自托管的 MCP server，为你的 agent 提供长期的共享记忆：一个 agent 在会话中学到的内容，下一个 agent 或工具可以找到。

agent 可以通过 MCP、HTTP、Python 或 TypeScript 连接。可以使用 Docker 自托管，也可以[通过 Helm chart 部署在 Kubernetes 上](https://github.com/ClickHouse/loom/blob/main/docs/operations/kubernetes.md)；agent 上下文和索引数据存放在 [ClickHouse](docs/use-cases/self-hosted-clickhouse.md) 中，部署时还可以选择[在首次启动时使用 Postgres](https://github.com/ClickHouse/loom/blob/main/docs/operations/postgres.md) 来存储实时记忆和知识状态。控制台将记忆、代码仓库、任务和运行集中在一个地方。

<a id="what-loom-is-for"></a>

## Loom 的适用场景

- **在多个编码 agent 之间共享的长期记忆**，插件会在会话开始时加载上下文，并在会话运行过程中捕获会话内容。
  [Agent 记忆](docs/use-cases/agent-memory.md) ·
  [连接编码 agent](https://github.com/ClickHouse/loom/blob/main/docs/getting-started/connect-a-coding-agent.md)
- **面向 agent 应用的记忆**，按用户划分，通过 Python 或 TypeScript SDK 使用。[你的第一个 agent](https://github.com/ClickHouse/loom/blob/main/docs/getting-started/first-agent.md)
- **多用户 MCP 记忆服务器**，支持命名空间、角色和令牌。
  [多用户模式](docs/getting-started/quickstart.md#start-in-multi-user-mode) ·
  [命名空间与共享](https://github.com/ClickHouse/loom/blob/main/docs/features/namespaces-and-sharing.md)
- **代码仓库的代码索引**，编码 agent 可以查询定义、调用方和变更影响。[代码索引](docs/use-cases/code-intelligence.md)
- **降低编码 agent 的模型成本**，见下文的[实测结果](#measured-results)。
  [编码 agent 成本](docs/use-cases/coding-agent-cost.md)
- **带引用的文档搜索（RAG）**，包括由 extractor 服务转换的 PDF、`.docx` 和 `.pptx` 文件。[文档搜索](docs/use-cases/document-search.md)
- **多 agent 工作流的共享上下文**：共享记忆、任务收件箱和决策。[多 agent](docs/use-cases/multi-agent.md)
- **为自主 agent 提供记忆、召回、审计和擦除。**
  [自主 agent](docs/use-cases/autonomous-agents.md)
- **agent 工作记录**，包括可搜索的对话（开启记录时）、可用于恢复工作的会话简报、带有目标、结果以及（如有提供）模型成本的运行记录，以及 PR 和跟踪器历史。
  [运行记录与 PR 历史](docs/use-cases/runs-and-pr-history.md)

所有使用场景，以及何时其他工具更合适：[使用场景](docs/use-cases/index.md)。

Loom 不是用于单个 agent 短期记忆的进程内库，也不是 agent 框架。[Loom 不适用的情况](https://github.com/ClickHouse/loom/blob/main/docs/getting-started/overview.md#when-it-does-not-fit)

<a id="measured-results"></a>

## 实测结果

| 评测                     | 使用 Loom | 不使用 Loom | 效率提升                    | 范围                              |
| ------------------------------ | --------- | ------------ | ---------------------------------- | ---------------------------------- |
| 编码 agent 回答质量    | **0.84**  | 0.78         | 模型成本**降低 20%**       | 24 个问题 × 3 次配对运行       |
| 编码 agent 文件定位 | **96.4%** | 91.4%        | 模型成本**降低 21%**       | 36 个问题 × 10 次配对运行      |
| 记忆与完整历史记录的回答对比 | **94%**   | 90%          | reader 提示词 token **减少 91%** | 100 个配对的 LongMemEval-S 问题 |

两种编码测试均在 ClickHouse 代码库上使用 Claude Code（`claude-sonnet-5`）；成本为整个会话的模型费用，不包括 Loom 服务端的模型调用、托管和索引。回答质量衡量的是对已验证答案要点的覆盖程度，并对矛盾之处扣分。文件定位统计的是正确文件是否在 agent 于回答末尾被要求给出的至多三个文件之中。

记忆这一行使用同一个 `gpt-5` reader，对比检索到的记忆与完整对话历史。token 节省不包括记忆的构建；94% 对 90% 的准确率差异仅为描述性结果，并未经统计学证实。

**另外，Loom 的 `memory.ask` 配置在全部 500 个 LongMemEval-S 问题上取得了 96.6% 的回答准确率。** 这是一项使用其自身配置的回答模型的产品评测，不同于上述固定 reader 的对比。

结果仅适用于所测试的工作负载。

[评测方案与可复现的产物](https://github.com/ClickHouse/loom/blob/main/docs/benchmarks/results.md) ·
[详细结果](#benchmark-details) ·
[在你的工作负载上评测](https://github.com/ClickHouse/loom/blob/main/docs/getting-started/evaluate.md)

<a id="get-started"></a>

## 快速开始

<a id="1-choose-your-docker-setup"></a>

### 1. 选择 Docker 部署方式

- **个人使用（默认）：** 无需登录，仅限 localhost 访问，使用 `dev/local` 命名空间。一个人可以连接多个 agent。
- **团队使用：** 提供经过认证的账户和命名空间访问控制。[首次启动即使用多用户模式](docs/getting-started/quickstart.md#start-in-multi-user-mode)。

两种模式支持相同的 Loom 功能，包括已配置的 LLM 和 embedding。单用户模式并不是功能缩减版。你也可以[在不删除数据的情况下切换现有安装](docs/getting-started/quickstart.md#2-open-the-dashboard)；再为新账户授予现有命名空间的访问权限。

如果想在不安装任何东西的情况下试用，可以在 codespace 中打开：

[![Open in GitHub Codespaces](https://github.com/codespaces/badge.svg)](https://codespaces.new/ClickHouse/loom?quickstart=1)
（[它运行什么，谁可以访问](docs/getting-started/quickstart.md#try-it-in-github-codespaces)）

默认的个人安装需要已安装 Docker（在 Windows 上，需在 `docker` 可用的 WSL 2 终端中操作：启用 WSL 集成的 Docker Desktop，或安装在该发行版内的 Docker Engine）：

```bash
curl -fsSL https://github.com/ClickHouse/loom/releases/latest/download/install.sh -o loom-install.sh && sh loom-install.sh && rm loom-install.sh
```

请在你的主目录或其他[只有你可以写入的目录](docs/getting-started/quickstart.md#fast-path-released-image-no-clone)中运行。安装程序会启动 Loom 和 ClickHouse，执行迁移，并打印控制台 URL：**http://localhost:7777/**；全新安装时，它还会检查记忆能否保存并读回。在终端中，它还会提示连接它找到的 coding agent（Claude Code、Codex、VS Code），全新安装时会打开控制台。镜像包含服务器、workers、控制台，以及 Python、TypeScript 和 Go 编译器索引器；TypeScript 依赖会自动准备。[快速入门和固定版本发布](docs/getting-started/quickstart.md)

安装位于 `~/.loom`（可用 `LOOM_INSTALL_DIR` 指定其他目录）：其中包含 Compose 文件，以及 `~/.loom/.env` 中的启动设置。再次运行安装命令会原地升级，并保留你的 `.env` 设置。

请让单用户模式保持仅限 localhost。如需共享访问，请参阅[团队部署指南](https://github.com/ClickHouse/loom/blob/main/docs/operations/deployment.md#identity-modes)，包括 TLS。

<a id="2-configure-models-and-api-keys"></a>

### 2. 配置模型和 API 密钥

**默认安装无需密钥或网络即可启动。** 存储、控制台、结构化代码索引和语义搜索都可以工作，语义搜索使用镜像自带的小型本地 embedding 模型；fake LLM 不会给出 AI 生成的回答。

| 组件       | 实际使用时的配置                                   | 未配置时                                                                    |
| ---------- | -------------------------------------------------- | --------------------------------------------------------------------------- |
| Embeddings | Provider、embedding 模型、维度和 API 密钥。        | `local`（CPU 上的 bge-small）：支持语义检索，但弱于托管模型。               |
| LLM        | Provider、模型和 API 密钥。                        | `fake-echo-1`：没有有意义的提取、摘要或回答。                               |

打开 **Settings → Models & keys**，选择两个 provider 和模型，输入它们的密钥，然后点击 **Test & apply**。无需重启 Docker；更改 embedding 空间会重建现有索引。控制台中的 embedding 选项均为托管 provider：如果要保留 `local` 并添加 LLM，请改为在 `~/.loom/.env` 中设置 LLM 相关行（[模型和密钥](https://github.com/ClickHouse/loom/blob/main/docs/getting-started/models-and-keys.md#claude-for-the-llm)）。

**一个 OpenAI API 密钥可以同时用于两种角色。** Claude 需要单独的 embedding provider。支持 OpenAI、Gemini 和兼容 OpenAI 的 embedding 端点。你的 agent 的 CLI 登录或订阅不能提供 Loom 的 provider 密钥。

更喜欢用环境变量配置？编辑 `~/.loom/.env`，然后重新创建服务器：

```bash
docker compose --project-directory "$HOME/.loom" up -d --force-recreate loom
```

仅有密钥不会改变已固定的模型（本地 embedder 和 fake LLM）；还需要同时设置 provider 和模型。控制台中保存的设置优先于 `.env`。provider 配置示例和健康检查请参阅[模型和 API 密钥](https://github.com/ClickHouse/loom/blob/main/docs/getting-started/models-and-keys.md)。

**其余设置都在一个文件中。** [`loom.example.yaml`](https://github.com/ClickHouse/loom/blob/main/loom.example.yaml) 按领域列出了每项设置及其默认值和作用：ClickHouse 连接、超时和预算、速率限制、workers，以及用于开关功能的 `*_enabled` 选项。可在 `~/.loom/.env` 中以 `LOOM_<NAME>` 的形式设置其中任意一项，然后重新创建服务器；挂载了配置文件的部署可以改为在该文件的副本中设置（[设置的读取方式](https://github.com/ClickHouse/loom/blob/main/docs/operations/deployment.md#how-settings-are-read)）。标记为 `live` 的设置还可以在 Loom 运行时更改：使用 `loom settings set <name> <value>`，或 Admin → Deployment settings。`loom settings list --changed` 会显示与默认值不同的设置，以及每个值的来源（[运行时设置](https://github.com/ClickHouse/loom/blob/main/docs/operations/runtime-settings.md)）。

<a id="3-connect-your-agents"></a>

### 3. 连接你的 agent

如果安装程序已经连接了你的 agent，请跳过下面的命令。在运行个人安装的机器上，插件不需要令牌：

```bash
# Claude Code
claude plugin marketplace add ClickHouse/loom
claude plugin install loom@loom

# Codex
codex plugin marketplace add ClickHouse/loom
codex plugin add loom@loom
```

启动一个新会话；第一个会话会连接到 `http://localhost:7777`。在 Codex 中，请先以交互方式运行一次 `codex` 并批准插件的 hooks；在此之前只有工具可用。告诉 agent 一些值得保留的内容，然后在下一个会话中询问它。

对于在运行个人安装的机器上使用的 Cursor，下面的按钮会添加[本地 MCP 条目](https://github.com/ClickHouse/loom/blob/main/docs/features/integrations/cursor.md#mcp-tools)，无需令牌：

[![Add Loom to Cursor](https://cursor.com/deeplink/mcp-install-dark.svg)](https://cursor.com/en/install-mcp?name=loom&config=eyJ1cmwiOiJodHRwOi8vbG9jYWxob3N0Ojc3NzcvbWNwP3Rvb2xzZXQ9Y29kaW5nIn0%3D)

对于在运行个人安装的机器上使用的 VS Code agent 模式，下面的按钮会添加[本地 MCP 条目](https://github.com/ClickHouse/loom/blob/main/docs/features/integrations/vscode.md#local-single-user-server)，无需令牌：

[![Add Loom to VS Code](https://img.shields.io/badge/VS_Code-Add_Loom-0098FF?logo=visualstudiocode&logoColor=white)](https://vscode.dev/redirect?url=vscode%3Amcp%2Finstall%3F%257B%2522name%2522%253A%2522loom%2522%252C%2522type%2522%253A%2522http%2522%252C%2522url%2522%253A%2522http%253A%252F%252Flocalhost%253A7777%252Fmcp%2522%257D)
[![Add Loom to VS Code Insiders](https://img.shields.io/badge/VS_Code_Insiders-Add_Loom-24bfa5?logo=visualstudiocode&logoColor=white)](https://insiders.vscode.dev/redirect?url=vscode-insiders%3Amcp%2Finstall%3F%257B%2522name%2522%253A%2522loom%2522%252C%2522type%2522%253A%2522http%2522%252C%2522url%2522%253A%2522http%253A%252F%252Flocalhost%253A7777%252Fmcp%2522%257D)

对于团队服务器，请向 Claude Code 传入 `--config server_url=… --config token=…`，或按照[连接 coding agent](https://github.com/ClickHouse/loom/blob/main/docs/getting-started/connect-a-coding-agent.md)中的说明让 Codex 指向它。请在 **Settings → Tokens** 中创建令牌。该文档也涵盖了 Cursor 和其他 MCP 客户端，以及 [SDK](https://github.com/ClickHouse/loom/blob/main/docs/getting-started/first-agent.md)。

<a id="why-loom"></a>

## 为什么选择 Loom

- **可延续的记忆。** 在多个 agent 和会话之间共享决策与发现。每条事实都会记录写入者，版本历史和冲突审查使修正可追溯，命名空间可以回滚到任意时间点。
  [记忆](https://github.com/ClickHouse/loom/blob/main/docs/concepts/memory.md)
- **有据可依的代码智能。** 查找定义、调用方、变更影响和相关测试；将代码与提交、拉取请求和 issue 关联起来。结果会标明其所属修订版本、源码位置和完整程度。[代码索引](https://github.com/ClickHouse/loom/blob/main/docs/features/code-index/overview.md)
- **有依据的检索。** 搜索有权限访问的记忆、文档、对话和跟踪器记录。答案会引用其来源；置信度较低时，检索可以放弃作答。
  [检索](https://github.com/ClickHouse/loom/blob/main/docs/concepts/retrieval-and-ranking.md)
- **多 agent 交接。** 通过持久的任务收件箱分派工作，支持租约、重试、审查规则和结果记录。由你的 agent 执行工作流，Loom 负责保存交接过程。[任务队列](https://github.com/ClickHouse/loom/blob/main/docs/features/tasks-and-decisions.md)
- **掌控 agent 数据。** 管理命名空间访问、来源追溯、保留策略、审计记录和经过验证的删除。上下文仍可在 ClickHouse 中查询。
  [安全](https://github.com/ClickHouse/loom/blob/main/docs/architecture/security.md)

<a id="integrations"></a>

## 集成

| 集成                                                                                                                                                                                                                                                                                   | 连接内容                                                              |
| --------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- | ----------------------------------------------------------------------------- |
| [Claude Code](https://github.com/ClickHouse/loom/blob/main/docs/features/integrations/claude-code.md)、[Codex](https://github.com/ClickHouse/loom/blob/main/docs/features/integrations/codex.md)、[Cursor](https://github.com/ClickHouse/loom/blob/main/docs/features/integrations/cursor.md) 和[其他 MCP 客户端](https://github.com/ClickHouse/loom/blob/main/docs/features/integrations/mcp-clients.md)（[所有集成](https://github.com/ClickHouse/loom/blob/main/docs/features/integrations/index.md)） | 在现有的 agent 工作流中提供记忆和代码智能。                 |
| [Python](https://github.com/ClickHouse/loom/blob/main/docs/reference/python-sdk.md)、[TypeScript](https://github.com/ClickHouse/loom/blob/main/docs/reference/typescript-sdk.md) 和 [HTTP](https://github.com/ClickHouse/loom/blob/main/docs/reference/operations.md)                                                                                                                                                         | 你自己的 agent 和应用程序。                                             |
| [Langfuse](https://github.com/ClickHouse/loom/blob/main/docs/features/integrations/langfuse.md)                                                                                                                                                                                                                                          | 请求追踪、模型成本、检索得分和提示词版本归因。 |
| [ClickStack / HyperDX](https://github.com/ClickHouse/loom/blob/main/docs/features/integrations/clickstack.md)                                                                                                                                                                                                                            | ClickHouse 中的 OpenTelemetry 追踪、日志和指标。                         |

Langfuse 和 ClickStack 是可选集成，并未内置在 Docker 技术栈中。

<a id="benchmark-details"></a>

## 基准测试详情

<a id="detailed-retrieval-reader-and-code-search-results"></a>

### 检索、阅读器与代码搜索的详细结果

| 评测                                                  | 结果                                                                                               |
| ----------------------------------------------------- | -------------------------------------------------------------------------------------------------- |
| LongMemEval-S 证据检索（500 个问题）                  | 前 30 条记忆中至少包含一个支持会话的问题占 **98.6%**。                                             |
| LongMemEval-S 固定 `gpt-5` 阅读器（500 个问题）       | 基于前 200 条记忆，回答准确率为 **92.0%**。                                                        |
| LoCoMo（全部 10 段对话）                              | 回答准确率 **89.6%**；对抗性问题的正确弃答率为 **90.8%**。                                         |
| MemoryAgentBench 事实问题样本（500 个问题）           | SubEM 为 **99.0%**；生成答案的 token 重叠 F1 为 **95.7%**。                                        |
| 概念性代码搜索（36 个问题）                           | 标准文件召回率：前 5 为 **91.2%**，前 10 为 **97.2%**；MRR 为 **0.91**。                           |
| SWE-bench Verified 文件定位（200 个 issue）           | 前 10 个结果中包含被修改文件的 issue：**82%**，BM25 为 **57%**；前 5 个结果中为 **76.5%**。        |
| 跟踪器重复检测（104 对）                              | 已记录的较早重复项排在第一位的比例为 **87.5%**。                                                   |

这些指标衡量的是不同的任务，而不是一个综合分数。SWE-bench 衡量的是文件定位，而不是 issue 的解决。[结果台账](https://github.com/ClickHouse/loom/blob/main/docs/benchmarks/results.md)包含评分定义、小样本评测、较弱和负面的结果，以及完整的测试协议、模型和产物。

[方法论](https://github.com/ClickHouse/loom/blob/main/docs/benchmarks/methodology.md) ·
[覆盖范围与已知不足](https://github.com/ClickHouse/loom/blob/main/docs/benchmarks/coverage.md) ·
[运行基准测试](https://github.com/ClickHouse/loom/blob/main/docs/benchmarks/running.md)

<a id="documentation-and-contributing"></a>

## 文档与贡献

如需了解共享 agent 上下文、代码智能和带权限的检索，请从[文档](https://github.com/ClickHouse/loom/blob/main/docs/index.md)开始。部署要求请参阅[先决条件](https://github.com/ClickHouse/loom/blob/main/docs/getting-started/overview.md#prerequisites)。

[部署](https://github.com/ClickHouse/loom/blob/main/docs/operations/deployment.md) · [常见问题](https://github.com/ClickHouse/loom/blob/main/docs/faq.md) ·
[贡献指南](https://github.com/ClickHouse/loom/blob/main/CONTRIBUTING.md) · [安全问题报告](https://github.com/ClickHouse/loom/blob/main/SECURITY.md) ·
[`llms.txt`](https://github.com/ClickHouse/loom/blob/main/llms.txt)

Apache-2.0 · [许可证](https://github.com/ClickHouse/loom/blob/main/LICENSE) · [声明](https://github.com/ClickHouse/loom/blob/main/NOTICE)
