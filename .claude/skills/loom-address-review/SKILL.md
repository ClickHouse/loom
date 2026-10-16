---
name: loom-address-review
description: Works through review comments on your own Loom pull request — fetches every unresolved thread, verifies each finding, fixes it at the source or answers why not, replies in the thread with the fixing commit, and repeats until a fresh review finds nothing. Use when a PR has review comments or requested changes, or when asked to "address the review", "fix the comments" or "respond to the reviewer". Not for reviewing someone else's change (loom-review).
argument-hint: "<pr-number>"
---

# Addressing review on a Loom PR

A review comment is a claim about the code. Treat each as a bug report: verify it, fix it at the source, and answer in its thread so the reviewer can check the fix without rereading the PR.

## 1. Collect every open thread

```bash
gh api graphql -F o=ClickHouse -F r=loom -F n=<n> -f query='
query($o:String!,$r:String!,$n:Int!){repository(owner:$o,name:$r){pullRequest(number:$n){
  reviewThreads(first:100){nodes{id isResolved isOutdated path line
    comments(first:50){nodes{databaseId author{login} body}}}}}}}' \
  --jq '.data.repository.pullRequest.reviewThreads.nodes[] | select(.isResolved | not)'
gh pr view <n> --json reviews,comments --jq '.reviews[].body, .comments[].body'   # review summaries and top-level comments
```

Findings from a `loom-reviewer` run in this session count the same as GitHub threads.

## 2. Verify, then decide

For each finding, before editing:

- Reproduce it: read the code path, write the failing test or a one-line probe. A finding you cannot reproduce gets a reply with what you checked, not a speculative change.
- Write down the failing case, the pattern it shares with other findings, and the fix you expect to work. Several findings with one cause get one fix.
- Fix every confirmed finding, nits included. Decline only with a reason a reviewer can check ("the caller already bounds this: `server/x.py` `_load` caps at 500"). Never decline silently.

## 3. Fix at the source

- The simplest change a new team member could follow: no new abstraction for one caller, no defensive branch for a state that cannot happen, no comment that restates the code.
- Add or change the test that would have caught it.
- Run the gates the fix touches (`loom-pre-push`), commit with a message that says why (`git commit -s`), and push.

## 4. Reply in each thread

Reply to the thread's first comment, in the `loom-writing` voice: what changed, the commit, the test.

```bash
gh api repos/ClickHouse/loom/pulls/<n>/comments/<databaseId>/replies -f body='Fixed in <sha>: <what now happens>; <test> covers it.'
```

- Resolve a thread only when the fix is pushed and the reply names it, and leave a human reviewer's thread for them to resolve when they asked to check it themselves.
- A top-level review comment gets one top-level reply that lists each point and its commit.
- Update the PR body when the fixes change what it claims (scope, wire changes, tests): the squash merge copies the body into `main`'s history.

## 5. Repeat

After the push, start a fresh review on the new head right away, in parallel with CI (`loom-reviewer` in Claude Code, `loom-review` elsewhere). The loop ends when a fresh reviewer returns no findings on the head and every check is green. An "approve" that still lists nits is not the end.

## Report

For each finding: what it was, fixed (commit) or declined (reason), and the test. Then the head commit, CI state and the last reviewer's verdict.
