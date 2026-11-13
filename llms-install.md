# Install Loom as an MCP server

This file is for a coding agent asked to set Loom up as an MCP server for its user.
People can read the [README](https://github.com/ClickHouse/loom#readme) and the
[quickstart](https://clickhouse.github.io/loom/getting-started/quickstart/). Claude Code and Codex have their own
setup contract:
[agent-setup.txt](https://github.com/ClickHouse/loom/releases/latest/download/agent-setup.txt).

Loom runs on the user's machine as a Docker Compose stack (the Loom server, its document
extractor and ClickHouse) and serves MCP over streamable HTTP. A coding agent connects on
`http://localhost:7777/mcp?toolset=coding`. Loom needs no API key and no account.

Do not use `sudo`, install or upgrade system packages, or change Docker's setup. When a
command fails, or a step below says to stop, stop and show the user the command's last
lines.

## 1. Check for a running Loom

On Windows, run every shell command in steps 1 to 3 (`curl`, `docker`, the `~/.loom`
check, `env` and the installer) in a WSL 2 terminal where `docker` works. Cline on Windows
keeps its configuration on Windows: write the step 3 entry there, not inside WSL.

```bash
curl -sS --noproxy '*' http://localhost:7777/v1/health
```

- A JSON reply with a `schema_version` field is a Loom server. If its `status` is `ok`, go
  to step 3. Otherwise stop and show the reply to the user.
- If no Loom answers (no reply, or one without `schema_version`) but
  `~/.loom/compose.yaml` exists, stop and ask the user: an install is there that is
  stopped or runs on another port (`LOOM_PORT` in `~/.loom/.env`), and running the
  installer again would start or upgrade it.

## 2. Install

Docker with the Compose plugin, version 2.20 or later, must be installed
(`docker compose version`). If that command fails or reports an older version, stop.

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
- When it finishes, it prints `Dashboard: http://<host>:<port>/`. Use that port in the
  steps below.
- If it says port `7777` is in use, run the command again with the `LOOM_PORT=…` its
  message names in front of `sh`.
- The install lives in `~/.loom`; `LOOM_INSTALL_DIR` chooses another directory.

## 3. Add the MCP server

Add a `loom` entry under `mcpServers` and keep every other entry in the file. If a `loom`
entry is there already, ask the user before changing it.

The stock install is single-user: it has no sign-in and needs no token. To check, list the
tools without one:

```bash
curl -s --noproxy '*' -o /dev/null -w '%{http_code}\n' -X POST 'http://localhost:7777/mcp?toolset=coding' -H 'Content-Type: application/json' -H 'Accept: application/json, text/event-stream' -d '{"jsonrpc":"2.0","id":1,"method":"tools/list"}'
```

`200` means no token is needed. `401` means sign-in is on: ask the user for a token from
**Settings → Tokens** in the dashboard. Add it as a `headers` entry,
`"Authorization": "Bearer <token>"`, beside `url` in the entry below, or with
`--header "Authorization: Bearer <token>"` on the `cline mcp add` command. A `401` on a
later tool call, in step 4, means the same: ask for a token and add it.

The Cline extension: open its settings file from **MCP Servers → Configure MCP Servers**
in Cline. The entry:

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

The Cline CLI: run
`cline mcp add loom 'http://localhost:7777/mcp?toolset=coding' --transport http --yes`, or
add this entry to `~/.cline/data/settings/cline_mcp_settings.json`:

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

The [MCP clients page](https://clickhouse.github.io/loom/features/integrations/mcp-clients/) gives the entry for
other clients.

## 4. Check it

1. Reload the MCP servers. The tool list includes `loom_memory_set` and `loom_search`.
2. Pick a key `setup/check/<random digits>` and call `loom_memory_get` on it. If it is
   found, pick another key.
3. Call `loom_memory_set` with that `memory_key` and a short `value`, then
   `loom_memory_get` with the same key: it returns the value.
4. Call `loom_memory_delete` with that key.
5. Tell the user where the dashboard is: the `Dashboard:` address the installer printed,
   or `http://localhost:7777/` when Loom was already running.

## Optional: index a repository

The code tools (`loom_code_search`, `loom_code_symbol` and the others) answer once a
repository is indexed. On the released stack, add it to `LOOM_CODE_REPOS_JSON`
([Index a repository](https://clickhouse.github.io/loom/getting-started/quickstart/#optional-index-a-repository-on-the-released-stack)).
