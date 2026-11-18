<!-- i18n: source=docs/reference/cli.md blob=ff91f4468115d6d9b0cb51b887479b6922e69b72 model=claude-sonnet-5-5 -->
> 本页由机器从[英文原文](https://github.com/ClickHouse/loom/blob/main/docs/reference/cli.md)翻译，内容以英文原文为准。
> Machine-translated from the [English page](https://github.com/ClickHouse/loom/blob/main/docs/reference/cli.md), which is authoritative.

<a id="cli-reference"></a>

# CLI 参考

`loom` 是用于运行、维护和连接 Loom 部署的唯一命令行入口。它是服务端软件包的 `loom` 控制台脚本（`pyproject.toml`，`[project.scripts]` → `server.cli:main`）；解析器树位于 `server/cli/app.py`，每个命令族在 `server/cli/` 下对应一个模块。本页末尾的表格由该解析器树生成。

<a id="where-it-runs"></a>

## 运行位置

| 位置                                               | 方式                                                               | 适用场景                                                                                               |
| -------------------------------------------------- | ------------------------------------------------------------------ | ------------------------------------------------------------------------------------------------------ |
| 服务器镜像内                                       | `docker exec loom-server loom <command>`（web 容器）               | 使用部署自身设置的运维命令：`doctor`、`migrate`、`mint-token`、`reembed`。                             |
| 拆分部署的 workers 容器内                          | `docker exec loom-workers loom <command>`                          | 希望从 web 层分离出去的长时间维护任务：`code backfill`、`code stats`。                                 |
| 代码检出目录                                       | 在仓库根目录执行 `pip install -e .`，然后执行 `loom <command>`     | `up`、`install`、`demo`，以及针对本地环境的开发。                                                      |

在容器内，设置就是容器的环境变量。在代码检出目录中，`loom` 会先把工作目录下的 `.env`（或 `LOOM_ENV_FILE` 指定的文件；为空表示不加载）加载到环境中，方式与 compose 的 `env_file` 相同，且不会覆盖你的 shell 已导出的变量。这对提供商密钥很重要：在检出目录中运行嵌入命令时，如果没有这些密钥，会回退到 fake embedder，并写入不可用的向量。

命令分为两类：

- **运维命令**读取 `server/settings/` 并直接与 ClickHouse 通信（`migrate`、`reembed`、`code backfill`、`revoke-token`、各类回填和修复命令）。它们使用部署的凭据运行，因此请在这些凭据所在的位置运行。
- **客户端命令**通过 HTTP，使用 URL 和令牌与运行中的服务器通信（`install`、`init`、`demo`、`memory-search`、`ingest-transcript`、`export-okf`、`import-okf`）。它们接受 `--server-url`（`demo` 为 `--base-url`），并且除 `init` 外还接受 `--token`，解析顺序与插件钩子相同：URL 依次取 `LOOM_BASE_URL`、`~/.loom/config.json` 的 `url`，否则为 `http://localhost:7777`；令牌依次取 `LOOM_API_KEY`、`~/.loom/config.json` 的 `token`、`~/.loom/token`（可由 `LOOM_API_KEY_FILE` 改变位置）。`LOOM_CONFIG_FILE` 可改变配置文件的位置。对于 agent（带有工作负载令牌的 `LOOM_CLIENT_ID`），它们只使用 `--token` 或 `LOOM_API_KEY`，从不使用已保存的令牌，没有则拒绝执行（退出码 2）；无论 `--token` 如何设置，`loom install` 都会拒绝，`loom init` 会在注册任何内容之前拒绝，而 `loom call` 会自行交换工作负载令牌。

`loom mint-token` 只需要 `LOOM_JWT_SECRET`：它在本地签发令牌。

**会话命令**（`login`、`logout`、`whoami`、`call`、`token`）属于 Python SDK 自带的命令（`loom.cli`；仅安装 SDK 时可用 `python -m loom …`），因此 `loom` 需要执行 `pip install clickhouse-loom` 才能使用它们。`loom login [--server URL] [--profile NAME] [--no-browser] [--org ORG [--wait S]]` 使用一次性代码登录，使用 `--org` 时登录到该组织所在的[单元（cell）](https://github.com/ClickHouse/loom/blob/main/docs/architecture/cells.md)，由路由器的查找结果指定；`loom call <op|/path> [key=text|key:=json …] [--data JSON] [--org] [--namespace]` 使用该会话访问任意操作或路由；`loom token create|list|revoke` 用于创建、列出和撤销你自己的令牌，供 CI 和 agent 使用。参见[不使用控制台来使用 Loom](https://github.com/ClickHouse/loom/blob/main/docs/guides/without-the-dashboard.md)。

<a id="conventions"></a>

## 约定

**重写类命令默认为试运行（dry run）。** 重写已存储行的命令，除非给出 `--apply`，否则只报告将要写入的内容；`--dry-run` 同样可用，它指明的就是默认行为。这两个标志互斥（`server/cli/_apply.py`，`add_apply_flags`）。试运行成功后，stderr 上会输出 `dry run: nothing was written; re-run with --apply to write`，因此命令的 `--json` 输出在 stdout 上保持独立。有些命令用自己的标志表达同样的意思（`loom entity-edges-invalidate-junk` 接受 `--dry-run`（默认）或 `--apply`；`loom reembed --dry-run` 只统计行数而不写入）；下表列出了每个命令的标志。

**每次写入都会标明其主体。** 命令的写入会被标记为 actor `process:cli:<command>`（`server/memory/domain/provenance.py`，`cli_actor`），因此来源记录和审计日志都能说明是哪个命令写入了某一行。

**每次查询都有归属。** 命令发出的 ClickHouse 查询带有 `log_comment` 标签 `cli.<command>[.<subcommand>]`，其读取在 `background` 流量类别中运行，除非该命令自带 `--traffic-class` 参数。`loom serve` 是例外：它本身就是服务器，其请求和 worker 轮次会自行打标签。

**`--json`**：提供该选项的命令会在 stdout 上输出一份机器可读的结果，并将面向人类的报告保留在 stderr 上。

<a id="common-commands"></a>

## 常用命令

<a id="run-and-check-a-deployment"></a>

### 运行并检查部署

```bash
loom up                          # from a checkout: create .env, boot Loom, its extractor and ClickHouse, print how to connect
docker exec loom-server loom doctor          # pre-boot checks + the /v1/health problem list, each with its fix
docker exec loom-server loom doctor --json   # the same, machine-readable
loom doctor --client             # also examine this machine as a coding-agent client
docker exec loom-server loom migrate --status              # applied and pending migrations
docker exec loom-server loom migrate --dry-run --strict    # what a boot would apply; exit 1 on ledger drift
```

`loom up` 会等待 `/v1/health`（最长 `--timeout`，默认 300 秒），在启用认证时签发一个 writer 令牌，并打印控制台 URL 和首次调用的示例代码；`--no-build` 会复用缓存的镜像，已经能响应健康检查的栈不会被改动，除非传入 `--rebuild`。只要有任何检查失败，`loom doctor` 就以退出码 1 退出。`loom serve` 在 uvicorn 下运行控制平面，供本地使用（镜像通过 `scripts/serve.sh` 启动它）。迁移和升级见[升级](https://github.com/ClickHouse/loom/blob/main/docs/operations/upgrades.md)；健康检查见[监控](https://github.com/ClickHouse/loom/blob/main/docs/operations/monitoring.md)。

<a id="tokens"></a>

### 令牌

```bash
# A writer token for one namespace, valid 30 days, printed on stdout
docker exec loom-server loom mint-token --subject my-bot --org dev \
  --namespaces local --roles writer --ttl-days 30

# A scoped token: per-namespace roles, in read-priority order
loom mint-token --subject triage --org acme \
  --namespace-role priv=admin --namespace-role pub=viewer --json

# Retire one token, or every token of a subject issued before an instant
docker exec loom-server loom revoke-token --jti <jti> --reason "leaked"
docker exec loom-server loom revoke-token --subject my-bot --issued-before 2030-01-01T00:00:00Z
```

永久吊销不能再设置过期时间，也不能收窄为恢复在它之前已被停用的令牌。请选择不早于其现有截止时间的截止时间，或选择永久吊销时间加上令牌时钟偏差容限。

`--save` 会把令牌写入 `~/.loom/token`（或你指定的路径），而不是打印出来。不带 `--ttl-days` 时，令牌没有过期时间，只能通过 `loom revoke-token` 停用。`--read-scope own_user` 将读取范围限制为每个请求所指定的 `user_id`；`--consumer NAME` 将[consumer 标签](https://github.com/ClickHouse/loom/blob/main/docs/reference/http-api.md#the-consumer-tag)绑定到该凭据。参见[令牌与授权](https://github.com/ClickHouse/loom/blob/main/docs/guides/tokens-and-grants.md)。

<a id="connect-a-client"></a>

### 连接客户端

```bash
loom install --client cursor --token "$LOOM_API_KEY" --server-url https://loom.example.com
loom install --client claude-code --toolset coding+tracker --dry-run
loom init --agent triage-bot --server-url https://loom.example.com   # self-signup: a namespace + token
```

`loom install` 会把 MCP server 条目写入客户端的配置（或运行 `claude mcp add` / `codex mcp add`）；不带 `--client` 时，它会接入找到的所有受支持客户端。参见 [MCP 客户端](https://github.com/ClickHouse/loom/blob/main/docs/features/integrations/mcp-clients.md)。`loom init` 把令牌保存到 `~/.loom/token`，并把 server、org 和命名空间保存到 `~/.loom/config.json`，客户端命令和插件 hook 都会读取这个文件。

<a id="re-embed-after-changing-the-embedder"></a>

### 更换 embedder 后重新嵌入

```bash
docker exec loom-server loom reembed --dry-run          # count rows whose embedding_model is not the configured one
docker exec loom-server loom reembed --namespace support
docker exec loom-server loom reembed --delete-old        # re-embed, then delete the off-model rows
```

Loom 从不自行重新嵌入：修改 `LOOM_EMBEDDING_MODEL` 后，请运行 `loom reembed`。它会根据已存储的内容重新生成向量，并保留其他所有列。`--in-database <named collection>` 使用 `aiEmbed`（需要 ClickHouse 26.7 或更高版本）在 ClickHouse 内部完成嵌入，每张表对应一条 `INSERT … SELECT`；当设置了客户或身份 header，或要求使用推理网关时，该选项会被拒绝；`--resume` 会从上一次运行的 `resume:` 行所指明的那一行之后继续。参见[维护](https://github.com/ClickHouse/loom/blob/main/docs/operations/maintenance.md)。

<a id="index-a-repository"></a>

### 为仓库建立索引

```bash
docker exec loom-workers loom code init --org acme --namespace code-myrepo
docker exec loom-workers loom code backfill --org acme --namespace code-myrepo --embed
docker exec loom-workers loom code stats --org acme --namespace code-myrepo
```

`code backfill` 可以恢复继续。`--phase index` 负责解析、分块并推导符号和边；`--phase embed` 负责填充待处理的向量；`--embed` 在一次运行中完成两者。除非传入 `--allow-fake-embedder`（仅用于开发），否则它会拒绝使用 fake embedder 进行嵌入。`--org` 和 `--namespace` 默认取自 `LOOM_CODE_ORG` 和 `LOOM_CODE_NAMESPACE`。完整流程、编译器层级以及 PR 历史回填见[代码索引运维](https://github.com/ClickHouse/loom/blob/main/docs/operations/code-index-operations.md)。

<a id="export-and-import"></a>

### 导出与导入

```bash
loom export-okf --namespace support --out ./support-okf            # a namespace as a Markdown bundle
loom okf-lint ./support-okf --strict                               # offline lint; exit 2 on a critical finding or, with --strict, a warning
loom import-okf ./support-okf --namespace support-copy
loom export-actions --format cedar -o loom.cedarschema             # the registry as a policy schema
```

`export-okf` 默认不导出 `sensitivity=high` 或敏感度未知的行，除非传入 `--include-high`，因为镜像仓库面向的受众比该命名空间的授权范围更广。与此类行关联的原始分块和摘要，以及包含 high 形态内容（凭据、银行卡号、SSN 或明示的 PIN）的原始文本，也会随该行一同留在本地；`--include-high` 会放行它们，服务器会对每个被放行的条目进行审计。参见 [OKF](../features/integrations/okf.md) 和[导出 action schema](https://github.com/ClickHouse/loom/blob/main/docs/reference/mcp.md#exporting-the-action-schema)。

<a id="diagnose-and-repair"></a>

### 诊断与修复

`loom report` 会把 `loom doctor` 的输出填入 bug 表单的 Environment 字段；`--open` 会打开已填好版本信息的表单。`loom drops` 会打印某个命名空间的记忆摄入丢弃台账。`loom erasure-re-verify` 会重新检查失败的擦除 saga。回填与修复命令（`integrity-repair`、`reconcile-status`、`repair-value-bytes`、`entity-edges-invalidate-junk`、`rechunk`、`lexical-stats-rebuild` 等）及其适用时机，见[维护](https://github.com/ClickHouse/loom/blob/main/docs/operations/maintenance.md)和[故障排查](https://github.com/ClickHouse/loom/blob/main/docs/operations/troubleshooting.md)。

<a id="reading-the-table"></a>

## 阅读表格

每一行对应一个命令或子命令：其完整调用方式、帮助文本的第一句话，以及它的参数。位置参数显示为 `<name>`；选项以其长格式显示。`loom <command> --help` 会打印完整文本、默认值和可选值。生成器（`scripts/codegen/docs_reference.py`，`render_cli`）读取已提交的解析器快照 `tests/unit/snapshots/cli_tree.json`。`tests/unit/test_cli_surface_snapshot.py` 使该快照和 `tests/unit/snapshots/cli_help.json` 与实际解析器保持一致，因此新增标志会导致 CI 失败，直到两个快照和本页面都重新生成：

```bash
LOOM_UPDATE_CLI_SNAPSHOT=1 python -m pytest tests/unit/test_cli_surface_snapshot.py
python scripts/codegen/docs_reference.py
```

<a id="commands"></a>

## 命令

<!-- BEGIN GENERATED: cli -->
<!-- Generated by `python scripts/codegen/docs_reference.py`; edit the source, not this region. -->
<!-- prettier-ignore-start -->

| 命令 | 摘要 | 参数与标志 |
| --- | --- | --- |
| `loom code` | 代码智能平面操作。 |  |
| `loom code init` | 创建 bare clone 并写入初始状态。 | `--org`, `--namespace` |
| `loom code backfill` | 全树索引（可恢复）。 | `--org`, `--namespace`, `--phase`, `--embed`, `--include-poisoned`, `--allow-fake-embedder`, `--no-fetch`, `--limit`, `--commit`, `--paths-file`, `--concurrency`, `--edges-second-pass`, `--no-edges-second-pass`, `--dialect-folder-pass`, `--embedding-model-suffix`, `--context-callees`, `--callers-from` |
| `loom code reconcile` | 基于内容的过期检查：找出其 chunk 早于分支 head 处最后一次变更的路径；--heal 会重新摄取这些路径（水位线不变）。 | `--org`, `--namespace`, `--heal`, `--limit`, `--max-commits`, `--no-fetch`, `--show` |
| `loom code compact` | 当代码向量表每个键的物理副本数达到 --min-ratio 时，对其执行 OPTIMIZE ... FINAL，并等待合并完成。 | `--table`, `--min-ratio`, `--apply`, `--dry-run`, `--timeout-s` |
| `loom code reembed` | 将命名空间的现有 chunk 以第二个 embedding 模型的标签写入：在数据库内完成、可恢复、无需 checkout。 | `--org`, `--namespace`, `--embedding-model`, `--source-model`, `--prefix`, `--batch` |
| `loom code vector-twin` | 在 ClickHouse 内填充命名空间中 chunk 向量的 Matryoshka 前缀副本：无需重新 embed，可恢复。 | `--org`, `--namespace`, `--embedding-model`, `--all-namespaces` |
| `loom code crossref` | SQL/shell 测试 -> 实现它的 C++ 引用扫描（写入 code_test_map method=content_reference；幂等）。 | `--org`, `--namespace` |
| `loom code coverage-import` | 导入所配置来源中最新的按测试统计的行覆盖率运行结果（写入 code_test_map method=coverage 和 code_coverage_files）。 | `--org`, `--namespace`, `--dry-run` |
| `loom code export-pairs` | 从真实流量（trace x outcome 联接）导出（查询 -> 被采用的单元，困难负例）检索数据对，用于 embedder/reranker 微调。 | `--org`, `--namespace`, `--days`, `--out` |
| `loom code backfill-prs` | 来自 GitHub 的已合并 PR 历史。 | `--org`, `--namespace`, `--months`, `--max-prs` |
| `loom code repack` | 按当前 pack 形态为最近合并的 PR 重新生成上下文 pack。 | `--org`, `--namespace`, `--days`, `--limit`, `--dry-run` |
| `loom code describe` | 为 PageRank 最高的 chunk 生成由 LLM 撰写的上下文单行描述。 | `--org`, `--namespace`, `--top-pct`, `--limit`, `--suffix`, `--concurrency` |
| `loom code answers-purge` | 将缓存的 code.ask 答案标记为墓碑（合成模型升级后，否则较弱模型的过期答案会一直被返回，因为缓存只会在所引用文件变化时自行失效）。 | `--llm-model`, `--all`, `--apply`, `--dry-run`, `--org`, `--namespace` |
| `loom code scip-full` | 手动执行完整的编译器层级重新索引（code_scip_full worker 默认每周运行一次：修复逐次合并增量中因 TU 上限导致的降级以及任何漂移）。 | `--checkout`, `--jobs`, `--keep-delta`, `--apply-delta`, `--org`, `--namespace` |
| `loom code scip-lock-path` | 打印该条目在本主机上的编译器层级锁文件路径。 | `--org`, `--namespace` |
| `loom code entry-lock` | 在该条目的编译器层级锁（本主机的文件和部署的租约）下运行命令：另一个流程持有锁时退出码为 75，运行中租约丢失时为 3（命令被停止）。 | `--org`, `--namespace`, `--tier`, `<command>` |
| `loom code scip-delta` | 手动执行一次编译器层级增量流程（与 code_scip_delta worker 使用相同代码）：对每个已配置仓库比较 git_head 与 scip_commit，对变更的 TU 运行 scip-clang，然后应用。 |  |
| `loom code entries` | 每个已配置仓库一行：org namespace checkout lock-file（已解析的锁路径；手动运行通过 `loom code entry-lock` 获取该锁）。 |  |
| `loom code warm` | 根据问题列表预先填充 code.ask 答案缓存（每行一个问题，或带 'question' 字段的 JSONL）。 | `<file>`, `--org`, `--namespace` |
| `loom code scip-retry` | 将失败的 SCIP 批次重置为 pending。 | `--org`, `--namespace`, `<batch_id>` |
| `loom code backfill-reviews` | 为 code_pr_history 中已有但尚未存储行内评审评论的已合并 PR 补充这些评论（从新到旧，每个 PR 一次 GitHub 请求）。 | `--org`, `--namespace`, `--prs` |
| `loom code backfill-assignees` | 为已存储的已合并 PR 填充 code_pr_history.assignees（仓库指派的评审人），从最新的已关闭 PR 开始遍历，每次请求 100 个。 | `--org`, `--namespace`, `--pages` |
| `loom code backfill-items` | 填充自某日期以来有更新的 code_gh_items（issue 和 PR，任意状态），每次请求 100 条，按最早更新优先；会移动 gh_items_cursor。 | `--org`, `--namespace`, `--since`, `--pages` |
| `loom code stats` | 水位线与现有行数。 | `--org`, `--namespace` |
| `loom code export-matrix` | 根据已合并 PR 的共同变更生成维护矩阵：“当此文件变更时，这些文件通常也会变更”，附带证据，输出为适用于说明文件（AGENTS.md / copilot-instructions.md）的 markdown。 | `--org`, `--namespace`, `--prs`, `--min-together`, `--min-conf`, `--max-partners`, `--max-rows`, `--out`, `--json` |
| `loom code lint-instructions` | 检查 agent 说明文件（CLAUDE.md、AGENTS.md、copilot-instructions、.claude/.md、skills）中，路径或符号引用在 clone 的 head 处已无法解析的条目。 | `--org`, `--namespace`, `--file`, `--glob`, `--symbols`, `--all`, `--json`, `--fail-on-stale` |
| `loom decisions` | 从文档中挖掘出的决策记录。 |  |
| `loom decisions extract` | 将一份知识文档挖掘为决策记录：模型整体阅读文档（超过 LOOM_MEMORY_DECISION_EXTRACT_WINDOW_CHARS 时按完整章节的窗口阅读；仅含图片的标题和… | `--org`, `--namespace`, `--source-uri`, `--repo`, `--into`, `--max-per-section`, `--window-chars`, `--apply`, `--dry-run` |
| `loom knowledge` | 知识文档及其来源。 |  |
| `loom knowledge sync` | 立即对仓库命名空间的文档来源执行一次同步：列出每个来源中变更的文件（使用 --full 则列出全部文件），将变更的文件写为文档，并将完整列表中已不存在的文档退役… | `--org`, `--namespace`, `--source`, `--full` |
| `loom knowledge source` | 命名空间的文档来源，通过运行中的服务器操作。 |  |
| `loom knowledge source list` | 列出命名空间的来源、条目和同步状态。 | `--namespace`, `--org`, `--server-url`, `--token` |
| `loom knowledge source add` | 根据链接或条目注册来源，存储前先用凭据探测（knowledge.source.add）。 | `--namespace`, `--org`, `--server-url`, `--token`, `--name`, `--kind`, `--link`, `--item-json`, `--include`, `--credential-file`, `--dry-run` |
| `loom knowledge source update` | 添加和移除来源的条目或更改其文件类型，新增条目用已存储的凭据探测（knowledge.source.update）。 | `--namespace`, `--org`, `--server-url`, `--token`, `--name`, `--link`, `--item-json`, `--include`, `--remove-link`, `--remove-item-json` |
| `loom knowledge migrate-originals` | 将仍保存在 ClickHouse 中、文档、上传或摄取所需的文档原件迁移到 LOOM_DOCUMENT_S3_BUCKET：每个原件复制为独立对象，经哈希校验后切换，并由…保护 | `--org`, `--namespace`, `--dry-run` |
| `loom export-okf` | 将命名空间渲染为 OKF v0.2 bundle（Markdown + frontmatter），即 Loom 记忆可用 git 评审的投影。 | `--server-url`, `--token`, `--org`, `--namespace`, `--out`, `--title`, `--include-tombstoned`, `--limit`, `--dry-run`, `--no-evidence`, `--no-links`, `--include-high` |
| `loom import-okf` | 通过 memory.set 将 OKF v0.2 bundle 的概念写入命名空间。 | `<path>`, `--server-url`, `--token`, `--org`, `--namespace`, `--agent`, `--user-id`, `--session-id`, `--keep-sessions`, `--verify`, `--dry-run`, `--no-evidence`, `--no-lint`, `--force-import`, `--no-links`, `--prune` |
| `loom okf-lint` | 离线检查 OKF bundle 目录，即镜像仓库 CI 运行的编译关卡。 | `<path>`, `--json`, `--strict` |
| `loom up` | 启动本地栈（Loom、其文档提取器和 ClickHouse）并打印连接方式：从模板创建 .env 和 ClickHouse 配置，运行 docker compose up --build，等待… | `--root`, `--file`, `--env-file`, `--no-build`, `--rebuild`, `--port`, `--timeout`, `--subject`, `--org`, `--namespace`, `--open` |
| `loom down` | 停止本地栈并保留数据；--delete-data 会在确认后执行重置。 | `--root`, `--file`, `--env-file`, `--delete-data`, `-y` |
| `loom ch-setup` | 为 Loom 准备自托管 ClickHouse 或 ClickHouse Cloud 服务：创建 loom 数据库和只读 reader 用户，或打印供管理员执行的 SQL。 | `--print-sql`, `--print-analyst-sql` |
| `loom participant` | 提供方变更所等待的 Loom 进程。 |  |
| `loom participant retire` | 退役一个未正常关闭就已死亡的进程的提供方参与者（其 id 是该进程 /v1/health 中的 `participant_id`）。 | `--participant`, `--yes` |
| `loom deployment` | 每个副本用来自检的配置。 |  |
| `loom deployment publish` | 将此环境的配置发布为部署配置，有意替换其根密钥或 LOOM_EMBEDDING_MODEL/DIM。 | `--yes`, `--check` |
| `loom settings` | 每个设置当前生效的值，以及对可实时修改项的覆盖。 |  |
| `loom settings list` | 每个设置：值、来源、是否可实时修改。 | `--prefix`, `--changed`, `--json` |
| `loom settings set` | 为整个部署覆盖一个可实时修改的设置；重启后仍然有效。 | `<name>`, `<value>`, `--expect` |
| `loom settings reset` | 移除覆盖：恢复为环境变量或默认值。 | `<name>`, `--expect` |
| `loom settings history` | 已记录的变更，最新在前：操作者、变更前后的值。 | `<name>`, `--limit`, `--json` |
| `loom secrets` | 在 ClickHouse 中加密存储的凭据和签名密钥。 |  |
| `loom secrets reseal` | 将只能用 LOOM_JWT_SECRET_PREVIOUS 解开的内容，重新用 LOOM_JWT_SECRET 加密。 | `--dry-run`, `--check` |
| `loom signing-key` | 与 LOOM_JWT_SECRET 并列、保存在 ClickHouse 中的令牌签名密钥。 |  |
| `loom signing-key rotate` | 添加一个密钥；所有进程都加载它之后才会用它签名。 | `--algorithm`, `--activate-in` |
| `loom signing-key retire` | 让某个密钥（或 'default'）不再用于验证令牌。 | `<kid>` |
| `loom signing-key record-root` | 在有意替换之后，将当前 LOOM_JWT_SECRET 记录为部署的根密钥（用旧密钥加密的密钥仍无法使用）。 |  |
| `loom signing-key list` | 列出密钥。 |  |
| `loom provider-lock` | 提供方配置锁。 |  |
| `loom provider-lock clear` | 释放进程已停止的持有者所占的提供方配置锁（若其仍在续期、发送心跳或有查询在运行则拒绝）。 | `--holder` |
| `loom export-analytics` | 将某个 UTC 日的用量和命名空间快照以 Parquet 格式写入 S3，名称使用 LOOM_ANALYTICS_EXPORT_NAME_KEY 做哈希。 | `--bucket`, `--prefix`, `--day`, `--role-arn`, `--region`, `--expected-bucket-owner` |
| `loom sql-access` | 创建、调整范围或删除一个 ClickHouse 用户，该用户通过 sql_v1_* 视图读取租户的记忆、对话和请求账本。 |  |
| `loom sql-access grant` | 让该用户恰好读取此范围；若尚不存在则创建（并仅打印一次密码）。 | `--user`, `--org`, `--namespaces`, `--all`, `--rotate` |
| `loom sql-access revoke` | 收回该用户的范围，然后删除该用户。 | `--user` |
| `loom sql-access list` | 按 SQL 用户列出每条已授予的范围记录。 |  |
| `loom reembed` | 重新 embed embedding_model 与所配置 embedder 不一致的向量（更改 LOOM_EMBEDDING_MODEL 后运行，Loom 不会自动重新 embed）。 | `--org`, `--namespace`, `--batch-size`, `--in-database`, `--dry-run`, `--delete-old`, `--key-only`, `--resume` |
| `loom reextract` | 通过运行中服务器的写入路径，将一个命名空间的原始证据（memory.set_from_messages 的轮次）重放到另一个命名空间：用当前提取器处理已存储的转录；来源是… | `--org`, `--namespace`, `--into`, `--base-url`, `--token`, `--dates`, `--concurrency`, `--limit-sessions`, `--dry-run` |
| `loom deploy-delta` | 一次蓝绿切换改变了什么：部署的账本行（每个操作的量、错误、p50/p95、上游开销）及其 ClickHouse 语句形态（运行次数、字节、秒数），取切换前后各一个窗口… | `--swap-at`, `--window-min`, `--ch-os-user`, `--cluster`, `--top`, `--json` |
| `loom entity-export` | 将命名空间的实体图（共现边和三元组）导出为 GraphML 或 JSON-LD，供外部图或 RDF 工具使用。 | `--org`, `--namespace`, `--format`, `--output`, `--all` |
| `loom audit-namespace` | 命名空间的使用情况（namespace.audit）：精确键读取与搜索、哈希键、双重编码和过大的值、手工搭建的索引行、被存成记忆的租约、版本抖动。 | `--org`, `--namespace`, `--window-days`, `--samples`, `--json` |
| `loom mentor` | 存储给某个 agent 的建议（mentor.brief）：它调用了什么、其命名空间提供的平面、带证据的提示，以及过去的提示被采纳的频率。 | `--org`, `--namespace`, `--agent`, `--window-days`, `--json` |
| `loom serve` | 启动控制平面（uvicorn）。 | `--host`, `--port`, `--log-level`, `--reload` |
| `loom migrate` | 对所配置的 CH 应用 schema 迁移。 | `--status`, `--dry-run`, `--strict`, `--target`, `--migrations-dir` |
| `loom reconcile-status` | 重新写出状态与 loom.memory 不一致的 memory_embeddings 行（被取代、冲突、重复的标记，而池从未看到这些标记），按每个身份行（org, namespace, agent… | `--org`, `--namespace`, `--all-namespaces`, `--since-days`, `--exclude`, `--limit`, `--apply`, `--dry-run` |
| `loom period-digest` | 写入周期摘要：针对一个命名空间中每个身份的 K 个连续会话摘要，生成一条派生的语义记忆。 | `--org`, `--namespace`, `--sessions`, `--max-identities`, `--max-periods`, `--apply`, `--dry-run`, `--rewrite` |
| `loom demote-activity-narration` | 找出命名空间中叙述工具活动（“User read lines 1-1000 of X”、“Claude ran a command on …”）而没有持久结果的已提取事实，并将其降级（置信度 x0.25）或通过…标注 | `--org`, `--namespace`, `--action`, `--classifier`, `--days`, `--limit`, `--apply`, `--dry-run` |
| `loom recalibrate` | 立即重新探测 embedder 并运行各命名空间的校准（否则由重新校准 worker 每周运行一次）。 |  |
| `loom import-clickgap` | 将 ClickGap（Detector→Validator→Reviewer）的 SQLite 导出导入 Loom（lessons、类型化实体和类别）。 | `--sqlite`, `--org`, `--namespace`, `--agent`, `--dry-run`, `--batch-size`, `--seed-recall-events` |
| `loom import-recall` | 将外部向量召回型 agent 记忆的 SQLite 导出导入 Loom。 | `--sqlite`, `--org`, `--namespace`, `--agent`, `--base-url`, `--token`, `--dry-run` |
| `loom import-brain` | 将 markdown 形式的 agent “brain”目录导入 Loom：MEMORY.md（始终注入的 HOT 层）→ profile.set 分区；memory/contexts/*.md（按需读取的 WARM 层）→ knowledge.add 文档。 | `--dir`, `--org`, `--namespace`, `--agent`, `--base-url`, `--token`, `--dry-run` |
| `loom claim-release` | 释放自助注册的认领：撤销其授权，清除所有者，并将其停用。 | `--org`, `--namespace`, `-y` |
| `loom mint-token` | 在本地签发 JWT（需要 LOOM_JWT_SECRET）。 | `--subject`, `--org`, `--namespaces`, `--roles`, `--platform-admin`, `--namespace-role`, `--ttl-days`, `--save`, `--read-scope`, `--read-user-id`, `--traffic-class`, `--consumer`, `--service-account`, `--user-token`, `--json` |
| `loom revoke-token` | 按 jti 或 subject 吊销一个令牌（通过吊销模块写入 loom.revoked_tokens；需要已配置的 ClickHouse）。 | `--jti`, `--subject`, `--list`, `--issued-before`, `--scim`, `--reason`, `--expires-at`, `--json` |
| `loom sso` | 将密码账户迁移到单点登录。 |  |
| `loom sso adopt` | 让 SSO 登录能够访问邮箱位于 --domain 的密码账户：之后每个人通过身份提供方登录到自己的账户，使用自己的命名空间和数据。 | `--domain`, `--org`, `--skip`, `--keep-tokens`, `--apply`, `--dry-run` |
| `loom sso link` | 让某个人的 SSO 登录能够访问其以另一个地址（个人邮箱、带 +tag 的地址）注册的密码账户。 | `--email`, `--org`, `--namespace`, `--apply`, `--dry-run` |
| `loom sso unlink` | 撤销某个邮箱的关联：其授权不再允许这些账户登录。 | `--email`, `--apply`, `--dry-run` |
| `loom sso password-link` | 打印一次性链接（有效期为 LOOM_PASSWORD_LINK_TTL_SECONDS，默认一天），用于为某个邮箱的 SSO 账户设置密码，适用于关闭单点登录之后。 | `--email`, `--org` |
| `loom bot` | 使用密钥或其 CI 身份登录的 bot 和 CI 作业。 |  |
| `loom bot create` | 注册 bot：包括其命名空间，以及密钥（--generate-key 或 --public-key）和/或 CI 信任（--issuer、--audience、--claim）。 | `--name`, `--org`, `--namespace-role`, `--generate-key`, `--public-key`, `--issuer`, `--audience`, `--claim`, `--jwks-uri`, `--max-token-age`, `--max-access-token-ttl`, `--max-agents`, `--agent-claim`, `--platform-admin`, `--replace` |
| `loom bot list` | 列出所有 bot，无论处于启用还是停用状态。 |  |
| `loom bot disable` | 拒绝其凭证，并撤销其持有的令牌。 | `--name` |
| `loom doctor` | 诊断启动前的配置错误（Python 版本、设置、JWT、CH 可达性），并打印 /v1/health 所携带的问题列表，每项附带修复方法。 | `--json`, `--client`, `--server-url`, `--token` |
| `loom report` | 打印错误报告表单的 Environment 字段：版本信息和 `loom doctor` 的检查结果。 | `--open` |
| `loom drops` | 按阶段/原因打印某个命名空间的记忆摄入丢弃台账（loom.memory_drops）：即管道丢弃、截断或放弃处理的内容。 | `--namespace`, `--org`, `--hours` |
| `loom namespaces-gc` | 列出（默认）或清除临时命名空间：即 bench 前缀下已完成的 bench 运行，且无人注册、在 --idle-hours 内无人写入；每个列表范围的清除 saga 一次处理… | `--org`, `--prefix`, `--namespace`, `--exclude-prefix`, `--idle-hours`, `--include-registered`, `--erase`, `--no-purge`, `--skip-external`, `--batch`, `--show`, `--requester`, `--worker-pass` |
| `loom erasure-re-verify` | 重新验证失败的清除 saga：打印每个 saga 最后一个以步骤命名的原因，以及新的残留统计（默认，只读）；--resume 会从检查点重新运行它们，越过恢复 worker 的… | `--org`, `--id`, `--resume`, `--skip-external`, `--completed-since`, `--orphan-graph`, `--erase`, `--batch`, `--show`, `--requester` |
| `loom reconcile` | 在恢复 ClickHouse 或 Postgres 之后，在已恢复的存储中重新执行其他记录中晚于恢复点的清除和脱敏操作，并（在 ClickHouse 恢复之后）重新发布每个… | `--restored`, `--point`, `--allow-live`, `--apply`, `--dry-run` |
| `loom router` | 部署中位于各 cell 前面的路由器。 |  |
| `loom router serve` | 提供放置、查找和运维 API；将其他请求路由到各 cell。 | `--host`, `--port`, `--log-level` |
| `loom router migrate` | 应用目录中待执行的迁移。 |  |
| `loom cell` | 通过路由器的目录运维部署中的 cell。 |  |
| `loom cell credential` | 创建路由器凭证，或轮换某个 cell 的凭证。 | `<action>`, `--cell`, `--next`, `--finish`, `--revert`, `--operator`, `--router` |
| `loom cell limit` | 关闭、重新开放某个 cell 或限制其接收新组织。 | `--cell`, `--closed`, `--open`, `--placements-max`, `--router` |
| `loom cell which` | 打印查找得到的某个组织所在的 cell 和主机（未放置时为第一个 cell）。 | `--org`, `--router` |
| `loom cell remove` | 删除处于附加状态且没有放置的 cell 的目录行。 | `--cell`, `--router` |
| `loom cell prepare` | 在路由器的目录中注册此 cell 并写入其行。 | `--cell`, `--host`, `--credential-sha256`, `--probe-url`, `--wait`, `--router` |
| `loom tracker-probe` | 对 tracker 源的只读首次接触：按凭证统计每个上游表的数量和最新时间戳、熔断器状态以及已配置的 tracker 命名空间。 |  |
| `loom tracker-contract-check` | 只读：每个 tracker 源的 schema 是否满足记录契约（docs/features/tracker/record-contract.md）？ | `--source`, `--namespace`, `--json` |
| `loom tracker-dupcheck-fit` | 立即为一个 tracker 命名空间重新拟合 tracker.duplicate_check 的校准置信度：回放提交时的重复项金标准，拟合并存储结果。 | `--namespace`, `--org`, `--negatives`, `--dry-run`, `--out` |
| `loom tracker-embed-reset` | 清除一个 tracker 命名空间的 tracker_embed 遍历游标，使下一次遍历重新扫描窗口，并重新嵌入所有文本配方已更改的条目。 | `--namespace`, `--org`, `--kind`, `--apply`, `--dry-run` |
| `loom namespaces-adopt` | 为持有有效记忆但没有注册行的命名空间提供所有者行：bench 前缀下为 kind=bench，否则为 memory；retention_days 为 0，无 TTL。 | `--org`, `--owner`, `--prefix`, `--namespace`, `--kind`, `--apply`, `--dry-run`, `--show` |
| `loom sensitivity-sweep` | 为有效行打标签：这些行本应在写入时被敏感度检测器打上标签，却因某条路径跳过而未打。 | `--org`, `--namespace`, `--since-days`, `--limit`, `--show`, `--only-shape`, `--apply`, `--dry-run` |
| `loom integrity-repair` | 读取行级完整性指标（持有向量的 dim-0 行、tombstoned_at/status 不一致、超过 valid_to 的活动行），并在使用 --apply 时以其真实维度重新发出 dim-0 行，使搜索… | `--org`, `--namespace`, `--limit`, `--show`, `--apply`, `--dry-run` |
| `loom rechunk` | 使用当前的分块器，根据已存储的内容对某个命名空间的知识文档重新分块并重新嵌入，使每个文档都带有章节元数据、分块上下文和文档频率。 | `--org`, `--namespace`, `--source-uri`, `--apply`, `--dry-run` |
| `loom export-actions` | 导出由操作注册表派生的动作 schema：带有 loom 扩展的 MCP tools/list 清单（json），或策略网关用来管控该接口的 Cedar schema（cedar）。 | `--format`, `--output` |
| `loom lexical-stats-rebuild` | 根据某个命名空间的有效行重新计算 BM25 文档频率（词频视图只能看到插入；取代和墓碑会留下过时的词汇）。 | `--org`, `--namespace`, `--no-knowledge`, `--no-memory` |
| `loom version` | 打印已安装的 Loom 版本。 |  |
| `loom install` | 将 Loom 接入本地 AI 客户端的配置。 | `--client`, `--server-url`, `--token`, `--name`, `--toolset`, `--dry-run`, `--force` |
| `loom demo` | 最初的五分钟：像 `make ingest` 那样，将本仓库的 AGENTS.md（或 --file）摄入知识平面，向其提问，并打印答案及其证据行。 | `--file`, `--question`, `--tags`, `--base-url`, `--token`, `--org`, `--namespace`, `--root`, `--search-only` |
| `loom init` | 在 Loom 服务器上创建身份。 | `--agent`, `--server-url`, `--org`, `--purpose`, `--ttl-days`, `--token-path`, `--config-path`, `--no-files`, `--json` |
| `loom ingest-transcript` | 将聊天记录文件（markdown / JSON）作为一个会话线程导入 Loom 的 conversation 表。 | `--server-url`, `--token`, `--org`, `--namespace`, `--agent`, `--thread-id`, `--file`, `--dry-run` |
| `loom memory-search` | 在 shell 中运行 memory.search。 | `--server-url`, `--token`, `--org`, `--namespace`, `--agent`, `--user-id`, `--query`, `--top-k`, `--search-mode`, `--json-agent` |
| `loom set-host` | 指定此部署的 Loom 主机。 | `<emails>`, `--env-file`, `--append`, `--dry-run` |
| `loom login` | 使用一次性代码在任意浏览器上登录 Loom 服务器。 |  |
| `loom logout` | 在服务器上结束已存储的会话，并在本地清除。 | |
| `loom whoami` | 显示已存储的会话以谁的身份登录。 |  |
| `loom call` | 使用已存储的会话调用任意操作或 API 路由（`loom call --help`）。 |  |
| `loom token` | 为 CI 和 agent 创建、列出和撤销你自己的令牌（`loom token --help`）。 |  |

<!-- prettier-ignore-end -->
<!-- END GENERATED: cli -->

<a id="related"></a>

## 相关内容

- [操作参考](https://github.com/ClickHouse/loom/blob/main/docs/reference/operations.md)：客户端命令所调用的 op。
- [维护](https://github.com/ClickHouse/loom/blob/main/docs/operations/maintenance.md)：重新嵌入、回填与修复。
- [代码索引操作](https://github.com/ClickHouse/loom/blob/main/docs/operations/code-index-operations.md)。
- [升级](https://github.com/ClickHouse/loom/blob/main/docs/operations/upgrades.md)：升级流程中的 `loom migrate`。
- [本地开发](../development/local-development.md)。
