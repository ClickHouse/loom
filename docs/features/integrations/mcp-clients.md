# Connecting an MCP client

Loom serves its operations as MCP tools at `<loom>/mcp`. A streamable-HTTP client connects
with a URL and a bearer token (none on a single-user server; Cursor and VS Code have
install links for that case: [Cursor](cursor.md#mcp-tools),
[VS Code](vscode.md#local-single-user-server)); a stdio-only client goes through the
`mcp-remote` bridge. A tool call runs the same auth, rate limit, audit and idempotency
path as `POST /v1/<op>`. The protocol is in the [MCP reference](../../reference/mcp.md).

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

| Flag           | Default                                                                                         | Meaning                                                                                                                                      |
| -------------- | ----------------------------------------------------------------------------------------------- | -------------------------------------------------------------------------------------------------------------------------------------------- |
| `--client`     | every supported client whose config directory exists                                            | one or more of `claude-desktop`, `claude-code`, `codex`, `cursor`, `vscode`, `cline`, `windsurf`, `zed`                                      |
| `--server-url` | `LOOM_BASE_URL`, then `~/.loom/config.json`, else `http://localhost:7777`                       | the Loom base URL; the entry points at `<url>/mcp`                                                                                           |
| `--token`      | `LOOM_API_KEY`, then `~/.loom/config.json`, then `~/.loom/token`                                | the JWT the entry authenticates with; required                                                                                               |
| `--name`       | `loom`                                                                                          | the server key in the config file, and the name `claude mcp add` / `codex mcp add` registers                                                 |
| `--toolset`    | empty: `coding` for Claude Code, Codex and Cursor, else the server default (`LOOM_MCP_TOOLSET`) | appended as `?toolset=`; a toolset name or a `+` union of curated rosters such as `coding+tracker`; anything else is rejected before writing |
| `--dry-run`    | off                                                                                             | print what would be written or run, touching nothing                                                                                         |
| `--force`      | off                                                                                             | Claude Code only: add the user-scope entry even when the `loom` plugin is installed                                                          |

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

The two entry shapes, the first as the Cursor command above writes it:

```json
{
  "url": "https://loom.example.com/mcp?toolset=coding",
  "headers": { "Authorization": "Bearer <JWT>" }
}
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

## Other editors and agents

[`loom install`](#what-it-writes-per-client) covers the clients in its table; VS Code also
has [install links](vscode.md#local-single-user-server). Each client below reaches
`<loom>/mcp` with its own config. In the entries, `URL` is `http://localhost:7777/mcp` for
a single-user server on the same machine (no header needed) or
`https://loom.example.com/mcp` with a token; add `?toolset=coding` for a coding agent that
uses the code index ([choosing a toolset](#choosing-a-toolset)). Restart or reload the
client afterwards.

| Client                                                              | Where                                                                                                            | Entry                                                                                                                                                                                                                    | Checked   |
| ------------------------------------------------------------------- | ---------------------------------------------------------------------------------------------------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------ | --------- |
| JetBrains AI Assistant (IntelliJ IDEA, PyCharm, WebStorm, GoLand)   | Settings → Tools → AI Assistant → Model Context Protocol (MCP) → Add                                             | `{"mcpServers": {"loom": {"url": "URL"}}}` (single-user server only: no header field or sign-in is documented)                                                                                                           |           |
| JetBrains Junie                                                     | `~/.junie/mcp/mcp.json`, or `.junie/mcp/mcp.json` in the project                                                 | `{"mcpServers": {"loom": {"url": "URL", "headers": {…}}}}`                                                                                                                                                               |           |
| Visual Studio (Copilot agent mode)                                  | `%USERPROFILE%\.mcp.json`, or `.mcp.json` in the solution                                                        | `{"servers": {"loom": {"type": "http", "url": "URL", "headers": {…}}}}` (Visual Studio also reads `.vscode/mcp.json`)                                                                                                    |           |
| Kiro                                                                | `~/.kiro/settings/mcp.json`, or `.kiro/settings/mcp.json`                                                        | `{"mcpServers": {"loom": {"url": "URL", "headers": {…}}}}`                                                                                                                                                               |           |
| Gemini CLI                                                          | `~/.gemini/settings.json`, or `.gemini/settings.json`                                                            | `{"mcpServers": {"loom": {"httpUrl": "URL", "headers": {…}}}}`, or `gemini mcp add --transport http --header "Authorization: Bearer <JWT>" loom URL` (drop `--header` for a single-user server); or the extension, below | connected |
| GitHub Copilot CLI                                                  | `~/.copilot/mcp-config.json`                                                                                     | `{"mcpServers": {"loom": {"type": "http", "url": "URL", "headers": {…}, "tools": ["*"]}}}`                                                                                                                               | accepted  |
| Neovim with mcphub.nvim (also serves avante.nvim and CodeCompanion) | `~/.config/mcphub/servers.json`                                                                                  | `{"mcpServers": {"loom": {"url": "URL", "headers": {…}}}}`                                                                                                                                                               |           |
| Emacs with mcp.el and gptel                                         | `mcp-hub-servers` in your init file, then `gptel-mcp-connect`                                                    | `(setq mcp-hub-servers '(("loom" . (:url "URL"))))` (single-user server only: no header field is documented)                                                                                                             |           |
| OpenCode                                                            | `opencode.json` in the project (read inside a git repository), or `~/.config/opencode/opencode.json`             | `opencode mcp add loom --url URL --header "Authorization=Bearer <JWT>"`, which writes `{"mcp": {"servers": {"loom": {"type": "remote", "url": "URL", "headers": {…}}}}}`                                                 | connected |
| Kilo Code (extension and CLI)                                       | `kilo.jsonc` in the project, or `~/.config/kilo/kilo.jsonc`                                                      | `{"mcp": {"loom": {"type": "remote", "url": "URL", "headers": {…}}}}`                                                                                                                                                    | connected |
| Cline CLI                                                           | `~/.cline/data/settings/cline_mcp_settings.json`                                                                 | `cline mcp add loom URL --transport http --header "Authorization: Bearer <JWT>" --yes` (the Cline extension: [`loom install`](#what-it-writes-per-client))                                                               | accepted  |
| Google Antigravity                                                  | `~/.gemini/config/mcp_config.json`, or `.agents/mcp_config.json` in the workspace                                | `{"mcpServers": {"loom": {"serverUrl": "URL", "headers": {…}}}}` (the key is `serverUrl`)                                                                                                                                |           |
| Devin Desktop and Devin CLI                                         | `.devin/mcp_config.local.json` in the project (git-ignored, for the token), or `~/.config/devin/mcp_config.json` | `{"mcpServers": {"loom": {"url": "URL", "transport": "http", "headers": {…}}}}`                                                                                                                                          |           |
| Qwen Code                                                           | `~/.qwen/settings.json`, or `.qwen/settings.json`                                                                | `qwen mcp add --transport http loom URL --header "Authorization: Bearer <JWT>"`; Qwen Code also installs the Claude Code plugin from Loom's marketplace                                                                  |           |
| Kimi Code CLI                                                       | `~/.kimi-code/mcp.json`, or `.kimi-code/mcp.json`                                                                | `{"mcpServers": {"loom": {"url": "URL", "headers": {…}}}}`                                                                                                                                                               |           |
| Grok Build                                                          | `~/.grok/config.toml`, or `.grok/config.toml`; it also reads a project `.mcp.json`                               | `grok mcp add --transport http loom URL --header "Authorization: Bearer <JWT>"`                                                                                                                                          |           |
| Qoder (CLI and IDE)                                                 | `~/.qoder/settings.json`, `.qoder/settings.json`, or a project `.mcp.json`                                       | `{"mcpServers": {"loom": {"type": "http", "url": "URL", "headers": {…}}}}`                                                                                                                                               |           |
| Trae                                                                | `.trae/mcp.json` (with project MCP turned on), or Settings → MCP → Add                                           | `{"mcpServers": {"loom": {"url": "URL", "headers": {…}}}}`                                                                                                                                                               |           |
| MiniMax Code                                                        | `~/.minimax/mcp.json`, or the MCP Servers tab                                                                    | `{"mcpServers": {"loom": {"type": "streamable-http", "url": "URL", "headers": {…}}}}`                                                                                                                                    |           |
| goose                                                               | `~/.config/goose/config.yaml`, under `extensions`                                                                | `loom: {type: streamable_http, uri: URL, headers: {Authorization: "Bearer <JWT>"}}`                                                                                                                                      |           |
| Amp                                                                 | `amp.mcpServers` in Amp's settings, or `amp mcp add`                                                             | `{"loom": {"url": "URL", "headers": {…}}}`                                                                                                                                                                               |           |
| Warp                                                                | `~/.warp/.mcp.json`                                                                                              | `{"mcpServers": {"loom": {"url": "URL", "headers": {…}}}}`                                                                                                                                                               |           |
| Factory droid                                                       | `~/.factory/mcp.json`                                                                                            | `droid mcp add loom URL --type http --header "Authorization: Bearer <JWT>"`                                                                                                                                              |           |
| Augment (Auggie CLI)                                                | Auggie's settings                                                                                                | `auggie mcp add loom --transport http --url URL --header "Authorization: Bearer <JWT>"`                                                                                                                                  |           |
| Hermes Agent                                                        | `~/.hermes/config.yaml`, under `mcp_servers`                                                                     | `loom: {url: URL, headers: {Authorization: "Bearer <JWT>"}}`, or `hermes mcp add loom --url URL`                                                                                                                         |           |
| Zed                                                                 | `context_servers` in `settings.json`                                                                             | `{"context_servers": {"loom": {"url": "URL", "headers": {…}}}}` ([`loom install`](#what-it-writes-per-client) writes the `mcp-remote` bridge instead)                                                                    |           |
| OpenHands                                                           | Customize → MCP Servers                                                                                          | the URL, with the token in its bearer-token field                                                                                                                                                                        |           |
| n8n                                                                 | an MCP Client Tool node                                                                                          | endpoint URL, transport HTTP Streamable, Bearer authentication with the token                                                                                                                                            |           |
| smolagents (Python)                                                 | code                                                                                                             | `MCPClient({"url": URL, "transport": "streamable-http", "headers": {…}})`                                                                                                                                                |           |

`{…}` is `{"Authorization": "Bearer <JWT>"}`; leave it out for a single-user server.
In **Checked**, `connected` means the client's own status command reported the server connected (a local Loom with a token, no model call), and `accepted` that the client wrote or accepted the entry but has no status command; a blank one was not run.

Gemini CLI also installs Loom as an extension, which asks for the MCP URL and, for a team
server, a token (leave it empty for a single-user server):

```bash
gemini extensions install https://github.com/ClickHouse/loom
```

Gemini installs an extension from the repository's latest release; `--ref <branch>` installs
another ref. The extension also installs Loom's two agent skills, `loom` and `loom-code`.
`gemini extensions config clickhouse-loom` changes the two values later; a scripted install
passes `--skip-settings` and writes `LOOM_MCP_URL` and `LOOM_API_KEY` to the extension's
`.env` file (`~/.gemini/extensions/clickhouse-loom/.env`). `gemini mcp list` shows the `loom` server as
connected once the URL and token are right. On a single-user server, an empty token still sends `Authorization: Bearer`, which the
server accepts with a warning in its log for each request; `gemini mcp add` without
`--header` (above) avoids it.

Claude Desktop's custom connectors (Settings → Connectors) reach the server from the
provider's cloud, so they need a Loom on a public HTTPS address; on the same machine,
`loom install --client claude-desktop` writes the `mcp-remote` bridge entry.

A hosted agent or app builder (Replit Agent, Lovable, v0, Genspark, the cloud editions of
n8n, Dify, Flowise or Langflow, and Zapier or Make) and a chat app's custom connector
(claude.ai, Grok, Perplexity, Le Chat) reach Loom only at a public HTTPS address; a
self-hosted one reaches it on the same network. ChatGPT's custom MCP servers send no
custom header, so they need the [browser sign-in](#clients-that-sign-in-by-browser).

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
