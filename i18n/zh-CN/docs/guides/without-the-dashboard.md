<!-- i18n: source=docs/guides/without-the-dashboard.md blob=d950ac1fe252e9d585139b1da802185c47ab7aa9 model=claude-sonnet-5-5 -->
> 本页由机器从[英文原文](https://github.com/ClickHouse/loom/blob/main/docs/guides/without-the-dashboard.md)翻译，内容以英文原文为准。
> Machine-translated from the [English page](https://github.com/ClickHouse/loom/blob/main/docs/guides/without-the-dashboard.md), which is authoritative.

<a id="use-loom-without-the-dashboard"></a>

# 不使用控制台也能用 Loom

`/me` 控制台能做的一切，都可以在终端、脚本或 agent 的 shell 中完成。使用 `loom login` 登录一次，然后使用 `loom call` 或 Python SDK。本页列出了每个控制台视图对应的命令和 SDK 调用；未列出的视图同样只是普通操作（`loom call --list`）。

<a id="sign-in"></a>

## 登录

```console
$ loom login --server https://loom.example.com   # approve on any browser: password or SSO
$ loom whoami
$ loom logout
```

`loom login` 会保存一个可自动刷新的会话。如果前面有一个由[单元](https://github.com/ClickHouse/loom/blob/main/docs/architecture/cells.md)组成的路由器，`loom login --org <org>` 会在该组织自己的单元上登录：它会请求路由器的 `GET /cells/lookup`；如果没有路由器，或者还没有单元，就沿用传入的服务器；遇到 429 时最多等待 `--wait` 秒（默认 120）；其他情况下会拒绝登录，且不会启动登录流程。此后的会话属于该单元所在的主机：请将 `--server` 或 `LOOM_BASE_URL` 指向它打印出的主机。CI 和各类服务没有人来批准验证码，因此它们以 bot 身份登录（见下文），或者在 `LOOM_API_KEY` 中使用带作用域的令牌。参见[令牌与授权](https://github.com/ClickHouse/loom/blob/main/docs/guides/tokens-and-grants.md#sign-in-from-a-terminal-or-a-notebook)。
如果只安装了 SDK，可以用 `python -m loom login` 等形式运行相同的命令。

<a id="bots-and-ci-jobs"></a>

## Bot 与 CI 作业

bot 无需 `loom login`：只需注册一次（使用 `loom bot create`，或由平台管理员执行 `loom call /v1/admin/bots.create`），然后在运行时设置 `LOOM_CLIENT_ID`，并提供其私钥（`LOOM_CLIENT_KEY_FILE`、`LOOM_CLIENT_KEY`，或保存在 `LOOM_CLIENT_KEY_SECRET` 中的 AWS Secrets Manager 密钥）；在 CI 作业中，则改为设置 `LOOM_WORKLOAD_AUDIENCE`。SDK 会自行获取并续期其令牌：有效期为一小时，若该 bot 的最长有效期更短，则取较短者。参见[单点登录](https://github.com/ClickHouse/loom/blob/main/docs/operations/sso.md#bots-and-ci)。

<a id="loom-call-the-whole-api"></a>

## `loom call`：完整的 API

```console
$ loom call --list                          # every operation and the role it needs
$ loom call memory.search query="deploy window" top_k:=5
$ loom call grants.approve namespace=support subject=bob@example.com decision=approve
$ loom call /v1/me/service_accounts         # a route outside the operation registry
```

用于 CI 和 agent 的个人令牌有专门的命令：

```console
$ loom token create --name ci --namespace-role support=writer --namespace docs --ttl-days 0
$ loom token list
$ loom token revoke --name ci
```

- **操作名称**会以 POST 方式发送到 `/v1/<op>`。`org` 和 `namespace` 取自你的令牌；`--org` 和 `--namespace` 可以覆盖它们。
- **路径**（以 `/` 开头）按原样发送：没有请求体时使用 GET，有请求体时使用 POST。
- **字段**：文本使用 `key=value`，按输入内容原样发送；其他类型的值使用 `key:=json`（`top_k:=5`、`pinned:=true`、`tags:='["a","b"]'`）。`--data` 接受一个完整的 JSON 对象，也可以用 `@file` 指定文件，或用 `-` 从标准输入读取。
- **输出：**返回结果以 JSON 格式打印。服务器返回的错误会在 stderr 上打印一行，并附带错误代码，然后以退出码 1 退出；命令本身有误（例如未知的操作、格式错误的字段）则以退出码 2 退出。

每个操作的字段列在[操作参考](https://github.com/ClickHouse/loom/blob/main/docs/reference/operations.md)中。

在 Python 中，`agent.call("<op>", {...})` 可以调用任何操作，每个操作也都是 `agent.client` 上的一个方法。操作注册表之外的路由位于 `agent.account` 和 `agent.deployment` 上。

<a id="dashboard-view--command-and-sdk-call"></a>

## 控制台视图 → 命令与 SDK 调用

| 控制台                  | `loom call`                                                                                                                    | SDK (`agent = loom.Agent(org, namespace, name)`)                                                                      |
| --------------------------------- | ------------------------------------------------------------------------------------------------------------------------------ | --------------------------------------------------------------------------------------------------------------------- |
| 记忆：搜索、浏览、编辑    | `memory.search`, `memory.list`, `memory.set`, `memory.update_by_id`, `memory.delete`                                           | `agent.memory.search(...)`, `.list(...)`, `.set(...)`                                                                 |
| 知识：上传、读取、下载 | `knowledge.add`, `knowledge.list`, `knowledge.search`, `/v1/knowledge.download?org=…&namespace=…&source_uri=…`                 | `agent.knowledge.add(...)`, `.list()`, `.download(source_uri)`                                                        |
| 命名空间：创建、设置      | `namespace.register`, `namespace.get`, `namespace.list_mine`, `policy.set`, `blacklist.add`                                    | `agent.namespaces.*`, `agent.policy.*`, `agent.blacklist.*`                                                           |
| 加入命名空间                  | `namespace.browse`, `namespace.join`, `namespace.leave`                                                                        | `agent.client.namespace.join(...)`                                                                                    |
| 审批加入请求             | `grants.pending_for_me`，然后 `grants.approve namespace=… subject=… decision=approve`（或 `reject`）                            | `agent.client.grants.pending_for_me()`, `agent.client.grants.approve(...)`                                            |
| 成员与授权                | `grants.list`, `grants.create`, `grants.revoke`                                                                                | `agent.grants.*`                                                                                                      |
| 审核队列（冲突）          | `conflicts.list`, `conflicts.resolve`, `learnings.review`                                                                      | `agent.conflicts.list()`, `.resolve(...)`                                                                             |
| 活动、审计日志               | `profile.activity`, `audit.list`, `agents.connected`, `mentor.brief`, `memory.exposure_summary`, `memory.owner_report`         | `agent.audit.list(...)`, `agent.profile.*`                                                                            |
| 成本与预算                  | `cost.summary`, `cost.budget_status`                                                                                           | `agent.cost.summary(...)`                                                                                             |
| 运行                              | `run.list`, `run.get`                                                                                                          | `agent.run.*`                                                                                                         |
| 代码索引                        | `code.index_status`, `code.repo.add`, …                                                                                        | `agent.code.*`                                                                                                        |
| 擦除、遗忘               | `erasure.request`, `erasure.status`, `erasure.list`, `erasure.export`, `memory.clear`                                          | `agent.erasure.*`                                                                                                     |
| 任务与收件箱                   | `task.create`, `task.pull`, `task.updates`, `inbox.*`                                                                          | `agent.tasks.*`                                                                                                       |
| 跟踪器来源                   | `tracker.source.add`, `tracker.source.list`, `tracker.item`, …                                                                 | `agent.tracker.*`                                                                                                     |
| 图谱（实体与事实）        | `facts.traverse`, `facts.*`                                                                                                    | `agent.facts.*`                                                                                                       |
| 对话与会话简报  | `conversation.*`, `session.brief_*`                                                                                            | `agent.conversations.*`, `agent.briefs.*`                                                                             |
| 设置：令牌                  | `/v1/me/service_accounts`, `/v1/me/service_accounts.create`, `/v1/me/service_accounts.revoke`, `loom token create`             | `agent.account.tokens()`, `.create_token(...)`, `.revoke_token(name)`                                                 |
| 默认命名空间                 | `/v1/me/set-default-namespace namespace=…`                                                                                     | `agent.account.set_default_namespace(...)`                                                                            |
| 共享概览                   | `/v1/me/sharing-summary`                                                                                                       | `agent.account.sharing_summary()`                                                                                     |
| 管理：健康状态                     | `/v1/health`, `ops.health`                                                                                                     | `agent.deployment.health()`                                                                                           |
| 管理：后台 worker         | `/v1/admin/workers.status`                                                                                                     | `agent.deployment.workers_status()`                                                                                   |
| 管理：模型与提供方       | `/v1/admin/models`, `/v1/admin/models/validate`, `/v1/admin/models` (POST), `/v1/admin/models/reset`, `/v1/admin/models/retry` | `agent.deployment.models()`, `.validate_models(...)`, `.save_models(...)`, `.reset_models(...)`, `.retry_models(...)` |
| 管理：用户、释放声明     | `admin.users`, `/v1/admin/claim_release`                                                                                       | `agent.client.admin.users()`, `agent.deployment.release_claim(...)`                                                   |

角色与控制台中的相同。批准加入请求需要该命名空间的管理员权限。管理视图需要组织管理员或平台管理员权限。
