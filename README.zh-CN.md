<h1 align="center">Loom</h1>

<p align="center">
  <b>为 AI 智能体提供共享记忆、文档检索和代码智能。</b><br>
  适用于 Claude Code、Codex、Cursor 和你自己的智能体。
</p>

<p align="center">
  <a href="./README.md">English</a> · 简体中文
</p>

本页是简短的中文介绍。完整文档为英文，见 [README](./README.md) 和 [文档首页](./docs/index.md)。

Loom 是一个开源（Apache-2.0）、自托管的 MCP 服务器和 HTTP API，为智能体提供长期的共享记忆：一个智能体在一次会话中学到的内容，下一次会话、另一个智能体或团队成员都能找到。智能体通过 MCP、HTTP、Python 或 TypeScript SDK 接入。可以用 Docker 部署，也可以用 Helm chart 部署在 Kubernetes 上；数据存储在 ClickHouse 中，也可以在首次启动时选择用 Postgres 存储实时的记忆与知识状态。

## 适用场景

| 需求                                                      | 文档（英文）                                                            |
| --------------------------------------------------------- | ----------------------------------------------------------------------- |
| 智能体的长期记忆与共享记忆（MCP 记忆服务器）              | [Agent memory](./docs/use-cases/agent-memory.md)                        |
| 文档检索与 RAG，带引用，支持 PDF、`.docx`、`.pptx`        | [Document search](./docs/use-cases/document-search.md)                  |
| 面向编程智能体的代码索引：定义、调用方、变更影响、PR 历史 | [Code index](./docs/use-cases/code-intelligence.md)                     |
| 降低编程智能体的模型成本                                  | [Coding-agent cost](./docs/use-cases/coding-agent-cost.md)              |
| 多智能体工作流中的共享上下文：共享记忆、任务收件箱        | [Multi-agent](./docs/use-cases/multi-agent.md)                          |
| 自主智能体的记忆、召回、审计与删除                        | [Autonomous agents](./docs/use-cases/autonomous-agents.md)              |
| 智能体运行记录、会话、PR 与工单历史                       | [Runs and PR history](./docs/use-cases/runs-and-pr-history.md)          |
| 基于 ClickHouse 自托管                                    | [Self-hosted on ClickHouse](./docs/use-cases/self-hosted-clickhouse.md) |

Loom 不是供单个智能体在进程内使用的短期记忆库，也不是智能体框架。
[何时不适合使用 Loom](./docs/getting-started/overview.md#when-it-does-not-fit)

## 实测结果

| 评测               | 使用 Loom | 不使用 Loom | 范围                                                     |
| ------------------ | --------- | ----------- | -------------------------------------------------------- |
| 编程智能体回答质量 | **0.84**  | 0.78        | 24 个问题 × 3 次配对运行，模型成本低 20%                 |
| 编程智能体文件定位 | **96.4%** | 91.4%       | 36 个问题 × 10 次配对运行，模型成本低 21%                |
| 记忆与完整历史对比 | **94%**   | 90%         | 100 道 LongMemEval-S 配对题，阅读模型的提示 token 少 91% |

编程评测使用 Claude Code（`claude-sonnet-5`），在 ClickHouse 代码库上进行；成本为整个会话的模型费用，不含 Loom 服务端的模型调用、托管和索引成本。记忆评测的 token 节省不含构建记忆的成本，94% 与 90% 的准确率差异在统计上不显著。结果仅适用于所测试的工作负载。详情见 [Measured results](./README.md#measured-results)。

## 快速开始

装好 Docker 后，默认的个人安装（无需登录，仅本机访问）：

```bash
curl -fsSL https://github.com/ClickHouse/loom/releases/latest/download/install.sh -o loom-install.sh && sh loom-install.sh && rm loom-install.sh
```

安装程序会启动 Loom 和 ClickHouse 并打印控制台地址：**http://localhost:7777/**。团队使用（账号与命名空间权限）见 [Quickstart](./docs/getting-started/quickstart.md#start-in-multi-user-mode)。模型与 API 密钥的配置见 [Models and keys](./docs/getting-started/models-and-keys.md)。

接入编程智能体（个人安装在本机时无需 token）：

```bash
# Claude Code
claude plugin marketplace add ClickHouse/loom
claude plugin install loom@loom

# Codex
codex plugin marketplace add ClickHouse/loom
codex plugin add loom@loom
```

Cursor 和其他 MCP 客户端见 [Connect a coding agent](./docs/getting-started/connect-a-coding-agent.md)；SDK 入门见 [Your first agent](./docs/getting-started/first-agent.md)。

## 链接

[文档首页](./docs/index.md) · [FAQ](./docs/faq.md) · [贡献指南](./CONTRIBUTING.md) · [安全报告](./SECURITY.md) · [许可证](./LICENSE)
