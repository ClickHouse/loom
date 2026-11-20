<!-- i18n: source=docs/operations/supported-versions.md blob=32631f7ef1ce0afc4871e89ec501bcad37732e46 model=claude-sonnet-5-5 -->
> 本页由机器从[英文原文](https://github.com/ClickHouse/loom/blob/main/docs/operations/supported-versions.md)翻译，内容以英文原文为准。
> Machine-translated from the [English page](https://github.com/ClickHouse/loom/blob/main/docs/operations/supported-versions.md), which is authoritative.

<a id="supported-versions"></a>

# 支持的版本

版本 N 和 N−1 会获得修复，`X.Y` 版本的 SDK 可与 `X.Y` 或 `X.(Y−1)` 版本的服务器配合使用。Loom 的服务器、两个镜像、Web 应用、两个 SDK、根目录 skills 以及 Helm chart 的 `appVersion` 使用同一个版本号（[发布流程](https://github.com/ClickHouse/loom/blob/main/docs/development/release-process.md)）。本页还介绍编码 agent 插件，以及移除功能的变更如何公告。

<a id="release-lines"></a>

## 发布线

| 发布线 | 获得的支持                                                                                      |
| ---- | ------------------------------------------------------------------------------------------------- |
| N    | 最新的次版本（`X.Y`）：所有修复；无法等待时以补丁版本（`X.Y.(Z+1)`）发布 |
| N−1  | 前一个次版本：安全修复，以及针对数据丢失或错误结果的修复，以补丁版本发布 |
| N−2  | 无：请升级到 N 或 N−1                                                                         |

次版本大约每两周从 `main` 发布一次；补丁版本来自该发布线的 `release/X.Y` 分支，修复须先合并到 `main`。升级请参照[升级](https://github.com/ClickHouse/loom/blob/main/docs/operations/upgrades.md)；在一个版本替换另一个版本的过程中，相邻的两个版本可以并行提供服务（[滚动升级](https://github.com/ClickHouse/loom/blob/main/docs/architecture/rolling-upgrades.md)）。

<a id="sdks-and-servers"></a>

## SDK 与服务器

SDK（PyPI 上的 `clickhouse-loom` 和 npm 上的 `@clickhouse/loom`）随每次发布以服务器的版本号发布；npm 的 `latest` 指向最高的正式版本，旧发布线的补丁版本则使用其 `release-X.Y` dist-tag。面对较旧的服务器时：

- SDK 发送了旧服务器不认识的字段，请求照常处理，并在响应的 `ignored_args` 中列出该字段；但对于会修改状态的 op，若服务器认为某个键是其某个字段的拼写错误（近似名称、同义词、字段名中的某个词），则以 `422 UNKNOWN_ARGUMENT` 拒绝，且不会写入任何内容（[未知字段和已移除字段](https://github.com/ClickHouse/loom/blob/main/docs/reference/http-api.md#unknown-and-removed-fields)）；
- 旧服务器不提供的操作会返回 `404`，SDK 将其抛出为 `NotFound`（Python 为 `loom.errors.NotFound`，TypeScript 为 `NotFound`）：对无法执行该操作的服务器，调用绝不会静默成功。同一个错误也表示行不存在，因此需要区分两者的客户端应在启动时读取 `ops.capabilities`（[了解部署提供的功能](https://github.com/ClickHouse/loom/blob/main/docs/reference/http-api.md#discovering-what-a-deployment-serves)）。

主版本（`X.0`）可能打破与上一个 `(X−1).Y` 服务器之间的这条规则；其发布说明会写明 `X.0` 版 SDK 的要求。

只要服务器仍提供旧版 SDK 所调用的内容，旧版 SDK 就能继续配合较新的服务器工作；移除功能遵循下文的规则。

<a id="coding-agent-plugins"></a>

## 编码 agent 插件

Claude Code 和 Codex 插件有各自的版本号，并标明其 hooks 支持的最旧服务器版本（`lib/server-version.mjs` 中的 `MIN_SERVER_VERSION`）。针对较旧服务器的会话开始时会输出一行 `loom:` 提示说明此事，插件会在可行的范围内继续工作（[Claude Code](../features/integrations/claude-code.md)）。Claude Code 插件需要 Claude Code 2.1.139 或更新版本，这是第一个不经 shell 运行 hook 的 `args` 的版本。

<a id="removing-something"></a>

## 移除功能

移除会在发布 pull request 中手动公告，写在移除该功能的前一个次版本的发布说明里，位于“What changed for clients”之下。在移除之前，服务器仍提供旧字段或 op；移除之后，已移除的请求字段仍会被接受并列入 `ignored_args`，因此发送它的客户端无需失败即可得知变更；已移除的 op 返回 `404`（`NotFound`），可通过 `ops.capabilities` 与行不存在的情况区分。

<a id="release-artifacts"></a>

## 发布产物

每个发布镜像都在其运行的架构上构建，经过针对已有修复的严重漏洞的扫描，并附带来源证明（provenance）和 SBOM 一同发布。每个镜像的多架构索引都带有来自发布工作流的免密钥签名；[容器镜像](https://github.com/ClickHouse/loom/blob/main/docs/operations/deployment.md#container-images)给出了 `cosign verify` 命令。GitHub Release 上的每个文件也以同样的方式签名，签名位于其旁边的 `<file>.sigstore.json` 中；[发布文件](https://github.com/ClickHouse/loom/blob/main/docs/operations/deployment.md#release-files)给出了 `cosign verify-blob` 命令。
