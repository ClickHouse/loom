# Connecting an MCP client

Loom serves its operations as MCP tools at `<loom>/mcp`. A streamable-HTTP client connects
with a URL and a bearer token (none on a single-user server; Cursor and VS Code have
install links for that case: [Cursor](cursor.md#mcp-tools),
[VS Code](vscode.md#local-single-user-server)); a stdio-only client goes through the
`mcp-remote` bridge. A tool call runs the same auth, rate limit, audit and idempotency
path as `POST /v1/<op>`. The protocol is in the [MCP reference](../../reference/mcp.md).

## Plugin or plain MCP

| You run                                       | Use                                                                                          | You get                                                                                                                                          |
| --------------------------------------------- | -------------------------------------------------------------------------------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------ |
| Claude Code                                   | the [Claude Code plugin](claude-code.md)                                                     | tools, capture hooks, code shunt, skills, the code-scope header                                                                                  |
| Codex                                         | the [Codex plugin](codex.md)                                                                 | the same, with Codex wiring                                                                                                                      |
| Cursor                                        | the [Cursor plugin](cursor.md#plugin) (untested in Cursor) or `loom install --client cursor` | the tools; the `loom` and `loom-code` skills from the plugin; capture through the [`sessionEnd` hook](cursor.md#transcript-capture) you register |
| Claude Desktop, VS Code, Cline, Windsurf, Zed | `loom install --client <name>` ([below](#wire-a-client-with-loom-install))                   | the tools; capture only through a [transcript template](index.md#transcript-capture-hooks)                                                       |
| an orchestrator running several agents        | one MCP mount per agent ([frameworks.md](frameworks.md))                                     | shared memory across the agents                                                                                                                  |

A plain MCP entry gives the tools only: no session capture, primer or code shunt. Loom
still records an implicit run per MCP session and names the agent after the client's
`clientInfo.name` (`Cursor` becomes `cursor`). To teach the agent when to call the tools,
add the [agent instructions](agent-instructions.md) block to a coding client's rule file,
with `?toolset=coding` on the entry's URL (`--toolset coding` with `loom install`, the
default only for Claude Code, Codex and Cursor).

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

Get a token from the dashboard's **Settings → Tokens** tab (`<server>/settings`), which
also prints the per-client blocks below, or with
`loom mint-token --subject you --org <org> --namespaces <ns> --roles writer`. A client
that only reads can use a `viewer` token; see
[tokens and grants](../../guides/tokens-and-grants.md).

### What it writes per client

| Client           | Config file (Linux; macOS and Windows use the platform equivalent)                          | Section           | Entry shape                                        |
| ---------------- | ------------------------------------------------------------------------------------------- | ----------------- | -------------------------------------------------- |
| `claude-desktop` | `~/.config/Claude/claude_desktop_config.json`                                               | `mcpServers`      | `npx mcp-remote` bridge                            |
| `cursor`         | `~/.cursor/mcp.json`                                                                        | `mcpServers`      | `url` + `headers`                                  |
| `vscode`         | `~/.config/Code/User/mcp.json` (a separate file, so `settings.json` is left alone)          | `servers`         | `url` + `headers` + `type: "http"`                 |
| `cline`          | `~/.config/Code/User/globalStorage/saoudrizwan.claude-dev/settings/cline_mcp_settings.json` | `mcpServers`      | `npx mcp-remote` bridge                            |
| `windsurf`       | `~/.codeium/windsurf/mcp_config.json`                                                       | `mcpServers`      | `url` + `headers`                                  |
| `zed`            | `~/.config/zed/settings.json`                                                               | `context_servers` | `url` + `headers`                                  |
| `claude-code`    | none: runs `claude mcp add … -s user`                                                       |                   | see [Claude Code](#claude-code-without-the-plugin) |
| `codex`          | none: runs `codex mcp add`                                                                  |                   | see [Codex](#codex-without-the-plugin)             |

The file is replaced atomically with mode 0600; every other key is kept. A file that is
not a valid JSON object is refused, not overwritten.

Zed reads `context_servers` from `settings.json`. On macOS that file is
`~/.config/zed/settings.json`, the same path as on Linux. On Windows it is
`%APPDATA%\Zed\settings.json`. The file may contain `//` comments, `/* */` comments
and trailing commas. `loom install --client zed` inserts the `loom` entry. Comments and the other
keys stay, and a member that had no trailing comma gains one. When the file cannot
be merged, the command leaves it unchanged and prints the entry to add by hand.

Cursor's CLI (`agent`) reads the same `mcp.json` as the editor, so the `cursor` entry
serves both.

Cursor CLI 2026.11.13-eacade1 discovered distinct disabled entries in both the user
and project files while signed out. This checks configuration discovery only: no
connection, token/header behavior or editor plugin was tested.

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

Without the plugin's skills, add the [agent instructions](agent-instructions.md) block to
the repository's `CLAUDE.md`, or to its `AGENTS.md` with `@AGENTS.md` in `CLAUDE.md`.

## Codex without the plugin

`loom install --client codex` runs, or prints when `codex` is not on `PATH`:

```bash
codex mcp add loom --url "https://loom.example.com/mcp?toolset=coding" --bearer-token-env-var LOOM_API_KEY
```

Codex reads the token from the environment, so export `LOOM_API_KEY` in your shell
profile. The command also prints the equivalent TOML block, writes `url` and `token` to
`~/.loom/config.json` for the plugin's hooks, and prints the plugin install commands.
With the plugin installed, read [codex.md](codex.md#the-server) first: an entry under the
same name replaces the plugin's. Without the plugin, add the
[agent instructions](agent-instructions.md) block to `AGENTS.md`.

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

| Client                                                              | Where                                                                                                                                                                                                                                                                              | Entry                                                                                                                                                                                                                                                                                                                          | Checked                                                            |
| ------------------------------------------------------------------- | ---------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------ | ------------------------------------------------------------------ |
| JetBrains AI Assistant (IntelliJ IDEA, PyCharm, WebStorm, GoLand)   | Settings → Tools → AI Assistant → Model Context Protocol (MCP) → Add                                                                                                                                                                                                               | `{"mcpServers": {"loom": {"url": "URL"}}}` (single-user server only: no header field or sign-in is documented)                                                                                                                                                                                                                 |                                                                    |
| JetBrains Junie                                                     | `~/.junie/mcp/mcp.json`, or `.junie/mcp/mcp.json` in the project                                                                                                                                                                                                                   | `{"mcpServers": {"loom": {"url": "URL", "headers": {…}}}}`                                                                                                                                                                                                                                                                     | installed (26.11.1); MCP status unmeasured                         |
| Visual Studio (Copilot agent mode)                                  | `%USERPROFILE%\.mcp.json`, or `.mcp.json` in the solution                                                                                                                                                                                                                          | `{"servers": {"loom": {"type": "http", "url": "URL", "headers": {…}}}}` (Visual Studio also reads `.vscode/mcp.json`)                                                                                                                                                                                                          |                                                                    |
| Kiro                                                                | `~/.kiro/settings/mcp.json`, or `.kiro/settings/mcp.json`; Kiro CLI reads the same `~/.kiro/settings/mcp.json` as the Kiro IDE (and copies `~/.aws/amazonq/mcp.json` there once, when Kiro CLI is installed, skipping servers that conflict with ones already there)               | `{"mcpServers": {"loom": {"url": "URL", "headers": {…}}}}` (Kiro documents that it expands a `${LOOM_API_KEY}` placeholder only for a variable listed in the **Mcp Approved Env Vars** setting); for a single-user server, an install link `https://kiro.dev/launch/mcp/add?name=loom&config=<URL-encoded {"url": "URL"}>`     | installed (CLI 3.3.0); `mcp list` requires login                   |
| Gemini CLI                                                          | `~/.gemini/settings.json`, or `.gemini/settings.json`                                                                                                                                                                                                                              | `{"mcpServers": {"loom": {"httpUrl": "URL", "headers": {…}}}}`, or `gemini mcp add --transport http --header "Authorization: Bearer <JWT>" loom URL` (drop `--header` for a single-user server); or the extension, below; Gemini lists a server as disabled in a folder it does not trust                                      | connected (`gemini mcp add`, and the extension)                    |
| GitHub Copilot CLI                                                  | `~/.copilot/mcp-config.json`                                                                                                                                                                                                                                                       | `{"mcpServers": {"loom": {"type": "http", "url": "URL", "headers": {…}, "tools": ["*"]}}}`; or the plugin, below                                                                                                                                                                                                               | accepted                                                           |
| Neovim with mcphub.nvim (also serves avante.nvim and CodeCompanion) | `~/.config/mcphub/servers.json`                                                                                                                                                                                                                                                    | `{"mcpServers": {"loom": {"url": "URL", "headers": {…}}}}`                                                                                                                                                                                                                                                                     |                                                                    |
| Emacs with mcp.el and gptel                                         | `mcp-hub-servers` in your init file, then `gptel-mcp-connect`                                                                                                                                                                                                                      | `(setq mcp-hub-servers '(("loom" . (:url "URL"))))` (single-user server only: no header field is documented)                                                                                                                                                                                                                   |                                                                    |
| OpenCode                                                            | `opencode.json` or `.opencode/opencode.json` in the project or a folder above it, or `~/.config/opencode/opencode.json`                                                                                                                                                            | `opencode mcp add loom --url URL --header "Authorization=Bearer <JWT>"`, which in OpenCode 2.x writes `{"mcp": {"servers": {"loom": {"type": "remote", "url": "URL", "headers": {…}}}}}`; the 1.x `opencode-ai` npm package takes `{"mcp": {"loom": {"type": "remote", "url": "URL", "headers": {…}}}}`, which 2.x still reads | connected                                                          |
| Kilo Code (extension and CLI)                                       | `kilo.jsonc` in the project, or `~/.config/kilo/kilo.jsonc`                                                                                                                                                                                                                        | `{"mcp": {"loom": {"type": "remote", "url": "URL", "headers": {…}}}}`                                                                                                                                                                                                                                                          | connected (CLI)                                                    |
| Cline CLI                                                           | `~/.cline/data/settings/cline_mcp_settings.json`                                                                                                                                                                                                                                   | `cline mcp add loom URL --transport http --header "Authorization: Bearer <JWT>" --yes` (the Cline extension: [`loom install`](#what-it-writes-per-client))                                                                                                                                                                     | accepted                                                           |
| Google Antigravity                                                  | `~/.gemini/config/mcp_config.json`, or `.agents/mcp_config.json` in the workspace                                                                                                                                                                                                  | `{"mcpServers": {"loom": {"serverUrl": "URL", "headers": {…}}}}` (`url` also works)                                                                                                                                                                                                                                            |                                                                    |
| Devin Desktop and Devin CLI                                         | `.devin/mcp_config.local.json` in the project (git-ignored, for the token), or `~/.config/devin/mcp_config.json`                                                                                                                                                                   | `{"mcpServers": {"loom": {"url": "URL", "transport": "http", "headers": {…}}}}` (`loom install --client windsurf` writes the older `~/.codeium/windsurf/` path)                                                                                                                                                                | accepted (CLI 3000.12.57 configuration only); Desktop unmeasured   |
| Qwen Code                                                           | `~/.qwen/settings.json`, or `.qwen/settings.json`                                                                                                                                                                                                                                  | `qwen mcp add --transport http loom URL --header "Authorization: Bearer <JWT>"`                                                                                                                                                                                                                                                | connected (`qwen mcp list`)                                        |
| Kimi Code CLI                                                       | `~/.kimi-code/mcp.json`, or `.kimi-code/mcp.json` in the working directory (read once the folder is trusted); or `/mcp-config` in the TUI                                                                                                                                          | `{"mcpServers": {"loom": {"url": "URL", "headers": {…}}}}`, or `"bearerTokenEnvVar": "LOOM_API_KEY"` in place of `headers`                                                                                                                                                                                                     | installed (2.5.3); MCP status unmeasured                           |
| Grok Build                                                          | `~/.grok/config.toml`, or `.grok/config.toml`; below those it also reads a project `.mcp.json`, `~/.claude.json` and `.cursor/mcp.json`, so an existing Claude Code or Cursor entry is picked up (`[compat.claude] mcps = false` and `[compat.cursor] mcps = false` turn that off) | `grok mcp add --transport http loom URL --header "Authorization: Bearer <JWT>"`                                                                                                                                                                                                                                                | accepted (1.0.77); connection unmeasured                           |
| Qoder CLI and IDE                                                   | CLI: `~/.qoder/settings.json`, `.qoder/settings.json`, or a project `.mcp.json`; IDE: Settings → MCP → My Servers → Add (its docs show no header field: single-user server)                                                                                                        | `qodercli mcp add -t http -s user loom URL` (single-user server), or `{"mcpServers": {"loom": {"type": "http", "url": "URL", "headers": {…}}}}`                                                                                                                                                                                | CLI accepted (1.1.90); connection unmeasured                       |
| Qoder CN CLI                                                        | `~/.qoder-cn/settings.json`                                                                                                                                                                                                                                                        | `qoderclicn mcp add -t http -s user loom URL` (single-user server); native HTTP entries use `mcpServers` with `url` and optional `headers`                                                                                                                                                                                     | accepted (1.1.90); connection unmeasured                           |
| Muse Code                                                           | `~/.config/muse/settings.json`                                                                                                                                                                                                                                                     | `muse mcp add loom --url URL --user`; add `--bearer-token-env-var LOOM_API_KEY` for a team server with the raw token in that environment variable                                                                                                                                                                              | accepted (1.4.20), without token; connection unmeasured            |
| MiniMax Code                                                        | `~/.minimax/mcp.json`, or the MCP Servers tab                                                                                                                                                                                                                                      | `{"mcpServers": {"loom": {"type": "streamable-http", "url": "URL", "headers": {…}}}}`                                                                                                                                                                                                                                          | installed (mcode 0.7.7); MCP status unmeasured                     |
| goose                                                               | `~/.config/goose/config.yaml`, under `extensions`                                                                                                                                                                                                                                  | `goose configure` → Add Extension → Remote Extension (Streamable HTTP), then add the `Authorization` header when asked; or, in the file, `loom: {type: streamable_http, name: loom, enabled: true, uri: URL, headers: {Authorization: "Bearer <JWT>"}}`                                                                        | starts (`goose doctor`)                                            |
| Amp                                                                 | `~/.config/amp/settings.json`, or `.amp/settings.json`, under `amp.mcpServers`                                                                                                                                                                                                     | `amp mcp add loom --header "Authorization=Bearer <JWT>" URL`, which writes `{"loom": {"url": "URL", "headers": {…}}}`                                                                                                                                                                                                          | accepted (`amp mcp list`; `amp mcp doctor` needs a sign-in)        |
| Warp desktop                                                        | `~/.warp/.mcp.json`, `.warp/.mcp.json` in the project (a project server needs approval), or Settings → Agents → MCP servers                                                                                                                                                        | `{"mcpServers": {"loom": {"url": "URL", "headers": {…}}}}`                                                                                                                                                                                                                                                                     |                                                                    |
| Factory droid                                                       | `~/.factory/mcp.json`                                                                                                                                                                                                                                                              | `droid mcp add loom URL --type http --header "Authorization: Bearer <JWT>" --no-oauth`                                                                                                                                                                                                                                         | connected (`droid mcp list`)                                       |
| Augment (Auggie CLI)                                                | `~/.augment/settings.json` (`--project` writes `.augment/settings.json`)                                                                                                                                                                                                           | `auggie mcp add loom --transport http --url URL --header "Authorization:Bearer <JWT>"`                                                                                                                                                                                                                                         | accepted (`auggie mcp list` does not probe the URL)                |
| Hermes Agent                                                        | `~/.hermes/config.yaml`, under `mcp_servers`                                                                                                                                                                                                                                       | `hermes mcp add loom --url URL --auth header`, which asks for the token, saves it to `~/.hermes/.env` as `MCP_LOOM_API_KEY` and writes the header as `Bearer ${MCP_LOOM_API_KEY}`; or `loom: {url: URL, headers: {Authorization: "Bearer <JWT>"}}` in the file                                                                 | connected (`hermes mcp test loom`)                                 |
| Zed                                                                 | `context_servers` in `settings.json`                                                                                                                                                                                                                                               | `{"context_servers": {"loom": {"url": "URL", "headers": {…}}}}` ([`loom install`](#what-it-writes-per-client))                                                                                                                                                                                                                 |                                                                    |
| OpenHands cloud UI                                                  | Customize → MCP Servers                                                                                                                                                                                                                                                            | transport SHTTP, the URL, Authentication `Bearer token` with the token in the API key field                                                                                                                                                                                                                                    |                                                                    |
| n8n                                                                 | an MCP Client Tool node on an AI Agent node                                                                                                                                                                                                                                        | **Endpoint** = URL, Server Transport HTTP Streamable, Authentication Bearer Auth, with the token in a new Bearer Auth credential                                                                                                                                                                                               |                                                                    |
| smolagents (Python)                                                 | code                                                                                                                                                                                                                                                                               | `MCPClient({"url": URL, "transport": "streamable-http", "headers": {…}})`; `ToolCollection.from_mcp` takes the same dict with `trust_remote_code=True` (smolagents 1.26 needs `mcp<2`: its MCP adapter does not load with `mcp` 2)                                                                                             | connected (`MCPClient` listed the tools)                           |
| OpenClaw                                                            | `~/.openclaw/openclaw.json`, under `mcp.servers`                                                                                                                                                                                                                                   | `openclaw mcp set loom '{"url": "URL", "transport": "streamable-http", "headers": {…}}'`                                                                                                                                                                                                                                       | connected (`openclaw mcp probe`)                                   |
| Strix                                                               | `~/.strix/mcp-servers.json` (a JSON list), or `--mcp-config <file>`                                                                                                                                                                                                                | `[{"name": "loom", "transport": "http", "url": "URL", "auth": {"kind": "bearer", "token": "<JWT>"}}]`                                                                                                                                                                                                                          | connected (Strix's MCP client listed the tools; no status command) |
| Open WebUI                                                          | Admin Panel → Settings → Integrations → External Tool Servers → + Add Connection                                                                                                                                                                                                   | Type MCP (Streamable HTTP), the URL, Auth Bearer with the bare token as the key (Open WebUI adds `Bearer`)                                                                                                                                                                                                                     |                                                                    |
| LibreChat v0.8.8                                                    | a `librechat.yaml` `mcpServers` entry                                                                                                                                                                                                                                              | `type: streamable-http`, `url: <loom>/mcp?toolset=assistant`, `headers: {Authorization: "Bearer {{LOOM_TOKEN}}"}` with `LOOM_TOKEN` declared under `customUserVars`, so each person enters their own token; see [LibreChat](librechat.md)                                                                                      | qualified ([cases](librechat.md#qualification))                    |
| Continue (no longer actively maintained)                            | a YAML file in `.continue/mcpServers/` in the workspace                                                                                                                                                                                                                            | `mcpServers: [{name: loom, type: streamable-http, url: URL, requestOptions: {headers: {Authorization: "Bearer <JWT>"}}}]`, with the file's `name`, `version` and `schema` fields                                                                                                                                               |                                                                    |
| Cherry Studio                                                       | Settings → MCP, added by JSON import                                                                                                                                                                                                                                               | `{"mcpServers": {"loom": {"type": "streamableHttp", "url": "URL", "headers": {…}}}}`; Cherry Studio switches an imported server on, and hovering the error dot on its server card shows why a connection failed                                                                                                                |                                                                    |
| LM Studio                                                           | `mcp.json`, opened from the Program tab → Install → Edit mcp.json                                                                                                                                                                                                                  | `{"mcpServers": {"loom": {"url": "URL", "headers": {…}}}}`                                                                                                                                                                                                                                                                     |                                                                    |
| Jan                                                                 | the Jan Agent CLI                                                                                                                                                                                                                                                                  | `jan cli mcp add loom --type http --url URL --header "Authorization=Bearer <JWT>" --active`                                                                                                                                                                                                                                    |                                                                    |
| Crush                                                               | `.crushrc` or `crushrc` in the project, or `~/.config/crush/crushrc` (there is no `crush mcp add` command)                                                                                                                                                                         | `mcp add loom --type http --url URL --header Authorization "Bearer <JWT>"`                                                                                                                                                                                                                                                     |                                                                    |
| Mistral Vibe                                                        | `~/.vibe/config.toml`                                                                                                                                                                                                                                                              | `[[mcp_servers]]` with `name = "loom"`, `transport = "streamable-http"`, `url = "URL"`, then `[mcp_servers.auth]` with `type = "static"`, `api_key_env = "LOOM_API_KEY"`, `api_key_header = "Authorization"`, `api_key_format = "Bearer {token}"`                                                                              |                                                                    |
| Amazon Q Developer (IDE plugins)                                    | Q Developer chat panel → tools icon → **+**; saved to `~/.aws/amazonq/default.json`, or `.amazonq/default.json` in the project                                                                                                                                                     | transport `http`, the URL, and the `Authorization` header under Headers                                                                                                                                                                                                                                                        |                                                                    |

`{…}` is `{"Authorization": "Bearer <JWT>"}`; leave it out for a single-user server. In
**Checked**:

- `connected`: the client's own status command reported the server connected, against a
  local Loom with a token and no model call (OpenCode 2.3.0, Kilo CLI 7.8.27, Gemini CLI
  0.68.0, Qwen Code 0.27.3, Factory droid 1.12.1, Hermes Agent 0.23.1, OpenClaw 2026.9.2);
  for smolagents 1.26.0 and Strix 1.7.0, their own MCP client listed the tools.
- `starts`: the client's check started the extension, which a rejected token stops (goose
  1.59.1).
- `accepted`: the client wrote or accepted the entry; a successful server connection was
  not established (Cline CLI 3.0.74, GitHub Copilot CLI 1.0.110, Amp 0.0.1794715238,
  Auggie CLI 0.37.1, Qoder and Qoder CN 1.1.90, Muse Code 1.4.20, Grok Build 1.0.77,
  Devin CLI 3000.12.57).
  The Qoder, Muse and Grok checks used a nonworking test URL without credentials; their
  token/header handling was not tested.
- `installed`: version and help were checked, without an MCP connection or successful
  MCP configuration check (Junie 26.11.1, Kimi Code 2.5.3, MiniMax mcode 0.7.7, Kiro CLI 3.3.0).
- `qualified`: the linked page's qualification cases were run through that entry, with the
  results it tables.

A blank Checked cell was not run.

The [Qoder installer](https://docs.qoder.com/cli/installation) provides `qodercli`;
the [CN installer](https://docs.qoder.cn/en/cli/installation) provides `qoderclicn`.
They use separate user configuration paths. Muse's tested CLI writes `mcpServers`
with `type: "streamable-http"`; use its
[CLI configuration](https://dev.meta.ai/docs/muse-code/configuration) rather than
assuming a hand-written entry is accepted by every version. `enabled` in Muse's output
means configured, not connected.

Devin CLI's [native configuration](https://docs.devin.ai/cli/extensibility/mcp/configuration)
uses the dedicated user file in the table. The tested CLI also reads
`~/.codeium/windsurf/mcp_config.json` through its default
[Windsurf import](https://docs.devin.ai/cli/reference/configuration/read-config-from).
`loom install --client windsurf` writes that imported file; disabling
`read_config_from.windsurf` removes that route from Devin. The CLI check wrote a native
entry and listed distinct disabled entries from both files; it did not connect to Loom.
Current [Devin Local documentation](https://docs.devin.ai/desktop/devin-local/customizations)
describes shared CLI configuration discovery; Desktop itself was not run.

Kimi's `/mcp` and `/mcp-config` are TUI commands. Kimi's
`doctor` does not validate its MCP file, and the tested MiniMax binary has no
`mcp list` command. Their installation checks do not establish a working Loom setup.

[OpenHands V1 CLI](https://github.com/OpenHands/OpenHands-CLI) is a separate,
no-longer-maintained client. Its 1.16.0 binary accepted
`openhands mcp add loom --transport http URL` without a token, writing
`~/.openhands/mcp.json`; connection and authentication were not tested. The cloud UI
row does not describe this legacy CLI or the current Agent Canvas.

goose's desktop app also takes a deeplink,
`goose://extension?type=streamable_http&url=<URL-encoded URL>&id=loom&name=loom&header=Authorization%3DBearer%20<JWT>`;
the link holds the token, so do not share it.

Gemini CLI also installs Loom as an extension, which asks for the MCP URL and, for a team
server, a token (leave it empty for a single-user server):

```bash
gemini extensions install https://github.com/ClickHouse/loom
```

Gemini installs an extension from the repository's latest release; `--ref <branch>`
installs another ref. The extension also installs Loom's two agent skills, `loom` and
`loom-code`. `gemini extensions config clickhouse-loom` changes the two values later; a
scripted install passes `--skip-settings` and writes `LOOM_MCP_URL` and `LOOM_API_KEY` to
the extension's `.env` file (`~/.gemini/extensions/clickhouse-loom/.env`).
`gemini mcp list` shows the `loom` server as connected once the URL and token are right.
On a single-user server, an empty token still sends `Authorization: Bearer`, which the
server accepts with a warning in its log for each request; `gemini mcp add` without
`--header` (above) avoids it.

GitHub Copilot CLI (experimental) installs Loom as a plugin from the repository's root
`plugin.json`, an [Agent Plugins](https://agent-plugins.org) package:

```bash
copilot plugin install ClickHouse/loom
```

Copilot CLI installs it from the default branch. The plugin adds the `loom` MCP server at
`http://localhost:7777/mcp?toolset=coding`, a single-user server on the same machine, and
the `loom` and `loom-code` skills; `copilot mcp get loom` shows the entry.

- **Team server**: the URL is fixed and a user entry named `loom` does not replace it, so
  skip the plugin and add the entry from the table. To remove an installed one, run
  `copilot plugin uninstall clickhouse-loom`, or `copilot plugin uninstall loom-code-skill`
  for an install named `loom-code-skill` (below).
- **Skills only**: a `loom-code-skill` install that `copilot mcp get loom` does not list
  has only the skills. `copilot plugin update loom-code-skill` turns it into this plugin,
  local entry included, so on a team server uninstall it instead; installing again without
  updating keeps both and loads the skills twice.
- **Claude Code marketplace**: `copilot plugin marketplace add ClickHouse/loom`, then
  `copilot plugin install loom-code-skill@loom`, installs the same package, MCP server
  included, under the name `loom-code-skill`.

Checked with Copilot CLI 1.0.112: the install, the update, the marketplace install,
`copilot mcp get loom`, and MCP `initialize` and `tools/list` through the entry pointed at
a local Loom on another port. A Copilot session needs a sign-in and was not run, so whether
a session offers the tools to the model is untested.

### Hosted builders and chat apps

A hosted agent or app builder (Replit Agent, Lovable, v0, Zapier, Make, Microsoft Copilot
Studio, the GitHub Copilot cloud agent, and the cloud editions of n8n, Dify, Flowise and
Langflow) and a chat app's custom connector (claude.ai and Claude Desktop, ChatGPT, Grok,
Perplexity, Le Chat) reach Loom from the provider's cloud, so only at a public HTTPS address
or through the provider's tunnel where it has one; a self-hosted builder reaches it on the
same network. On the same machine, `loom install --client claude-desktop` writes the
`mcp-remote` bridge entry instead. Where these take the server:

- **Flowise**: add a Custom MCP tool to an Agent node. Its
  [Streamable HTTP configuration](https://docs.flowiseai.com/tutorials/tools-and-mcp#streamable-http-recommended)
  accepts a `url` and `headers` object. Set the URL to your reachable Loom `/mcp` endpoint
  and the `Authorization` header to `Bearer <JWT>`; use a Flowise variable for the token.
  Refresh Available Actions and select the Loom tools the agent needs.
- **Langflow**: register the server in Settings → MCP Servers or the MCP sidebar, using
  [HTTP/SSE and header fields](https://docs.langflow.org/mcp-client#connect-to-a-non-langflow-mcp-server).
  Enter the reachable Loom `/mcp` URL and `Authorization: Bearer <JWT>`; header values
  can use Langflow global variables. Drag the registered server onto the canvas, select
  its tools, enable Tool mode and connect its Toolset port to the Agent's Tools port.
- **Zapier**: add an Apps connection for
  [MCP Client](https://help.zapier.com/hc/en-us/articles/38777069364109-Connect-remote-MCP-servers-to-Zapier-using-MCP-Client),
  select Streamable HTTP, enter your reachable Loom `/mcp` URL, select OAuth **No** and
  put the JWT in Bearer Token. OAuth **Yes** ignores that token field and requires the
  [browser sign-in](#clients-that-sign-in-by-browser) route instead. MCP Client is beta
  and exposes tool calls; its Run Tool action can call a chosen Loom tool explicitly.
- **Make**: add an [MCP Client module](https://apps.make.com/c5ZhYquO55e90hoCJ0rzt),
  create a connection and choose **+ New MCP Server**. Enter your reachable Loom `/mcp`
  URL. The connection offers an API Key / Access token field; its documented instructions
  do not establish the outgoing header or bearer-prefix behavior for Loom, so verify
  those before treating an authenticated connection as working
  ([token errors](#bearer-or-bare-token)). Use **Call a tool** for an explicit tool call;
  **Execute an action with AI** also uses a model and consumes provider or Make credits.
  The client is open beta; a custom connection is separate from its verified-server list.
- **Replit Agent**: an install link, `https://replit.com/integrations?mcp=` followed by the
  base64 of `{"displayName": "Loom", "baseUrl": "https://loom.example.com/mcp"}`. A
  `headers` list (`[{"key": "Authorization", "value": "Bearer <JWT>"}]`) puts the token in
  the link, so a link you share leaves it out and relies on the
  [browser sign-in](#clients-that-sign-in-by-browser), which registers Replit through
  dynamic client registration at `/oauth/register`
  ([MCP reference](../../reference/mcp.md#oauth)).
- **Lovable**: Connectors → Add custom → MCP server, with a bearer token
  ([telling which](#bearer-or-bare-token)).
- **v0**: **+** → MCPs in the prompt form, authentication Bearer Token; the v0 Platform
  API's create-MCP-server reference caps the token at 1000 characters, so a longer JWT goes
  under Custom Headers as `Authorization: Bearer <JWT>`
  ([telling which](#bearer-or-bare-token)).
- **n8n**: the MCP Client Tool node, as in the table above.
- **Dify**: Integrations → Tools → MCP, with the URL, and `Authorization: Bearer <JWT>`
  under the custom headers in Advanced Options.
- **Microsoft Copilot Studio**: Tools → Add a tool → New tool → Model Context Protocol,
  authentication API key, type Header, header name `Authorization`; Microsoft's docs do
  not say whether the key needs the `Bearer ` prefix, so enter `Bearer <JWT>`
  ([telling which](#bearer-or-bare-token)).
- **GitHub Copilot cloud agent**: the repository's Settings → Copilot → MCP servers, with
  `{"mcpServers": {"loom": {"type": "http", "url": "URL", "tools": ["*"], "headers": {"Authorization": "Bearer $COPILOT_MCP_LOOM_TOKEN"}}}}`
  and the token in an Agents secret whose name starts with `COPILOT_MCP_`. It does not
  support a remote server's OAuth, so the header is the only route.
- **claude.ai and Claude Desktop**: Customize → Connectors → Add → Add custom connector on
  an individual plan, or Organization settings → Connectors → Add → Custom → Web on Team and
  Enterprise. Where the dialog shows Request headers, choose No sign-in and add
  `Authorization` with the value `Bearer <JWT>`, which is sent exactly as entered (up to
  four headers; `Authorization` cannot be a request header on an OAuth connection). The
  header cannot be edited later (remove and add the connector again), and on Team and
  Enterprise every member reaches Loom with that one token. Without Request headers, use the
  [browser sign-in](#clients-that-sign-in-by-browser).
- **ChatGPT**: a custom MCP server (Plugins → Add custom MCP server) authenticates with
  OAuth or not at all and sends no custom header, so it needs the
  [browser sign-in](#clients-that-sign-in-by-browser), at a public HTTPS address or through
  a Secure MCP Tunnel. Workspace permissions can restrict adding a custom server.
- **Grok**: grok.com/connectors → New Connector → Custom, with the URL. xAI's docs name
  OAuth and API keys but not the header an API key is sent in: where it asks for a key,
  enter `Bearer <JWT>` ([telling which](#bearer-or-bare-token)); for OAuth, use the
  [browser sign-in](#clients-that-sign-in-by-browser).
- **Perplexity**: the connector docs Perplexity publishes offer an API key or no
  authentication and do not say which header the key is sent in: enter `Bearer <JWT>` as its
  API key ([telling which](#bearer-or-bare-token)); `missing bearer token` means it sends
  another header. Where it offers OAuth, Loom's
  [browser sign-in](#clients-that-sign-in-by-browser) registers it as a public client with
  no client secret; that route has not been tested with Loom.
- **Le Chat** (Mistral's connector docs call the app Work): an admin adds it under
  Connectors → Add Connector → Custom MCP Connector; Mistral documents that it detects the
  authentication method from the URL. Choose **HTTP Bearer Token / Basic Auth** and enter
  the token as a bearer token, since Loom does not accept Basic
  ([telling which](#bearer-or-bare-token)).

The Flowise, Langflow, Zapier and Make instructions above follow their linked client
documentation. Their connection to Loom and model tool selection have not been tested.
These entries provide MCP tools, without the coding plugins' transcript capture or hooks.
The builders' MCP server features expose their own workflows to other clients; they are
separate from the MCP client features that call Loom.

### Bearer or bare token

Where a provider does not say whether it adds the `Bearer ` prefix, enter `Bearer <JWT>`,
then read Loom's answer if the connection fails. A refused token answers `401` with
`unauthenticated: <reason>`, and its `WWW-Authenticate` header carries the reason as
`error_description` beside `error="invalid_token"`. A request with no `Authorization` header
answers `401` with `missing bearer token`, without the `unauthenticated:` prefix or an
`error_description`. Read the table from the top: the first row whose text the error
contains applies.

| The error contains                                                         | It means                                                                                                                            | Then                                                                                                                       |
| -------------------------------------------------------------------------- | ----------------------------------------------------------------------------------------------------------------------------------- | -------------------------------------------------------------------------------------------------------------------------- |
| `Invalid header`                                                           | the provider added its own prefix (`Bearer Bearer <JWT>`)                                                                           | enter the bare token                                                                                                       |
| `invalid token header:` or `Not enough segments`                           | the value is not a JWT: an unexpanded `${VAR}` or a truncated paste                                                                 | check the variable or secret expanded, and paste the whole token                                                           |
| ``authorization header must be `Bearer <token>` ``                         | the provider sent no `Bearer` scheme (a bare token, or Basic), or the scheme with no token after it                                 | enter `Bearer <JWT>` (check the token field is filled), or choose the provider's bearer option                             |
| `missing bearer token`                                                     | no `Authorization` header arrived                                                                                                   | check the header name, or the field the token went in                                                                      |
| `token expired`, says it is revoked, or says its sign-in session has ended | the token itself is refused                                                                                                         | mint a new one ([tokens and grants](../../guides/tokens-and-grants.md))                                                    |
| `invalid token:`, `untrusted issuer` or `has no … key`                     | a token cut short when pasted (it often reads `Signature verification failed`), or one minted by another Loom or with a retired key | paste the whole token; if it still fails, mint one on this server ([tokens and grants](../../guides/tokens-and-grants.md)) |
| a `503` that says to retry shortly                                         | the server cannot check tokens yet (keys or revocation list not loaded or stale)                                                    | retry; the token may be good                                                                                               |

## Use a local model for the coding agent

The coding agent's model and Loom's server-side model jobs are configured separately.
Selecting Ollama for Loom does not select the model that your coding agent uses.

For an OpenCode agent, follow [Ollama's OpenCode configuration](https://docs.ollama.com/integrations/opencode)
to select a local model and set the client's context window. Its documented command
`ollama launch opencode --config` configures the integration without starting an
interactive agent session. Then add Loom using the OpenCode MCP entry above and the
[agent instructions](agent-instructions.md). The agent's model
must support the client's tool use and fit the available hardware; the MCP checks
above do not qualify a local model or measure a coding run.

Configure [Loom's models and keys](../../getting-started/models-and-keys.md) separately
for its server-side jobs.

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
