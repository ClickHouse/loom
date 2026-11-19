<!-- i18n: source=docs/concepts/identities-and-namespaces.md blob=05f85261bcf9106427aff88cb70b9f1b5e978a92 model=claude-sonnet-5-5 -->
> 本页由机器从[英文原文](https://github.com/ClickHouse/loom/blob/main/docs/concepts/identities-and-namespaces.md)翻译，内容以英文原文为准。
> Machine-translated from the [English page](https://github.com/ClickHouse/loom/blob/main/docs/concepts/identities-and-namespaces.md), which is authoritative.

<a id="identities-and-namespaces"></a>

# 身份与命名空间

Loom 在每次调用时都要回答两个问题：这一行属于谁（身份元组，外加由服务器写入的两列），以及调用方是否可以访问它（令牌、命名空间的授权及其分类）。本页说明这一模型。铸造和撤销令牌的方法见[令牌与授权](https://github.com/ClickHouse/loom/blob/main/docs/guides/tokens-and-grants.md)；强制执行机制见[安全](https://github.com/ClickHouse/loom/blob/main/docs/architecture/security.md)。

<a id="where-namespaces-fit"></a>

## 命名空间的定位

- **org** 是租户。
- **命名空间**是 org 内共享和访问的单位。一个部署通常包含：每个团队或项目一个命名空间，每个必须保持隔离的 bot 一个命名空间，每个人一个 `personal-<slug>`，以及每个被索引的分支一个 `code-<repo>[-<branch>]`。
- 命名空间的每个成员都可以读取其中的每一行。
- `agent` 记录是谁写入了某一行，它不是边界。
- `user_id` / `read_scope=own_user` 用于在同一个 bot 的命名空间内区分不同的终端用户。
- 访问权限是命名空间上的一个角色（viewer/writer/admin），由令牌携带，或通过授权（grant）添加。

<a id="the-identity-tuple"></a>

## 身份元组

每一行都带有五个身份列：

```
(org, namespace, agent, user_id, session_id)
```

| 部分         | 含义                                                                 | 默认值                                                                                                |
| ------------ | -------------------------------------------------------------------- | ----------------------------------------------------------------------------------------------------- |
| `org`        | 租户。除通过通配符运维令牌外，任何内容都不能跨越 org 边界。          | （必填；单用户模式会自动填充，见下文）                                                                |
| `namespace`  | org 内部隔离与共享的单位。每次读取都限定在 `(org, namespace)` 范围内。 | （必填；单用户模式会自动填充，见下文）                                                                |
| `agent`      | 写入该行的一方（`support-bot`、`claude-code`）。表示作者身份，不是读取边界。 | （必填；单用户 HTTP 模式在部分操作中会自动填充，见 [HTTP 参考](https://github.com/ClickHouse/loom/blob/main/docs/reference/http-api.md#request)） |
| `user_id`    | 该行所涉及的终端用户。                                               | `-`                                                                                                   |
| `session_id` | 一次对话，或一次对话的一次运行。                                     | `-`                                                                                                   |

许多操作还接受 `run_id`（默认值为 `-`），用于将写入或读取限定到某一次任务执行。令牌决定调用方可以访问的 org 和命名空间；元组的其余部分随每个请求体一起传递。SDK 在构造客户端时绑定 `(org, namespace, agent)`，并在每次调用时传入 `user_id`、`session_id` 和 `run_id`，调用中未指定的字段一律为 `-`。当其中某个字段用于过滤读取时（搜索、ask、启动包、对话和运行列表、`memory.update` 的 key 查找），`-` 表示不过滤：未指定 run 的搜索会读取所有 run 的行。当该字段是行的地址时（`memory.get`、`memory.list`、`memory.stack`、工作记忆、profile 写入、已保存的消息），`-` 表示写入的是不带 user、session 或 run 的行。

**读取跨越所有作者。** 读取默认使用 `agent="*"`：命名空间中的每一行，无论由谁写入。传入 `agent` 只会缩小范围。读取时的 `user_id` 和 `session_id` 会过滤出在其下写入的行。

**为什么元组排在表的最前面。** `loom.memory` 和 `loom.memory_embeddings` 的排序键都以 `(org, namespace, agent, user_id, session_id)` 开头，因此限定到某个命名空间的读取在 ClickHouse 中是主键范围查询，而不是全表扫描，指定 agent 还能进一步缩小范围。跨 agent 的读取（默认情况）会在该命名空间的范围内过滤 `user_id` 和 `session_id`：排序键只能在 agent 列允许的程度内跳过它们对应的 granule，也没有任何跳数索引覆盖它们。任何限定范围的读取，对其范围所跨越的每个 part，至少仍会读取一个 granule（[大规模下的记忆](https://github.com/ClickHouse/loom/blob/main/docs/benchmarks/results.md#memory-at-scale)）。

<a id="what-the-server-stamps"></a>

### 服务器写入的内容

另有两列在每次写入时由服务器决定，绝不从请求体中读取，唯一的例外是调用方可以将 `source_class` 降级（`actor` 见 `server/memory/app/provenance.py`，`source_class` 见 `server/memory/domain/provenance.py`）：

| 列             | 取值                                                                   | 决定方                                                                                                   |
| -------------- | ---------------------------------------------------------------------- | -------------------------------------------------------------------------------------------------------- |
| `actor`        | `human:<subject>` 或 `process:<subject>`（服务账号）                   | 经过验证的令牌（`writer_actor`）。排队的作业或 CLI 导入以发起它的主体身份写入。                          |
| `source_class` | `asserted`、`extracted`、`observed`、`derived`、`imported`、`external` | 操作本身以及 `source_trace` 约定（`source_class_for`）。                                                 |

请求只能声明将 `source_class` 降级为：`external`（来自工具或页面的内容）、`imported` 或 `derived`。请求其他任何值都会返回 422。原因在于来源信息会影响排序以及 `memory.ask` 对证据的选择：如果调用方可以把抓取的文本标记为 `asserted`，这个标签就失去了意义。`agent` 由调用方提供，因为它表示系统中的一个角色，而 `actor` 是其背后经过身份验证的主体。

<a id="orgs-and-namespaces"></a>

## 组织与命名空间

**组织**（org）是一个租户；**命名空间**是数据行所在的位置，也是授予访问权限的单位。命名空间在以下几种情况下产生：通过注册（`namespace.register`，调用者成为其 `admin`）、在注册账号时认领、由服务器预置（单用户命名空间，以及为每个登录用户创建的 `personal-<slug>` 命名空间），或为已建立索引的仓库而创建。它在 `loom.namespaces` 中的目录行保存其配置：描述、类型、可见性、加入策略、冲突与提案策略、成本上限、保留期限和检索默认值。

| 命名空间类型 | 存放内容                         | 读取方                                                   |
| ------------ | -------------------------------- | -------------------------------------------------------- |
| `memory`     | 事实、文档、对话：默认类型       | 记忆、知识和对话相关操作，以及扇出（fan-out）            |
| `code`       | 一个已建立索引的仓库和分支       | 按名称寻址的 `code.*` 操作；代码范围下的 `loom_search`   |
| `bench`      | 基准测试语料库                   | 基准测试；其流量归类为 `bench`                           |

遍历调用者所有命名空间的搜索扇出只覆盖 `memory` 类型（`LOOM_MEMORY_SEARCH_FANOUT_KINDS`）：为查找个人事实而扫描每个代码索引，每个索引都要花费一条语句，却一无所获。

**为什么用命名空间，而不是每个 agent 一个存储。** 常见情况是多个 agent 和多个人需要看到相同的事实：团队的部署窗口、客户的历史记录。把命名空间作为读取边界，意味着在其内部默认共享，跨命名空间则默认不共享；给某个人或机器人授权，是通过在命名空间上授予角色，而不是复制数据行。

<a id="visibility-joining-and-restriction"></a>

### 可见性、加入与限制

| 设置                | 取值                                            | 作用                                                                                                                  |
| ------------------- | ----------------------------------------------- | --------------------------------------------------------------------------------------------------------------------- |
| `visibility`        | `private`（默认）、`public`                     | 公开命名空间会列在 `namespace.browse` 中，并且可以加入。可见性本身不授予读取权限。                                    |
| `join_policy`       | `open`、`approval`、`closed`、`inherit`（未设置） | `namespace.join` 的行为。对于不要求审批的公开命名空间，`inherit` 解析为 `open`，否则解析为 `approval`。              |
| `requires_approval` | `true`、`false`                                 | 仅在 `inherit` 下生效：此时每次加入都要等待 admin 批准（`grants.approve`）。                                          |
| `classification`    | `standard`（默认）、`restricted`                | 关闭所有放宽默认值（见下文）。                                                                                        |

在 `open` 命名空间上，`viewer` 加入立即生效，而 `writer` 或 `admin` 加入则处于待定状态，直到 admin 批准；在 `approval` 下，每次加入都处于待定状态；`closed` 拒绝加入。

**受限**命名空间（目录行上或 `LOOM_CODE_REPOS_JSON` 中对应条目上设置 `classification=restricted`）只有在满足以下全部条件时才放行调用：令牌属于该命名空间的组织，按字面写出该命名空间（不通过 `*`），在其中持有的角色达到该操作所需的角色，并且令牌自身的主体持有有效的直接授权。任何组织可读规则、公开可见性、通配符、继承的服务账号授权、`org_admin` 或 `platform_admin` 都不能绕过它，拒绝时也不会透露该命名空间是否存在。扇出、上下文包和汇总会跳过调用者无法读取的受限命名空间，每个被放行的请求都会被审计。具体的设置步骤见[令牌与授权](https://github.com/ClickHouse/loom/blob/main/docs/guides/tokens-and-grants.md#restricted-namespaces-classificationrestricted)。

<a id="roles"></a>

## 角色

角色是有序的，较高的角色满足所有针对较低角色的检查。每个操作都声明其所需的最低角色（`GET /v1/operations` 会列出）。

| 角色             | 授予的权限                                                                                        | 获得方式                                                                                                                                                                                      |
| ---------------- | ------------------------------------------------------------------------------------------------- | --------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `viewer`         | 读取。                                                                                            | 令牌声明或命名空间授权。                                                                                                                                                                      |
| `writer`         | 读取，并向记忆、知识和其他数据平面写入。                                                          | 令牌声明或命名空间授权。                                                                                                                                                                      |
| `admin`          | 管理一个命名空间：授权、配置、策略、恢复，以及提升到共享层。                                      | 令牌声明或命名空间授权；注册命名空间后，你即成为其 admin。                                                                                                                                    |
| `org_admin`      | 组织范围的视图（例如不指定命名空间的成本）。                                                      | 系统可识别的准入条件，产品内没有分配途径。                                                                                                                                                    |
| `platform_admin` | 跨组织的运维视图。                                                                                | 登录时授予 `LOOM_PLATFORM_ADMINS` 中列出的账号和已验证邮箱，也授予以 `--platform-admin` 注册的 bot 的令牌；仅为服务账号签发，并带有过期时间。                                                  |

授权只包含 `viewer`、`writer` 或 `admin`，Loom 签发的令牌也是如此，但 `platform_admin` 令牌除外：组织范围的权力由运维人员在配置中或注册 bot 时决定，不能由令牌带走。代码命名空间有读取者（`viewer`）和维护者（`admin`），没有 `writer`。

<a id="who-may-call-what"></a>

## 谁可以调用什么

当命名空间未受限（或通过了受限检查），并且令牌自身对该命名空间的角色达到操作所需的角色，或者存在有效的授权时，对 `(org, namespace)` 的调用才被允许。

- **令牌**（`sub`、`org`、`namespaces`、`roles`，以及可选的 `ns_roles` 和 `ns_write`）说明调用方读取哪些命名空间（按优先级排序）以及以什么角色读取。**限定范围**的令牌（`ns_roles`，按命名空间设置角色）仅对其列出的命名空间具有权威性。**扁平**令牌会把它的 `roles` 应用到其指明的每个命名空间。`ns_write` 把写入限制在其中一个子集，因此同一个令牌可以读取团队命名空间，而只写入自己的命名空间：

  ```
  namespaces = ["team-shared", "agent-private"]
  ns_write   = ["agent-private"]
  ```

- **授权**（`loom.namespace_grants`，每个 `(org, namespace, subject)` 一行）在令牌所带角色之上，为某个命名空间增加一个角色。
  - **声明不会超出授权。** 当主体在令牌所指明的命名空间上持有授权行（无论状态如何）时，声明的范围不会超出该授权：被撤销或被降级的授权（`grants.revoke`、带 `replace=true` 的 `grants.create`、`namespace.leave`、被释放的声明、一次恢复）会终止所有较早令牌在该处的声明，而待处理或被拒绝的请求会把它限制为无权访问。限定范围的令牌在该处保留其自身的角色，但不超过有效授权。
  - **不受影响的情况。** 主体从未持有授权的命名空间会保留其声明；如需取消，请撤销该令牌。
  - **生效时机。** 修改授权的进程会立即应用，其他进程（无论在本服务器还是其他服务器上）会在 `LOOM_ACL_WATERMARK_POLL_SECONDS`（默认 1 s）内应用。
- **服务账号会继承。** 在某个命名空间上没有自身授权的服务账号，会使用其创建者在该处的授权，因此从控制台创建的令牌会跟随创建它的人，受限命名空间除外。
- **通过组织范围 id 寻址的操作**，例如 `memory.get_by_id`，会在持有该行的范围上对调用方进行授权。当调用方在该行所在命名空间上不具备该操作所需的角色时，其应答与该行不存在时相同，因此拒绝永远不会确认该 id 存在（[行级范围操作](https://github.com/ClickHouse/loom/blob/main/docs/architecture/security.md#row-scoped-operations)）。

为什么同时使用令牌和授权：令牌在每次调用时于本地验证，无需查询，使热路径保持低开销；授权则让管理员无需重新签发令牌即可授予和收回访问权限。撤销信息通过 ClickHouse 共享，并在撤销缓存间隔内到达每个副本。

<a id="reading-several-namespaces"></a>

## 读取多个命名空间

一次搜索可以同时读取多个命名空间：`namespace_scope` 取 `this`（被寻址的命名空间，默认值）或 `all_mine`（被寻址的命名空间加上调用方自己的命名空间）。`loom_search` MCP 工具和 `context.pack` 使用相同的扇出方式。记忆搜索会在 `namespaces_searched` 中列出范围内的命名空间（分层搜索可能在读完最后一个之前停止），并在 `namespaces_skipped` 中列出因上限而被排除的命名空间。

调用方自己的命名空间取决于令牌：

| 令牌                                                        | `all_mine` 增加的命名空间                                                                      |
| ----------------------------------------------------------- | ---------------------------------------------------------------------------------------------- |
| 平面令牌（大多数令牌；`loom mint-token`、自助注册）          | 令牌指名的命名空间，加上该主体持有授权的每个命名空间                                           |
| 限定范围令牌（`ns_roles`；用于 _Only what you choose_ 的令牌） | 恰好是令牌列出的命名空间，按其顺序；之后新增的授权不会加入                                     |
| 带 `*` 的委托令牌（用于 _Everything you belong to_ 的令牌）  | 创建者当前有效的授权，因此此人之后加入的命名空间也会被搜索                                     |
| 指名命名空间的委托令牌                                      | 它所指名且创建者仍持有授权的那些命名空间                                                       |
| 无令牌，关闭认证（`single-user`）                           | 单用户持有授权的命名空间：默认命名空间和其注册的每一个命名空间                                 |

随后列表会被缩小：

- **种类。** 只扫描种类在 `LOOM_MEMORY_SEARCH_FANOUT_KINDS`（默认 `memory`）中的命名空间。代码和基准测试命名空间按名称读取，或使用它们各自的工具。例外是代码范围下的 MCP `loom_search`：当调用方有权读取时，它还会读取该范围内仓库的主干代码命名空间，以获取其中的文档和决策（[统一搜索](https://github.com/ClickHouse/loom/blob/main/docs/features/unified-search.md#which-namespaces-it-reads)）。
- **受限命名空间** 仅在调用方通过受限检查时保留。
- **上限。** 除被寻址的命名空间外，最多读取 `LOOM_MEMORY_SEARCH_FANOUT_MAX_NAMESPACES`（默认 32）个，按调用方列表的顺序：限定范围令牌的顺序，或委托令牌指名的列表；否则按名称排序。限定范围令牌保留排在前面的命名空间；对其他调用方，截断时会先丢弃公共命名空间，再丢弃私有命名空间。记忆、知识和对话搜索会在 `namespaces_skipped` 中列出其余的命名空间（分页记忆搜索的每一页也是如此），`loom_search` 则在其文本中说明；按名称寻址即可读取它。

被寻址的命名空间总是会被搜索，而且最先搜索：扇出只会扩大搜索范围。记忆搜索随后分层读取。对于限定范围令牌，其他命名空间按其顺序依次读取，搜索在遇到第一个有可信匹配的命名空间时停止。对于其他调用方，先读取被寻址的命名空间和私有命名空间，只有在这第一轮结果不足时才读取公共命名空间。

调用方可以读取的某些命名空间不属于它自己，也从不会被扫描：通过 `*` 声明或组织、平台角色可达的命名空间，它尚未加入的公共命名空间，以及它通过写入而创建的命名空间（这些命名空间有所有者记录但没有授权，因此每个任务写入一个命名空间的测试框架不会扩大之后的每一次搜索）。请按名称寻址它们。一次搜索只覆盖一个组织，即令牌所属的组织。

<a id="read-scope-inside-a-namespace"></a>

## 命名空间内的读取范围

成员资格是读取边界，这对团队共享的记忆是合适的，但对于通过一个令牌为许多终端用户服务的单个 agent 就不合适：来自某一用户的提示可能把另一用户的记忆拉进回答中。`read_scope` 在命名空间内部限定读取范围：

| `read_scope`          | 读取返回的内容                                        |
| --------------------- | ----------------------------------------------------- |
| `namespace`（默认）   | 命名空间的所有行                                      |
| `own_user`            | 在请求所指定的 `user_id` 下写入的行                   |
| `own_session`         | 在请求所指定的 `session_id` 下写入的行                |

`memory.search`、`memory.search_trace`、`memory.ask`、`memory.recall`、`context.pack`、`namespace.inventory`、`profile.render` 和 `profile.structured` 支持该参数；`profile.structured` 拒绝 `own_session`，因为用户的画像跨越其多个会话。令牌可以携带 `read_scope` 声明（以及固定的 `read_user_id`），它是一个上限：请求可以收窄它，但不能放宽它；如果有范围限定的读取没有提供其所绑定的 `user_id` 或 `session_id`，则会以 `IDENTITY_INCOMPLETE` 被拒绝，而不是悄悄放宽。为多用户 agent 签发 `own_user` 令牌。

两种传输方式都在受理调用时一次性解析范围，该调用中对记忆行的每次读取都会带上这一范围，无论是哪个 op 发起的读取：`memory.get`、`get_by_id`（另一用户的 id 会返回 `found=false`）、`list`、`bulk_get`、`history`、`timeline`、`related`、`evidence`、`conflicts.list`、`memory.proposals`、会话简报、摘要、流程、工作记忆以及 `loom_search` 的各个分支。在比 `namespace` 更窄的范围下：

- `user_id="*"` 会以 `AUTH_INSUFFICIENT` 被拒绝，在 `own_session` 下 `session_id="*"` 同样如此。
- 用 `read_user_id` 固定的令牌只能以该用户身份行事，写入也一样：请求体中指定了其他 `user_id` 的，在每个 op 上都会以 `AUTH_INSUFFICIENT` 被拒绝，未指定（或为 `-`）的请求体则以被固定的用户身份运行。
- 写入的答复不会指出范围之外的任何行：合并到其他用户的行、与其重复，或与其被固定的行冲突，都只报告结论，而不给出该行的 id、哈希或键，`conflicts.list` 也会从 `conflict_with` 中去掉这类对端。被固定的行仍然会阻止任何用户的相近写入（这是命名空间范围的不变量）：冲突距离内存在他人被固定的行时，无论写入者自己的行有多近，该写入都会被拒绝，且不指明是哪一行。被收窄的写入者的检查，会在其自身范围内的读取之外，再读取命名空间中被固定的行（其类型取自记忆行），因此开销至多约为命名空间范围写入者的两倍。延迟执行的 `memory.set_from_messages`（`defer=true` 或 `extraction_defer_agents`）会连同请求的范围一起存储，并在该范围下重放。使用其他用户的 `run_id` 调用 `run.begin` 会以新的 id 启动一个新的运行，而对它调用 `run.complete` 会返回 `NOT_FOUND`。由调用方提供 id 的 begin 会在 ClickHouse Keeper（`loom.agent_run_claims`）中认领该 id，并在其运行行写入后释放认领，因此两个用户对同一 id 的并发 begin，恰好只有一个保留该 id。在写入运行行之前失败的 begin 会保留认领，其他用户的 begin 因而无法取得该 id。当 Keeper 出现故障时，begin 只检查运行行；此时被收窄的令牌只能为重新打开自己已有的运行而保留所提供的 id，新运行则会得到服务器生成的 id，因此自行生成 id 的客户端必须使用返回的 `run_id`。
- 会话简报按用户保存（在 `own_session` 下按会话保存）：两个用户保存同一个 `brief_key` 会得到两份简报，各自只能看到自己的，而命名空间范围的令牌会在以 `~<user>~`（或 `~<user>~<session>~`）为前缀的键下列出它们。
- `code.search` 用该范围自身用户最近的文件来打破平局，无论请求体指定的是哪个 `user_id`。
- `memory.search` 的术语分支在任何读取范围下都会提供受治理的定义：管理员已将其发布给该命名空间的所有读取者（[搜索中受治理的定义](#governed-definitions-in-search)）。
- 范围无法限定的 `context.pack` 分支（不带用户的对话，以及读取某用户所有会话的运行清单）不返回任何内容。
- `namespace.inventory` 在范围内统计记忆和运行，以及其记忆行上出现的 agent、作者和身份主体（identity subject，而非主题或话题）；文档和成员则按命名空间范围统计（与 `knowledge.*` 和 `grants.list` 读取它们的方式一致），而记录（每个键一条，取最后写入者）和集成（`webhook.list` 会被拒绝）则标为未统计（`complete: false`）。
- 黑名单模式会拒绝所有用户的写入，却不记录用户（其 `created_by` 是令牌主体，同一后端的所有按用户令牌共用它），因此每个 `blacklist.*` op 都会以 `AUTH_INSUFFICIENT` 被拒绝：模式只能用命名空间范围的令牌来添加、列出、移除和清理。模式仍会拒绝被收窄令牌的写入，且该 422 不会给出模式 id 或原因。
- `policy.decisions` 只列出调用方自己的决策。`memory.unlock` 和 `memory.unpin`（管理员）只作用于调用方自己的行，与 `memory.lock` 相同。
- 无法把答复限定到单个用户的 op（实体图 `facts.*`、`conversation.*`、命名空间导出、管理和整理类 op、`ops.projection_failures.*`、`erasure.*`、`audit.list`、`cost.agent_summary`、`agents.connected`）会以 `AUTH_INSUFFICIENT` 被拒绝，对不以用户为键的表的写入也一样，因为它会覆盖其他用户的行：`memory.record.write`、`decision.record`、`procedure.record_run` 和 `context.offload`。`memory.set_from_messages` 会跳过其向记录表的 `record_schema` 投影，并在 `degraded` 中说明。范围无法解析的令牌（`own_session` 上限而请求体未指定会话，或未固定的 `own_user` 令牌而请求体未指定用户），在其限定范围的每次读取，以及请求体带有用户或会话的每次写入（和 `cost.summary`）上都会被拒绝，而不是以用户 `-` 的身份运行。因此在 `own_session` 下，没有会话字段的 op（`cost.summary`、`memory.outcome`）总是被拒绝。代码索引 op（`code.*`）带有用户仅用于归属，会被受理；其中对记忆的读取则会被拒绝。命名空间范围的规则（`policy.set`、`policy.delete`、`memory_schema.register` / `propose` / `approve`、`blacklist.*`）需要命名空间范围的令牌，`policy.check` 也是如此，因为它会对所有用户的调用评估调用方的规则。该清单位于 `server/identity/domain/read_scope.py`（`SCOPED_OPS`、`NARROW_SAFE_OPS`），其中未列出的 op 会被拒绝。

<a id="agent-written-rows-and-the-shared-layer"></a>

## agent 写入的行与共享层

在一个命名空间内，`loom.memory` 中的行以完整元组为键：同一个 `memory_key` 由两个 agent 写入，或针对两个用户写入，就是两行。这是每个 agent 的（“私有”）层，也是 `memory.search` 默认读取的层（`scope="private"`）。这里的“私有”指的是_未被提升_，而不是_被隐藏_：除非 `read_scope` 加以限制，命名空间的每个读取者都可以读取这些行。

**共享层** `loom.shared` 对每个 `(org, namespace, memory_key)` 保存一行：这是由命名空间所有的、某个事实的规范版本，它不再属于最初学到它的 agent。`memory.promote` 将一行复制到该层，并附带理由和可选的审批人；`memory.demote` 将其撤下（响应会说明这是否发生在该次提升的 `reversible_for_days` 窗口内，默认 7，最大 90）。使用 `scope="shared"` 的搜索读取该层，`memory.lineage` 可将共享行追溯到其提升记录。`memory.promote` 和 `memory.demote` 需要 `admin`。`memory.cross_promote` 将一行连同来源信息复制到另一个命名空间，调用者须在两个命名空间中都是 admin。

`definition` 只有在满足以下条件时才会被提升：

- 来自其有效的命名空间级行（agent、user 和 session 均为 `'-'`，状态为 `active`，未被取代而下线，且未超过其 `expires_at` 或 `valid_to`），位于 `definition/` 键下，并提升到同一个键；其他类型的行绝不会被提升到 `definition/` 键下。否则返回 422 `DEFINITION_PROMOTION_REFUSED`。
- 带有其所批准的哈希（`approved_sha256`，即该行的 `content_sha256`）：缺少则被拒绝（422 `VALIDATION_FAILED`），文本已不再对应该哈希时返回 409 `PRECONDITION_FAILED`。
- 其值是一个 JSON 对象，包含非空字符串 `term`，以及可选的字符串列表 `aliases`；否则返回 422 `VALIDATION_FAILED`。
- 未被标记为 `medium`，且读取时不被评为 `medium` 或 `high`（422 `SENSITIVE_DEFINITION_REFUSED`；`high` 标记会先返回 `HIGH_SENSITIVITY_PROMOTION_REFUSED`，与任何行相同）。
- 在 `memory_shared_definitions_max` 以内：新键已达上限时，或命名空间已超出上限时的任何提升，都会被拒绝（409 `DEFINITION_CEILING`）。
- 所用名称未被其他有效的共享 definition 用作 term 或别名（409 `DEFINITION_NAME_TAKEN`，会指出相关的键和名称）；名称比较采用折叠形式：去除格式字符、NFKC、合并空白、大小写折叠，然后再次 NFKC。

同一个命名空间的 definition 提升（含其上限与名称检查）以及 definition 降级逐个运行，最后运行者决定该 definition 是否被共享（若另一个操作持有该命名空间超过 10 秒的等待，则返回 503 `OVERLOADED`）。降级一个 definition 会归还其名称和名额。令牌的操作者即其 `promoted_by`：`approver` 和 `promoted_from_candidate_id` 会被忽略，并列在 `ignored_args` 中，且不会因另一个键下存在相近的共享行而拒绝。`learnings.review` 不会把类型为 `definition` 的候选项提升到记忆中。

共享 definition 在以下情形下是**受管的（governed）**：它恰好由一行提升而来，即其键下的命名空间级行，且该行为 `active`，未被取代而下线，未超过其 `expires_at` 或 `valid_to`，既未被标记也未被评为 `medium` 或 `high`，并且其文本与共享副本的文本一致（比较两者的服务哈希，因此存储时对象键顺序不同不算变化）。编辑、重新标记为 `medium` 或 `high`，或对来源打墓碑标记，都会使其不再受管；将编辑还原为已批准的文本后，无需新的提升即可重新受管。当共享 definition 的命名空间级来源已不存在（被删除，或被过期清理打了墓碑标记），或被标记为 `medium` 或 `high` 时，`memory.search` 会将其排除，因为共享层没有自己的标签。当来源无法读取时，它会排除所有共享 definition。由命名空间级行以外的行提升而来的副本，由其键下的命名空间级行来判定：没有该行则不返回，否则按该行的标签决定是否返回。副本本身会保留：改回标签，或重新写入来源，即可再次提供它，`memory.demote` 则会将其撤下。`memory.definitions.list`（admin）列出命名空间中有效的共享 definition，按提升时间从新到旧排序，最多为 `memory_shared_definitions_max` 的两倍，并以 `count` 给出全部数量。每个条目带有其键和 `promoted_at`；位于上限之内的条目还带有其 term 和别名、`verdict`（`governed` 或 `lapsed`）、`lapse_reason`（`not_namespace_wide`、`no_term`、`gone`、`superseded`、`conflicted`、`duplicate`、其他状态对应的 `not_active`、`expired`、超过其 `valid_to` 的 `ended`、`changed` 或 `sensitive`），对于受管的条目还有 `lost`：它输给另一个持有该名称的受管 definition 的每个名称，以 `name` 和 `winner` 的键表示（最近提升者胜出，平局由其提升事件决定）。超出上限的条目为 `capped`，不会读取其来源。

<a id="governed-definitions-in-search"></a>

### 搜索中的受管 definition

当查询提到某个受管 definition 的 term 或其某个别名时，`memory.search` 会优先返回它，并标记 `governed: true`。这一 term 分支在 `memory.search` 运行的所有位置都会运行：在 `memory.ask` 和 `context.pack` 内部，以及 `loom_search` 的记忆部分。

- **读取内容。** 只读取所寻址命名空间中有效共享 definition 的名称，无论 `namespace_scope` 扩展到哪些命名空间，最多读取 `memory_shared_definitions_max` 个（超出时取最近提升的，且 `term_leg` 为 `capped`）。它会读取查询所提到的 definition，以及与其中某个共享名称的 definition 的完整值，并读取和评定其来源，不读取其他的；不保证顺序一致性：片刻前在另一个副本上进行的提升可能不会出现在搜索中。它与其他分支并行运行，其他分支完成后，搜索最多再为它等待 `memory_term_leg_wait_ms`（重入的一轮不会超过其剩余时间）。固定的工作预算还限制了规范化、名称比较和用于校验的正文解码。工作量过大会使整个分支以 `timed_out` 被排除；已存储的 definition 和提升规则不变。
- **匹配。** 查询和每个名称都会去除格式字符，经过 NFKC 并合并空白；组合标记会与其前面的字母或数字相连。名称按完整字符匹配，且其边缘不能有继续延伸的相邻字符：紧邻拉丁字母、数字、`_`、`+` 或 `#` 时，不能再有这些字符；紧邻以空格分词书写的其他文字（西里尔文、希腊文、天城文）的字母时，不能有拉丁字母以外的任何字母或任何数字；位于韩文字母之前时，不能有韩文字母，其后则不限，因为后面会跟助词；紧邻汉字、假名、泰文、老挝文、高棉文、缅甸文或标点时，不限制。因此 `MRR` 能匹配“什么是ＭＲＲ”，`ARR` 能匹配“ARR은”，而 `рост` 不能匹配“простой”内部。大小写按名称中的每个词决定：全部大写的词，或首字母之后含有大写字母的词，只按原样匹配（`IT` 不匹配 "what is it"）；其他词按大小写折叠匹配。
- **排位。** 被提到的 definition 排在返回列表的前面，按其名称在查询中首次出现的顺序排列，两个名称同时起始时较长的名称在前，再按最近提升者排序；派生聚合的计算行仍在它们之上。同一命名空间中同一键下的任何其他命中都会被排除。它们计入 `top_k`，且最后才被截断；被截断的内容，无论是受管命中还是被它挤出的命中，都会排在下一页的开头。被搁置的受管命中在返回之前，会根据原始查询以及当前的提升和来源再次检查；检查未能读取时则不返回。完整正文计入 `body_budget_tokens`；放不下的 definition 保留一段标记为截断的摘录。没有下限、阈值或后续阶段会丢弃它们或改变其顺序：包括 `min_similarity`、校准、`loom_search` 的下限和答案优先规则、tracker 判定的记忆上限（它们计入其中）、`context.pack` 的打包，以及 `memory.ask` 对重述值的检查。
- **分数。** `similarity` 和 `distance` 取自该 definition 的共享嵌入（没有嵌入时分别为 `0` 和 `1`），`rank_score` 高于所有命中，但低于计算行，且 `below_threshold` 从不设置。返回了此类 definition 的响应中 `abstain` 为 false；`retrieval_confidence` 和 `retrieval_risk` 描述的是其他命中。
- **关闭时。** 页面句柄，以及要求其他行的字段：`memory_type`、`tags` 或 `category`（不含 `tags_soft` 或 `category_soft`）、`as_of`、`min_body_chars`、大于 0 的 `recent_first` 或 `anchor_first`，或不含 `memory` 的 `legs`。无效的 Unicode 查询在受限的 Unicode 检查读取它时会关闭该分支；超出该检查工作量上限的查询会在校验之前报告 `timed_out`。指明读取谁的行的字段（`agent`、`user_id`、`session_id`、`session_ids`、`run_id`、`scope`）不会关闭它，而 `exclude_keys` 会排除其对应的键。
- **`term_leg`** 在响应中说明该分支做了什么：`complete`、`empty`（没有 definition）、`capped`、`timed_out`、`failed`（读取失败，因此没有返回任何内容）或 `off`。
- **范围。** 在任何读取范围下，包括默认的 `scope` 为 `private` 时，它都会返回受管 definition：admin 已将它们发布给命名空间的每个读取者。
- **未命中。** 返回了受管 definition 的搜索，或其分支未报告 `complete` 或 `empty`（包括 `off`）的搜索，不会记录[知识缺口](https://github.com/ClickHouse/loom/blob/main/docs/architecture/knowledge-gaps.md#recording-misses)未命中。
- **已存储的答案。** 提升和降级都会推进命名空间的记忆写入标记，因此标记落定后，`memory.ask` 不会再复用此前的答案。

为什么有两层：agent 以自己的身份自由且常常重复地写入，而命名空间中所有人都应依赖的精选事实，需要一个规范的键、所有者的决定和审计轨迹。提升就是那个决定。

<a id="identity-modes"></a>

## 身份模式

调用方如何获得令牌，取决于部署的身份模式（`LOOM_IDENTITY_PROVIDER`）：

- **`single-user`**：不做身份验证；在 MCP 上，除 `code.*` 之外的所有 op，未指定命名空间的调用都会解析到一个命名空间（`LOOM_DEFAULT_NAMESPACE`，默认 `local`，所属组织为 `LOOM_SELF_SIGNUP_ORG`，默认 `dev`）。在 HTTP 上，不带令牌的调用，除 `code.*` 和 `tracker.*` 之外的所有 op 都会得到该组织，常用的记忆和搜索 op 还会得到该命名空间；具体是哪些 op，以及各自得到的 agent，见 [HTTP 参考](https://github.com/ClickHouse/loom/blob/main/docs/reference/http-api.md#request)。`all_mine` 搜索还会读取该用户注册的命名空间。仅适用于个人笔记本电脑。
- **`trust-network`**：使用邮箱和密码自助注册；第一个申领某个命名空间名称的主体拥有该命名空间。agent 可以自行注册（`loom init --agent`），之后由用户使用 agent 打印出的代码来认领；该代码只能使用一次，认领后会被用户自己的密码取代。
- **`gateway`**：由上游身份层对用户进行身份验证，并使用共享密钥签发短期有效的 Loom 令牌；Loom 只负责验证。`LOOM_TRUSTED_CALLERS` 中的主体可以通过 `X-Loom-On-Behalf-Of` 代表最终用户操作，并且每条账本记录都会记录真实的调用方。如果某个服务的用户没有 Loom 账号，该服务可以改为针对 `user:<id>` 为每个请求签发一个短期令牌，并为其注册一个密钥作为最终用户签发方（[代表服务的最终用户操作](https://github.com/ClickHouse/loom/blob/main/docs/guides/tokens-and-grants.md#act-for-a-services-end-users)）。

<a id="collections"></a>

## 集合

集合是名称以 `col-…` 开头的命名空间，只有授权（grant）才能访问它：令牌声明和组织管理员角色都无法访问集合。集合可以嵌套在另一个集合内并继承其授权；对集合的授权除了可以指向个人或服务账号外，还可以指向 `user:`、`group:` 或 `agent:` 主体，或指向 `everyone`（在该组织内发起的每个请求）。用户通过标题来称呼集合，服务器根据标题生成其 `col-…` 名称；该名称此后始终作为集合的固定地址。[共享知识](https://github.com/ClickHouse/loom/blob/main/docs/architecture/sharing.md)介绍了这一模型以及名称的生成方式。用户在资料库（Library）中看到的是集合本身，按标题及其所在位置显示；存储集合的命名空间则是授权、搜索范围、审计和擦除所依据的对象，它不会出现在控制台的命名空间选择器中。

名称为 `acl-` 加 24 位十六进制数字的命名空间是镜像：它保存某个遵循其存储系统权限的文档源的文档，每份文档只显示给存储系统允许访问的读者（[文档源](https://github.com/ClickHouse/loom/blob/main/docs/architecture/document-sources.md#the-mirror)）。任何令牌都无法访问镜像（只有平台管理员的擦除操作可以指定镜像），因此 `namespace.register` 和 `namespace.restore` 会拒绝这种形式的名称。

当 `LOOM_IDENTITY_PROVIDER` 为空时，服务器会推断模式：关闭认证时为 `single-user`；开启认证且 `LOOM_SELF_SIGNUP=open` 时为 `trust-network`；开启认证的其他情况为 `gateway`。在非回环地址上绑定且开放注册时，还需要显式设置 `trust-network`。[部署](https://github.com/ClickHouse/loom/blob/main/docs/operations/deployment.md#identity-modes)列出了各模式的设置。

<a id="related"></a>

## 相关内容

- [令牌与授权](https://github.com/ClickHouse/loom/blob/main/docs/guides/tokens-and-grants.md)：分步介绍令牌的签发、声明、授权、受限命名空间、历史策略和撤销。
- [命名空间与共享](https://github.com/ClickHouse/loom/blob/main/docs/features/namespaces-and-sharing.md)：命名空间操作和控制台的共享视图。
- [安全](https://github.com/ClickHouse/loom/blob/main/docs/architecture/security.md)：认证和授权是如何执行的。
- [记忆](https://github.com/ClickHouse/loom/blob/main/docs/concepts/memory.md)：元组所指向的行。
