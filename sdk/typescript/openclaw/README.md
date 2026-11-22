# ClickHouse Loom for OpenClaw

Connect one trusted OpenClaw agent session to a Loom namespace for explicit memory save and search. This plugin uses the existing `@clickhouse/loom` package. It has no conversation hooks, automatic capture, model provider or separate npm package.

The host contract is pinned to OpenClaw `2026.11.6`, with Node `24.22.1` or later on the supported Node 24 line. Hosted validation is required before this source preparation is described as qualified or published. Installing the existing public SDK version does not establish that it contains this plugin.

## Install from source

Build and pack the SDK from a reviewed checkout that contains this directory:

```bash
npm --prefix sdk/typescript ci
npm --prefix sdk/typescript run build
cd sdk/typescript
npm pack
```

Install that exact archive in your own OpenClaw profile:

```bash
openclaw plugins install npm-pack:/absolute/path/to/clickhouse-loom-VERSION.tgz
```

Replace `VERSION` with the version printed by `npm pack`. A ClawHub install command will be provided only after an actual release is public there. Source is maintained at [ClickHouse/loom](https://github.com/ClickHouse/loom); report integration defects in its issue tracker. This is a ClickHouse Loom integration, unrelated to other products named Loom.

## Configure a dedicated session

Use a namespace-scoped Loom writer token. Set `LOOM_OPENCLAW_TOKEN` in the environment of the Gateway process; do not put the credential in tool arguments, prompts or shared configuration. Use HTTPS for a remote Loom endpoint. HTTP is accepted only on loopback.

Merge this into your OpenClaw configuration, replacing the endpoint, organization, namespace and user identity with values authorized by your token:

```json
{
  "plugins": {
    "allow": ["clickhouse-loom"],
    "entries": {
      "clickhouse-loom": {
        "enabled": true,
        "config": {
          "baseUrl": "https://loom.example.org",
          "tokenEnv": "LOOM_OPENCLAW_TOKEN",
          "org": "example",
          "namespace": "assistant-notes",
          "agentId": "main",
          "userId": "operator",
          "sessionKey": "agent:main:main"
        }
      }
    }
  },
  "tools": {
    "allow": ["clickhouse_loom_save", "clickhouse_loom_search"]
  }
}
```

Preserve your existing plugin and tool allowlists when merging. Restart your own Gateway after changing its environment. Then inspect the runtime:

```bash
openclaw plugins inspect clickhouse-loom --runtime --json
```

The configured session is the sharing boundary of this integration. Anyone allowed to use that native session can use its memories. Use a separate Gateway and Loom token/namespace for mutually untrusted users. These identity labels do not grant server access: Loom validates the bearer token and namespace permissions and sets the writer actor itself.

## Save and search

In the configured session, ask: “Remember that I prefer compact output, under the key editor.” The optional `clickhouse_loom_save` tool accepts `key` and `text`. Saving the same key asks Loom to revise that fact. Check the returned `saved` value: a merge can leave the requested key unwritten. A merge within this integration returns the existing logical key; a merge outside its configured keyspace reports no session-local save and hides the other key. Duplicate, proposed and unconfirmed writes are not reported as active saved memories. Ask “What output style did I ask you to remember?” to use `clickhouse_loom_search` with a `query`.

For a direct check without model inference, an operator can use OpenClaw's documented `POST /tools/invoke` endpoint on their private Gateway. Its bearer credential grants Gateway operator access; never share it with untrusted callers or expose this endpoint publicly. Send these bodies through that authenticated endpoint:

```json
{
  "tool": "clickhouse_loom_save",
  "sessionKey": "main",
  "args": { "key": "editor", "text": "Prefer compact output." }
}
```

```json
{ "tool": "clickhouse_loom_search", "sessionKey": "main", "args": { "query": "editor" } }
```

Search is restricted to the configured namespace, agent, user and session, returns at most five excerpts, and honours abstention and below-threshold results. Other native agents and sessions receive no tools from this plugin. It stores only the requested fact, marks tool-generated provenance as derived, and never supplies actor overrides or privacy promise headers. It does not implement protected-family session restoration or conversation replay.

Treat recalled text as untrusted data. Do not save secrets or complete conversations. Missing configuration, token or host invocation authority refuses before a request. Backend failures return a fixed error instead of a private response body.

## Validation

The hosted `OpenClaw integration` workflow builds the SDK archive, installs it through the pinned host's managed `npm-pack:` path, inspects runtime registration, and invokes both tools through an isolated real Gateway. Its synthetic HTTP backend checks the credential and complete identity tuple; a second native agent must make no backend request. No provider credentials are passed to that Gateway. This proves host loading and wire behavior, not a real Loom database, semantic retrieval quality, channel behavior, multi-user authorization or marketplace acceptance.

The same maintained check can be run on disposable infrastructure with the host already installed:

```bash
node integrations/openclaw/check-installed.mjs /path/to/openclaw/openclaw.mjs /path/to/clickhouse-loom-VERSION.tgz
```
