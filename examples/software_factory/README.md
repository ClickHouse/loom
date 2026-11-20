# A small coding factory

This example runs a bounded coding agent against a frozen task, verifies its
source edits with objective checks, and records every attempt. Application code
owns the context policy. A scripted replay makes the entire offline path runnable
without network, credentials, a database or third-party packages.

The replay is not model reasoning. Its patches and outcomes measure neither
quality nor savings, adoption or end-to-end live performance. The same loop has
injected Bedrock Converse and Loom SDK adapters; neither is enabled by this CLI.

## Run offline

From the repository root with Python 3.12.13:

```bash
python -m examples.software_factory run \
  --task name-validation --mode orchestrated --output /tmp/factory-one
python -m examples.software_factory smoke --output /tmp/factory-smoke
python -m examples.software_factory compare \
  /tmp/factory-smoke/name-validation-baseline/result.json \
  /tmp/factory-smoke/name-validation-orchestrated/result.json \
  --output /tmp/factory-comparison.json
python -m unittest examples.software_factory.tests.test_factory -v
```

Choose unused output directories. Commands refuse to overwrite an attempt.
On a host with a required admission supervisor, put that supervisor before each
command, including unit tests and smoke. The job's local validation receipts
record the exact supervisor invocation.

The three tasks are authored fixtures covering name validation, path composition
and HTTP method normalization. Each replay inspects source, observes a failure,
applies an incomplete edit, observes another failure, revises the edit and passes
visible checks. Independent hidden checks decide the final result. Frozen hashes
in `suite.json` cover tasks, replay actions and grader inputs. The agent receives
the problem and source tools; it cannot read the replay, grader or suite files.

`historical-candidates.json` separately describes three pinned public Flask
cases and imported objective-control receipts. These are **not** replay tasks,
current environment qualifications or factory resolutions. Its dependency image
digests and selectors support a later independent qualification. No public source
checkout, reference patch or hidden solution is copied into the agent checkout.
The source repository is [pallets/flask](https://github.com/pallets/flask), with
pytest configuration in its [project file](https://github.com/pallets/flask/blob/main/pyproject.toml).

## Modes

| Mode           | Application behavior                                                                           | Agent behavior                                             |
| -------------- | ---------------------------------------------------------------------------------------------- | ---------------------------------------------------------- |
| `baseline`     | No context backend or requests                                                                 | No Loom schema or instructions                             |
| `orchestrated` | Bounded intake lookup; impact before verifying changed source; context after failure as needed | Uses ordinary source tools; need not select any Loom calls |
| `natural`      | No automatic context hooks                                                                     | Optional exact Loom lookup; zero calls is a valid outcome  |

The standard replay selects zero Loom calls in every mode. Orchestrated replay
still exercises the host lookup path using fixture responses. Contract tests
also exercise a natural agent-selected lookup; they impose no adoption quota.
Physical replay network requests are zero. Live host requests and model-selected
requests are distinct counters. Never equate zero selected calls with zero host
requests.

## How the loop works

`engine.py` owns bounded provider turns, source tools, verification and the final
independent grade. The provider can plan in text, read/search source, edit exact
fragments, run fixed tests and revise. It cannot choose its grader or run shell
commands. A final visible verification runs even if the provider submits early.
A completed attempt resolves only if both visible and hidden checks pass for the
final source state. Budget stops remain unresolved even if a partial patch happens
to pass hidden checks.

`context.py` uses exact symbol bodies with a token budget. Conceptual search is
not implemented because these tasks name a symbol. Intake, impact and failure
hooks route by local source state. Identical requests reuse evidence; a changed
failure with unchanged source does not repeat an already sufficient lookup.
Changed source triggers new evidence checks. Every returned body must match its
local path and line span exactly. Stale index bodies are rejected; working edits
are never described as present in the main-branch index. Abstention remains
explicit. Permission, repository, namespace or full-commit uncertainty stops an
attempt. No path creates indexes, grants access or writes memory.

`workspace.py` limits reads and edits to the manifest's source allowlist. Each
attempt has its own source-only checkout without git history or future objects.
The offline evaluator parses a finite scalar-expression subset: no Python
`exec`, imports, filesystem, network, subprocess or arbitrary calls. This is an
objective semantic grader for these authored functions, not a Python sandbox or
a substitute for Flask pytest. Hidden checks stay in the controller and are
never appended to the agent conversation. Do not add a host subprocess runner
for untrusted candidate code. Historical execution needs the isolated runner
specified in [the pilot protocol](PILOT.md).

## Provider and SDK extension points

A provider implements `step(messages, tools, max_tokens)` and returns native
Converse content blocks plus usage, physical request count, request id and cost
status. Messages contain tool results, so subsequent decisions can respond to
failures. `ReplayProvider` supplies scripted actions; `BedrockProvider` calls an
injected client's real `converse` API with the same tools. It requires an explicit
approved model id, bounded client timeouts and disabled SDK retries. It sends no
temperature. It preserves raw input, output and cache usage; unknown pricing
remains unknown. Importing adapters creates no client and reads no credentials.

`SDKContext` reuses `loom.Agent.call_with_meta` with a one-attempt retry policy.
Construct that Agent with ambient runs and mentor requests disabled. The caller
must supply an independently qualified index-scope verifier that checks the
same immutable namespace before and after each call. The op has no arbitrary
historical commit selector, so sending an invented commit field is not scoping.
The verifier must return the exact org, namespace, repository and commit plus
physical request counts. Mutable, restricted or unverified scopes fail closed.
A qualified verifier and server usage-ledger join are deliberately prerequisites
for live work; no fixture receipt qualifies them.

To plug in a provider, load a frozen `Task` and `ObjectiveGrader`, construct a
`Factory` with the provider and mode, then call `run()`. Supply no backend in
baseline. Supply a qualified retrieval adapter in the other modes. The CLI
accepts only the frozen offline suite. A live controller must implement the
approved protocol, pricing reservation and historical sandbox; adding credentials
to the replay command cannot turn it into a paid run.

## Budgets and evidence

Defaults are 18 turns, 30 source tools, 6 logical context requests, 12,000 context
characters, 64,000 transcript characters per request, 2,048 output tokens per
turn and 60 seconds. Source files and provider outputs also have size caps.
Policy and event routing are deterministic; real model trajectories are not.
No model capability is inferred from an opaque model id.

Each attempt writes `events.jsonl`, `candidate.diff` and `result.json`. Events
include inputs, decisions, tool outcomes, source states, context grounding,
request ids, latency and content hashes. Result manifests separate logical and
physical requests, retries, host and selected calls, provider and retrieval usage,
cost and unknown cost. Live server-side provider overhead requires a billing
ledger join; the adapter reports unknown rather than zero. A failed request whose
transport count is unavailable reports an unknown physical count.

Comparison output retains all supplied attempts, including failures and nonuse.
Cost per successful resolution includes failed-attempt costs and becomes unknown
if any cost is unknown. Zero successes has no finite cost-per-success estimate.
Replay dollar totals are zero **offline expense**, not evidence of live savings.
No statistical inference or significance estimate is produced. Cold attempts have
no cross-issue memory or warm learning path.

## Before live use

Read [the proposed pilot](PILOT.md). It specifies paired randomized repeats,
A/A noise, objective graders without a judge, dependency and sandbox qualification,
provider exclusions, billing evidence and explicit count ceilings. The owner must
approve the exact frozen protocol before any actual model cohort. No live model,
index ingestion, judge, native plugin or provider gateway is invoked here.

The SDK adapter also requires an HTTP request counter attached to its transport.
Counts are measured across both scope checks and the retrieval, including a
failed request. Scope-check receipts are retained separately. A missing server
provider ledger still leaves cost unknown even when transport counts are known.
Use `SDKContext.close()` in the controller's `finally` block. The adapter keeps
one async runner across requests so the injected SDK transport stays on its
own event loop; closing it closes that Agent and the runner. Inject an unopened
Agent dedicated to this adapter.
The adapter's `expected_scope` must name the frozen org, namespace, repository
and full commit. The preflight compares all four before issuing the code request;
postflight compares them again before releasing evidence.

## Historical qualification without inference

`historical.py` supplies a trusted exact-base blob exporter and a focused
container grader. The agent receives only ordinary implementation source, with
no object database. Edits, hashes and grounding use complete files; agent reads
are limited to 16,000 characters and accept an optional 1-based `start_line`.
Files are limited to 1 MiB. Display limits never truncate source authority.

The historical grader builds an independent checkout for each invocation,
overlays only allowlisted implementation files, and preserves tests and runner
configuration. Hidden tests are applied only after the agent finishes. Test
containers use an existing image digest, no network, a read-only root and source
mount, a separate results mount, one CPU, 512 MiB and 64 processes. Tests cannot
change their source or evaluator configuration. Only fixed focused pytest nodes
are accepted. Missing tests, collection errors, skips and duplicate JUnit ids
fail qualification. Candidate code executes inside that container, never in the
controller process. Container isolation is a prerequisite to verify on the host;
it is not established by a mocked command-construction test.

The qualification CLI consumes independently frozen controller inputs. It checks
their hashes against the historical manifest and runs public-base, hidden-base
and private-reference controls. Reference patches stay outside agent access.
Visible selectors come from tests present at the public base; the complete
frozen hidden selector list remains mandatory.

```bash
python -m examples.software_factory.qualify \
  --task pallets__flask-4045 --config /private/grade-pallets__flask-4045.json \
  --output /private/factory-controls
python -m examples.software_factory.qualify_sandbox \
  --image sha256:REPLACE_WITH_FROZEN_LOCAL_IMAGE --output /private/factory-isolation
```

Use the required local supervisor around these finite checks. No image pull,
provider call, ingestion or model judge occurs. Each container has an exact-name,
label-verified ownership receipt. Normal exits clean it in `finally`. After a
supervisor interruption, reconcile those receipts before resuming:

```bash
python -m examples.software_factory.cleanup --root /private/factory-controls
```

Cleanup refuses another owner's label. Preserve failed qualification directories
and choose a fresh output path for a diagnosed rerun. These controls qualify an
environment; they are not agent resolutions or benchmark results.

`live_support.py` provides the exact scope/build verifier, dispatch event hooks,
a reproducible paired schedule and global token/request/cost reservations.
A failed request retains its reservation. Unknown cache or usage semantics stop
settlement; a fixture cannot supply real billing evidence. Scope verification
requires an external immutable-index/authorization certificate and compares the
full repository, namespace, commit and server build before releasing evidence.
Matching watermarks do not establish immutability by themselves.

These pieces can be injected into `Factory` with `HistoricalGrader`, but do not
constitute an approved live cohort command. The controller still needs qualified
provider/dependency locks, verified token bounds, integrated hard supervision,
server billing joins and actual protocol approval.
The offline CLI never constructs live clients. Provider failures and invalid
hidden graders retain failed result manifests, counts and unknown costs.

The shared intake names the public repository/base, named symbol and allowed
source paths so an unscripted provider can discover files. It exposes no grader,
replay, namespace or history. `supervision.run_bounded` can enforce a hard deadline
for one trusted controller process, retain its output and timeout receipt, and
reconcile its recorded containers. It pins the process-group leader until cleanup
so a reused PID cannot identify another owner's group. This is a controller
utility; it is never an agent tool or permission to launch a paid cohort.
`TransportAudit` accepts a physical request cap and a durable recording callback;
failed I/O consumes a dispatch, and a request beyond the cap is refused before I/O.

`cohort.Cohort` persists the protocol digest, exact schedule, every attempt and
global model reservations in an fsynced journal under an exclusive file lock.
`ReservedProvider` writes the reservation before calling an injected provider,
then saves raw usage and settles only explicitly priced fields. Model cost is
labelled generation-only; server and transport overhead remain separate.
A failed call retains its worst-case reservation. A restart refuses an active
attempt or unsettled usage instead of repeating it; a changed protocol or
partial journal also refuses. Preserve those receipts for explicit controller
reconciliation. No automatic retry or journal repair is provided.

The caller supplies an eligibility verifier before each attempt. That verifier
must check the actual owner approval, merged integration and frozen dependencies;
this accounting primitive cannot establish permission by itself. It constructs
no provider or Loom client and grants no spending permission. Connecting this
journal, the process supervisor, live transports and complete billing into one
qualified cohort entrypoint remains required before live execution.

Transport events retain bounded request IDs for joining client dispatches and
responses to usage receipts. HTTP records use only `x-request-id`; Bedrock
records use only parsed `ResponseMetadata.RequestId`. Other headers and response
content are excluded. Missing or malformed IDs remain unknown; correlation
alone does not establish complete billing or a zero cost.

## Analyze the proposed pilot

```bash
python -m examples.software_factory paired \
  --schedule /private/proposed-schedule.json \
  --attempts /private/controller-attempts.json \
  --output /private/paired-report.json
```

The schedule contains `attempts` and their `schedule_sha256` from the proposal.
The controller export is a JSON list of `{"order": 0, "result": {}}` entries,
where each result is the complete evaluator-owned attempt manifest. The command
constructs no clients. Duplicate slots, changed task/provider labels and baseline
Loom exposure refuse analysis. Missing attempts remain visible and suppress
partial-cohort intervals. Natural nonuse and objective failures remain included.

The report pairs the three repeated arms by task and repeat and separates A/A
reference pairs. It averages within each task before using the repository's
`bench.bootstrap.paired_bootstrap` helper to resample tasks. Its intervals are
exploratory: three task clusters cannot establish significance, savings or
adoption. The report never certifies input provenance or grants benchmark claims.
An analysis seed reproduces the report, not the model's trajectory.

Arm costs include failed attempts; unknown prices and partial usage stay unknown.
Whole-cohort cost includes the A/A attempts. Tool totals combine agent requests
and logical host lookups, without counting model-selected Loom twice; physical
provider/Loom requests remain separate. Raw provider token fields retain their
names and missing cache counts stay unknown. `attempt_duration_s` comes from the
factory manifest; complete elapsed time requires a separate controller-supplied
`end_to_end_s` including setup, hidden grading and cleanup. It is unknown when
absent. Authored contract inputs exercise the analysis but measure no live effect.
