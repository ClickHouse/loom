<!-- i18n: source=docs/features/code-index/operations.md blob=0fe0101809c6f7360e6abcbf26ce81e8828343d5 model=claude-sonnet-5-5 -->
> 本页由机器从[英文原文](https://github.com/ClickHouse/loom/blob/main/docs/features/code-index/operations.md)翻译，内容以英文原文为准。
> Machine-translated from the [English page](https://github.com/ClickHouse/loom/blob/main/docs/features/code-index/operations.md), which is authoritative.

<a id="code-operations"></a>

# 代码操作

本页将常见问题对应到 `code.*` 操作。每个参数和输出都可以在自动生成的[操作参考](https://github.com/ClickHouse/loom/blob/main/docs/reference/operations.md#code)中找到，也可以在每个操作的 MCP 描述中找到。

<a id="calling-a-code-op"></a>

## 调用 code op

所有读取类 op 都接受 `org` / `namespace`。当调用省略它们时，MCP 传输层会使用编码 agent 插件针对当前检出发送的 `X-Loom-Code-Scope: <org>/<namespace>` 标头，否则使用部署的默认代码命名空间（`LOOM_CODE_ORG` / `LOOM_CODE_NAMESPACE`）。显式参数优先，并且无论哪种情况，授权都针对所指定的命名空间进行：如果标头指定的命名空间是该令牌无法读取的，请求会被拒绝，而不会扩大权限。如果没有设置默认值，未指定命名空间的调用会被拒绝，并返回验证错误。

部分 op 还接受 `namespaces=[…]`（最多 8 个），用于一次性跨多个分支索引进行查询。每个返回结果的响应都带有 `trace_run_id`；请通过 `code.outcome` 报告你对这些命中结果的处理方式，以便排序从中学习。

各接口中的相同调用：

```bash
# HTTP
curl -fsS -X POST "$LOOM_BASE_URL/v1/code.search" \
  -H "Authorization: Bearer $LOOM_API_KEY" -H 'Content-Type: application/json' \
  -d '{"org":"acme","namespace":"code-myrepo","query":"where are request quotas enforced?","top_k":5}'
```

MCP，`loom_code_search` 工具：

```json
{ "query": "where are request quotas enforced?", "namespace": "code-myrepo", "top_k": 5 }
```

```python
# Python SDK
from loom import Agent

async with Agent("acme", "support", "review-bot") as agent:
    # agent.code.* sends no namespace: the server's default code namespace answers
    hits = await agent.code.search("where are request quotas enforced?")
    # any code namespace, through the generic call (fields win over the Agent's identity)
    hits = await agent.call(
        "code.search", namespace="code-myrepo", query="where are request quotas enforced?"
    )
    for u in hits["results"]:
        print(u["path"], u["start_line"], u["symbol"], u["tier"])
    await agent.code.outcome(hits["trace_run_id"], "used_in_answer")
```

**MCP 列表。** `coding` 工具集列出 `code.search`、`grep`、`enclosing`、`ask`、`symbol`、`outline`、`callers`、`context_pack`、`impact`、`pr_check` 和 `bisect_candidates`。`core` 仅在 `LOOM_MCP_CODE_TOOLS=true`（默认关闭）时列出前八个；`tracker` 列出 `impact`、`pr_check` 和 `bisect_candidates`。其他所有 code op 都可以按名称调用，或通过 `loom_op` 调用：`loom_op {"op": "code.index_status", "arguments": {"namespace": "code-myrepo"}}`。以下 op 仅通过 HTTP 和 SDK 提供：`code.outcome`、`code.refresh`、`code.reindex`、`code.calibrate`、`code.context_packs`、`code.coverage.watch`、`code.coverage.unwatch`、`code.scip_ingest`、`code.scip_status`。op 的 MCP 工具名称是 `loom_` 加上将点替换为下划线后的 op 名称。工具集：[MCP 参考](https://github.com/ClickHouse/loom/blob/main/docs/reference/mcp.md#toolsets)。

**CLI。** 读取类 op 没有客户端 CLI。`loom code …`（`init`、`backfill`、`reconcile`、`compact`、`reembed`、`backfill-prs`、`scip-full`、`scip-delta`、`warm`、`describe`、`stats` 等）是服务器的管理 CLI（[CLI 参考](https://github.com/ClickHouse/loom/blob/main/docs/reference/cli.md)，[代码索引运维](https://github.com/ClickHouse/loom/blob/main/docs/operations/code-index-operations.md)）。

<a id="find"></a>

## 查找

| Op               | 适用场景                                                                                                                                                                                          |
| ---------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `code.grep`      | 你持有一个确切的字符串。默认按字面匹配，`regex=true` 时使用 RE2；`mode` 可取 `matches`、`files`、`presence`（跨 `namespaces`）或 `message`（一条打印出的消息 → 打印它的语句） |
| `code.symbol`    | 你持有一个名称：返回带签名和范围的定义；`uses=true` 会附加提到它的每一行，`near_paths` 会按你当前所在的文件对多个定义排序                                                                          |
| `code.search`    | 你有一个用自然语言表述的问题。混合符号、词法和向量检索；把你持有的标识符放进 `pattern=`                                                                                                           |
| `code.ask`       | 你想要带引用的综合答案（按问题缓存，被引用的文件变化时失效）                                                                                                                                      |
| `code.files`     | 你想按名称、glob 或基本文件名（不含扩展名）查找文件                                                                                                                                               |
| `code.enclosing` | 你有 `path:line` 形式的位置（最多 200 个），想得到每个位置所在的外层函数                                                                                                                          |
| `code.outline`   | 你原本需要通读整个文件才能知道其中有什么                                                                                                                                                          |
| `code.map`       | 你想要仓库各模块的概览地图                                                                                                                                                                        |
| `code.trace`     | 你有一段堆栈跟踪：把每一帧解析到它所属的单元                                                                                                                                                      |

自带时钟的调用方可以向 `code.grep` 或 `code.search` 传入 `deadline_ms`（限制在 200–60000 之间；0 表示不设限，未设置时取 `LOOM_CODE_SEARCH_DEADLINE_MS`）：预算耗尽时，请求不再启动新的工作，并返回已有的结果，同时带有 `partial: true` 和 `partial_reason`，说明哪一部分没有完成（被 ClickHouse 的 break-on-timeout 中止的 grep 扫描、某个检索分支、重排序、HyDE），而不是超时。在 `mode: "message"` 下，如果某个 pattern 没有匹配到任何语句模板（它是一行代码，而不是渲染后的消息），会再按字面量 grep 一次，并且返回结果中带有 `fallback: "literal"`。

**示例。**

```bash
# Which release branches contain this string? One call, one row per namespace.
curl -fsS -X POST "$LOOM_BASE_URL/v1/code.grep" \
  -H "Authorization: Bearer $LOOM_API_KEY" -H 'Content-Type: application/json' \
  -d '{"org":"acme","pattern":"retry budget exhausted","mode":"presence","namespaces":["code-myrepo","code-myrepo-2-4"]}'
```

```python
d = await agent.code.symbol("QuotaLimiter::check", uses_brief=True, include_body=True)
located = await agent.code.enclosing(["src/quota/limiter.py:88", "src/api/routes.py:140"])
```

`code.search` 值得了解的字段：`path_prefix`、`path_globs` / `exclude_globs`（rg `-g`）、`langs`（rg `-t`）、`pattern`（把两个检索分支都限定在包含它的块内）、`bodies: "none"`（只返回范围和签名，是最省的答案）、`token_budget`、`expand`（用一次 LLM 跳转，把自然语言措辞与标识符衔接起来）以及用于获取下一页的 `cursor`。

<a id="structure-and-change"></a>

## 结构与变更

| Op                      | 适用场景                                                                                                         |
| ----------------------- | ---------------------------------------------------------------------------------------------------------------- |
| `code.callers`          | 谁调用了它，向上追溯 `depth` 跳；若是重写（override），同时返回基类声明的调用者                                  |
| `code.callees`          | 它调用了什么，向下追溯 `depth` 跳                                                                                |
| `code.impact`           | 某个符号，或一组 `files` / 一份 `diff` 的影响范围：调用者、能触达该变更的测试、经常一起变更的文件                |
| `code.test_gate`        | 一次变更需要运行哪些测试，以及哪些被改动的函数没有任何测试能触达                                                 |
| `code.tests_for`        | 哪些测试覆盖了某个对象                                                                                           |
| `code.test_home`        | 新测试应该放在哪里                                                                                               |
| `code.coverage_claim`   | “这个已经有测试了吗？”                                                                                           |
| `code.verify`           | 对照索引检查一条封闭形式的断言（“A 调用 B”、“X 重写 Y”）                                                         |
| `code.verify_citations` | 在发送草稿之前，解析其中每一处 `path:line` / 符号引用                                                            |
| `code.path_guards`      | 两个符号之间的调用路径上存在的条件                                                                               |
| `code.examples`         | 某个符号的真实调用位置                                                                                           |
| `code.setting_facts`    | 代码树中关于某个 ClickHouse 设置的信息（声明、变更历史、CI 随机化）；仅对 ClickHouse 源码树有意义               |

`code.callers` / `code.callees` 接受 `min_confidence`，用于丢弃置信度低于该等级的解析器层级边（编译器边始终为 1.0）；还接受 `include_ambiguous`，用于把解析器放弃判定的调用位置作为单独的 `candidate_sites` 块一并返回。请阅读答案中的 `epistemic` 和 `causes`（[overview.md](https://github.com/ClickHouse/loom/blob/main/docs/features/code-index/overview.md#reading-an-answer)）。

**示例：一份 diff 的影响。**

```python
import subprocess

diff = subprocess.run(["git", "diff", "origin/main"], capture_output=True, text=True).stdout
r = await agent.code.impact(diff=diff, exclude_authors=["me"])
print(r["epistemic"], r.get("causes"))
for row in r.get("co_change_missing", []):
    print("usually changes with this diff:", row)
```

传入 diff 时，返回结果包含 hunk 所覆盖的符号、能触达这些符号的测试，以及 `co_change_missing`；只传 `files` 时，则把这些文件中的每个函数都视为已变更。两种形式都会返回 `owners`：即基于种子路径、按时间近度加权的 PR 作者，其中已去除 `exclude_authors`（通常是该变更本身的作者）。索引中保存的是已合并的代码，因此 diff 是针对分支头部来解读的；你尚未提交的函数，只有在分支头部已存在时才能被解析。

<a id="history-prs-and-releases"></a>

## 历史、PR 与发布

这些操作读取 PR 平面，该平面由 `code_pr_history` worker 从 GitHub 填充（`LOOM_CODE_PR_HISTORY_ENABLED`）。发布分支命名空间的历史包含合并到该分支的 PR。PR 平面如何填充，以及 bisect 和 backport 的详细说明，见 [history-and-prs.md](https://github.com/ClickHouse/loom/blob/main/docs/features/code-index/history-and-prs.md)。

| 操作                     | 适用场景                                                                                                       |
| ------------------------ | -------------------------------------------------------------------------------------------------------------- |
| `code.context_pack`      | 为某个 PR（`pr_number`）、你即将进行的修改（`task`）或整个仓库生成简报；`sections` 只保留你会用到的部分         |
| `code.history`           | 触及某个符号或路径的已合并 PR                                                                                  |
| `code.symbol_history`    | 在两个发布版本之间修改过某个符号的已合并 PR                                                                    |
| `code.blame_range`       | 某个行范围背后的已合并 PR                                                                                      |
| `code.review_brief`      | 评审者在某个已合并 PR 上检查了什么                                                                             |
| `code.conventions`       | 仓库中反复出现的评审检查项，基于单个 PR                                                                        |
| `code.pr_check`          | 在提交 PR 之前：是否已存在类似的 PR（已合并、未关闭或已关闭）？                                                |
| `code.bisect_candidates` | 根据调用栈帧、路径、符号和正常/异常窗口，找出哪些已合并 PR 可能引入了某个 bug                                  |
| `code.backport_gap`      | 哪些发布分支仍然带有某个修复所消除的 bug                                                                       |
| `code.backport_gaps`     | 整个集合范围的报告：被标记为需要 backport、但某些分支尚缺的修复                                                |
| `code.first_build`       | 哪个 CI 构建最先包含某个提交                                                                                   |

**示例：提交 PR 前的重复检查。**

```bash
curl -fsS -X POST "$LOOM_BASE_URL/v1/code.pr_check" \
  -H "Authorization: Bearer $LOOM_API_KEY" -H 'Content-Type: application/json' \
  -d '{"org":"acme","namespace":"code-myrepo","title":"Retry quota refresh on 503","paths":["src/quota/limiter.py"],"symbols":["QuotaLimiter::refresh"]}'
```

`verdict` 为 `duplicate_of`（有未关闭的 PR 正在做这件事，或已有合并的 PR 做过）、`related` 或 `none`；各项包含状态、分数和证据行。该检查是启发式的，不会调用 GitHub；仅凭标题不会被判定为重复。

<a id="status-and-administration"></a>

## 状态与管理

| Op                      | 角色   | 返回内容                                                                                                                                                |
| ----------------------- | ------ | ------------------------------------------------------------------------------------------------------------------------------------------------------ |
| `code.repos`            | viewer | 可读取的条目，包含新鲜度、访问权限和引导阶段；`repo` / `repos` 可将范围缩小到某一个检出（GitHub fork 会解析到其上游仓库的索引） |
| `code.index_status`     | viewer | 水位线、滞后、层级、`commit_coverage`（[freshness.md](https://github.com/ClickHouse/loom/blob/main/docs/features/code-index/freshness.md#is-one-commit-in-every-tier)）                                                  |
| `code.coverage`         | viewer | 按语言统计的文件、chunk、各层级的符号、边，以及 `gaps`，即索引尚不了解的内容                                                         |
| `code.index_errors`     | viewer | 索引器处理失败的文件                                                                                                                            |
| `code.recent_commits`   | viewer | 最近摄入的提交                                                                                                                              |
| `code.context_packs`    | viewer | 已存储的 PR pack                                                                                                                                        |
| `code.outcome`          | viewer | 记录你对某个结果所做的处理（`opened_file`、`used_in_answer`、`fell_back_grep`、`ignored`）                                                      |
| `code.coverage.watch`   | viewer | 某个提交被覆盖后，向你的收件箱发送一个 `code.coverage.ready` 任务事件；需要对收件箱所有者命名空间拥有 writer 权限                                 |
| `code.coverage.unwatch` | viewer | 取消一个待处理的监视                                                                                                                                  |
| `code.scip_status`      | viewer | 已上传的编译器层级增量的状态                                                                                                                                  |
| `code.refresh`          | writer | 立即触发一次摄入（[freshness.md](https://github.com/ClickHouse/loom/blob/main/docs/features/code-index/freshness.md#pushing-freshness-from-ci)）                                                                    |
| `code.scip_ingest`      | writer | 从外部生成方上传编译器层级增量（由 `code_scip_apply` 应用）                                                                  |
| `code.reindex`          | admin  | 将摄入水位线移动到某个提交                                                                                                                  |
| `code.calibrate`        | admin  | 重新拟合搜索的噪声下限和弃答阈值                                                                                                   |

添加、创建分支、限制和移除仓库（`code.repo.add`、`code.repo.remote_branches`、`code.repo.add_branch`、`code.repo.set_access`、`code.repo.set_source`、`code.repo.remove`）见 [repositories.md](repositories.md)。

<a id="related"></a>

## 相关内容

- [overview.md](https://github.com/ClickHouse/loom/blob/main/docs/features/code-index/overview.md) — 层级、语言支持、如何阅读答案
- [freshness.md](https://github.com/ClickHouse/loom/blob/main/docs/features/code-index/freshness.md) — `as_of`、提交覆盖范围、CI 刷新、覆盖范围监视
- [history-and-prs.md](https://github.com/ClickHouse/loom/blob/main/docs/features/code-index/history-and-prs.md) — PR 平面、bisect、backport
- [Python SDK 参考](https://github.com/ClickHouse/loom/blob/main/docs/reference/python-sdk.md)，[MCP 参考](https://github.com/ClickHouse/loom/blob/main/docs/reference/mcp.md)
- [操作参考](https://github.com/ClickHouse/loom/blob/main/docs/reference/operations.md#code) — 自动生成的参数
