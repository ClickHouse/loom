# Code operations

This page maps questions to `code.*` ops. Every argument and output is in the
generated [operations reference](../../reference/operations.md#code), and in each op's
MCP description.

## Calling a code op

All read ops take `org` / `namespace`. When a call omits them, the MCP transport uses
the `X-Loom-Code-Scope: <org>/<namespace>` header the coding-agent plugins send for the
current checkout, and otherwise the deployment's default code namespace
(`LOOM_CODE_ORG` / `LOOM_CODE_NAMESPACE`). Explicit arguments win, and authorization
runs on the named namespace either way: a header naming a namespace the token cannot
read is refused, never widened. With no default set, a call that names no namespace is
refused with a validation error.

Several ops also take `namespaces=[…]` (up to 8) to ask across branch indexes at once.
Every answer that returns results carries a `trace_run_id`; report what you did with the
hits through `code.outcome` so ranking learns from it.

The same call in each surface:

```bash
# HTTP
curl -fsS -X POST "$LOOM_BASE_URL/v1/code.search" \
  -H "Authorization: Bearer $LOOM_API_KEY" -H 'Content-Type: application/json' \
  -d '{"org":"acme","namespace":"code-myrepo","query":"where are request quotas enforced?","top_k":5}'
```

MCP, the `loom_code_search` tool:

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

**MCP listing.** The `coding` toolset lists `code.search`, `grep`, `enclosing`, `ask`,
`symbol`, `outline`, `callers`, `context_pack`, `impact`, `pr_check` and
`bisect_candidates`. `core` lists the first eight only with `LOOM_MCP_CODE_TOOLS=true`
(default off); `tracker` lists `impact`, `pr_check` and `bisect_candidates`. Every other
code op is callable by name or through `loom_op`:
`loom_op {"op": "code.index_status", "arguments": {"namespace": "code-myrepo"}}`. These
are served over HTTP and the SDKs only: `code.outcome`, `code.refresh`, `code.reindex`,
`code.calibrate`, `code.context_packs`, `code.coverage.watch`, `code.coverage.unwatch`,
`code.scip_ingest`, `code.scip_status`. An op's MCP tool name is `loom_` plus its name
with dots as underscores. Toolsets: [MCP reference](../../reference/mcp.md#toolsets).

**CLI.** There is no client CLI for the read ops. `loom code …` (`init`, `backfill`,
`reconcile`, `compact`, `reembed`, `backfill-prs`, `scip-full`, `scip-delta`, `warm`,
`describe`, `stats`, …) is the server's admin CLI
([CLI reference](../../reference/cli.md),
[code index operations](../../operations/code-index-operations.md)).

## Find

| Op               | Use it when                                                                                                                                                                                       |
| ---------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `code.grep`      | you hold an exact string. Literal by default, `regex=true` for RE2; `mode` = `matches`, `files`, `presence` (across `namespaces`) or `message` (a printed message → the statement that prints it) |
| `code.symbol`    | you hold a name: definitions with signature and span; `uses=true` adds every line that mentions it, `near_paths` ranks several definitions by the files you are in                                |
| `code.search`    | you have a question in words. Hybrid symbol + lexical + vector retrieval; put an identifier you hold in `pattern=`                                                                                |
| `code.ask`       | you want a synthesized answer with citations (cached per question and invalidated when a cited file changes)                                                                                      |
| `code.files`     | you want files by name, glob or basename stem                                                                                                                                                     |
| `code.enclosing` | you have `path:line` locations (up to 200) and want the enclosing function of each                                                                                                                |
| `code.outline`   | you would otherwise read a whole file to see what is in it                                                                                                                                        |
| `code.map`       | you want an orientation map of the repository's modules                                                                                                                                           |
| `code.trace`     | you have a stack trace: each frame resolved to its unit                                                                                                                                           |

A caller with a clock of its own passes `deadline_ms` to `code.grep` or `code.search`
(clamped to 200–60000; 0 means none, and unset takes `LOOM_CODE_SEARCH_DEADLINE_MS`):
the request stops starting new work when the budget runs out and returns what it has,
`partial: true` with a `partial_reason` naming what did not finish (a grep scan stopped
by ClickHouse's break-on-timeout, a search leg, rerank, HyDE), instead of timing out.
In `mode: "message"`, a pattern that matches no statement template (a code line rather
than a rendered message) is grepped once more as a literal and the answer says
`fallback: "literal"`.

**Examples.**

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

`code.search` fields worth knowing: `path_prefix`, `path_globs` / `exclude_globs`
(rg `-g`), `langs` (rg `-t`), `pattern` (constrains both legs to chunks containing it),
`bodies: "none"` (spans and signatures only, the cheapest answer), `token_budget`,
`expand` (one LLM hop to bridge wording to identifiers) and `cursor` for the next page.

## Structure and change

| Op                      | Use it when                                                                                                                                   |
| ----------------------- | --------------------------------------------------------------------------------------------------------------------------------------------- |
| `code.callers`          | who calls this, `depth` hops; an override also reports the base declaration's callers                                                         |
| `code.callees`          | what this calls, `depth` hops                                                                                                                 |
| `code.impact`           | blast radius of a symbol, or of `files` / a `diff`: callers, tests reaching the change, co-change partners                                    |
| `code.test_gate`        | which tests a change obliges, and which touched functions no test reaches                                                                     |
| `code.tests_for`        | which tests cover something                                                                                                                   |
| `code.test_home`        | where a new test belongs                                                                                                                      |
| `code.coverage_claim`   | "is this already tested?"                                                                                                                     |
| `code.verify`           | check one closed-form claim ("A calls B", "X overrides Y") against the index                                                                  |
| `code.verify_citations` | resolve every `path:line` / symbol citation in a draft before sending it                                                                      |
| `code.path_guards`      | the conditions that stand on the call path between two symbols                                                                                |
| `code.examples`         | real call sites of a symbol                                                                                                                   |
| `code.setting_facts`    | what the tree says about one ClickHouse setting (declaration, change history, CI randomization); meaningful only for a ClickHouse source tree |

`code.callers` / `code.callees` take `min_confidence` to drop parser-tier edges below a
grade (compiler edges are always 1.0) and `include_ambiguous` to add the call sites the
resolver declined as a separate `candidate_sites` block. Read `epistemic` and `causes`
on the answer ([overview.md](overview.md#reading-an-answer)).

**Example: impact of a diff.**

```python
import subprocess

diff = subprocess.run(["git", "diff", "origin/main"], capture_output=True, text=True).stdout
r = await agent.code.impact(diff=diff, exclude_authors=["me"])
print(r["epistemic"], r.get("causes"))
for row in r.get("co_change_missing", []):
    print("usually changes with this diff:", row)
```

The diff shape returns the symbols the hunks overlap, the tests that reach them, and
`co_change_missing`; `files` alone treats every function in those files as changed.
Both shapes return `owners`: recency-weighted PR authors over the seed paths, with
`exclude_authors` (normally the change's own author) removed. The index holds merged
code, so the diff is read against the branch head; your uncommitted functions resolve
only when they already exist there.

## History, PRs and releases

These read the PR plane that the `code_pr_history` worker fills from GitHub
(`LOOM_CODE_PR_HISTORY_ENABLED`). A release-branch namespace's history holds the PRs
merged into that branch. How the plane is filled, and bisect and backports in depth, are
on [history-and-prs.md](history-and-prs.md).

| Op                       | Use it when                                                                                                                                       |
| ------------------------ | ------------------------------------------------------------------------------------------------------------------------------------------------- |
| `code.context_pack`      | briefing for a PR (`pr_number`), for a change you are about to make (`task`), or the repository; `sections` keeps only the blocks you will act on |
| `code.history`           | merged PRs that touched a symbol or a path                                                                                                        |
| `code.symbol_history`    | merged PRs that changed one symbol, between two releases                                                                                          |
| `code.blame_range`       | merged PRs behind a line range                                                                                                                    |
| `code.review_brief`      | what reviewers checked on a merged PR                                                                                                             |
| `code.conventions`       | the repository's recurring review checks, over one PR                                                                                             |
| `code.pr_check`          | before opening a PR: does a PR like this already exist (merged, open or closed)?                                                                  |
| `code.bisect_candidates` | which merged PRs could have introduced a bug, from frames, paths, symbols and a good/bad window                                                   |
| `code.backport_gap`      | which release branches still carry the bug one fix removed                                                                                        |
| `code.backport_gaps`     | the set-wide report of fixes labelled for backport that some branch lacks                                                                         |
| `code.first_build`       | which CI build first carried a commit                                                                                                             |

**Example: duplicate check before a PR.**

```bash
curl -fsS -X POST "$LOOM_BASE_URL/v1/code.pr_check" \
  -H "Authorization: Bearer $LOOM_API_KEY" -H 'Content-Type: application/json' \
  -d '{"org":"acme","namespace":"code-myrepo","title":"Retry quota refresh on 503","paths":["src/quota/limiter.py"],"symbols":["QuotaLimiter::refresh"]}'
```

`verdict` is `duplicate_of` (an open PR is doing this, or a merged one did), `related`
or `none`; items carry state, score and evidence lines. The check is heuristic and makes
no GitHub call; a title alone never reads as a duplicate.

## Status and administration

| Op                      | Role   | Returns                                                                                                                                                |
| ----------------------- | ------ | ------------------------------------------------------------------------------------------------------------------------------------------------------ |
| `code.repos`            | viewer | readable entries with freshness, access and bootstrap stage; `repo` / `repos` narrow it to one checkout (a GitHub fork resolves to its parent's index) |
| `code.index_status`     | viewer | watermarks, lags, tiers, `commit_coverage` ([freshness.md](freshness.md#is-one-commit-in-every-tier))                                                  |
| `code.coverage`         | viewer | per-language files, chunks, symbols per tier, edges, and `gaps` — what the index does not know                                                         |
| `code.index_errors`     | viewer | files the indexer failed on                                                                                                                            |
| `code.recent_commits`   | viewer | recently ingested commits                                                                                                                              |
| `code.context_packs`    | viewer | stored PR packs                                                                                                                                        |
| `code.outcome`          | viewer | records what you did with a result (`opened_file`, `used_in_answer`, `fell_back_grep`, `ignored`)                                                      |
| `code.coverage.watch`   | viewer | a `code.coverage.ready` task event on your inbox once a commit is covered; needs writer on the inbox's owner namespace                                 |
| `code.coverage.unwatch` | viewer | drops a pending watch                                                                                                                                  |
| `code.scip_status`      | viewer | the state of an uploaded compiler-tier delta                                                                                                           |
| `code.refresh`          | writer | trigger an ingest pass now ([freshness.md](freshness.md#pushing-freshness-from-ci))                                                                    |
| `code.scip_ingest`      | writer | upload a compiler-tier delta from an external producer (applied by `code_scip_apply`)                                                                  |
| `code.reindex`          | admin  | move the ingest watermark to a commit                                                                                                                  |
| `code.calibrate`        | admin  | refit the search's noise floor and abstain threshold                                                                                                   |

Adding, branching, restricting and removing repositories (`code.repo.add`,
`code.repo.remote_branches`, `code.repo.add_branch`, `code.repo.set_access`,
`code.repo.set_source`, `code.repo.remove`) are on
[repositories.md](repositories.md).

## Related

- [overview.md](overview.md) — tiers, language support, reading an answer
- [freshness.md](freshness.md) — `as_of`, commit coverage, CI refresh, coverage watches
- [history-and-prs.md](history-and-prs.md) — the PR plane, bisect, backports
- [Python SDK reference](../../reference/python-sdk.md), [MCP reference](../../reference/mcp.md)
- [operations reference](../../reference/operations.md#code) — generated arguments
