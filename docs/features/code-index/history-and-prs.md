# History, PRs and releases

These ops need merged-PR history; the `code_pr_history` worker is off by default
(`LOOM_CODE_PR_HISTORY_ENABLED`).

The PR plane answers "how did the code get here". It lives in the same code namespace
as the index, so a history answer joins a merged PR's files against today's symbols,
callers and tests in one read, with no GitHub call at question time. A team's issue and
incident records join in through a linked [tracker](../tracker/overview.md).

## The PR plane

| Table                     | Holds                                                                                                              | Filled by                                                                       |
| ------------------------- | ------------------------------------------------------------------------------------------------------------------ | ------------------------------------------------------------------------------- |
| `loom.code_pr_history`    | one row per merged PR of the namespace's branch: number, title, author, labels, files, linked issues, merge commit | the `code_pr_history` worker; `loom code backfill-prs` for the first months     |
| `loom.code_pr_reviews`    | inline review comments of merged PRs                                                                               | the same worker (`LOOM_CODE_PR_REVIEWS_ENABLED`, default on; needs a token)     |
| `loom.code_gh_items`      | issues and PRs in any state, open PRs with the files their head touches                                            | the same worker (`LOOM_CODE_GH_ITEMS_ENABLED`, default on; skipped anonymously) |
| `loom.code_backport_gaps` | the nightly set-wide backport report                                                                               | the `code_backport_gaps` worker                                                 |

The worker uses `LOOM_CODE_GITHUB_TOKEN` or the entry's `github_token`, else GitHub's
anonymous rate limit. A release-branch namespace holds the PRs merged into that branch,
so "what changed on 2.4" reads the 2.4 namespace. `loom code backfill-prs` loads
`LOOM_CODE_PR_BACKFILL_MONTHS` (12) of history for a new repository
([code index operations](../../operations/code-index-operations.md#first-index)).

## Which op answers which question

| Question                                                                  | Op                                                                                       |
| ------------------------------------------------------------------------- | ---------------------------------------------------------------------------------------- |
| Brief me on this PR, or on the change I am about to make                  | `code.context_pack` (`pr_number` or `task`; `sections` keeps only the blocks you act on) |
| Which merged PRs touched this symbol or path, and what did reviewers say? | `code.history`, `code.symbol_history`                                                    |
| Which merged PRs last touched these lines, between release A and B?       | `code.blame_range`                                                                       |
| What did reviewers check on this merged PR? What do they usually check?   | `code.review_brief`, `code.conventions`                                                  |
| Before I open a PR: does one like it already exist?                       | `code.pr_check`                                                                          |
| Which merged PRs could have introduced this bug?                          | `code.bisect_candidates`                                                                 |
| Which release branches still carry the bug a fix removed?                 | `code.backport_gap` (one fix), `code.backport_gaps` (every labelled fix)                 |
| Which CI build first carried this commit?                                 | `code.first_build`                                                                       |

Arguments are in the [operations reference](../../reference/operations.md#code).

### History of a symbol, a path or a line range

`code.history` lists the merged PRs that most recently touched a symbol (`name` or
`symbol_id`), a `path`, or given PR `numbers`: number, title, author, labels, and up to
`reviews_per_pr` (default 3, `0` = none) human review comments on that file.
`code.symbol_history` narrows this to one function's regression history: the PRs that
touched its span, their linked issues and reverts, and the review comments inside it.

`code.blame_range` takes a `path`, a line range (at most 400 lines) and a window as
release versions (`from_version`, `to_version`) or commit shas. It combines the PR
ledger's file lists in the window (a floor) with `git blame -w -M -L` on the mirror,
each blamed commit mapped to its PR. On a release-branch namespace it also reads the
trunk namespace's ledger, only when the caller may read the trunk; otherwise the
trunk's rows are left out and `legs.ledger.skipped` names it. A release version
resolves to its branch namespace's indexed head when the caller may read that
namespace, else through the mirror's branch or `-new` tag.

- `stage: "introduced"` (default) demotes backports, reverts and whitespace- or
  rename-only touches, and promotes a PR that names the enclosing function, drew a
  review comment inside it, or whose linked issue names the words in `mentions`.
- `stage: "last_touched"` keeps the blame order.

The answer is exact for "last touched", never for "introduced the bug"; its `note` says
so.

### Review briefs and conventions

`code.review_brief` reads the brief the ingest built when the PR merged: the class of
every changed file, the touched functions with their callers and fan-in, the tests that
cover them, and what reviewers said.

At the `standard` and `full` tiers the brief also has a `rationale` section: the design
decisions and constraints in the repository's trunk code namespace whose `files` or code
links touch the PR's files. It is read on every call, only when the caller may read the
trunk namespace, and never stored with the brief, so a decision recorded or changed after
the merge shows up on the next read. It reads the first
`LOOM_CODE_REVIEW_BRIEF_RATIONALE_PATHS` (10) changed files, source files first, up to 50
rows each, and serves at most `LOOM_CODE_REVIEW_BRIEF_RATIONALE_ROWS` (8) rows, those with
something to check first, in whole rows up to `LOOM_CODE_REVIEW_BRIEF_RATIONALE_TOKENS`
(250) tokens: rows that say `check` first, then constraints; the first row is always
served. Each row carries the line the pre-edit block prints for it (value, source,
`(pinned)`, a code-change note), `pinned`, `code_state` and a `verdict`.

The check looks for two kinds of name: the code-shaped names in a row's `Rejected:` clause
(backticked, qualified like `A::b`, or calls like `f()`), and the snake_case names a
constraint states, backticked or not. The closing `Applies to` clause, which names the
code the row governs, is not checked. It is a whole-word match on the lines the diff adds,
read up to 200,000 characters of a posted diff or 200,000 bytes of the mirror's, so
`check` means "read this row against the diff", not a violation found.

| Row verdict | Meaning                                                                                                                                                                                    |
| ----------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------ |
| `check`     | the diff adds one of the row's checked names (`names`)                                                                                                                                     |
| `clear`     | the row has checked names and the diff adds none of them                                                                                                                                   |
| `unknown`   | no name found, but the diff could not be read here, was cut, or has a hunk that miscounts its lines, or the row's value was cut (`diff_unread`, `diff_cut`, `diff_malformed`, `value_cut`) |
| `""`        | the row names nothing to check                                                                                                                                                             |

The section's verdict is `check` when any row says so, then `unknown`, then `clear`, and
`none` when no row has anything to check. It says `clear` or `none` only when every
changed path and every matching row was checked; otherwise it says `unknown`, and the
causes say what was left: changed paths not read (`paths_not_read`), a path whose read
reached its 50 rows (`paths_at_row_ceiling`), or rows the row cap left out
(`rows_not_read`) when one of them had something to check.

`code.conventions` states the repository's
recurring maintainer checks as facts over one PR's diff (`pr_number`, `head_sha`,
`files` or `diff`), never as opinions; `rules` narrows which checks run. The diff it
reads is capped at `LOOM_CODE_CONVENTIONS_DIFF_MAX_BYTES`.

## Pre-PR duplicate check: `code.pr_check`

An agent about to open a pull request passes the draft and gets one of three verdicts:
`duplicate_of` (an open PR is already doing this, or a merged one already did),
`related`, or `none`.

```python
from loom import Agent

async with Agent(org="acme", namespace="code-widgets", agent="pr-bot") as bot:
    check = await bot.call(
        "code.pr_check",
        title="Retry the upload when the token expires",
        paths=["src/upload/client.py"],
        symbols=["UploadClient.send"],
        issues=[1432],
    )
    if check["verdict"] == "duplicate_of":
        for item in check["items"]:
            print(item["pr_number"], item["state"], item["evidence"])
```

The same call over HTTP is `POST /v1/code.pr_check`, and over MCP the tool is
`loom_code_pr_check` (in the `coding` and `tracker` toolsets). A title, changed paths or
an issue number is required.

Candidates come from Loom's own ledgers through four legs, with no GitHub call:

| Leg             | Reads                                                                                                                    |
| --------------- | ------------------------------------------------------------------------------------------------------------------------ |
| open PRs        | every open PR in `code_gh_items`: its title and the files its `refs/pull/N/head` touches (no body)                       |
| closed unmerged | PRs closed without merging in the last `LOOM_CODE_PR_CHECK_CLOSED_DAYS` (180): "someone tried this"; capped at `related` |
| merged, by path | merged PRs that touched the draft's `paths`                                                                              |
| merged, by text | merged PRs whose text scores against the draft's title and symbols, and merged PRs closing the same `issues`             |

Each candidate is scored by one fixed rule: the share of the draft's title and symbol
tokens it carries (weighted by rarity), a small body term for merged PRs, path overlap
in both directions, and whether it closes an issue the draft names. There is no labelled
set to fit the weights on, since duplicate PRs are closed, not merged. The answer says
`epistemic: "heuristic"`, and a title alone never reaches `duplicate_of`.

| Setting                                  | Default | Effect                                                                  |
| ---------------------------------------- | ------- | ----------------------------------------------------------------------- |
| `LOOM_CODE_PR_CHECK_DUPLICATE_THRESHOLD` | 0.55    | score at which a candidate is `duplicate_of`                            |
| `LOOM_CODE_PR_CHECK_RELATED_THRESHOLD`   | 0.2     | score at which it is `related`                                          |
| `LOOM_CODE_PR_CHECK_MERGED_DAYS`         | 365     | merged PRs older than this do not vote as duplicates                    |
| `LOOM_CODE_PR_CHECK_MASS_FILES`          | 200     | a candidate touching more files is a mass change; its files do not vote |

`tracker.duplicate_check` is the issue-side sibling: on a `none` or `related` verdict it
carries `code.pr_check`'s PRs that may already address the draft as `related_prs`
([tracker overview](../tracker/overview.md#duplicate-check)).

## Bisect candidates: `code.bisect_candidates`

Given a reported bug, which merged PRs should an engineer investigate first?
`code.bisect_candidates` prioritizes candidates; it is not an automated `git bisect`.
It does not build the repository, run tests, reproduce the report, discover good and bad
versions, or prove that a candidate caused the bug. A candidate's score is an
investigation ranking, not a probability of causation.

The regression boundary comes from evidence recorded before the call:

| Window input                  | Where it comes from                                                                                                               | What Loom does with it                                                                                                                              |
| ----------------------------- | --------------------------------------------------------------------------------------------------------------------------------- | --------------------------------------------------------------------------------------------------------------------------------------------------- |
| `bad_version`, `good_version` | A user, CI job, support workflow or testing agent has observed the bug on the bad release and observed the good release as clean. | Scans the releases between those observations. Loom does not retest either release.                                                                 |
| `issue_number`                | The linked tracker issue already has `reproduces_on` and `clean_on` triage observations, populated by the tracker workflow.       | Reads those observations when explicit versions were not supplied. The existence of an issue or triage value is not proof that the report is valid. |
| `after`, `before`             | The caller knows only an approximate merge-time interval, such as the last known-good date and first-seen date.                   | Narrows the PR-history scan to that interval; the dates are bounds, not reproduction results.                                                       |
| `bad_sha`                     | The caller knows a commit at or before the first observed failure.                                                                | Resolves the commit time on the repository mirror and uses it as `before` when `before` is absent. It does not run the commit.                      |

The caller then supplies whatever report evidence it has: crashing frames (`symbols`),
source `paths`, `patterns`, free `text`, an `issue_number` and, when known, a `fix_pr`.
When no precise release range is available, repository-history fallback uses the recent
history retained for this scan (`LOOM_CODE_BISECT_HISTORY_DAYS`, default 90), which is a
broader and less precise investigation window.

1. Frames resolve to files through the code index.
2. The candidate scan reads the merged, non-derivative PRs whose first release lies in
   the window and that touched those files, ranked by the frames their diff names. With
   a linked tracker it scans the tracker's PR diffs; the previously recorded triage
   columns of `issue_number` (`reproduces_on`, `clean_on`) can supply the window. With no tracker,
   or a tracker holding no changed files, it reads the repository's own PR history
   (`LOOM_CODE_BISECT_HISTORY_DAYS`, default 90), and the answer says
   `candidates_from: "code_history"`.
3. With no frame (a wrong-result report) and an `issue_number`, the scan is bounded by
   the issue's rarest identifiers in the window's diffs, the component's directories and
   the fix's files; `wideners` chooses which, `widened_by` says which applied.
4. Report-time fusion fuses the diff scan with vector, lexical, path, subtree and
   recency ranks; a PR the report names as introducing the bug leads. When the fused top
   is weak (margin over #2 under 0.1 and not a report-named suspect), a wider window
   (12 releases, or `LOOM_CODE_BISECT_WIDE_DAYS`) joins and one LLM call reranks the top
   20 (`LOOM_CODE_BISECT_LLM_RERANK_MODEL`; a timeout keeps the fused order), or
   `rerank: true` asks for it.
5. A report naming no fix and no issue borrows the fix of the issue the duplicate check
   matches, inside `LOOM_CODE_BISECT_DUPCHECK_TIMEOUT_S`.

Each candidate carries marks (backport, revert, reverted, re-land, whitespace- or
rename-only, `touches_symbol` from the diff's hunk headers); `marks: false` returns the
bare ranked list. Which paths count as source code is the tracker's
`bisect.code_prefixes` ([record contract](../tracker/record-contract.md#source-entry-options)).
The rerank needs a real LLM provider; the other stages run on the stock stack once PR
history is loaded. The bisect settings are listed in the
[configuration reference](../../reference/configuration.md#code).

The final verification remains external to this operation: an engineer, CI workflow or
testing agent must reproduce the issue and test the ranked candidates.

## Backport gaps

`code.backport_gap` names a merged fix (`pr_number`) or the issue it closes
(`issue_number`, resolved through the tracker's `fixes` edges or a recovered link) and
answers per release-branch namespace of the same repository, newest first, or the
`branches` you name (namespaces or versions such as `26.4`). Three legs meet: the backport
record (the tracker's `is_backport_pr`, `upstream_pr_number` and `base_branch`, beside the
branch ledger's merged backports), the fix's first release (a branch cut at or after it
carries the fix), and whether the functions the fix's hunks touch exist on each branch.

| Verdict        | Means                                                                                                                                |
| -------------- | ------------------------------------------------------------------------------------------------------------------------------------ |
| `gap`          | no backport, the branch predates the fix, and the touched functions (or files) are present; `cherry_pick` names the PR and merge sha |
| `pending`      | a backport PR exists and is open                                                                                                     |
| `covered`      | a backport merged, the branch was cut at or after the fix's first release, or the issue's `fix_versions` names the branch            |
| `not_affected` | every touched function (or file) is absent on the branch                                                                             |
| `unknown`      | the record was unavailable while the code is present, the presence check ran out of time, or the branch has no version to match on   |

A symbol-level `gap` is a lower bound: the code exists on the branch, which does not
prove the bug does.

- `exact: true` checks the patch per hunk. Removed lines still present and added lines
  absent make `gap` exact; added lines present, removed lines gone and no backport
  recorded make `not_affected` exact.
- `history: true` (implies `exact`) also reads the branch's tree and history in the
  mirror for rows the lines leave undecided.
- `presence.patch` shows the mode and the overlaps as evidence.

`code.backport_gaps` is the set-wide report: every fix of the last 90 days that
maintainers labelled for backport, per release branch, gaps first. The
`code_backport_gaps` worker writes it nightly into `loom.code_backport_gaps` by running
`code.backport_gap(exact, history)` per fix (`LOOM_CODE_BACKPORT_GAPS_ENABLED`, default
on, effective only with `LOOM_CODE_PR_HISTORY_ENABLED`); the op reads that table.
`wanted_only: false` includes unlabelled fixes. Every branch namespace is authorized
before it is read, failing closed.

## First build

`code.first_build` takes a commit `sha` or a merged PR's `pr_number` and answers which CI
build first carried it. It needs the linked tracker's `version_history` table: a run
naming the sha is exact; otherwise the bases of the runs after the merge are checked on
the mirror (`git merge-base --is-ancestor`, at most ten) for the earliest descendant, and
the answer is a lower bound.

## PR and tracker planes

A maintainer links trackers to a repository with `code.repo.set_source`, primary first
([tracker overview](../tracker/overview.md)). Then:

- `code.bisect_candidates` scans the tracker's PR diffs and checks suspects against
  known fixes;
- `code.backport_gap` reads the backport record, and `code.first_build` the tracker's
  CI runs;
- `code.history` and the history parts of `code.search` and `code.ask` carry the
  tracker record beside the PR when a binding resolves and the caller may read it;
- `code.context_pack` can include tracker items (`sections=["qa"]`).

Without a tracker the ops answer from the PR plane where they can, and name the
tracker-only parts as missing.

## Limits and failure modes

- **No history, no answer.** With `LOOM_CODE_PR_HISTORY_ENABLED` off and no
  `backfill-prs`, the history tables are empty and the ops answer with nothing to rank.
- **Anonymous GitHub access** covers public repositories at the public rate limit; review
  comments and the issue mirror need a token.
- **Open PRs are not indexed code.** `code.pr_check` sees an open PR's title and files,
  not its body or its symbols.
- **Heuristic verdicts.** `code.pr_check` is uncalibrated by design; `code.blame_range`
  is exact only for "last touched"; `code.bisect_candidates` ranks suspects and never
  names a culprit as fact.
- **Tracker outages degrade, never fail.** A tracker read that times out or trips its
  breaker sets `tracker_unavailable`; the PR-plane legs still answer.

## Related

- [Tracker overview](../tracker/overview.md) and [record contract](../tracker/record-contract.md)
- [Adding repositories](repositories.md)
- [Code index operations by question](operations.md)
- [Code index architecture](../../architecture/code-index-architecture.md)
- [Operations reference](../../reference/operations.md#code)
