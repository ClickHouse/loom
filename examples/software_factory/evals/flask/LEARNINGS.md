# Coding evaluation findings

The evaluation measures submitted fixes, cost and elapsed agent time. A successful
tool call proves delivery of a response, not relevance or correct use. A coding
assistant can still miss a required change after reading its definition.

## Findings and decisions

| Observation                                                            | Diagnosis and decision                                                                                                                                                                                 | Validation                                                                                                                                 |
| ---------------------------------------------------------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------ | ------------------------------------------------------------------------------------------------------------------------------------------ |
| Code-shaped regexes contain unescaped punctuation                      | Pass a list of literal alternatives to `code.grep` for exact fragments. Keep regex validation for intentionally supplied expressions.                                                                  | Three of four Loom runs in the grep body pilot reject the first regex. The next pilot records one invalid regex and no literal-list calls. |
| Agents search, then read matched definitions through a local command   | `code.grep(include_body=true)` returns distinct enclosing bodies with matching lines. The caller chooses the option.                                                                                   | Two attempts in the grep body pilot request and receive six definitions in one call. Both skip a separate local read of those definitions. |
| Several symbol calls appear expensive                                  | Count model turns as well as tools. Parallel symbol calls in one response can be replaced by one batch without removing a model round trip.                                                            | Native trajectory groups and main-session response ids establish the grouping.                                                             |
| A memory arm costs more despite fewer turns and tokens                 | Prompt-cache writes, cache reads and generated tokens have different prices. Attribute each component before blaming memory payload size.                                                              | Reconstructed components reconcile to native reported cost. Initial-cache sensitivity is a supplement, not an isolated cache experiment.   |
| Identical lessons appear on several responses                          | Stateless reads may repeat relevant facts. Count repeated excerpts before deciding whether the caller should opt out of already-read lessons. Do not silently omit facts through hidden session state. | The response contains stable lesson identities and code-link states. Repetition varies by attempt; it is not present in every memory arm.  |
| A hidden callback test fails after relevant code is read               | The patch can be incomplete even when retrieval succeeds. Keep executable grading and inspect the candidate; do not add the hidden requirement to issue prompts or historical memory.                  | Compare the original source, returned context, submitted functions and hidden failure.                                                     |
| Full-suite regressions are zero but hidden success is false            | A base failure can remain unfixed without introducing a regression. Zero regressions does not mean the full suite is green.                                                                            | Report absolute full-suite failures alongside changes from the frozen base.                                                                |
| An interrupted cohort has no final provider ledger                     | Capture each attempt's scoped ledger before cleanup, including failed calls. Missing billing evidence stays unknown.                                                                                   | The harness saves per-attempt diagnostics and captures the ledger at attempt end and before final cleanup.                                 |
| A successful request does not prove that it supplied source or history | Count returned bodies, source characters, elisions, lesson rows and code-link states in the ledger and existing trace spans. Keep content out of these counters.                                       | The projection preserves recorded response counts. Remote trace delivery still requires a configured exporter and a checked request.       |

## Evidence boundaries

In the retained callback pilot, both memory arms receive six file-level lessons
once, totaling 1,364 excerpt characters. One excerpt names a matched definition;
the other five discuss methods outside that matched set. Across 24 retained
responses, an initial projection omitted 12,333 of 31,595 excerpt characters
while keeping direct symbol matches. The certificates do not record pending or
incomplete links. Measure the policy on live responses before attributing any
cost, time or quality change to it.

Grep checks live link sets before omitting an unrelated symbol lesson. Pending
links can have no path, and an enclosing chunk can contain a nested definition.
The focus rule keeps both cases, along with constraints, decisions, pins and broad
file notes. Use `memory.for_file` to retrieve the full file context.

The original upstream hidden callback test also exercises teardown ordering.
The public issue names four earlier callback types. In the grep body pilot the
blind judge rejects the one patch that changes teardown, which is also the only
patch that passes the hidden test. Preserve both verdicts and keep the original
issue, verifier, and history fixed.

The two index tiers use the same source base, issue, model and grader. Prompt-cache
state, call grouping, generated patch and verification choices can differ between
attempts. A difference between parser and compiler arms does not
by itself identify a compiler-index defect.

Historical memory adds sourced facts as well as code links. This comparison cannot
attribute a quality difference to linking alone. One issue with one attempt per
arm cannot establish a general quality benefit or a reliable efficiency gain.
Retain every development result and require separate confirmation before making a
public benefit claim.

## Observability

Native Harbor trajectories supply global tool counts and model usage. Main-session
diagnostics add response grouping, selected tool errors, returned character counts,
and lesson repetition. These are passive readers of recorded artifacts; they do not
change agent execution. API duration includes model, network and queuing time.
The remainder of CLI time includes client and tool work; it is not isolated Loom
server latency. Character counts are not billed provider tokens.

Body retrieval is opt-in. Compare the recorded request flag, body counts and the
next model response before claiming that a combined lookup removed a local read
or a model turn. A smaller tool count alone does not establish either saving.

Loom's ledger supplies request ids, latency, cost and response-size estimates.
Its OpenTelemetry spans supply internal stages. The existing Langfuse integration
forwards these spans when configured; it does not observe the external coding
model automatically. An isolated evaluation without exporter credentials must not
claim a remote trace was captured. Check local exporter state and confirm a known
request in the remote project before relying on that view.

See [automatic reports](../../harbor/README.md), the
[debugging guide](../../../../docs/guides/debugging.md#diagnose-coding-agent-overhead)
and the [native grep body pilot](results/native-grep-body-pilot/README.md) and
[literal-alternatives pilot](results/native-literal-alternatives-pilot/README.md).
