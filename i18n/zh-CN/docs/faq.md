<!-- i18n: source=docs/faq.md blob=d1e8cdcb41e8e749ecd46d194697f615642a3ca8 model=claude-sonnet-5-5 -->
> 本页由机器从[英文原文](https://github.com/ClickHouse/loom/blob/main/docs/faq.md)翻译，内容以英文原文为准。
> Machine-translated from the [English page](https://github.com/ClickHouse/loom/blob/main/docs/faq.md), which is authoritative.

<a id="frequently-asked-questions"></a>

# 常见问题

针对大家最先提出的问题给出简短回答。每个回答都链接到对应的详细说明页面。

<a id="using-it"></a>

## 使用

<a id="what-is-the-fastest-way-to-try-loom"></a>

### 试用 Loom 最快的方式是什么？

如果已经有 Loom 服务器，安装 Claude Code 或 Codex 插件并指向该服务器即可：[连接编码 agent](https://github.com/ClickHouse/loom/blob/main/docs/getting-started/connect-a-coding-agent.md)。要在笔记本电脑上运行服务器，`loom up` 几分钟即可启动一个：[快速入门](getting-started/quickstart.md)。

<a id="how-do-i-give-claude-code-codex-or-cursor-long-term-memory"></a>

### 如何为 Claude Code、Codex 或 Cursor 提供长期记忆？

使用 Claude Code 或 Codex 插件获取会话启动时的上下文并进行捕获，或者用 `loom install --client <name>` 连接任何其他 MCP 客户端（参见 [MCP 客户端](https://github.com/ClickHouse/loom/blob/main/docs/features/integrations/mcp-clients.md)）。通过[快速入门](getting-started/quickstart.md)启动服务器。Cursor 会获得 MCP 条目以及 `loom` 和 `loom-code` skill（没有用于会话启动上下文的 hook；尚未在 Cursor 中测试）；对话记录的捕获来自单独的 `sessionEnd` hook。参见[连接编码 agent](https://github.com/ClickHouse/loom/blob/main/docs/getting-started/connect-a-coding-agent.md)和 [Cursor](https://github.com/ClickHouse/loom/blob/main/docs/features/integrations/cursor.md)。

<a id="why-are-my-search-results-nonsense"></a>

### 为什么我的搜索结果毫无意义？

该安装运行的是假 embedder：它的 `.env`（某个检出目录中的，或 `~/.loom/.env`）指定了 `fake-ngram-*`，其向量会让检索失去意义。切换它的 embedder，已存储的内容会被重新嵌入（[模型与密钥](https://github.com/ClickHouse/loom/blob/main/docs/getting-started/models-and-keys.md)）；全新安装的发布版技术栈已经使用本地模型进行嵌入。对于大量换种说法的问题，托管 embedder 的检索效果更好（[提供方](https://github.com/ClickHouse/loom/blob/main/docs/operations/deployment.md#providers)）。

<a id="why-does-memorysearch-say-abstain-instead-of-returning-something"></a>

### 为什么 `memory.search` 返回 `abstain` 而不是返回一些结果？

没有任何命中项超过针对你的 embedder 校准的相似度下限，所以 Loom 会表示不知道，而不是交出最接近的弱匹配。原因包含在响应中。参见[检索与排序](https://github.com/ClickHouse/loom/blob/main/docs/concepts/retrieval-and-ranking.md)。

<a id="why-was-my-write-flagged-as-a-conflict-or-refused"></a>

### 为什么我的写入被标记为冲突或被拒绝？

与已有记忆含义相近的写入会被标记为 `conflicted` 并与其关联；与已固定记忆相近的写入会被以 `409 PINNED_CONFLICT` 拒绝；匹配黑名单模式的写入会收到 `422 BLACKLISTED`。可在控制台的审核队列中或通过 `conflicts.resolve` 解决冲突。参见[冲突与拒绝](https://github.com/ClickHouse/loom/blob/main/docs/features/memory/conflicts-and-refusal.md)。

<a id="can-i-use-loom-just-for-myself"></a>

### 我可以只给自己使用 Loom 吗？

可以。运行[本地技术栈](getting-started/quickstart.md)，它无需登录，且只有一个命名空间，然后用[插件或 `loom install`](https://github.com/ClickHouse/loom/blob/main/docs/getting-started/connect-a-coding-agent.md)连接你的 agent。在启用登录的服务器上，每个人都有一个 `personal-<slug>` 命名空间，其 MCP 客户端默认写入该命名空间，除非获得授权，其他人无法读取。这样，从 Claude Code 保存的记忆在 Codex、Cursor 和控制台中也能看到。

<a id="can-two-agents-see-each-others-memories"></a>

### 两个 agent 能看到彼此的记忆吗？

命名空间的每个成员都可以读取其中的行。每个 agent 写入自己的层，管理员可以用 `memory.promote` 将记忆复制到该命名空间的共享层。一个令牌可以读取多个命名空间，同时只写入一个。参见[身份与命名空间](https://github.com/ClickHouse/loom/blob/main/docs/concepts/identities-and-namespaces.md)。

<a id="when-should-i-use-knowledge-documents-instead-of-memories"></a>

### 什么时候应该使用知识文档而不是记忆？

一条记忆是一个可以修订的简短陈述。文档、长页面或超过几千字节的内容，应通过 `knowledge.add` 放入知识平面，它会对内容分块并嵌入。参见[记忆](https://github.com/ClickHouse/loom/blob/main/docs/concepts/memory.md#what-goes-where)和[知识文档](https://github.com/ClickHouse/loom/blob/main/docs/features/knowledge.md)。

<a id="how-do-i-make-loom-forget-something"></a>

### 如何让 Loom 忘记某些内容？

`memory.delete` 会为一条记忆打上删除标记（在宽限期内可恢复）；`erasure.request` 会删除某个用户或命名空间拥有的全部内容，涉及所有表，并验证没有任何残留。除非运维人员设置了保留期，否则 Loom 不会按自己的计划删除任何内容。参见[保留与擦除](https://github.com/ClickHouse/loom/blob/main/docs/features/memory/retention-and-erasure.md)。

<a id="how-do-i-find-out-why-a-result-ranked-where-it-did"></a>

### 如何查明某条结果为什么排在当前位置？

调用 `memory.search` 并设置 `explain=true`：每个命中项的 `score_components` 包含排序信号；经过 reranker 之后，还包含它的位移（`rerank_from`、`rerank_pos`、`rerank_delta`）；当 reranker 改变了顺序时，还包含其自身的融合得分（`rank_score_pre_rerank`）。`memory.search_trace` 只对向量路径打分。参见[搜索与问答](https://github.com/ClickHouse/loom/blob/main/docs/features/memory/search-and-ask.md)。

<a id="can-loom-answer-questions-about-my-code"></a>

### Loom 能回答关于我的代码的问题吗？

可以，适用于服务器已建立索引的仓库：`code.search`、`code.symbol`、`code.callers`、`code.impact` 等，每个答案都会说明由哪个提交和哪个索引层级支撑。参见[代码索引](https://github.com/ClickHouse/loom/blob/main/docs/features/code-index/overview.md)和[添加仓库](https://github.com/ClickHouse/loom/blob/main/docs/features/code-index/repositories.md)。

<a id="why-doesnt-my-coding-agent-use-the-code-index"></a>

### 为什么我的编码 agent 不使用代码索引？

只有当会话在某个 git 检出目录中启动，且其远程是你有权读取的已索引仓库时，插件才会附加该索引。请从该仓库的检出目录启动会话，确认该仓库已列在控制台的 **Repositories** 下（`<server>/repositories`），并参见[何时使用代码索引](https://github.com/ClickHouse/loom/blob/main/docs/getting-started/connect-a-coding-agent.md#when-the-code-index-is-used)。

<a id="running-it"></a>

## 运行

<a id="do-i-need-clickhouse-cloud"></a>

### 我需要 ClickHouse Cloud 吗？

不需要。Loom 也可以运行在 ClickHouse OSS 上，笔记本本地栈会在 Docker 中启动一个。由于单节点 OSS 没有复制和时间点恢复功能，Cloud 是预期的生产环境方案。参见[部署](https://github.com/ClickHouse/loom/blob/main/docs/operations/deployment.md)。

<a id="can-i-use-anthropic-or-gemini-models"></a>

### 我可以使用 Anthropic 或 Gemini 的模型吗？

可以。LLM 提供方可以是 OpenAI 或 Anthropic，嵌入（embedding）可以来自 OpenAI、Gemini 或本地 CPU 模型。更换嵌入模型会改变向量空间，因此之后需要运行 `loom reembed`。参见 [LLM 与嵌入](architecture/llm-architecture.md)。

<a id="how-do-i-give-someone-access"></a>

### 如何为他人授予访问权限？

生成一个令牌（`loom mint-token`，或在控制台的 Settings → Tokens 中生成），并在某个命名空间上授予角色。参见[令牌与授权](https://github.com/ClickHouse/loom/blob/main/docs/guides/tokens-and-grants.md)。

<a id="something-is-wrong-where-do-i-start"></a>

### 出了问题，该从哪里开始？

在服务器上运行 `loom doctor`，在 Claude Code 中运行 `/loom:loom-status`，并检查 `/v1/health`。然后查看[故障排查](https://github.com/ClickHouse/loom/blob/main/docs/operations/troubleshooting.md)，该文档按症状组织。

<a id="the-project"></a>

## 项目

<a id="why-is-an-operation-missing-from-my-mcp-tool-list"></a>

### 为什么我的 MCP 工具列表中缺少某个操作？

MCP 列表取决于工具集（`?toolset=` 或 `LOOM_MCP_TOOLSET`，默认为 `core`），代码工具只有在设置 `LOOM_MCP_CODE_TOOLS=true` 时才会列出。在 `all` 下，既不在 core 清单也不在 atomic 清单中、且标记为 `experimental` 的 op 不会列出。每个 op 仍可通过 `loom_op` 按名称调用（在 `readonly` 下仅限读操作），无论是通过 HTTP 还是各 SDK。参见 [MCP 工具集](https://github.com/ClickHouse/loom/blob/main/docs/reference/mcp.md#toolsets)。

<a id="how-good-is-the-retrieval-and-how-do-you-know"></a>

### 检索效果如何，你们怎么知道？

Loom 附带完整的基准测试套件，包含方法、数据集和确切的命令。只有能够通过已提交文件复现的结果才会对外说明。参见[基准测试](https://github.com/ClickHouse/loom/blob/main/docs/benchmarks/overview.md)。

<a id="how-is-loom-different-from-putting-a-vector-database-next-to-my-app"></a>

### Loom 与在应用旁边放一个向量数据库有什么不同？

在默认的 ClickHouse authority 下，一个 ClickHouse 数据库就保存了行数据、向量、文本索引、图、审计和擦除状态，因此无需保持多处同步。[Postgres 记忆 authority](architecture/storage-authority.md#why-two-stores) 将实时记忆和知识状态保存在 Postgres 中，两个存储之间的同步由 Loom 自行完成。每次写入都会与已存储的内容进行核对。[概览](https://github.com/ClickHouse/loom/blob/main/docs/getting-started/overview.md)列出了每项特性的代价，以及 Loom 在什么情况下不适用。
