<h1 align="center">ClickHouse Loom</h1>

<p align="center">
  <b>Shared memory, document search and code intelligence for AI agents.</b><br>
  For Claude Code, Codex, Cursor and your own agents.
</p>

<p align="center">
  <a href="https://pypi.org/project/clickhouse-loom/"><img alt="Python SDK on PyPI" src="https://img.shields.io/pypi/v/clickhouse-loom?label=python%20sdk"></a>
  <a href="https://www.npmjs.com/package/@clickhouse/loom"><img alt="TypeScript SDK on npm" src="https://img.shields.io/npm/v/%40clickhouse%2Floom?label=typescript%20sdk"></a>
  <a href="./LICENSE"><img alt="License: Apache-2.0" src="https://img.shields.io/badge/license-Apache--2.0-blue"></a>
  <a href="https://glama.ai/mcp/servers/ClickHouse/loom"><img alt="MCP server score" src="https://glama.ai/mcp/servers/ClickHouse/loom/badges/score.svg"></a>
</p>

<p align="center">
  <!-- languages -->
  English · <a href="./i18n/zh-CN/README.md">简体中文</a> · <a href="./i18n/ja/README.md">日本語</a> · <a href="./i18n/ko/README.md">한국어</a> · <a href="./i18n/pt-BR/README.md">Português (Brasil)</a> · <a href="./i18n/es/README.md">Español</a> · <a href="./i18n/ru/README.md">Русский</a>
  <!-- /languages -->
</p>

<p align="center">
  <a href="#what-loom-is-for">What Loom is for</a> ·
  <a href="#measured-results">Benchmarks</a> ·
  <a href="#get-started">Get started</a> ·
  <a href="#why-loom">Capabilities</a> ·
  <a href="#integrations">Integrations</a> ·
  <a href="https://clickhouse.github.io/loom/">Documentation</a>
</p>

<p>
  <img src="./docs/assets/screenshots/memories.webp" width="32%" alt="Loom dashboard showing shared agent memories, a pinned fact and flagged conflicts">
  <img src="./docs/assets/screenshots/document-search.webp" width="32%" alt="Library search: three passages from a refunds policy, an on-call runbook and an FX note, each cited by document, revision and lines">
  <img src="./docs/assets/screenshots/code-callers.webp" width="32%" alt="A repository's Explore tab: payments.fx.convert with its definition and callers">
</p>

Loom is an open-source, self-hosted MCP server that gives your agents long-term, shared
memory: what one agent learns in a session, the next agent or tool can find.

Agents connect through MCP, HTTP, Python or TypeScript. Self-host with Docker or
[on Kubernetes with the Helm chart](./docs/operations/kubernetes.md); agent context and
index data live in [ClickHouse](./docs/use-cases/self-hosted-clickhouse.md), and a
deployment can choose [Postgres at first boot](./docs/operations/postgres.md) for live
memory and knowledge state. The dashboard brings memories, repositories, tasks and runs
into one place.

## What Loom is for

- **Long-term memory shared across your coding agents**, with plugins that load context
  at session start and capture the session as it runs.
  [Agent memory](./docs/use-cases/agent-memory.md) ·
  [Connect a coding agent](./docs/getting-started/connect-a-coding-agent.md)
- **Memory for an agent application**, per user, from the Python or TypeScript
  SDK. [Your first agent](./docs/getting-started/first-agent.md)
- **A multi-user MCP memory server** with namespaces, roles and tokens.
  [Multi-user mode](./docs/getting-started/quickstart.md#start-in-multi-user-mode) ·
  [Namespaces and sharing](./docs/features/namespaces-and-sharing.md)
- **A code index of your repositories** that coding agents query for definitions,
  callers and change impact. [Code index](./docs/use-cases/code-intelligence.md)
- **Lower model cost for coding agents**, as [measured](#measured-results) below.
  [Coding-agent cost](./docs/use-cases/coding-agent-cost.md)
- **Document search with citations (RAG)**, including PDF, `.docx` and `.pptx` files the
  extractor service converts. [Document search](./docs/use-cases/document-search.md)
- **Shared context for multi-agent workflows**: shared memory, task inboxes and
  decisions. [Multi-agent](./docs/use-cases/multi-agent.md)
- **Memory, recall, audit and erasure for autonomous agents.**
  [Autonomous agents](./docs/use-cases/autonomous-agents.md)
- **A record of agent work**, with searchable conversations (when recording is on),
  session briefs to resume from, runs with their goals, outcomes and model costs when
  supplied, and PR and tracker history.
  [Runs and PR history](./docs/use-cases/runs-and-pr-history.md)

Every use case, and when another tool fits better: [use cases](./docs/use-cases/index.md).

Loom is not an in-process library for one agent's short-lived memory, nor an agent
framework. [When Loom does not fit](./docs/getting-started/overview.md#when-it-does-not-fit)

## Measured results

| Evaluation                     | With Loom | Without Loom | Efficiency gain                    | Scope                              |
| ------------------------------ | --------- | ------------ | ---------------------------------- | ---------------------------------- |
| Coding-agent answer quality    | **0.84**  | 0.78         | **20% lower** model cost           | 24 questions × 3 paired runs       |
| Coding-agent file localization | **96.4%** | 91.4%        | **21% lower** model cost           | 36 questions × 10 paired runs      |
| Memory vs full-history answers | **94%**   | 90%          | **91% fewer** reader prompt tokens | 100 paired LongMemEval-S questions |

Both coding setups use Claude Code (`claude-sonnet-5`) on the ClickHouse codebase;
cost is the whole session's model bill, excluding Loom's server-side model calls, hosting
and indexing. Answer quality
measures coverage of a verified answer's key points, with a contradiction penalty.
File localization counts whether the right file is among the up to three files the agent is
asked to name at the end of its answer.

The memory row compares retrieved memories with full conversation history using
the same `gpt-5` reader. Token savings exclude memory construction; the 94% vs 90%
accuracy difference is descriptive, not statistically established.

**Separately, Loom's `memory.ask` configuration scored 96.6% answer accuracy on all
500 LongMemEval-S questions.** This is a product evaluation with its own configured
answer models, not the fixed-reader comparison above.

Results are specific to the tested workloads.

[Protocols and reproducible artifacts](./docs/benchmarks/results.md) ·
[detailed results](#benchmark-details) ·
[evaluate on your workload](./docs/getting-started/evaluate.md)

## Get started

### 1. Choose your Docker setup

- **Personal use—the default:** no login, localhost-only access and a `dev/local`
  namespace. One person can connect multiple agents.
- **Team use:** authenticated accounts and namespace access controls.
  [Start in multi-user mode from the first boot](./docs/getting-started/quickstart.md#start-in-multi-user-mode).

Both modes support the same Loom features, including configured LLMs and embeddings.
Single-user is not a reduced-feature edition. You can also
[switch an existing installation without deleting its data](./docs/getting-started/quickstart.md#2-open-the-dashboard);
grant new accounts access to the existing namespaces.

To try it with nothing installed, open it in a codespace:

[![Open in GitHub Codespaces](https://github.com/codespaces/badge.svg)](https://codespaces.new/ClickHouse/loom?quickstart=1)
([what it runs and who can reach it](./docs/getting-started/quickstart.md#try-it-in-github-codespaces))

For the default personal installation, with Docker installed (on Windows, in a WSL 2
terminal where `docker` works: Docker Desktop with its WSL integration on, or Docker Engine
installed in the distribution):

```bash
curl -fsSL https://github.com/ClickHouse/loom/releases/latest/download/install.sh -o loom-install.sh && sh loom-install.sh && rm loom-install.sh
```

Run it from your home directory, or another
[directory only you can write to](./docs/getting-started/quickstart.md#fast-path-released-image-no-clone).
The installer starts Loom and ClickHouse, applies migrations and prints the dashboard URL:
**http://localhost:7777/**; on a new install it then checks that a memory saves and reads
back. On a terminal it also offers to connect the coding agents it finds (Claude Code, Codex,
VS Code) and, on a new install, opens the dashboard. The image includes the server,
workers, dashboard and Python, TypeScript and Go compiler indexers, plus the C++ one on
`amd64`; TypeScript dependencies are prepared automatically. [Quickstart and pinned releases](./docs/getting-started/quickstart.md)

The installation lives in `~/.loom` (`LOOM_INSTALL_DIR` chooses another directory): the
Compose file and the startup settings in `~/.loom/.env`. Running the install command again
upgrades in place and keeps your `.env` settings.

Keep single-user mode localhost-only. For shared access, follow the
[team deployment guide](./docs/operations/deployment.md#identity-modes), including TLS.

### 2. Configure models and API keys

**The stock installation boots without keys or network.** Storage, the dashboard,
structural code indexing and semantic search work, on a small local embedding model the
image carries; the fake LLM gives no AI-generated answers.

| Component  | Configure for real use                             | Without configuration                                                       |
| ---------- | -------------------------------------------------- | --------------------------------------------------------------------------- |
| Embeddings | Provider, embedding model, dimensions and API key. | `local` (bge-small on CPU): semantic retrieval, weaker than a hosted model. |
| LLM        | Provider, model and API key.                       | `fake-echo-1`: no meaningful extraction, summaries or answers.              |

Open **Settings → Models & keys**, choose both providers and models, enter their
keys, then click **Test & apply**. No Docker restart is needed; changing the embedding
space rebuilds the existing index. The dashboard's embedding choices are hosted
providers: to keep `local` and add an LLM, set the LLM lines in `~/.loom/.env` instead
([Models and keys](docs/getting-started/models-and-keys.md#claude-for-the-llm)).

**One OpenAI API key can serve both roles.** Claude needs a separate embedding provider.
OpenAI, Gemini and OpenAI-compatible embedding endpoints are supported. Your agent's
CLI login or subscription does not supply Loom's provider keys.

Prefer environment configuration? Edit `~/.loom/.env`, then recreate the server:

```bash
docker compose --project-directory "$HOME/.loom" up -d --force-recreate loom
```

A key alone does not change the pinned models (the local embedder and the fake LLM); set
the provider and model too.
Saved dashboard settings take precedence over `.env`. Follow
[models and API keys](./docs/getting-started/models-and-keys.md) for provider recipes
and health checks.

**Everything else is in one file.** [`loom.example.yaml`](./loom.example.yaml) lists
every setting with its default and what it does, by area: the ClickHouse connection,
timeouts and budgets, rate limits, workers, and the `*_enabled` switches that turn
features on and off. Set any of them as `LOOM_<NAME>` in `~/.loom/.env` and recreate the
server; a deployment that mounts a config file can set them in a copy of this file
instead ([how settings are read](./docs/operations/deployment.md#how-settings-are-read)). One marked
`live` also changes while Loom runs: `loom settings set <name> <value>`, or Admin → Deployment
settings. `loom settings list --changed` shows what differs from the defaults and
where each value came from ([runtime settings](./docs/operations/runtime-settings.md)).

### 3. Connect your agents

If the installer connected your agent, skip the commands below. On the machine running a
personal installation, the plugins need no token:

```bash
# Claude Code
claude plugin marketplace add ClickHouse/loom
claude plugin install loom@loom

# Codex
codex plugin marketplace add ClickHouse/loom
codex plugin add loom@loom
```

Start a new session; the first one connects to `http://localhost:7777`. In Codex, run
`codex` interactively once and approve the plugin's hooks; until then only the tools
work. Tell the agent something worth keeping, then ask about it in the next session.

For Cursor on the machine running a personal installation, the button below adds the
[local MCP entry](./docs/features/integrations/cursor.md#mcp-tools), which needs no token:

[![Add Loom to Cursor](https://cursor.com/deeplink/mcp-install-dark.svg)](https://cursor.com/en/install-mcp?name=loom&config=eyJ1cmwiOiJodHRwOi8vbG9jYWxob3N0Ojc3NzcvbWNwP3Rvb2xzZXQ9Y29kaW5nIn0%3D)

For VS Code agent mode on the machine running a personal installation, the buttons below
add the [local MCP entry](./docs/features/integrations/vscode.md#local-single-user-server),
which needs no token:

[![Add Loom to VS Code](https://img.shields.io/badge/VS_Code-Add_Loom-0098FF?logo=visualstudiocode&logoColor=white)](https://vscode.dev/redirect?url=vscode%3Amcp%2Finstall%3F%257B%2522name%2522%253A%2522loom%2522%252C%2522type%2522%253A%2522http%2522%252C%2522url%2522%253A%2522http%253A%252F%252Flocalhost%253A7777%252Fmcp%2522%257D)
[![Add Loom to VS Code Insiders](https://img.shields.io/badge/VS_Code_Insiders-Add_Loom-24bfa5?logo=visualstudiocode&logoColor=white)](https://insiders.vscode.dev/redirect?url=vscode-insiders%3Amcp%2Finstall%3F%257B%2522name%2522%253A%2522loom%2522%252C%2522type%2522%253A%2522http%2522%252C%2522url%2522%253A%2522http%253A%252F%252Flocalhost%253A7777%252Fmcp%2522%257D)

For a team server, pass `--config server_url=… --config token=…` to Claude Code, or
point Codex at it as described in
[connect a coding agent](./docs/getting-started/connect-a-coding-agent.md). Create
tokens in **Settings → Tokens**. Cursor and other MCP clients, and
[the SDK](./docs/getting-started/first-agent.md), are covered there too.

## Why Loom

- **Memory that carries forward.** Share decisions and findings across agents and
  sessions. Each fact records who wrote it, version history and conflict review make
  corrections traceable, and a namespace can be rolled back to any point in time.
  [Memory](./docs/concepts/memory.md)
- **Code intelligence with evidence.** Find definitions, callers, change impact and
  relevant tests; connect code to commits, pull requests and issues. Results identify
  their revision, source location and completeness. [Code index](./docs/features/code-index/overview.md)
- **Grounded retrieval.** Search permitted memories, documents, conversations and
  tracker records. Answers cite their sources; low-confidence retrieval can abstain.
  [Retrieval](./docs/concepts/retrieval-and-ranking.md)
- **Multi-agent handoffs.** Assign work through durable task inboxes with leases,
  retries, review rules and outcomes. Your agents execute the workflow; Loom
  preserves the handoffs. [Task queues](./docs/features/tasks-and-decisions.md)
- **Control over agent data.** Manage namespace access, provenance, retention, audit
  records and verified erasure. Context remains queryable in ClickHouse.
  [Security](./docs/architecture/security.md)

## Integrations

| Integration                                                                                                                                                                                                                                                                                   | What it connects                                                              |
| --------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- | ----------------------------------------------------------------------------- |
| [Claude Code](./docs/features/integrations/claude-code.md), [Codex](./docs/features/integrations/codex.md), [Cursor](./docs/features/integrations/cursor.md) and [other MCP clients](./docs/features/integrations/mcp-clients.md) ([all integrations](./docs/features/integrations/index.md)) | Memory and code intelligence in your existing agent workflow.                 |
| [Python](./docs/reference/python-sdk.md), [TypeScript](./docs/reference/typescript-sdk.md) and [HTTP](./docs/reference/operations.md)                                                                                                                                                         | Your own agents and applications.                                             |
| [Langfuse](./docs/features/integrations/langfuse.md)                                                                                                                                                                                                                                          | Request traces, model costs, retrieval scores and prompt-version attribution. |
| [ClickStack / HyperDX](./docs/features/integrations/clickstack.md)                                                                                                                                                                                                                            | OpenTelemetry traces, logs and metrics in ClickHouse.                         |

Langfuse and ClickStack are optional integrations, not bundled services in the Docker stack.

## Benchmark details

### Detailed retrieval, reader and code-search results

| Evaluation                                            | Result                                                                                             |
| ----------------------------------------------------- | -------------------------------------------------------------------------------------------------- |
| LongMemEval-S evidence retrieval (500 questions)      | **98.6%** of questions with at least one supporting session in the first 30 memories.              |
| LongMemEval-S fixed `gpt-5` reader (500 questions)    | **92.0%** answer accuracy from the top 200 memories.                                               |
| LoCoMo (all 10 conversations)                         | **89.6%** answer accuracy; **90.8%** correct abstentions on adversarial questions.                 |
| MemoryAgentBench fact-question sample (500 questions) | **99.0%** SubEM; **95.7%** token-overlap F1 on generated answers.                                  |
| Conceptual code search (36 questions)                 | Gold-file recall **91.2%** at 5 / **97.2%** at 10; MRR **0.91**.                                   |
| SWE-bench Verified file localization (200 issues)     | Issues with a changed file in the first 10: **82%** vs **57%** for BM25; **76.5%** in the first 5. |
| Tracker duplicate detection (104 pairs)               | Recorded older duplicate ranked first **87.5%** of the time.                                       |

These measure different tasks, not one aggregate score. SWE-bench measures
file localization, not issue resolution. The [results ledger](./docs/benchmarks/results.md)
includes scoring definitions, small-sample evaluations, weaker and negative findings,
and the full protocols, models and artifacts.

[Methodology](./docs/benchmarks/methodology.md) ·
[coverage and known gaps](./docs/benchmarks/coverage.md) ·
[run the benchmarks](./docs/benchmarks/running.md)

## Documentation and contributing

For shared agent context, code intelligence and permissioned retrieval, start with the
[documentation](https://clickhouse.github.io/loom/). For deployment requirements, see the
[prerequisites](./docs/getting-started/overview.md#prerequisites).

[Deployment](./docs/operations/deployment.md) · [FAQ](./docs/faq.md) ·
[Contributing](./CONTRIBUTING.md) · [Security reporting](./SECURITY.md) ·
[`llms.txt`](./llms.txt)

Apache-2.0 · [License](./LICENSE) · [Notice](./NOTICE)
