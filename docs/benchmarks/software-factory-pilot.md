# Proposed software factory pilot

This draft measures whether application-owned context retrieval changes objective
resolution, total calls, tokens, cost per successful resolution and elapsed time.
It compares three cold arms under the same tools, graders and resource limits.
It measures controlled context utility; three tasks cannot establish adoption,
savings or a general quality improvement.

The plan is not frozen or approved. Publishing or merging it authorizes no model
calls, index ingestion or paid benchmark. No live result is reported here.

## Implementation and tasks

The [factory implementation](https://github.com/ClickHouse/loom/pull/1050) supplies
offline replay, historical qualification, attempt accounting and paired analysis.
A qualified live runner and deployment-specific evidence remain prerequisites.
The example's offline replay fixtures are separate from the historical pilot.

Use the three historical cases in the implementation's
[`historical-candidates.json`](https://github.com/ClickHouse/loom/blob/feat/flask-software-factory/examples/software_factory/historical-candidates.json):
`pallets__flask-4045`, `pallets__flask-4575` and `pallets__flask-4935`.
Each task pins its exact public source base, dependency image digest, visible
selectors and evaluator-private hidden selectors. Retain all failures. Do not
replace tasks or choose the best attempt after observing results.

Before running, independently verify visible-base pass, hidden-base fail and
reference pass controls on every exact dependency image. Missing selectors,
collection errors and skipped tests invalidate qualification. Imported controls
and image identity alone do not qualify a different runner or grading path.

## Arms and schedule

| Arm          | Application behavior                                                                        | Agent exposure                                |
| ------------ | ------------------------------------------------------------------------------------------- | --------------------------------------------- |
| Baseline     | No context client construction or retrieval                                                 | No Loom schema, instructions or hooks         |
| Orchestrated | Exact intake lookup, impact before changed-source verification, failure retrieval as needed | Ordinary source tools; no selected-call quota |
| Natural      | No automatic context hooks                                                                  | Optional exact lookup with zero minimum calls |

All arms share the same provider/model, native response handling, source tools,
objective graders and per-attempt limits. Only retrieved context and its resulting
trajectory may differ. The treatment cannot change the source base, hidden tests,
prices, schedule, provider or retained attempts.

Use 3 tasks × 3 paired repeats × 3 arms = 27 primary attempts. Add 3 tasks × 2
baseline A/A copies = 6 reference attempts, for 33 attempts total. The 15 baseline
attempts allow zero Loom requests. All attempts start cold; no cross-task memory
writes, warm reuse or chronological learning is part of this pilot.

The [proposed schedule](software-factory-schedule.json) uses seed 741 and persists
all 33 slots before calls. Its `schedule_sha256` covers the canonical attempt
list. Freeze this order with the final protocol; a seed reproduces the schedule
and analysis, not the model's responses. Retain failures, nonuse and budget stops
without replacement attempts, automatic retries or post-hoc exclusions.

## Proposed limits

| Resource                       | Per attempt                                                | Whole primary and A/A cohort |
| ------------------------------ | ---------------------------------------------------------- | ---------------------------- |
| Generation requests            | 12                                                         | 396                          |
| Input tokens                   | 20,000 per generation request, subject to a verified bound | 7,920,000                    |
| Output tokens                  | 2,048 per generation request                               | 811,008                      |
| Agent tools                    | 30                                                         | 990                          |
| Logical Loom reads             | 6 in each context arm; zero in baseline                    | 108                          |
| Physical Loom HTTP requests    | 18 in each context arm; zero in baseline                   | 324                          |
| Serialized transcript          | 64,000 characters per generation request                   | Enforced on every request    |
| Accepted context               | 12,000 characters total                                    | Enforced on every attempt    |
| Visible tests                  | 3 invocations, 30 seconds each                             | No replacement runs          |
| Independent final hidden grade | 1 invocation, 30 seconds                                   | No model judge               |
| Agent policy deadline          | 180 seconds                                                | Serial bounded attempts      |
| Hard controller deadline       | 210 seconds, including hidden grading                      | Retain interrupted attempts  |

The physical Loom cap assumes one retrieval and two single-request scope checks
for each logical read. Auth, discovery, retries, failed dispatches and any extra
checks consume the same cap. Stop before another physical request would exceed
it; additional traffic requires a revised protocol and approval.

The generation request cap excludes a token-counting service. If a verified
counting mechanism needs network requests, freeze a separate ceiling of at most
396 counting requests, their endpoint, latency and cost before approval. Do not
silently replace an unsupported counting API or treat characters as token counts.
The proposed 20,000-input-token bound remains unqualified.

After stopping a controller, reconcile at most four recorded, owned containers.
Each has a 10-second inspection limit and a 10-second removal limit, for at most
80 seconds of additional cleanup. Record end-to-end elapsed time including setup,
independent grading and cleanup. Incomplete cleanup halts the cohort. A timeout
or unresolved in-flight request remains a failure with unknown usage.

## Provider and spending freeze

Use one exact model through a direct Converse provider, with existing authorized
configuration, standard inference, no temperature default and no explicit cache
checkpoints. Preserve native reasoning blocks. Freeze its exact model identifier,
region, endpoint, API behavior and complete dependency hashes before approval.
Do not introduce an alternate gateway, provider or credential route to complete a
run. No infrastructure, permission or credential changes are authorized here.

The working price assumption is $2.20 per million input tokens and $11 per million
output tokens. These assumptions require account-specific verification for the
exact model, region, inference class and cache semantics. They are not a promise
of current pricing or a complete bill.

At the proposed token caps, generation reserves at most
`7,920,000 / 1,000,000 × 2.20 + 811,008 / 1,000,000 × 11 = $26.345088`.
The proposed total ceiling is **$30**, conditional on bounding all counting,
server, transport, auth, cache and infrastructure overhead within the remaining
**$3.654912**. Unknown overhead is unknown, not zero. If verified token bounds or
all-in costs do not fit, revise the plan before requesting approval.

No dollar amount in this draft authorizes spending. Freeze the final protocol
digest, schedule, dependency locks, price table and whole-run reservation, then
obtain actual owner approval referencing that digest and ceiling. Approval of
benchmark intent or of a PR does not replace that approval.

## Qualification required before the first request

1. Pin the merged server/SDK integration and factory controller commits. Check the
   actual integration and its required CI evidence separately.
2. Freeze all public task inputs, hidden grader selectors and patches, source
   allowlists, tool schemas, prompts, runtime locks, controller and trusted worker
   hashes. Keep hidden tests, reference fixes and evaluator credentials outside
   agent access.
3. Qualify a source-only checkout per attempt, with no Git history, future
   objects, sibling mounts, network or credential access. Candidate code runs in
   an isolated focused grader. Verify traversal, symlink, import, subprocess and
   egress isolation. Export only allowlisted implementation edits into a separate
   independent final grader; preserve evaluator tests and runner configuration.
4. Use a private server and database owned by this pilot; never reuse a production
   server. Supply authorized immutable index scopes for all three exact source
   bases using reader credentials. Use exact retrieval only, with no embeddings,
   conceptual search or model enrichment. Verify repository, full commit, server build,
   permissions and every required index tier before and after reads. External
   immutability evidence is mandatory; matching watermarks alone are insufficient.
   Restricted zero counts mean unknown. No index creation or grants are implied.
5. Qualify the actual controller/worker and client transport paths, hard deadlines,
   durable pre-dispatch reservations and cleanup. Baseline constructs no context
   client. Instrument auth and metadata side traffic or refuse it. Failed or
   interrupted attempts retain their reservations and require reconciliation.
6. Verify the input-token bound and complete usage/pricing semantics. Join client
   request IDs to model and server billing evidence. Count failed and retried
   physical dispatches. Never record credentials or arbitrary response headers.
7. Obtain exact protocol and total-spend approval after all evidence exists.
   Refuse inference when a required field or receipt is missing.

Offline contracts, stubbed transports, scripted replay and proposed schedules
are preparation evidence. They establish no live model resolution, complete
billing, index authorization or spending permission.

## Measurement and analysis

Retain every scheduled attempt and its independent objective result, invalid-run
reason, source and patch hashes, provider/index provenance, failures, nonuse,
abstentions and stale, repeated or empty contexts. Separate accepted and failed
calls, host-selected and agent-selected calls, logical reads, physical dispatches
and retries. Avoid counting an agent-selected Loom call twice in tool totals.

Keep raw input, output and cache usage, priced and unknown cost, component latency
and total elapsed time. Cost per successful resolution includes failed attempts;
zero successes has no finite estimate. Unknown usage or pricing suppresses an
all-in cost estimate. Whole-cohort cost includes A/A references.

Pair by task and repeat. Average repeats within each task and use seed 741 for
paired bootstrap intervals that resample task clusters with arms kept together.
Report A/A variation separately. Missing attempts remain visible and suppress
partial-cohort intervals. Three task clusters support exploratory intervals,
with no significance, percentage-gain, general adoption or savings claim.

Freeze original outputs before fixing any observed failure. Diagnose a concrete
pattern, add a focused regression, fix the smallest cause and compare under a
preserved equivalent protocol. Never tune against hidden solutions. Any rerun
needs its own reserved budget and approval; it is not a free replacement attempt.
