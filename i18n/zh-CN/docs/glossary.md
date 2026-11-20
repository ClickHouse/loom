<!-- i18n: source=docs/glossary.md blob=8dd4bdacf4b7cf0e994f5fcf1af3353f19f26540 model=claude-sonnet-5-5 -->
> 本页由机器从[英文原文](https://github.com/ClickHouse/loom/blob/main/docs/glossary.md)翻译，内容以英文原文为准。
> Machine-translated from the [English page](https://github.com/ClickHouse/loom/blob/main/docs/glossary.md), which is authoritative.

<a id="glossary"></a>

# 术语表

Loom 的术语按字母顺序排列，每个术语只定义一次。代码标识符用反引号标出。

**Abstain（弃答）** — 当最佳命中低于为 embedder 校准的弃答阈值时，`memory.search` 返回 `abstain=true` 和一个原因码，而不是返回最接近的弱匹配。它区分了“没有存储任何内容”和“某个不相关的内容得分 0.3”。参见[检索与排序](concepts/retrieval-and-ranking.md)。

**Actor（操作者）** — 由服务器写入的列，标明谁写了这一行：`human:<subject>` 或 `process:<subject>`，类型取自令牌。绝不从请求体中读取。参见[身份与命名空间](concepts/identities-and-namespaces.md)。

**Agent** — 身份元组的第三个元素：执行写入和读取的程序名称（`claude-code`、`support-bot`）。它记录作者身份，不是读取边界：命名空间的每个成员都能读取所有 agent 的行。

**Attestation（认证）** — 记录某一行已被检查的 `verified_*` 列。编辑不会删除这些列，只会将其标记为过期；读取方通过比较已认证的内容哈希来决定是否信任。参见[历史与溯源](https://github.com/ClickHouse/loom/blob/main/docs/features/memory/history-and-provenance.md)。

**Audit log（审计日志）** — `loom.audit_log`：每个被审计的操作对应一行带内容哈希的记录，并按小时归入哈希链式检查点。行中保存的是哈希，而不是内容。参见[审计](https://github.com/ClickHouse/loom/blob/main/docs/features/observability/audit.md)。

**Belief revision（信念修正）** — 记忆生命周期的设计视角：扩展（`memory.set`）、修订（`memory.update_by_id`）、收缩（`memory.delete`）、强化（`memory.pin`）、拒绝（`blacklist.add`）。参见[写入如何被评判](concepts/memory.md#how-a-write-is-judged)。

**Blacklist（黑名单）** — 已登记的子串或正则表达式，用于拒绝匹配的写入（`422 BLACKLISTED`）；`sweep=true` 还会将已有的匹配项标记为墓碑。

**Chunk（分块）** — 知识文档或源文件的一个片段，单独存储并生成 embedding，这样检索可以返回段落而不是整篇文档。

**Code link（代码链接）** — 记忆指向其所提及的定义或文件的链接，会随重命名和编辑而更新。其状态为 `fresh`、`moved`、`changed`、`unresolved` 或 `missing`。尚未进入主干的代码名称最多等待 `memory_code_links_pending_days`（14 天）。参见[关联到代码的记忆](concepts/memory.md#memories-linked-to-code)。

**Code namespace（代码命名空间）** — 类型为 `code` 的命名空间，保存一个已建立索引的 git 仓库：其分块、符号、边、提交和 PR 历史。参见[代码索引](features/code-index/overview.md)。

**Commit coverage（提交覆盖）** — 每个索引层级是否都已处理某个提交；`code.index_status(commit=...)` 可以回答这一点，`code.coverage.watch` 会在其变为真时发出通知。参见[新鲜度](features/code-index/freshness.md)。

**Compiler tier（编译器层级）** — 由编译器级索引器（SCIP）而非解析器层级得出的代码索引事实；调用边和引用通过类型信息解析。

**Conflict（冲突）** — 与已有记忆语义上非常接近的写入（余弦相似度 ≥ `LOOM_CONFLICT_THRESHOLD`）。两行都会被标记为 `conflicted` 并相互链接，由人或 agent 来解决。参见[冲突与拒绝](https://github.com/ClickHouse/loom/blob/main/docs/features/memory/conflicts-and-refusal.md)。

**Consolidation（整合）** — 后台处理流程，合并关于同一主题的零散记忆并记录谱系（`memory_belief_sources`，`support_type='derived_from'`）。

**Content hash（内容哈希）** — 记忆值的 `content_sha256`。它用于去除完全重复的内容，支撑 `if_match_sha256` 前置条件，并作为审计行的锚点。

**Context pack（上下文包）** — `context.pack`：一个有大小限制的集合，包含工作记忆、画像、相关记忆、流程和决策，在会话开始时一次性加载。参见[统一搜索](https://github.com/ClickHouse/loom/blob/main/docs/features/unified-search.md#contextpack)。

**Dashboard（控制台）** — 位于服务器根路径的 Web 应用，面向用户：记忆、活动、来源、审核队列、任务、仓库和设置。参见[控制台](https://github.com/ClickHouse/loom/blob/main/docs/features/dashboard.md)。

**Decision（决策）** — 对一次选择的一等记录（`decision.record`），包含理由、被否决的备选方案，以及之后的结果（`decision.outcome`）。参见[任务与决策](https://github.com/ClickHouse/loom/blob/main/docs/features/tasks-and-decisions.md)。

**Entity edge（实体边）** — `loom.entity_edges` 中两个实体之间带权重的共现链接，具有有效期区间；`facts.traverse` 遍历的就是这张图。参见[实体图](https://github.com/ClickHouse/loom/blob/main/docs/features/entity-graph.md)。

**Erasure saga（擦除 saga）** — `erasure.request` 执行的可恢复、失败即关闭的一系列步骤，用于从每张表（以及已配置的外部后端）中移除某个主体的数据，最后进行残留普查。参见[保留与擦除](https://github.com/ClickHouse/loom/blob/main/docs/features/memory/retention-and-erasure.md)。

**Extraction（提取）** — 将对话消息转换为候选记忆（`memory.set_from_messages`），使用 LLM 和质量过滤器。参见[提取](https://github.com/ClickHouse/loom/blob/main/docs/features/memory/extraction.md)。

**Extractor stamp（提取器戳）** — 代码索引提取器模块的代码 token 的哈希。它一旦变化，就会对每个已索引的仓库重新分块；注释和文档字符串不计入。参见[代码索引架构](https://github.com/ClickHouse/loom/blob/main/docs/architecture/code-index-architecture.md)。

**FINAL** — ClickHouse 的修饰符，在读取时将 `ReplacingMergeTree` 表折叠为每个键的最新版本。Loom 读取 `loom.memory` 时使用它。参见 [ClickHouse](https://github.com/ClickHouse/loom/blob/main/docs/architecture/clickhouse.md)。

**Fence（围栏令牌）** — `task.claim` 随租约返回的令牌；之后的 `task.progress`、`task.heartbeat`、`task.complete` 和 `task.fail` 调用必须出示它，因此租约已失效的认领者无法写入。

**Grant（授权）** — 为某个主体授予其在命名空间上的角色的一行记录（`grants.*`）。参见[令牌与授权](https://github.com/ClickHouse/loom/blob/main/docs/guides/tokens-and-grants.md)。

**Identity tuple（身份元组）** — 每一行上的 `(org, namespace, agent, user_id, session_id)`。前三项必填；后两项默认为 `'-'`。参见[身份与命名空间](concepts/identities-and-namespaces.md)。

**`ignored_args`** — 响应字段，列出服务器未使用的请求字段（未知或已废弃的），使客户端能在不失败的情况下发现差异。

**Inbox（收件箱）** — 由命名空间拥有的具名任务队列（`task.*`、`inbox.*`）。参见[任务与决策](https://github.com/ClickHouse/loom/blob/main/docs/features/tasks-and-decisions.md)。

**Knowledge document（知识文档）** — 通过 `knowledge.add` 添加的文档，经分块并生成 embedding 以供检索。与记忆不同：它是参考资料，而不是信念。参见[知识文档](https://github.com/ClickHouse/loom/blob/main/docs/features/knowledge.md)。

**Learning（学习项）** — 在 `loom.learnings_queue` 中等待人工决定的候选项（`learnings.list`、`learnings.review`）：由多个写入者支持的事实，被提升到共享层；由对话产生的事实，被提升为命名空间记忆行；或机器人陈述的关于自身的规则，被提升到该命名空间的知识中。

**`log_comment`** — ClickHouse 查询设置，Loom 用调用它的 op、worker 或 CLI 命令填充，使 `system.query_log` 能归因每条语句。

**`maturity`** — op 声明的 `stable`/`experimental` 字段。既不在核心工具集也不在 atomic 工具集中列出的 experimental op 不会出现在 `all` 列表中，但仍可按名称调用。参见 [MCP 工具集](https://github.com/ClickHouse/loom/blob/main/docs/reference/mcp.md#toolsets)。

**MCP tool（MCP 工具）** — op 在 Model Context Protocol 中的映射，命名为 `loom_<op>`，其中的点替换为下划线（`loom_memory_set`），另有几个合成工具，如 `loom_search` 和 `loom_op`。参见 [MCP](https://github.com/ClickHouse/loom/blob/main/docs/reference/mcp.md)。

**Memory（记忆）** — `loom.memory` 中的一行：键、值、其身份元组、类型、标签、有效期和溯源信息。参见[记忆](concepts/memory.md)。

**Memory key（记忆键）** — 记忆在其命名空间和 agent 内的可读标识符，按惯例为 `<scope>/<kind>/<id>`。对已有键的写入会取代它。

**Memory type（记忆类型）** — 记忆的种类：`semantic`、`episodic`、`procedural`、`preference`、`profile` 等；类型决定它是否生成 embedding 以及如何被搜索。

**Mentor hint（导师提示）** — Loom 附加在结果中或通过 `mentor.brief` 提供的简短使用建议，指引 agent 使用它没有用好的 plane 或 op。

**Namespace（命名空间）** — 存储、共享和访问的单位：org 内的具名工作区。类型有 `memory`、`code` 和 `bench`。参见[命名空间与共享](https://github.com/ClickHouse/loom/blob/main/docs/features/namespaces-and-sharing.md)。

**Operation（op，操作）** — 注册表（`server.operations.registry.ALL_OPERATIONS`）中的一个已声明条目，通过 `POST /v1/<op>`、MCP 工具和 SDK 提供。参见[操作参考](https://github.com/ClickHouse/loom/blob/main/docs/reference/operations.md)。

**Org** — 顶层租户；身份元组的第一个元素。

**Pinned（已固定）** — 被 `memory.pin` 标记的记忆。针对已固定记忆的冲突写入会被拒绝，返回 `409 PINNED_CONFLICT`。

**Plane** — 拥有自己的 op 和表的产品领域：记忆、知识、对话、图、代码、tracker、运行与任务、审计。

**Procedure（流程）** — 可复用的操作指南（`procedure.add` / `match`），存储为类型为 `procedural` 的记忆。参见[流程与学习项](https://github.com/ClickHouse/loom/blob/main/docs/features/memory/procedures-and-learnings.md)。

**Profile（画像）** — agent 或用户的常驻上下文（`profile.*`），由 `context.pack` 最先加载。

**Projection（投影）** — 由写入派生出的行（embedding、图的边、摘要）。失败的投影会被记录并重试（`ops.projection_failures.*`）。

**Read scope（读取范围）** — `read_scope`，限定在一个命名空间内的读取：`namespace`（每一行，默认值）、`own_user` 或 `own_session`。令牌中的声明是上限，请求可以收窄但不能放宽。参见[身份与命名空间](concepts/identities-and-namespaces.md#read-scope-inside-a-namespace)。

**Record（记录）** — 按已注册的记录模式（`memory_schema.*`）校验的结构化记忆。不要与 tracker 记录混淆。

**ReplacingMergeTree (RMT)** — 支撑大多数 Loom 表的 ClickHouse 引擎：新版本的行在合并或 `FINAL` 读取时替换旧行，因此每条写入路径都必须带上所有列。参见 [ClickHouse](https://github.com/ClickHouse/loom/blob/main/docs/architecture/clickhouse.md)。

**Residue census（残留普查）** — 擦除 saga 的最后一步：在读取路径涉及的每张表上，统计范围内仍有效的行数。任何残留都会使 saga 失败。

**Role（角色）** — `viewer`、`writer`、`admin`、`org_admin`、`platform_admin`，权限依次递增。每个 op 声明其所需的最低角色。

**Run（运行）** — 一次 agent 执行，由 `run.begin` 和 `run.complete` 括起，记录它回忆和写入的内容。参见[运行](https://github.com/ClickHouse/loom/blob/main/docs/features/runs.md)。

**Session（会话）** — 一次对话或工作时段，身份元组的第五个元素；**会话简报**（`session.brief_*`）是其保存的摘要，供之后恢复使用。

**Shared layer（共享层）** — 每个 `(org, namespace, memory_key)` 对应一条规范行，由 agent 写入的行提升而来（`memory.promote` / `memory.demote`，表 `loom.shared`）。另一层的“私有”指未被提升，而不是被隐藏。

**Source class（来源类别）** — 由服务器写入的行的溯源：`asserted`、`extracted`、`observed`、`derived`、`imported` 或 `external`。请求只能降级它。

**Tier（层级）** — 产生某个代码索引事实的提取器（解析器或编译器）；答案会说明由哪个层级支撑。

**Tombstone（墓碑）** — 标记记忆已被删除的更高版本行。`memory.restore` 可以在宽限期内撤销它；`memory.delete` 不会物理删除任何内容。

**Toolset（工具集）** — 客户端请求的 MCP 列表（`core`、`coding`、`atomic`、`tasks`、`tracker`、`readonly`、`all`）；它改变列出的内容，而不是令牌可以调用的内容。

**Tracker** — 从已配置的来源读取问题跟踪器记录（issue、PR、CI 运行）的 plane，用于重复检查、二分定位和测试历史。参见[跟踪器](https://github.com/ClickHouse/loom/blob/main/docs/features/tracker/overview.md)。

**Trust mode（信任模式）** — 收件箱所有者为发布者设定的接受策略；Loom 只存储它，自己从不修改。

**Valid time（有效时间）** — 记忆上的 `valid_at` / `valid_to`（实体边上的 `valid_from` / `valid_to`）：事实在现实世界中为真的时间。Loom 只记录有效时间，不是双时态的。

**Version（版本）** — 单调递增的列，`ReplacingMergeTree` 保留其最大值；记忆的每次写入（包括删除）都是一个新版本。

**Worker** — 受监督的后台循环，在 `server/workers/registry.py`（`_WORKERS`）中注册为一行。参见[后台任务](https://github.com/ClickHouse/loom/blob/main/docs/reference/workers.md)。

**Working memory（工作记忆）** — 用于当前任务的草稿块（`working_memory.*`）：不生成 embedding，不参与排序，由 `context.pack` 在持久记忆之前加载。参见[工作记忆与画像](https://github.com/ClickHouse/loom/blob/main/docs/features/memory/working-memory-and-profiles.md)。
