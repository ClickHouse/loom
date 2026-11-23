# Connect selected documents through MCP

An MCP source reads selected resources from an operator-approved remote server.
The server must expose readable resources or approved read and search tools.
Protocol compatibility alone does not establish that a service can supply documents.

Loom supports remote HTTPS Streamable HTTP with anonymous access or an existing
bearer for the exact approved endpoint and audience. OAuth enrollment and refresh,
local commands, stdio, legacy SSE and binary resource imports are outside this
source profile. Connecting never starts an arbitrary local server command.

## Set up a source

Open **Sources** from the Library, then choose an approved connection profile.
Check the displayed server origin, account-verification limits and capabilities.
A connection belongs to the signed-in owner. Another user's destination grant does
not let them use that owner's connection. This setup binds the supplied credential
to that owner; a team destination does not create a shareable account connection.

1. Connect the account with its authorized credential. Keep the request identifier
   until inspection confirms the result; an interrupted response is not evidence
   that connecting failed.
2. Discover capabilities. Browse the bounded metadata window or enter exact
   resource identities when enumeration is unavailable. Select the resources to read.
   A server with only action tools cannot become a source by marking them read-only.
3. Choose **Live**, **Snapshot** or **Selected refresh**. Live returns selected
   content on demand without importing it. Snapshot imports once; selected refresh
   re-reads the saved selection. Neither mode discovers future documents automatically.
4. Choose an ordinary private or team collection, or create one. A team child inherits
   its parent's grants. The automatic personal namespace cannot be a source destination.
5. Preview effective access and capability limits. Owner-only access requires both
   the source owner and destination permission. A team copy is a separate intentional
   publication: it requires operator approval, destination administrator authority
   and explicit confirmation for that selection. A shared destination alone does
   not publish private content.
6. Save once. If the outcome is uncertain, inspect using the original request
   identifier and connection slot before making another mutation.

The preview describes current rules and grants no authority. Changes to account
standing, grants, operator approval or selected scope can refuse a later request.
Collection grants describe local access, not a complete list of upstream readers.

## Read and maintain the selection

Live retrieval and direct reads use the existing search and protected-result paths.
Selected imports use ordinary knowledge documents, chunks, originals and revisions.
Their local citation identifies the source registration and exact resource; it is
not an upstream URL and grants no access. A protected participating client must
retain session proof and guard later automatic writes before it can receive private
results. Adding a header alone does not implement this contract.

A snapshot's current-read proof expires under the ordinary freshness policy.
Refresh it explicitly when needed. Selected refresh needs the source worker enabled;
a saved registration by itself does not import anything. A changed selection has a
new scope revision, and old successful reads cannot authorize it.

Pause and disconnect stop local access while retaining stored copies. Disconnect
does not revoke a bearer at its issuer. Remove explicitly retires the registration;
request document deletion separately when authorized. Missing resources, partial
pages and failed reads never authorize deletion. A schema mismatch can stop the
current binding and require a fresh owner-approved connection.

The supported contract does not synchronize upstream ACLs, prove a complete listing
or infer deletion. For a team copy, confirm that you may publish the exact material
under the destination's grants. Imported bytes and account history follow ordinary
retention and writer-requested erasure. Live lookup has no persistent result cache
or automatic memory save; the remote host and participating clients retain their
own requests, traces and conversations under their respective policies.

## API, SDK and CLI controls

The API exposes approved profiles, connect, list, inspect, discover, browse and
disconnect under `knowledge.connection.mcp_*`. Source preview, add, update, list,
inspect, pause, resume, refresh, read and remove use `knowledge.source.mcp_*`.
Ordinary `collection.*` and `grants.*` operations manage destinations. SDK operation
calls use the same arguments and authority; no client grants additional access.

Use the [source CLI guide](mcp-source-cli.md) for exact operation mappings, private
credential files, request reconciliation and the application-owned protected-session
context. The dashboard, CLI and SDKs require their actual protection implementation;
unbound automatic tools and nonparticipating applications are not qualified simply
because they can send HTTP.

Native document-source registrations remain retained and unavailable in the
replacement build. They are not converted into MCP connections. Their owners must
choose an approved endpoint, authorize a fresh account binding, select content and
preview access. Existing documents, originals, credentials and checkpoints are not
deleted as upgrade cleanup. Recording sources remain separate.

See [MCP document sources](../architecture/mcp-document-sources.md) for operator
approval, storage authority, rolling upgrades and qualification boundaries. Synthetic
protocol tests are not qualification of an external service.
