# Plugin client checks

Read the checks in the order `SKILL.md` gives: the first FAIL is the cause
to fix. Every action below is the user's to take or approve; the skill
names it and does not run it.

| check | FAIL means | next action |
|---|---|---|
| `ensure-node.sh --check` | `/bin/sh` finds no Node 18+, so no hook runs | with the user's yes, run `ensure-node.sh` without `--check`; then restart the session |
| `server` | nothing answers at the URL the hooks use | point the client at the reachable server (`LOOM_BASE_URL`, the plugin's `server_url` option, or `url` in `~/.loom/config.json`) |
| `token` | no readable JWT, or it expired | get a new writer token from /me → Setup and update the plugin config |
| `auth` | the server rejects the bearer (401/403) | refresh the token; hooks fail open and otherwise lose captures |
| `namespace` | the token's listed grant cannot write the hook namespace | get a writer grant, or set `LOOM_NAMESPACE` / `namespace` in `~/.loom/config.json` to one it can write |
| plugin registration | `loom` is not an enabled plugin, or its MCP server is not connected | `claude plugin enable loom` (Codex: `codex plugin add loom`), then restart the session; a failed MCP connection with every check above passing points at the URL the MCP entry uses (Claude Code: the `server_url` option; Codex: the plugin's `.mcp.json`) |
| `flush` (WARN) | capture jobs older than ten minutes never reached the server (the flusher that owned them died) | fix the lines above, then replay each file with `node "<root>/hooks/loom-flush.mjs" <file>`; it deletes the file when done. Conversation turns also retry on the next Stop, deduplicated by message id |

`loom doctor --client` may add more checks when the optional Python CLI is
installed. Never report a namespace write as verified from a listed grant.
