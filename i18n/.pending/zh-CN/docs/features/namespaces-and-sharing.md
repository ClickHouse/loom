<!-- i18n: source=docs/features/namespaces-and-sharing.md blob=7e1ba0ffe7a1ef671a51197f949ea03d8a321737 model=claude-sonnet-5-5 -->
> 本页由机器从[英文原文](https://github.com/ClickHouse/loom/blob/main/docs/features/namespaces-and-sharing.md)翻译，内容以英文原文为准。
> Machine-translated from the [English page](https://github.com/ClickHouse/loom/blob/main/docs/features/namespaces-and-sharing.md), which is authoritative.

<a id="namespaces-and-sharing"></a>

# 命名空间与共享

命名空间是组织内部隔离和共享的基本单位。每次读取都限定在 `(org, namespace)` 范围内，每个授权都指向一个命名空间，每项针对命名空间的设置（冲突策略、保留策略、检索默认值、重排序器）都保存在 `loom.namespaces` 中对应的行上。身份模型见[身份与命名空间](../concepts/identities-and-namespaces.md)。

<a id="registering-a-namespace"></a>

## 注册命名空间

`namespace.register` 会创建注册表中的行，并使调用方成为该命名空间的管理员。任何主体都可以调用它（viewer）；重新注册已有的命名空间需要管理员权限。如果某个名称没有对应的行，但已经存有数据或存在有效授权，则会被拒绝，除非调用方已经是它的管理员（[新名称可以占用什么](https://github.com/ClickHouse/loom/blob/main/docs/architecture/security.md#what-a-new-name-may-claim)）。当 `LOOM_NAMESPACE_AUTOREGISTER_ENABLED` 开启（默认）时，首次向没有对应行的命名空间写入会自动注册它，以令牌的 subject 作为所有者（如果该名称已被占用，则没有所有者），并设置 `retention_days = 0`。只要没有其他人在该名称上持有有效授权，该所有者就可以完成注册。

重新注册是部分更新：省略的字段保留其已存储的值。`tags` 会被整体替换。对于 `conflict_threshold`、`capture_generation_io` 和 `coverage_sweep`，省略表示保留已存储的选择，显式的 `null` 则恢复为服务器默认值。

| 字段 | 作用 |
| --------------------------------------------------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------ |
| `kind` | `memory`、`code`（一个已索引的仓库和分支；只读参考数据）或 `bench`（一个一次性的测试框架命名空间，空闲后由 GC 清除）。在新行上省略时由名称决定：匹配 `LOOM_NAMESPACE_BENCH_PREFIXES` 前缀的为 `bench`，其他均为 `memory`，与首次写入时相同。 |
| `visibility`、`join_policy`、`requires_approval` | 谁可以发现并加入它：`private`（仅成员）或 `public`（由 `namespace.browse` 列出）；`join_policy` 可为 `open`、`approval`、`closed` 或 `inherit`。 |
| `default_conflict_policy` | `auto_reinforce`（默认）、`manual_review`、`newest_wins` 或 `append_only`，即一次写入如何与已有的同类记忆比较判定。参见[冲突与拒绝](https://github.com/ClickHouse/loom/blob/main/docs/features/memory/conflicts-and-refusal.md)。 |
| `conflict_threshold` | 相近的写入被视为冲突的余弦值；显式的 `null` 继承 `LOOM_CONFLICT_THRESHOLD`。 |
| `proposal_policy` | 如何裁定处于试用中的事实；只有所有者设置的 `auto` 才允许 Loom 在评分运行中自行裁定。参见[写入记忆](https://github.com/ClickHouse/loom/blob/main/docs/features/memory/writing.md)。 |
| `retrieval_defaults` | 未固定参数的 `memory.search` 所使用的设置（`search_mode`、`top_k`、`min_similarity`、`floor_mode`、`recency_pool_weight` 等）；`{}` 表示清除。参见[搜索与提问](https://github.com/ClickHouse/loom/blob/main/docs/features/memory/search-and-ask.md)。 |
| `rerank_endpoint`、`classify_endpoint` | 可选的重排序器（内置名称或 `LOOM_RERANK_ENDPOINTS_JSON` 中的名称，不能是 URL）和分类器 URL（公网地址上的 https，或 `LOOM_MEMORY_CLASSIFY_PRIVATE_HOSTS` 列出的主机；当要求使用网关时，必须是 `LOOM_INFERENCE_GATEWAY_HOSTS` 中的主机；在列出的网关主机上，即使不要求网关，也须是默认端口的 https），以及它们的提示词模板。两者都是失败时放行，但网关拒绝该调用的身份时，写入会失败。 |
| `retention_days`、`warm_days`、`tag_registry` | 命名空间的保留与分层，以及按类型设置的保留和去重阈值。`0` 表示永久保留。参见[保留与擦除](https://github.com/ClickHouse/loom/blob/main/docs/features/memory/retention-and-erasure.md)。 |
| `conversation_logging_enabled`、`capture_generation_io` | 是否同意存储对话记录，以及是否同意将提示词和补全文本发送到 Langfuse。两者都需主动开启。 |
| `consolidation_enabled`、`daily_cost_cap_usd`、`coverage_sweep` | 后台整合、该命名空间的上游支出上限，以及提取覆盖率扫描。 |

```python
from loom import Agent

async with Agent(org="acme", namespace="eng-tooling", agent="setup") as agent:
    await agent.namespaces.register(
        owner_email="platform@acme.example",
        description="Shared memory for the build tooling agents",
        visibility="public",
        default_conflict_policy="newest_wins",
        retrieval_defaults={"search_mode": "hybrid", "top_k": 8},
    )
    # Fields the SDK method does not take go through Agent.call:
    await agent.call(
        "namespace.register",
        owner_email="platform@acme.example",
        join_policy="approval",
        daily_cost_cap_usd=5.0,
    )
```

`namespace.delete`（管理员）会将该行标记为未激活，并停止接受写入；它不会触及数据。要删除数据，请先执行擦除（`erasure.request(scope_type="namespace")`，参见[保留与擦除](https://github.com/ClickHouse/loom/blob/main/docs/features/memory/retention-and-erasure.md)）。使用 `active=true` 调用 `namespace.register` 可重新激活它。

<a id="membership-and-grants"></a>

## 成员与授权

授权（grant）赋予一个主体（个人的登录名、SSO 或已关联账号的已验证邮箱，或服务账号 `sa:<name>`）在某个命名空间上的角色：`viewer` 可读，`writer` 可写，`admin` 可管理授权和设置。令牌携带组织以及可访问的命名空间，具体角色由授权表决定。

| 操作                                                                 | 角色      | 作用                                                                                                                                                   |
| -------------------------------------------------------------------- | --------- | ------------------------------------------------------------------------------------------------------------------------------------------------------ |
| `grants.create`                                                      | admin     | 授予角色。若创建的角色低于该主体当前的角色，则返回 409 `GRANT_WOULD_DOWNGRADE` 并拒绝，除非设置 `replace=true`。                                       |
| `grants.list`                                                        | viewer    | 列出谁拥有访问权限、角色是什么，以及 `pending_role`。                                                                                                  |
| `grants.revoke`                                                      | admin     | 移除授权。                                                                                                                                             |
| `grants.approve`                                                     | admin     | 批准或拒绝待处理的请求，可以选择授予不同的角色。                                                                                                       |
| `grants.pending_for_me`                                              | viewer    | 调用者担任管理员的各命名空间中的所有待处理请求，范围为整个组织。                                                                                       |
| `namespace.browse`                                                   | viewer    | 组织中的公开命名空间，不包括 collection（`closed` 的命名空间仅在使用 `include_closed` 时返回）。                                                       |
| `namespace.join`                                                     | viewer    | 自行加入公开命名空间。`viewer` 会立即授予；`writer` 或 `admin` 的请求始终处于待处理状态，在管理员批准之前没有任何访问权限。                             |
| `namespace.leave`, `namespace.cancel_request`, `namespace.mark_seen` | viewer    | 放弃自己的授权、撤回待处理的请求、确认已收到邀请。                                                                                                     |
| `namespace.list_mine`                                                | viewer    | 调用者所属的命名空间，每个都带有其 `kind`（collection 对应 `collection`）和 `title`。                                                                  |
| `namespace.list`                                                     | 组织管理员 | 组织中的所有命名空间。                                                                                                                                 |

签发令牌、服务账号、读取范围和受限命名空间的内容，请参阅[令牌与授权指南](https://github.com/ClickHouse/loom/blob/main/docs/guides/tokens-and-grants.md)。

<a id="sharing-facts-across-agents-and-namespaces"></a>

## 在 agent 和命名空间之间共享事实

在一个命名空间内，默认情况下每个写入方的行对所有读取方都可见（`agent="*"`）。跨越边界始终是带有溯源信息的复制，而不是共享同一行：

| 操作                   | 角色               | 作用                                                                                                                                                                      |
| ---------------------- | ------------------ | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `memory.promote`       | admin              | 将一条私有记忆复制到命名空间的共享层（`loom.shared`），可通过 `memory.search(scope="shared")` 读取。在 `reversible_for_days`（默认 7）天内可撤销。                         |
| `memory.demote`        | admin              | 将共享副本及其 embedding 标记为已删除（tombstone）；`was_reversible` 表示该操作是否发生在可撤销期限内。                                                                   |
| `memory.copy_to`       | 目标上的 writer    | 自助复制：按 key 将你可读取的记忆复制到你可写入的命名空间。非破坏性；目标由其自身的 embedder 重新生成 embedding。                                                         |
| `memory.cross_promote` | 两端的 admin       | 面向运维人员的形式，按记忆 id 指定，并在目标上带有 `cross_promoted_from_*` 溯源信息。                                                                                     |

`memory.copy_to` 按 key 读取源行，并将调用方记录为该副本的操作者，因此需要已登录的调用方：即设置 `LOOM_AUTH_REQUIRED=true`，并使用既能读取源又能写入目标的令牌。默认的单用户部署会拒绝该操作。Python SDK 通过 `agent.client` 调用它：

```python
from loom import Agent
from loom.errors import NotFound

async with Agent(org="acme", namespace="support", agent="triage-bot") as agent:
    try:
        result = await agent.client.memory.copy_to(
            from_org="acme",
            from_namespace="support",
            memory_key="customer/4812/plan",
            to_namespace="billing",
            rationale="Billing checks invoices against this plan.",
        )
    except NotFound:
        ...  # no live row under that key in the source namespace
    else:
        print(result["copied"], result["target_key"])  # True customer/4812/plan
```

除非 `target_key` 指定了其他 key，否则副本沿用源 key；除非你指定了 agent，否则它在目标中归命名空间所有（`target_agent="-"`）。`from_agent="*"`（默认值）会复制该 key 下最近写入的一行。rationale 不会作为文本存储：审计行只保留它的 SHA-256 和长度。

跨命名空间的复制不是原子的：复制中途失败时，源保持不变，而目标中没有该行。learnings 队列（`learnings.*`）会提出值得提升的行，详见[过程与 learnings](https://github.com/ClickHouse/loom/blob/main/docs/features/memory/procedures-and-learnings.md)。

<a id="reading-a-namespaces-health"></a>

## 读取命名空间的健康状况

| 操作 | 回答的问题 |
| --- | --- |
| [`namespace.inventory`](https://github.com/ClickHouse/loom/blob/main/docs/reference/operations/namespace.md#namespaceinventory) | 命名空间中有什么、出现了哪些主体：记忆、记录、文档、运行和集成的实时数量与最新写入；成员数；在记忆行上观察到的 agent、作者和主体；响应请求的进程是否由真实的模型和 embedder 提供服务。 |
| `namespace.summary` | 按记忆类型统计的数量、热门实体、最近的写入和召回、不同的 agent、最近活动。 |
| `namespace.validate` | 完整性报告：卡住的冲突、低置信度的行、仍在等待 embedding 的行。在一批写入之后，或召回结果看起来不对时运行它。 |
| `namespace.audit` | 命名空间被用作记忆还是对象存储：按精确键读取与搜索的对比、哈希形态的键、双重编码或过大的值、被存为记忆的租约、版本频繁变动。它还会指出部署自身的状态（类型 `system_state`：缺少遥测、索引积压）；当账本没有记录窗口内写入背后的任何调用时，它会跳过账本检查，而不是将其报告为正常。每个发现都附带修复建议。 |
| `memory.inspect` | 单个用户、agent 或会话：它自己的行（与命名空间中的其余部分分开）、窗口内的调用，以及生成其记忆的指南、类别和提取策略（[见下文](#inspecting-one-user-agent-or-session)）。 |
| `namespace.tune` | 对生成命名空间记忆的提取规则、类别和指南，以及对其文档的分块和提取器设置的修改草案，每条都附有证据和用于应用它的写入操作；不会自动应用任何内容（[见下文](#drafting-changes-to-how-memories-are-made)）。 |
| `namespace.taxonomy` | 实时词汇表：热门标签、实体、记忆类型和社区。 |
| `agents.connected` | 窗口内（默认 24 小时）活跃的 agent：最后和首次出现时间、操作数、涉及的命名空间以及每个 agent 的成本。回答“我的客户端是否真的在与 Loom 通信？” |

`namespace.inventory` 将各类对象分开统计。`members` 统计授权持有者：即持有有效授权的人，不包括待处理的请求、组、agent 或服务账号（在命名空间上以及在集合的每个上级上调用 `grants.list`，会列出他们的授权）。声明中直接指明该命名空间的令牌无需授权即可读取，且不计入其中；集合也会统计其上级的授权持有者，因为父级的授权会覆盖每个子级。在这条层级链上的任何位置，只要存在授予 `everyone`、组或 agent（在其下登录的每个终端用户）的授权，或存在整个组织都可读取的代码命名空间，计数就会是 `complete: false`，因为这些成员没有被列出。`agents`、`authors`（写入某行的 `actor`）和 `subjects`（某行所属的 `user_id`，不含未设置的 `-`）是在实时记忆行上观察到的身份，而不是一份目录。个人集合的所有者不持有授权，因此不计入成员（`total` 为 null）。如果某个部分失败，或调用方无权读取，会以 `complete: false` 并附带原因说明：集成数量仅对命名空间管理员可见；在绑定到单个用户的读取范围下（`read_scope=own_user` 或 `own_session`，或固定到某个用户的令牌），记忆数、运行数以及观察到的 agent、作者和主体都只属于该用户，文档和成员按整个命名空间统计，与 `knowledge.*` 和 `grants.list` 的读取结果一致，而记录（每个键一条，无论最后由谁写入）和集成则不统计。

每个 `namespace.audit` 和 `namespace.validate` 的发现都带有相同的证据字段，因此 CLI、控制台和 MCP 客户端读取的是同一种结构：

| 字段 | 含义 |
| --- | --- |
| `kind` | 当存储的值违反已声明的不变量或证明时为 `contract_violation`；当某种模式通常会损害召回或存储，但合理的设计也可能命中它时为 `usage_heuristic`。启发式命中只是警告，不是误用的证明。当发现涉及的是部署而不是写入方时为 `system_state`：索引积压（`index_backlog`）或账本未记录的写入（`telemetry_unavailable`，此时会跳过账本检查，而不是将其报告为正常）。这些是 `namespace.audit` 的发现；`namespace.validate` 的 `unembedded` 检查属于 `contract_violation`，会统计所有超过宽限期仍没有向量的行，包括被暂扣的模型调用。 |
| `rule_version`、`source_op` | 规则的版本（当其谓词或内置阈值改变时提高，因此两份报告只有在同一版本下才可比较；规则读取的设置或请求输入，例如 `confidence_floor` 或值大小建议，不计入版本）以及计算它的操作。 |
| `window_seconds` | 使用类规则统计所覆盖的账本窗口；对读取当前状态的规则为 null。 |
| `population`、`count` | `share` 的分母（发现所对照的行或调用，其中可能包含规则按设计排除的行），以及匹配的数量。`namespace.validate` 的检查只报告没有分母的计数，并将 `population` 留空；摘要读取被跳过的审计发现也是如此。 |
| `sample_limit`、`samples_complete` | 样本上限，以及样本是否列出了所有匹配项：当上限截断了样本，或样本无法指明每一个匹配项时为 false。 |
| `evidence` | 样本所指明的每一行的实时修订版本（见下文）。绝不包含值。 |
| `exceptions`、`uncertainty` | 规则按设计排除的内容，以及命中为何仍可能是正确的。 |

每个 `evidence` 条目都是样本所指明的某一行的实时修订版本，在检查运行完毕后读取：`memory_key`、`memory_id`、`version`、`content_sha256`（`memory.get` 提供的哈希；对于高敏感度的行、长度超过 `LOOM_NAMESPACE_REPORT_HASH_MAX_BYTES`（64 KiB；[配置](https://github.com/ClickHouse/loom/blob/main/docs/reference/configuration.md)）的值，或在报告读取期间发生变化的行，该值为空）、身份列、`memory_type`、`source_class` 和 `updated_at`。样本通过 `memory_id` 指明其行，来自 embedding 表时则通过其身份列指明；在多个身份下持有的同一个键，只会引用匹配的那一个。在检查运行之后被修订的行，会按其较新的修订版本引用；此时它的 `updated_at` 不早于报告的 `computed_at` 或 `checked_at`（精确到整秒）。仅改变行状态的写入可能保持其 `updated_at` 不变，因此这种比较只是提示，而不是证明。不指明单个行的样本（`exact_key_store` 热点键、`version_churn` 键）、不再有效的行（孤立向量）没有条目；在 Postgres authority 下，授权方在所读取版本上不再提供的行也没有条目（[存储 authority](../architecture/storage-authority.md)）。

报告的 `coverage` 列出已运行的规则（`rules_run`）和无法运行的规则（`rules_skipped`，同时附带原因列在 `skipped` 中）。两者都会指明所读取的证据（`evidence`），对于 `namespace.audit`，还包括其摘要读取（大多数发现都以此为衡量基准），以及在 Postgres authority 下的 `projection`，即等待 ClickHouse 发布该命名空间已提交的内容（[存储 authority](../architecture/storage-authority.md)）。只要其中任何一项被跳过，`complete` 就为 false。

`loom audit-namespace` 从 shell 运行 `namespace.audit`；[优化命名空间指南](https://github.com/ClickHouse/loom/blob/main/docs/guides/optimize-namespace.md)会把它的发现转化为经过审阅的改造计划。词汇通过 `namespace.register_category`、`namespace.list_categories`、`namespace.search_categories`（类别）以及 `namespace.register_ontology`、`namespace.list_ontology`（用于[实体图](https://github.com/ClickHouse/loom/blob/main/docs/features/entity-graph.md)的实体类型和关系动词）进行注册。

快照和时间点回滚（`namespace.snapshot`、`namespace.restore`、`namespace.rollback`）在[历史与溯源](https://github.com/ClickHouse/loom/blob/main/docs/features/memory/history-and-provenance.md)中介绍。

<a id="inspecting-one-user-agent-or-session"></a>

## 检查某个用户、agent 或会话

`memory.inspect`（viewer）接受 `subject`（`user`、`agent` 或 `session`）和 `id`，即要查看的 `user_id`、agent 名称或 `session_id`；未设置的 `-` 和 `*` 不指代任何对象，会被拒绝。它不会修改所读取的任何内容，也不计入召回。与 `memory.list` 一样，它会为所列出的行写入一条暴露账本行。

- **`own`**：其身份指向该 subject 的有效行。对于 agent，是以其名称写入的行；该名称说明某一行来自哪个 agent，并不是读取边界。对于用户，是归在该 `user_id` 下的行，无论由谁写入：这也是绑定到该用户的令牌（`read_scope: own_user`）所能读取的内容。对于会话，是任何用户在该 `session_id` 中写入的行；绑定到某一用户和该会话的令牌（`read_scope: own_session`）读取的是该用户的那部分。
- **`others`**：该命名空间中所有其他的有效行。成员同样可以读取这些行，因此仅凭 `own` 并不能代表某个用户、agent 或会话能读取的全部内容。两个视图都不统计已提升的行（`memory.promote`），搜索也会把这些行提供给每个 agent。
- 每个视图的 `definition` 用通俗的语言说明它包含哪些行；其 `refs` 列出它所指的确切列，以及定义中提到的读取范围和操作。
- 每个视图按类型、状态和来源类别统计其行数，列出热门标签以及其行所归入的已注册类别，并列出最新的行（`sample_limit`，默认 10），包含 `memory_id`、`version`、`memory.get` 提供的 `content_sha256`、`actor`、`sensitivity`、历史中保留的版本数（包括实体标注器重新发出的版本，`memory.history` 不包含这些版本）以及 200 个字符的摘录。高敏感度的行既没有摘录也没有哈希；值大于 `namespace_report_hash_max_bytes`（0 表示不引用任何哈希）的行，以及在列出与引用读取之间被修订的行，没有哈希。当列出的行数少于视图中的行数、无法读取视图的统计数据，或者在 Postgres 作为权威存储时无法确认 ClickHouse 已包含命名空间所提交的内容（等待超时或 Postgres 未响应，`skipped` 会指明 `projection`）时，`complete` 为 false。
- **`activity`**：账本在 `window_seconds`（默认 7 天）内按操作归入该 subject 的调用，包括尝试次数和成功次数，不含 worker 行，也不含控制台和助手发起的调用。账本根据调用参数中指明的 `agent`、`user_id` 和 `session_id`，或受限令牌所绑定的用户和会话来归类调用，因此任何人发出的、指明了该 subject 的调用都会被计入，而不指明任何 subject 的 `memory.inspect` 调用则不会。调用只说明某个工具运行过，不说明其结果已到达模型。当账本保留的天数少于该时间窗口时，`gap` 会说明这一点（取命名空间自己的 `retention_days`，否则取部署的设置）。
- **`settings`**：指南的修订版本和摘要、已注册类别的数量，以及仅对组织管理员显示的操作员提取策略（缺失时由 `extraction_policy_unavailable` 说明原因）。提取指令随每个请求发送；没有任何用户、agent 或会话设置会存储它们，`extraction_instructions_refs` 指明承载这些指令的操作和参数。
- 无法读取的部分会列在 `skipped` 中；被跳过的部分并不等于空。权限被收窄到命名空间以下的令牌（`read_scope`）不能调用它。

<a id="drafting-changes-to-how-memories-are-made"></a>

## 起草对记忆生成方式的修改

`namespace.tune`（viewer）会读取命名空间的审计、其标签和键布局、已注册的类别、其指南，对于组织管理员还会读取当前生效的运营方提取策略，并起草对创建其记忆的设置的修改。它读取整个命名空间，而不是单个用户、agent 或会话（`memory.inspect` 可查看单个），不应用任何修改，也不调用模型。每条提议都包含：

- **`target`**：它所修改的设置。可以是 `extraction_instructions.request`（`memory.set_from_messages` 请求所发送的指令；没有任何用户、agent 或会话设置存储这些指令）、`namespace.category`、`namespace.guide`，对于文档则是 `settings.knowledge_chunking` 和 `settings.document_extractors_json`（见[下文](#document-drafts)）。提取规则绝不会被起草为运营方提取策略：生效的策略会覆盖所有其他指令，并关闭提取的恢复流程，因此在那里写入风格规则会使命名空间损失目前保留的事实。如果当前生效的策略已包含某条规则的完全相同的文本，则不会再次起草该规则。
- **`current`** 和 **`proposed`**：设置当前的值和将要设置的值；对于指南，还有 `current_revision`。
- **`evidence`**：它所回应的审计发现（`finding_codes`），有多少行支持它（前 `sample_limit` 行以 `memory_id`、`version` 和 `memory.get` 返回的 `content_sha256` 引用，依次从每个发现中各取一行），以及它必须保持可用的行数（`counter_count`：至少是检查所读取的、且没有被任何检查标记的最小总体的行数，或另一个类别已持有的带标签行数；当该数量无法读取时为 null，对于不改变任何已存储行的指南也为 null）。
- **`affected_agents`**：该修改所影响的写入方，最多 20 个，并附带 `affected_agents_total`：对于提取指令，是其行由提取写入的 agent；对于类别或指南，是写入过活跃行的每个 agent；当命名空间的写入方无法读取时，`affected_agents_total` 为 null。
- **`effect`**（`future`）和 **`historical`**：设置只影响其修改之后写入的记忆；对已存储行的重新处理（`memory.move`、`memory.assign_category`）是单独的操作，需要各自的批准。
- **`apply`**：普通的写入操作、其参数（当参数只是要合并进每个请求的一部分时为 `args_complete: false`）、所需的角色及其保护条件。`namespace.guide_set` 会拒绝已超出 `expected_revision` 的指南；`namespace.register_category` 没有修订检查，因此在应用之前会重新读取类别。
- **`refs`**：草稿文本中以普通文字提到的每个确切名称，供据此执行操作的调用方使用。设置按其被修改的方式命名：可实时修改时为 `settings.set <field>`，仅由环境设置时为其 `LOOM_` 变量。关于某个设置、丢失的能力、某个发现或错误码的 `skipped` 原因，或指示读者执行操作的原因，会在括号中给出确切名称；`projection:` 行和 `audit.<check>` 行原样保留其来源的文本。

草稿来自固定规则：针对每个写入方一侧的审计发现各有一条提取规则（双重编码的值、JSON 前缀摘录、过大的值、情景性 blob、协调状态、手工构造的索引行）。只有当 `memory.set_from_messages` 的提取向该命名空间写入（行带有其来源 trace），且该发现所引用的行中至少有一行来自它时才会起草，因为这些指令不会影响其他任何内容；如果某个发现所引用的行都不是提取写入的，或审计没有引用该发现的任何活跃行，或提取没有向该命名空间写入任何内容，则不起草任何内容，并在 `skipped` 中说明。其受影响的 agent 是其行由提取写入的 agent，它引用由提取写入的被标记行，而被标记的数量涵盖所有写入方。键的布局属于写入方（提取出的键会被清理），因此不会为它起草规则。其次，对于在至少 `min_rows` 个活跃行上带有、但没有同名类别的每个标签，起草一个类别；对于在至少 `min_rows` 行上列出键前缀和标签（或现有指南遗漏的前缀）的情况，起草一份指南；个人集合（`col-me-…`）不起草，因为它不接受指南。不是普通名称的标签或前缀绝不会被起草，因此存储在行中的文本无法左右目标或参数；`<prefix>:` 形式的标签（`source:`、`speaker:`、`agent:`）也不会被起草，因为它们表示一行来自哪里，而不是关于什么。审计无法运行的检查会在 `skipped` 中列为 `audit.<check>`。范围被缩小到命名空间以下的令牌（`read_scope`）无法调用它。

<a id="document-drafts"></a>

### 文档草稿

切分和提取文档的设置属于整个部署，且只存在于那里：文档草稿的 `scope` 是部署的每个命名空间，其证据来自当前命名空间，其 `apply` 通过 `via` 指明该设置如何修改。

- **分块大小和步长**（`settings.knowledge_chunking`）：最多 `namespace_tune_document_sample` 个活跃文档（默认 50，按其 `source_uri` 的哈希挑选）会在进程内按当前的 `knowledge_chunk_size` 和 `knowledge_chunk_stride` 重新切分，每个文档使用其写入路径所用的分块器：文本文档用其文本，已摄取的文档用其存储的产物，直到加载了 8 MiB 的文本和产物为止；超出该上限的文档、超过 2,000,000 个字符的文本或超过 8 MiB 的产物计为 `unread`，`load_cap` 给出总量。不写入任何内容，不调用模型，也不返回文本。`chunking` 报告切分结果：块数、每块的字符数、按大小切分后产生的块多于其结构所需块数的小节（`sections_cut`）、最大的嵌入输入（以 token 计），以及超出嵌入器限制的输入数量（当其提供方没有声明 Loom 会检查的限制时为 null）。当有输入超出限制时会起草一个大小（能容纳的最大的更小值，最低到 200），或者当超过四分之一的抽样小节被切分、涉及至少五个文档且限制已知时也会起草（最小的更大值，最高为当前值的四倍，使四分之三的小节在限制内保持完整）；每个候选大小只尝试一次，由近及远，步长与大小保持比例。`preview` 保存在这两个值下对样本的切分。`apply` 是 `settings.set`（`POST /v1/admin/settings.set`，需要平台管理员）并带有 `expected`，`apply_also` 用于设置步长。两条规则都不触发的样本不起草任何内容，并作为证据保留。`knowledge_chunk_size` 为 0 或更小（此时不切分窗口：文本文档是一个块，已摄取的文档每个块对应一个 block）以及 `knowledge_chunk_stride` 为 0 或更小（此时不会对长于该大小的小节或 block 做窗口切分）的情况也一样：两者都不起草大小。
- **提取器路由**（`settings.document_extractors_json`）：如果某个格式的来源的当前任务以 `format_unavailable` 失败，或其已发布的提取不完整，则该格式会被路由到在其能力中声明了它的已配置提取器。来源当前所用的服务按摄取读取的方式，根据其格式和媒体类型确定；草稿会为该格式本身添加一条路由，它优先于媒体类型路由，因此会接管该格式的每个来源。在声明了该格式的服务中（对于不完整的提取，则是那些没有造成不完整提取的服务），路由会指向其来源已经在使用的服务（完整提取的来源，或失败来源的服务现在已声明该格式），否则指向其媒体类型路由所到达的服务，否则指向任意服务；在每一类中，没有造成不完整提取的服务排在前面。路由仍可能移动在另一服务上完整提取的来源，其理由中会点名它们；无法解决不完整提取的路由会在 `skipped` 中说明。草稿通过保存其 URL 的设置来指明服务，而绝不写出 URL（URL 可能带有凭据），因此 `args_complete` 为 false；`via` 为 `environment`，在每个服务器和 worker 重启后生效。没有已配置的提取器声明的格式不起草任何内容，并在 `skipped` 中列出（`extractor.<format>`）；失败来源的服务现在已声明该格式的情况也一样，这类来源应重新摄取。

决策提取（`loom decisions extract` 以及来源同步的挖掘）根据固定文本构建其提示，不接受来自请求、命名空间或组织的指令，因此不为它起草任何指令；当命名空间有文档时，`skipped` 会说明这一点。已存储的文档保留其块和提取结果：重新切分或重新提取它们是单独的操作（`historical`）。

<a id="a-namespaces-guide"></a>

## 命名空间的指南

命名空间的指南是为该命名空间编写的一份 Markdown README：说明命名空间存放什么、键的布局、每个标签的含义，以及应调用哪些 op。agent 在首次写入之前阅读它，并在改变其中某项约定时更新它；人也阅读同一份文本。它只记录关于数据的约定，不记录事实本身，事实仍保存在记忆中，由搜索和擦除覆盖。

| 操作                      | 角色   | 作用                                                                                                   |
| ------------------------- | ------ | ------------------------------------------------------------------------------------------------------ |
| `namespace.guide_get`     | viewer | 返回当前指南（或某个历史 `revision`）：正文、一行摘要、修订号、写入者和备注。                          |
| `namespace.guide_set`     | writer | 当 `expected_revision` 是当前修订号时写入下一个修订；否则拒绝，并返回当前指南以便合并。                |
| `namespace.guide_history` | viewer | 返回最新的若干修订，包含摘要、备注、写入者和长度。                                                     |

每次保存都要指明所基于的修订号。若两个 agent 基于同一修订编辑，先保存的生效，后保存的会收到 `conflict: true` 以及当前最新的指南；该 agent 将自己的改动合并到这份文本中，再用返回的修订号重新保存。服务器自己从不合并文本。重试一次已经生效的保存时，会发现自己的文本已是当前版本，不会写入任何内容。保存时省略 `summary` 会保留当前摘要，`content` 为空则清空指南。终端用户的个人集合（`col-me-…`）没有指南。每个修订都会保留，因此带 `revision` 调用 `namespace.guide_get` 即可读取其中任何一个。

指南无需请求就会送达 agent：

- **会话开始时**，`context.pack` 会渲染一个 `namespace_guide` 部分：先是指南的修订号、写入者和摘要，然后是按该部分预算（200 个 token）截断的正文。正文属于召回的数据，与其他写入者的文本一样，经转义后缩进在同一条目之下。
- **在目录中**，公开命名空间的每个 `namespace.browse` 行都带有 `guide_revision` 和 `guide_summary`，`query` 也会匹配指南的摘要，因此 agent 在众多公开命名空间中查找时，能找到指南描述了所需内容的那一个。对非成员而言，私有行不显示指南，受限行则完全不显示。
- **`namespace.get`** 会返回 `guide_revision` 和 `guide_summary`。

读取指南需要对其命名空间有读取权限，因为公开行的可见性不会扩大任何读取权限：`namespace.browse` 向组织展示每个公开命名空间的摘要，而完整的指南只供其成员阅读（`namespace.join`）。在控制台中，记忆页面标题处，**Add memory** 旁边有一个 **Guide** 按钮，按钮上会显示指南的修订号和摘要；对于没有指南的命名空间，writer 点击后会直接进入编辑器。首份指南无论从这里还是从组织内的命名空间页面开始，都以一份提纲起步，其中列出新人会问的问题（命名空间存放什么、键、标签、如何使用），且在未修改的情况下无法保存。组织内的命名空间页面会在描述下方显示公开命名空间的指南摘要；对于你所属的命名空间，点击摘要即可打开指南，没有显示摘要时则使用 **Guide** 菜单项。writer 可以在这两处任一位置编辑，并像 agent 一样基于所打开的修订保存。擦除命名空间会删除其指南的所有修订。

<a id="the-mentor"></a>

## 导师

`mentor.brief` 返回针对某个 agent 的建议及其依据：该 agent 在一个时间窗口内（默认 7 天）的调用、命名空间提供的各个 plane，以及每条提示和触发它的计数。`context.pack` 在会话开始时最多渲染三条提示，成功的响应最多携带一条（MCP 上为 `hint`，HTTP 上为 `X-Loom-Hint`），上限由 `LOOM_MENTOR_MAX_HINTS_PER_SESSION` 控制。`loom mentor --org … --namespace … --agent …` 会打印完整的 brief。参见 [导师操作](https://github.com/ClickHouse/loom/blob/main/docs/reference/operations/mentor.md)。

brief 的 `usage_evidence` 列出该 agent 使用 Loom 的情况，每个层级一项，并注明记录者（`server`：Loom 亲眼所见；`client`：由调用方上报，未经验证）。这些层级在每次生成 brief 时读取，而不是来自缓存的画像，时间窗口为 brief 所报告的窗口。从调用账本读取的层级（`attempt`、`success`、`committed_write`、`returned_hit` 以及 `memory.outcome` 标签）统计的是账本中记录在该 agent 名下的行，即调用请求体中指明的 agent（对于按密钥发起的调用，则是该行的写入者，无论调用者是谁）：请求体未指明 agent 的调用（按 id 读取、MCP 的 `memory.update`、Python SDK 默认的 `memory.update`），或者跨 agent 读取并记录在 `*` 下的调用（SDK 中的 `memory.search` 和 `memory.ask`、MCP 的跨 agent 读取、未指明 agent 的 `memory.outcome`），都不会被计入。该层级体系不使用画像中的任何计数，因此即使导师处于关闭状态也能给出结果；不针对特定 agent 的 brief 没有层级体系。

| 层级 | 来源 | 统计内容 |
| --------------------- | -------- | --------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `attempt`             | `server` | 发起的调用，无论是否得到应答；不包括控制台或助手发起的调用、brief，以及延迟的 `memory.ask` 留下的裁决 |
| `success`             | `server` | 未返回错误的调用；读取不到内容的读取也计入 |
| `committed_write`     | `server` | 实际存储了一行的 `memory.set` 调用：不包括空操作、试运行或重放 |
| `indexed_write`       | `server` | 在窗口内首次创建的记忆（每个用户、会话和键一条），由任意写入 op 产生，带有当前 embedder 生成的向量，处于可服务状态且未过期 |
| `returned_hit`        | `server` | 未弃答的 `memory.search` 或 `memory.ask`，命中记忆、文档或对话的 `search.query`，以及找到其键的 get |
| `client_acknowledged` | `client` | agent 上报其依赖过的不同记忆键：`run.complete` 的 `memories_used`，以及按 id 提交、标记为 `used` 或 `positive` 且落在某一行上的 `memory.outcome`；不包括按键提交的标签（记录在该行写入者的 agent 名下）、Python SDK 的 `outcome` 调用（没有 agent）或 `memory.outcome_run` |
| `task_outcome`        | `client` | 被评为 `pass` 或 `fail` 的该 agent 的运行（`run.grade`），以评分者上报为准 |

返回命中并不能说明答案已送达模型，或改变了它的工作。`ledger_from` 是该 agent 在窗口内最早的账本行，`ledger_retention_days` 表示已配置的保留策略会将该命名空间的账本行保留多久（0：直到被删除）；窗口长于此值时，从账本读取的层级所覆盖的范围会小于窗口所声明的范围。

[`mentor.guide`](https://github.com/ClickHouse/loom/blob/main/docs/reference/operations/mentor.md#mentorguide) 返回如何使用 Loom 的说明，面向模型编写：即 `skills/loom/` 中的 `loom` skill，`part` 可取 `skill`（循环与规则）、`reference`（每个 plane 及其 op）或 `all`。它对所有调用者都相同，因此任何 viewer 令牌都可以读取，包括限定到某个用户的令牌，以及通过 HTTP 访问的终端用户的令牌。

<a id="throwaway-namespaces"></a>

## 临时命名空间

基准测试和测试工具会写入以 `LOOM_NAMESPACE_BENCH_PREFIXES` 中的前缀命名的命名空间；自动注册会将它们标记为 `kind = bench`。`namespace_gc` 工作进程会清除仍有记忆、但在 `LOOM_NAMESPACE_GC_IDLE_HOURS`（默认 24）小时内没有写入的 bench 命名空间，每批通过一次按列表限定范围的擦除 saga 完成。`loom namespaces-gc --org <org>` 会列出候选命名空间，`--erase` 则手动清除它们；`loom namespaces-adopt` 会为那些持有记忆但没有注册表行的命名空间添加一条所有者行。

<a id="settings"></a>

## 设置

| 设置                                                           | 作用                                                                                    |
| -------------------------------------------------------------- | --------------------------------------------------------------------------------------- |
| `LOOM_NAMESPACE_AUTOREGISTER_ENABLED`                          | 首次写入时注册该命名空间。                                                              |
| `LOOM_NAMESPACE_BENCH_PREFIXES`, `LOOM_NAMESPACE_GC_*`         | 哪些命名空间是一次性的，以及 GC 何时清除它们。                                          |
| `LOOM_MENTOR_TTL_SECONDS`, `LOOM_MENTOR_MAX_HINTS_PER_SESSION` | mentor 缓存，以及每个会话中每次调用提示的数量上限；TTL 为 0 时禁用 mentor。             |
| `LOOM_CONFLICT_THRESHOLD`                                      | 命名空间继承的部署级冲突阈值。                                                          |

参见[配置：命名空间注册与 GC](https://github.com/ClickHouse/loom/blob/main/docs/reference/configuration.md#namespace-registration-and-gc)和[配置：上下文](https://github.com/ClickHouse/loom/blob/main/docs/reference/configuration.md#context)。

<a id="limits"></a>

## 限制

- 命名空间名称长度为 1–128 个字符，建议使用 kebab-case。
- `namespace.delete` 是软删除。它不会删除记忆，只有擦除操作才会删除。
- 修改 `default_conflict_policy` 或 `retrieval_defaults` 会改变之后的写入和搜索行为，已存储的行不会被重新判定。
- `memory.promote` 只在一个命名空间内共享。其他命名空间只能通过副本（`memory.copy_to`、`memory.cross_promote`）看到某条事实，副本不会跟随源记忆之后的修改。

<a id="related"></a>

## 相关内容

- [身份与命名空间](../concepts/identities-and-namespaces.md)：模型说明。
- [令牌与授权](https://github.com/ClickHouse/loom/blob/main/docs/guides/tokens-and-grants.md)：创建令牌并限定访问范围。
- [控制台](dashboard.md)：组织中的命名空间、设置和管理抽屉。
- [安全架构](https://github.com/ClickHouse/loom/blob/main/docs/architecture/security.md)：请求如何获得授权。
- [操作参考：命名空间](https://github.com/ClickHouse/loom/blob/main/docs/reference/operations.md#namespace)、[授权](https://github.com/ClickHouse/loom/blob/main/docs/reference/operations.md#grants)、[导师](https://github.com/ClickHouse/loom/blob/main/docs/reference/operations.md#mentor)、[agent](https://github.com/ClickHouse/loom/blob/main/docs/reference/operations.md#agents)。
