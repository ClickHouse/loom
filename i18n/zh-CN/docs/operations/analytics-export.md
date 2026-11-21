<!-- i18n: source=docs/operations/analytics-export.md blob=241a7492fe019249ba054a278a0059bcb928b7e0 model=claude-sonnet-5-5 -->
> 本页由机器从[英文原文](https://github.com/ClickHouse/loom/blob/main/docs/operations/analytics-export.md)翻译，内容以英文原文为准。
> Machine-translated from the [English page](https://github.com/ClickHouse/loom/blob/main/docs/operations/analytics-export.md), which is authoritative.

<a id="exporting-usage-to-a-data-warehouse"></a>

# 将使用情况导出到数据仓库

`loom export-analytics` 会将某一个 UTC 日的使用情况以及命名空间的快照以 Parquet 文件的形式写入 S3 存储桶，每个实体每天一个文件，供数据仓库加载。Helm chart 会将其作为 CronJob 每天运行一次（见 `values.yaml` 中的 `analyticsExport`）。若要就地查询某个租户的记忆（含其内容）及其请求账本，请改用 [SQL 接口](https://github.com/ClickHouse/loom/blob/main/docs/reference/sql-surface.md)。

```bash
loom export-analytics --bucket analytics-landing --prefix loom \
  --role-arn arn:aws:iam::123456789012:role/loom-analytics-writer \
  --expected-bucket-owner 123456789012
```

它需要 `LOOM_ANALYTICS_EXPORT_NAME_KEY`，即用于对名称进行哈希的密钥（参见[哈希名称](#hashed-names)）。

`--day YYYY-MM-DD` 可导出其他日期；默认为昨天（UTC）。尚未结束的日期会被拒绝。

<a id="files"></a>

## 文件

```text
<prefix>/<entity>/<yyyy>/<mm>/<dd>/<entity>-<yyyy>-<mm>-<dd>.parquet    entity: usage, namespaces
```

每次上传都带有 `If-None-Match: *`，因此对象不会被替换。同一天再运行一次时，会保留第一次运行生成的文件，并为每个文件打印 `already there, kept`；失败或重复的运行可以原样重试。若要重新导出某一天，请先删除该天的对象。

| 实体           | 每行对应                                                                                     | 列                                                                                                                                                                                                                                       |
| ------------ | ---------------------------------------------------------------------------------------- | ---------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `usage`      | 当天来自 `loom.agent_events` 的一组 `org`、`namespace`、`agent`、`operation`、`kind`、`transport`、`traffic_class` | `day`, `ops`, `errors`, `server_errors`, `slow`, `input_tokens`, `output_tokens`, `embedding_tokens`, `llm_calls`, `cost_usd`, `bytes_written`, `latency_p50_ms`, `latency_p95_ms`, `latency_p99_ms`, `users`, `sessions`, `snapshot_at` |
| `namespaces` | 在 `loom.namespaces` 中注册或持有有效记忆行的一组 `org`、`namespace`                                      | `registered`, `kind`, `visibility`, `active`, `created_on`, `live_memories`, `last_memory_on`, `day`, `snapshot_at`                                                                                                                      |

`usage` 中的 `kind`：`request` 表示请求，`worker` 表示一次 worker 处理，`verdict` 表示延迟执行的 `memory.ask` 一致性检查，`settle` 表示延迟执行的冲突裁定。每个 `/v1/me/*` 路由都算作同一个 `operation`，即 `me`：账本会以请求路径命名这些行，而请求路径可能包含调用方发送的任意字符串。`users` 和 `sessions` 是行内的去重计数，不包含未指明用户或会话的调用所记的 `-`；对多行求和时，同一用户在每一行中各计一次。`slow` 和延迟分位数来自 SLI 汇总：它们统计该汇总读取的 `ok` 请求（即具有 SLO 类别的操作），`slow` 为超出其延迟预算的请求（预算为 0 时每个请求都算慢），没有此类请求的行，其延迟为 0。`/v1/me/*` 路由、延迟执行的裁决或 worker 处理都不计入这两项。`namespaces` 文件反映的是 `snapshot_at` 时的状态，与 `--day` 无关：回填旧日期时，得到的是该日的 usage 和今天的命名空间。未注册的命名空间其 `registered = false`，注册表列为 NULL。`created_on` 和 `last_memory_on` 是日期而非时间：哈希后的个人命名空间旁若有精确时间，可能被用来匹配当时注册或写入的人。各行按哈希后的名称排序，而不是按名称本身排序。

<a id="what-never-leaves-loom"></a>

## 哪些内容绝不会离开 Loom

导出的列采用允许列表机制（`server/insights/store/analytics_export.py`；集成测试会固定每个实体的确切列清单）。存储桶中的文件不在[擦除](https://github.com/ClickHouse/loom/blob/main/docs/features/memory/retention-and-erasure.md)范围之内，因此导出不包含记忆内容，也不包含任何已存储的个人标识符：没有记忆的键或值，没有 `args`，没有 `user_id`、`session_id`、`subject`、`owner_email` 或请求 ID。用户和会话只以计数形式导出。

<a id="hashed-names"></a>

## 哈希名称

组织（org）、命名空间（namespace）和 agent 的名称可能指向某个具体的人：个人命名空间以其所有者的邮箱命名，自助注册会根据操作系统用户名和主机名生成 agent 的命名空间和名称，MCP 客户端也会为自己的 agent 命名。因此，`org`、`namespace` 和 `agent` 在导出时一律输出为名称在密钥下的参考实现 SipHash-2-4-128 哈希值的小写十六进制形式。

每个名称单独哈希，因此同一个字符串无论出现在哪里，哈希值都相同：同一个命名空间名称在每个组织中的哈希值相同，名称相同的组织、命名空间和 agent 也共用同一个哈希值。数据仓库无需密钥就能关联这些行，例如同一个个人命名空间出现在两个组织中的情况。

- 密钥是 `LOOM_ANALYTICS_EXPORT_NAME_KEY` 中长度不少于 32 个字符的随机字符串；如果密钥更短、为空，或带有首尾空白字符，导出将拒绝运行（用 `--from-file` 创建的 Secret 会保留文件末尾的换行符，这会使每个哈希都基于该换行符计算）。
- 16 字节的 SipHash 密钥是该字符串（UTF-8）的 SHA-256 的前 16 个字节。所用哈希函数是 ClickHouse 的 `sipHash128ReferenceKeyed`，任何 SipHash-2-4-128 库都可以用这 16 个字节复现其结果，因此持有密钥的系统可以对自己的 id 做哈希，再用来进行关联。ClickHouse 以两个字的形式接收密钥，每个字由 8 字节的一半按小端序读取。对 id 做哈希时，要使用与 Loom 存储的相同的字符串（UUID 或数字的 `toString`）：对带类型的列做 SipHash 会哈希其二进制形式，结果永远无法匹配。

  ```sql
  WITH SHA256({key:String}) AS d
  SELECT lower(hex(sipHash128ReferenceKeyed(
      (reinterpretAsUInt64(substring(d, 1, 8)), reinterpretAsUInt64(substring(d, 9, 8))),
      toString(tenant_id)))) AS hashed_tenant_id
  FROM tenants
  ```

- 哈希是假名，而不是匿名值：持有密钥的人可以根据已知的名称重新计算出哈希。在 Loom 中删除某个命名空间，不会删除已经在存储桶中的相应行。
- 只要密钥不变，哈希就保持稳定。更换密钥会改变所有哈希，而已导出日期的旧文件会保留：在用新密钥重新导出该日期之前，请先删除这些文件。
- 使用密钥的查询会记录密钥，无论密钥是作为参数绑定还是写成字面量：ClickHouse 会把绑定的值代入 `system.query_log` 中的查询文本，并写入其调试和跟踪日志。在数据仓库一侧，请限制谁可以读取运行关联查询的服务器上的这些日志，或者在 ClickHouse 之外用任意 SipHash-2-4-128 库对 id 做哈希。在 Loom 一侧，导出所用的密钥字也会出现在 Loom 的 ClickHouse 的同样日志中，而能读取这些日志的人本来就能读取这些名称。

<a id="credentials"></a>

## 凭据

该命令使用其进程的默认 AWS 凭据，并在执行任何 ClickHouse 操作之前检查一次：指定 `--role-arn` 时通过assume该角色来检查，未指定时则通过 STS 查询凭据属于谁（`GetCallerIdentity`）。凭据缺失或已过期，或者无法 assume 该角色，都会在这一步失败，而不是在读取之后才失败；缺少 `s3:PutObject` 授权、bucket 错误，或者 bucket 的所有者不是 `--expected-bucket-owner` 指定的账户，则会在第一次上传时失败。指定 `--role-arn` 时，它会在每次上传之前重新 assume 该角色，并以该角色的凭据写入。assume 写入角色适用于 bucket 位于另一个账户的情况：bucket 所在账户向写入角色授予 `s3:PutObject`，而写入角色只信任导出任务自身的身份。导出不需要对 bucket 拥有读取、列出或删除权限；使用客户管理的 KMS 密钥加密的 bucket，还需要为写入角色授予该密钥的 `kms:GenerateDataKey` 权限，否则每次上传都会被拒绝。`--expected-bucket-owner` 会让 S3 在 bucket 属于其他账户时拒绝写入。

请传入 `--region`（即 bucket 所在区域，在 chart 中为 `analyticsExport.region`），除非进程已经通过 `AWS_DEFAULT_REGION` 或 AWS 配置文件获得了区域；boto3 在这里不会读取 `AWS_REGION`。

<a id="on-kubernetes"></a>

## 在 Kubernetes 上

```yaml
analyticsExport:
  enabled: true
  bucket: analytics-landing
  prefix: loom
  roleArn: arn:aws:iam::123456789012:role/loom-analytics-writer
  expectedBucketOwner: "123456789012"
  nameKeySecret:
    name: loom-analytics-export-key # key LOOM_ANALYTICS_EXPORT_NAME_KEY
  serviceAccount:
    annotations:
      eks.amazonaws.com/role-arn: arn:aws:iam::210987654321:role/loom-analytics-export
```

```bash
kubectl create secret generic loom-analytics-export-key \
  --from-literal=LOOM_ANALYTICS_EXPORT_NAME_KEY="$(openssl rand -hex 32)"
```

只有导出任务的 pod 会读取该 Secret。如果没有 `nameKeySecret.name`，chart 会拒绝渲染；凡是会把该密钥交给每个 web 和 workers pod 的配置，chart 也会拒绝：在 `env`、`extraEnv` 或 `secrets.extra` 中设置 `LOOM_ANALYTICS_EXPORT_NAME_KEY`，在 `settingsFile`（每个 pod 都会挂载的 ConfigMap）中设置 `analytics_export_name_key`，以及让 `nameKeySecret.name` 指向该 release 的 Secret（`secrets.existingSecret` 或 chart 自带的 Secret）或某个 `envFrom` Secret。chart 无法查看这些 Secret 的内容：请不要把该密钥放进其中。

chart 会创建一个名为 `<fullname>-analytics-export` 的 ServiceAccount，以及同名的 CronJob，其中 `<fullname>` 是 chart 的完整名称：设置了 `fullnameOverride` 时取该值；否则，若 release 名称包含 chart 名称（`nameOverride`，否则为 `loom`），则取 release 名称；再否则取 `<release>-<chart name>`；最后截取前 35 个字符，使名称不超过 52 个字符（`kubectl get sa` 会显示该名称）。该任务在 UTC 时间 00:30 运行（`schedule`），同一时间只运行一次，最多重试 3 次，所有尝试合计的期限为三小时；每条语句最多可运行 20 分钟，ClickHouse 客户端还可能重试一次，因此在较慢的日子里，尝试次数可能少于四次。拒绝（退出码 2）会让 Job 立即失败，而不是重试。该任务的 pod 不会沿用 web pod 的 `podLabels`、`podAnnotations` 或 `affinity`。
web 和 workers pod 使用各自的 ServiceAccount，因此可以assume writer 角色的那个角色只绑定到导出任务的 pod。使用 IRSA 时，该角色的信任策略指向 `system:serviceaccount:<namespace>:<fullname>-analytics-export`。设置了 `roleArn` 时，它唯一的权限是对 `roleArn` 执行 `sts:AssumeRole`；`roleArn` 为空时，它自行写入，需要对该 bucket 拥有 `s3:PutObject` 权限（如果使用客户管理的 KMS 密钥，还需要 `kms:GenerateDataKey`）。使用 EKS Pod Identity 时，把角色绑定到该 ServiceAccount，并让 `annotations` 保持为空。

该任务使用 release 的 Secret、设置、`extraEnv` 和 `extraVolumes` 读取 ClickHouse，与 web pod 相同，并且不向 ClickHouse 写入任何内容。运行失败会表现为 Job 失败；请在 CronJob 最近一次成功运行距今超过一天时发出告警。

如果某一天的运行在所有尝试后都失败，或者错过了启动窗口（`startingDeadlineSeconds`，一小时），之后不会自动补导出：下一次运行只会导出它前一天的数据。请用由该 CronJob 生成的 Job 来导出，这样它会使用导出任务的 ServiceAccount 和参数，只需在其命令中加上 `--day`：

```bash
kubectl create job --from=cronjob/<fullname>-analytics-export analytics-export-backfill \
  --dry-run=client -o yaml > backfill.yaml
# add `- --day=YYYY-MM-DD` to the container's command in backfill.yaml, then:
kubectl apply -f backfill.yaml
```

usage 文件是当天的数据；命名空间文件是运行时刻的快照。
