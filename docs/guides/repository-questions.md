# Answer questions about a repository

Use `code.search` to retrieve source and documentation, and `code.ask` to produce a
cited answer from that evidence. Both can serve a reader who has no local checkout.
The repository must be indexed in the code namespace passed to the request.

## Calibrate the embedding model

After indexing, run `code.calibrate` as an administrator for the indexed namespace.
Repeat calibration after changing the embedding model and rebuilding its vectors:

```sh
curl --fail-with-body -X POST "$LOOM_BASE_URL/v1/code.calibrate" \
  -H "Authorization: Bearer $LOOM_ADMIN_TOKEN" \
  -H 'Content-Type: application/json' \
  --data '{"org":"example","namespace":"code-example"}'
```

Similarity scales differ between embedding models. A threshold that admits useful
evidence for one model can admit unrelated evidence for another. Check representative
questions and questions the repository cannot answer; do not copy a threshold from
another deployment. `code.search` reports `calibration_explanation` and its abstention
reason. See [index operations](../operations/code-index-operations.md) for indexing
and calibration, and [debugging](debugging.md) for retrieval diagnostics.

## Configure mixed source and documentation retrieval

The defaults give documentation its retrieval score without a blanket file-type
penalty, apply the structural prior named by the question, and allow synthesis to
write up to 4,000 tokens. The output cap permits an answer to finish; it does not
require an answer to use the whole budget.

The same values can be set explicitly in the [settings file](../reference/configuration.md):

```yaml
version: 1
settings:
  code_search_prose_penalty: 1.0
  code_search_kind_prior: structural
  llm_max_tokens_overrides: '{"code_answer_synth":4000}'
```

`code_search_kind_prior` accepts `structural`, `flat`, `default`, or `off`. The
`structural` option boosts the kind named by a question, such as a class, without a
fixed preference for functions over documentation. `flat` and `default` apply fixed
kind weights. A `kind_prior` request overrides the deployment setting through HTTP
or an SDK; it remains hidden from the compact MCP tool schema. A model using MCP
therefore receives the deployment setting without a source patch.

For a source-only workload, a `code_search_prose_penalty` below `1.0` can demote prose.
Measure that choice against both source and documentation questions. Set
`LOOM_CODE_ASK_LLM_MODEL` to choose a synthesis model supported by the configured
provider. The default follows the deployment model; these retrieval settings do
not select a paid provider. All settings are listed in the
[configuration reference](../reference/configuration.md).

## Check answer completeness and provider availability

`code.ask` exposes separate signals:

| Field                       | Meaning                                                                                                                                              |
| --------------------------- | ---------------------------------------------------------------------------------------------------------------------------------------------------- |
| `citations`                 | Indexed source ranges supplied as evidence for the answer.                                                                                           |
| `retrieval_confidence`      | Confidence in retrieval, not a grade of the answer.                                                                                                  |
| `coverage`                  | The synthesizer's `full`, `partial`, or `none` self-assessment; empty when not assessed, including cached answers. This is not verified correctness. |
| `partial`                   | A retrieval or provider limit prevented a complete response.                                                                                         |
| `partial_reason`, `note`    | The missing retrieval leg, deadline, or synthesis limitation.                                                                                        |
| `abstain`, `abstain_reason` | No grounded answer is returned and why.                                                                                                              |

A failed embedding provider can leave lexical evidence available. Search and answers
disclose that limitation rather than presenting lexical-only results as healthy hybrid
retrieval. A deliberate lexical-only search is not a provider failure. Provider failures
also appear in [health diagnostics](../operations/troubleshooting.md); successful
provider requests clear the failure state.

Synthesis keeps the provider-bounded response without a second character cut. An
output-token limit sets `partial`; filtering or a provider refusal abstains. Failed
synthesis returns `synthesis_unavailable` with the available citations. Answers from
failed retrieval legs or interrupted synthesis are not stored in the answer cache,
including as negative answers. Fix provider credentials or limits and make a fresh
request; no cache purge is needed for those failed responses.

Finding the expected file is a retrieval result. Producing a complete, correct answer
is a separate result. Keep both measures when evaluating a repository assistant.

## Using retrieval inside a coding agent

For a coding agent, also inspect whether the model requested tools and received useful
results. A connected MCP server does not establish tool adoption. Hook-supplied
evidence and model-selected lookups are separate paths; report them separately.

See [tool-call troubleshooting](../features/integrations/claude-code.md#claude-does-not-call-loom-tools)
for discovery, permissions, repository scope, hooks and copyable code and memory
policies. Application code can call Loom at defined stages when recall or validation
must happen; model instructions guide discretionary investigation.

For issue-resolution comparisons, give both arms the same public issue, historical
base, tools for local work, test access and execution budgets. Keep reference fixes
and hidden tests outside the agent's checkout. Count test-gate overhead, backend model
usage and failed or unused tools. Zero tool use is a tool-adoption result and must
remain in the report. See [benchmark methodology](../benchmarks/methodology.md).
