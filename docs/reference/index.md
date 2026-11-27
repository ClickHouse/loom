# Reference

Look up SDKs, request and response fields, CLI commands, and server settings.

## Look up an interface

- [Call Loom](http-api.md)
  Find HTTP routes, authentication, pagination, and request headers.
- [Connect over MCP](mcp.md)
  Check authentication, toolsets, and tool results.
- [Look up an operation](operations.md)
  Find the required role and the input and output fields.
- [Look up a setting](configuration.md)
  Check defaults and allowed values for every server setting.

## Client SDKs

[Python](python-sdk.md) · [TypeScript](typescript-sdk.md) · [Go](go-sdk.md) · [Rust](rust-sdk.md)

## Generated from the code

These references describe what the server exposes with each release.

| Page                              | What it lists                                             |
| --------------------------------- | --------------------------------------------------------- |
| [Configuration](configuration.md) | Settings, defaults, and allowed values.                   |
| [Operations](operations.md)       | Operation names, roles, MCP listings, and audit behavior. |
| [Workers](workers.md)             | Background workers and their schedules and settings.      |
| [CLI](cli.md)                     | Commands and flags, with examples.                        |
| [Errors](errors.md)               | Error codes, retry rules, and common fixes.               |
| [Status](status.md)               | Operation maturity and settings that default to off.      |
| [Schema](schema.md)               | Tables, materialized views, keys, TTLs, and indexes.      |

Contributors can read how to update these pages in [Code generation](../development/code-generation.md).

## Written by hand

| Page                                          | What it covers                                                                                                                                                     |
| --------------------------------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------ |
| [HTTP API](./http-api.md)                     | Every route (ops, discovery, health, streams, sign-in, OAuth, dashboard, operator), the gate order, headers, unknown fields, idempotency, pagination, rate limits. |
| [MCP](./mcp.md)                               | The `/mcp` endpoint, authentication and OAuth, sessions and identity, the tool list and toolsets, results, errors, the action-schema export.                       |
| [Python SDK](./python-sdk.md)                 | Install, construction, calling ops, typed shapes, sessions and tools, framework adapters, errors, retries.                                                         |
| [TypeScript SDK](./typescript-sdk.md)         | Install, clients and options, typed ops, sessions and tools, pagination, the Strands adapter, errors, retries.                                                     |
| [Go SDK](./go-sdk.md)                         | Install, save and recall agent memory, configure a team connection, and handle timeouts and errors.                                                                |
| [Rust SDK](./rust-sdk.md)                     | Install, async and blocking clients, generated contracts, identity, call options, errors and retries.                                                              |
| [Environment variables](./environment.md)     | Variables read outside the settings model: provider keys, the image, compose files, the CLI, the SDKs and the operator scripts.                                    |
| [SQL surface](./sql-surface.md)               | The read-only `sql_v1_*` views over a tenant's data: columns, compatibility rule, what they withhold, freshness, `loom sql-access`.                                |
| [Extractor contract](./extractor-contract.md) | Version 1 of the HTTP contract an extractor service serves: capabilities, extraction, error codes, the artifact's fields and rules.                                |

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
