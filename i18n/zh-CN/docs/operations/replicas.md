<!-- i18n: source=docs/operations/replicas.md blob=f4c8729ef4e795f5b6ab89dcd363a0bf9c30aad0 model=claude-sonnet-5-5 -->
> 本页由机器从[英文原文](https://github.com/ClickHouse/loom/blob/main/docs/operations/replicas.md)翻译，内容以英文原文为准。
> Machine-translated from the [English page](https://github.com/ClickHouse/loom/blob/main/docs/operations/replicas.md), which is authoritative.

<a id="running-n-replicas"></a>

# 运行 N 个副本

每个 Loom 进程都是一个完整的副本。你可以按需运行任意数量的副本，让它们共用同一个 ClickHouse 服务和同一个 URL，无需粘性会话：每个副本都处理所有 op，验证所有令牌，并写入共享服务。只需执行一次的工作，由持有 ClickHouse lease 的进程负责，该进程停止后，这些工作会转移到其他进程：

| 每个部署只执行一次 | 运行位置 |
| ----------------------------------------- | ---------------------------------------------------------------------------------------------------------- |
| Schema 迁移 | 第一个启动带有待执行迁移的版本的进程，持有 migrate lease；其他进程等待它完成 |
| 后台 worker | 持有 `workers_supervisor` lease 的进程（[部署](https://github.com/ClickHouse/loom/blob/main/docs/operations/deployment.md)） |
| 发布部署的配置 | 同一个 lease 持有者（见下文） |

每个进程各自保存自己的 git 镜像（历史读取时会克隆缺失的镜像；见 `LOOM_CODE_HISTORY_CLONE_ON_READ`）、自己的缓存和自己的限流桶。

<a id="one-env-for-all-of-them"></a>

## 所有副本使用同一份 env

用同一份 env 启动每个副本，并设置 `LOOM_DEPLOYMENT=1`：

```bash
LOOM_DEPLOYMENT=1
LOOM_CH_HOST=...                 # the one ClickHouse service
LOOM_CH_PORT=8443
LOOM_CH_USER=...
LOOM_CH_PASSWORD=...
LOOM_JWT_SECRET=...              # the same root secret on every replica
LOOM_EMBEDDING_MODEL=...         # the same model and width on every replica
LOOM_EMBEDDING_DIM=...
OPENAI_API_KEY=...               # each replica makes its own provider calls
LOOM_TRUSTED_PROXIES=...         # the load balancer, so per-client limits see the client
```

`LOOM_JWT_SECRET` 以 kid `default` 签名，并加密保护其他所有签名密钥和所有已存储的凭据，因此只要取值相同，每个令牌在每个副本上都有效。签名密钥、撤销记录、授权和服务账号都是共享服务中的行；密钥轮换或撤销会在 `LOOM_JWT_KEYRING_REFRESH_SECONDS` 和 `LOOM_JWT_REVOCATION_CACHE_TTL_SECONDS` 内传播到每个副本。

<a id="configuration-drift"></a>

## 配置漂移

每个进程每隔 `LOOM_INSTANCE_HEARTBEAT_SECONDS`（默认 10 秒）向 `loom.instances` 写入一行心跳：包含它的构建版本、MUST_MATCH 设置的指纹（embedding 空间：`LOOM_EMBEDDING_PROVIDER`、`_MODEL`、`_DIM`、`_BASE_URL` 和 `_SEND_DIMENSIONS`）、根密钥的指纹以及启动时间。如果某一行连续三个间隔没有续期，或者其进程在关闭时将其标记为已停止，则该行不再视为存活。

lease 持有者把它的配置发布到 `loom.deployment_config`：包括 MUST_MATCH 设置和根密钥的 HMAC 指纹。每个进程在启动时以及每次心跳时，都会将自身与该快照比较。根密钥不同（它的令牌和已存储的凭据在其他副本上无法打开），或 MUST_MATCH 的值不同（它的向量会落在另一个空间），都属于漂移：该进程会变为未就绪，负载均衡器不再向它发送请求，`/v1/health` 会报告 `config_drift` 问题及具体差异。未就绪只会阻止新连接，因此该进程还会对所有 API、MCP 和登录调用返回 `503 CONFIG_DRIFT` 并关闭连接：客户端重试时会落到一致的副本上。用于比较的值是 env 中的值：在控制台中切换模型属于整个部署（每个副本都从 ClickHouse 读取），因此不算漂移，之后用相同 env 启动的副本是就绪的。`/v1/health` 的 `deployment_config` 会显示最近一次检查和存活的主机。

lease 持有者绝不会覆盖与自己存在漂移的快照：根密钥或 embedding 模型错误的副本即使拿到 lease，也仍然保持未就绪，而使用已发布 env 的副本保持就绪。对于与自己一致的快照，只有在没有其他存活副本运行已存储的那份配置时才会重写，因此滚动升级期间 N-1 和 N 两种形态不同的快照不会来回翻转。

**更改 MUST_MATCH 设置或 `LOOM_JWT_SECRET`** 无法滚动进行：使用新值的副本未就绪，而使用旧值的副本仍在提供服务。请停止所有副本，修改 env，用新 env 运行 `loom deployment publish --yes`（它会列出将要变更的内容），然后再启动它们（embedding 变更之后需要完整重新 embed，使用 `loom reembed`）。更改密钥之后，需要重新签发令牌并再次存储凭据。

没有设置 `LOOM_DEPLOYMENT` 的进程（同一服务上的分支或 A/B 服务器）同样会发送心跳，但从不发布，也不会被检查，并且不计入副本数。

<a id="rate-limits"></a>

## 限流

配置的上限针对整个部署。每个副本分到的份额是上限除以在 `loom.instances` 中有存活行的主机数，因此增加或移除副本会在一次心跳内调整每个桶的大小。`LOOM_REPLICA_COUNT` 是该计数的下限，也是无法读取这些行时所用的计数。同一主机上的多个 web worker 共用一张桶表（[部署](https://github.com/ClickHouse/loom/blob/main/docs/operations/deployment.md)）。

<a id="code-repositories"></a>

## 代码仓库

每个进程每隔 `LOOM_CODE_REPOS_REFRESH_SECONDS`（默认 30 秒）重新读取 `loom.code_repos`，因此在某个副本上添加或限制的仓库，会在该时间内对所有副本生效。

如果某台主机不应提供 git 历史（例如没有磁盘存放镜像的边缘节点），可设置 `LOOM_GIT_HISTORY=none`：此后 blame、commit 检查和 PR 历史在没有它的情况下作答。

<a id="stopping-a-replica"></a>

## 停止副本

请优雅地停止它（`docker stop`，SIGTERM）：它会将自己的心跳行标记为已停止，并注销其 provider participant 行。强制终止会让两者都保留下来；心跳行在三个间隔后不再计入。在同一容器中重启的进程会自行注销已死进程的 participant 行。如果 provider 的重新配置在别处等待某个已死进程的行，请在确认该进程已不存在后，用其 `/v1/health` 报告的 `participant_id` 将其注销（如果该行的心跳仍然新鲜，或其查询仍在运行，命令会拒绝执行）：

```bash
loom participant retire --participant <participant_id> --yes
```
