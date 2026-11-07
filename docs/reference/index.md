# Reference

Reference pages say what exists and what each option means. The reasons behind a design are in
[Concepts](../concepts/memory.md) and [Architecture](../architecture/system-overview.md).
Procedures are in the [guides](../guides/agent-integration.md) and
[operations](../operations/deployment.md) sections.

## Generated from the code

These pages, or the tables in them, are rendered from the source that defines them. Each
generator has a `--check` mode that `make lint` runs, so a page that drifts from the code fails
CI. Edit the source and regenerate; never edit a generated table by hand.

| Page                                | What it lists                                                                                                 | Source                                                                               | Generator                                      |
| ----------------------------------- | ------------------------------------------------------------------------------------------------------------- | ------------------------------------------------------------------------------------ | ---------------------------------------------- |
| [Configuration](./configuration.md) | Every `LOOM_*` setting, its default and meaning, by area.                                                     | the comments above fields in `server/settings/`                                      | `python scripts/codegen/settings_reference.py` |
| [Operations](./operations.md)       | Every operation, grouped, with role, kind, MCP `all` listing and audit flag; plus how the registry works.     | `server.operations.registry.ALL_OPERATIONS`                                          | `python scripts/codegen/docs_reference.py`     |
| [Workers](./workers.md)             | Every background worker, its switch, default and cadence; plus how the supervisor runs them.                  | `server/workers/registry.py` (`_WORKERS`)                                            | `python scripts/codegen/docs_reference.py`     |
| [CLI](./cli.md)                     | Every `loom` command and subcommand with its flags; plus where the CLI runs and the common commands.          | the `loom` parser tree (`tests/unit/snapshots/cli_tree.json`)                        | `python scripts/codegen/docs_reference.py`     |
| [Errors](./errors.md)               | Every error code the server answers with; plus the envelope, retry rules and fixes for the common codes.      | `server/core/errors.py` and every `"code"` literal under `server/`                   | `python scripts/codegen/docs_reference.py`     |
| [Status](./status.md)               | How many of each op group's ops are stable, each experimental op, and every boolean setting that loads false. | op `maturity` in the registry and the boolean fields of `server/settings/catalog.py` | `python scripts/codegen/docs_reference.py`     |
| [Schema](./schema.md)               | Every table and materialized view with engine, keys, TTL and indexes; plus the column conventions.            | `schemas/schema.sql`                                                                 | `python scripts/codegen/docs_reference.py`     |

On the `docs_reference.py` pages only the regions between the
`<!-- BEGIN GENERATED: … -->` and `<!-- END GENERATED: … -->` markers are generated; the prose
around them is hand-written. The op tables inside the SDK packages (`sdk/python/loom/ops.py`,
`sdk/typescript/src/generated/ops.ts`) are generated too, by
`scripts/codegen/sdk_py_gen.py` and `sdk_ts_gen.py`; see
[Code generation](../development/code-generation.md).

## Written by hand

| Page                                          | What it covers                                                                                                                                                     |
| --------------------------------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------ |
| [HTTP API](./http-api.md)                     | Every route (ops, discovery, health, streams, sign-in, OAuth, dashboard, operator), the gate order, headers, unknown fields, idempotency, pagination, rate limits. |
| [MCP](./mcp.md)                               | The `/mcp` endpoint, authentication and OAuth, sessions and identity, the tool list and toolsets, results, errors, the action-schema export.                       |
| [Python SDK](./python-sdk.md)                 | Install, construction, calling ops, typed shapes, sessions and tools, framework adapters, errors, retries.                                                         |
| [TypeScript SDK](./typescript-sdk.md)         | Install, clients and options, typed ops, sessions and tools, pagination, the Strands adapter, errors, retries.                                                     |
| [Environment variables](./environment.md)     | Variables read outside the settings model: provider keys, the image, compose files, the CLI, the SDKs and the operator scripts.                                    |
| [Extractor contract](./extractor-contract.md) | Version 1 of the HTTP contract an extractor service serves: capabilities, extraction, error codes, the artifact's fields and rules.                                |

The hand-written pages name only things that exist in the tree and link to the source they
describe.

## Which page answers…

- "What is this op called, and who may call it?" → [Operations](./operations.md).
- "What does this setting do?" → [Configuration](./configuration.md).
- "What does this error code mean, and should I retry?" → [Errors](./errors.md).
- "Is this op stable or experimental, and is this setting on by default?" → [Status](./status.md).
- "Which header, which route, how do I page?" → [HTTP API](./http-api.md).
- "Why does my MCP client not list a tool?" → [MCP](./mcp.md#toolsets).
- "Is this worker running, and how do I turn it on?" → [Workers](./workers.md).
- "Will my client break on the next deploy?" → [unknown and removed fields](./http-api.md#unknown-and-removed-fields) and [discovering what a deployment serves](./http-api.md#discovering-what-a-deployment-serves).
- "What is in this table?" → [Schema](./schema.md).
- "What does `loom <command>` take?" → [CLI](./cli.md).
