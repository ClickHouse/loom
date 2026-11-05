# Codex plugin

The Codex plugin gives Codex the same Loom integration as Claude Code: MCP tools, hooks
that capture the session and load a primer, and the code shunt. Its scripts, skills and
listing icon (`assets/`) are generated copies of the Claude Code plugin's; only the wiring
in `integrations/codex-plugin/hooks/hooks.json` differs. Hooks and knobs are documented in
[claude-code.md](claude-code.md); the exceptions are in
[What differs from Claude Code](#what-differs-from-claude-code).
What the plugin sends, and where, is in [data handling](claude-code.md#data-handling).

## Install

```bash
codex plugin marketplace add ClickHouse/loom        # or a path to a local clone
codex plugin add loom@loom
```

The plugin brings its own MCP entry (`.mcp.json`): the server URL with
`?toolset=core,coding,tracker,tasks`, authenticated by the plugin's header helper,
`bin/loom-mcp-headers.mjs`. Do not add `codex mcp add loom` on top: an entry with the
same name replaces the plugin's, and one under another name lists every tool twice.

### Approve the hooks

Codex runs a plugin's hooks only after the user approves them, and again after an update
that changes their commands. Run `codex` interactively once after installing and approve
the Loom hooks; Codex records each approval as a `trusted_hash` under `[hooks.state]` in
`~/.codex/config.toml`. Until then the MCP tools work but sessions are not captured,
including under `codex exec`.

### The token

A single-user server on the same machine needs no token. Otherwise the helper and the
hooks read `LOOM_API_KEY`, then `~/.loom/config.json`, then `~/.loom/token`; an agent
(`LOOM_CLIENT_ID` with a workload token) uses `LOOM_API_KEY` when set, otherwise only the
token it exchanges. On a server with `LOOM_SELF_SIGNUP=open`, the first session with no
credential signs the machine up and writes `~/.loom/config.json` itself
(`LOOM_AUTO_SIGNUP=off` disables this). To use a token you already have:

```bash
mkdir -p ~/.loom && cat > ~/.loom/config.json <<'EOF'
{ "url": "https://loom.example.com", "token": "<writer JWT>" }
EOF
chmod 600 ~/.loom/config.json
```

Get the token from the dashboard's **Settings → Tokens** tab (`<server>/settings`) or with
`loom mint-token --subject you --org <org> --namespaces <ns> --roles writer`. The hooks
need `writer`; the tools can read with `viewer`. See
[tokens and grants](../../guides/tokens-and-grants.md).

### The server

Codex plugins have no install options, so the URL is fixed in the plugin's `.mcp.json`
(`http://localhost:7777` as shipped) and in `plugin-defaults.json` for the hooks. A
deployment that publishes its own marketplace sets both.

For another server on one machine, override the entry in `~/.codex/config.toml` (the
override replaces the plugin's entry whole, helper included), and put the same URL in
`~/.loom/config.json` for the hooks:

```toml
[mcp_servers.loom]
url = "https://loom.example.com/mcp?toolset=core,coding,tracker,tasks"
http_headers_helper = 'h="$(ls -d "$HOME"/.codex/plugins/cache/*/loom/*/bin/loom-mcp-headers.mjs 2>/dev/null | sort -V | tail -1)"; sh "${h%/*}/loom-node" "$h"'
```

Codex runs the helper through `sh -c` with a reduced environment (`HOME`, `PATH`, the
locale and a few others) and does not expand `${PLUGIN_ROOT}`, so the command finds the
newest installed helper under `$HOME/.codex` and starts it through the plugin's
`bin/loom-node`. The launcher finds Node 18+ on that `PATH`, in a version manager's
default directory under `HOME` or in the usual install directories; `LOOM_NODE` and
variables such as `NVM_DIR` do not reach it. With a custom `CODEX_HOME`, spell the path
out. On Windows Codex runs the helper through `cmd.exe`, which cannot run this command:
use `bearer_token_env_var` there. A Codex build that rejects `Authorization` from a
header helper ("returned a reserved header") also needs
`bearer_token_env_var = "LOOM_API_KEY"`, with `LOOM_API_KEY` exported.

`loom install --client codex --server-url https://loom.example.com --token <JWT>` writes
such an override with `bearer_token_env_var` and `?toolset=coding`, and writes `url` and
`token` to `~/.loom/config.json` for the hooks ([mcp-clients.md](mcp-clients.md#codex-without-the-plugin)).

### Where things live

| What        | Where                                                                                                                    |
| ----------- | ------------------------------------------------------------------------------------------------------------------------ |
| MCP entry   | the plugin's `.mcp.json`; a `[mcp_servers.loom]` in `~/.codex/config.toml` overrides it whole                            |
| Bearer      | `LOOM_API_KEY` → `~/.loom/config.json` `token` → `~/.loom/token`                                                         |
| Hook config | `~/.loom/config.json` `{url, token, namespace?, org?}`; `LOOM_BASE_URL` overrides; with no `url`, `plugin-defaults.json` |
| Plugin      | `~/.codex/plugins/cache/<marketplace>/loom/<version>/`                                                                   |

## What differs from Claude Code

| Codex                                                            | Effect                                                                                                                                                                                                                                                                                                                |
| ---------------------------------------------------------------- | --------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| no `Read` / `Grep` tools; files are read through the shell tool  | the code shunt watches the shell tool (`Bash`, `shell`, `exec_command`, `local_shell`) for `cat`, `rg`, `grep -r`; the command may be a string or an argv array                                                                                                                                                       |
| edits go through `apply_patch`                                   | file context and blast radius read the target paths out of the patch text (`*** Update File:` / `*** Add File:`)                                                                                                                                                                                                      |
| `transcript_path` may be missing or in Codex's own shape         | the hooks keep a turn journal: `loom-run` records each prompt, `loom-conversation` each reply (`last_assistant_message`) at Stop; the working-memory note, the run close, the conversation plane and session memory read it                                                                                           |
| no per-turn token counters in the payload                        | `loom-usage` is not wired                                                                                                                                                                                                                                                                                             |
| `SessionEnd` hooks get about one second                          | only fire-and-forget scripts run there (capture, run close, session memory, session brief); conversation turns are sent at Stop                                                                                                                                                                                       |
| no `PostToolUseFailure` event                                    | a `PostToolUse` result flagged as an error (`is_error`, `isError` or `error`) is observed as type `error`                                                                                                                                                                                                             |
| the mentor-hint hook matches every tool                          | it ignores results without a Loom `hint` and makes no request of its own                                                                                                                                                                                                                                              |
| hooks run through `$SHELL -lc`, and through `cmd.exe` on Windows | each command starts its script through `bin/loom-node`, which finds Node 18+ as in Claude Code; on Windows `commandWindows` runs `node` from the system `PATH`. Codex reads it from 0.131; an earlier build on Windows runs the `sh` command through `cmd.exe`, where it fails, so Windows needs Codex 0.131 or later |
| identity                                                         | run ids are `cx-<session_id>`, the default agent is `codex`, session memories carry `source:codex`                                                                                                                                                                                                                    |

SessionStart fires on `startup`, `resume`, `clear` and `compact`; the primer is printed
to stdout, which Codex adds as developer context. Every command in `hooks/hooks.json`
names the client as the script's third argument (`… PostToolUse codex`).

## Skills

The plugin carries `loom-memory`, `loom-code`, `loom-mentor`, `loom-optimize`,
`loom-session`, `loom-status` and `improve-loom-for-my-repo`, described in
[claude-code.md](claude-code.md#skills). Codex loads them from the plugin; invoke one by
name (`$loom-status`) or let its description trigger it.

## A short contract for AGENTS.md

Codex reads `AGENTS.md` at the repository root (and `~/.codex/AGENTS.md`). Keep the
contract short; the skills carry the detail:

```markdown
## Memory (Loom)

- Before a non-trivial task, search memory for prior decisions on the files or topic (`loom_search`).
- When the user states a preference, decision or correction, record it (`memory.set`).
- In this repository use `code.search` / `code.symbol` / `code.callers` for structure questions; grep for uncommitted files.
- At the end of a task, review what should outlive the session (the `loom-memory` skill).
```

## Headless

The hooks run under `codex exec` when the plugin is installed for the user running it.
To point one run at a server without the plugin's entry:

```bash
codex exec --json \
  -c 'mcp_servers.loom.url="https://loom.example.com/mcp?toolset=coding"' \
  -c 'mcp_servers.loom.bearer_token_env_var="LOOM_API_KEY"' \
  "summarise what we decided about retries"
```

Normal coding work needs only `?toolset=coding`; add `&schema_profile=compact` for a
smaller tool listing ([MCP reference](../../reference/mcp.md)). The
[headless bots guide](../../guides/headless-bots.md) covers scheduled and CI runs.

## Checking it works

- `codex mcp list` shows `loom`; the first tool call turns the token's row green on the
  dashboard's **Settings → Tokens** tab.
- After one session: the dashboard's Sessions view shows the conversation (when logging
  is on for the namespace), `run.get` on `cx-<session_id>` shows the run's observations,
  and the next primer starts from the working-memory note.
- Nothing arrives: run the `loom-status` skill. It checks the Node runtime and runs
  `bin/loom-client-status.mjs` from the plugin, with no Loom checkout or Python needed.
  `loom doctor --client` (operator CLI) adds the wiring checks.

## Troubleshooting

| Symptom                                        | Cause                                                  | Fix                                                                            |
| ---------------------------------------------- | ------------------------------------------------------ | ------------------------------------------------------------------------------ |
| MCP connection fails with 401                  | no token found by the helper                           | write `~/.loom/config.json`, or export `LOOM_API_KEY`                          |
| MCP connection fails with 401, hooks work      | the helper finds no Node: it gets no `LOOM_NODE`       | put Node's directory on the `PATH` Codex starts with; `$loom-status` says      |
| "returned a reserved header"                   | this Codex build rejects `Authorization` from a helper | override with `bearer_token_env_var = "LOOM_API_KEY"`                          |
| tools talk to the wrong server                 | the bundled URL is not your deployment                 | the `config.toml` override above, plus `url` in `~/.loom/config.json`          |
| tools listed twice                             | a second MCP entry under another name                  | remove it (`codex mcp remove <name>`)                                          |
| a hook prints a `loom:` line about Node.js 18+ | the launcher found no Node 18+, or only an old one     | `LOOM_NODE=/path/to/node` where Codex starts (macOS, Linux), or `$loom-status` |
| hooks write nothing                            | viewer token, or the hooks read a different `url`      | writer token; check `~/.loom/config.json` and `LOOM_BASE_URL`                  |
| Sessions view empty                            | conversation logging is off for the namespace          | turn it on in the dashboard or with `conversation.set_logging`                 |

## Related

- [Claude Code plugin](claude-code.md): the hook reference and every knob.
- [MCP clients](mcp-clients.md) and the [MCP reference](../../reference/mcp.md).
- [Integrations](index.md).
