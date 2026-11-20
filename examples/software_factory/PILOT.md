# Proposed live pilot protocol

This protocol is a proposal. It authorizes no provider calls, ingestion, judge calls
or historical candidate execution. The offline replay results cannot qualify it.

## Question and arms

Measure whether application-owned exact context changes objective resolution,
total cost per successful resolution, and latency on pinned historical tasks.
This measures controlled context utility. It does not establish organic adoption.

Use three arms: baseline, orchestrated Loom, and natural agent-selected Loom.
Baseline receives no Loom backend, schemas, instructions, hooks or SDK setup.
The natural arm has an optional exact lookup tool and no minimum call count.
The orchestrated arm uses the intake, changed-diff and failure-context hooks.
All arms share the same agent tools, provider, tests and task budgets.

Candidate tasks are 4045 (name validation), 4575 (redirect customization feature),
and 4935 (nested subdomain routing), as pinned in `historical-candidates.json`.
The imported receipts record individually valid controls for these tasks; the
whole prior cohort is incomplete. Case 5063 is excluded because inspected controls
are incomplete. Do not infer qualification from an image tag or a prior summary.

## Qualification and freeze before approval

Historical admission is blocked by candidate-controlled pytest result authority.
`HistoricalGrader` is diagnostic; the factory and worker refuse it before client
construction, and the qualification CLI refuses it before container execution.
Replace it with an oracle whose assertions and result authority remain outside
candidate control, then independently qualify that boundary. Imported controls
and filesystem probes cannot satisfy this requirement.

1. Freeze the implementation commit, all agent-visible inputs, tool schemas,
   prompts, output limits, exact provider model id, API version and complete
   dependency locks. Select a direct Bedrock Converse model supported by the
   account and approve that exact id. Exclude GPT Luna, internal gateways and
   Cohere. Do not infer capabilities from an alias. Send no temperature default.
2. Freeze a currently available dependency image by digest for each historical
   base. Independently run visible-base pass, hidden base fail and reference
   pass controls; require every selected test to collect and reject errors,
   missing selectors and skips. Reference patches stay evaluator-private.
   Imported controls and image digests are evidence to inspect, not permission
   to reuse an invalid environment. No full local suites on the shared host.
3. Qualify a sandbox: one source checkout, no `.git`, alternates, future objects,
   reference fixes, grader files, credential files, network or sibling mounts.
   Candidate tests run without evaluator authority. Mount only immutable runtime
   dependencies. Verify traversal, symlink, import, subprocess and egress attacks
   fail. Use a separate independent evaluator checkout for hidden grading; apply
   only source allowlisted changes, preserving tests and runner configuration.
4. Authorize existing immutable index namespaces for each repository/base using
   reader credentials only. Verify full commit and repository identity before
   and after reads. Zero restricted counts mean unknown. Do not grant access or
   create an index. Missing scope, mutable watermarks or provider-dependent name
   resolution blocks the affected arm. The supplied SDK adapter needs this
   deployment-specific verifier, with every physical check in its receipt.
5. Freeze a price table and provider/retrieval billing evidence. Unknown cost is
   unknown, including server embedding/rerank overhead. Reject a paid cohort if
   a conservative whole-run reservation cannot be priced. Disable retries in
   the model SDK and count all scope checks, network attempts and failures.
6. Show the exact frozen protocol and maximum reservation to the owner and obtain
   new approval. No broad earlier authorization starts this cohort.

## Counts and budgets proposed for approval

Pair by task and repeat. Use 3 tasks × 3 repeats × 3 arms = 27 primary attempts.
Add baseline A/A using 3 tasks × 2 identical baseline copies = 6 attempts, for
33 total. Shuffle arm order within each task/repeat using a published seed and
persist the complete schedule before running. Keep all failures and nonuse;
no best-of selection, retries as replacement attempts, or post-hoc task removal.

Per attempt: at most 12 provider turns, 30 agent tools, 6 logical context reads,
2,048 output tokens per turn, 64,000 serialized transcript characters per turn,
12,000 context characters total, 180 seconds wall time in the qualified runner,
and 3 candidate test invocations of at most 30 seconds each. The authored demo
uses its own defaults; freeze the historical runner's distinct budgets first.
The SDK has no automatic retries: at most 18 physical Loom requests per attempt
if every logical read needs two one-request scope checks. If a verifier needs
more, revise the exact protocol before approval. Max provider requests: 396;
max output tokens: 811,008; max logical Loom requests: 108; max physical Loom
requests under that verifier: 324. The 18 nonbaseline attempts each allow six
logical reads; the 15 baseline attempts have zero retrievals. Price input tokens using the
frozen tokenizer and full transcript, including repeated tool results. Propose a
$25 total reservation only if that worst-case computation fits; otherwise reduce
the design and show the revised plan. No spend is authorized by this number.
No judge is needed: objective graders decide patch correctness.

## Analysis and boundaries

Report objective resolutions, regressions, invalid runs, all costs including
failed attempts, cost per successful resolution, latency, host calls, selected
calls, physical requests/retries, provider input/output/cache tokens, unknown
usage and retrieval overhead. Join client request ids to server billing evidence.
Report costs as unknown when the join or pricing is incomplete.

Use paired task-cluster bootstrap confidence intervals with a frozen seed and
resample tasks, retaining paired arms and repeats inside each cluster. Report
A/A variation separately. Three task clusters are a workflow pilot, not grounds
for significance or percentage-gain claims. Expand independent tasks before any
adoption, savings or general quality conclusion.

Each attempt starts cold. There is no cross-issue memory write path or warm
learning arm. A future chronological study needs a separate freeze and may save
only independently verified facts. Workflow determinism means event routing;
model trajectories and patches remain stochastic.

## Authored oracle proposal

The optional host behavioral oracle covers the three public bug surfaces with
parameterized API observations. Assertions and final grades remain outside the
candidate container. It requires fresh base/reference controls and independent
review. Its narrower authored coverage is not the official pytest selector
coverage described by the diagnostic artifacts. A pilot using this oracle must
freeze that change explicitly, including probe seeds and grader/receipt hashes,
before approval; no existing draft or diagnostic control authorizes substitution.
