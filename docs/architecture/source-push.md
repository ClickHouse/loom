# Retained document-source push state

Document-source push registrations are retained maintenance data. Native push
setup, callbacks, delivery handling and renewal execution are removed. Retained
rows do not request sync or activate an MCP source. MCP selected refresh uses the
bounded source worker described in [MCP document sources](mcp-document-sources.md).

## Lifecycle and storage

The push registry remains separate from source configuration and sync cursors.
Existing configuration, credentials, push rows and history are retained without
upgrade deletion or automatic migration. Unsupported native document sources are
unavailable, and their credentials are never reused for MCP enrollment.

Explicit local source retirement may retire its retained push binding under the
existing source lifecycle. Retirement writes a retired registry version and
clears that current binding's sealed secret; it does not claim upstream channel
revocation or delete all historical rows. The account owner must revoke remote
credentials or registrations at their issuer when required. No callback, native
stop request or provider OAuth fallback runs here.

Offline secret resealing preserves retained registrations, including rows without
an executable source. It rotates only the purpose-bound sealed secret and keeps
the existing identity, status and configuration. Unreadable secrets are reported
for operator resolution rather than discarded as cleanup. Resealing does not
activate a source or perform provider work.

The registry keeps its identity tuple, server-stamped actor and provenance.
Writes use the existing fenced writer lease; resealing also holds the namespace's
source lease and refuses active erasure. Namespace erasure removes stored registry
versions through the existing settled erasure machinery. Source retirement,
namespace erasure and secret resealing remain distinct authorized actions.

## Source entry points

- `server/knowledge/app/source_push.py` (`retire`, `reseal_registry`) performs
  explicit local retirement and offline secret rotation.
- `server/knowledge/store/source_push.py` persists retained registry versions and
  supplies bounded sealed-row scans.
- `server/knowledge/app/sources.py` coordinates source lifecycle under its lease.
- `server/erasure/store/steps_ledgers.py` (`delete_source_push_by_namespace`)
  participates in namespace erasure.

## Invariants

Retained state is not an executable notification registration. Upgrade and
resealing delete no history and activate no work. Only an explicit authorized
lifecycle action retires a local binding, and only writer-requested erasure
removes its stored history. Local retirement makes no remote revocation promise.

Protected copies continue to require actual current source authority. Existing
ordinary published copies retain their namespace access and erasure rules;
unsupported protected mirrors stay unavailable. See
[document-source erasure](document-sources.md#erasure-and-audit) and
[operator cutover](mcp-document-sources.md#rolling-upgrades-and-operator-cutover).
