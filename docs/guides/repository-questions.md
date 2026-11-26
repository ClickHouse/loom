---
title: Repository Q&A without a local checkout
description: Give triage agents, design assistants and team chatbots access to indexed repositories. Retrieve code citations and check answer scope, freshness and completeness.
---

# Answer repository questions without a local checkout

An AI agent can answer questions about a Git repository without cloning it onto the
machine that runs the agent. Loom indexes the repository on the server and returns
source ranges from the branch the caller can read. A triage bot can find where a
retry is handled, a design assistant can look up callers, and a team chatbot can
answer questions with file and line citations.

## Try a question from a machine without the repository

First [add and index the repository](../features/code-index/repositories.md). Give
the bot a Loom identity with viewer access to the intended code namespace. Have the
application choose the namespace from the caller's authorized repositories. The Git
credential used to build the index grants no access to the chatbot's users.
[Repository access rules](../features/code-index/repositories.md#who-can-read)

From any client that can reach Loom, replace the organization, namespace and question
with those of your indexed repository:

```sh
curl --fail-with-body -sS "$LOOM_BASE_URL/v1/code.ask" \
  -H "Authorization: Bearer $LOOM_API_KEY" \
  -H 'Content-Type: application/json' \
  --data '{
    "org": "example",
    "namespace": "code-example",
    "question": "Where is the cache retry delay calculated?",
    "synthesize": false,
    "no_cache": true,
    "rerank": "none"
  }'
```

This returns retrieved `citations` with `path`, `start_line` and `end_line`. The
`answer` field is empty because the request skips synthesis and cached drafts.
`rerank: "none"` skips reranking. Retrieval can still use the deployment's
embedding provider. To let Loom draft the answer, set `synthesize` to `true` with a
[configured server model](../getting-started/models-and-keys.md). Omit `no_cache`
to let Loom reuse a cached draft. The corresponding MCP tool is
[`loom_code_ask`](../reference/operations/code.md#codeask).

Use `as_of` when available to check the indexed branch and commit behind the result.
Check `abstain`, `partial` and their reasons before displaying an answer. To test
the integration, start with a question whose source you can verify, then ask one
the repository cannot answer and check that the client surfaces the abstention or
missing evidence. Use a separate identity without repository access to check that
it cannot retrieve those citations. [Index freshness](../features/code-index/freshness.md)

| Use case                    | Evidence to request                                                                                                                                 |
| --------------------------- | --------------------------------------------------------------------------------------------------------------------------------------------------- |
| Triage a reported behavior  | `code.search` to find relevant code, then `code.symbol` and `code.callers` to inspect a definition and its indexed callers.                         |
| Review a proposed change    | `code.impact` to inspect mapped definitions and tests, and [code-linked team lessons](../use-cases/memory-linked-to-code.md) for earlier decisions. |
| Answer a team chat question | `code.ask` to retrieve citations or produce an answer backed by indexed source ranges.                                                              |

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

The defaults apply a 0.25 multiplier to prose, use the flat chunk-kind prior,
and allow synthesis to write up to 900 tokens. The output cap limits cost and
answer length. A response that reaches the cap discloses incomplete synthesis.

For a mixed source and documentation workload, compare the defaults with this
explicit [settings file](../reference/configuration.md) configuration:

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
