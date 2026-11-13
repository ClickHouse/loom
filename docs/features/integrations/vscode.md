# VS Code

VS Code's agent mode (GitHub Copilot) connects to Loom as a remote MCP server over
streamable HTTP. There is no VS Code extension: the `loom_*` tools are the integration,
with no session capture or code shunt; the [Claude Code](claude-code.md) and
[Codex](codex.md) plugins have those.

## Local single-user server

A single-user server on the same machine takes MCP calls without a token.
[Add Loom to VS Code](https://vscode.dev/redirect?url=vscode%3Amcp%2Finstall%3F%257B%2522name%2522%253A%2522loom%2522%252C%2522type%2522%253A%2522http%2522%252C%2522url%2522%253A%2522http%253A%252F%252Flocalhost%253A7777%252Fmcp%2522%257D) ·
[Add Loom to VS Code Insiders](https://insiders.vscode.dev/redirect?url=vscode-insiders%3Amcp%2Finstall%3F%257B%2522name%2522%253A%2522loom%2522%252C%2522type%2522%253A%2522http%2522%252C%2522url%2522%253A%2522http%253A%252F%252Flocalhost%253A7777%252Fmcp%2522%257D) adds a `servers.loom`
entry to the user profile's `mcp.json`, with the `type` and `url` the dashboard and
`loom install --client vscode` give VS Code. The links' payload, URL-encoded into
`vscode:mcp/install?<payload>`, is:

```json
{ "name": "loom", "type": "http", "url": "http://localhost:7777/mcp" }
```

The links open VS Code's install prompt. From a terminal,
`code --add-mcp '{"name":"loom","type":"http","url":"http://localhost:7777/mcp"}'` does
the same. Coding sessions that use the code index add `?toolset=coding` to `url`
([choosing a toolset](mcp-clients.md#choosing-a-toolset)).

## Any other server

Write the entry with its token:

```bash
loom install --client vscode --server-url https://loom.example.com --token "$LOOM_API_KEY" --toolset coding
```

This merges a `servers.loom` entry (`type: "http"`, `url` and an `Authorization` header)
into the user profile's `mcp.json`, leaving `settings.json` alone. To keep the token out
of the file, write the entry by hand with an input VS Code prompts for once and stores:

```json
{
  "servers": {
    "loom": {
      "type": "http",
      "url": "https://loom.example.com/mcp?toolset=coding",
      "headers": { "Authorization": "Bearer ${input:loom-token}" }
    }
  },
  "inputs": [
    { "id": "loom-token", "type": "promptString", "password": true, "description": "Loom token" }
  ]
}
```

Open the agent mode chat and check that the `loom_*` tools are listed;
[checking the connection](mcp-clients.md#checking-the-connection) covers a `401`.

## Agent instructions

Add the [agent instructions](agent-instructions.md) block to
`.github/copilot-instructions.md` so the agent knows when to call the tools, unless the
repository's `AGENTS.md` already carries it. The block names the `coding` toolset's
tools: connect with `?toolset=coding` on `url`; otherwise the tools it names that the
listing leaves out run through `loom_op`, as the block says.

## Related

- [Connecting an MCP client](mcp-clients.md): `loom install`, toolsets and the other editors.
- [MCP reference](../../reference/mcp.md): endpoint, auth modes and the tool list.
- [Integrations](index.md): everything Loom connects to.
