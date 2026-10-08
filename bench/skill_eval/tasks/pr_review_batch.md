---
task: pr_review_batch
namespace: skill-eval-pr-review
agent: skill-eval
traps: blob-per-pr, batch-blob, hash-keys, review-body-as-memory
---

# Review batch: eight `acme/api` pull requests after incident 4711

You are the review agent. Review the eight PRs below and record the outcome
of each in Loom, org from your token, namespace **`skill-eval-pr-review`**,
so that (a) another agent can answer "which PRs still need changes", "who
authored the high-risk ones", "did #4105 touch tests" without reading your
review bodies, (b) your review body for each PR is kept and readable, and
(c) a second reviewer picking one PR up later can see it was claimed.

Each PR has the same fields: `repo`, `number`, `title`, `author`, `risk`
(low / medium / high), `verdict` (approve / request-changes / comment),
`tests_touched` (yes / no), `merged_at` (or unknown), and your review body
(two to six paragraphs).

## The batch

| #    | title                                             | author | files                                                              | tests                       | your read                                                                                      |
| ---- | ------------------------------------------------- | ------ | ------------------------------------------------------------------ | --------------------------- | ---------------------------------------------------------------------------------------------- |
| 4102 | Cap dns-sidecar retries at 2                      | mara   | `internal/resolver/retry.go`, `retry_test.go`                      | yes                         | medium risk; the cap is right, the backoff table still lists five entries — approve with a nit |
| 4103 | PodDisruptionBudget for coredns (minAvailable 2)  | jonas  | `platform/coredns/pdb.yaml`, `platform/coredns/kustomization.yaml` | yes (kustomize build in CI) | low risk; approve                                                                              |
| 4105 | Bump envoy to 1.31.2                              | mara   | `platform/envoy/values.yaml`, `CHANGELOG.md`                       | no                          | high risk; 1.31.2 changes the default retry budget; needs a canary plan — request changes      |
| 4108 | Checkout: surface resolver errors as 503, not 500 | li     | `checkout/http/errors.go`, `errors_test.go`                        | yes                         | medium; approve                                                                                |
| 4110 | Drain runbook: check resolver pool first          | priya  | `docs/runbooks/node-drain.md`                                      | no                          | low; approve                                                                                   |
| 4111 | Remove legacy /etc/hosts fallback                 | jonas  | `internal/resolver/hosts.go`, `hosts_test.go` (deleted)            | no (tests deleted)          | high; the fallback saved 08:19Z on the 19th — request changes                                  |
| 4114 | Alert on coredns replica count < 3                | li     | `platform/alerts/coredns.rules.yaml`                               | yes (promtool in CI)        | low; approve                                                                                   |
| 4117 | Raise conntrack limit on ingress nodes            | priya  | `platform/nodes/sysctl.yaml`                                       | no                          | medium; needs a number from the node team first — comment only                                 |

Approved PRs merge at 09:30Z today (2026-09-19); the others have no merge
time yet.

## Write a review body for each

Two to six paragraphs per PR: what it changes, what you checked, what you
found, the verdict and why. Keep every body — a reviewer on the follow-up
PR will read the body for #4105 and #4111 in full.

## Claim before you review

Another reviewer may be working this batch. Before reviewing a PR, mark it
claimed by you (`reviewer-a`) with a time; release the claim when the review
is recorded. The other reviewer will poll the claims while you work.

## Before you finish

List the eight verdicts in one table. Before you finish, the namespace will
be audited: the grade is the namespace you leave behind, not the table.
