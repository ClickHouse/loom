<!-- i18n: source=docs/features/tracker/github-feed.md blob=81fe53135dda14b5a303c6f1f10b89c97e768376 model=claude-sonnet-5-5 -->
> 本页由机器从[英文原文](https://github.com/ClickHouse/loom/blob/main/docs/features/tracker/github-feed.md)翻译，内容以英文原文为准。
> Machine-translated from the [English page](https://github.com/ClickHouse/loom/blob/main/docs/features/tracker/github-feed.md), which is authoritative.

<a id="maintain-a-github-tracker-record"></a>

# 维护 GitHub tracker 记录

`loom tracker-feed` 会把仓库的 issue、拉取请求、issue 评论和行内评审评论复制到团队专用的 ClickHouse 数据库中。Loom 通过[只读 tracker 数据源](https://github.com/ClickHouse/loom/blob/main/docs/features/tracker/record-contract.md)读取生成的视图。复制内容、备份和删除由团队自行负责。该作业不会调用任何模型或 embedding。

<a id="prepare-the-record"></a>

## 准备记录

使用一个空数据库，其数据库 UUID 须保持稳定，并与 Loom 部署数据库分开。为专用的写入账户授予在该数据库中创建表和视图、读取并插入行的权限。其凭据须与 tracker 读取账户的凭据分开保管。

```sql
CREATE DATABASE team_feed;
CREATE USER feed_writer IDENTIFIED BY '…';
GRANT CREATE TABLE, CREATE VIEW, SELECT, INSERT ON team_feed.* TO feed_writer;
GRANT SELECT ON system.columns TO feed_writer;
GRANT SELECT ON system.databases TO feed_writer;
GRANT SELECT ON system.tables TO feed_writer;
CREATE USER feed_reader IDENTIFIED BY '…' SETTINGS readonly = 2;
GRANT SELECT ON system.columns TO feed_reader;
```

通过调度器的密钥机制提供写入账户的密码，变量名为 `LOOM_TRACKER_FEED_WRITER_PASSWORD`。运行 CLI 时使用与服务进程相同的 Loom 部署配置，包括其 ClickHouse 连接和密钥根。
将 `LOOM_TRACKER_ORG` 设置为要初始化的组织。CLI 在使用前会校验写入账户的直接授权，并拒绝角色、授权选项以及该数据库之外的权限，上述三个系统元数据表除外。

```bash
loom tracker-feed init --org acme --host record.example --database team_feed \
  --writer-user feed_writer
```

初始化会以 definer 视图的形式创建 `github_issues`、`github_prs` 和 `edges`。只向读取账户授予这些视图的访问权限：

```sql
GRANT SELECT ON team_feed.github_issues TO feed_reader;
GRANT SELECT ON team_feed.github_prs TO feed_reader;
GRANT SELECT ON team_feed.edges TO feed_reader;
```

使用 `feed_reader` 以 views 模式[注册 tracker 数据源](https://github.com/ClickHouse/loom/blob/main/docs/features/tracker/record-contract.md#adding-a-tracker-source)。将每个仓库加入数据源的命名空间允许列表，并为私有记录使用受限命名空间。将部署的 `LOOM_TRACKER_ORG` 设置为同一组织。数据源注册仍保留原有的主机、凭据和契约检查。HTTP 和私有地址需要开启开发用的私有主机设置；生产环境的连接使用 HTTPS。

<a id="configure-and-schedule-a-repository"></a>

## 配置并调度仓库

创建一个只读的 fine-grained 令牌或 App 安装令牌，范围仅限于要复制的仓库。它需要 Metadata、Issues 和 Pull requests 的读取权限。`--read-only-token` 是操作人员对权限的声明；CLI 无法通过请求成功或令牌前缀来证明该令牌没有写入授权。

通过密钥机制提供该令牌，变量名为 `LOOM_TRACKER_FEED_GITHUB_TOKEN`，然后配置一个在允许列表中的仓库：

```bash
loom tracker-feed configure --org acme --source widgets --repo acme/widgets \
  --writer-user feed_writer --read-only-token --max-pages 5
loom tracker-feed sync --org acme --source widgets --repo acme/widgets \
  --writer-user feed_writer
```

配置时会把令牌加密封存在团队的 feed 数据库中。同步需要写入账户密码和部署的密钥根，但不需要令牌环境变量。
复制的仓库标识沿用已注册数据源允许列表中的拼写，因此即使 API 返回的大小写不同，命名空间过滤器也能匹配。
App 安装令牌[一小时后过期](https://docs.github.com/en/apps/creating-github-apps/authenticating-with-a-github-app/authenticating-as-a-github-app-installation)。外部密钥流程必须在过期前刷新安装令牌并重新运行 `configure`。feed 不会签发或刷新安装令牌。
使用 `--writer-password-env` 或 `--token-env` 可选择其他凭据变量。
在轮换根密钥期间，成功的元数据写入会重新封存由 `LOOM_JWT_SECRET_PREVIOUS` 打开的凭据。在停用旧的根密钥之前，请先完成一次成功的轮次。

每五分钟或更频繁地调度 `sync`。可见性观测在 15 分钟后过期，如果检查持续失败，仓库将被隐藏。每个作业有 120 秒的时限，每个流最多读取 `--max-pages` 页，每页 100 条，页数预算为 2 到 100 页。最后一页的时间戳边界会被重放；最小值为后续进展保留了前一页。
容量错误会使游标保持不变；重试前请提高有限的页数预算，或缩短该仓库的积压量。标准输出中的计数报告已处理的行数，包括重放的行。

<a id="visibility-and-erasure"></a>

## 可见性与删除

每个流都会校验仓库的不可变标识和当前可见性。检查失败会隐藏其视图。每个条目和评论都会将该次观测的隐私分类与其内容一起保存。视图采用仓库、条目和已存储评论中最严格的分类，即使这些表到达副本的时间不同也是如此。这是一种带有限过期时间的轮询观测，而不是即时的权限镜像。常规的 tracker 隐私规则和直接授权检查仍然适用。

当仓库变为公开时，配置或同步会重置其游标，调度的轮次会在相同的页数预算内重放所有流。条目的行及其已存储评论在公开观测下刷新后，该条目才会变为公开。上游已删除的行需要删除保留的副本；重放无法刷新它们。

正文最多包含 32,768 个条目字符和最近更新的 64 条评论，每条评论最多 2,048 个字符。当数据源未提供相关证据时，评审请求和测试证据仍为未知。
视图的 `updated_at` 跟踪条目和评论在记录中的发布时间，因此在条目之后导入的评论仍能被增量 tracker 读取看到。

增量列表不会报告删除。要删除复制的内容，请停止所有调度的写入作业，删除受影响的原始行、评论以及仓库配置或游标，并在重新启动前将该仓库排除。删除 Loom 命名空间会移除 Loom 的派生数据，但不会删除团队的数据库或备份。初始化会把一条记录绑定到一个部署和一个组织。其他部署请使用单独的记录。

有关游标边界、并发和所有权，请参阅 [feed 架构](https://github.com/ClickHouse/loom/blob/main/docs/architecture/github-feed.md)。
