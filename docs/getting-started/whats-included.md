# What's included

This page lists what Loom does, self-hosted from the Apache-2.0 repository: one table per
area, each row linked to the page that covers it. Several capabilities must be turned on;
the linked page says how, and the boolean switches that load false are listed in
[status](../reference/status.md). The embedder is your choice: the bundled local model, a
hosted provider or an OpenAI-compatible endpoint you run. Extraction and cited answers
need a real LLM ([models and API keys](models-and-keys.md)).

## Memory

| Capability                  | What it does                                                                                                                           | Page                                                                             |
| --------------------------- | -------------------------------------------------------------------------------------------------------------------------------------- | -------------------------------------------------------------------------------- |
| Shared long-term memory     | One claim per key in a namespace, judged against the facts already there before it lands                                               | [Writing memory](../features/memory/writing.md)                                  |
| Typed records               | Rows with exact fields for data that has identity, answering predicate questions without parsing text                                  | [Typed records](../features/memory/writing.md#typed-records)                     |
| Search and ask              | Ranked hits, or an answer that cites memory keys; both abstain when memory does not support one                                        | [Search and ask](../features/memory/search-and-ask.md)                           |
| Conflicts and refusal       | A close but different value is flagged and the namespace owner decides which fact stands; a registered pattern refuses matching writes | [Conflicts and refusal](../features/memory/conflicts-and-refusal.md)             |
| History and provenance      | Every write is a new version, with who wrote it and how it entered memory                                                              | [History and provenance](../features/memory/history-and-provenance.md)           |
| Extraction from transcripts | The server decides what in a transcript is durable                                                                                     | [Extraction](../features/memory/extraction.md)                                   |
| Import                      | Facts another memory system already distilled, written with the same checks as any write                                               | [Importing](../features/memory/writing.md#importing-from-another-memory-system)  |
| Change webhooks             | One signed event per memory change, posted to a URL a namespace admin registers                                                        | [Change webhooks](../features/memory/writing.md#change-webhooks)                 |
| Working memory and profiles | Small named blocks and profile text an agent is given every time                                                                       | [Working memory and profiles](../features/memory/working-memory-and-profiles.md) |
| Procedures and learnings    | Steps learned from reported runs, reflections and a review queue of candidate facts                                                    | [Procedures and learnings](../features/memory/procedures-and-learnings.md)       |
| Memories linked to code     | A memory about code follows the definitions it names as they move or change                                                            | [Memory](../concepts/memory.md#memories-linked-to-code)                          |
| Entity graph                | Entities and their relations beside the memory rows                                                                                    | [Entity graph](../features/entity-graph.md)                                      |

## Documents, code and work records

| Capability                    | What it does                                                                                                                                              | Page                                                                                                                                                            |
| ----------------------------- | --------------------------------------------------------------------------------------------------------------------------------------------------------- | --------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| Document search               | Documents split into sections, embedded, searched and cited by passage; searches that found nothing are grouped into knowledge gaps                       | [Knowledge documents](../features/knowledge.md) · [Knowledge gaps](../features/knowledge.md#knowledge-gaps)                                                     |
| Files and document sources    | PDF, DOCX, PPTX, XLSX, CSV and more parsed by an extractor service; registered sources synced into a namespace                                            | [Ingesting files](../features/knowledge.md#ingesting-files) · [Document sources](../features/knowledge.md#document-sources)                                     |
| Unified search                | Memory, profile, documents and past conversations in one call, and a context pack for a task                                                              | [Unified search](../features/unified-search.md)                                                                                                                 |
| Ask AI                        | An assistant in the dashboard that answers about what a namespace holds, with a citation for every claim; it runs on a chat server you deploy beside Loom | [Dashboard](../features/dashboard.md#ask-ai) · [Chat server setup](../features/integrations/librechat.md)                                                       |
| Code index                    | Definitions, callers, impact and search by meaning over your git repositories                                                                             | [Code index](../features/code-index/overview.md)                                                                                                                |
| PR and release history        | Merged pull requests joined against today's symbols, callers and tests                                                                                    | [History, PRs and releases](../features/code-index/history-and-prs.md)                                                                                          |
| Tracker                       | Issues, pull requests, incidents and CI runs read live from your own ClickHouse                                                                           | [Tracker plane](../features/tracker/overview.md)                                                                                                                |
| Conversations and sessions    | Searchable verbatim transcripts, recorded once an admin turns logging on for the namespace, and session briefs an agent resumes from                      | [Conversations and sessions](../features/conversations-and-sessions.md) · [Recording](../features/conversations-and-sessions.md#recording-is-the-owners-choice) |
| Runs                          | One record per task execution, with the memories it relied on                                                                                             | [Runs](../features/runs.md)                                                                                                                                     |
| Tasks, decisions and policies | Task inboxes, a decision record and policies for coordination between agents                                                                              | [Tasks, decisions and policies](../features/tasks-and-decisions.md)                                                                                             |

## Access, governance and operations

| Capability                     | What it does                                                                                                                  | Page                                                                                                 |
| ------------------------------ | ----------------------------------------------------------------------------------------------------------------------------- | ---------------------------------------------------------------------------------------------------- |
| Namespaces and sharing         | The unit of isolation and sharing inside an org; every read is scoped to one                                                  | [Namespaces and sharing](../features/namespaces-and-sharing.md)                                      |
| Tokens and roles               | Tokens, grants and roles per namespace                                                                                        | [Tokens and grants](../guides/tokens-and-grants.md)                                                  |
| Single sign-on and SCIM        | OIDC sign-in for the dashboard, CLI and MCP clients, and deprovisioning through SCIM                                          | [Single sign-on](../operations/sso.md)                                                               |
| Audit log                      | One hash-only row per audited state change, with a verified checkpoint chain                                                  | [Audit log](../features/observability/audit.md)                                                      |
| Retention and erasure          | Expiry, configured retention, and erasure of a scope everywhere Loom keeps it                                                 | [Retention and erasure](../features/memory/retention-and-erasure.md)                                 |
| Sensitive values               | A `high`-sensitivity memory is never embedded and is readable by key only, with an audited read; detection is configurable    | [Memory types](../concepts/memory.md#memory-types)                                                   |
| Model-call controls            | An org admin can switch off every model call for the org's data, and a deployment can route them through an inference gateway | [Inference gateway](models-and-keys.md#through-an-inference-gateway)                                 |
| Cost and usage                 | Every embedding and LLM dollar recorded against the operation that spent it                                                   | [Cost and usage](../features/observability/cost-and-usage.md)                                        |
| Telemetry                      | A per-operation ledger, OpenTelemetry traces, logs and metrics, and query attribution                                         | [Telemetry](../features/observability/telemetry.md)                                                  |
| Dashboard                      | The web app where people decide conflicts, proposals, tasks, grant requests and policies                                      | [Dashboard](../features/dashboard.md)                                                                |
| SQL access                     | Read-only ClickHouse views over a tenant's memories, history, conversations and request ledger, also readable from Postgres   | [SQL surface](../reference/sql-surface.md) · [Query from Postgres](../guides/query-from-postgres.md) |
| Replicas and Kubernetes        | Identical replicas behind one URL, and a Helm chart                                                                           | [Running N replicas](../operations/replicas.md) · [Kubernetes](../operations/kubernetes.md)          |
| Postgres memory authority      | Optional: live memory and document heads in Postgres, chosen at first boot                                                    | [Postgres](../operations/postgres.md)                                                                |
| Backup and recovery            | ClickHouse backups, plus the document bucket and Postgres where used                                                          | [Backup and recovery](../operations/backup-and-recovery.md)                                          |
| Upgrades                       | Upgrades that replace the image and apply the migrations the new build carries                                                | [Upgrades](../operations/upgrades.md)                                                                |
| Namespace copy and OKF bundles | A namespace copied to another Loom on a schedule, or exported and imported as an OKF bundle                                   | [Copying a namespace](../operations/okf-sync.md) · [OKF bundles](../features/integrations/okf.md)    |
| Usage export                   | One UTC day of usage and a snapshot of the namespaces, written to S3 as Parquet for a data warehouse                          | [Analytics export](../operations/analytics-export.md)                                                |

## Ways to connect

MCP for any MCP client, plugins for Claude Code and Codex, framework adapters, an HTTP API,
Python and TypeScript SDKs, and the `loom` CLI:
[Integrations](../features/integrations/index.md) ·
[MCP clients](../features/integrations/mcp-clients.md) ·
[Frameworks](../features/integrations/frameworks.md) ·
[HTTP API](../reference/http-api.md) · [Python SDK](../reference/python-sdk.md) ·
[TypeScript SDK](../reference/typescript-sdk.md) · [CLI](../reference/cli.md).
