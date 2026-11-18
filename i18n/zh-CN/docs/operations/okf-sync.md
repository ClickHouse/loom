<!-- i18n: source=docs/operations/okf-sync.md blob=d6e5ec3b4a727da4861241c4b6c6b3efe03f98f2 model=claude-sonnet-5-5 -->
> 本页由机器从[英文原文](https://github.com/ClickHouse/loom/blob/main/docs/operations/okf-sync.md)翻译，内容以英文原文为准。
> Machine-translated from the [English page](https://github.com/ClickHouse/loom/blob/main/docs/operations/okf-sync.md), which is authoritative.

<a id="copying-a-namespace-to-another-loom"></a>

# 将命名空间复制到另一个 Loom

在私有网络内运行的作业会按计划把一个命名空间从私有 Loom 复制到公共 Loom：`loom export-okf` 从私有 Loom 读取数据，`loom import-okf --prune` 把数据写入公共 Loom。公共一侧不会获得通往私有 Loom 的路由或令牌。`deploy/okf-sync/cronjob.yaml` 是 Kubernetes 示例：一个每 15 分钟运行一次的 CronJob，以及它的 ServiceAccount 和 NetworkPolicy。

```bash
LOOM_API_KEY="$PRIVATE_VIEWER" loom export-okf --server-url "$PRIVATE_URL" \
  --namespace shared-lessons --no-evidence --out /tmp/bundle
LOOM_API_KEY="$PUBLIC_WRITER" loom import-okf /tmp/bundle --server-url "$PUBLIC_URL" \
  --namespace shared-lessons --agent okf-sync --prune
```

<a id="what-a-run-writes"></a>

## 一次运行会写入什么

- **没有变化时不写入任何内容。** 导入会先读取其身份下的有效行，如果某个概念对应的行已经具有相同的 value、type、tags、confidence、`valid_at`、`valid_to`、`source_trace` 和 `source_class`（声明的类别，否则为 `imported`），以及 bundle 携带的实体，就会跳过该概念。`memory.set` 自行添加的标签（`sensitivity:`、`attribute:`）不算差异。已经生效的边也会被跳过。当 bundle 中有多个概念共用同一个 key（来自不同写入者的行）时，写入的是最近更新的那一个。重复发送一模一样的行会被算作一次强化：每次运行都会产生一条版本行、一条审计行，并提高排序权重。带有 `sources:` 的概念是例外：每次运行都会写入，因为导入所做的读取不会返回它所链接的来源。目标端将其合并到另一个 key 的行中的概念也是如此：每次运行都会再次发送它并强化那一行，最后一行会将其计为已合并。
- **每个新增或变更的概念对应一次 `memory.set`**，与其他写入一样要经过冲突、黑名单和审计检查，并以该令牌作为操作者。源端标记为 `derived` 或 `external` 的行保留该类别；其他所有行都存为 `imported`，由服务器根据 `okf:` trace 推导得出。两侧的 org 和命名空间相同时，副本不带 `origin:` 标签。
- **每个被清理的行对应一次 `memory.delete_version`。** `--prune` 只删除满足以下条件的行：位于该导入的 `--agent` 之下，由其令牌写入，`source_trace` 以 `okf:` 开头，并且其 key 已不在 bundle 中。其他人在该命名空间中写入的行会保留。导入到同一命名空间的每个 bundle 都应使用各自的 `--agent`（没有它，`--prune` 会拒绝执行）。删除是一个墓碑标记：`memory.restore` 可在 14 天的期限内恢复该行。

最后一行会报告各项计数：
`500 rows from the bundle into acme/shared-lessons: 0 written, 500 unchanged, 0 failed …,
0 pruned`。
退出码 1 表示无法读取目标，或者某次写入、链接、删除、证据批次或 `--verify` 失败；退出码 2 表示导入在发出任何请求之前就被拒绝：原因可能是 lint 发现问题、触发了 prune 保护、路径不是目录，或者缺少 `--agent`、令牌或 org。

<a id="guards"></a>

## 防护机制

- `--prune` 会拒绝没有任何概念的 bundle，也会拒绝概念文件的哈希与导出时写入 `index.md` 的 `loom.corpus_digest` 不一致的 bundle：被编辑过的 bundle，或导出到一个原本存放着旧 bundle 的目录中的 bundle，都不会触发删除。如果 `index.md` 不带 `loom.complete: true`，该 bundle 会被写入但不会被清理，最后一行输出会说明这一点。导出在以下情况下不写这个标记，并会打印具体原因：在 `--limit` 处停止、使用了 `--include-tombstoned`（此类 bundle 中会保留已删除的行），或源在读取期间发生了变化（分页时有写入，或某一行在完整读取之前被删除）；旧版本写出的 bundle 没有这个标记。
- 导出会把源中被取代的行和重复的行以 `status: deprecated` 带上，因此源中被合并到其他键的键会保留在副本中，而不会被清理。
- 导出按偏移量对源分页，因此在列出过程中对源的写入可能使分页发生偏移，从而漏掉一个有效行。分页结束后，它会读取 `memory.keys`，即用一条语句取得每个有效行的标识和键（命名空间在某一时刻的状态），只有当这些行都已被列出时，才认为列表是完整的。在该时刻之后写入的行属于之后的变更，由下一次运行带上。它最多总共列出三次，否则不写 `loom.complete`，这样该次运行只复制而不清理，之后一次更安静的运行才会清理。如果源太旧，无法响应 `memory.keys`，或者有效行超过 100,000 行，则永远不会被标记为完整。
- 每次删除之前，它会按该行自身的标识重新读取这一行，只有在满足以下条件时才继续：该行属于它的 `--agent`，由它自己的令牌写入（仅有 agent 名称不够，因为它由调用方自行选择），仍是导入时列出的、带有 `okf:` 追踪的那个版本，并且没有 `memory.lock`。其他任何情况都计为保留；而此时已不存在或已属于其他 agent 的行不计入。它使用 `memory.delete_version` 删除，该操作只删除该版本，且只针对该确切标识，绝不会删除其他 agent 的行。如果删除生效时恰有对该键的 `memory.set` 或 `memory.bulk_set` 正在进行，该行会被保留；例外情况见[记忆架构](https://github.com/ClickHouse/loom/blob/main/docs/architecture/memory-architecture.md#lifecycle)。
- 目标副本如果太旧，不认识 `memory.delete_version`，会返回 404 且不删除任何内容；清理会统计这次失败，导入以退出码 1 结束。任一 Loom 的滚动升级都无需暂停。
- 只有当导出以 0 退出（`sh -e`）时才会运行导入，并且每次运行都把 bundle 写入新的 `emptyDir`，因此之前某次运行留下的 bundle 不会被导入。
- `--dry-run` 在通过相同检查之后，列出将要写入和删除的内容，但不写入任何内容。
- `concurrencyPolicy: Forbid` 避免两次运行交错执行。

一次运行可清理的行数没有上限。源命名空间如果有意丢失了大部分行，会在下一次运行时自动传播，无需运维人员介入。如果丢失了全部行，则不会传播：导入会拒绝空 bundle，作业在每次运行时都会失败。此时应暂停 CronJob（`kubectl patch cronjob okf-sync -p '{"spec":{"suspend":true}}'`），并用 `memory.delete` 删除公共 Loom 上的副本；如果内容必须彻底清除，则通过擦除来处理。

被误清理的键，会在下一次其 bundle 中包含该键的运行中作为新行重新写入：它的 `memory_id` 会改变。`memory.restore` 则会恢复原来的那一行。

<a id="what-a-run-costs"></a>

## 一次运行的开销

如果一次运行没有发现需要更改的内容，则只会发送读取请求。对每一行 `sensitivity: high` 的记录按 key 读取都会被审计（`memory.sensitive_read`），因此当高敏感度行属于导入所用的身份时，即使是一次没有变更的运行，在按 key 读取这些行的每一侧，每个高敏感度行仍会新增一条审计记录。对于包含 N 行的命名空间：

| 一侧                 | 请求                                                                                                                                                                                                                                                                                                                              |
| -------------------- | --------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| 导出，私有 Loom      | 每页 100 行的 `memory.list` 分页（`LOOM_MEMORY_LIST_VIEWER_LIMIT` 限制 viewer 令牌的页大小），一次 `memory.keys`，一次用于获取构建提交的 `GET /v1/health`，对每个（agent、user、session）每 500 行一次 `memory.bulk_get`，以及每 2,000 条类型化边一页 `memory.export_evidence`                                                    |
| 导入，公共 Loom      | 每页 1,000 行的 `memory.list` 分页，每 500 行一次 `memory.bulk_get`，以及再次读取的边分页；然后对每个变更的行一次 `memory.set`，对每条新边一次 `memory.link`，对每个待清理候选项（带有 `okf:` 痕迹且其 key 已不在包中的行，无论是否保留）一次 `memory.bulk_get`，以及对每个被清理的行一次 `memory.delete_version` |

一个包含 501 行、只有一个写入者的命名空间，总共需要 15 次请求（导出侧：6 次 `memory.list` 调用、1 次 `memory.keys`、1 次 health 读取、2 次 `memory.bulk_get` 和 1 页边；导入侧：1、2 和 1）。在同一身份下有 50,000 行时，一次运行约 750 次请求，在 15 分钟的调度周期内平均每秒不到一次；如果命名空间分散在许多 session 中（如 hook 写入的情形），则改为每个 session 一次 `memory.bulk_get`，并且当源在分页期间发生变化时，导出最多总共会列出源三次。

<a id="tokens"></a>

## 令牌

该作业持有两个令牌，每个令牌的作用范围仅限于这一个命名空间：

```bash
# On the private Loom: read the namespace, nothing else.
loom mint-token --subject okf-sync --org <org> --namespace-role shared-lessons=viewer \
  --ttl-days 90 --save ./private-viewer.jwt
# On the public Loom: write the namespace, nothing else.
loom mint-token --subject okf-sync --org <org> --namespace-role shared-lessons=writer \
  --ttl-days 90 --save ./public-writer.jwt
```

将它们存入 CronJob 读取的 Secret：

```bash
kubectl create secret generic okf-sync \
  --from-file=private-viewer=./private-viewer.jwt \
  --from-file=public-writer=./public-writer.jwt
```

请在 TTL 到期之前重新签发令牌，使用相同的 `--subject` 和类型（默认为服务账号），并替换该 Secret。Prune 只删除其令牌对应的 actor 写入的行（服务账号为 `process:<subject>`，用户令牌为 `human:<subject>`），因此使用其他 subject 或类型的令牌会保留之前的副本（最后一行会将它们计为保留）；`loom revoke-token` 可用于停用已泄露的令牌。在关闭了身份验证的服务器上，没有任何行带有 actor，因此 prune 不会删除任何内容，并将所有候选行都计为保留。

- 在公共 Loom 上，该作业的令牌是此命名空间唯一的写入者。它的读取者（公共 CI 中的 bot）持有 `viewer` 令牌。
- 在私有 Loom 上，该命名空间的写入者是向其发布内容的人员和 agent。读取该命名空间的其他所有主体都持有 `viewer`，因此不会有写入者未打算发布的内容进入该命名空间。

<a id="the-copied-namespace-is-published"></a>

## 复制的命名空间会被发布

命名空间中的所有内容会在一个调度间隔内到达公共 Loom。请把私有侧对该命名空间的写入视为发布：机密内容应放在其他命名空间中。标记为 `sensitivity: high` 的行会保留在私有侧（除非使用 `--include-high`，否则导出会跳过它们）；某一行变为 `high` 后，会在下一次运行时从公共侧清除，但如果它是导出本会携带的最后一行，则不会清除：此时数据包中没有任何概念，导入会拒绝它，公共侧的副本得以保留。请在公共 Loom 上使用 `memory.delete` 删除它们，内容必须彻底移除时则通过擦除来处理。导出通过 `memory.bulk_get` 读取的 `sensitivity` 来识别 `high` 行，也可以通过 `memory.set` 和 `memory.update` 添加的 `sensitivity:high` 标签来识别。如果私有 Loom 的 `memory.bulk_get` 不返回 `sensitivity`，则所有行都会留在本地（导出会将它们计入“no known sensitivity”），因此数据包中没有任何概念，`--prune` 会拒绝它。清除操作是一个墓碑标记：该值在 14 天内仍可恢复，并保留在公共 Loom 的版本历史中。必须从公共 Loom 中移除的内容，请通过[擦除](https://github.com/ClickHouse/loom/blob/main/docs/features/memory/retention-and-erasure.md)处理。

<a id="network"></a>

## 网络

NetworkPolicy 不允许任何入站流量。出站流量仅限于 DNS、私有 Loom 的 web pod（端口 7777）以及公共 Loom 的地址（端口 443）。NetworkPolicy 匹配的是地址，而不是主机名：请在 `ipBlock` 中列出公共 Loom 的地址；如果这些地址会变化，请使用 CNI 的主机名策略或出站代理。

当私有 release 设置了 `networkPolicy.enabled` 和 `ingressFrom` 时，请将该作业的 pod（`app.kubernetes.io/name: okf-sync`）或其命名空间添加到 `ingressFrom` 中。否则该 release 自身的策略会拒绝这次导出。

<a id="related"></a>

## 相关内容

- [OKF 数据包](../features/integrations/okf.md)：数据包格式与导入。
- [Kubernetes](https://github.com/ClickHouse/loom/blob/main/docs/operations/kubernetes.md)：CronJob 所沿用其 Pod 安全设置的 chart。
