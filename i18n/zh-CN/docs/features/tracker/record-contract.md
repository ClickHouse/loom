<!-- i18n: source=docs/features/tracker/record-contract.md blob=88cada3e321cc9c82e54440ec12b93a202fcc39a model=claude-sonnet-5-5 -->
> 本页由机器从[英文原文](https://github.com/ClickHouse/loom/blob/main/docs/features/tracker/record-contract.md)翻译，内容以英文原文为准。
> Machine-translated from the [English page](https://github.com/ClickHouse/loom/blob/main/docs/features/tracker/record-contract.md), which is authoritative.

<a id="tracker-record-contract"></a>

# Tracker 记录契约

`tracker.*` 操作，以及 `code.*` 和 `loom_search` 中的 tracker 部分，会使用只读凭据，从团队自己的 ClickHouse 中实时读取团队的 issue、pull request 和事件（incident）。Loom 不会复制这些记录。每条语句都基于同一个逻辑模型编写，即**契约**：下面列出的表和列。

团队可以通过两种方式接入自己的 schema：

| 模式 | 方式 |
| ------- | ------------------------------------------------------------------------------------------------------------------------------------------ |
| 视图 | 团队使用契约中的表名和列名创建视图。Loom 按原样读取这些视图。 |
| 映射 | Loom 列出该凭据可以读取的表，并提出映射建议。确认后的映射会作为数据保存；不会在远端创建任何对象。 |

映射只是位于契约之前的一层投影，因此对每个数据源都会运行相同的语句、测试和隐私谓词。契约对应的代码是 `server/tracker/domain/contract.py`（`REQUIRED`、`OPTIONAL`、`FEATURES`、`ALIASES`），映射对应的代码是 `server/tracker/domain/mapping.py`，每张表读取的列见 `server/tracker/domain/columns.py` 中的 `COLUMNS_READ`。

<a id="adding-a-tracker-source"></a>

## 添加 tracker 来源

1. **为 Loom 创建一个只读的 ClickHouse 用户**，设置 `readonly = 2`。`readonly = 1` 会拒绝 Loom 在每次读取时附带的单次查询设置，导致任何读取都无法执行（`CREDENTIAL_READONLY_MODE`）。

   ```sql
   CREATE USER loom_ro IDENTIFIED BY '…' SETTINGS readonly = 2;
   GRANT SELECT ON system.columns TO loom_ro;
   -- views mode: the views only
   GRANT SELECT ON loom_tracker.* TO loom_ro;
   -- mapping mode instead: each mapped table
   GRANT SELECT ON tracker_db.tickets TO loom_ro;
   ```

2. **仅限 views 模式：创建视图。** 创建一个数据库（例如 `loom_tracker`），其中包含 `github_issues`、`github_prs` 和 `edges`，以及你有数据的任何可选表。创建时使用 `DEFINER = CURRENT_USER SQL SECURITY DEFINER`，这样 `loom_ro` 只需要视图的 `SELECT` 权限，而不需要视图背后各表的权限。普通视图会以读取者自己的权限读取，每次读取都会因 `ACCESS_DENIED` 而失败；注册检查会发现这一点并指出修复方法。ClickHouse 之外的来源可以放在 `PostgreSQL`、`MySQL` 或 `S3` 引擎表之后。`tracker.contract` 会返回每张表和每一列及其内容，并为每张表提供一份空白的视图模板，其中包含用于生成条目 `id` 的 `loom_uuid5(name)` SQL 函数。

3. **注册来源。** 组织内任何已登录的成员都可以注册，方式是通过控制台的 _Add a tracker_ 流程（[tracker 概览](https://github.com/ClickHouse/loom/blob/main/docs/features/tracker/overview.md#adding-a-tracker-from-the-dashboard)），或使用下面的 op；注册者会成为这些 tracker 命名空间的第一个 `admin`。在写入任何内容之前，Loom 会检查：主机解析到公网地址（`HOST_REFUSED`）、凭据是只读的（`CREDENTIAL_NOT_READONLY`、`CREDENTIAL_READONLY_MODE`）、能够连接（`SOURCE_UNREACHABLE`），以及 schema 或映射符合契约（`CONTRACT_FAILED`）。只读的含义是：`SHOW GRANTS` 和 `SHOW GRANTS FINAL` 列出的每一种访问类型（包括所授予角色的类型）都是 `SELECT`、对数据库、表、列或字典的 `SHOW`、`dictGet` 或 `CREATE TEMPORARY TABLE`，并且没有任何权限带有 `WITH GRANT OPTION` 或 `WITH ADMIN OPTION`（嵌套角色也一样）；其他任何类型都会被拒绝。授予该用户的每个角色都必须是默认角色，这样检查才能看到完整的角色链。不支持 `SHOW GRANTS FINAL` 的服务器会被拒绝（`SOURCE_UNREACHABLE`）。使用 `dry_run: true` 时，schema 检查失败会改为返回报告，并为每张失败的表附上一份 `CREATE OR REPLACE VIEW` 草稿。

   | Op                           | 作用                                                                                                                                                                   |
   | ---------------------------- | ---------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
   | `tracker.contract`           | 在建立任何连接之前返回契约和视图模板。                                                                                                                                 |
   | `tracker.source.inspect`     | 连接并列出每张可读的表及其列，同时给出建议的映射。不写入任何内容。                                                                                                     |
   | `tracker.source.add`         | `name`、`host`、`port`（8443）、`secure`（true）、`database`、`user`、`password`、`namespaces`，可选 `mapping`；`dry_run: true` 会运行所有检查但不写入任何内容。         |
   | `tracker.source.set_mapping` | 替换已注册来源的映射（同样支持 `dry_run`）；`mapping: null` 会让它回到 views 模式。                                                                                    |
   | `tracker.source.check`       | 立即重新运行契约检查。                                                                                                                                                 |
   | `tracker.source.rotate`      | 替换凭据。                                                                                                                                                             |
   | `tracker.source.remove`      | 停用该来源及其命名空间（`confirm`）。                                                                                                                                  |
   | `tracker.source.list`        | 组织内的所有来源：契约检查结果、熔断器状态、命名空间，以及调用者在每个命名空间中的身份。绝不会返回凭据。                                                               |

   `namespaces` 的每一项形如 `{namespace, repos, restricted, join_policy}`：`repos` 是该命名空间可以读取的 `source_id` 的 `Org/repo` 前缀（未列出的仓库永远不会被读取）；`restricted` 使它成为私有记录；`join_policy` 对标准命名空间为 `open`（默认）或 `approval`。自助注册需要设置 `LOOM_TRACKER_ORG`，并且默认开启（`LOOM_TRACKER_SELF_SERVICE_ENABLED`，每个组织最多 `LOOM_TRACKER_SELF_SERVICE_MAX_SOURCES` 个，默认 10；本地开发用的 ClickHouse 可设置 `LOOM_TRACKER_SELF_SERVICE_ALLOW_PRIVATE_HOSTS=true`）。密码会加密存储，且永远不会返回。按名称执行 `inspect` 需要在该来源的某一个命名空间上拥有 `admin`；`set_mapping`、`rotate` 和 `remove` 则需要在每一个命名空间上都拥有，因为它们会改变该来源下每个命名空间所读取的内容。标准命名空间由 `admin` 级别的门控决定，受限命名空间则按受限门控的规则决定，只是令牌不必指明它；一旦注册者的授权被撤销，仅仅注册过该来源并不足够（[受限命名空间](https://github.com/ClickHouse/loom/blob/main/docs/architecture/security.md#restricted-namespaces)）。除平台管理员外，任何人的控制台会话都是 `writer` 令牌，因此无法更改或停用带有私有（受限）命名空间的来源：这需要具有 `admin` 角色的令牌，运维人员可以用 `loom mint-token --roles admin` 签发。页面会将这些按钮置灰并说明原因；最终由服务器决定。

   运维人员也可以改为配置同一个来源（[配置](#configuration)）；同名时，配置中的条目优先于注册的条目，并且只能在配置中修改（`SOURCE_FROM_CONFIG`）。

4. **Loom 会创建这些命名空间的目录行**：`kind = tracker`，私有；受限命名空间为 `classification = restricted`；标准命名空间的 `join_policy` 为 `open`（或 `approval`），受限命名空间为 `approval`。

5. **读取者加入。** 在开放的 tracker 命名空间上执行 `role: "viewer"` 的 `namespace.join` 会立即准入，服务账号也包括在内。受限命名空间则要等待其 admin 批准，并且读取者的令牌必须指明该命名空间（[身份与命名空间](https://github.com/ClickHouse/loom/blob/main/docs/concepts/identities-and-namespaces.md)）。

6. **将 tracker 关联到仓库**，这样在该检出目录中工作的 agent 就能读取它：使用带 `tracker_namespaces` 的 `code.repo.set_source`（需要仓库 `admin`）、控制台中的仓库卡片，或者（仅限运维人员自己的仓库）使用 `LOOM_TRACKER_CODE_NAMESPACE_BINDINGS_JSON`。一个仓库可以按顺序关联多个 tracker；第一个是主 tracker，其编号就是该仓库的 issue 和 PR 编号（[关联 tracker](https://github.com/ClickHouse/loom/blob/main/docs/features/tracker/overview.md#linking-a-tracker-to-a-repository)）。`tracker.binding` 会报告某个代码命名空间对调用者解析到什么。

<a id="configuration"></a>

## 配置

运维人员对同一条目的写法：

```bash
LOOM_TRACKER_ORG=acme
LOOM_TRACKER_SOURCES_JSON='{
  "sources": {
    "widgets": {"host": "abc.clickhouse.cloud", "port": 8443, "secure": true, "database": "loom_tracker"}
  },
  "namespaces": {
    "tracker-widgets":         {"source": "widgets", "repos": ["acme/widgets"]},
    "tracker-widgets-private": {"source": "widgets", "repos": ["acme/widgets-private"], "restricted": true}
  }
}'
LOOM_TRACKER_CREDENTIALS_JSON='{"widgets": {"user": "loom_ro", "password": "…"}}'
LOOM_TRACKER_CODE_NAMESPACE_BINDINGS_JSON='{"code-widgets*": ["tracker-widgets", "tracker-helpdesk"]}'
```

凭据放在单独的变量中，与映射分开，这样展示和比对映射时不会泄露密钥。绑定值可以是一个 tracker 命名空间，也可以是一个有序列表，主命名空间在前；键是对运维人员所管理仓库的代码命名空间名称进行匹配的 glob（自助仓库由其维护者负责关联）。维护者执行的 `code.repo.set_source` 关联会替换该仓库对应的映射。tracker 命名空间归属于 `LOOM_TRACKER_ORG`；该变量为空时，不提供任何 tracker 命名空间服务。

<a id="source-entry-options"></a>

### 数据源条目选项

数据源条目还可以包含：

- `"mapping"`：保存的映射（[映射模式](#mapping-mode)）；缺省表示视图模式。
- `"checks": {"from": "<table or table function>", "repo": "Org/repo"}`：tracker 的 CI 结果所在位置，由 `tracker.flaky` 和 `tracker.test_signal` 读取。解析顺序：先取此条目，其次取该数据源自己的 `checks` 表，最后取 `LOOM_TRACKER_CHECKS_FROM` / `LOOM_TRACKER_CHECKS_REPO`（两者默认均为空：没有 CI 检查）。请填写表函数，而不是基于表函数的视图：视图的过滤条件会在扫描之后到远端执行。未指定 `repo` 时，只有一个仓库的命名空间会使用该仓库。
- `"bisect": {"code_prefixes": ["src/"]}`：bisect 时哪些路径算作源代码；缺省表示任何不在 docs、tests、CI、examples 或 benchmarks 之下的路径。没有 tracker 的仓库仍然可以基于 Loom 自身的 PR 历史进行 bisect（此时答案中会显示 `candidates_from: "code_history"`）。
- `"columns": {"<table>": {"<contract column>": "<your column>"}}`：契约列在你的表中使用了其他名称时的对应关系，例如 `{"github_issues": {"triage_bisect_outcome": "bisect_result"}}`。实际的列会与它所对应的契约列按同样方式检查；只接受普通标识符。

规则：

- 指向未知数据源的命名空间不提供服务（启动时输出一行错误，不会自行猜测）。
- 每个数据源都有自己的连接、熔断器和时间预算，因此某个团队的服务变慢时，不会触发另一个团队的熔断器。
- 已注册的数据源（`loom.qa_sources`）每隔 `LOOM_TRACKER_SOURCES_REFRESH_SECONDS`（60）重新读取一次；同一进程内的写入会立即刷新。

所有 tracker 设置都列在[配置参考](https://github.com/ClickHouse/loom/blob/main/docs/reference/configuration.md#tracker)中。

<a id="public-and-private"></a>

## 公开与私有

tracker 命名空间要么是 **standard**，要么是 **restricted**，由 Loom 逐条语句强制执行：

- standard 命名空间的每条语句都带有 `is_private = 0`，而仅保存私有数据的表（`core_incidents`、`support_cases`、`slack_threads`）会在任何读取之前被拒绝。
- restricted 命名空间会去掉该谓词，只读取它自己的 `repos`，并受 restricted 命名空间规则的限制：需要指明该命名空间的令牌，以及直接授权。

有三种布局可用：

- 一组带有 `is_private` 列的视图，由两个命名空间共同读取（为只读用户在 `is_private` 上设置行策略，可在 ClickHouse 一侧增加一道检查）；
- 两组视图配两个用户，其中一个只持有公开行，分别注册为两个数据源；
- 仅使用一个 restricted 命名空间。

<a id="the-contract"></a>

## 契约

类型按**族**比较：`String`、`LowCardinality(String)`、`Nullable(String)`、`FixedString` 和 `Enum` 属于同一族；`DateTime`、`DateTime64` 和 `Date` 属于同一族；`UInt*`、`Int*` 类型和 `Bool` 属于同一族；`Array(x)` 按 `x` 比较。基于其他存储的视图可以在编码和可空性上有所不同，但结构不能不同。视图模板把每个族写成其最宽的类型（`UInt64`、`DateTime64(3)` 等）。

<a id="required-tables"></a>

### 必需的表

缺少这些表的数据源无法提供服务。

**`github_issues`**：每个 issue 一行。

| 列                                                                                           | 族            | 含义                                                                                                                    |
| -------------------------------------------------------------------------------------------- | ------------- | ----------------------------------------------------------------------------------------------------------------------- |
| `id`                                                                                         | UUID          | `uuid5(NAMESPACE_DNS, 'github_issue:' \|\| source_id)`（私有行使用 `github_issue_private:`）；`edges` 使用的键          |
| `source_id`                                                                                  | String        | `Org/repo#number`；该仓库必须在 tracker 命名空间的列表中                                                                |
| `number`                                                                                     | UInt          | issue 编号                                                                                                              |
| `url`, `title`, `body`, `author`                                                             | String        | 人可以打开的链接；每次搜索读取的标题；重复检查读取的正文；创建者                                                        |
| `labels`, `assignees`                                                                        | Array(String) |                                                                                                                         |
| `primary_component`, `severity_level`                                                        | String        | 团队的分类体系；没有时为 `''`                                                                                           |
| `state_reason`                                                                               | String        | `completed`、`not_planned`、`duplicate` 或 `''`                                                                         |
| `affected_versions`, `fix_versions`                                                          | Array(String) | issue 影响的版本；包含修复的版本                                                                                        |
| `is_flaky_test`, `is_crash_ci`, `is_performance`                                             | UInt          | 0/1 标志；未知时为 `0`                                                                                                  |
| `has_test`                                                                                   | UInt          | 存在关联的测试；未知时为 NULL                                                                                           |
| `error_code`                                                                                 | UInt          | 报告中提到的服务器错误码；没有时为 `0`                                                                                  |
| `is_private`                                                                                 | UInt          | 私有仓库的行为 `1`                                                                                                      |
| `created_at`, `updated_at`, `closed_at`                                                      | DateTime      | issue 未关闭时 `closed_at` 为 epoch 0                                                                                   |
| `triage_bisect_outcome`, `triage_introduced_commit`, `triage_component`, `triage_confidence` | String        | 分诊机器人的结论；tracker 没有时按 `''` 提供                                                                            |
| `triage_reproduces_on`, `triage_clean_on`                                                    | Array(String) | 同上，按 `[]` 提供                                                                                                      |

**`github_prs`**：每个 pull request 一行。

| 列                                                                                           | 族            | 含义                                                                       |
| -------------------------------------------------------------------------------------------- | ------------- | -------------------------------------------------------------------------- |
| `id`                                                                                         | UUID          | `uuid5(NAMESPACE_DNS, 'github_pr:' \|\| source_id)`（`github_pr_private:`） |
| `source_id`, `number`, `url`, `title`, `body`, `author`                                      | 同上          |                                                                            |
| `pr_number`                                                                                  | UInt          | 等于 `number`                                                              |
| `primary_component`, `severity_level`, `is_private`, `created_at`, `updated_at`, `closed_at` | 同上          |                                                                            |
| `state`                                                                                      | String        | `open`、`closed` 或 `merged`                                               |
| `is_merged`, `has_test`, `review_requests_test`                                              | UInt          |                                                                            |
| `test_types`, `reviewers`, `src_components`                                                  | Array(String) |                                                                            |
| `ci_result`, `changelog_category`, `minor_version`, `base_branch`                            | String        |                                                                            |
| `is_backport_pr`, `is_cherrypick_pr`, `is_sync_pr`                                           | UInt          | 衍生的 PR，不计入活动读取                                                  |
| `upstream_pr_number`                                                                         | UInt          | backport 所重复的 PR；没有时为 `0`                                         |
| `additions`, `deletions`, `time_to_merge_days`                                               | UInt          |                                                                            |

**`edges`**：条目之间的链接。

| 列                     | 族       | 含义                                                                                                      |
| ---------------------- | -------- | --------------------------------------------------------------------------------------------------------- |
| `from_id`, `to_id`     | UUID     | 如上所述的条目 id                                                                                         |
| `from_type`, `to_type` | String   | `github_issue`, `github_pr`, `github_issue_private`, `github_pr_private`, `core_incident`, `support_case` |
| `rel_type`             | String   | `fixes`, `related_to`, `duplicate_of`, `caused_by`, `backport_of`, `reverts`, …                           |
| `created_at`           | DateTime |                                                                                                           |

Loom 在本地推导条目的 `id`，并以它连接 `edges`，因此视图必须生成完全相同的值。ClickHouse 没有 `uuid5()`；`tracker.contract` 返回的 `loom_uuid5` 函数是用 SQL 实现的 RFC 4122 版本 5 构造，与 Python 的 `uuid.uuid5` 结果一致。

<a id="optional-tables"></a>

### 可选的表

缺少某个表时，需要它的 op 会返回 `not_provided`；表存在但缺少其最小列之一（`contract.py` 中的 `OPTIONAL`）时，会报告为 `incomplete` 并视为缺失。

| 表                                           | 最小列                                                                                                                                                        |
| -------------------------------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `core_incidents`                             | `id`, `source_id`, `title`, `body`, `primary_component`, `severity_level`, `stack_functions Array(String)`（栈帧匹配）, `error_code`, `created_at`            |
| `support_cases`                              | `id`, `source_id`, `title`                                                                                                                                    |
| `slack_threads`                              | `source_id`, `title`, `body`                                                                                                                                  |
| `component_risk_snapshot`                    | `computed_at`, `component`                                                                                                                                    |
| `release_items`                              | `release_version`, `issue_number`                                                                                                                             |
| `release_health_snapshot`                    | `computed_at`, `release_version`                                                                                                                              |
| `version_history`, `version_history_private` | `commit_sha`, `version`                                                                                                                                       |
| `checks`                                     | `check_name`, `test_name`                                                                                                                                     |
| `cloud_instances`                            | `instance_id`                                                                                                                                                 |
| `rel_instance_mentions`                      | `from_id`, `instance_id`                                                                                                                                      |
| `slack_incidents`                            | `incident_id`                                                                                                                                                 |

`core_incidents`、`support_cases` 和 `slack_threads` 只能通过受限的命名空间读取。

<a id="feature-columns"></a>

### 特性列

必需表上的可选列，报告为存在或缺失。数据源缺少的特性列会读取其别名，否则读取默认值（见[别名](#aliases)）：读取它的语句按没有该列的行来回答，该特性仍为缺失。

| 特性         | 列                                                  | 启用                                                                      |
| ------------ | --------------------------------------------------- | ------------------------------------------------------------------------- |
| `bisect`     | `github_prs.diff`, `github_prs.affected_file_paths` | bisect 候选和罪魁提交扫描                                                 |
| `merge_time` | `github_prs.merged_at`                              | 合并时间相关事实                                                          |
| `vector`     | `github_issues.embedding`, `github_prs.embedding`   | 仅作报告；`tracker.similar` 使用 Loom 自己计算的 embedding                |

<a id="aliases"></a>

### 别名

数据源缺少某个契约列时，如果 `contract.py` 中的 `ALIASES` 为它指定了一个可接受的表达式，且该表达式基于数据源已有的列，则仍然满足契约。检查会按族验证别名自身所用的列，并报告为 `aliased`；语句继续读取契约中的名称，因为表引用会被替换为添加了该别名的投影（查询分析器会将其裁剪为语句所读取的列）。没有自身列的别名是**默认值**（`defaulted`）：每一行读取 `''`、`0`、`[]` 或 `NULL`，报告中会注明，因此默认值不会被误认为数据。

| 表                                               | 列                                                                                           | 可接受的形式，按顺序                                                       |
| ------------------------------------------------ | -------------------------------------------------------------------------------------------- | -------------------------------------------------------------------------- |
| `github_issues`                                  | `severity_level`                                                                             | `severity`                                                                 |
| `github_issues`                                  | `has_test`                                                                                   | `toUInt8(metadata.has_test.:Bool)`（JSON `metadata` 列），否则 `NULL`      |
| `github_issues`                                  | `triage_bisect_outcome`, `triage_introduced_commit`, `triage_component`, `triage_confidence` | `''`                                                                       |
| `github_issues`                                  | `triage_reproduces_on`, `triage_clean_on`                                                    | `[]`                                                                       |
| `github_prs`                                     | `has_test`                                                                                   | `toUInt8(metadata.has_test.:Bool)`，否则 `toUInt8(tests_added > 0)`        |
| `github_prs`                                     | `upstream_pr_number`                                                                         | `ifNull(toUInt32(metadata.original_pr_number.:Int64), 0)`，否则 `0`        |
| `github_prs`                                     | `review_requests_test`                                                                       | `NULL`                                                                     |
| `github_prs`                                     | `test_types`, `src_components`                                                               | `[]`                                                                       |
| `github_prs`                                     | `merged_at`                                                                                  | `if(is_merged = 1, closed_at, toDateTime64(0, 3))`                         |
| `github_prs`, `core_incidents`                   | `severity_level`                                                                             | `''`                                                                       |
| `qa_bugs`（answer 路由的跨数据源视图）           | `severity_level`, `has_test`                                                                 | `severity`；`toUInt8(metadata.has_test.:Bool)`，否则 `NULL`                |

契约中未列出的重命名仍然算作缺失列。请在数据源条目的 `columns` 映射中声明它（[数据源条目选项](#source-entry-options)），或在 `contract.py` 中添加一个 `Alias`，让所有数据源都接受另一种写法。

<a id="mapping-mode"></a>

## 映射模式

映射会针对每个逻辑表，说明它读取团队的哪些表，以及针对每个逻辑列，给出一个基于该表各列的表达式：

```json
{
  "version": 1,
  "tables": {
    "github_issues": {
      "source": "tracker_db.tickets",
      "final": true,
      "columns": {
        "source_id": { "concat": [{ "lit": "acme/widgets#" }, "ticket_no"] },
        "number": "ticket_no",
        "title": "summary",
        "body": "description",
        "created_at": "opened_at",
        "state_reason": { "lower": "resolution" }
      }
    }
  }
}
```

- **表达式是数据，不是 SQL**：可以是实际表中存在的列名（或 `{"col": …}`）、`{"lit": …}`，或 `concat`、`coalesce`、`eq`、`in`、`has`、`cast`（`[expr, family]`）、`lower`、`digits`（取键末尾的数字：`PROJ-1234` → `1234`）之一。Loom 渲染这些表达式时，标识符加反引号，字面量加引号，并将每个表达式转换为语句所读取的类型族，然后使用只读凭据运行它们。
- `source` 是 `db.table`，或者是跟踪器所在数据库中的 `table`。`final: true` 表示使用 `FINAL` 读取该表（对 Replacing、Collapsing、Aggregating 和 Summing 引擎会建议启用）。
- Loom 根据映射后的 `source_id` 推导每个条目的 `id`，并根据映射后的 `from_ref` / `to_ref`（`Org/repo#number`）推导边的 `from_id` / `to_id`。这些字段不能被映射。
- 每个被映射的表都需要其核心列：`github_issues` 需要 `source_id`、`number`、`title`、`created_at`；`github_prs` 需要 `source_id`、`pr_number`、`title`、`created_at`；`edges` 需要 `from_ref`、`to_ref`、`from_type`、`to_type`、`rel_type`；`core_incidents` 需要 `source_id`、`title`、`created_at`。映射必须包含 `github_issues`。未映射的列会采用默认值或被推导出来（例如 PR 的 `state` 由其合并和关闭字段推导），报告会列出这些列；映射中未提及的逻辑表会被视为空表。
- 报告中的能力项（`items`、`duplicate_check`、`duplicate_calibration`、`pull_requests`、`links`、`severity_board`、`bisect`、`incidents`、`ci_checks`）说明了映射后的数据能让 Loom 回答哪些问题。

读取时，每个对逻辑表的引用都会被替换为映射后的投影，因此同样的语句、测试和隐私谓词对每个数据源都以相同方式运行。

<a id="checking-a-source"></a>

## 检查数据源

```bash
loom tracker-contract-check                           # every configured source
loom tracker-contract-check --source widgets          # one source
loom tracker-contract-check --namespace tracker-widgets
loom tracker-contract-check --json
```

服务器会在每个进程首次读取 tracker 时执行同样的检查：它使用该数据源自己的凭据读取表结构（`system.columns`，若其不可见，则对每个表执行 `DESCRIBE`），并将检查结果缓存 `LOOM_TRACKER_CONTRACT_TTL_S`（3,600 秒）；失败的结果只缓存 60 秒，这样修复后的视图可在一分钟内被重新识别。检查未通过的数据源，对每次读取都会返回 `tracker_unavailable` 并指明缺少的内容，绝不返回部分结果。`tracker.source.check` 可按需运行该检查。

<a id="related"></a>

## 相关内容

- [Tracker 概述](https://github.com/ClickHouse/loom/blob/main/docs/features/tracker/overview.md)：这些 op 能回答什么问题、关联方式、控制台流程
- [历史与 PR](https://github.com/ClickHouse/loom/blob/main/docs/features/code-index/history-and-prs.md)：读取 tracker 的代码平面 op
- [配置参考](https://github.com/ClickHouse/loom/blob/main/docs/reference/configuration.md#tracker)
- [操作参考](https://github.com/ClickHouse/loom/blob/main/docs/reference/operations.md#tracker)
