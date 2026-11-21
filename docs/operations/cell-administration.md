# Administer every cell

`loom cell each` applies one supported administration command to every registered cell
and records confirmed outcomes in a private local file. Run it from the operator's
machine with access to the router and each cell's public HTTPS origin. It needs no
local Postgres or ClickHouse connection, server profile, or root signing secret.

Every cell must be serving and enforced. A cell closed to placements remains a target.
An attaching cell, unsupported server, changed deployment, missing key or incomplete
automation registration refuses the run.

## Prepare credentials

Each cell has two distinct key-proof platform-admin bots: one for automation and one
for recovery. Their names and public key thumbprints are registered in the directory.
Keep their private keys separate. The automation bot accepts exactly its approved
public key and has no alternate workload trust. This restriction lets the cell match a
short-lived access token's bot to the one approved key without inventing an assertion
key claim in the token.

Register both purposes only after each bot has its own approved key and platform-admin
grant. On the configured cell operator profile, run `loom cell automation --cell CELL
--automation-bot NAME --automation-thumbprint THUMBPRINT --automation-key PRIVATE_FILE
--recovery-bot NAME --recovery-thumbprint THUMBPRINT --recovery-key PRIVATE_FILE`.
The command holds the cell's control lock, claims its current directory registration,
and proves both keys through the cell's public authentication and bot-registration
endpoints before writing the public pair. Neither private key is sent to the router.

Create a private credential manifest with these fields:

| Field               | Value                                                                         |
| ------------------- | ----------------------------------------------------------------------------- |
| `version`           | `1`                                                                           |
| `router_token_file` | Absolute path to the private router operator-token file                       |
| `keys`              | One entry per target, with `cell`, `deployment_id` and an absolute `key_file` |
| `ca_file`           | Optional absolute path to an explicit CA certificate bundle                   |

The manifest, token and key files must be regular files owned by the current user,
with private permissions and private containing directories. Symlinks and unsafe
writable ancestors refuse. The command does not change permissions or use ambient
server credentials. It reads each automation key at use, derives its public thumbprint,
and refuses a mismatch before signing. It never tries a recovery key.

Use the router's exact HTTPS origin for `--router`. Redirects and ambient HTTP proxies
are disabled. The operator token goes only to the router; assertions and cell tokens
go only to the cell's exact public origin. Normal CA and hostname verification applies.
For private PKI, configure the cell server's operating-system CA trust for its router
connection. The server uses the normal TLS trust store, including `SSL_CERT_FILE`
when the operator supplies it, while ignoring ambient HTTP proxies. The manifest's
`ca_file` controls the command's router and cell connections; it does not change the
server's trust store. Neither side disables certificate or hostname verification.

## Run a supported command

Set `ROUTER`, `CREDENTIALS` and `STATE` to the trusted router origin, private manifest
path and a fresh private outcome-file path, then run:

```bash
loom cell each --router "$ROUTER" --credentials "$CREDENTIALS" --state "$STATE" -- settings list
```

The supported command families are closed:

| Family         | Actions                                                 |
| -------------- | ------------------------------------------------------- |
| `settings`     | `list`, `set`, `reset`, `history` for live settings     |
| `signing-key`  | `list`, `rotate`, `retire`                              |
| `revoke-token` | One exact global subject                                |
| `ratelimit`    | Namespace-name limit, shared across orgs with that name |
| `budget`       | Operation budget or consumer multiplier                 |
| `blacklist`    | Global `add`, `list`                                    |

Tenant policy, org-limit sweeps, arbitrary commands and native store operations are
not supported. Use `loom cell each --help` for coordinator options. A server lacking
the capability contract or requested action refuses before business work; the command
does not fall back to an older API.
Global blacklist removal is unavailable. A parsed removal command receives no
advertised capability.

The command runs one cell at a time. It reads the directory before the run, again for
each target, and once after all attempts. Exit zero requires every target to have a
confirmed completion and the final identities to match. Directory drift discovered
at the end fails the run while retaining the completed history.

## Resume confirmed failures

Use the existing state file without supplying another command:

```bash
loom cell each --router "$ROUTER" --credentials "$CREDENTIALS" --resume "$STATE"
```

Completed cells are not repeated. Confirmed pre-business refusals can be attempted
again with fresh credentials. The original command and router binding cannot change.
New cells require complete key coverage and are appended only while existing target
identities remain unchanged. Removed or replaced targets refuse continuation.

`dispatching` and `UNSETTLED` indicate that a mutation may have reached a cell without
a confirmed response. Resume stops before credential or network access. Preserve the
state and inspect that cell's audit and business state using the separate recovery
procedure; do not edit away uncertainty or automatically replay the command. Action
correlation is not idempotency, and an HTTP error alone does not prove no action ran.

Keep outcome files private. They contain public target identities, command arguments,
completion hashes and result digests, with no access token or private key. Command
arguments can still contain sensitive setting values or blacklist text. Preserve the
file and its sibling lock location for the lifetime of the run.
