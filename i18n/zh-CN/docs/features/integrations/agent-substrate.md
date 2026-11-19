<!-- i18n: source=docs/features/integrations/agent-substrate.md blob=3054a7336ba22e483dd7ab222be1f25e1bd0deee model=claude-sonnet-5-5 -->
> 本页由机器从[英文原文](https://github.com/ClickHouse/loom/blob/main/docs/features/integrations/agent-substrate.md)翻译，内容以英文原文为准。
> Machine-translated from the [English page](https://github.com/ClickHouse/loom/blob/main/docs/features/integrations/agent-substrate.md), which is authoritative.

<a id="agent-substrate"></a>

# agent 基座

需要在同一个 Kubernetes 集群中同时运行 [agent 基座](https://ate.dev) v1.0.0 和 Loom 的 Helm chart：Substrate 的出口网关会把每个 agent 的 Loom 令牌添加到它的 MCP 调用中。

该令牌不属于 actor 的模板或快照。Substrate v1.0.0 需要 Kubernetes 1.37 或更高版本，并且节点上要有 gVisor 或 microVM 运行时。下面的步骤还需要 Substrate 的 Kubernetes 凭据提供程序，可通过 `ate-setup deploy ... --credential-provider='{"name":"k8s.io"}'` 部署。

下面的 Substrate 步骤遵循 Substrate 针对 v1.0.0 的官方文档；本页尚未在真实集群上完整运行过，请对照你所使用的 Substrate 版本进行检查。

<a id="how-the-pieces-fit"></a>

## 各部分如何配合

| 组件          | 运行位置                                                               | 作用                                                                         |
| ------------- | ---------------------------------------------------------------------- | ---------------------------------------------------------------------------- |
| Loom          | 命名空间 `loom` 中的 `helm install loom`                               | 记忆服务器；其 Service 为 `loom.loom.svc.cluster.local:7777`                 |
| Agent         | atespace 中的 Substrate actor                                          | 使用占位的 `Authorization` 请求头调用 Loom 的 MCP 端点 `/mcp`                |
| 出站策略      | Substrate 的控制平面（`kubectl ate`），由 `atenet-egress` 强制执行     | 允许 actor 访问 Loom，除非你添加规则，否则不允许访问其他任何地址             |
| Loom 令牌     | Kubernetes Secret                                                      | 每个 agent 一个；出站网关将其写入 `Authorization` 请求头                     |

每个请求都经由 Substrate 的出站网关（`ate-system` 中的 `atenet-egress`）离开 actor，网关会拒绝 actor 的策略未允许的所有出站流量。因此，Loom 看到的连接来自网关 pod，而不是来自 actor。每一行记录都会保存令牌的 subject，用来区分不同的 actor；该行的 `agent` 是 MCP 客户端的名称，运行同一客户端的 actor 共用该名称。

<a id="1-install-loom-in-the-cluster"></a>

## 1. 在集群中安装 Loom

按照 [Kubernetes](https://github.com/ClickHouse/loom/blob/main/docs/operations/kubernetes.md) 中的说明安装 chart，命名空间为 `loom`，release 名称为 `loom`，这样 Service 的名称就是 `loom`。agent 不需要 Ingress：它们通过集群 DNS 名称直接访问该 Service。

当 `networkPolicy.ingressFrom` 为空（默认值）时，chart 的 NetworkPolicy 允许任何来源。如果你在其中列出了对端（例如负载均衡器的子网），请把出口网关也加入列表，因为你设置的列表会替换默认值，而不是在默认值上追加：

```yaml
networkPolicy:
  enabled: true
  ingressFrom:
    # ...the peers you already allow...
    - namespaceSelector:
        matchLabels:
          kubernetes.io/metadata.name: ate-system
      podSelector:
        matchLabels:
          app: atenet-egress
```

<a id="2-a-token-per-agent"></a>

## 2. 每个 agent 一个令牌

为每个 agent 单独签发一个令牌，其服务账号主体以 atespace 和 Substrate actor 命名。Loom 会把该主体记录在 agent 写入的每一行上，而令牌的命名空间和角色决定了它可以读取和写入的内容（[令牌与授权](https://github.com/ClickHouse/loom/blob/main/docs/guides/tokens-and-grants.md)）。

在持有签名密钥的 Loom pod 中签发令牌，并将其存入一个 Secret，该 Secret 所在的命名空间只存放该 atespace 的令牌：

```bash
TOKEN=$(kubectl -n loom exec deploy/loom -- \
  loom mint-token --subject sa:research.research-agent --org acme \
    --namespaces research --roles writer --ttl-days 30)
kubectl create namespace loom-tokens-research
kubectl -n loom-tokens-research create secret generic research-agent \
  --from-literal=token="$TOKEN"
```

Substrate 的 Kubernetes credential provider 只会从已授予该 atespace 的命名空间中读取 Secret。管理员在 provider 的命名空间策略中为该 atespace 授予令牌所在的命名空间，然后重启 provider，因为 provider 在启动时读取该策略：

```bash
kubectl -n ate-system edit configmap k8s-credential-provider-namespace-policy
```

```yaml
data:
  namespace-policy.yaml: |
    policies:
    - atespace: research
      allowedNamespaces:
      - loom-tokens-research
```

```bash
kubectl -n ate-system rollout restart deployment/k8s-credential-provider
```

provider 授予 atespace 的是整个命名空间，而不是单个 Secret，因此被授予某个命名空间的 atespace 可以使用其中的每一个令牌：每个 atespace 使用一个命名空间，可以让它只接触自己 agent 的令牌。

令牌会在 `--ttl-days` 之后过期：在此之前，请签发新令牌并替换该 Secret。credential provider 在每次请求时都会读取该 Secret，因此下一次调用就会携带新令牌：

```bash
TOKEN=$(kubectl -n loom exec deploy/loom -- \
  loom mint-token --subject sa:research.research-agent --org acme \
    --namespaces research --roles writer --ttl-days 30)
kubectl -n loom-tokens-research create secret generic research-agent \
  --from-literal=token="$TOKEN" --dry-run=client -o yaml | kubectl apply -f -
```

<a id="3-configure-the-agent-and-create-its-actor"></a>

## 3. 配置 agent 并创建其 actor

Substrate 的文档没有描述 actor 的 MCP 配置：请在 agent 的镜像中配置其 MCP 客户端，或在 actor 模板中以字面环境值的形式配置。服务器条目是 Loom 的端点，并带有一个占位符请求头，网关会替换该请求头：

```json
{
  "mcpServers": {
    "loom": {
      "type": "http",
      "url": "http://loom.loom.svc.cluster.local:7777/mcp",
      "headers": { "Authorization": "Bearer placeholder" }
    }
  }
}
```

键名遵循所用的客户端（[MCP 客户端](https://github.com/ClickHouse/loom/blob/main/docs/features/integrations/mcp-clients.md)中列出了每种客户端的条目）。不要把令牌放进模板：Substrate 会存储模板和快照，而且它的容器环境只接受字面值。

然后创建 atespace、承载 agent 镜像的 actor 模板，以及基于该模板的 actor。Substrate 的 [API 指南](https://ate.dev/docs/reference/api-guide/)描述了模板的各个字段；这里模板的 `metadata` 将其命名为 atespace `research` 中的 `research-agent`。模板的 `workerSelector` 必须与集群中的某个 WorkerPool 匹配，其镜像必须按 digest 固定。创建这三项：

```bash
kubectl ate create atespace research
kubectl ate create actor-template -f template.yaml
kubectl ate create actor research-agent --template=research-agent -a research
```

<a id="4-allow-the-actor-to-reach-loom-with-its-token"></a>

## 4. 允许 actor 使用其令牌访问 Loom

一个 actor 只有一个出口策略。下面这个策略针对上文创建的 actor，允许它通过 HTTP 访问 Loom 的 Service，并将 agent 发送的 `Authorization` 标头替换为 Secret 中的 `Bearer <token>`：

```bash
kubectl ate create egress-policy research-agent -a research -f - <<'EOF'
rules:
- http:
    hostnames: ["loom.loom.svc.cluster.local"]
    ports:
      numbers: [7777]
    effects:
      replaceHeaders:
      - header: Authorization
        prefix: "Bearer "
        credentialUri: ate-secret://k8s.io/default/loom-tokens-research/research-agent/token
EOF
```

- 规则按请求的主机名匹配，因此要写 MCP 条目 URL 中的主机：`loom.loom.svc.cluster.local`。
- 网关只替换已存在的标头。不带 `Authorization` 标头的请求会原样发出，Loom 会拒绝它。
- Secret 不存在，或者 atespace 没有授权，网关都会拒绝该请求。
- 本指南中的 `http://` URL 和规则要求使用未设置 `tls.secretName` 的 chart，设置后该端口只应答 HTTPS。此时 chart 在集群内提供 HTTP 服务，因此按这条规则，令牌从网关到 Loom 这一跳是未加密的。若要加密这一跳，请让 agent 访问 Loom 前面的 TLS 端点，其证书需为公开受信任的证书，例如 chart 的 Ingress 主机，并用 `https` 规则允许该主机。网关随后会拦截 agent 的 TLS，因此 actor 需要挂载 Substrate 的[出口信任包](https://ate.dev/docs/guides/egress-trust-bundle/)，并让其 MCP 客户端使用它（按客户端的运行时，设置 `SSL_CERT_FILE`、`NODE_EXTRA_CA_CERTS` 或 `REQUESTS_CA_BUNDLE`），而 Loom 看到的连接来自 ingress 控制器，而不是 `atenet-egress`。

agent 需要的其他规则，例如访问其模型提供方，放在同一个策略中。`kubectl ate update egress-policy` 会替换所有规则，并拒绝元数据不是当前版本的策略，因此请先用 `kubectl ate get egress-policy research-agent -a research -o yaml` 读取策略，在 Loom 规则旁边添加规则，再用 `kubectl ate update egress-policy ... -f` 把该文件提交回去。

策略就绪后，启动 actor：

```bash
kubectl ate resume actor research-agent -a research
```

<a id="several-agents"></a>

## 多个 agent

- 同一个 Loom 命名空间中的 agent 会共享它们写入的所有内容。如果某个子 agent 需要读取团队的命名空间，但只能写入自己的命名空间，则应为它分配一个按命名空间指定角色的令牌（`--namespace-role team=viewer --namespace-role research=writer`；[创建令牌](https://github.com/ClickHouse/loom/blob/main/docs/guides/tokens-and-grants.md#mint-a-token)）。
- 每个执行者都有各自的令牌、Secret 和出站策略；位于另一个 atespace 中的执行者会获得该 atespace 自己的令牌命名空间和授权（[第 2 步](#2-a-token-per-agent)）。
- 使用[任务收件箱](https://github.com/ClickHouse/loom/blob/main/docs/features/tasks-and-decisions.md#task-inbox)在 agent 之间交接工作。

<a id="related"></a>

## 相关内容

- [Agent 框架与编排器](https://github.com/ClickHouse/loom/blob/main/docs/features/integrations/frameworks.md)，了解共享的 MCP 挂载。
- Substrate 的指南：[出口流量](https://ate.dev/docs/guides/egress-traffic/)和[出口凭据注入](https://ate.dev/docs/guides/egress-credential-injection/)。
