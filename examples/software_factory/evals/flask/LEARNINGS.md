# Coding evaluation findings

The evaluation measures submitted fixes, cost and elapsed agent time. A successful
tool call proves delivery of a response, not relevance or correct use. A coding
assistant can still miss a required change after reading its definition.

## Findings and decisions

| Observation                                                          | Diagnosis and decision                                                                                                                                                                                 | Validation                                                                                                                                |
| -------------------------------------------------------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------ | ----------------------------------------------------------------------------------------------------------------------------------------- |
| Code-shaped regexes contain unescaped punctuation                    | Shared validation and grep fallback guidance explain escaping and literal recovery. Do not guess or silently rewrite the caller's intended expression.                                                 | The change is merged. A subsequent pilot still contains an invalid regex; guidance does not guarantee correct arguments.                  |
| Agents search, then read matched definitions through a local command | The source is available, but the composed retrieval workflow competes with a familiar local range read. Offer a direct path from matched lines to full enclosing bodies and verify adoption.           | Batched `code.enclosing` works on recorded locations. The pilot still uses local reads in every Loom arm; efficient Loom use is unproven. |
| Several symbol calls appear expensive                                | Count model turns as well as tools. Parallel symbol calls in one response can be replaced by one batch without removing a model round trip.                                                            | Native trajectory groups and main-session response ids establish the grouping.                                                            |
| A memory arm costs more despite fewer turns and tokens               | Prompt-cache writes, cache reads and generated tokens have different prices. Attribute each component before blaming memory payload size.                                                              | Reconstructed components reconcile to native reported cost. Initial-cache sensitivity is a supplement, not an isolated cache experiment.  |
| Identical lessons appear on several responses                        | Stateless reads may repeat relevant facts. Count repeated excerpts before deciding whether the caller should opt out of already-read lessons. Do not silently omit facts through hidden session state. | The response contains stable lesson identities and code-link states. Repetition varies by attempt; it is not present in every memory arm. |
| A hidden callback test fails after relevant code is read             | The patch can be incomplete even when retrieval succeeds. Keep executable grading and inspect the candidate; do not add the hidden requirement to issue prompts or historical memory.                  | Compare the original source, returned context, submitted functions and hidden failure.                                                    |
| Full-suite regressions are zero but hidden success is false          | A base failure can remain unfixed without introducing a regression. Zero regressions does not mean the full suite is green.                                                                            | Report absolute full-suite failures alongside changes from the frozen base.                                                               |
| An interrupted cohort has no final provider ledger                   | Capture each attempt's scoped ledger before cleanup, including failed calls. Missing billing evidence stays unknown.                                                                                   | The harness saves per-attempt diagnostics and captures the ledger at attempt end and before final cleanup.                                |

## Evidence boundaries

The two index tiers use the same source base, issue and grader. Model choice,
prompt-cache state, call grouping, generated patch and verification choices can
differ between attempts. A difference between parser and compiler arms does not
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

Loom's ledger supplies request ids, latency, cost and response-size estimates.
Its OpenTelemetry spans supply internal stages. The existing Langfuse integration
forwards these spans when configured; it does not observe the external coding
model automatically. An isolated evaluation without exporter credentials must not
claim a remote trace was captured. Check local exporter state and confirm a known
request in the remote project before relying on that view.

See [automatic reports](../../harbor/README.md), the
[debugging guide](../../../../docs/guides/debugging.md#diagnose-coding-agent-overhead)
and the [five-arm pilot results](results/native-five-arm-pilot/README.md).
