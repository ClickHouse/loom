# Shared memory for Claude Code, Codex and Cursor

Loom gives Claude Code, Codex and Cursor shared long-term memory through plugins or MCP
tools connected to the same self-hosted server. Give each teammate a token with access
to the team's namespace so their agents can find the facts others save. Install the
plugin or MCP entry below; the next session can recall what an agent saved. What the Claude Code and Codex
plugins send to Loom is in [data
handling](../features/integrations/claude-code.md#data-handling). Conversation recording
is off by default; the namespace owner turns it on with the **Record conversations**
switch on the dashboard's Conversations page, or with `conversation.set_logging`. To run a
server yourself, start with the [quickstart](quickstart.md); to have your agent install a
pinned release and prove it works, give it the latest release's
`https://github.com/ClickHouse/loom/releases/latest/download/agent-setup.txt` ([Set up
Loom with your coding agent](agent-setup.md)).

## What you need

![Settings → Tokens: a new token and the Claude Code setup it prints](../images/dashboard-connect.png)

- **The server URL**, for example `https://loom.example.com`, or `http://localhost:7777`
  for a stack started with `loom up`.
- **A writer token for your namespace**, except against a single-user server on the same
  machine: there the plugins and the install links for
  [Cursor](../features/integrations/cursor.md#mcp-tools) and
  [VS Code](../features/integrations/vscode.md#local-single-user-server) connect without one.
  Create one in the dashboard at
  `<server>/settings` → **Tokens**, or ask an operator to run
  `loom mint-token --subject you --org <org> --namespaces <namespace> --roles writer`.
  On a server with self-signup open, the Claude Code plugin can sign the machine up by
  itself on the first session. See [what happens with no credential](#no-token-yet).

<a id="claude-code"></a>

## How do I give Claude Code shared memory?

Install the Loom plugin with the server URL and a writer token for the namespace you
want to share. The plugin loads relevant memories at session start and gives the agent
tools to save and search facts.

```bash
claude plugin marketplace add ClickHouse/loom
claude plugin install loom@loom \
  --config "server_url=https://loom.example.com/mcp?toolset=coding" \
  --config token=<writer JWT>
```

Both `--config` values are optional; without them Claude Code asks once. Start a new
session. Loom's session primer arrives at the top, and from then on:

- the hooks send Loom what [data handling](../features/integrations/claude-code.md#data-handling)
  lists;
- the `loom-memory`, `loom-code`, `loom-session` and other skills tell Claude when to save
  a fact, when to search, and how to use the code index;
- `loom_search`, `loom_memory_set` and the code tools are available as MCP tools.

Everything the plugin installs, and how to configure it, is on the
[Claude Code](../features/integrations/claude-code.md) page.

<a id="codex"></a>

## How do I give Codex shared memory?

Install the Loom plugin, connect its hooks and MCP entry to the same server, and use a
token with access to the shared namespace. The plugin loads a memory primer when a
session starts; its hooks need the approval described below.

```bash
codex plugin marketplace add ClickHouse/loom
codex plugin add loom@loom
```

Then run `codex` interactively once and [approve the plugin's hooks](../features/integrations/codex.md#approve-the-hooks);
until then the tools work but nothing is captured.

The plugin's MCP entry points at `http://localhost:7777`. For another server, write its URL
and your token to `~/.loom/config.json` and override the MCP entry in
`~/.codex/config.toml`, as shown in [Codex: the server](../features/integrations/codex.md#the-server).

The Codex plugin ships the same hooks and skills; the differences are on the
[Codex](../features/integrations/codex.md) page. Don't also run `codex mcp add loom` with
the plugin installed: an entry with the same name replaces the plugin's.

<a id="cursor-and-other-mcp-clients"></a>

## How do I give Cursor or another MCP client shared memory?

Connect the client to Loom's MCP endpoint with a token for the shared namespace, then
add the agent instructions linked below. The tools save and recall facts; capturing
the session transcript also needs the client's hook integration.

On a Cursor Teams or Enterprise plan, a team admin can import the [Cursor
plugin](../features/integrations/cursor.md#plugin) (untested in Cursor), which carries the
MCP entry and Loom's skills.

For Cursor or VS Code against a single-user server on the same machine, no token is needed:
use the [Cursor](../features/integrations/cursor.md#mcp-tools) or
[VS Code](../features/integrations/vscode.md#local-single-user-server) install link. Otherwise, in the dashboard,
open **Settings → Tokens**, fill in the **New token** form (press **New token** first if a
token exists) and press **Create token**, then choose your client (Cursor, VS Code, Cline,
Windsurf, Zed, Gemini CLI, OpenCode or Claude Desktop) under **Connect**: the dialog shows the entry for its
config file, with the token in it. For any other MCP client, copy the entry from
[MCP clients](../features/integrations/mcp-clients.md). With the `loom` CLI installed
(`pip install -e .` from a clone of the repository), one command writes the entry instead:

```bash
loom install --client cursor --server-url https://loom.example.com --token "$LOOM_API_KEY"
```

Restart the client afterwards. `--client` also takes `claude-desktop`, `vscode`, `cline`,
`windsurf`, `zed`, `gemini` and `opencode`; without it, `loom install` wires every client it finds. These clients
get the tools; transcript capture needs a hook, which [Cursor](../features/integrations/cursor.md)
has and the others take from a [transcript template](../features/integrations/index.md#transcript-capture-hooks).
[MCP clients](../features/integrations/mcp-clients.md) covers toolsets and the entry the
command writes. Then add the [agent instructions](../features/integrations/agent-instructions.md)
block to a coding client's rule file, so the agent knows when to call the tools; for a
client other than Cursor, add `?toolset=coding` to the entry's URL (`--toolset coding`
with `loom install`) so the tools the block names are listed.

## Share a team namespace with separate credentials

Use an [authenticated multi-user deployment](quickstart.md#2-open-the-dashboard)
with TLS for remote clients. Single-user mode does not enforce team access boundaries.
The server must support create-if-absent memory writes through
`if_match_sha256: "absent"`, and its session-protection policy must admit the write.
Token minting and namespace roles alone do not establish these prerequisites.
A precondition or protection refusal stops this walkthrough; it does not qualify
the access check. Keep the precondition when diagnosing a refusal.

The operator chooses an ordinary, disposable memory namespace used only for this check,
shown as `acme/team-alpha`, with no existing `example/team-access-check` key. The other
team's example namespace, `team-beta`, is also a memory namespace, not an org-readable
code namespace. Restricted namespaces
also require their [explicit grants](../guides/tokens-and-grants.md#restricted-namespaces-classificationrestricted).

As the operator, run these commands in the server's signing-key environment, following
[Mint a token](../guides/tokens-and-grants.md#mint-a-token). The CLI ships with the server,
not the SDK. Do not give clients the signing key or use a wildcard namespace:

```bash
ALPHA_WRITER=$(loom mint-token --subject sa:team-author --org acme \
  --namespace-role team-alpha=writer --ttl-days 1)
ALPHA_READER=$(loom mint-token --subject sa:team-reader --org acme \
  --namespace-role team-alpha=viewer --ttl-days 1)
```

These are distinct authenticated principals because the token subjects differ. The
writer may save and read memory; the viewer may only read. Both tokens are scoped to
`team-alpha`. For ordinary plugin use, give each teammate their own scoped **writer**
token and follow the client setup above; the viewer here tests read-only access. Deliver
each token privately to its intended client, never in a shared repository or transcript.

With `curl` and `jq` installed, set the server URL and select the test row. Run the write
with the author's token and the read with the reader's token, on their respective clients.
The tokens below are shell variables containing the issued credentials, not placeholder
credentials to send literally:

```bash
LOOM_BASE_URL=https://loom.example.com
DEMO_ROW='{"org":"acme","namespace":"team-alpha","agent":"team-author","user_id":"sample-user","session_id":"team-check","memory_key":"example/team-access-check"}'

curl --fail-with-body --silent --show-error "$LOOM_BASE_URL/v1/memory.set" \
  -H "Authorization: Bearer $ALPHA_WRITER" -H 'Content-Type: application/json' \
  --data "$(jq '. + {value:"Review deployment changes before release.",if_match_sha256:"absent"}' <<<"$DEMO_ROW")"

curl --fail-with-body --silent --show-error "$LOOM_BASE_URL/v1/memory.get" \
  -H "Authorization: Bearer $ALPHA_READER" -H 'Content-Type: application/json' \
  --data "$DEMO_ROW"
```

The write's `absent` precondition refuses an existing live row; do not remove it to retry.
Require a successful write receipt, then `found: true`, the saved `value` and the selected
`agent`, `user_id` and `session_id` in the read. The server records the writer's token
principal as `actor`. The request's `agent` is a memory identity label, not an access grant
or a way to become that principal. The requested tuple is tried first; if absent,
`memory.get` can fall back to another agent's row for the same user. A returned tuple
that differs from the one selected does not pass this check.

Keep the reader token unchanged and request another team's namespace:

```bash
curl --silent --show-error --write-out '\nHTTP %{http_code}\n' \
  "$LOOM_BASE_URL/v1/memory.get" \
  -H "Authorization: Bearer $ALPHA_READER" -H 'Content-Type: application/json' \
  --data "$(jq '.namespace = "team-beta"' <<<"$DEMO_ROW")"
```

For this namespace-addressed read, expect HTTP 403 with `AUTH_INSUFFICIENT`, and no fact.
It is a scope check, not a test that a particular row exists in `team-beta`. A 401,
transport error, provider failure or session-protection refusal does not establish that
this check passed. Stop on unexpected responses and diagnose them; do not disable auth
or weaken a token's scope to make a check succeed. By-ID reads have a different
[missing-row contract](../concepts/identities-and-namespaces.md#who-may-call-what).

After confirming the positive read, the author can tombstone the example while the
disposable namespace remains otherwise unused:

```bash
curl --fail-with-body --silent --show-error "$LOOM_BASE_URL/v1/memory.delete" \
  -H "Authorization: Bearer $ALPHA_WRITER" -H 'Content-Type: application/json' \
  --data "$DEMO_ROW"
unset ALPHA_WRITER ALPHA_READER DEMO_ROW
```

Unsetting a shell variable does not revoke its token; follow
[token revocation](../guides/tokens-and-grants.md#revoke-and-rotate) when the test credentials
are no longer needed. These commands describe the expected contract, not a captured
deployment result. Record actual responses separately, then run the remaining
[shared-memory evaluation checks](evaluate.md#shared-memory-across-clients), including
revoked credentials and by-ID reads, before relying on a team deployment.

## Check that it works

1. In Claude Code, run `/loom:loom-status`. It reports the server the plugin calls, whether
   the token is accepted and can write the namespace, and any captured batches that never
   reached the server.
2. Tell the agent something durable, for example "our deploy window is Tuesday and
   Thursday only". In a new session, ask "when can we deploy?". The answer comes from Loom.
3. Open the dashboard (`<server>/`). The saved fact is there, and the session's
   conversation too while conversation recording is on; you can correct or delete either.

`loom doctor --client` adds the local wiring checks when you have the CLI.

## When the code index is used

The plugin attaches the code index only when the session starts inside a git checkout of
an indexed repository:

- It reads the checkout's remotes (`origin`, then `upstream`, then the others) and looks
  them up among the repositories the server indexes. A fork with no index of its own uses
  its parent's index.
- You need read access to that repository's code namespace. The dashboard's
  **Repositories** page shows what you can read and lets you join.
- On a match, the session's code questions are scoped to that repository, and grep and
  whole-file reads of indexed paths are answered from the index as of its latest indexed
  commit.
- With no match (a directory outside git, a repository nobody indexed, or one you can't
  read), nothing changes: the agent reads local files, and memory works as usual.

To use a repository's index anyway, name it: `LOOM_CODE_REPO=<owner>/<name>` (for example
`ClickHouse/loom`, or just `loom` when one repository you can read has that name), the
Claude Code plugin's `code_repo` option, or `"code_repo"` in `~/.loom/config.json` (the
setting a Codex session reads). It is a fallback: outside any git checkout, or in a
checkout whose repository has no index (a mirror with a different URL), the primer names
that repository's namespace and every `loom_code_*` call defaults to it. Inside an indexed
checkout, the checkout's own index still wins; to search another repository, pass its
namespace to the call. `local_first` (`LOOM_CODE_LOCAL_FIRST=on`) keeps
the index out of grep and file reads, for example while you work on uncommitted changes.
Details: [Claude Code](../features/integrations/claude-code.md#the-code-shunt).

## No token yet

On a server started with `LOOM_SELF_SIGNUP=open`, the first Claude Code session that finds
no token signs the machine up. It writes the token to `~/.loom/config.json` (mode 0600)
and a claim code to `~/.loom/claim-<namespace>.txt`, and the primer says how to claim the
namespace: the code works once, at the server's `/oauth/agent-claim` page, with a password of
your own. `LOOM_AUTO_SIGNUP=off` turns this off. A server without
self-signup leaves the session without a token, and the primer says how to get one.

## Next

- [Your first agent](first-agent.md): the same memory from your own code with the SDK.
- [Agent integration](../guides/agent-integration.md): what to save, when to search, and
  mistakes to avoid.
- [Code index](../features/code-index/overview.md): ask Loom about a repository the server
  indexes.
- [Troubleshooting](../operations/troubleshooting.md), starting from `/loom:loom-status`.
