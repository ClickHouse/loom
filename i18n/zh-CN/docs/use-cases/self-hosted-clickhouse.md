<!-- i18n: source=docs/use-cases/self-hosted-clickhouse.md blob=347e84ca2fb7bc0a85662a875e99064979582695 model=claude-sonnet-5-5 -->
> 本页由机器从[英文原文](https://github.com/ClickHouse/loom/blob/main/docs/use-cases/self-hosted-clickhouse.md)翻译，内容以英文原文为准。
> Machine-translated from the [English page](https://github.com/ClickHouse/loom/blob/main/docs/use-cases/self-hosted-clickhouse.md), which is authoritative.

<a id="self-hosted-agent-memory-on-clickhouse"></a>

# 基于 ClickHouse 的自托管 agent 记忆

Loom 运行在你自己的基础设施上，可以使用 Docker，也可以通过 Helm chart 部署在 Kubernetes 上，并将 agent 记忆、文档、对话和代码索引作为表存储在 ClickHouse 中。部署时可以在首次启动时选择 Postgres 来保存实时记忆和知识状态。数据只存储在你自己的存储中（你配置的托管模型提供方和追踪接收端会收到它们所处理的内容），并且你的 agent 所掌握的知识可以通过 SQL 查询。

<a id="what-you-get"></a>

## 你将获得

- **Docker 或 Kubernetes。** 一行命令的安装程序会启动本地栈；Helm chart 会在同一个 URL 后运行 N 个相同的副本。[部署](https://github.com/ClickHouse/loom/blob/main/docs/operations/deployment.md) ·
  [Kubernetes](https://github.com/ClickHouse/loom/blob/main/docs/operations/kubernetes.md) · [运行 N 个副本](https://github.com/ClickHouse/loom/blob/main/docs/operations/replicas.md)
- **ClickHouse OSS 或 ClickHouse Cloud。** [选择 ClickHouse](https://github.com/ClickHouse/loom/blob/main/docs/operations/deployment.md#choosing-the-clickhouse) ·
  [ClickHouse Cloud 与 Managed Postgres](https://github.com/ClickHouse/loom/blob/main/docs/operations/clickhouse-cloud.md)
- **向量、文本和审计共用一个存储。** 在默认的 ClickHouse 权威存储下，向量搜索、词法搜索和图搜索，以及过滤、审计和删除，都是同一个数据库中的 SQL。[存储权威](https://github.com/ClickHouse/loom/blob/main/docs/architecture/storage-authority.md)
- **多用户。** 组织、命名空间、角色、令牌和单点登录。[身份与命名空间](https://github.com/ClickHouse/loom/blob/main/docs/concepts/identities-and-namespaces.md) ·
  [单点登录](https://github.com/ClickHouse/loom/blob/main/docs/operations/sso.md)
- **本地模型。** 发布的栈使用内置的本地模型在 CPU 上生成嵌入，嵌入提供方也可以接入你自己运行的 OpenAI 兼容端点。[模型与 API 密钥](https://github.com/ClickHouse/loom/blob/main/docs/getting-started/models-and-keys.md)

<a id="measured"></a>

## 实测

在一次合成负载测试中，每个键有 2.4 个版本，嵌入维度为 3072（每行两个向量），ClickHouse 中每个键约占 13.6 KB，其中几乎全部是嵌入行；更小的嵌入模型占用更少，而行数很少的 part 在合并之前，每行会占用稍多一些。[大规模下的记忆](https://github.com/ClickHouse/loom/blob/main/docs/benchmarks/results.md#memory-at-scale)

<a id="start"></a>

## 开始

[快速开始](../getting-started/quickstart.md) · [前提条件](https://github.com/ClickHouse/loom/blob/main/docs/getting-started/overview.md#prerequisites) ·
[升级](https://github.com/ClickHouse/loom/blob/main/docs/operations/upgrades.md) · [备份与恢复](https://github.com/ClickHouse/loom/blob/main/docs/operations/backup-and-recovery.md)
