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
