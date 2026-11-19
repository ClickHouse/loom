<!-- i18n: source=docs/features/code-index/freshness.md blob=9dcf44236c7be8f6d338e452c10b5a0be2e98f93 model=claude-sonnet-5-5 -->
> 本页由机器从[英文原文](https://github.com/ClickHouse/loom/blob/main/docs/features/code-index/freshness.md)翻译，内容以英文原文为准。
> Machine-translated from the [English page](https://github.com/ClickHouse/loom/blob/main/docs/features/code-index/freshness.md), which is authoritative.

<a id="code-index-freshness"></a>

# 代码索引的新鲜度

索引会滞后于代码仓库：它只对已配置分支上已合并的提交建立索引，按轮次逐步处理，并且每个层级各自推进自己的水位线。在依赖已索引的代码之前，请先查看答案中的新鲜度元数据（如果有），以及相关层级的状态。

<a id="the-as_of-block"></a>

## `as_of` 块

返回 `as_of` 的操作会暴露以下新鲜度字段。普通的 `code.symbol` 定义不包含此块，也不包含 commit；请改为检查 `code.index_status`，并对照相应的源代码修订版本来验证返回的定义。

| 字段            | 含义                                                                                                                                                 |
| --------------- | ---------------------------------------------------------------------------------------------------------------------------------------------------- |
| `commit`        | 此命名空间的快速层级 commit（`git_head`），不是每个编译器层级的修订版本                                                                              |
| `branch`        | 命名空间所跟踪的分支                                                                                                                                 |
| `indexed_at`    | 该 commit 落地的时间                                                                                                                                 |
| `lag_commits`   | `commit` 与镜像已获取的最新提交之间的 first-parent 提交数；`-1` 表示未知                                                                             |
| `stale`         | `lag_commits` > `LOOM_CODE_STALE_AFTER_COMMITS`（默认 5），或镜像获取已逾期                                                                          |
| `status`        | `fresh`（位于最新提交）、`behind`（未超过过期阈值）、`stale`、`unknown`（lag 为 `-1`）                                                              |
| `stale_reason`  | `mirror_not_fetched`：滞后量是相对于一个已停止获取的镜像统计的                                                                                       |
| `fetched_at`    | 与 `stale_reason` 同时出现：镜像上次获取的时间                                                                                                       |
| `failing_tiers` | 条目上有层级失败时出现：`tier`、`kind`、`failing_since`、`failed_passes`、`fix`，对于带有水位线的层级还有 `lag_commits`                              |

该块按命名空间缓存 30 秒；移动 `git_head` 的一次处理会使其失效。它采用 fail-open 策略：无法读取新鲜度时，该块为空，答案仍会返回。请将空的或缺失的 `as_of` 视为未知。

即使 `stale` 为 false，失败层级的事实数据也可能落后于最新提交：当 `failing_tiers` 指明某个编译器层级时，请将编译器级别的答案（精确的调用图、引用）视为可能已过时，并阅读你所依赖的代码。

**不要只检查构建状态，也要检查新鲜度。** 首次构建已完成的命名空间仍可能落后若干个 commit。返回 `as_of` 时，检查 `stale`；没有返回时，通过 `code.index_status` 检查相应的水位线。当新鲜度为 stale 或未知时，请从你的检出副本中读取即将编辑的文件。

<a id="the-mirrors-own-age"></a>

## 镜像自身的时效

滞后是相对于 Loom 本地镜像来计算的，因此当镜像已停止拉取时，滞后仍显示为 0。所以当拉取超时，`stale` 也会变为 true，状态操作会报告镜像的时效：

- `code.index_status`（单个命名空间）和 `code.repos`（每个可读条目）会返回 `clone_head`、`clone_fetched_at` 和 `fetch_overdue`（超过 `LOOM_CODE_FETCH_STALE_AFTER_SECONDS`，默认 3600）。此外还有：`git_head`、`fast_tier_lag`、`scip_lag` / `scip_py_lag`、`compiler_tiers` 列表、`last_refresh_at`，以及 `chunks_total` / `chunks_embedded` / `embedding_coverage`。
- 二者都会返回 `tier_health`：每个层级最近一次记录的结果（拉取、摄取、编译器层级以及每周一次的 C++ 完整运行），失败时会给出错误类型、`failing_since`、`failed_passes` 和修复方法。若某个 Go 层级因本机无法拉取依赖而跳过了部分模块，则显示为 `state: "unavailable"`（类型为 `dependency_unavailable`，`failing_since` 为首次记录的时间，没有失败次数），并附有修复方法：它不算失败，也不会出现在 `as_of.failing_tiers` 中。
- `code.repos` 的每一行都带有 `searchable`：`embedding_coverage` 已达到 `LOOM_CODE_READY_EMBED_COVERAGE`。

`compiler_tiers` 为该条目运行的每个层级提供一张卡片（`producer`、`tool`、`label`、`commit`、`lag`、`workers`）。通过 MCP 调用时，以及设置了 `commit` 时，`code.index_status` 会返回精简的结果；`brief: false` 则返回完整结果。

<a id="is-one-commit-in-every-tier"></a>

## 某个提交是否已进入所有层级？

审查已合并 PR 的人需要确认每个层级都已包含该合并。带 `commit=<sha>` 调用 `code.index_status`，结果会在 `commit_coverage` 中给出：

| 字段                    | 含义                                                                 |
| ----------------------- | -------------------------------------------------------------------- |
| `ready`                 | 当快速层级以及每个带有水位线的编译器层级都已到达该提交时为 true      |
| `waiting_on`            | 尚未到达的层级                                                       |
| `in_mirror`             | 镜像中是否包含该提交                                                 |
| `fast`, `tiers[]`       | 每个层级：`covered`（镜像缺少该提交时为 null）和 `commits_since`     |
| `commits_since`         | 快速层级已越过该提交多远                                             |
| `paths_changed_overlap` | 带 `paths=[…]` 时：这些路径中有哪些被之后的合并修改过                |

`paths_changed_overlap` 中的路径在索引中与该提交时不同，请从你的检出副本中读取。

```python
st = await agent.code.index_status(namespace="code-myrepo", commit=merge_sha, paths=pr_files)
cov = st["commit_coverage"]
if not cov["ready"]:
    print("waiting on", cov["waiting_on"])
```

<a id="waiting-for-coverage-without-polling"></a>

## 无需轮询即可等待覆盖

一旦某个提交被覆盖，`code.coverage.watch` 就会在任务收件箱中发布一个事件：

```bash
curl -fsS -X POST "$LOOM_BASE_URL/v1/code.coverage.watch" \
  -H "Authorization: Bearer $LOOM_API_KEY" -H 'Content-Type: application/json' \
  -d '{"org":"<org>","namespace":"code-myrepo","commit":"'"$MERGE_SHA"'","inbox":"<your-inbox>"}'
```

- `commit` 是完整的 SHA，或是镜像可以解析的缩写。`inbox` 是任务收件箱（`inbox.register`）。调用方需要在代码命名空间上拥有 viewer 权限，并在收件箱所属命名空间上拥有 writer 权限。
- `code_coverage_notify` worker（默认开启，每隔 `LOOM_CODE_COVERAGE_NOTIFY_INTERVAL_SECONDS` 运行一次，默认值为 60）会发布一个 `code.coverage.ready` 事件，其 `patch` 为 `{namespace, commit, tiers: [{producer, label, watermark}]}`。该事件通过 `/v1/task/watch?inbox=…`、`task.updates` 以及收件箱的签名 webhook 送达。
- 对于已被覆盖的提交，调用会返回 `ready=true` 和 `emitted=true`，并在同一次调用中发布事件。
- 每个（命名空间，提交，收件箱）组合只产生一个事件：其 `task_id` 即返回结果中的 `event_id`，因此重试不会重复发布。同一事件的多次投递在该 id 的租约下逐个执行，所以在两个竞争的投递中，只有存储了该事件的那一个会返回 `emitted=true`。如果某个在同一次调用中投递的 `code.coverage.watch` 等待另一次投递超过 10 秒，则返回 409 `PRECONDITION_FAILED`；如果租约存储（Keeper）没有响应，则返回 503；这两种情况都可以重试。
- `code.coverage.unwatch`（字段相同）会删除一个待处理的监视；`removed: false` 表示没有有效的监视。

Python SDK 提供了 `agent.code.coverage_watch(commit, inbox, namespace=…)` 和 `agent.code.coverage_unwatch(...)`。收件箱的说明见[任务与决策](https://github.com/ClickHouse/loom/blob/main/docs/features/tasks-and-decisions.md#task-inbox)。

<a id="pushing-freshness-from-ci"></a>

## 从 CI 推送新鲜度

轮询器（`LOOM_CODE_INGEST_INTERVAL_SECONDS`，120）会捕获每一次合并。若要立即为某次合并建立索引，请在合并后的任务中调用 `code.refresh`，并使用作用域限定在代码命名空间的写入令牌：

```bash
curl -fsS -X POST "$LOOM_BASE_URL/v1/code.refresh" \
  -H "Authorization: Bearer $LOOM_CI_TOKEN" -H 'Content-Type: application/json' \
  -d '{"org":"<org>","namespace":"code-myrepo","expected_head":"'"$GITHUB_SHA"'"}'
```

- 该次处理只覆盖指定的命名空间，该命名空间必须是已配置的条目（否则返回 404），并将其配置的分支索引到获取到的最新提交。
- `expected_head` 仅用于记录日志。`wait=true` 会阻塞到处理结束（最长 `wait_timeout_seconds`，默认 300）；响应中包含 `triggered`、`already_running`、`head_before` 和 `head_after`。
- 该调用是尽力而为的；轮询器会修复错过的触发。

运维人员应关注 `loom.code.tier_lag_seconds{tier}`；滞后层级的运维手册见[代码索引运维](https://github.com/ClickHouse/loom/blob/main/docs/operations/code-index-operations.md)。

<a id="settings"></a>

## 设置

| 设置                                         | 默认值  | 作用                                                                 |
| -------------------------------------------- | ------- | -------------------------------------------------------------------- |
| `LOOM_CODE_STALE_AFTER_COMMITS`              | 5       | 落后多少个提交后，`as_of.stale` 变为 true                            |
| `LOOM_CODE_FETCH_STALE_AFTER_SECONDS`        | 3600    | 镜像存在多久后触发 `fetch_overdue` 和 `stale`（0 表示禁用该检查）    |
| `LOOM_CODE_INGEST_INTERVAL_SECONDS`          | 120     | `code_ingest` 轮询镜像的频率                                         |
| `LOOM_CODE_READY_EMBED_COVERAGE`             | 0.95    | 已嵌入的比例达到多少后，`code.search` 才使用其向量检索分支           |
| `LOOM_CODE_COVERAGE_NOTIFY_INTERVAL_SECONDS` | 60      | 检查覆盖率监视的频率                                                 |

<a id="limits"></a>

## 限制

- 新鲜度按分支命名空间计算。合并到 `main` 的修复在合并到发布分支之前，不会出现在该发布分支的命名空间中；`code.grep mode=presence namespaces=[…]` 可以一次检查多个命名空间。
- 未合并的 pull request 和你的工作树永远不会进入索引。
- 索引更新时不会主动推送通知；`code.coverage.watch` 会按每个 commit 发出通知。

<a id="related"></a>

## 相关内容

- [overview.md](https://github.com/ClickHouse/loom/blob/main/docs/features/code-index/overview.md)：解读答案，故障排查
- [operations.md](https://github.com/ClickHouse/loom/blob/main/docs/features/code-index/operations.md)：状态操作
- [代码索引架构](https://github.com/ClickHouse/loom/blob/main/docs/architecture/code-index-architecture.md)：水位线、对账、提取器标记
