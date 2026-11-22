# Workflow audit reporting

The workflow security findings workflow runs a bounded audit on pull requests, main
and release-branch pushes, and a weekly schedule. Pull-request jobs have read-only
contents access. Findings appear as native annotations, a complete job summary and a
complete escaped log record. Display limits on annotations do not limit the full report.

The scanner is pinned with a verified archive checksum. It checks workflow and
dependency-update configuration with the auditor persona, strict collection, no
configuration overrides and no ignored findings. Its offline mode disables scanner
network-context lookups; the job itself runs on GitHub. That mode does not establish
remote action identity, branch protection or every repository security setting.

The audit is report only. Findings do not make its exit status fail; scanner, collection
and reporting errors do. A green audit is not evidence that the repository has no
vulnerabilities. Ordinary repository checks still gate each change.

## Automatic maintenance tasks

Trusted main pushes and the weekly schedule automatically reconcile maintenance issues.
No manual triage or dispatch is required. The job uses its own trusted checkout and a
fresh scan; it never consumes a pull-request artifact, executable or report. Its token
has contents read and issues write permissions. Pull-request jobs receive no issue
write permission.

Only low or informational observations from these maintenance rules can become public
issues:

| Rule                       | Task                                                |
| -------------------------- | --------------------------------------------------- |
| `anonymous-definition`     | Add names that make workflow audit locations clear. |
| `superfluous-actions`      | Review an action that duplicates a runner tool.     |
| `undocumented-permissions` | Explain why an explicit permission is needed.       |

Other rules and higher severity observations remain in the complete audit and are sent
by a separate trusted job to the existing code scanning view. That job has contents
read and security-events write permissions, without issue write access. The public
issue selector is not a scanner ignore or a claim that other findings are harmless.
Security-sensitive reports follow [SECURITY.md](../../SECURITY.md); automation does not
send emails, publish exploit payloads or promise a bounty.

Each task has a stable rule-and-path fingerprint. Line shifts, commit hashes and new
run links do not create another task or status comment. A changed observation count
or determination adds one status comment; absence and later reappearance also update
the latest status. Titles and issue bodies are never rewritten, issues are never
closed or reopened automatically, and an existing closed decision is preserved.
Keep an automation identity marker in the title or body when editing a managed task.
Removing both markers removes its association with the audit.

Publication uses one trusted concurrency group without canceling a running trusted
workflow. A newer pending main run can replace an older pending run; the remaining run
reconciles its complete current report. Each execution permits at most ten issue or
comment writes, forty API calls and three minutes inside the client, with ten-second
socket operations and bounded responses. Deferred work is counted in the job summary
and reconsidered automatically on the next trusted run. API refusals and incomplete
pagination fail the job; there is no retry or alternate credential.

## Qualification

Focused controls use an in-memory API to check deduplication, status transitions,
human edits, closed decisions, sensitive metadata exclusion, failure propagation,
write deferral and the trusted event/permission boundaries. They do not prove a live
token grant or native scheduler execution. The first merged trusted run must verify
actual issue and code scanning publication. Standard public runners and public-repository
code scanning use GitHub's included services; this workflow adds no app, account,
paid runner or provider.
