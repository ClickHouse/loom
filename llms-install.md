# Install Loom as an MCP server

This file is for a coding agent asked to set Loom up as an MCP server for its user.
People can read the [README](README.md) and the
[quickstart](docs/getting-started/quickstart.md). Claude Code and Codex have their own
setup page: [agent-setup.md](docs/getting-started/agent-setup.md).

Loom runs on the user's machine as a Docker Compose stack (the Loom server, its document
extractor and ClickHouse) and serves MCP over streamable HTTP. A coding agent connects on
`http://localhost:7777/mcp?toolset=coding`. Loom needs no API key and no account.

## 1. Check for a running Loom

```bash
curl -fsS http://localhost:7777/v1/health
```

A JSON reply with a `version` field means Loom already runs there: go to step 3.

## 2. Install

Docker with the Compose plugin, version 2.20 or later, must be installed
(`docker compose version`). If that command fails or reports an older version, stop and
tell the user; do not install or upgrade system packages. On Windows, run the commands in
a WSL 2 terminal where `docker` works.

The installer reads `LOOM_*` environment variables, which can point it at another
ClickHouse, port or directory. If `env | grep '^LOOM_'` prints any, stop and ask the user
whether to keep them.

Run from the user's home directory:

```bash
curl -fsSL https://github.com/ClickHouse/loom/releases/latest/download/install.sh -o loom-install.sh && LOOM_CONNECT=none sh loom-install.sh && rm loom-install.sh
```

- `LOOM_CONNECT=none` stops the installer from setting up other agents, since you add the
  MCP entry yourself in step 3.
- The installer verifies the release bundle, pulls the images and waits until
  `/v1/health` answers. The first run takes a few minutes.
- If it says port `7777` is in use, run the command again with `LOOM_PORT=7778` in front
  of `sh`, and use that port below.
- The install lives in `~/.loom`; `LOOM_INSTALL_DIR` chooses another directory.

## 3. Add the MCP server

The stock install is single-user: it has no sign-in and needs no token. On a deployment
with sign-in on, ask the user for a token from **Settings → Tokens** in the dashboard and
add it as a `headers` entry, `"Authorization": "Bearer <token>"`, beside `url` in the
JSON below (`--header "Authorization: Bearer <token>"` on the `cline mcp add` command).

The Cline extension, in `cline_mcp_settings.json`:

```json
{
  "mcpServers": {
    "loom": {
      "type": "streamableHttp",
      "url": "http://localhost:7777/mcp?toolset=coding"
    }
  }
}
```

The Cline CLI, with
`cline mcp add loom 'http://localhost:7777/mcp?toolset=coding' --transport http --yes`, or
in `~/.cline/data/settings/cline_mcp_settings.json`:

```json
{
  "mcpServers": {
    "loom": {
      "transport": {
        "type": "streamableHttp",
        "url": "http://localhost:7777/mcp?toolset=coding"
      }
    }
  }
}
```

The [MCP clients page](docs/features/integrations/mcp-clients.md) gives the entry for
other clients.

## 4. Check it

1. Reload the MCP servers. The tool list includes `loom_memory_set` and `loom_search`.
2. Call `loom_memory_set` with `memory_key` `setup/check` and a short `value`, then
   `loom_memory_get` with the same key: it returns the value.
3. Call `loom_memory_delete` with that key.
4. Tell the user that the dashboard is at `http://localhost:7777/`.

## Optional: index a repository

The code tools (`loom_code_search`, `loom_code_symbol` and the others) answer once a
repository is indexed. On the released stack, add it to `LOOM_CODE_REPOS_JSON`
([Index a repository](docs/getting-started/quickstart.md#optional-index-a-repository-on-the-released-stack)).
