# Headless bots

A headless bot runs with no person to paste a token, notice a silent failure or say
"remember this". This guide covers the bot's identity, a connection check, three runtimes,
what the bot writes, and how to diagnose a bot that has gone quiet.

## 1. Give the bot an identity

The `loom` CLI ships in the server image and installs from a clone with `pip install -e .`.
The SDK package (`clickhouse-loom`) has no CLI.

**With self-signup.** On a server with `LOOM_SELF_SIGNUP=open` the bot signs itself up:

```bash
loom init --agent nightly-triage --server-url https://<loom> --purpose "Files the nightly failures."
```

`loom init`:

1. Creates the namespace `agent-nightly-triage` (`--purpose` becomes its description).
2. Grants a service-account subject `writer` on it.
3. Writes the token to `~/.loom/token` (mode 0600) and the URL, namespace and org to
   `~/.loom/config.json` (`--token-path` and `--config-path` move them).
4. Prints a claim code and the claim page's address (`/oauth/agent-claim`). A person enters
   the code there once, with a password of their own, and from then on signs in with the
   namespace name and that password as its admin. The code never signs in, and a second claim
   with it is refused. The bot's token keeps working.

`--ttl-days` sets the token lifetime (default 365). `--org` picks an org other than the
server's self-signup org.

**In CI**, print the token instead of writing files and store it as a secret. With
`--no-files` the token is the first line of the output:

```bash
loom init --agent ci-reviewer --server-url https://<loom> --no-files | head -n 1 > token.txt
```

With `--json` the sign-up result is printed as JSON without the token; with
`--json --no-files` the token rides inside that JSON, the only place it lands.

**Without self-signup**, an operator mints the token, or a person creates one under
**Settings → Tokens** that reads only the bot's namespaces:

```bash
loom mint-token --subject ci-reviewer --org <org> --namespaces agent-ci --roles writer \
  --service-account --ttl-days 30 --save
```

See [tokens and grants](tokens-and-grants.md) for roles, lifetimes and rotation.

## 2. Check the connection before the first run

```bash
LOOM_BASE_URL=https://<loom> LOOM_API_KEY="$(cat token.txt)" loom doctor --client
```

`loom doctor --client` resolves the server and the token the way the plugin hooks and the SDK
do, and says where each came from. It probes `/v1/health` and `namespace.list_mine`, and
checks that the token can write the bot's namespace. Each problem is a `FAIL` line naming the
fix, and the exit code is non-zero, so the command works as a CI gate:

```yaml
- run: loom doctor --client --server-url ${{ vars.LOOM_BASE_URL }} --token ${{ secrets.LOOM_API_KEY }}
```

`--json` emits the checks and the problem list as JSON for a pipeline that parses them.

## 3. Connect the runtime

### Claude Agent SDK or `claude -p`

Mount Loom as an MCP server from a file and allow only its tools. A headless run has no
interactive OAuth, so the bearer rides the header:

```bash
cat > loom.mcp.json <<EOF
{"mcpServers": {"loom": {"type": "http",
  "url": "https://<loom>/mcp?toolset=coding",
  "headers": {"Authorization": "Bearer $LOOM_API_KEY"}}}}
EOF
claude -p --mcp-config loom.mcp.json --strict-mcp-config \
  --allowedTools "mcp__loom__*" \
  --output-format stream-json --verbose \
  "Triage the failures in the last nightly run; record each root cause you find."
```

| Flag                            | Why                                                                                                  |
| ------------------------------- | ---------------------------------------------------------------------------------------------------- |
| `--strict-mcp-config`           | Keeps the bot on the servers the file names, not the user's config.                                  |
| `--allowedTools "mcp__loom__*"` | Pre-approves every Loom tool, so no permission prompt blocks the run.                                |
| `--output-format stream-json`   | The first message is the session's `system/init` (see below). `claude -p` needs `--verbose` with it. |
| `--bare`                        | Skips the user's hooks, plugins and `CLAUDE.md` for identical behaviour.                             |

> **Warning:** gate on the connection. The `system/init` message lists `mcp_servers[]` with a
> `status` each. Trust the run only when `loom` is `connected`; otherwise the model ran
> without memory.

Pick the tool listing with `?toolset=`; the toolsets are listed in
[MCP toolsets](../reference/mcp.md#toolsets).

If the Loom plugin is installed for the user running `claude -p`, its hooks run too
([Claude Code plugin](../features/integrations/claude-code.md)).

### Codex `codex exec`

```bash
export LOOM_API_KEY="$(cat token.txt)"
codex exec --json \
  -c 'mcp_servers.loom.url="https://<loom>/mcp?toolset=coding"' \
  -c 'mcp_servers.loom.bearer_token_env_var="LOOM_API_KEY"' \
  "Summarise what we decided about retries; record any decision that is missing."
```

Codex reads the bearer from the variable the entry names, in the environment `codex`
starts from. With the Codex plugin installed its hooks run here too
([Codex plugin](../features/integrations/codex.md)).

### The SDK, in the bot's own process

For a bot that is code rather than a coding agent, skip MCP and call the operations:

```python
from loom import Agent

async with Agent(org="acme", namespace="agent-nightly-triage", agent="nightly-triage") as a:
    hits = await a.memory.search("flaky tests in the ingest suite", top_k=5)
    await a.memory.set(
        "flaky/ingest_retry_test",
        "Times out against the S3 mock.",
        tags=["failure", "ingest"],
    )
    ans = await a.memory.ask("which suites flaked this week?")
```

`Agent` reads the server from `base_url=` or `LOOM_BASE_URL`, and the token from
`api_key=`, `LOOM_API_KEY`, then `~/.loom/token`. Other entry points:

- `agent.tools()` hands a model Loom's verbs (`loom_remember`, `loom_recall`, `loom_ask`,
  `loom_offload`, `loom_fetch`, `loom_note`) as tool definitions with one `execute()`.
- `agent.session(id)` is the unit an agent framework holds: resume, append, flush, close.
- A LangGraph graph passes `LoomStore` (`loom.integrations.langgraph`) to
  `compile(store=...)`; a Strands agent uses `LoomMemoryStore` (`loom.integrations.strands`).
- [`examples/claude_agent_loop.py`](../../examples/claude_agent_loop.py) is a minimal loop
  with the Anthropic SDK (needs `ANTHROPIC_API_KEY`).

See the [Python SDK reference](../reference/python-sdk.md) and
[framework integrations](../features/integrations/frameworks.md).

## 4. Decide what the bot writes

The plugins capture a coding session on their own. A bot outside them decides what to write.
In short: record decisions and failures, not transcripts; recall once before a task, not once
per step; hand a finished run's transcript to `memory.set_from_messages`; and report what the
run relied on with `run.complete(memories_used=[...])`.
[Agent integration](agent-integration.md#3-decide-what-to-write) covers each choice.

## 5. When the bot is quiet

Run `loom doctor --client` on the bot's machine. It reports each of the usual causes on its
own line:

| Symptom                               | Cause                                                                                               |
| ------------------------------------- | --------------------------------------------------------------------------------------------------- |
| Reads and writes go to another server | `LOOM_BASE_URL` disagrees with `~/.loom/config.json`                                                |
| 401 on every call                     | The token expired or was rotated                                                                    |
| 403 on every write                    | The token is a `viewer` on the namespace the bot writes to                                          |
| Captures never appear                 | A background flusher never delivered them: stranded `loom-flush-*.json` files in the temp directory |

## Related

- [Tokens and grants](tokens-and-grants.md)
- [Agent integration](agent-integration.md)
- [MCP clients](../features/integrations/mcp-clients.md)
- [Debugging a request](debugging.md)
