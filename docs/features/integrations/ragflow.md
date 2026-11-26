# RAGFlow

Connect a RAGFlow agent to an authenticated Loom deployment over Streamable HTTP
so it can look up a handoff saved in Loom.

## Connect Loom

1. In Loom, create a `viewer` token scoped to the namespace that holds the handoff.
   [Tokens and grants](../../guides/tokens-and-grants.md#one-token-for-every-use)
2. In RAGFlow, open **User settings → MCP → Add MCP**.
3. Enter `loom` as the name. Set **URL** to your Loom server's HTTPS endpoint with
   `/mcp?toolset=readonly`, set **Server type** to `streamable-http`, and paste the
   viewer token into **Authorization Token**.
4. Refresh **Tools available**. Confirm that `loom_search` appears, then save the
   connection.

The RAGFlow backend must be able to reach the Loom URL. Check the allowed addresses
and other [connection requirements](https://ragflow.io/docs/connect_external_mcp_server#connection-requirements).

The workflow uses the connection's token for each lookup. Limit that token to the
namespace the workflow may read, and share the workflow with people authorized to
see the results.

## Check a handoff lookup

1. In an **Agent** component, add the Loom connection and enable `loom_search`.
2. Ask the agent for a handoff you know is saved in the namespace. For example,
   ask what the last deployment handoff said about the cutover.
3. Inspect the run's tool calls. Check that the agent called `loom_search` and that
   the result includes the expected handoff.

See [Unified search](../unified-search.md) for what `loom_search` returns.
