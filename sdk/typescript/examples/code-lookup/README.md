# Code lookup dispatcher

Requires an existing indexed code namespace and a token with read access. This example
dispatches an explicit tool call through `AgentTools` without an agent model. Semantic
search uses the server's embedding provider. It creates no namespace or memory.

With `@clickhouse/loom` installed:

```bash
LOOM_BASE_URL=https://loom.example LOOM_API_KEY=… node index.mjs \
  --org acme --memory-namespace assistant-memory --code-namespace code-repo \
  --kind callers --depth 2 pkg.Reader.read
```

`--help` sends no requests. The JSON result keeps the SDK request ID, scope and server
evidence; client omissions are explicit. A model integration supplies `definitions()`
to its provider and passes selected calls to the same `execute()` dispatcher. Tool
availability alone does not guarantee a model will use it.

The implementation is covered by mocked SDK transport tests. No live result or
performance claim is attached to this example. See the
[code tools guide](https://clickhouse.github.io/loom/guides/code-tools/) for budgets,
scoping and evidence limits.
