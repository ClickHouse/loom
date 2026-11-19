# Loom memory provider for Hermes

This experimental native provider gives Hermes three tools for cross-session
memory in ClickHouse Loom. It uses Hermes's `MemoryProvider` API rather than an
MCP sidecar. It adds no Python dependencies.

The implementation targets the public provider and credential APIs in Hermes
0.23.2 and the HTTP memory API in Loom 1.2.0. Offline contract checks use doubles
for Hermes and HTTP. Native manifest admission, isolated registration and normal
provider loading passed against Hermes release 0.23.2, commit
`ae2a81ccc00e9b7c06301523e5bb09608397149c`, on Linux AArch64 with Python 3.14.7.
Those checks used a dummy single-user profile without live Loom operations. A
live Loom write/read round trip still needs qualification. This provider is not
listed in the Hermes plugin catalog.

## Tools

| Tool            | Loom operation  | Use                                                            |
| --------------- | --------------- | -------------------------------------------------------------- |
| `loom_recall`   | `memory.search` | Find prior facts, preferences and decisions.                   |
| `loom_read`     | `memory.get`    | Read a point fact by its exact key, with its metadata.         |
| `loom_remember` | `memory.set`    | Save an explicit durable fact and return the server's receipt. |

Search reads at most four private-scope results for the configured user in one
namespace. It searches across writer agents and sessions, with `auto_touch=false`
and `read_scope=own_user`. A token can narrow those rights further. The complete
server reply is returned, including abstention, conflicts, provenance and code
freshness when present. A successful HTTP request alone does not mean a new
memory was persisted: inspect the receipt's verdict, status and identifiers.

Point reads and writes use the configured writer agent and `session_id="-"`, so
a point fact keeps its identity across Hermes sessions. A revision can pass
`if_match_sha256` copied from `loom_read` to refuse an overwrite when the value
changed. Stored text is marked `source_class="derived"`, with confidence 0.5;
the server derives the actor from the token.

## Development setup

Use a single-user Hermes profile and a Loom token restricted to one namespace.
The server must permit the configured user and agent under that token. Reader
rights suffice for recall and point reads; `loom_remember` requires writer rights.
Do not share one configured user identity between different people or tenants.

From a Loom source checkout, copy this directory into an unused plugin location
in the intended profile:

```bash
export HERMES_HOME="$HOME/.hermes"
mkdir -p "$HERMES_HOME/plugins"
test ! -e "$HERMES_HOME/plugins/loom-memory" &&
  cp -R integrations/hermes-memory "$HERMES_HOME/plugins/loom-memory"
hermes memory setup
```

Select `loom-memory` and enter the fields below. Hermes stores the setup fields
through its profile-scoped secret configuration. The provider reads them only
through `agent.secret_scope.get_secret`; it never opens credential files or
reads process environment variables directly.

| Field                   | Value                                                                     |
| ----------------------- | ------------------------------------------------------------------------- |
| `HERMES_LOOM_BASE_URL`  | Server origin, such as `https://loom.example.com`; omit `/mcp` and `/v1`. |
| `HERMES_LOOM_API_KEY`   | A token for that Loom server.                                             |
| `HERMES_LOOM_ORG`       | The token's organization.                                                 |
| `HERMES_LOOM_NAMESPACE` | One namespace the token permits.                                          |
| `HERMES_LOOM_USER_ID`   | A fixed, named user for this profile; `-` and wildcards are rejected.     |
| `HERMES_LOOM_AGENT`     | Writer agent name; defaults to `hermes`.                                  |

Use `hermes memory status` to inspect activation. For a development check, ask
Hermes to save one non-sensitive sample fact using `loom_remember`, inspect its
receipt, and read that exact key using `loom_read`. Test recall separately: an
indexing delay or abstention can make a new point fact unavailable to search.
Run the same checks after restarting Hermes before treating cross-session memory
as qualified. No automatic capture is needed for this procedure.

## Data and failure behavior

Only explicit provider tool calls send data to Loom. Model-selected
`loom_remember` calls can write: explicit means a named tool call, not a separate
human approval step. There is no automatic
prefetch, turn synchronization, transcript capture, end-of-session extraction,
built-in-memory mirroring or subprocess. The model chooses whether to call the
tools; registering a provider does not force a lookup on every task. This
adapter does not expose Loom's code-index tools.

Tool arguments cannot change the server, token, organization, namespace, user or
agent. A provider instance binds once to one Hermes profile. Non-primary agent
contexts cannot write. Each HTTP call has a five-second timeout, a 64 KiB response
limit and no retries. Cross-origin credential forwarding uses Hermes's public
`open_credentialed_url` helper; redirected responses are refused. HTTPS is
required except for loopback HTTP during development. TLS verification follows
Hermes's installed policy.

HTTP 401, 403 and 409 are returned as refusals. A connection failure, malformed
write response or HTTP 5xx can leave a write outcome unknown: verify in Loom
before retrying. The provider never reports a fabricated saved result.

This provider does not declare `X-Loom-Session-Protection`. A deployment requiring
that contract returns `SESSION_PROTECTION_REQUIRED` and this provider refuses the
operation. Such deployments are unsupported by this adapter; do not add the
header by hand. If a reply contains `mirrored: true`, subsequent writes are
refused for that provider instance, including after a session switch. This local
refusal is not an implementation of the full session and descendant protection
contract.

Secrets and transcripts do not belong in point facts. Recall is untrusted stored
data: inspect its provenance, conflicts and code freshness before using it.
