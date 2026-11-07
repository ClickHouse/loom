# Code index freshness

The index trails the repository: it indexes merged commits on a configured branch, pass
by pass, and each tier advances its own watermark. Every answer says how far behind it
is.

## The `as_of` block

Every conversational answer (`code.search`, `code.ask`, `code.grep`, `code.symbol`, …)
carries `as_of`:

| Field           | Meaning                                                                                                                              |
| --------------- | ------------------------------------------------------------------------------------------------------------------------------------ |
| `commit`        | the commit the index holds for this namespace (`git_head`)                                                                           |
| `branch`        | the branch the namespace tracks                                                                                                      |
| `indexed_at`    | when that commit landed                                                                                                              |
| `lag_commits`   | first-parent commits between `commit` and the mirror's fetched tip; `-1` unknown                                                     |
| `stale`         | `lag_commits` > `LOOM_CODE_STALE_AFTER_COMMITS` (default 5), or the mirror fetch is overdue                                          |
| `status`        | `fresh` (at the tip), `behind` (under the stale threshold), `stale`, `unknown` (lag `-1`)                                            |
| `stale_reason`  | `mirror_not_fetched` when the lag is counted against a mirror that stopped fetching                                                  |
| `fetched_at`    | with `stale_reason`: when the mirror last fetched                                                                                    |
| `failing_tiers` | present while a tier is failing on the entry: `tier`, `kind`, `since`, `failed_passes`, `fix`, and for a compiler tier `lag_commits` |

The block is cached for 30 seconds per namespace; a pass that moves `git_head`
invalidates it. It is fail-open: when freshness cannot be read, the block is empty and
the answer still returns. Treat an empty `as_of` as unknown.

A failing tier stops moving, so its facts trail the head even while `stale` is false:
with `failing_tiers` naming a compiler tier, treat compiler-grade answers (exact call
graphs, references) as possibly behind and read the code you rely on.

**Gate on `stale`, not on the build state.** A namespace whose first build finished can
still be commits behind. When `stale` is true, treat line numbers and spans as
approximate and read files you are about to edit from your checkout.

## The mirror's own age

The lag is counted against Loom's local mirror, so it reads 0 while the mirror has
stopped fetching. `stale` therefore also turns true when the fetch is overdue, and the
status ops report the mirror's age:

- `code.index_status` (one namespace) and `code.repos` (every readable entry) return
  `clone_head`, `clone_fetched_at` and `fetch_overdue` (older than
  `LOOM_CODE_FETCH_STALE_AFTER_SECONDS`, default 3600). Beside them: `git_head`,
  `fast_tier_lag`, `scip_lag` / `scip_py_lag`, the `compiler_tiers` list,
  `last_refresh_at` and `chunks_total` / `chunks_embedded` / `embedding_coverage`.
- Both return `tier_health`: each tier's last recorded outcome (fetch, ingest and the
  compiler tiers), with the error kind, `failing_since`, `failed_passes` and the fix
  while it fails.
- `code.repos` rows carry `searchable`: `embedding_coverage` has reached
  `LOOM_CODE_READY_EMBED_COVERAGE`.

`compiler_tiers` has one card per tier the entry runs (`producer`, `tool`, `label`,
`commit`, `lag`, `workers`). `code.index_status` answers compactly over MCP and whenever
`commit` is set; `brief: false` returns the full answer.

## Is one commit in every tier?

A reviewer of a merged PR needs every tier to hold that merge. `code.index_status` with
`commit=<sha>` answers in `commit_coverage`:

| Field                   | Meaning                                                                                  |
| ----------------------- | ---------------------------------------------------------------------------------------- |
| `ready`                 | true once the fast tier and every compiler tier with a watermark have reached the commit |
| `waiting_on`            | the tiers that have not                                                                  |
| `in_mirror`             | whether the mirror holds the commit                                                      |
| `fast`, `tiers[]`       | per tier: `covered` (null when the mirror lacks the commit) and `commits_since`          |
| `commits_since`         | how far the fast tier has moved past the commit                                          |
| `paths_changed_overlap` | with `paths=[…]`: which of those paths a later merge changed                             |

Paths in `paths_changed_overlap` differ in the index from the commit; read them from
your checkout.

```python
st = await agent.code.index_status(namespace="code-myrepo", commit=merge_sha, paths=pr_files)
cov = st["commit_coverage"]
if not cov["ready"]:
    print("waiting on", cov["waiting_on"])
```

## Waiting for coverage without polling

`code.coverage.watch` posts an event on a task inbox once a commit is covered:

```bash
curl -fsS -X POST "$LOOM_BASE_URL/v1/code.coverage.watch" \
  -H "Authorization: Bearer $LOOM_API_KEY" -H 'Content-Type: application/json' \
  -d '{"org":"<org>","namespace":"code-myrepo","commit":"'"$MERGE_SHA"'","inbox":"<your-inbox>"}'
```

- `commit` is a full SHA or an abbreviation the mirror resolves. `inbox` is a task
  inbox (`inbox.register`). The caller needs viewer on the code namespace and writer on
  the inbox's owner namespace.
- The `code_coverage_notify` worker (on by default, every
  `LOOM_CODE_COVERAGE_NOTIFY_INTERVAL_SECONDS`, 60) posts one `code.coverage.ready`
  event with `patch` `{namespace, commit, tiers: [{producer, label, watermark}]}`. It
  arrives through `/v1/task/watch?inbox=…`, `task.updates` and the inbox's signed
  webhook.
- An already-covered commit answers `ready=true`, `emitted=true` and posts the event in
  the same call.
- One event per (namespace, commit, inbox): its `task_id` is the answer's `event_id`, so
  a retry never posts twice. Deliveries of one event run one at a time under a lease on
  that id, so of two racing deliveries only the one that stored the event answers
  `emitted=true`. A `code.coverage.watch` that delivers in the same call and waits more
  than 10 s for another delivery answers 409 `PRECONDITION_FAILED`, and 503 when the
  lease store (Keeper) does not answer; retry either.
- `code.coverage.unwatch` (same fields) drops a pending watch; `removed: false` means
  none was live.

The Python SDK has `agent.code.coverage_watch(commit, inbox, namespace=…)` and
`agent.code.coverage_unwatch(...)`. The inbox is described in
[tasks and decisions](../tasks-and-decisions.md#task-inbox).

## Pushing freshness from CI

The poller (`LOOM_CODE_INGEST_INTERVAL_SECONDS`, 120) catches every merge. To index a
merge at once, call `code.refresh` from the post-merge job with a writer token scoped to
the code namespace:

```bash
curl -fsS -X POST "$LOOM_BASE_URL/v1/code.refresh" \
  -H "Authorization: Bearer $LOOM_CI_TOKEN" -H 'Content-Type: application/json' \
  -d '{"org":"<org>","namespace":"code-myrepo","expected_head":"'"$GITHUB_SHA"'"}'
```

- The pass covers only the addressed namespace, which must be a configured entry (404
  otherwise), and indexes its configured branch to the fetched tip.
- `expected_head` is only logged. `wait=true` blocks until the pass ends (up to
  `wait_timeout_seconds`, default 300); the answer carries `triggered`,
  `already_running`, `head_before` and `head_after`.
- The call is best-effort; the poller heals a missed trigger.

Operators watch `loom.code.tier_lag_seconds{tier}`; the lagging-tier runbook is in
[code index operations](../../operations/code-index-operations.md).

## Settings

| Setting                                      | Default | Effect                                                               |
| -------------------------------------------- | ------- | -------------------------------------------------------------------- |
| `LOOM_CODE_STALE_AFTER_COMMITS`              | 5       | commits behind before `as_of.stale` is true                          |
| `LOOM_CODE_FETCH_STALE_AFTER_SECONDS`        | 3600    | mirror age before `fetch_overdue` and `stale` (0 disables the check) |
| `LOOM_CODE_INGEST_INTERVAL_SECONDS`          | 120     | how often `code_ingest` polls the mirror                             |
| `LOOM_CODE_READY_EMBED_COVERAGE`             | 0.95    | embedded share before `code.search` uses its vector leg              |
| `LOOM_CODE_COVERAGE_NOTIFY_INTERVAL_SECONDS` | 60      | how often coverage watches are checked                               |

## Limits

- Freshness is per branch namespace. A fix merged to `main` is not in a release-branch
  namespace until merged there; `code.grep mode=presence namespaces=[…]` checks several.
- Open pull requests and your working tree are never in the index.
- There is no push for "the index moved"; `code.coverage.watch` notifies per commit.

## Related

- [overview.md](overview.md): reading an answer, troubleshooting
- [operations.md](operations.md): the status ops
- [code index architecture](../../architecture/code-index-architecture.md): watermarks, reconciliation, the extractor stamp
