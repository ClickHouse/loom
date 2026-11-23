# Manage MCP sources from the CLI

The Python SDK supplies source controls through `python -m loom.source_cli source`.
The server CLI exposes the same controls under `loom knowledge source` when the
matching-release `clickhouse-loom` SDK is installed alongside it. A server-only
installation can show command help but refuses execution with a dependency message.

## Application-owned session authority

Every command requires `--session-file`, an absolute path to an existing private
application-owned session pointer. The CLI reopens the maintained SDK's
`SessionProtection` and sends requests through its `Transport`. It does not create,
reset or repair session families. Missing pointers or proof refuse before dispatch.

The trusted application provisions the pointer once, before a session receives
private output:

```python
from pathlib import Path

from loom.cli_context import session_environment
from loom.session_protection import SessionProtection
from loom.source_cli import save_session_pointer

application = Path("application-state").absolute()
application.mkdir(mode=0o700)
family = SessionProtection.create(base=application / "families")
save_session_pointer(application / "session.json", family)
session_env = session_environment(str(application / "session.json"))
```

`save_session_pointer` creates an owned mode-0600 file exclusively and syncs it to
disk. Its directory must already be owned and mode 0700. Keep both the pointer and
its SDK authority. Replacing either with a clean family or restoring older state
breaks the application contract. Source responses must never select these paths.

The trusted launcher merges `session_env` into every contributing command's inherited
environment and persists it unchanged for resume. Its `LOOM_SOURCE_SESSION_CONTEXT`
capsule names the pointer file and pins the expected SDK pointer. It contains no
credentials and grants no additional server authority. Replacing the file with a
different valid family refuses. The application must retain this same capsule across
namespaces, servers, credentials, subprocesses and automatic tasks. Source output
cannot select or replace it.

`--intent automatic` is the default and requires that inherited capsule to match
`--session-file`. It enters the SDK's automatic guard for writes and operations
without generated read-only metadata, including grants and recording writes. After
private output protects the family, those automatic commands refuse; known read-only
operations remain available. A direct user action
may pass `--intent explicit`; its response is still observed before output. Explicit
intent must come from the application's user interaction, never from source content
or an automatic callback.

The server CLI's common JSON/file helpers, shell search/transcript ingestion, Recall
HTTP imports, OKF requests and delegated SDK `call` requests restore the same inherited
context. Configured context without the matching installed SDK refuses. Unbound
ordinary commands retain their normal behavior. For a direct human write, the root
CLI accepts `loom --request-intent explicit ...`; SDK `call` also accepts that flag
after `call`. Intent ends with that dispatch and is never stored on a client. Later
automatic commands still use the protected capsule. Explicit source invocation may
use its own `--session-file` without an inherited capsule, but does not establish a
qualified automatic application session by itself.

Direct database administration and nonparticipating external clients are outside
this HTTP client qualification. An application must retain every contributing
context and use participating request paths before claiming protection. See the SDK's
[session protection contract](../../sdk/python/SESSION_PROTECTION.md).
The [execution design](../architecture/source-cli.md) describes the distribution boundary.

## Request and credential inputs

Every operation accepts `--server-url`, `--org`, `--session-file`,
`--request-file` and `--token-file`. Request files contain one UTF-8 JSON object of
at most 1 MiB. Duplicate keys and unknown fields refuse without echoing their
values. `org` comes from `--org`. Where a namespace is accepted, `--namespace` may
supply it; a conflicting JSON `namespace` refuses.

Credentials never travel in argv. `--token-file` reads an owned mode-0600 regular
file; otherwise the command uses `LOOM_API_KEY`. It does not borrow a saved human
token. Connection setup accepts `--bearer-file`; recording setup requires
`--credential-file`. Each secret is at most 64 KiB. A file argument of `-` reads
stdin, and only one input may consume stdin. Secret files and session pointers
cannot be symlinks or have multiple hard links.

The examples below use application-supplied `LOOM_URL`, `LOOM_ORG`,
`APP_SESSION_FILE` and `APP_TOKEN_FILE`. For a direct user action:

```bash
python -m loom.source_cli source connection profiles \
  --server-url "$LOOM_URL" --org "$LOOM_ORG" \
  --session-file "$APP_SESSION_FILE" --token-file "$APP_TOKEN_FILE" \
  --intent explicit
```

All commands return JSON. Paginated lists return their server continuation; pass
that exact `next_cursor` as `cursor` in another request with the same filters.
Browse returns only its declared first window. Inspect `complete`, `truncated`,
`limitations` and manual resource-ID support; a browse response is not proof of
complete enumeration or upstream permissions.

## Connections and selected sources

The request fields below exclude `org` and the separate secret input. Required
fields and bounds come from the [operations reference](../reference/operations.md).

| Command after `source` | Operation | JSON request fields |
| --- | --- | --- |
| `connection profiles` | `knowledge.source.mcp_profiles` | `query`, `cursor`, `limit` |
| `connection connect` | `knowledge.connection.mcp_connect` | `profile_id`, `request_id`, `label` |
| `connection list` | `knowledge.connection.mcp_list` | `cursor`, `limit` |
| `connection inspect` | `knowledge.connection.mcp_inspect` | `slot`, `request_id` |
| `connection discover` | `knowledge.connection.mcp_discover` | `slot` |
| `connection browse` | `knowledge.connection.mcp_browse` | `slot`, `mode`, `query`, `limit` |
| `connection disconnect` | `knowledge.connection.mcp_disconnect` | `slot` |
| `preview`, `destination preview` | `knowledge.source.mcp_preview` | `namespace`, `slot`, `mode`, `reader_policy`, `resource_ids` |
| `add` | `knowledge.source.mcp_add` | Preview fields plus `request_id`, `label`, `confirm_copy_authority` |
| `update` | `knowledge.source.mcp_update` | `namespace`, `name`, `if_scope_revision`, `resource_ids`, `mode`, `label`, `confirm_copy_authority` |
| `list` | `knowledge.source.mcp_list` | `namespace`, `cursor`, `limit`, `include_retired` |
| `inspect` | `knowledge.source.mcp_inspect` | `namespace`, `name`, `slot`, `request_id` |
| `pause`, `resume`, `refresh` | `knowledge.source.mcp_pause`, `knowledge.source.mcp_resume`, `knowledge.source.mcp_refresh` | `namespace`, `name` |
| `read` | `knowledge.source.mcp_read` | `namespace`, `name`, `resource_id` |
| `remove` | `knowledge.source.mcp_remove` | `namespace`, `name`, `confirm`, `delete_documents` |

Select an approved profile, connect with a client-generated canonical UUID, and
keep that UUID. Discover capabilities before browsing. Browse resources with
`mode: "resources"`, or an approved mapped search with `mode: "search"` and `query`.
An approved endpoint and supported capability do not verify remote identity.

Preview an exact selection before adding it. A preview request file contains:

```json
{
  "namespace": "col-research",
  "slot": "20000000-0000-4000-8000-000000000002",
  "mode": "snapshot",
  "reader_policy": "owner_only",
  "resource_ids": ["resource:one", "resource:two"]
}
```

Run `source preview` with `--request-file selection.json`. Its access rule explains
stored copies, collection grants, freshness and publication confirmation. Preview
grants no authority. Source modes are `live`, `snapshot` and `selected_refresh`.
Reader policies are `owner_only` and `collection_copy`. A collection copy requires
the server's operator approval, destination grants and explicit publication consent.
Future items are not included, and upstream ACL or deletion tracking is not implied.

For `source add`, include `request_id` in the same file. Set
`confirm_copy_authority` only when authorized to publish that exact selection.
For `source update`, `resource_ids` is the complete replacement selection, and
`if_scope_revision` can reject concurrent changes. An audience change requires a
separate source registration and its access checks.

`pause` and `connection disconnect` stop reads while retaining copies. Disconnect
does not revoke the upstream credential; follow the response's upstream action.
`remove` requires `confirm` to repeat the opaque source name. Its
`delete_documents` field explicitly requests asynchronous copy erasure; otherwise
copies remain. No command infers erasure from a failed read or missing resource.

## Ordinary collection destinations

| Command after `source destination` | Operation | JSON request fields |
| --- | --- | --- |
| `list` | `collection.list` | `limit`, `include_document_counts`, `cursor` |
| `create` | `collection.create` | `namespace`, `title`, `derive_name`, `parent`, `description` |
| `grants` | `grants.list` | `namespace`, `limit`, `cursor`, `fields` |
| `grant` | `grants.create` | `namespace`, `subject`, `role`, `granted_by`, `replace` |
| `preview` | `knowledge.source.mcp_preview` | The exact preview fields above |

List collections and choose the returned namespace with the required destination
role. Create an ordinary top-level collection for a private destination, or set
`parent` to a team collection whose grants it should inherit. Explicit grants may
share a top-level collection. No source command creates a personal namespace or
replaces grants automatically.

For example, `destination create` accepts
`{"namespace":"col-research","title":"Research"}`. Adding
`"parent":"col-team"` requests the inherited team destination. Inspect grants
and run the source access preview for the selected namespace and reader policy.
A grant listing is not a complete upstream reader list, and an `owner_only` source
still requires destination access. The server decides each grant and publication
request independently.

## Uncertain outcomes and reconciliation

The CLI disables transport retries and rate-limit replay for every command. It
never regenerates a request UUID or follows a failed save with another save.

After an uncertain connect, run `connection inspect` with
`{"request_id":"10000000-0000-4000-8000-000000000001"}`. Preserve the original
UUID; inspect reports `absent`, `present` or `incomplete` reconciliation. An
incomplete result does not authorize another registration.

After an uncertain source add, run `inspect` with the original `namespace`, `slot`
and `request_id`. A confirmed saved source can then be inspected by its opaque
`name`. A failed inspection is not evidence that the save was absent. Keep the
original request file and session proof after cancellation or interruption.

## Recordings and retained sources

`recording add` calls `knowledge.source.add` with `namespace`, `name`,
`kind: "recording"`, `config` and optional `dry_run`, plus the separate
`--credential-file`. `config` contains the recording provider and explicitly
selected recording IDs. `recording list` calls `knowledge.source.list` with
`namespace`; `recording sync-now` accepts `namespace`, `name` and optional `full`.

`retained list`, `retained inspect` and `retained remove` expose ordinary source
listing, inspection and retirement. Inspection takes `namespace` and `name`;
removal also requires `confirm` and accepts `delete_documents`. Retained native
document sources remain unavailable for execution. Document setup and updates use
the MCP controls above.
