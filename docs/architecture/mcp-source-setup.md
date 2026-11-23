# MCP source setup

MCP source controls register a fixed selection against the signed-in owner's
connection and an ordinary destination namespace or collection. They use the
common operation transport and the existing source registry, state and retirement
machinery. The automatic personal namespace cannot hold these sources.

## Setup and identity

Connect an account, discover its configured read capabilities, choose exact
resource IDs and a destination, then preview the access rule before registration.
Connecting, listing and previewing import no documents. Registration discovers
whether the configured resources or reviewed read mapping are available. An
action-only server cannot pass this check. Discovery does not read the selection
or prove that the owner can read each document.

`knowledge.source.mcp_add` takes `org`, `namespace`, the owner's `slot`, a UUID
`request_id`, ordered `resource_ids`, `mode`, `reader_policy`, an optional `label`
and `confirm_copy_authority`. It accepts no owner, endpoint, tool mapping, schema
or credential. Operator configuration and the authenticated connection supply
those values. The configured selection and UTF-8 byte limits apply before
admission.

The registry name and source UUID derive from the organization, destination,
canonical owner, connection slot and request UUID. Labels are bounded display
metadata. Retrying a lost response returns only the exact active registration
with the same binding, selection, mode, audience and label. Changed arguments,
a stopped connection, a paused or retired source, or a replacement connection
refuse. A replacement binding requires a separate registration and consent;
existing copies, history and checkpoints keep their identity.

## Modes and access

| Mode | Stored content | Scope and refresh |
| --- | --- | --- |
| `live` | No knowledge copy | Only selected IDs; durable import and explicit refresh are unavailable |
| `snapshot` | A selected copy | One import per selection or explicit refresh |
| `selected_refresh` | A selected copy | Periodic attempts for those exact IDs when source sync is enabled |

No mode includes recursion or future matching items. Notifications, incomplete
listings, read refusals and missing resources are not deletion instructions.
Upstream ACLs, complete enumeration and deletion tracking remain unsupported.
A registered state is not an attestation of current upstream access. Responses
report whether global source sync is disabled; accepting a refresh request does
not mean an import has completed.

`owner_only` requires both the source owner and current destination access.
Adding collection members or broadening inherited grants does not publish this
material. `collection_copy` means intentional publication through the
destination's current, inherited and future reader grants. It does not mirror
upstream permissions.

A collection copy requires an approved operator profile, current destination
administrator authority and explicit confirmation of publication authority over
the exact selected material. The handler creates a versioned consent record
binding the authenticated publisher, destination and hash of the ordered exact
IDs. The caller cannot supply this proof. An update of a collection copy requires
fresh confirmation. Consent cannot authorize live sharing, a different audience
or a wider selection. Admission still requires the execution authority helper to
accept the entire proposed row. The current execution authority refuses
collection copies; confirmation alone cannot register one.

## Lifecycle controls

| Operation | Effect on source and retained content |
| --- | --- |
| `knowledge.source.mcp_list` | Lists only the caller's registrations in the requested destination |
| `knowledge.source.mcp_inspect` | Returns only an owned source; a foreign ID answers as missing |
| `knowledge.source.mcp_update` | Replaces the fixed selection, mode, audience and label; retained IDs keep source document identity |
| `knowledge.source.mcp_pause` | Sets contract pause state; retains registration, copies and checkpoints |
| `knowledge.source.mcp_resume` | Revalidates the original connection binding before clearing pause |
| `knowledge.source.mcp_refresh` | Requests an explicit import attempt for a copy mode through existing source state |
| `knowledge.source.mcp_remove` | Retires the source, retaining copies unless `delete_documents=true` explicitly requests asynchronous erasure |

Removing selected IDs does not declare upstream deletion or request erasure of
stored copies. Retirement and pause refuse source reads while retaining content;
retention does not promise that the copies remain readable. Erasure requests use
the existing retirement machinery and report `erasure_requested`, without
claiming deletion has finished. Unsupported contract versions remain opaque and
can be inspected as unsupported or retired without interpreting their payloads.

Disabling self-service blocks registration before any remote discovery. Existing
list, inspect and remove controls remain available. Selection changes use the
existing connection and cannot replace credentials.

## Write boundary

Remote discovery finishes before writer locks are taken. Mutations acquire the
SCIM subject barrier, connection account lease and destination source lease in
that order. Under those locks they recheck the caller, destination grants,
namespace validity, erasure state and exact stored registration. Add also checks
name availability and existing quotas. Immediately before admission, the
registration authority checks the current connection, profile approval, standing,
fence and destination writer authority.

Generic source rows carry `generic_config_json`, `generic_items_json`,
`connection_slot` and a contract version. The ordinary `config` and `items`
projections remain empty. Application codecs apply deployment limits to the
stored opaque columns. Source changes retain the registration UUID and use the
existing store's complete version-row writes; no separate source database or
worker framework is introduced.

Refresh requests merge into existing state without clearing checkpoints. A
selection marker prevents a label-only change from requesting another snapshot.
Mutation audit subjects omit private source labels, resource IDs and counts.

See [document sources](document-sources.md) for the shared document, retirement
and erasure lifecycle.
