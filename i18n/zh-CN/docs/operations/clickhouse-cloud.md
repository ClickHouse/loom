<!-- i18n: source=docs/operations/clickhouse-cloud.md blob=3fc467ae1903f7f94d3cdaccb6831c92c883fd58 model=claude-sonnet-5-5 -->
> 本页由机器从[英文原文](https://github.com/ClickHouse/loom/blob/main/docs/operations/clickhouse-cloud.md)翻译，内容以英文原文为准。
> Machine-translated from the [English page](https://github.com/ClickHouse/loom/blob/main/docs/operations/clickhouse-cloud.md), which is authoritative.

<a id="clickhouse-cloud-and-managed-postgres"></a>

# ClickHouse Cloud 与托管 Postgres

本指南在 Kubernetes 上运行 Loom，并使用两个托管服务：ClickHouse Cloud 负责投影、向量、历史和分析，ClickHouse Managed Postgres 作为记忆的权威存储。内容涵盖这两个服务、chart 的 `postgres` 配置项、安装后的检查、轮换 Postgres 密码和 CA，以及 SQL 接口用户。chart 本身的说明见 [Kubernetes](https://github.com/ClickHouse/loom/blob/main/docs/operations/kubernetes.md)，权威存储的说明见 [Postgres 记忆权威存储](https://github.com/ClickHouse/loom/blob/main/docs/operations/postgres.md)。

覆盖文件 `deploy/helm/loom/values-clickhouse-cloud.yaml` 叠加在 EKS 的配置之上。它适用于部署在空的 ClickHouse 上：权威存储在部署首次启动时选定，如果 ClickHouse 中已有记忆，pod 将拒绝启动，因为目前没有任何迁移方式可以把记忆从 ClickHouse 权威存储迁移到 Postgres。

<a id="the-services"></a>

## 服务

- **ClickHouse Cloud**：一个包含名为 `loom` 的数据库和一个供 Loom 使用的用户的服务，其 IP 访问列表允许集群的 NAT 地址（[Kubernetes，在 EKS 上](https://github.com/ClickHouse/loom/blob/main/docs/operations/kubernetes.md#on-eks)）。
- **ClickHouse Managed Postgres**：一个带有**两个 HA 备库**的服务，包含一个数据库和拥有该数据库的角色，并使用 5432 端口上的**直连**端点（[Postgres，ClickHouse Managed Postgres](https://github.com/ClickHouse/loom/blob/main/docs/operations/postgres.md#clickhouse-managed-postgres)）。请为该角色授予 `pg_read_all_stats`，以便 Loom 能够看到已连接的备库：

  ```sql
  GRANT pg_read_all_stats TO loom;
  ```

<a id="secrets"></a>

## 密钥（Secrets）

overlay 会读取 `loom-secrets` 旁边的两个 Secret：`loom-postgres`，键为 `password`，存放该角色的密码；`loom-postgres-ca`，键为 `ca.crt`，存放 CA 证书包。创建方式与 `loom-secrets` 相同，每个 Secret 各用一个独立的 ExternalSecret。

`ca.crt` 包含该服务的 CA 当前发布的所有根证书，以 PEM 格式依次拼接；驱动会把该文件作为证书包加载，除此之外不信任任何其他证书。请从 CA 发布的列表中获取根证书（服务器证书中的 "CA Issuers" URL 指明了该 CA）；`openssl s_client -starttls postgres -showcerts` 通常打印的证书链不包含根证书。如果获取到的根证书是 DER 格式，请先转换：

```bash
openssl x509 -inform DER -in root.der -out root.pem
```

将各个 `root.pem` 文件拼接成 `ca.crt`，即 Secret 所存放的文件。然后按驱动的方式，只信任该证书包，用它校验服务器发送的证书链：`leaf.pem` 是 `s_client -showcerts` 打印的第一个证书，`chain.pem` 是其余证书（`-no-CAstore` 需要 `openssl` 3 版本）：

```bash
openssl verify -no-CApath -no-CAstore -CAfile ca.crt -untrusted chain.pem \
  -verify_hostname <postgres host> leaf.pem
```

证书包未覆盖的证书会导致校验失败并拒绝连接：使用它启动的 pod 无法启动，日志中会出现 `SSLCertVerificationError`；对于正在运行的 pod，每个新连接都会失败。当 CA 宣布其层级结构发生变更时，请使用新的 Secret 名称更新证书包（见下文）。

<a id="install"></a>

## 安装

```bash
helm install loom deploy/helm/loom -n loom \
  -f deploy/helm/loom/values-eks.yaml -f deploy/helm/loom/values-clickhouse-cloud.yaml \
  --set publicBaseUrl=https://loom.example.com \
  --set clickhouse.host=<service>.<region>.aws.clickhouse.cloud \
  --set postgres.host=<postgres direct host>
```

设置 `postgres.host` 后，chart 会为每个运行 Loom 的 pod（web、workers、hook Job 和分析导出）配置以下内容：

- `LOOM_STORAGE_AUTHORITY=postgres`，以及分别来自 `postgres.durability` 和 `postgres.poolMaxSize` 的 `LOOM_PG_DURABILITY` 和 `LOOM_PG_POOL_MAX_SIZE`；
- 不含密码的 `LOOM_PG_DSN`，即 `postgres://<user>@<host>:<port>/<database>?sslmode=verify-full&sslrootcert=/etc/loom/pg-ca/<key>`，因此密码不会出现在 pod spec 中；
- 来自 `postgres.passwordSecret` 的 `PGPASSWORD`，当 DSN 中没有密码时驱动会读取它；
- `postgres.caSecret` 以只读方式挂载到 `/etc/loom/pg-ca`；
- 启用 `networkPolicy.enabled` 时，允许出站访问 `postgres.port`。

如果 `postgres` 块无法按原样传递下去，chart 会拒绝渲染；如果它渲染的任何名称在 `env`、`extraEnv`、`secrets.extra` 或 `settingsFile` 中被再次设置，chart 也会拒绝渲染。每次拒绝都会指出需要修正的值，`deploy/helm/loom/values.yaml` 中该块的注释说明了相关规则。chart 渲染的值优先于 `existingSecret` 和 `envFrom` 中的同名项。

<a id="connections"></a>

### 连接

每个 Loom 进程最多持有 `postgres.poolMaxSize` 个连接；如果调用发现连接全部繁忙，会等待最长 `LOOM_PG_STATEMENT_TIMEOUT_MS` 以获取连接，超时后返回 503。滚动发布期间，旧 pod 和新 pod 会同时运行，因此服务的连接容量应按稳定连接数的两倍来规划：

```text
poolMaxSize × (webWorkers × web pods + worker processes) × 2
  + booting processes + running erasures + CLI runs  ≤  max_connections − reserved
```

启用自动扩缩容时，web pod 数为 `autoscaling.maxReplicas`，否则为 `replicaCount`；worker 进程数在 `workers.enabled` 时为 `workers.replicaCount`，否则为一个，由 web pod 运行。每个正在运行的擦除在 Postgres 步骤中会多占用一个连接。overlay 将 `webWorkers` 设为 2（镜像会根据 chart 的 2 CPU 限制选择该值），将 `poolMaxSize` 设为 5：在有三个 web pod 和一个 workers pod 的情况下，共需 70 个连接，适合 `max_connections` 为 Loom 留出 100 个连接的服务。提高 `webWorkers` 时，需要相应调整 `resources.limits.cpu` 和内存（每个进程 1 GiB）。
其他途径在 chart 副本数之外额外增加的副本不计入该总和。

<a id="after-the-install"></a>

## 安装之后

- 当服务的 `synchronous_standby_names` 为空时，Loom 拒绝启动。如果已连接的备库数量少于配置中列出的数量，Loom 仍会启动，并记录 WARNING `Postgres durability: fewer synchronous standbys are attached than required`。此后，提交会等待这些备库，最长为 `LOOM_PG_STATEMENT_TIMEOUT_MS` 加一秒，然后返回 503 `POSTGRES_UNAVAILABLE`，并附带 `outcome_unknown`（[启动时检查的内容](https://github.com/ClickHouse/loom/blob/main/docs/operations/postgres.md#what-the-boot-checks)）。
- `GET /v1/health` 会报告 `storage_authority: postgres` 和 `pg_ok`。`GET /v1/health?include=postgres` 会额外返回一个 `postgres` 块，其中包含 `standby: synchronous`；只要 `pg_ok` 为 false，该块就不会出现，即检查所用的 `SELECT 1` 因任何原因失败，或耗时超过 2 s。
- 如果角色没有 `pg_read_all_stats`，Loom 仍可启动，但启动时会记录 WARNING `Postgres durability: cannot see pg_stat_replication; standbys are unverified`，同时 `standby` 的值为 `null`。这条 WARNING 才是可靠的信号；当读取失败或耗时超过 2 s 时，`standby` 同样为 `null`。

<a id="rotating-the-password-and-the-ca"></a>

## 轮换密码和 CA

密码轮换按以下顺序连续执行：

1. 在密钥存储中更新密码，并确认 `loom-postgres` 已同步。
2. 在服务上运行 `ALTER ROLE loom PASSWORD '…'`。
3. 对 web 和 workers 两个 Deployment 一起运行 `kubectl rollout restart`。

已打开的连接继续可用。在第 2 步和第 3 步之间，如果某次调用所在的 pod 必须新建连接（连接池会关闭空闲 300 s 的连接），则返回 500，服务器日志显示 `InvalidPasswordError`（SQLSTATE 28P01）；在 pod 重启之前，重试无效。在第 1 步和第 2 步之间启动的 pod（扩容、驱逐、分析导出或 hook Job）在第 2 步完成之前无法启动，下次重启时才能启动成功。

CA：以新的 Secret 名称创建新的证书包，并执行 `helm upgrade`，将 `postgres.caSecret.name` 设置为该名称。pod 规格会发生变化，因此 pod 会滚动更新。只有在滚动更新完成，且没有任何可能回滚到的修订版本引用旧 Secret 之后，才能删除旧 Secret。

<a id="the-sql-surface-user"></a>

## SQL 接口用户

组织通过一个 ClickHouse 用户以 SQL 方式读取其 agent 的记忆，该用户由 `loom sql-access grant` 创建（[SQL 接口](https://github.com/ClickHouse/loom/blob/main/docs/reference/sql-surface.md)）。在 web pod 中使用具有创建用户权限的 ClickHouse 管理员凭据来运行该命令，管理员密码从文件传入，不要在会回显的终端中输入：

```bash
web=$(kubectl get deploy -n loom -o name \
  -l app.kubernetes.io/instance=loom,app.kubernetes.io/component=web)
kubectl exec -i -n loom "$web" -- sh -c \
  'read -r LOOM_CH_PASSWORD; export LOOM_CH_PASSWORD LOOM_CH_USER=default;
   exec loom sql-access grant --user acme_sql --org acme --namespaces support,research' < admin.pass
```

该命令只会向终端打印一次 SQL 用户的密码：exec 流不是容器日志。`--namespaces '*'` 还会涵盖以后创建的命名空间；对于允许自助注册的组织，切勿使用它（[访问权限](https://github.com/ClickHouse/loom/blob/main/docs/reference/sql-surface.md#access)）。如果部署中的数据只属于一个组织，可以用 `--all` 代替 `--org … --namespaces …`。卸载之前请先撤销该用户：`loom sql-access revoke --user acme_sql`，运行方式相同。

<a id="restoring-a-service"></a>

## 恢复服务

恢复任一服务之后，都要运行 `loom reconcile`，它在发布处于停止状态时，以 web pod 的规格创建的 Job 中运行（[恢复单个存储](https://github.com/ClickHouse/loom/blob/main/docs/operations/backup-and-recovery.md#restoring-one-store)）。

<a id="related"></a>

## 相关内容

[Kubernetes](https://github.com/ClickHouse/loom/blob/main/docs/operations/kubernetes.md) · [Postgres 记忆权威](https://github.com/ClickHouse/loom/blob/main/docs/operations/postgres.md) ·
[部署](https://github.com/ClickHouse/loom/blob/main/docs/operations/deployment.md) · [SQL 接口](https://github.com/ClickHouse/loom/blob/main/docs/reference/sql-surface.md) ·
[从 Postgres 查询 Loom](https://github.com/ClickHouse/loom/blob/main/docs/guides/query-from-postgres.md)
