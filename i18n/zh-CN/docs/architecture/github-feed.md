<!-- i18n: source=docs/architecture/github-feed.md blob=bcf50268160cb35aa8da8040dcb9b21ab7f6601a model=claude-sonnet-5-5 -->
> 本页由机器从[英文原文](https://github.com/ClickHouse/loom/blob/main/docs/architecture/github-feed.md)翻译，内容以英文原文为准。
> Machine-translated from the [English page](https://github.com/ClickHouse/loom/blob/main/docs/architecture/github-feed.md), which is authoritative.

<a id="github-tracker-feed"></a>

# GitHub tracker feed（GitHub tracker 数据源）

GitHub feed 在团队的 ClickHouse 数据库中维护团队的 tracker 记录。Loom 的服务进程通过现有的[只读 tracker 来源](https://github.com/ClickHouse/loom/blob/main/docs/features/tracker/record-contract.md)读取该记录。该 feed 不会把 issue 正文、评论或 pull request 放入 Loom 的部署表中。

<a id="ownership-and-credentials"></a>

## 所有权与凭据

运维人员将 feed 作为有界的 CLI 作业运行，由团队的作业调度器调度。在读取已注册的端点或打开该记录之前，CLI 会检查其实际所在部署的存储权威（即该部署选定的权威数据存储／后端），以及其 cell 是否持有该 feed 所属的组织。外部连接会重复这一部署检查；绝不会把记录的元数据当作部署权威。作业退出时会关闭两个客户端以及该部署的 Postgres 连接池。初始化会在一个空的专用数据库中创建 feed 表和契约视图；如果已存在对象，则拒绝执行。随后，运维人员使用单独的只读凭据，把该数据库注册为 tracker 来源。配置和同步都需要该已注册的端点及其仓库允许列表。单独提供的写入凭据只能写入团队的 feed 数据库；读取凭据绝不会被升级或复用于写入。

来源凭据是只读的细粒度 GitHub 令牌或 App 安装令牌，限定在已配置的仓库内：Metadata、Issues 和 Pull requests，均为只读。令牌权限属于运维人员声明的配置：一次成功的 GET、令牌前缀或端点权限头，都不能证明不存在写权限。无法识别的主机令牌不适用于只读的实时探测。该令牌使用现有的 secret-root 机制密封保存在团队的 feed 配置表中。任何令牌都不会出现在参数、输出或契约视图中。CLI 通过指定名称的环境变量接收数据库写入密码，不会持久化该密码。对 GitHub 的请求使用 GET，指向固定的 `https://api.github.com` 源，拒绝重定向，并根据已验证的仓库名和数字形式的条目 id 构造路径。它们绝不会跟随响应中的 URL。

团队拥有所复制的记录，以及其备份保留和擦除的责任。要擦除已复制的内容，团队需停止其定时写入器，并在重新启动前删除受影响的 feed 行、评论和检查点，同时设置排除项或移除来源仓库。删除 Loom 命名空间会使 Loom 的派生数据和授权失效，但不会删除团队的记录。这与任何已注册的 tracker 数据库的边界相同。记录写入凭据无法访问 Loom 自身的数据库；复制的内容绝不会写入其中。只有现有的部署存储租约是本地的。

<a id="record-and-progress"></a>

## 记录与进度

原始条目和评论在团队的数据库中带版本保存，并附带身份元组、进程执行者和导入的来源类别。契约视图公开最新的条目版本、契约 UUID 和有效可见性。Issue 评论和 pull request 评论分开保存，并包含在对应条目的可搜索正文中。不受支持的分诊字段使用契约的类型化默认值；在出现证据之前，边为空。契约的 `updated_at` 是条目及其评论中最新的持久化发布时间戳，因此迟到的导入和评论编辑会推进 tracker 的增量读取。发布版本使用整毫秒，以匹配 tracker 游标。在记录的写入租约下，每个内容批次取记录时钟与其所属组织下所有已持久化内容版本再加一毫秒（跨所有仓库）两者中较晚的一个。这种排序同样适用于通过同一个游标读取多个仓库的命名空间。仓库控制写入同样会越过其所属组织和仓库已持久化的版本，即使接收端的时钟回退也是如此。因此，可见性撤销、凭据轮换和游标检查点会替换先前的控制行，而不会因时间戳而落败。上游原始时间戳保持独立，并驱动 API 游标。无需调用任何模型或嵌入。仓库身份保留已注册允许列表中的拼写，即使 API 返回不同的大小写。

所有者、仓库修订和发布水位线的读取会请求 `select_sequential_consistency = 1`；仅靠写入租约并不能让副本保持最新。Feed 写入器需要 SharedMergeTree 存储或单个自托管服务器，参见[读己之写保证](https://github.com/ClickHouse/loom/blob/main/docs/architecture/clickhouse.md#read-your-writes)。仅此设置并不能覆盖自托管复制表上已确认的写入。写入器验证仅接受已连接记录数据库中五个 feed 表使用 `ReplacingMergeTree` 和 `SharedReplacingMergeTree`。复制型、分布式以及无法识别的表引擎会在作业发布之前被拒绝。

每个仓库都有独立的条目、issue 评论和 review 评论游标。[issues 端点](https://docs.github.com/en/rest/issues/issues#list-repository-issues)会同时列出 issue 和 pull request；pull request 通过其数字端点补全详情。[issue 评论端点](https://docs.github.com/en/rest/issues/comments#list-issue-comments-for-a-repository)和 [review 评论端点](https://docs.github.com/en/rest/pulls/comments#list-review-comments-in-a-repository)各有独立的按更新时间扫描。每个扫描都按 `updated_at` 升序，并限制页数、响应字节数和请求时间。游标只在已确认的写入之后推进，并且要越过完整的时间戳边界。由于 `since` 是排他的，请求会把已保存的时间戳减少一秒。当页面预算用满时，会保存其最后一页上最早的时间戳；下一轮会重放该边界。预算为 2 到 100 页：前面的页面允许推进，而最后一页保留不完整的时间戳边界。如果单个时间戳就占满了整个已配置的页面预算，作业会报告容量错误并保留检查点，而不会跳过行或声称已有进展。运维人员可以为该仓库提高有界的页面预算。重放的行会替换相同身份的行。该 API 是不断变化的集合，而非快照；重叠扫描可减少分页竞争，但不声称上游读取具有事务性。

每一轮在复制内容之前，都会验证仓库身份和可见性。不可用、已迁移或不匹配的仓库，会先隐藏该仓库的契约行，然后拒绝该轮。契约视图还要求最近一次成功的可见性观测，如果调度器停止，该观测会在一个有界的时间间隔后过期。每次条目和评论发布都会把该观测的隐私分类与其内容存放在同一行中。视图对 `is_private` 和契约 UUID 使用仓库、条目和已存储评论中最严格的分类。因此，先于仓库元数据获知新内容的副本，不能把私有发布标记为公开。标准 tracker 现有的谓词会排除这些行。受限命名空间保留其实时的直接授权检查。可见性是轮询得到的观测，而不是即时的权限镜像。即使另一轮改变了其修订，经过验证的身份不匹配或私有观测也可以隐藏当前记录；这样可以保留当前的凭据和游标。来自旧凭据的一般性失败仍保留修订检查。过时的公开观测不能扩大访问范围。

当仓库变为公开时，配置或同步会在发布公开元数据之前重置其现有的流游标。有界扫描会重放条目和评论，并为每一项标记公开观测。在条目自身的行及其已存储评论都被重放之前，该条目保持受限。重复的定时轮次会在相同的页数和时间戳边界限制下继续处理积压。已在上游删除的行无法重放；团队必须擦除这些保留的副本，才能清除其私有分类。

增量 API 列表不会报告已删除的 issue 或评论。团队需要明确处理这些擦除；feed 不声称能检测删除。配置、密封凭据和游标保存在团队的记录中，因此 CLI 重启不依赖本地文件。初始化会把记录绑定到某一个 Loom 部署的数据库 UUID；配置和同步会拒绝任何其他部署。该部署中的并发作业使用一个以记录数据库 UUID 为键的发布租约，与来源别名和仓库重命名无关。向提供方的获取发生在该租约之外；初始化、配置和发布共享该租约，因此并发的首次身份观测无法绕过串行化。不确定的数据库写入会在有界的服务器发送窗口内保留其声明。擦除记录要求团队先停止所有写入器。

<a id="validation-and-rollout"></a>

## 验证与上线

模拟测试覆盖分页平局、部分响应、速率限制、重试、可见性变化、重定向和凭据脱敏。私有的 ClickHouse 用于验证契约、当前视图、重放以及被中断的游标发布。如有只读令牌，则用它检查一个有界的公开仓库切片；绝不会改用具有写入能力的 CLI 令牌。该 feed 不增加 Loom 的线上操作或自动 worker。在运维人员初始化并调度 feed 之前，现有部署和已注册的 tracker 保持不变。

<a id="source-entry-points"></a>

## 源码入口

- [CLI](https://github.com/ClickHouse/loom/blob/main/server/cli/tracker_feed.py)：`register_tracker_feed` 和 `_run` 选择自有记录及其单独的写入凭据；`_validate_writer` 检查授权和受支持的表引擎。
- [Feed 应用](https://github.com/ClickHouse/loom/blob/main/server/tracker/app/github_feed.py)：`initialize`、`configure`、`sync` 和 `_publish` 强制执行所有权、标记发布隐私并隔离写入。
- [记录存储](https://github.com/ClickHouse/loom/blob/main/server/tracker/store/github_feed.py)：`statements` 定义契约视图；`repository`、`put_repository` 和 `put_items` 以一致的方式读取发布状态。
- [API 客户端](https://github.com/ClickHouse/loom/blob/main/server/clients/github_feed.py)：`pages` 限定上游扫描；[领域规则](https://github.com/ClickHouse/loom/blob/main/server/tracker/domain/github_feed.py)验证身份，并通过 `next_cursor` 选择重放边界。

<a id="invariants"></a>

## 不变式

- 复制的内容保留在团队已注册的记录中，由团队负责擦除。
- 仓库身份、可见性和修订检查先于内容发布；在受支持的存储拓扑上，控制读取包含已确认的记录写入。
- 内容及其隐私分类共用同一个原始行版本。契约隐私和 UUID 包含每个已存储组成部分的分类，即使各表各自滞后也是如此。
- 从私有到公开的转换会在公开发布之前重置游标，因此重试会重放保留的内容，而不会过早丢失其私有分类。
- 记录范围的写入租约覆盖发布和检查点写入。不确定的发送会保留其声明直至结算；游标仅在已确认的写入之后推进。
- 整毫秒的内容版本在记录内所有仓库之间递增，因此合并后的条目／评论正文对现有 tracker 游标保持可见。
