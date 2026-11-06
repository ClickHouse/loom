# Overview

Loom is a server that stores memory, documents, conversations, an entity
graph and a code index for AI agents, as rows in one ClickHouse database. Agents and
people reach it over MCP, HTTP, the Python and TypeScript SDKs, and the dashboard.
It is Apache-2.0. This page helps you decide whether it fits.

## What it does

- **Memory that lasts.** Facts an agent saves are there in later sessions and in other
  tools, one claim per key, with history, provenance and validity. It can be one person's
  memory or shared by many agents and people.
- **Write-time checks.** A near-duplicate merges, a contradiction is flagged, a write
  against a pinned fact is refused.
- **Search that abstains.** `memory.search` returns `abstain=true` with a reason code
  when nothing clears a gate calibrated for your embedder.
- **Audit and erasure.** Every write that changes memory leaves a content-hashed audit
  row. `erasure.request` ends with a residue census over every table the read paths
  touch.
- **A code index with its history.** Search, symbols, callers, impact, PR history with
  review comments and bisect candidates for your repositories, each answer naming the
  commit and index tier behind it. With a tracker source connected, the issue tracker
  too: duplicate checks, issue↔PR links and flaky tests.

Memory and the code index are separate planes on one server. Use either alone or both.
Memory needs a server and a writer token. The code index also needs a repository added
(`LOOM_CODE_REPOS_JSON`, or **Repositories → Add repository** in the dashboard where the
[repository-job sandbox](../operations/code-index-operations.md#repository-job-sandbox) runs) and
`LOOM_CODE_INGEST_ENABLED=true`. The [Claude Code and Codex plugins](connect-a-coding-agent.md)
use both.

## Where namespaces fit

- An **org** is the tenant.
- A **namespace** is the unit of sharing and access inside it. A deployment usually has
  one per team or project, one per bot that must stay isolated, one `personal-<slug>`
  per person (created on first use, and the default for that person's MCP clients), and one `code-<repo>[-<branch>]` per indexed branch.
- Every member of a namespace reads every row in it.
- `agent` records who wrote a row; it is not a boundary.
- `user_id` / `read_scope=own_user` separates end users inside one bot's namespace.
- Access is a role (viewer/writer/admin) on a namespace, carried by a token or added by a
  grant.

[Identities and namespaces](../concepts/identities-and-namespaces.md) has the full model.

## When Loom fits

- You use several AI tools and want them to share what you told any of them, in a
  personal namespace.
- Several tools and people (Claude Code, Codex, Cursor, a support bot, a person at the
  dashboard) need the same memory.
- You want SQL over what your agents know: facts, traces, costs and audit rows are
  ClickHouse tables.
- You already run ClickHouse, or can. It is Loom's only storage dependency.
- An abstain or a flagged conflict is better than a confident wrong answer.
- You need history, provenance, audit or verifiable erasure.
- Coding agents need an index of your repositories.

## When it does not fit

| Situation                                                    | Why not                                                     | Consider instead                                |
| ------------------------------------------------------------ | ----------------------------------------------------------- | ----------------------------------------------- |
| One agent, one process, short-lived memory                   | A server and a database are overhead when nothing is shared | An in-process memory library                    |
| Sub-millisecond key lookups on a hot path                    | Every read is a ClickHouse query                            | A cache in front of your own store              |
| Whole-graph analytics or long paths                          | Traversal is one bounded query per hop, at most four hops   | A graph store, fed by `loom entity-export`      |
| You need an agent framework                                  | Loom does not run the agent loop, prompts or model          | An agent framework, with Loom as its memory     |
| A compliance guarantee out of the box                        | Erasure and audit are engineering primitives                | Your compliance process, using those primitives |
| Production on single-node ClickHouse OSS with no backup plan | No replication or point-in-time recovery                    | ClickHouse Cloud                                |
| Frozen APIs                                                  | Operations and fields change between releases               | Pin a release; check `ops.capabilities`         |

Validity is valid time only (`valid_at`, `valid_to`); what Loom recorded and when lives
in the version history, not a second time axis. Some parts need an operator to turn them
on: the code index, tracker sources, OAuth, telemetry export, and erasure in Langfuse,
ClickStack.

## What each property costs

| Property                        | Mechanism                                                                                               | Cost                                                                                                                          |
| ------------------------------- | ------------------------------------------------------------------------------------------------------- | ----------------------------------------------------------------------------------------------------------------------------- |
| One engine, no second store     | Vector, lexical, graph, filters, audit and erasure are SQL over the same rows                           | Every read is a query. Readers pay `FINAL` on versioned rows. Derived rows (entities, summaries) land later, through workers. |
| Abstain instead of a weak match | Gates derived at boot from the embedder's own cosine distribution (`LOOM_ADAPTIVE_THRESHOLDS`)          | Some weak but correct matches are refused; set `min_similarity` or `floor_mode` per request.                                  |
| Facts that change               | Merge, duplicate, conflict and pinned verdicts on each write; append-only `loom.memory_versions`        | Each write compares against neighbours. A paraphrase can wait in the review queue until someone resolves it.                  |
| Explainable ranking             | `memory.search` with `explain=true` itemises each hit's score; hits carry `source_class` and `verified` | Above `top_k` 15, where `explain` is off by default, `explain=true` makes each hit larger.                                    |
| Sharing with access control     | The identity tuple on every row; tokens and grants carry namespace roles                                | You mint and rotate tokens and grant roles. A cross-principal leakage suite gates every read op in CI.                        |
| Erasure you can check           | A resumable saga ending in a residue census; external backends fail closed                              | An erasure is a multi-step job. A backend that cannot confirm deletion leaves the saga `failed` for manual escalation.        |
| Code answers with freshness     | Parser and compiler (SCIP) tiers; answers carry `as_of.commit` and a stale flag                         | A large repository takes hours to index. The compiler tier needs the toolchain.                                               |

Quality claims are yours to measure. The repository ships the benchmark harnesses and
the raw results behind the published numbers ([results](../benchmarks/results.md)). Run the gates
on your deployment with real providers before relying on a default
([running](../benchmarks/running.md)).

## Decision checklist

Two or more answers in the right-hand column mean Loom is probably the wrong tool.

| Question                                                                  | Points to Loom | Points away             |
| ------------------------------------------------------------------------- | -------------- | ----------------------- |
| Do two or more tools or agents need to share memory?                      | Yes            | No, one process         |
| Can you run ClickHouse and operate a service?                             | Yes            | No, you want it managed |
| Must the data stay in your own infrastructure?                            | Yes            | No preference           |
| Is an abstain or a flagged conflict better than a confident wrong answer? | Yes            | Always return something |
| Do you need history, provenance, audit or verifiable erasure?             | Yes            | No                      |
| Is your hot path a key lookup under a millisecond?                        | No             | Yes                     |
| Do you need whole-graph analytics rather than a few hops?                 | No             | Yes                     |
| Can you absorb API changes between releases, or pin one?                  | Yes            | No                      |
| Will you pay for a real embedder and LLM, and bench on your data?         | Yes            | No                      |

## Prerequisites

| You need                                          | For                                                                                                    |
| ------------------------------------------------- | ------------------------------------------------------------------------------------------------------ |
| ClickHouse (OSS to evaluate, Cloud in production) | Everything. See [ClickHouse requirements](../operations/deployment.md#clickhouse-requirements).        |
| Docker with Compose 2.20 or later                 | The released local stack (one-line installer) and server image.                                        |
| Python 3.12+                                      | Contributor-only `loom up`, server development and operator CLI work.                                  |
| An embedding provider                             | Real retrieval: OpenAI, Gemini, or a local CPU embedder. The stock fake embedder only checks wiring.   |
| An LLM provider                                   | Extraction, `memory.ask` synthesis, `code.ask`: OpenAI or Anthropic.                                   |
| Tokens and namespaces                             | Access. See [tokens and grants](../guides/tokens-and-grants.md).                                       |
| For the code index                                | Git access to the repositories, `LOOM_CODE_GITHUB_TOKEN` for PR history, and time for the first index. |

## Next

[Evaluate Loom](evaluate.md) separates a product smoke test from a real quality
evaluation. [Quickstart](quickstart.md) runs the local stack.
[Deployment](../operations/deployment.md) runs it for a team. The
[documentation map](../index.md) lists every page in reading order.
