# Integrations

Client-side pieces that connect a host to a Loom server. Pick the row for
your host; each path's own README or doc is the reference. For MCP tools
alone in any MCP client (Claude Desktop, Cursor, VS Code, Cline, Windsurf,
Zed), `loom install --client <name>` writes the config; see
[docs/reference/mcp.md](../docs/reference/mcp.md). [docs/features/integrations/index.md](../docs/features/integrations/index.md)
covers the server-side integrations (Langfuse, ClickStack, framework
adapters).

## Supported integrations

| Host                                       | Path                                                   | Lifecycle events                                                                                          | Install                                                                                                                                                      | Runtime                       | Auth                                                                                   | Tests                                                                                        |
| ------------------------------------------ | ------------------------------------------------------ | --------------------------------------------------------------------------------------------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------ | ----------------------------- | -------------------------------------------------------------------------------------- | -------------------------------------------------------------------------------------------- |
| Claude Code                                | [`claude-code-plugin/`](claude-code-plugin/README.md)  | SessionStart, UserPromptSubmit, PreToolUse, PostToolUse, PostToolUseFailure, PreCompact, Stop, SessionEnd | `claude plugin marketplace add ClickHouse/loom && claude plugin install loom@loom`                                                                           | Node 18+                      | writer token: install option, `LOOM_API_KEY`, `~/.loom/config.json` or `~/.loom/token` | `node --test integrations/claude-code-plugin/tests/*.test.mjs`                               |
| Codex                                      | [`codex-plugin/`](codex-plugin/README.md)              | SessionStart, UserPromptSubmit, PreToolUse, PostToolUse, PreCompact, Stop, SessionEnd                     | `codex plugin marketplace add ClickHouse/loom && codex plugin add loom@loom` ([docs/features/integrations/codex.md](../docs/features/integrations/codex.md)) | Node 18+                      | writer token: `LOOM_API_KEY`, `~/.loom/config.json` or `~/.loom/token`                 | generated from `claude-code-plugin/`: `python3 scripts/codegen/agent_plugin_sync.py --check` |
| Any Agent-Skills client                    | [`../skills/loom-code/`](../skills/loom-code/SKILL.md) | none (a skill, no hooks)                                                                                  | `npx skills add ClickHouse/loom`, or `claude plugin install loom-code-skill@loom`                                                                            | the host's MCP tools          | the host's MCP entry                                                                   |                                                                                              |
| Cursor (transcript capture)                | [`cursor/`](cursor/README.md)                          | `sessionEnd` (or `stop`) in `~/.cursor/hooks.json`                                                        | copy the hook, register it in `hooks.json`                                                                                                                   | bash, python3, the `loom` CLI | `LOOM_API_KEY` or `~/.loom/token`, never on argv                                       | `integrations/cursor/test.sh`                                                                |
| Aider, Continue, OpenCode, any chat export | [`generic/`](generic/README.md)                        | none: run after the session on an exported `.json` / `.md` transcript                                     | run `generic/ingest.sh`                                                                                                                                      | bash, the `loom` CLI          | `LOOM_API_KEY` or `~/.loom/token`, never on argv                                       | `integrations/generic/test.sh`                                                               |
| Agents that only use shell grep            | [`grep-shim/`](grep-shim/README.md)                    | none: `grep` / `rg` on `PATH`                                                                             | put `grep-shim/bin` first on `PATH`                                                                                                                          | Node 18+                      | viewer token in `LOOM_API_KEY`                                                         | `node --test integrations/grep-shim/tests/*.test.mjs`                                        |
| Git repositories holding OKF bundles       | [`okf-ci/`](okf-ci/README.md)                          | push, pull request                                                                                        | copy `okf-lint.yml` to `.github/workflows/`, pin `LOOM_REF`                                                                                                  | GitHub Actions, uv            | none: the lint is offline; `contents: read`                                            | actionlint; `loom okf-lint` is covered by `tests/unit/test_okf_lint.py`                      |

Install either the `loom` plugin or `loom-code-skill`, not both: each ships a
skill named `loom-code`, and the model would see two.

## Where captured chat goes

The plugins and the transcript scripts write chat turns to the conversation
plane (`conversation.append`), one thread per session, deduplicated by
`msg_id`. A namespace stores them only when its conversation logging is on
(`conversation.set_logging`, or the `/me` toggle). Stored turns are not mined
for memories; the plugins write memories through their own hooks.
`LOOM_CAPTURE_CONVERSATIONS=false` turns capture off in the plugins and the
Cursor hook.
