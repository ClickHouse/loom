# Connecting an MCP client

Loom serves its operations as MCP tools at `<loom>/mcp`. A streamable-HTTP client
connects with a URL and a bearer token (none on a single-user server; Cursor has an
[install link](cursor.md#mcp-tools) for that case); a stdio-only client goes through the `mcp-remote`
bridge. A tool call runs the same auth, rate limit, audit and idempotency path as
`POST /v1/<op>`. The protocol is in the [MCP reference](../../reference/mcp.md).

## Plugin or plain MCP

| You run                                               | Use                                                                        | You get                                                                                                          |
| ----------------------------------------------------- | -------------------------------------------------------------------------- | ---------------------------------------------------------------------------------------------------------------- |
| Claude Code                                           | the [Claude Code plugin](claude-code.md)                                   | tools, capture hooks, code shunt, skills, the code-scope header                                                  |
| Codex                                                 | the [Codex plugin](codex.md)                                               | the same, with Codex wiring                                                                                      |
| Claude Desktop, Cursor, VS Code, Cline, Windsurf, Zed | `loom install --client <name>` ([below](#wire-a-client-with-loom-install)) | the tools; capture only through a [transcript template](index.md#transcript-capture-hooks) ([Cursor](cursor.md)) |
| an orchestrator running several agents                | one MCP mount per agent ([frameworks.md](frameworks.md))                   | shared memory across the agents                                                                                  |

A plain MCP entry gives the tools only: no session capture, primer or code shunt. Loom
still records an implicit run per MCP session and names the agent after the client's
`clientInfo.name` (`Cursor` becomes `cursor`).

## Wire a client with `loom install`

`loom install` merges the server entry into a client's config file, or, for Claude Code
and Codex, runs their `mcp add` command:

```bash
loom install --client cursor --server-url https://loom.example.com --token "$LOOM_API_KEY"
```

| Flag           | Default                                                                   | Meaning                                                                                                                                      |
| -------------- | ------------------------------------------------------------------------- | -------------------------------------------------------------------------------------------------------------------------------------------- |
| `--client`     | every supported client whose config directory exists                      | one or more of `claude-desktop`, `claude-code`, `codex`, `cursor`, `vscode`, `cline`, `windsurf`, `zed`                                      |
| `--server-url` | `LOOM_BASE_URL`, then `~/.loom/config.json`, else `http://localhost:7777` | the Loom base URL; the entry points at `<url>/mcp`                                                                                           |
| `--token`      | `LOOM_API_KEY`, then `~/.loom/config.json`, then `~/.loom/token`          | the JWT the entry authenticates with; required                                                                                               |
| `--name`       | `loom`                                                                    | the server key in the config file, and the name `claude mcp add` / `codex mcp add` registers                                                 |
| `--toolset`    | empty: the server default (`core`); `coding` for Claude Code and Codex    | appended as `?toolset=`; a toolset name or a `+` union of curated rosters such as `coding+tracker`; anything else is rejected before writing |
| `--dry-run`    | off                                                                       | print what would be written or run, touching nothing                                                                                         |
| `--force`      | off                                                                       | Claude Code only: add the user-scope entry even when the `loom` plugin is installed                                                          |

Restart the client afterwards. Exit codes: `2` no token, `5` no client detected, `1` any
client failed (the others still run). See the [CLI reference](../../reference/cli.md).

Get a token from the dashboard's **Settings → Tokens** tab (`<server>/settings`), which also prints the per-client
blocks below, or with `loom mint-token --subject you --org <org> --namespaces <ns> --roles writer`.
A client that only reads can use a `viewer` token; see
[tokens and grants](../../guides/tokens-and-grants.md).

### What it writes per client

| Client           | Config file (Linux; macOS and Windows use the platform equivalent)                          | Section           | Entry shape                                        |
| ---------------- | ------------------------------------------------------------------------------------------- | ----------------- | -------------------------------------------------- |
| `claude-desktop` | `~/.config/Claude/claude_desktop_config.json`                                               | `mcpServers`      | `npx mcp-remote` bridge                            |
| `cursor`         | `~/.cursor/mcp.json`                                                                        | `mcpServers`      | `url` + `headers`                                  |
| `vscode`         | `~/.config/Code/User/mcp.json` (a separate file, so `settings.json` is left alone)          | `servers`         | `url` + `headers` + `type: "http"`                 |
| `cline`          | `~/.config/Code/User/globalStorage/saoudrizwan.claude-dev/settings/cline_mcp_settings.json` | `mcpServers`      | `npx mcp-remote` bridge                            |
| `windsurf`       | `~/.codeium/windsurf/mcp_config.json`                                                       | `mcpServers`      | `url` + `headers`                                  |
| `zed`            | `~/.config/zed/mcp.json` (a sibling of `settings.json`)                                     | `context_servers` | `npx mcp-remote` bridge                            |
| `claude-code`    | none: runs `claude mcp add … -s user`                                                       |                   | see [Claude Code](#claude-code-without-the-plugin) |
| `codex`          | none: runs `codex mcp add`                                                                  |                   | see [Codex](#codex-without-the-plugin)             |

The file is replaced atomically with mode 0600; every other key is kept. A file that is
not a valid JSON object is refused, not overwritten.

The two entry shapes:

```json
{ "url": "https://loom.example.com/mcp", "headers": { "Authorization": "Bearer <JWT>" } }
```

```json
{
  "command": "npx",
  "args": [
    "-y",
    "mcp-remote",
    "https://loom.example.com/mcp",
    "--allow-http",
    "--header",
    "Authorization: Bearer <JWT>"
  ]
}
```

The second is for stdio-only clients: `mcp-remote` bridges stdio to HTTP, and
`--allow-http` lets it reach a local plain-HTTP server. Both shapes store the token in the
config file; treat it as a secret.

## Claude Code without the plugin

Prefer the [plugin](claude-code.md). When it is installed, `loom install --client
claude-code` adds no entry (it would list every tool twice) unless you pass `--force`.

Without the plugin, `loom install --client claude-code` runs, or prints when `claude` is
not on `PATH`:

```bash
claude mcp add --transport http loom "https://loom.example.com/mcp?toolset=coding" \
  -H 'Authorization: Bearer ${LOOM_API_KEY}' -s user
```

The header names the variable, not the token; Claude Code expands `${LOOM_API_KEY}` at
launch. `loom install` writes `LOOM_BASE_URL` and `LOOM_API_KEY` into the `env` block of
`~/.claude/settings.json` (the plugin's hooks read them there too) and prints the plugin
install commands. For one repository, put the entry in `<repo>/.mcp.json`:

```json
{
  "mcpServers": {
    "loom": {
      "type": "http",
      "url": "https://loom.example.com/mcp?toolset=coding",
      "headers": { "Authorization": "Bearer ${LOOM_API_KEY}" }
    }
  }
}
```

## Codex without the plugin

`loom install --client codex` runs, or prints when `codex` is not on `PATH`:

```bash
codex mcp add loom --url "https://loom.example.com/mcp?toolset=coding" --bearer-token-env-var LOOM_API_KEY
```

Codex reads the token from the environment, so export `LOOM_API_KEY` in your shell
profile. The command also prints the equivalent TOML block, writes `url` and `token` to
`~/.loom/config.json` for the plugin's hooks, and prints the plugin install commands.
With the plugin installed, read [codex.md](codex.md#the-server) first: an entry under the
same name replaces the plugin's.

## Choosing a toolset

A toolset decides which tools `tools/list` shows, not what is callable. Every op not
hidden from MCP stays callable by name or through `loom_op`. Under `all`, an experimental
op that neither the core nor the atomic roster lists is left out of the listing; code
tools are listed only with `LOOM_MCP_CODE_TOOLS=true`. Fewer listed tools means less
context spent on every turn.

The server default is `LOOM_MCP_TOOLSET` (`core`). Coding agents use `?toolset=coding`.
Curated rosters combine with `,` or `+` (`?toolset=coding,tracker`). The rosters are
tabled in the [MCP reference](../../reference/mcp.md#toolsets).

Two more URL parameters shape a connection:

- `?schema_profile=compact` sends the first paragraph of each description and the
  argument tree, for a smaller listing; `standard` (default) and `full` (adds
  `outputSchema`) are the others.
- A **second registration** on another toolset adds a roster without changing the first:
  register `loom-tracker` at `?toolset=tracker` beside `loom` at `?toolset=coding`, for
  example with `loom install --client cursor --name loom-tracker --toolset tracker`.

## Clients that sign in by browser

Needs `LOOM_OAUTH_ENABLED=true`. Loom then acts as an OAuth 2.1 authorization server: a
client that supports MCP OAuth connects with the bare URL and a browser sign-in, and gets
a Loom JWT for the signed-in user. Details: [MCP reference](../../reference/mcp.md#oauth),
[configuration](../../reference/configuration.md#auth).

## Checking the connection

- The client lists `loom_*` tools after a restart. A `401` in the client means the
  bearer is missing or invalid: the server answers with `WWW-Authenticate: Bearer`, so
  most clients report "needs authentication".
- After the first tool call the client appears on the admin drawer's **Agents** page
  (`agents.connected`), named after its `clientInfo.name`.
- `loom_activate` returns the session primer, the quickest end-to-end check that the
  token resolves to the namespace you expect.
- `loom install --dry-run` shows exactly what would be written, for comparing with a
  config file that behaves unexpectedly.

## Related

- [MCP reference](../../reference/mcp.md): endpoint, auth modes, sessions, tool list,
  results, errors, security, action-schema export.
- [Integrations](index.md): the map of everything Loom connects to.
- [Quickstart](../../getting-started/quickstart.md): a first MCP connection on the
  stock stack.
- [Agent integration guide](../../guides/agent-integration.md).
