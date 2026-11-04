# Integrations

Loom on a bare ClickHouse service is a complete deployment. Each integration below is
opt-in; one you leave unconfigured does nothing. They fall into three groups:

- **Agent-side:** put Loom's tools in front of a model (plugins, MCP clients, framework
  adapters).
- **Capture:** feed the conversation and observation planes without the agent's help
  (plugin hooks, transcript templates).
- **Backend:** receive Loom's telemetry, or delete a user's data in another system during
  erasure (Langfuse and ClickStack).

## Map

| Integration                                           | Kind                                                    | Where                                                                                      |
| ----------------------------------------------------- | ------------------------------------------------------- | ------------------------------------------------------------------------------------------ |
| Claude Code                                           | plugin: MCP tools, hooks, code shunt, skills            | `integrations/claude-code-plugin/`: [claude-code.md](claude-code.md)                       |
| Codex                                                 | plugin: MCP tools, hooks, code shunt, skills            | `integrations/codex-plugin/`: [codex.md](codex.md)                                         |
| Claude Desktop, Cursor, VS Code, Cline, Windsurf, Zed | MCP client config                                       | `loom install --client …`: [mcp-clients.md](mcp-clients.md)                                |
| Cursor                                                | MCP tools + `sessionEnd` transcript hook                | `integrations/cursor/`: [cursor.md](cursor.md)                                             |
| Any Agent-Skills client                               | the `loom` and `loom-code` skills, no hooks             | `skills/loom/`, `skills/loom-code/`: [below](#coding-agents)                               |
| Aider, Continue, OpenCode, JetBrains AI               | transcript capture template                             | `integrations/generic/`: [below](#transcript-capture-hooks)                                |
| Agents that only use shell grep                       | the grep shim                                           | `integrations/grep-shim/`: [grep-shim.md](grep-shim.md)                                    |
| LibreChat v0.8.8                                      | chat client on the assistant toolset, per-user token    | `integrations/librechat/`: [librechat.md](librechat.md)                                    |
| LangGraph, Strands Agents                             | Python SDK memory adapters; needs the framework         | `loom.integrations.langgraph`, `loom.integrations.strands`: [frameworks.md](frameworks.md) |
| Agent orchestrators                                   | shared MCP mount                                        | [frameworks.md](frameworks.md)                                                             |
| AX (sandboxed agent tasks)                            | Workspace manifest: MCP config, `AGENTS.md`, run budget | `integrations/ax/`: [README](../../../integrations/ax/README.md)                           |
| Langfuse                                              | traces, retrieval scores, erasure; needs Langfuse       | [langfuse.md](langfuse.md)                                                                 |
| ClickStack / HyperDX                                  | OpenTelemetry spans, logs, metrics; erasure             | [clickstack.md](clickstack.md)                                                             |
| Git repositories (OKF bundles)                        | export, import, CI lint gate                            | `integrations/okf-ci/`: [okf.md](okf.md)                                                   |
| Shell pipelines                                       | `loom memory-search --json-agent`                       | [below](#command-line-search-for-pipelines)                                                |
| Provider prompt caches                                | byte-stable `context.pack` prefix                       | [below](#prompt-caching)                                                                   |
| GitHub                                                | code index, PR history; needs the code workers enabled  | [code index overview](../code-index/overview.md)                                           |

## Coding agents

- Plugins (MCP tools, session hooks, code shunt): [connect a coding agent](../../getting-started/connect-a-coding-agent.md),
  then [claude-code.md](claude-code.md) or [codex.md](codex.md).
- Any other MCP client (`loom install --client …`): [mcp-clients.md](mcp-clients.md).
- The `loom` and `loom-code` skills alone, no hooks: `npx skills add ClickHouse/loom --skill loom loom-code`,
  or `claude plugin install loom-code-skill@loom`. Install either the `loom` plugin or
  `loom-code-skill`, not both; the plugin already ships `loom-code`, and its other skills cover what `loom` says.

## Transcript capture hooks

These are templates you wire into a client yourself. Turns are stored only when
conversation logging is on.

Two shell templates pass a finished transcript to `loom ingest-transcript`, which posts
each turn to `conversation.append`:

| Client                                  | Template                                     | Wiring                                                                                               | `LOOM_AGENT` default |
| --------------------------------------- | -------------------------------------------- | ---------------------------------------------------------------------------------------------------- | -------------------- |
| Cursor                                  | `integrations/cursor/cursor-session-hook.sh` | `sessionEnd` hook in `~/.cursor/hooks.json`, reads hook input on stdin ([cursor.md](cursor.md))      | `cursor`             |
| Aider, Continue, OpenCode, JetBrains AI | `integrations/generic/ingest.sh`             | run after the session on the exported transcript ([README](../../../integrations/generic/README.md)) | `generic`            |

Both need `LOOM_NAMESPACE` and a token (`LOOM_API_KEY`, else `~/.loom/token`).
`LOOM_BASE_URL` defaults to `http://localhost:7777`; `LOOM_BIN` to `loom` on `PATH`.
Limits, exit codes and the dry run are in
[`integrations/generic/README.md`](../../../integrations/generic/README.md).

A transcript is a `.json` array of `{role, content}` objects or a `.md` file with
alternating `# user` / `# assistant` sections. By hand:

```bash
loom ingest-transcript --server-url https://loom.example.com --namespace <ns> --file session.md
```

- The thread is named after the file's resolved path (or `--thread-id`).
- Each turn's `msg_id` hashes its role, content and count of identical earlier turns, so
  re-ingesting a grown file appends only new turns.
- `--dry-run` prints the turn count and sizes, never the text.
- **Logging must be on** (`conversation.set_logging`, off for a new namespace).
  `conversation.append` succeeds even when it drops the turn.
- **Stored turns are not mined.** To write memories from a transcript, post its turns to
  `memory.set_from_messages`.

The plugins capture sessions with their own hooks and do not need these templates
([Claude Code hooks](claude-code.md#what-the-hooks-do)). See
[conversations and sessions](../conversations-and-sessions.md).

## Joining a request across systems

Every HTTP response carries an `X-Request-Id`: yours, if you sent one of 1 to 64 letters,
digits, `.`, `_` or `-`; else a generated UUID.
The same id is written to:

| System              | Where                                                     |
| ------------------- | --------------------------------------------------------- |
| Loom's event ledger | `loom.agent_events.request_id`                            |
| Langfuse            | trace metadata `request_id`                               |
| OpenTelemetry       | span attribute `loom.request_id` in `default.otel_traces` |

Send your own `X-Request-Id` to join Loom's records to your application's logs.

## Command-line search for pipelines

`loom memory-search` runs `memory.search` over HTTP:

```bash
loom memory-search --namespace <ns> --query "retry policy for the ingest queue" --json-agent
```

`--json-agent` prints one JSON envelope (`command`, `query`, `namespace`, `results`; each
result has `memory_key`, `content`, `similarity`, `source`, `temporal_anchor`,
`namespace`). Without it the output is a readable list.

| Flag            | Default                                                                   |
| --------------- | ------------------------------------------------------------------------- |
| `--search-mode` | `bm25_hybrid`                                                             |
| `--top-k`       | `10`                                                                      |
| `--token`       | `LOOM_API_KEY`, then `~/.loom/config.json`, then `~/.loom/token`          |
| `--org`         | the token's `org` claim, else `dev`                                       |
| `--server-url`  | `LOOM_BASE_URL`, then `~/.loom/config.json`, else `http://localhost:7777` |

All flags: [CLI reference](../../reference/cli.md).

## Prompt caching

Tool results serialise in a fixed field order, so the same content returns the same
bytes. `context.pack` puts query-independent sections first and returns
`stable_sections`, `stable_prefix_chars` and `stable_hash`. The first
`stable_prefix_chars` characters of `rendered` stay byte-identical until working memory,
the profile or the pins change: put the cache breakpoint there, and compare `stable_hash`
to see whether the prefix moved. The Python SDK exposes `pack.stable_prefix` and
`pack.volatile_suffix`.

Loom's own model calls report `gen_ai.usage.cached_input_tokens` and
`loom.llm.prompt_cache_hit_ratio` on their spans.

## Related

- [Agent integration guide](../../guides/agent-integration.md): plugin, MCP or SDK.
- [MCP reference](../../reference/mcp.md): endpoint, toolsets, results, errors.
- [Tokens and grants](../../guides/tokens-and-grants.md): the token each integration needs.
- [Operations reference](../../reference/operations.md#conversation): `conversation.*`.
