# Loom nodes for n8n

Loom Chat Memory keeps an n8n AI Agent's conversation history in your Loom deployment.
The Loom action node remembers facts, recalls memories and searches knowledge documents;
it can also connect to an AI Agent as a tool. Data stays in your Loom deployment on
ClickHouse or Postgres. These nodes add no database or cache.

This package targets self-hosted n8n. It bundles Loom's SDK and its framework libraries;
it is not verified for n8n Cloud. The node callbacks and the framework memory interface
are tested with `n8n-workflow` 2.47.2, `@langchain/classic` 1.0.27 and
`@langchain/core` 1.4.0. A full n8n application is not part of the automated tests.

## Install

Needs Node.js 24 and a self-hosted n8n deployment using `n8n-workflow` 2.47.
Install `@clickhouse/n8n-nodes-loom` from n8n's Community Nodes settings after the
package is published, then restart n8n.

To build and install from this repository:

```sh
cd integrations/n8n
corepack pnpm install --frozen-lockfile
corepack pnpm check
corepack pnpm pack --out loom-nodes.tgz
```

Install the tarball into your n8n community nodes directory with `npm install
/path/to/loom-nodes.tgz`, then restart n8n. See
[n8n's manual installation guide](https://docs.n8n.io/integrations/community-nodes/installation/manual-install/).

## Configure

Create a Loom API credential with your Loom server URL and a writer token for the
namespace. The URL must be reachable from the n8n worker. Use HTTPS outside a private
development network. n8n keeps the credential; the node sends the token only to that
server. The credential's connection test checks the authenticated `/v1/me` route.

Set Organization, Namespace and Agent Name on each node. User ID filters that user's
memories. Leaving it empty writes shared memories and Recall searches every user
allowed by the token in this namespace. Token grants enforce access. Give mutually
untrusted users separate namespaces and restricted tokens.

- **Loom Chat Memory:** connect its Memory output to an AI Agent's Memory input. Set
  Session ID to a conversation identifier, such as `{{ $json.sessionId }}` from a Chat
  Trigger. Workflow ID, user and session identify the history, so another workflow or
  user with the same session ID has its own history. Context Window Length controls
  how many previous interactions the agent sees; older messages stay stored until cleared.
- **Loom:** select Remember, Recall or Search Knowledge. Remember takes a memory key and
  text value; the searches take a query and result limit. Connect it as an agent tool
  or use it as an action in a workflow. A Loom refusal fails the node unless n8n's
  continue-on-fail option is selected, in which case it returns an error item.

Conversation messages are episodic memories, kept verbatim rather than extracted into
facts. Independent processes can resume one history; simultaneous writers have no
transaction across a whole turn. The memory adapter's `clear()` removes only that
workflow/user/session's messages. It does not remove memories written by Remember.

## Check

```sh
corepack pnpm check
LOOM_BASE_URL=http://localhost:7790 corepack pnpm test
```

The first command builds, lints and runs fake-server tests. The local-server test skips
unless `LOOM_BASE_URL` is set; point it only at a private development Loom. It writes,
resumes and clears a conversation, including a long message and a second user's empty
history. No model provider is called.

## Publishing

The repository's `n8n-publish.yml` workflow checks pull requests and publishes on a stable
Loom release tag. npm authentication uses GitHub OIDC and provenance, with no registry
token in the repository. An npm organization administrator registers the trusted
publisher for `@clickhouse/n8n-nodes-loom`, repository `ClickHouse/loom`, workflow filename
`n8n-publish.yml`, environment `npm-n8n`. Initial package registration may require an
administrator before trusted publishing is available.

Source and issues: [ClickHouse/loom](https://github.com/ClickHouse/loom).
