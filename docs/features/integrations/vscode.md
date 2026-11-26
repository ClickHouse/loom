---
description: Connect GitHub Copilot in VS Code to Loom over MCP. Save project decisions and recall them across chats and editors with access to the same namespace.
seo:
  title: Shared agent memory for VS Code and Copilot
---

# VS Code

GitHub Copilot in VS Code can save and retrieve project decisions through Loom's
MCP server over streamable HTTP. Connect clients to the same Loom server and
give them access to the same namespace to share those decisions.

There is no VS Code extension: the `loom_*` tools are the integration,
with no session capture or code shunt; the [Claude Code](claude-code.md) and
[Codex](codex.md) plugins have those.

## Local single-user server

Use the tokenless setup when the Copilot harness can reach your single-user Loom server
at `localhost:7777` from the machine or container where the harness runs. Otherwise, use
an [authenticated Loom server](#any-other-server) the harness can reach.

In a chat with the **Copilot** harness selected, run **Chat: Open Customizations**,
open **MCP Servers**, and select **Add Server** in the **User** group. Choose HTTP,
enter `http://localhost:7777/mcp`, name the server `loom`, and select
**Global → Copilot Global**. Add
`?toolset=coding` to the URL if the session uses the code index
([choosing a toolset](mcp-clients.md#choosing-a-toolset)).

### Install links for the editor profile

[Add Loom to VS Code](https://vscode.dev/redirect?url=vscode%3Amcp%2Finstall%3F%257B%2522name%2522%253A%2522loom%2522%252C%2522type%2522%253A%2522http%2522%252C%2522url%2522%253A%2522http%253A%252F%252Flocalhost%253A7777%252Fmcp%2522%257D) ·
[Add Loom to VS Code Insiders](https://insiders.vscode.dev/redirect?url=vscode-insiders%3Amcp%2Finstall%3F%257B%2522name%2522%253A%2522loom%2522%252C%2522type%2522%253A%2522http%2522%252C%2522url%2522%253A%2522http%253A%252F%252Flocalhost%253A7777%252Fmcp%2522%257D)

The links open the server details in VS Code. Review the configuration and select
**Install**. Both links carry this server configuration:

```json
{ "name": "loom", "type": "http", "url": "http://localhost:7777/mcp" }
```

To install directly from a terminal, run:

```bash
code --add-mcp '{"name":"loom","type":"http","url":"http://localhost:7777/mcp"}'
```

Both methods add `servers.loom` to the editor profile's `mcp.json`. The dashboard
provides a matching configuration to copy into that file. The Copilot harness can receive
[eligible editor entries](https://code.visualstudio.com/docs/agents/reference/mcp-configuration#_editor-forwarding-to-copilot)
from a connected editor.

## Any other server

On the host running the Copilot harness, add the entry below to `mcpServers` in
`$COPILOT_HOME/mcp-config.json`, or `~/.copilot/mcp-config.json` if `COPILOT_HOME` is
unset. For a remote session, edit the file on the remote host or in the container
where the harness runs. Replace `https://loom.example.com` with your server URL. Set
`LOOM_API_KEY` to your [Loom token](../../getting-started/connect-a-coding-agent.md#what-you-need)
in the Agent Host's launch environment so the token stays out of the file. If you
change that environment, restart the client or Agent Host so it inherits the variable.

```json
{
  "mcpServers": {
    "loom": {
      "type": "http",
      "url": "https://loom.example.com/mcp?toolset=coding",
      "headers": { "Authorization": "Bearer ${LOOM_API_KEY}" },
      "tools": ["*"]
    }
  }
}
```

See the [Copilot configuration reference](https://code.visualstudio.com/docs/agents/reference/mcp-configuration)
for other destinations and authentication options. For a cloud session that uses
another provider, use that provider's MCP configuration.

### Editor profile for the Local harness

If you still use the Local harness, write the editor entry with its token:

```bash
loom install --client vscode --server-url https://loom.example.com --token "$LOOM_API_KEY" --toolset coding
```

This merges a `servers.loom` entry into `mcp.json` for the default profile in
VS Code Stable. To edit the file in a named profile or VS Code Insiders, switch to
that profile, run **MCP: Open User Configuration**, and choose
**VS Code User Configuration** if prompted. If the picker only offers Copilot
configuration, run **MCP: Add Server**, choose **HTTP**, enter your Loom MCP URL,
name the server `loom`, and choose **Global → User Configuration (deprecated)**
to create the editor file. In `mcp.json`, add or update the `loom` entry under
`servers` and add the `loom-token` input to have VS Code prompt for and store the
token. Keep the other entries in the file:

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

The `${input:loom-token}` form works in the Local harness. Editor entries that use
input prompts cannot be forwarded to the Copilot harness.

## Verify the connection

In a chat with the session target you configured, ask the agent to call
`loom_activate`. Expand the tool call and check that its result contains the Loom
session primer.
If the tool is missing, check the session target and where you saved the entry.
If Copilot returns `401`, check that the Agent Host inherited `LOOM_API_KEY`, then see
[checking the connection](mcp-clients.md#checking-the-connection).
To check server-side recall across clients on an authenticated server, use the
[two-session memory check](mcp-clients.md#verify-shared-memory-through-mcp).

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
