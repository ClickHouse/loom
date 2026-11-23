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
{
  "query": "where are request quotas enforced?",
  "namespace": "code-myrepo",
  "top_k": 5
}
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
`code.scip_ingest`, `code.scip_status`; `LOOM_MCP_CODE_UPLOAD_TOOLS=true` serves the last
two over MCP as well. An op's MCP tool name is `loom_` plus its name with dots as
underscores. Toolsets: [MCP reference](../../reference/mcp.md#toolsets).

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

Pass `pattern` as a list of exact strings to find any of them in one scan. For
example, `["parse(", "parse_async("]` matches both fragments without escaping
the parentheses. All alternatives share a result limit, and each matching line
or file appears once.
`mode: "message"` and `regex: true` require a single string.

In matches mode, `code.grep(include_body=true)` returns the enclosing definitions
beside the hit lines. Each definition appears once; its `lines` lists the matching
locations. The same retained-source reader backs `code.enclosing`, including type
and document-section fallback. The Python index keeps decorators in their
owning function's source chunk, so a hit inside a decorator can return the
complete definition. No token budget is imposed on these grep bodies.

The default remains matching lines only. Bodies are read only for the served page,
and page reruns remove definitions belonging to earlier pages. If source cannot be
read, matching lines remain available with `definitions_partial=true` and a
`definitions_note`. Unresolved or elided definitions retain their structural
disclosures and next lookup when available. Check those fields before treating a
missing body as missing code. The flag is rejected outside matches mode.

A caller with a clock of its own passes `deadline_ms` to `code.grep` or `code.search`
(clamped to 200–60000; 0 means none, and unset takes `LOOM_CODE_SEARCH_DEADLINE_MS`):
the request stops starting new work when the budget runs out and returns what it has,
`partial: true` with a `partial_reason` naming what did not finish (a grep scan stopped
by ClickHouse's break-on-timeout, a search leg, rerank, HyDE), instead of timing out.

A failed lexical or vector retrieval leg also returns `partial: true`. Its
`partial_reason` names the unavailable leg, and `note` warns that results are
incomplete. These warnings survive the default MCP result shaping and searches
across repositories. An empty result during an outage does not prove that no match
exists. Failed-leg results are neither memoized nor saved as continuation pools;
retry after restoring the provider or store. Choosing `legs: "lexical"` deliberately
is not a failure.

Providers that record request outcomes expose failures in `/v1/health` under
`providers_degraded` and `problems`, and in the dashboard's Health view. A successful
recorded call clears its warning. Other providers and constructor failures can
produce a search warning without a health warning. Check credentials, permissions
and quota. Health reads make no provider calls and cannot detect expired credentials
before a real request encounters them.

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

Set `example_context_lines` on `code.symbol` to return retained source around each
usage example alongside the definition. Where the source is available, the
example's `context` contains `commit`, `start_line`, `end_line` and `text`.
Source must match the indexed revision of the usage site. An explicit token
budget reserves room for the definition before adding usage context.

`code.search` fields worth knowing: `path_prefix`, `path_globs` / `exclude_globs`
(rg `-g`), `langs` (rg `-t`), `pattern` (constrains both legs to chunks containing it),
`bodies: "none"` (spans and signatures only, the cheapest answer), `token_budget`,
`expand` (one LLM hop to bridge wording to identifiers) and `cursor` for the next page.

For `code.search` and `code.grep` in matches mode, pass the returned `next_cursor` as
`cursor` with the original search arguments to get the next page. Omit `cursor`
when starting a new search with a different query, pattern or filters.

`code.grep` includes linked lessons when available. Set `include_lessons: false`
for a follow-up lookup when the relevant notes are already in context.

`code.search` has no response token cap by default. Set
`LOOM_CODE_SEARCH_TOKEN_BUDGET` to a positive token count to set a deployment default;
`0` means unlimited. A request's `token_budget` overrides that default, including
`token_budget: 0` for an unlimited response. This applies to HTTP and MCP, cursor pages
and searches across repositories. `top_k` still limits the number of results per page,
and `bodies: "none"` still returns pointers without code bodies (the MCP default).
Small finite budgets prioritize result heads over optional risk annotations.

For a Compose installation, put the variable in the `.env` file beside `compose.yaml`
and recreate the server container after changing it.

## Follow a specification to code and back

Repository Markdown and imported knowledge documents take different paths. An indexed
`.md` file has a symbol for each heading. Outside fenced code blocks, a backticked
identifier such as `enforce_quota` can create a `reads` edge from that section to a code
definition when the name resolves uniquely in the same namespace. Ambiguous names and
unsupported reference forms create no such edge. This is a recorded textual reference,
not proof that the implementation satisfies the specification.

Use an indexed repository, a token that may read it and the `coding`
MCP toolset. Check the branch and indexed revision before comparing documents and implementation;
editor changes are not indexed. The names below illustrate the call shapes: replace them
with a section and definition returned from your repository, rather than expecting these
example names to exist.

1. Find the specification with `loom_code_search`, using the repository's `namespace`,
   a question such as "where is request quota enforcement specified?", and
   `path_prefix: "docs/"` when that directory is indexed. Read the returned section and
   copy its exact `symbol` value.
2. Follow that section's recorded references with `code.callees` through `loom_op`:

   ```json
   {
     "op": "code.callees",
     "arguments": {
       "namespace": "code-myrepo",
       "name": "docs.quotas::Quotas::Enforcement",
       "edge_kinds": ["reads"],
       "depth": 1,
       "max_nodes": 20,
       "explain": true
     }
   }
   ```

   Each returned `edges` item names its target in `to_symbol` and `to_symbol_id`;
   `path` and `line` locate the reference in the document. Keep the edge's confidence
   and any ambiguity or truncation warning. `explain` belongs inside `arguments` and
   keeps the full result; ordinary MCP compaction can omit symbol IDs and a repeated
   target name.

3. Read the target and find documents that cite it with `code.symbol` through `loom_op`:

   ```json
   {
     "op": "code.symbol",
     "arguments": {
       "namespace": "code-myrepo",
       "name": "enforce_quota",
       "include_body": true,
       "explain": true
     }
   }
   ```

   Prefer the previous edge's `to_symbol_id` as `symbol_id` when selecting one exact
   definition. `definitions` contains the implementation; `documented_in` lists up to
   five citing sections with their `section`, `path` and `line`. Read those sections
   before describing what they require. An absent link or an elided body does not prove
   that documentation or implementation is absent. For more recorded references, use
   `code.callers` with `edge_kinds: ["reads"]` and inspect the document paths among its
   results.

### Documents outside the repository

An internal document synced into the repository's trunk code namespace is knowledge
content, not a Markdown symbol in that branch's code graph. With a readable
`X-Loom-Code-Scope: acme/code-myrepo` MCP connection, `loom_search` can retrieve the
repository's documents alongside the caller's other content. It has no `namespace`
argument: setting a namespace on a separate code call does not set this connection scope.
A document source must be configured and synced; its worker needs
`LOOM_KNOWLEDGE_SOURCE_SYNC_ENABLED`. See [document sources](../knowledge.md#document-sources)
and [search scope](../unified-search.md#which-namespaces-it-reads).

Search for the symbol name and the behavior it implements, then inspect
`structuredContent.knowledge.results`: the MCP text summary quotes only an excerpt.
Preserve the returned `source_uri`, `section`, `revision` and any `locator` when
citing the passage. A knowledge revision is not a Git commit. Repository tags group
ordinary synced documents for retrieval; they do not create symbol edges or grant access.
A retrieved passage that names a symbol still needs an explicit code lookup and a human
or agent comparison. This workflow does not automatically join imported documents to
symbols, verify their claims, or prove that a missing search result means no document
exists. These call shapes are not captured output from a document/code qualification run.

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

### Check function changes and API contracts

Use `code.impact` to find indexed callers and tests that reach a proposed change.
Read `epistemic` and `causes` before treating the result as a complete dependency
list. Dynamic calls and consumers outside the indexed repositories can be missing.

For an API change, check the consumer contract as well as the call graph. For
example, making an optional request field required can break an existing client
without changing any indexed call site. Compare the request and response schemas,
required fields, status codes and error behavior, then check the expectations of
the clients that use that interface.

Report the indexed revision, the callers and tests found, the contract checks
performed, and any consumers outside the available scope. A caller list identifies
where to investigate; it does not establish that an API remains compatible.

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

### Response token limits

Code retrieval returns selected context without an implicit token cap. `code.symbol`,
`code.enclosing`, `code.outline`, `code.trace`, `code.context_pack`, `code.map` and
`code.review_brief` default `token_budget` to `0` (unlimited). A positive value opts
into a cap. Symbol lookups return complete retained bodies, including large containers;
explicitly capped container lookups can return a skeleton and member outline.

`code.ask` uses `code_ask_token_budget` when the request omits a budget; the setting
defaults to `0`. An explicit request value of `0` overrides a finite deployment cap.
Attached file lessons use `code_result_lessons_tokens`, also unlimited by default.
The internal code grounding reads for memory questions likewise default to no token cap.

For search and grep matches, a file with a lesson linked to a returned definition
can omit ordinary lessons linked only to other definitions in that file.
File-level results keep their wider scope. Constraints, decisions, pins, file
notes and uncertain links stay eligible. `lessons_left_out` includes omissions.

Result counts, requested body modes, access checks, source completeness checks and
execution deadlines still apply. Unlimited does not reconstruct source absent from the
index. Model generation limits are separate from retrieval context limits.
