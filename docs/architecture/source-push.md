# Document source push notifications

Needs `LOOM_KNOWLEDGE_SOURCE_PUSH_ENABLED=true` to request document source syncs
from provider notifications. Notifications never write document content or change
the source's access policy. The normal 15-minute poll remains enabled and is the
recovery path for missed deliveries. Disabling push pauses setup, deliveries and
renewal without preventing secret resealing or setup retirement.

## Source entry points

- `server/knowledge/api/ops_source_push.py` (`OPERATIONS`) declares admin setup,
  confirmation and status operations.
- `server/knowledge/app/source_push_routes.py` (`notification`) bounds callback
  bodies and dispatches provider deliveries.
- `server/knowledge/app/source_push.py` (`receive`, `_fresh`, `_put`, `renew_drive`,
  `reseal_registry`) authenticates hints and coordinates registry lifecycle writes.
- `server/knowledge/domain/source_push.py` (`fingerprint`, `verify_drive`,
  `verify_notion`, `verify_linear`) binds setup and verifies provider proof;
  `server/knowledge/store/source_push.py` (`get`, `put`) persists registry versions.
- `server/knowledge/app/sources.py` (`sources_lease`, `deactivate_namespace_sources`)
  coordinates source lifecycle changes;
  `server/erasure/store/steps_ledgers.py` (`delete_source_push_by_namespace`) settles
  registry erasure.

## Invariants

- Authenticated deliveries request sync through the existing source path. Provider
  payloads cannot write content or widen source access.
- A delivery must match the current source ID, setup generation and credential,
  item and configuration fingerprint. The source lease and sequential reads fence
  source replacement, retirement and late provider responses.
- Push secrets use purpose-bound sealing. Only an admin can reveal a pending
  challenge; ordinary reads, audit subjects and idempotency replay omit it.
- Registry writes and namespace erasure share a fenced writer lease. Erasure
  removes every stored version, and setup publication requires verified fleet
  support for that erasure step.

## Registration and authentication

The push registry is separate from the source registry and sync cursors. A row
binds an org, namespace, source ID, provider and random setup generation to a
fingerprint of the source's credential and items. Configuration is an admin
operation. An explicitly configured HTTPS public base URL is required.

The callback URL puts the namespace, source ID and setup generation in the path
and the exact organization in the required `org` query parameter. Query encoding
preserves organization names that contain slashes or reserved URL characters.
A callback re-reads the registry and current source under the namespace's source lease
before recording a sync request. A removed or replaced source, changed
credential, changed item set, inactive namespace or retired generation refuses
the delivery. Callback payloads and headers are not stored or logged.

- Drive channels use independent random channel IDs and sealed channel tokens.
  A delivery must match the active channel ID, token and resource ID before its
  expiration. This is channel-token verification: Drive does not sign these
  deliveries. Early `sync` notifications are acknowledged without requesting
  work. Watch responses are activated only after rechecking the setup generation.
- Notion setup captures one bounded, sealed verification challenge for a fresh
  generation. Only an admin can reveal it while setup is pending. After verifying
  the subscription in the provider's UI, the admin confirms that exact generation
  and challenge fingerprint. An unsigned challenge cannot activate a subscription
  or request sync. Active deliveries require the raw-body HMAC and the configured
  workspace identity.
- Linear setup first exposes a pending notification address. The admin creates
  its webhook there, then confirms the same generation with its signing secret.
  Deliveries require the raw-body HMAC, the signed body's millisecond timestamp
  within one minute and the configured organization identity.

Provider identity is read using the source's existing credential during setup.
Notification bodies have a fixed size limit. Duplicate valid notifications
coalesce with the existing pending sync request; a waiting full sync is preserved.
Successful callbacks answer HTTP 200 within a four-second handler budget.

## Drive coverage and renewal

A Drive user change channel does not cover every shared-drive change. Setup
therefore creates channels for the user corpus and every shared drive the account
belongs to, up to 19 drives. A complete, bounded membership listing avoids mistaking
a directly shared file's drive ID for membership: a nonmember's directly shared
items use the user log. Setup refuses an incomplete or over-limit corpus set
rather than claiming complete push coverage. Moving a file
to a new corpus may require setup again; polling still discovers the change.

A bounded, default-off worker renews channels before expiration. It creates a
new unique channel, validates the response and source generation, then swaps it
under the source lease. It stops the old channel best effort. A failed renewal
leaves polling available and exposes the expired channel in setup status.

The configuration catalog controls the provider-work, per-channel stop, total
cleanup and worker-pass deadlines. Their defaults are 40, two, five and 120
seconds. A pass stops starting renewals with enough time for one provider attempt
and two cleanup windows. Configuration refuses provider work plus cleanup that
could outlive the 48-second work window within the 120-second renewal lease, or a
pass budget that leaves no time to start a renewal. The channel stop deadline
cannot exceed the total cleanup window.

## Lifecycle and storage

The registry is ordered by org, namespace and source ID, so namespace reads
prune their physical key range. Maintenance scans advance in that same order.
The registry stores the full identity tuple, server-stamped actor and derived
provenance. Secrets use purpose-bound sealing and participate in resealing.
Ordinary source reads and audit subjects never include tokens or challenges.
The admin setup read bypasses idempotency replay storage, so a revealed pending
challenge cannot be replayed from an ordinary cache after activation.

Source removal, credential rotation, item updates and namespace retirement
retire push setup under the same source lease. Late external watch responses
cannot revive retired setup. Namespace erasure removes every registry version
through the existing settled erasure machinery. Configuration writes require a
fleet that understands the registry's erasure step; an unreadable fleet refuses.
Before rolling back to a build without this registry, disable push, erase the
registry rows with the current build and verify the namespace erasures complete.
Provider channels that cannot be stopped expire; retired callbacks refuse them.

## Alternatives

Putting push state in the source row would make every delivery contend with
credential writes and risk carrying an old credential forward. Putting it in
sync detail would mix sealed setup secrets with ordinary inspection output.
Both are avoided by the separate registry. Notifications are hints, so a durable
event queue, provider payload archive and a second ingestion path are unnecessary.

## Provider contracts

- [Drive push notifications](https://developers.google.com/workspace/drive/api/guides/push)
- [Drive user and shared-drive logs](https://developers.google.com/workspace/drive/api/guides/about-changes)
- [Notion webhook verification](https://developers.notion.com/reference/webhooks)
- [Notion event delivery](https://developers.notion.com/reference/webhooks-events-delivery)
- [Linear webhook verification](https://linear.app/developers/webhooks)
