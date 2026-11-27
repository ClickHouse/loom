---
seo:
  description: Install Loom, connect agents to shared memory, and find guides for deploying and operating Loom.
  title: Agent memory and code search documentation
mode: center
pagination: false
---

# ClickHouse Loom documentation

Give agents access to shared memory, cited documents, and indexed code through MCP, HTTP, or the SDKs.

[Install Loom](getting-started/quickstart.md)
[Try the playground](https://loom.clickhouse.com/playground/source-change)

## Start here

- [Set up Loom](getting-started/overview.md)
  Choose a deployment and connect your first coding agent.
- [Build with Loom](getting-started/first-agent.md)
  Write and recall a memory from an agent you build.
- [Evaluate Loom](getting-started/evaluate.md)
  Test recall, access, and code lookup with your own workload.
- [Operate Loom](operations/index.md)
  Deploy for a team and plan access, backups, and upgrades.

## Use cases

See how teams use [shared agent memory](use-cases/agent-memory.md),
[document search and RAG](use-cases/document-search.md), and
[code intelligence](use-cases/code-intelligence.md). Browse [all use cases](use-cases/index.md)
or [casos de uso en español](https://loom.clickhouse.com/docs/es/use-cases).

![The dashboard's Library: a search for a refund rule answered by cited passages](assets/screenshots/document-search.webp)

[Video demonstration of a cited answer](assets/screenshots/clip-assistant-answer.webm)

## 1. Getting started

Check [what Loom includes](getting-started/whats-included.md), run the
[quickstart](getting-started/quickstart.md), and [configure models and API keys](getting-started/models-and-keys.md).

## 2. Install and run the server

Choose a [deployment](operations/deployment.md), configure [single sign-on](operations/sso.md),
and use the [operations guides](operations/index.md) to run Loom day to day.

## 3. Connect agents

[Connect a coding agent](getting-started/connect-a-coding-agent.md) or choose an
[integration](features/integrations/index.md) for your editor, MCP client, or framework.

## 4. SDK and API

Find client libraries, commands, and wire contracts in the [reference](reference/index.md).

## 5. Namespaces, access and the memory model

Read how Loom scopes [identities and namespaces](concepts/identities-and-namespaces.md),
stores [agent memory](concepts/memory.md), and [ranks retrieval results](concepts/retrieval-and-ranking.md).
Use [tokens and grants](guides/tokens-and-grants.md) to set access.

## Guides

Use the [guides](guides/index.md) for tasks such as connecting an agent, comparing memory
setups, and tracing a long run.

## Features

The [features index](features/index.md) covers memory, documents, code, work records,
and access and audit controls.

## 6. Code index

Start with the [code-index overview](features/code-index/overview.md), add a
[repository](features/code-index/repositories.md), and check its
[indexing status](features/code-index/freshness.md).

## Architecture

Read the [system overview](architecture/system-overview.md),
[storage authority](architecture/storage-authority.md), and [security model](architecture/security.md)
to understand how Loom fits into a deployment.

## Benchmarks

Read the [methodology](benchmarks/methodology.md), inspect the [results](benchmarks/results.md),
and [reproduce the evaluations](benchmarks/running.md).

## Development

Contributors can start with the [repository guide](development/repository-guide.md),
[local development](development/local-development.md), and [testing](development/testing.md).

## Also

Find a definition in the [glossary](glossary.md) or a short answer in the [FAQ](faq.md).
Agents can use the [documentation index](../llms.txt) or the
[combined Markdown](https://loom.clickhouse.com/docs/llms-full.txt).
