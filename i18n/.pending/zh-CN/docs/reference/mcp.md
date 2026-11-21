<!-- i18n: source=docs/reference/mcp.md blob=6a2cb653387481ccd3b42d2036cf7323fc58a763 model=claude-sonnet-5-5 -->
> 本页由机器从[英文原文](https://github.com/ClickHouse/loom/blob/main/docs/reference/mcp.md)翻译，内容以英文原文为准。
> Machine-translated from the [English page](https://github.com/ClickHouse/loom/blob/main/docs/reference/mcp.md), which is authoritative.

<a id="mcp-reference"></a>

# MCP 参考

Loom 将其操作作为 MCP 工具提供，地址为 `<loom>/mcp`。任何支持 streamable HTTP 的 MCP 客户端，只需一个 URL 和一个 bearer 令牌即可连接。工具列表由与 [HTTP API](https://github.com/ClickHouse/loom/blob/main/docs/reference/http-api.md) 相同的操作注册表生成，工具调用与 `POST /v1/<op>` 走同一条授权、限流、审计和幂等路径。传输层位于 `server/transport/mcp.py`，工具调用位于 `mcp_call.py` 和 `mcp_outcome.py`，bearer 校验位于 `mcp_auth.py`，工具列表位于 `mcp_tools.py`，schema 整形位于 `mcp_schemas.py`，协议和会话常量位于 `mcp_config.py`，精选的描述位于 `mcp_descriptions.py`。

使用 `loom install` 或手动方式接入具体客户端（Claude Code、Codex、Cursor、VS Code、Claude Desktop 等），请参见 [MCP 客户端](https://github.com/ClickHouse/loom/blob/main/docs/features/integrations/mcp-clients.md)。

<a id="endpoint"></a>

## 端点

| 请求                             | 作用                                                                                                                                                                |
| -------------------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `POST /mcp`                      | 一个 JSON-RPC 请求，或 1 到 64 个请求组成的批量请求。响应是 JSON 正文，而不是事件流。通知（notification）返回 `202`，没有正文。                                       |
| `DELETE /mcp` + `Mcp-Session-Id` | 结束会话并关闭其隐式 run。按 `run.complete` 限流，按调用方在组织预算内计算：超过上限时返回 `429` 并带有 `Retry-After`。 |

URL 上的查询参数决定该连接的 `tools/list` 显示什么：

- `?toolset=` 选择要列出的工具清单（[工具集](#toolsets)）。默认值：`LOOM_MCP_TOOLSET`（`core`）。
- `?schema_profile=` 选择列表的形态：`standard`（精选的描述和输入 schema；默认值，`LOOM_MCP_SCHEMA_PROFILE`）、`full`（增加 `outputSchema`）或 `compact`（第一段文字和参数树）。

两者的值未知时，都会回退到部署的默认值，因此拼写错误不会悄悄地扩大或缩小列表。`?toolset=` 的值同样决定会话入门说明（primer）的内容（`initialize` 的 instructions、`server/discover`、`loom` 提示、`loom://primer` 和 `loom_activate`）：当工具集列出某个 op 自己的工具时，primer 使用该工具的名称；否则使用标注为“(via loom_op)”的 op 名称；`loom_op` 不会路由到该处的 op 则不会出现。

支持的方法：`initialize`、`notifications/initialized`、`notifications/cancelled`、`ping`、`tools/list`、`tools/call`、`prompts/list`、`prompts/get`（`loom` 提示：会话 primer）、`resources/list`、`resources/read`（`loom://primer`）和 `server/discover`。列表结果带有 `ttlMs` 和 `cacheScope` 提示，客户端可据此缓存。

**协议版本。** `2025-11-25`、`2025-06-18`、`2025-03-26` 和 `2024-11-05`（`MCP_SUPPORTED_PROTOCOL_VERSIONS`）。当客户端的版本属于其中之一时，`initialize` 以该版本应答，否则以 `2025-11-25` 应答。`MCP-Protocol-Version` 请求头指定其他版本时，返回 HTTP 400 和 JSON-RPC 错误 `-32022`，其 `data.supported` 列出所支持的版本。`server/discover` 返回同样的版本列表、能力、服务器标识和 instructions。

<a id="authentication"></a>

## 认证

`LOOM_MCP_AUTH_MODE` 决定哪些方法需要 bearer：

| 模式                 | 需要 bearer 的方法                                            |
| -------------------- | ------------------------------------------------------------- |
| `required`（默认）   | 除通知之外的所有方法                                          |
| `discovery_only`     | 仅 `tools/call`；其他所有方法均开放                           |
| `open`               | 无；无效的 bearer 会被忽略（仅限受信任的网络）                |

当 `LOOM_BIND_HOST` 不是回环地址时，服务器在 `open` 模式下拒绝启动，除非设置 `LOOM_ALLOW_INSECURE=1`。关闭认证的部署（`LOOM_AUTH_REQUIRED=false`）行为等同于 `open`。受保护的方法缺少 bearer 或 bearer 无效时，返回 HTTP `401` 和 `WWW-Authenticate: Bearer realm="loom"`（如果请求带了令牌，还会加上 `error="invalid_token"`）。如果令牌暂时无法校验，因为吊销列表尚未加载，则返回 HTTP `503` 和 `Retry-After`，这样客户端会保留该令牌并重试。**为什么是 401 而不是 JSON-RPC 错误：** 客户端的“需要认证”流程及其 OAuth 发现都依据 HTTP 状态码触发；JSON-RPC 错误体只会被当作解析错误或工具错误。该令牌是普通的 Loom JWT，参见[令牌与授权](https://github.com/ClickHouse/loom/blob/main/docs/guides/tokens-and-grants.md)。

bearer 只从 `Authorization` 头读取。在 `/mcp` URL 上通过 `?token=` 传递的令牌会被忽略，请求按未认证处理：请求行会被写入代理和访问日志，URL 中的令牌会以明文形式落盘。

<a id="oauth"></a>

### OAuth 授权

仅在设置 `LOOM_OAUTH_ENABLED=true` 时提供。对于通过浏览器登录而不是粘贴令牌来连接的客户端，Loom 是一个 OAuth 2.1 授权服务器（`server/auth/oauth.py`）：

- 发现端点位于 `/.well-known/oauth-authorization-server` 和 `/.well-known/oauth-protected-resource`。
- 动态客户端注册位于 `/oauth/register`：公共客户端，必须使用 PKCE `S256`。
- `/oauth/authorize` 和 `/oauth/token` 会签发访问令牌和轮换的刷新令牌。仅当请求给出该令牌所签发给的 `client_id` 时，刷新才会成功。
- 访问令牌是为已登录用户签发的 Loom JWT，因此用户之后加入的命名空间在同一连接上即可访问。
- 登录使用 Loom 的密码登录，除非设置 `LOOM_OAUTH_LOGIN_PROVIDER=oidc`，将登录委托给外部身份提供方（`LOOM_OIDC_ISSUER`、`LOOM_OIDC_CLIENT_ID`、`LOOM_OIDC_CLIENT_SECRET`）。委托登录同时会关闭密码自助注册。
- 远程部署必须提供 HTTPS，并将 `LOOM_OAUTH_ISSUER` 设置为其公开的 `https://` 基础 URL。
- 注册的重定向 URI 必须是绝对地址且不含片段：`https`、指向回环主机的 `http`，或应用 scheme（`cursor://…`）。登录页面会显示授权所指向的主机。
- 每次刷新都会重新检查账号是否仍处于活跃状态，并重新推导其角色，因此被释放的账号，或已从 `LOOM_PLATFORM_ADMINS` 中移除的主体，无法通过刷新保留其访问权限。

[安全](https://github.com/ClickHouse/loom/blob/main/docs/architecture/security.md)介绍了其背后的令牌模型。

<a id="sessions-and-identity"></a>

## 会话与身份

- **会话 id。** `initialize` 会分配一个 `Mcp-Session-Id`，每个响应都会回传它。客户端的 `clientInfo.name` 会与该 id 关联并缓存，并在之后的调用中成为 `agent`，经过 slug 化处理（`Cursor` → `cursor`；缺失时为 `claude-desktop`）。该缓存位于进程内，且有大小上限。
- **身份默认值。** 工具调用通常只携带该操作自身的参数。服务器根据令牌填充 `org`，并根据调用方的默认工作区填充 `namespace` 和 `agent`；显式传入的参数优先。`code.*` 调用则改为解析到代码平面；在 `assistant` 工具集上，如果读取操作没有指定 `namespace` 或 `agent`，则遵循该工具集的规则（参见[assistant 工具集](#the-assistant-toolset)）。如果 `org` 参数指向令牌无权操作的组织，则会被拒绝（`ORG_MISMATCH`）：令牌只能在自己的组织中操作，组织级（`*`）令牌或平台管理员可以在任意组织中操作，规则与通过 HTTP 访问时相同（`authz.may_act_in_org`）。受限命名空间仍会拒绝所有其他组织，包括平台管理员。`loom_search` 不接受 `namespace` 参数：它会搜索调用方的主命名空间及其他记忆类命名空间，数量不超过扇出上限。
- **代码范围。** 客户端可以发送 `X-Loom-Code-Scope: <org>/<namespace>`（Claude Code 插件会发送其检出目录所解析到的仓库）。该请求头适用于所有未指定命名空间的 `code.*` 调用，授权随后会针对该命名空间进行，就像调用中显式指定了它一样。带有该请求头时，`loom_search` 在调用方有权读取的情况下，还会从仓库的主干代码命名空间中读取该仓库的文档和 `decision` 记忆，并对缺陷类查询执行跟踪器重复检查。
- **隐式 run。** 每个（org、令牌主体、会话）对应一个 run，由该会话中第一次属于 run 范围的工具调用在 run 账本中开启，并归档到调用方的默认命名空间；写入操作会携带其 `run_id`（`run.*` 除外，它会指定自己的 run；`code.*` 除外，它以部署的代码身份执行；`working_memory.*` 和 `conversation.update` 也除外，其中 `run_id` 用于限定或绑定行），读取操作会带上该标记，但不会据此过滤。run 会在 `DELETE /mcp` 时关闭，或者当 `mcp_session_run_sweep` worker 发现该令牌在 `LOOM_MCP_SESSION_RUN_IDLE_SECONDS`（默认 1800）秒内没有在该 run 下发起任何调用时关闭；如果部署中任何位置都没有运行后台 worker，空闲的 run 会一直保持打开。run 不存储调用次数：它的调用就是带有其 `run_id` 的 `agent_events` 行。参见[运行](https://github.com/ClickHouse/loom/blob/main/docs/features/runs.md)。

<a id="tool-list"></a>

## 工具列表

每个不在 `_MCP_HIDDEN_OPS` 中的注册表操作都是一个工具：

- **名称。** `loom_` 加上操作名，其中的点号换成下划线（`memory.set` → `loom_memory_set`）。
- **描述。** 有对应条目时使用 `TOOL_DESCRIPTIONS`（`server/transport/mcp_descriptions.py`）中经过整理的文本，否则使用一行兜底文本，内容包括该操作的延迟预算、幂等性和最低角色。整理过的文本会说明何时使用某个工具、何时不该使用；具体如何选择由模型决定。文本按操作名引用其他操作，每次列出工具时会针对读取该列表的客户端重新渲染（见 `server/transport/mcp_op.py` 中的 `name_ops_for_listing`）：列表中包含的操作用对应的工具名引用（`loom_memory_update`）；列表中不包含的操作保留其操作名，也就是 `loom_op` 所接受的名称，并且当 `loom_op` 在该连接上能路由该操作时，在其首次出现后附上“(via loom_op)”；它不会路由隐藏操作、`readonly` 上的写操作，也不会路由 `assistant` 上的任何操作。输入字段的描述、`full` 配置保留的输出 schema，以及隐藏操作的 `instead` 文本，都以同样方式渲染；`loom_op` 自身的文本是关于操作名的，因此按每次列表单独构建。导师文本在 MCP 上同样会针对调用方的列表渲染（工具结果的 `hint`、`context.pack` 的 `mentor` 部分，以及 `mentor.brief` 的提示和 plane `when`）；通过 HTTP 访问时则按存储的原样返回。
- **输入 schema。** 操作的 Pydantic 输入模型，去掉服务器自行填充的字段（`org`、`agent`、`user_id`、`session_id`、`consumer`）、仅用于控制台的字段（`server/transport/mcp_schemas.py` 中的 `_HIDDEN_OP_PROPS`）以及所有标记为 `MCP_HIDDEN` 的字段；`namespace` 仍作为可选属性保留。隐藏字段仍可按名称传入。
- **注解。** `readOnlyHint`、`destructiveHint` 和 `idempotentHint` 由注册表推导得出（角色、幂等性、审计、名称中的破坏性动词）。只有当操作是幂等的 viewer 操作，既不被审计，也没有标记 `writes` 时，`readOnlyHint` 才为 true，这样自动批准只读工具的客户端就不会在无提示的情况下执行状态变更。它们只是提示；真正的边界是调用时的角色检查。

除 `assistant` 外，每个工具集都会列出三个合成工具：

| 工具            | 作用                                                                                                                                                                                                                 |
| --------------- | -------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `loom_search`   | 一次调用并行搜索记忆、画像、知识和对话，并返回全部四个部分。`explain: true` 会补充每条记忆命中的排名原因。                                                                                                           |
| `loom_activate` | 返回会话引导文本（画像、偏好、默认工作区），与 `loom` prompt 的文本相同。                                                                                                                                            |
| `loom_op`       | `{op, arguments}` 通过与已列出工具相同的路径运行名单中未列出的任何操作；`{op: "help", query}` 最多列出 15 个匹配的操作（`_CATALOG_LIMIT`），并给出每个操作的必填和可选参数。 |

`loom_search` 就是注册表中的 `search.query` 操作（通过 HTTP 对应 `POST /v1/search.query`，返回相同的各部分，但不含文本摘要）。在 MCP 上，它与所有注册表工具走同一条路径（调用方默认值、会话运行、授权、受限访问审计、流量类别、历史策略、速率限制、幂等性），并保留自己列出的 schema、宽松的选项（超出范围的 `top_k` 或未知的 `recipe` / `bodies` 会被截断到有效值，而不是被拒绝），以及精简结果旁的文本摘要。`Operation.mcp_tool` 指向它，因此不会再列出或允许调用第二个 `loom_search_query` 工具。参见[统一搜索](https://github.com/ClickHouse/loom/blob/main/docs/features/unified-search.md#searchquery-over-http-and-the-sdks)。

**隐藏操作**（`server/operations/toolsets.py` 中的 `_MCP_HIDDEN_OPS`）在 MCP 上既不会列出，也不能调用：包括 `loom_search` 所取代的各存储单独搜索（`memory.search`、`knowledge.search`、`conversation.search`）、审阅队列、快照与恢复、策略管理、审计与成本控制台、代码索引导入。调用这些操作会返回 `isError`，错误码为 `OP_NOT_ON_MCP`，并说明应改用什么。它们仍可通过 HTTP 和 SDK 访问。在 `assistant` 工具集上，`memory.search` 和 `knowledge.search` 会被列出并可调用，而调用任何其他隐藏操作都会返回 `UNKNOWN_OP`。隐藏操作的判断标准是：典型的最终用户（而非运维人员）会不会在聊天中用到它？覆盖整个命名空间的视图可以被隐藏，而限定在调用方范围内的变体仍保留在 MCP 上：`namespace.list` 被隐藏，`namespace.list_mine` 可调用（在 `all` 和 `readonly` 上列出，在除 `assistant` 外的其他所有工具集上通过 `loom_op` 调用）；`memory.extraction_status`（命名空间中排队的所有批次）被隐藏，`memory.extraction_status_mine`（调用方排队的批次，按 `queue_id` 查询，每次最多 20 个）可按名称调用，或在除 `assistant` 外的每个工具集上通过 `loom_op` 调用（包括 `readonly`）。与之对应的写操作 `memory.extraction_retry_mine` 会再重放一次调用方失败的批次；`loom_op` 在除 `readonly` 和 `assistant` 外的每个工具集上都会路由它。

<a id="toolsets"></a>

### 工具集

工具集决定**列出**哪些工具；无论使用哪个工具集，每个非隐藏的操作都仍可按工具名或通过 `loom_op` 调用。有两个例外：在 `assistant` 上只能调用它自己的读操作；在 `readonly` 上（通过 `?toolset=` 指定，或在请求未指定时由 `LOOM_MCP_TOOLSET` 指定），`loom_op` 只路由读操作。

| 工具集      | 列出的工具                                                                                                                                                                                                                                                                                                                                                                                                                           | 适用于                                         |
| ----------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------ | ---------------------------------------------- |
| `core`      | 记忆循环：`memory.set/update/delete/pin/get/history/apply/lock`、`blacklist.add`、`context.pack`。其他所有操作通过 `loom_op` 运行；`core` 上列出的工具都不会记录对话轮次。设置 `LOOM_MCP_CODE_TOOLS=true` 后会加入代码工具。                                                                                                                                                                                                           | 聊天客户端（默认）                             |
| `coding`    | `memory.set/update/get/delete`、`working_memory.set`、`context.pack`、代码工具（`code.search`、`grep`、`enclosing`、`ask`、`symbol`、`outline`、`callers`、`context_pack`）、`tracker.duplicate_check`、`code.bisect_candidates`、`code.pr_check`、`code.impact`。                                                                                                                                                                    | 编码 agent                                     |
| `tracker`   | `tracker.answer`、`binding`、`component`、`duplicate_check`、`flaky`、`item`、`links`、`release`、`similar`、`test_signal`，以及 `code.bisect_candidates`、`code.pr_check`、`code.impact`。                                                                                                                                                                                                                                           | 处理失败测试、崩溃或 PR 的会话                 |
| `tasks`     | `task.pull/create/complete/fail/wont_do/defer/heartbeat/updates/list/get`、`inbox.list`。                                                                                                                                                                                                                                                                                                                                            | 作为带任务收件箱的 agent 运行的会话            |
| `atomic`    | `memory.set/get/delete/ask`、`working_memory.set/append`、`run.begin/complete`。                                                                                                                                                                                                                                                                                                                                                     | 最小化、由策略塑造的 agent                     |
| `readonly`  | `all` 的列表去掉所有 `readOnlyHint` 为 false 的工具；`loom_op` 只路由读操作。                                                                                                                                                                                                                                                                                                                                                        | 只读注册                                       |
| `assistant` | 一组固定的读操作，用于代表用户调查 Loom：`namespace.get/summary/inventory/tune/taxonomy/audit/validate/guide_get/guide_history/list_categories`、`mentor.brief/guide`、`help.search/get`、`grants.list`、`memory.inspect/search/ask/get/list/history/provenance/aggregate`、`facts.traverse/path`、`knowledge.search/get/list/inspect`、`code.search/symbol`、`insights.catalog/query`。没有合成工具。 | 代表已登录用户操作的聊天客户端                 |
| `all`       | 每个 `maturity` 为 `stable` 的非隐藏操作，加上 core 或 atomic 名单所列出的 `experimental` 操作。两个名单都没有列出的 experimental 操作不会出现在列表中，但仍可按名称或通过 `loom_op` 调用。仅在设置 `LOOM_MCP_CODE_TOOLS=true` 时才包含代码工具。                                                                                                                                                                                      | 运维人员                                       |

精选名单可以组合：`?toolset=coding+tracker`、`coding,tracker`，或者未转义、到达时变成空格的 `+`。只有精选名单可以组合（`all`、`readonly` 和 `assistant` 是完整的工具面），其中任何一部分未知都会使整个值无效，此时列表会回退到部署默认值。这些名单是 `server/operations/toolsets.py` 中的 frozenset；`ops.capabilities` 会报告每个操作所属的 `toolsets`。在另一个工具集上做第二个 MCP 注册（`?toolset=tracker`、`?toolset=tasks`）会增加一份名单，而不会改变第一个注册。

**为什么列表很小。** 每个已列出工具的名称、描述和 schema，在每个会话的每一轮中都要消耗 token，而且列表越长，工具选择的准确率越低。`tests/unit/test_mcp_listing_budget.py` 让每份名单都保持在 token 上限之内。宁可让某个操作只能按名称调用，也不要把它列出来；[添加操作](../development/adding-an-operation.md)介绍了何时以及如何列出一个操作。

<a id="results"></a>

## 结果

`structuredContent`（以及 `content[0].text` 中的 JSON）是该 op 的输出，已裁剪为只保留答案（`server/transport/lean_results.py`）：命中项包含其 key、内容、命名空间、日期、相似度、来源和状态，另有计数、分页和 abstain 字段。分数组成、解释、检索计划和成本、哈希以及空字段都会被丢弃。**原因：**有些客户端除文本块外，还会把 `structuredContent` 交给模型，而完整的诊断结构为一个简短的答案要多花几千个 token。调用时加上 `explain: true` 会返回完整结构。HTTP、SDK 和 `assistant` 工具集始终返回完整结果（在 `assistant` 上，`memory.search` 不含每个命中项的分数明细）。

<a id="the-assistant-toolset"></a>

### assistant 工具集

`?toolset=assistant` 是聊天客户端代表已登录用户调查 Loom 时使用的连接：命名空间中有什么、如何被使用，以及基于记忆、文档和代码的带引用回答。该值只能是 `assistant`（不区分大小写）：拼写错误或 `assistant+core` 这样的组合无法解析，连接会得到部署的默认列表，且不报错。保证来自运营者配置的 URL 和 `viewer` 令牌，而不是客户端。早一个版本的副本同样不认识这个值，会提供其默认列表，所以滚动升级期间只有令牌能限制连接：等每个副本都运行了支持它的构建后，再让客户端使用 `?toolset=assistant`，并且无论如何都要给它一个 `viewer` 令牌。在该连接上：

- **只有它的读操作可以调用。**对任何其他工具（包括 `loom_op` 和 `loom_search`）的 `tools/call`，会在处理程序运行之前返回 `isError`，无论令牌允许什么：返回代码 `UNKNOWN_OP`；如果调用指定了令牌无权读取的组织或命名空间，则返回访问拒绝，这一项先检查。每次调用仍会检查令牌，因此用户无权进行的读取会像在任何连接上一样被拒绝。用 `viewer` 角色签发客户端的令牌，可使同样的限制在其他所有入口上也成立。
- **它的调用以 consumer `loom-assistant` 记入账本**，无论调用发送了什么，被拒绝的注册表工具调用也包括在内（未知的工具名不会写入行）。`namespace.audit`、`namespace.summary`、`mentor.brief` 和 `knowledge.reads` 会把该 consumer 以及控制台（`loom-ui`）的读取排除在使用计数之外，因此查看命名空间不会改变它的评级（在控制台中修改记忆属于用户使用命名空间，仍会计入 `namespace.audit` 和 `namespace.summary`）；`namespace.audit` 在 `summary.inspector_calls` 中报告排除了多少次。`consumer` 声明指向其他 consumer 的令牌保留其声明，其调用会被计入。
- **未指定命名空间的读取读的是令牌的命名空间。**默认值是令牌声明中指定且它有权读取的第一个命名空间，而不是它有权写入的第一个：assistant 回答所用的令牌是为一个命名空间签发的，而其主体的个人命名空间才是它可以写入的。声明中没有指定命名空间的令牌，保留调用者的默认工作区。
- **读取要指明读取谁的行。**assistant 不写入任何内容，所以其客户端自己的 agent 没有任何行，未指定 `agent` 的读取也不会被限定到它：`memory.ask` 跨 agent 读取，`mentor.brief` 针对命名空间中写入最多的 agent 做简报，就像 `memory.search`、`memory.list` 和 `memory.history` 已经跨 agent 读取一样。`insights.query` 用 SQL 回答针对 `insights.catalog` 所列表的开放式分析问题，并由 ClickHouse 限定在令牌的组织和命名空间内（[assistant 设计](../architecture/assistant.md#the-analysis-op)）。读取某一个写入者的行，即 `memory.aggregate` 或 `memory.provenance`，会因缺少 `agent` 而被拒绝，直到调用指明写入者：即命中项的 `agent`、`user_id` 和 `session_id`。列表在每个记忆读取上都提供这些参数，在 `mentor.brief` 上提供 `agent`。`memory.get` 会回退到任意 agent 下的那一行。
- **读取不记录使用情况。**`memory.search` 以 `auto_touch: false` 运行，`memory.ask` 以 `reinforce: false` 运行，无论调用发送了什么，因此调查不会提高用于结果排序的召回计数；`memory.ask` 还以 `precedents: false` 运行，所以不会存储可供复用的回答。剩下的是遥测和缓存：请求账本行；每次读取记忆都会留下的暴露行（`loom.memory_exposures`，记录谁被提供了哪一行）；`code.search` 或 `code.symbol` 写入的代码搜索追踪（`loom.code_search_trace`，受 `LOOM_CODE_TRACE_LOG_ENABLED` 控制）；在 `LOOM_SEARCH_TRACE_LOG_ENABLED` 或 `LOOM_MEMORY_QUERY_NODES_ENABLED` 开启时 `memory.search` 写入的搜索追踪和查询节点行；延迟一致性下 `memory.ask` 的一致性行；以及模型响应缓存。assistant 调用不会打开 MCP 会话运行，其 `initialize` 和 `loom` 提示也不写入 profile 读取行：它的引导内容不内联 profile。
- **结果是完整输出**，与 HTTP 返回的一致，因为引用需要裁剪后的结构所丢弃的标识、版本和证明；`memory.search` 以 `explain: false` 运行，所以它的命中项不带每个命中项的分数明细。`memory.search` 和 `knowledge.search` 在其他列表中留给 `loom_search`，在这里则单独列出，以便回答可以一次只来自一个平面。`knowledge.search` 的命中项给出其 `source_uri`、标题面包屑（`section`）以及其段落写入时的 `revision`（未记录时为 0），已摄取文件的命中项则给出其 `locator`。命中项是最接近的段落，而不是答案：该 op 没有 abstain 信号，所以当没有命中项陈述某个事实时，模型应说明文档中没有该事实。
- **`initialize` 指令**要求模型按 key 和版本或内容哈希来引用每个论断，或按文档的 `source_uri`、章节以及命中项携带的 revision（为 0 时则没有）来引用；要把来源陈述、计算结果和它自己的解读区分开；要把缺失的证据报告为未观察到；并要把存储的文本视为数据，它不能授予工具权限，也不能批准变更。

<a id="errors"></a>

## 错误

无法按原样执行的调用会以 **tool result** 的形式返回，带有 `isError: true`、一个供模型读取的文本块和一个 `structuredContent` 主体。客户端会把这些内容展示给模型，使其能够修正调用；许多客户端在收到 JSON-RPC 错误帧时会丢弃状态，因此 Loom 只把 JSON-RPC 错误帧用于传输故障。

- **参数错误。** 校验失败时，返回 `"<op>: invalid
arguments."`，后面依次列出每个字段的错误、每个未知键及其最可能对应的字段（包括嵌套字段）、必填和可选参数，以及一个修正后的示例调用：

  ```text
  memory.get: invalid arguments.
  memory_key: Field required [missing]
  unknown 'key' — did you mean 'memory_key'?
  required: memory_key
  optional: if_version, include_recall_breakdown, as_of, fields, follow
  example: {"memory_key": "deploy_window"}
  ```

  `structuredContent` 包含相同的部分：`errors`、`op`、`required`、`optional`、`unknown`（键 → 建议）和 `example`。会改变状态的 op（任何非只读的 op）如果收到近似拼写的键，会在校验之前被拒绝（`UNKNOWN_ARGUMENT`，不会写入任何内容），并返回 `did_you_mean` 和该 op 的参数。

- **拒绝。** 处理程序返回的 401 以外的 4xx（`LOCKED`、`PINNED_CONFLICT`、`BLACKLISTED`、`RESOURCE_NOT_FOUND`、`RATE_LIMITED` 等）是一个 tool result，其主体为 HTTP 的 `detail` 加上 `status`；如果存在 `Retry-After`，还会包含 `retry_after_s`。
- **访问。** 角色校验失败时返回 `AUTH_INSUFFICIENT` 和 `required_role`；令牌既未指明、也未被授予权限的命名空间，返回 `NAMESPACE_NOT_ACCESSIBLE`。对于代码或 tracker 命名空间，或共享 collection（不是他人的个人 collection），拒绝信息会说明如何获取访问权限。
- **传输错误**仍然是 JSON-RPC 错误：解析错误 `-32700`，无效请求 `-32600`（认证拒绝带有其 HTTP 状态码；格式错误的请求或不在 1 到 64 范围内的批量请求返回 200），方法不存在 `-32601`，不支持的协议版本 `-32022`，以及服务器故障 `-32000 "tool execution
failed"`。Loom 命名的故障（`OVERLOADED`、`BACKEND_TIMEOUT`、`SCHEMA_PENDING`、`EMBED_TIMEOUT`）会在该错误中给出其代码和消息，`error.data` 为 HTTP 的 `detail` 加上 `status`、`tool` 和 `request_id`（当错误指明了等待时间时还包含 `retry_after_s`）；其他任何故障只会显示 `internal error` 和请求 id。

每个带有 `code` 的拒绝主体以及每个故障的 `error.data` 还会携带 `category` 和 `retryable`，与 HTTP 错误信封一致。工具的拒绝文本末尾会用文字给出同样的分类（`(auth: do not retry unchanged)`），供只向模型展示文本的宿主使用；`loom_op` 自身的拒绝会在其 JSON 文本中携带这些字段。对于不是 collection 的命名空间，如果来自非代码或非 tracker 工具、且所在工具集能够访问 `loom_op`，其 `NAMESPACE_NOT_ACCESSIBLE` 拒绝还会携带一个 `next` 步骤（[错误参考](https://github.com/ClickHouse/loom/blob/main/docs/reference/errors.md#the-error-envelope-over-http)）。每个错误码及其原因和修复方法见[错误参考](https://github.com/ClickHouse/loom/blob/main/docs/reference/errors.md)。

<a id="security"></a>

## 安全

`tools/call` 会运行 HTTP 传输层的各道关卡（`server/transport/gates.py`）：

- **授权**：每次调用都会检查，依据是操作的 `required_role` 以及命名空间的访问规则。
- **速率限制**：与 HTTP 使用相同的限流桶。由于 MCP 客户端不会重试，当被限流的调用给出的等待提示不超过 `LOOM_MCP_RATELIMIT_RETRY_MAX_WAIT_S`（默认 2 s）时，它会休眠相应时长，然后再检查一次同样的限流桶；如果等待提示更长，或者第二次仍被拒绝，则返回 `RATE_LIMITED` 以及 `retry_after_s`（设置 `LOOM_MCP_RATELIMIT_RETRY_MAX_WAIT_S=0` 则永不等待）。
- **审计**：声明了 `audit=True` 的操作会写入相同的 `loom.audit_log` 行。
- **幂等性**：MCP 没有幂等性请求头，因此幂等操作在其参数中接受 `_idempotency_key`（或 `idempotency_key`）。该参数会在校验之前被移除，其重放行为与 HTTP 的 `Idempotency-Key` 完全一致：只有在授权通过之后才会重放，并且只会重放给存储该键的主体。

<a id="exporting-the-action-schema"></a>

## 导出 action schema

策略网关可以根据由注册表生成的 schema（`server/transport/action_schema.py`）来管控 Loom 的工具：

```bash
loom export-actions --format json  -o loom-tools.json    # tools/list manifest + a `loom` block per tool
loom export-actions --format cedar -o loom.cedarschema   # Cedar: one action per operation
```

JSON manifest 采用 MCP `tools/list` 的结构（每个 op 都有 `name`、`description`、`inputSchema`、`outputSchema`），另外还有一个 `loom` 块，其中包含策略引擎所需、而 MCP 没有对应字段的信息：op 名称、HTTP 路径、所需角色、效果类别（`read`、`write` 或 `destructive`，由与注解相同的分类器得出）、幂等性、审计标志、MCP 可见性，以及输入和输出模型的名称。Cedar schema 在命名空间 `Loom` 中声明了一个 `Agent` 主体和一个 `Namespace` 资源，为每个输入和输出模型各声明一个记录类型，声明操作组 `Read`、`Write` 和 `Destructive`（`Destructive` 属于 `Write`），并为每个操作在其所属组中声明一个 action。`-o -` 会写入标准输出。

<a id="related"></a>

## 相关内容

- [MCP 客户端](https://github.com/ClickHouse/loom/blob/main/docs/features/integrations/mcp-clients.md)：连接 Claude Code、Codex、Cursor 及其他客户端。
- [Claude Code](../features/integrations/claude-code.md) 和 [Codex](../features/integrations/codex.md)：自行挂载服务器的插件。
- [操作参考](https://github.com/ClickHouse/loom/blob/main/docs/reference/operations.md)：每个 op 的角色、类型，以及 `all` 是否列出它。
- [错误参考](https://github.com/ClickHouse/loom/blob/main/docs/reference/errors.md)。
