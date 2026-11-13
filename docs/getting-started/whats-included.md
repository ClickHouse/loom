# What's included

Every capability on this page is in the Loom repository, under the Apache-2.0 license, and
runs on your own infrastructure. Each row links the page that says how it works and what it
needs; a capability that is off by default says so on its page.

## Memory

| Capability                  | What it does                                                                                    | Page                                                                             |
| --------------------------- | ----------------------------------------------------------------------------------------------- | -------------------------------------------------------------------------------- |
| Shared long-term memory     | One claim per key in a namespace, judged against the facts already there before it lands        | [Writing memory](../features/memory/writing.md)                                  |
| Search and ask              | Ranked hits, or an answer that cites memory keys; both abstain when memory does not support one | [Search and ask](../features/memory/search-and-ask.md)                           |
| Conflicts and refusal       | A close but different value is flagged, and the namespace owner decides which fact stands       | [Conflicts and refusal](../features/memory/conflicts-and-refusal.md)             |
| History and provenance      | Every write is a new version, with who wrote it and how it entered memory                       | [History and provenance](../features/memory/history-and-provenance.md)           |
| Extraction from transcripts | The server decides what in a transcript is durable                                              | [Extraction](../features/memory/extraction.md)                                   |
| Working memory and profiles | Small named blocks and profile text an agent is given every time                                | [Working memory and profiles](../features/memory/working-memory-and-profiles.md) |
| Procedures and learnings    | Steps learned from reported runs, reflections and a review queue of candidate facts             | [Procedures and learnings](../features/memory/procedures-and-learnings.md)       |
| Memories linked to code     | A memory about code follows the definitions it names as they move or change                     | [Memory](../concepts/memory.md#memories-linked-to-code)                          |
| Entity graph                | Entities and their relations beside the memory rows                                             | [Entity graph](../features/entity-graph.md)                                      |

## Documents, code and work records

| Capability                    | What it does                                                                                 | Page                                                                    |
| ----------------------------- | -------------------------------------------------------------------------------------------- | ----------------------------------------------------------------------- |
| Document search               | Documents split into sections, embedded, searched and cited by passage                       | [Knowledge documents](../features/knowledge.md)                         |
| Unified search                | Memory, profile, documents and past conversations in one call, and a context pack for a task | [Unified search](../features/unified-search.md)                         |
| Code index                    | Definitions, callers, impact and search by meaning over your git repositories                | [Code index](../features/code-index/overview.md)                        |
| PR and release history        | Merged pull requests joined against today's symbols, callers and tests                       | [History, PRs and releases](../features/code-index/history-and-prs.md)  |
| Tracker                       | Issues, pull requests, incidents and CI runs read live from your own ClickHouse              | [Tracker plane](../features/tracker/overview.md)                        |
| Conversations and sessions    | Searchable verbatim transcripts, and session briefs an agent resumes from                    | [Conversations and sessions](../features/conversations-and-sessions.md) |
| Runs                          | One record per task execution, with the memories it relied on                                | [Runs](../features/runs.md)                                             |
| Tasks, decisions and policies | Task inboxes, a decision record and policies for coordination between agents                 | [Tasks, decisions and policies](../features/tasks-and-decisions.md)     |

## Access, governance and operations

| Capability                | What it does                                                                             | Page                                                                                        |
| ------------------------- | ---------------------------------------------------------------------------------------- | ------------------------------------------------------------------------------------------- |
| Namespaces and sharing    | The unit of isolation and sharing inside an org; every read is scoped to one             | [Namespaces and sharing](../features/namespaces-and-sharing.md)                             |
| Tokens and roles          | Tokens, grants and roles per namespace                                                   | [Tokens and grants](../guides/tokens-and-grants.md)                                         |
| Single sign-on and SCIM   | OIDC sign-in for the dashboard, CLI and MCP clients, and deprovisioning through SCIM     | [Single sign-on](../operations/sso.md)                                                      |
| Audit log                 | One row for each operation that changes state, with a verified checkpoint chain          | [Audit log](../features/observability/audit.md)                                             |
| Retention and erasure     | Expiry, configured retention, and erasure of a scope everywhere Loom keeps it            | [Retention and erasure](../features/memory/retention-and-erasure.md)                        |
| Cost and usage            | Every embedding and LLM dollar recorded against the operation that spent it              | [Cost and usage](../features/observability/cost-and-usage.md)                               |
| Telemetry                 | A per-operation ledger, OpenTelemetry traces, logs and metrics, and query attribution    | [Telemetry](../features/observability/telemetry.md)                                         |
| Dashboard                 | The web app where people decide conflicts, proposals, tasks, grant requests and policies | [Dashboard](../features/dashboard.md)                                                       |
| Replicas and Kubernetes   | Identical replicas behind one URL, and a Helm chart                                      | [Running N replicas](../operations/replicas.md) · [Kubernetes](../operations/kubernetes.md) |
| Postgres memory authority | Live memory and document heads in Postgres, beside ClickHouse                            | [Postgres](../operations/postgres.md)                                                       |
| Backup and recovery       | ClickHouse backups, plus the document bucket and Postgres where used                     | [Backup and recovery](../operations/backup-and-recovery.md)                                 |

## Ways to connect

MCP for any MCP client, plugins for Claude Code and Codex, an HTTP API, and Python and
TypeScript SDKs: [Integrations](../features/integrations/index.md) ·
[MCP clients](../features/integrations/mcp-clients.md) ·
[HTTP API](../reference/http-api.md) · [Python SDK](../reference/python-sdk.md).
