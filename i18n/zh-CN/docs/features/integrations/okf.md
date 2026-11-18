<!-- i18n: source=docs/features/integrations/okf.md blob=7d8ba9d5d921d351c46c4592c82a48277bb4f2cb model=claude-sonnet-5-5 -->
> 本页由机器从[英文原文](https://github.com/ClickHouse/loom/blob/main/docs/features/integrations/okf.md)翻译，内容以英文原文为准。
> Machine-translated from the [English page](https://github.com/ClickHouse/loom/blob/main/docs/features/integrations/okf.md), which is authoritative.

<a id="okf-bundles"></a>

# OKF 包

命名空间可以渲染为 OKF（Open Knowledge Format）v0.2 包：每条记忆对应一个带 YAML frontmatter 的 Markdown 文件，每个目录有一个 `index.md`，用一行描述列出其中的每个概念，另有一个记录变更的 `log.md`。CLI 在客户端运行，通过 HTTP API 访问。这个包可以做 diff、在拉取请求中评审，也可以离线阅读；Loom 仍然是存储。

这个包是一种投影，而不是第二份存储：它由命名空间重新生成，编辑会通过普通写入路径（冲突检测、黑名单、审计）回到 Loom。

```bash
loom export-okf --namespace <ns> --out ./knowledge        # render
loom okf-lint ./knowledge                                 # offline check: exit 2 critical, 1 warnings, 0 clean
loom import-okf ./knowledge --namespace <ns> --dry-run    # plan the write-back
```

这三个命令都支持 `--server-url`、`--token`（默认值为 `$LOOM_API_KEY`，其次是 `~/.loom/token`）和 `--org`（默认值：令牌的 `org` claim）；`okf-lint` 不需要其中任何一个。所有标志见 [CLI 参考](https://github.com/ClickHouse/loom/blob/main/docs/reference/cli.md)。

<a id="export"></a>

## 导出

`loom export-okf` 是只读的。它通过 `memory.list` 分页读取命名空间中所有有效的行（包含所有 agent），用 `memory.bulk_get` 获取完整的值，并把规划好的文件写入 `--out` 指定的目录。已有的概念文件会被覆盖，目录中的其他内容不会被改动。映射逻辑位于 `server/memory/domain/okf_bundle.py`（纯函数，有单元测试）；`server/cli/okf.py` 负责传输和文件系统操作。

| 记忆                               | 概念                                                                                                                                                                                                                                                                                                                                                           |
| ---------------------------------- | -------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `memory_key`                       | 文件名和 `loom.memory_key`：键的 slug，上限 160 字节；当另一个键占用了该名称，或该名称是 bundle 文件名（`index`、`log`、`README`、`AGENTS`，不区分大小写）或 Windows 设备名（`nul`、`con`、`com1`）时，附加由键和行 id 生成的 `-<hash>`                                                                                                                        |
| `memory_type`                      | 目录（类型的小写 slug，上限与文件名相同；对于 `evidence`、`digests`、`staging`、`node_modules`、bundle 文件的完整名称（`index.md`、`log.md`）或 Windows 设备名如 `nul`，使用 `type-<slug>`）和 `type`：`semantic` → Fact，`procedural` → Procedure，`preference` → Preference，`episodic` → Event，其他类型转为首字母大写 |
| 值                                 | 正文：文本原样输出，JSON 以围栏代码块输出                                                                                                                                                                                                                                                                                                                      |
| `tags`                             | `tags`                                                                                                                                                                                                                                                                                                                                                         |
| agent 和 `created_at`              | `generated: {by: "agent:<agent>", at}`                                                                                                                                                                                                                                                                                                                         |
| 有效的 `verified_by` / `verified_at` | `verified: [{by, at}]`（仅限人工和流程的认证）                                                                                                                                                                                                                                                                                                                 |
| `status`                           | `active` → `stable`，`conflicted` → `draft`，其他 → `deprecated`                                                                                                                                                                                                                                                                                               |
| `expires_at`                       | `stale_after`，即审阅截止日期                                                                                                                                                                                                                                                                                                                                  |
| `source_resource_id`               | `sources: [{resource}]`                                                                                                                                                                                                                                                                                                                                        |
| 所有 Loom 特有的内容               | `loom:` 块，包括 `source_class`、`sensitivity`、`superseded_by`、`valid_to` 和 `content_sha256`                                                                                                                                                                                                                                                                |

**链接。** 人工建立的边（命名空间中带类型的 `memory.link` 边，通过 `memory.export_evidence kind=edges` 读取，以及被取代的行所隐含的 `supersedes` 链）会写成具有权威性的 frontmatter `relates_to: [{path, kind}]`，并在正文的 "Related Concepts" 部分中镜像。随后是共享实体链接，数量有上限且双向对称，使 bundle 带有可供验证器遍历的图。没有任何链接的概念仍是孤儿，CLI 会报告它们而不是隐藏。

**来源信息随之保留。** `loom:` 块携带该行的来源层级，因此往返转换不会把推断变成第一手事实。

| 标志                   | 效果                                                                                                                                                                                                                                                                                                                                                                                                  |
| ---------------------- | ----------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `--include-high`       | 同时导出 `sensitivity: high` 的行、与之关联的原始分块和摘要，以及包含 high 特征（凭据、卡号、SSN 或明确给出的 PIN）的原始文本；服务器会审计每个被释放的条目；`memory.bulk_get` 不会提供超过保留期的 `high` 行，因此这类行会被列出名称并略过。默认关闭：镜像仓库的受众比命名空间的授权范围更广 |
| `--no-evidence`        | 只导出概念；跳过 bundle 默认携带的原始分块（`evidence/`）和会话摘要（`digests/`）                                                                                                                                                                                                                                                                                                                     |
| `--no-links`           | 跳过带类型的边（共享实体链接仍会写入）                                                                                                                                                                                                                                                                                                                                                                |
| `--include-tombstoned` | 同时导出已软删除的行，标记为 `status: deprecated`；列表中未完整提供的行（值超出预览长度，或在 `--include-high` 下的 `high` 行）会被列出名称并略过                                                                                                                                                                                                                                                      |
| `--limit N`            | 最多导出最近的 N 行（用于冒烟测试）                                                                                                                                                                                                                                                                                                                                                                   |
| `--title`, `--dry-run` | 根索引的标题；只做规划而不写入                                                                                                                                                                                                                                                                                                                                                                        |

<a id="lint"></a>

## 代码检查

`loom okf-lint DIR` 只读取 bundle 目录，不依赖其他任何东西：不需要服务器，不需要 ClickHouse，也不需要网络。每条发现都带有 id、路径、消息和严重级别（`server/memory/domain/okf_lint.py`）。

| id                              | 严重级别 | 含义                                                                                                 |
| ------------------------------- | -------- | ---------------------------------------------------------------------------------------------------- |
| `frontmatter`                   | critical | 某个 concept 没有 frontmatter、YAML 无法解析、`type` 为空，或者 `tags` 的值不是列表                  |
| `required_keys`                 | critical | 缺少 `title`、`description`、`loom.memory_key` 或 `loom.memory_type`                                 |
| `relates_to`                    | critical | 某条边格式错误，或者它指向的 `.md` 目标在 bundle 中不存在                                            |
| `index_missing` / `okf_version` | critical | 包含 concept 的目录没有 `index.md`；根索引没有声明 `okf_version`                                     |
| `staging`                       | critical | 存在 `staging/` 目录；staging 永远不会被用于回答                                                     |
| `type_directory_mismatch`       | warning  | `loom.memory_type` 与文件所在的文件夹不一致                                                          |
| `pointer_unmounted`             | warning  | `relates_to` 的路径是 `loom://org/namespace/key` 指针，指向 bundle 之外的 concept                    |
| `unknown_relation`              | warning  | `relates_to` 的关系类型不在 Loom 的标准动词之内                                                      |
| `edited_since_export`           | warning  | 正文的哈希值与 `loom.content_sha256` 不再一致（concept 或原始分块被手工编辑过）                      |
| `derived_needs_confidence`      | warning  | `source_class: derived` 的置信度为 1.0：把推断写成了事实                                             |
| `high_sensitivity_present`      | warning  | 高敏感度的行（`loom.sensitivity: high` 或 `sensitivity:high` 标签）被一并导出                        |
| `index_listing`                 | warning  | 某个 concept 没有被其所在目录的索引或根索引列出                                                      |
| `drift`                         | warning  | 根索引的 `loom.corpus_digest` 与 concept 文件不再匹配                                                |
| `orphans`                       | info     | 既不链接到任何内容、也没有被任何内容链接的 concept（这是数据的特征，不是缺陷）                       |

退出码遵循编译步骤的需要：出现任何 critical 发现时为 **2**，只有 warning 时为 **1**，没有问题时为 **0**。`--strict` 会让 warning 也以 2 退出（orphans 仍然只是提示信息）；`--json` 会输出机器可读的报告。

**语料摘要（corpus digest）。** 当根 `index.md` 带有 `loom.corpus_digest` 时，lint 会重新计算它：只取 concept 文件，按相对于 bundle 的路径排序，依次拼接每个文件的 SHA-256 十六进制摘要（不加分隔符），再对这个字符串计算 SHA-256。不匹配时会给出 `drift` 警告，并说明目录树中有多少个 concept 文件，以及索引中记录的数量。

<a id="import"></a>

## 导入

`loom import-okf DIR --namespace NS` 会先做 lint，如果 bundle 中有严重（critical）问题则拒绝导入，除非加上 `--force-import`（`--no-lint` 会跳过这道检查）。之后它通过 `memory.set` 写入每个 concept，身份来自令牌，或来自 `--agent`、`--user-id` 和 `--session-id` 标志，写入要经过冲突检测、黑名单和审计日志。随后它用 `memory.link` 重新创建 `relates_to` 边（`--no-links` 会跳过这一步）。

写入之前，导入会读取其身份下的现有记录，并跳过所有已有记录与待写入内容一致的 concept：比较的内容包括值（`content_sha256`）、类型、标签、置信度、`valid_at`、`valid_to`、`source_trace` 和 `source_class`（已声明的类别，否则为 `imported`），以及 bundle 携带的实体。已存在的有效边也会被跳过。落在同一行上的 concept（同一个 key 为多个写入者导出）只写入一次：以最近更新的为准。重复发送一条完全相同的记录会被计为一次强化，所以对同一个 bundle 导入两次，第二次不会写入任何 concept 或边。有两种例外：带有 `sources:` 的 concept 每次运行都会写入，因为导入所做的读取不会返回它的来源；目标把它合并到另一个 key 的 concept 每次运行都会再次发送，并强化那一行（计为 merged）。最后一行会统计目标行中已写入、未变化、已合并和失败的数量。

`source_trace` 是 `okf:<path inside the bundle>`，所以 bundle 在磁盘上的位置不会改变它：每次运行都可以导出到一个新的目录。

bundle 中的 `evidence/` 和 `digests/` 会通过 `memory.import_evidence` 写回（`--no-evidence` 会跳过它们），并且每次导入时，无论是否有变化，都会由执行导入的服务器重新做 embedding：定时导入应使用 `--no-evidence`。如果某个 chunk 或 digest 的文本被判定为高敏感度（凭据、银行卡号、SSN 或明确给出的 PIN），或者某个 chunk 含有在 chunk 边界被截断的这类内容的一部分，它会在没有向量的情况下写入，也无法被搜索（[敏感度](https://github.com/ClickHouse/loom/blob/main/docs/architecture/security.md#sensitivity)）。

| 标志              | 作用                                                                                                                                                                                  |
| ----------------- | --------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `--verify`        | 对 frontmatter 带有 `human:` 验证的 concept，通过 `memory.verify` 重新认证，**以执行导入的调用方身份**                                                              |
| `--keep-sessions` | 把每个 concept 写到其 `loom.session_id` 中的 session 下，因此曾存在于多个 session 下的 key 仍然是多行；`--session-id` 作为回退值                            |
| `--dry-run`       | 对照目标的现有记录（会读取它们）规划写入，但不发送任何写入                                                                                                |
| `--prune`         | 通过 `memory.delete_version` 删除此前由同一 `--agent` 和令牌的导入写入（带 `okf:` 的 `source_trace`）、且其 key 已不在 bundle 中的记录；需要 `--agent` |

导入到命名空间并使用 `--prune` 的每个 bundle 都需要各自独立的 `--agent`：prune 会删除该 agent 的、带 `okf:` trace 的、由其令牌写入的、而 bundle 中没有的记录。`--prune` 会拒绝没有 concept 的 bundle，也会拒绝 concept 文件的哈希与根索引中的 `loom.corpus_digest` 不一致的 bundle，因此手工编辑过的 bundle 永远不会触发删除。如果 bundle 的根索引不带 `loom.complete: true`，则会写入但不会 prune。只有当导出读取了每一行时才会写入该标记：即在列出之后，`memory.keys` 在一条语句中读取的每一行，以及每个被列出的行都由 `memory.bulk_get` 返回。在以下情况下不会写入：在 `--limit` 处停止、使用了 `--include-tombstoned`（它会在 bundle 中保留已删除的行），或来自较旧的版本。每次删除都通过 `memory.delete_version` 进行，使用导入的列表所看到的、带 `okf:` trace 的版本，并且使用完全相同的身份：此后被重写或被 `memory.lock` 锁定的行会被保留，而太旧、不认识该 op 的目标则什么都不会删除。要做 prune，请导出到一个空目录：导出会覆盖 concept 文件，但永远不会移除已离开命名空间的 key 的文件，因此重复使用的 `--out` 目录将不再与其摘要匹配。[把命名空间复制到另一个 Loom](https://github.com/ClickHouse/loom/blob/main/docs/operations/okf-sync.md) 会按计划运行导出和导入。

`--verify` 绝不会伪造认证。服务器根据令牌确定操作者；bundle 原来的验证者会记录在该记忆的标签中，用于溯源。

<a id="ci-gate"></a>

## CI 门禁

`integrations/okf-ci/okf-lint.yml` 是一个 GitHub Actions 工作流，用于存放该 bundle 的仓库：

1. 将它复制到该仓库中，路径为 `.github/workflows/okf-lint.yml`。
2. 将 `LOOM_REF` 设置为 40 个字符的 Loom commit SHA。其他任何值，该作业都会拒绝（文件默认此项为空）：规则随 Loom 一起版本化，分支或标签可能在检查通过后发生移动。
3. 将 `OKF_ROOT` 设置为 bundle 目录，即包含根目录 `index.md` 的目录（默认为 `.`）：可以是某一个命名空间的 bundle，也可以是针对该仓库所含各命名空间的矩阵。

该作业会在 bundle 旁边检出 `LOOM_REF` 对应的 Loom，并使用 `uv run --frozen` 运行 lint，因此依赖包来自该 commit 的 `uv.lock`：一个 `LOOM_REF` 对应一套规则和一套依赖。它以 `--json` 运行 lint，将摘要和前四十条发现（按严重程度、id 和路径排序）输出到作业日志，把 `okf-lint.json` 作为构建产物上传，并在退出码为 2 或 lint 未能运行（任何其他退出码）时失败。警告会通过检查，报告会保留供审阅者查看。

每个 action 都固定到完整的 commit SHA，并在注释中标明其版本。该工作流只请求 `contents: read` 权限，两次 checkout 都不会把令牌保留在 git 配置中（`persist-credentials: false`）。lint 本身不会建立任何网络连接，也不需要 ClickHouse。`integrations/okf-ci/README.md` 描述了每项检查。

<a id="run-the-gate-locally"></a>

### 在本地运行门禁

同样的命令会得到同样的结果：

```bash
git clone https://github.com/ClickHouse/loom .okf-lint-loom
git -C .okf-lint-loom checkout <LOOM_REF>
uv run --project .okf-lint-loom --frozen --python 3.12 loom okf-lint <OKF_ROOT>          # human report
uv run --project .okf-lint-loom --frozen --python 3.12 loom okf-lint <OKF_ROOT> --json   # the CI report
```

<a id="limits"></a>

## 限制

- **bundle 只是一份快照。** 导出之后在 Loom 中所做的更改，要到下一次导出才会出现在 bundle 中；编辑已过期的 bundle 之前，请先重新导出。
- **删除只会随 `--prune` 一起传递，** 并且只针对导入时写入的行。已从 bundle 中移除的边不会被删除。
- **高敏感度的行不会离开本地，** 除非传入 `--include-high`：一行是否为高敏感度，取决于它的 `sensitivity` 字段或 `sensitivity:high` 标签。如果导出时无法读取某行的敏感度，或读到的级别无法识别，该行同样不会离开本地。与这类行关联的原始文本块（raw chunk）和会话摘要也一样，文本中含有高敏感特征的也是如此（凭据、银行卡号、SSN 或明确给出的 PIN；原始文本没有敏感度列，因此由 `memory.set` 运行的检测器决定，无论 `LOOM_MEMORY_SENSITIVITY_AUTODETECT` 如何设置）。如果有高敏感度的行被导出，lint 会发出警告。

<a id="related"></a>

## 相关内容

- [写入记忆](https://github.com/ClickHouse/loom/blob/main/docs/features/memory/writing.md)和[冲突与拒绝](https://github.com/ClickHouse/loom/blob/main/docs/features/memory/conflicts-and-refusal.md)：导入会经历哪些处理。
- [历史与来源](https://github.com/ClickHouse/loom/blob/main/docs/features/memory/history-and-provenance.md)。
- [实体图](https://github.com/ClickHouse/loom/blob/main/docs/features/entity-graph.md)：`relates_to` 所携带的类型化边。
