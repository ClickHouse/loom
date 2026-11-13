<h1 align="center">ClickHouse Loom</h1>

<p align="center">
  <b>为 AI 智能体提供共享记忆、文档检索和代码智能。</b><br>
  适用于 Claude Code、Codex、Cursor 和你自己的智能体。
</p>

<p align="center">
  <a href="./README.md">English</a> · 简体中文
</p>

本页是简短的中文介绍，内容以英文 [README](./README.md) 为准；完整文档为英文，见[文档首页](./docs/index.md)。

Loom 是一个开源、自托管的 MCP 服务器，为你的智能体提供长期的共享记忆：一个智能体在一次会话中学到的内容，下一个智能体或工具都能找到。

智能体通过 MCP、HTTP、Python 或 TypeScript 接入。可以用 Docker 部署，也可以用 Helm chart
[部署在 Kubernetes 上](./docs/operations/kubernetes.md)；智能体上下文和索引数据存储在 ClickHouse
中，也可以在[首次启动时选择 Postgres](./docs/operations/postgres.md) 存储当前的记忆与知识状态。

## 适用场景

- **在多个编程智能体之间共享的长期记忆**：插件在会话开始时加载上下文，并在会话进行中持续记录。[Agent memory](./docs/use-cases/agent-memory.md)
- **智能体应用的记忆**：通过 Python 或 TypeScript SDK 按用户保存。[Your first agent](./docs/getting-started/first-agent.md)
- **多用户 MCP 记忆服务器**：命名空间、角色和 token。[Multi-user mode](./docs/getting-started/quickstart.md#start-in-multi-user-mode)
- **代码仓库索引**：编程智能体可以查询定义、调用方和变更影响。[Code index](./docs/use-cases/code-intelligence.md)
- **降低编程智能体的模型成本**：见下方[实测结果](#实测结果)。[Coding-agent cost](./docs/use-cases/coding-agent-cost.md)
- **带引用的文档检索（RAG）**：包括由提取服务转换的 PDF、`.docx` 和 `.pptx` 文件。[Document search](./docs/use-cases/document-search.md)
- **多智能体工作流中的共享上下文**：共享记忆、任务收件箱和决策。[Multi-agent](./docs/use-cases/multi-agent.md)
- **自主智能体的记忆、召回、审计与擦除。**[Autonomous agents](./docs/use-cases/autonomous-agents.md)
- **智能体工作记录**：可检索的对话记录（开启记录时）、可用于恢复工作的会话摘要、包含目标、结果及模型成本（如有上报）的运行记录，以及 PR 与 issue 历史。[Runs and PR history](./docs/use-cases/runs-and-pr-history.md)

全部场景，以及更适合其他工具的情况：[Use cases](./docs/use-cases/index.md)

Loom 不是供单个智能体在进程内使用的短期记忆库，也不是智能体框架。[何时不适合使用 Loom](./docs/getting-started/overview.md#when-it-does-not-fit)

## 实测结果

| 评测                   | 使用 Loom | 不使用 Loom | 效率提升                                  | 范围                          |
| ---------------------- | --------- | ----------- | ----------------------------------------- | ----------------------------- |
| 编程智能体回答质量     | **0.84**  | 0.78        | 模型成本**低 20%**                        | 24 个问题 × 3 次配对运行      |
| 编程智能体文件定位     | **96.4%** | 91.4%       | 模型成本**低 21%**                        | 36 个问题 × 10 次配对运行     |
| 记忆与完整历史作答对比 | **94%**   | 90%         | 作答模型（reader）的提示 token **少 91%** | 100 个 LongMemEval-S 配对问题 |

两组编程评测都在 ClickHouse 代码库上使用 Claude Code（`claude-sonnet-5`）；成本为整个会话的模型费用，不含 Loom 服务端的模型调用、托管和索引成本。回答质量衡量回答覆盖经核实的参考答案要点的程度，与参考答案矛盾时扣分。文件定位统计智能体最后列出的三个文件中是否包含正确的文件。

记忆这一行用同一个 `gpt-5` 作答模型，比较“检索到的记忆”与“完整会话历史”两种输入。token 节省不含构建记忆的成本；94% 与 90% 的准确率差异只是描述性的，在统计上不显著。

**另外，Loom 的 `memory.ask` 配置在 LongMemEval-S 全部 500 个问题上取得 96.6% 的回答准确率。** 这是一项使用其自身配置的作答模型的产品评测，不同于上面固定作答模型的对比。

结果仅适用于所测试的工作负载。协议、模型与可复现的结果文件见[结果台账](./docs/benchmarks/results.md)。

## 快速开始

默认的个人安装：无需登录，仅允许本机访问，使用 `dev/local` 命名空间；一个人可以接入多个智能体。团队使用（账号与命名空间权限）见[多用户模式](./docs/getting-started/quickstart.md#start-in-multi-user-mode)。

无需在本机安装任何东西，也可以在 Codespaces 中试用：

[![Open in GitHub Codespaces](https://github.com/codespaces/badge.svg)](https://codespaces.new/ClickHouse/loom?quickstart=1)
（[它运行什么、谁能访问](./docs/getting-started/quickstart.md#try-it-in-github-codespaces)）

装好 Docker 后运行：

```bash
curl -fsSL https://github.com/ClickHouse/loom/releases/latest/download/install.sh -o loom-install.sh && sh loom-install.sh && rm loom-install.sh
```

请在主目录或其他只有你有写权限的目录中运行：在共享目录中，他人可能在运行前替换 `loom-install.sh`。安装程序会启动 Loom 和 ClickHouse、执行迁移、在新安装时确认记忆能写入并读回，然后打印控制台地址：**http://localhost:7777/**；在终端中运行时，它还会提议接入找到的编程智能体（Claude Code、Codex、VS Code），新安装时还会打开控制台。

单用户模式请保持仅本机访问；需要共享访问时，按照[团队部署指南](./docs/operations/deployment.md#identity-modes)配置，包括 TLS。

默认安装无需密钥或网络即可启动：存储、控制台、代码结构索引和语义检索都可用，使用镜像自带的小型本地嵌入模型；默认的 `fake-echo-1` LLM 不会生成 AI 回答。在控制台的 **Settings → Models & keys** 中为嵌入模型和 LLM 分别选择提供商与模型、填写密钥，然后点击 **Test & apply**，无需重启 Docker；更换嵌入模型会重建已有索引。一个 OpenAI 密钥可以同时用于两者；在控制台中使用 Claude 作为 LLM 时，还需另选一个嵌入模型提供商。编程智能体的登录或订阅不会为 Loom 提供模型密钥。

控制台的嵌入模型选项均为托管服务；如需保留 `local` 并添加 LLM，请改为在 `~/.loom/.env` 中只设置 LLM 相关的几行，然后重建容器（[Models and keys](./docs/getting-started/models-and-keys.md#claude-for-the-llm)）：

```bash
docker compose --project-directory "$HOME/.loom" up -d --force-recreate loom
```

只填写密钥不会切换已固定的模型（本地嵌入模型和 fake LLM），还需设置提供商与模型；控制台中保存的设置优先于 `.env`。

如果安装程序已接入你的智能体，可跳过下面的命令。在运行个人安装的机器上，插件无需 token：

```bash
# Claude Code
claude plugin marketplace add ClickHouse/loom
claude plugin install loom@loom

# Codex
codex plugin marketplace add ClickHouse/loom
codex plugin add loom@loom
```

然后开启一个新会话，第一次会话会连接 `http://localhost:7777`。在 Codex 中，需要先交互式运行一次 `codex` 并批准插件的 hooks，在此之前只有工具可用。Cursor 和其他 MCP 客户端见 [Connect a coding agent](./docs/getting-started/connect-a-coding-agent.md)；SDK 入门见 [Your first agent](./docs/getting-started/first-agent.md)。

## 链接

[文档首页](./docs/index.md) · [FAQ](./docs/faq.md) · [贡献指南](./CONTRIBUTING.md) ·
[报告安全问题](./SECURITY.md) · [许可证](./LICENSE)
