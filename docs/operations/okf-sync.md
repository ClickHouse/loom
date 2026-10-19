# Copying a namespace to another Loom

A job inside the private network copies one namespace from a private Loom to a public
one on a schedule: `loom export-okf` reads it from the private Loom, and
`loom import-okf --prune` writes it to the public Loom. The public side never gets a
route or a token into the private Loom. `deploy/okf-sync/cronjob.yaml` is the
Kubernetes example: a CronJob every 15 minutes, its ServiceAccount and a
NetworkPolicy.

```bash
LOOM_API_KEY="$PRIVATE_VIEWER" loom export-okf --server-url "$PRIVATE_URL" \
  --namespace shared-lessons --no-evidence --out /tmp/bundle
LOOM_API_KEY="$PUBLIC_WRITER" loom import-okf /tmp/bundle --server-url "$PUBLIC_URL" \
  --namespace shared-lessons --agent okf-sync --prune
```

## What a run writes

- **Nothing, when nothing changed.** The import reads the live rows under its identity
  first and skips a concept whose row already holds the same value, type, tags,
  confidence, `valid_at`, `valid_to`, `source_trace` and `source_class` (the declared
  class, else `imported`), and the entities when the bundle carries them. Tags
  `memory.set` adds itself (`sensitivity:`, `attribute:`) are not a difference. It skips
  an edge that is already live. When several concepts in the bundle share a key (rows of
  different writers), the most recently updated one is written. Re-sending an identical
  row would count as a reinforcement: a version row, an audit row and a higher ranking
  boost on every run. A concept with `sources:` is the exception: it is written on every
  run, because the reads the import makes do not return the source it links to. So is a
  concept the target merges into another key's row: each run sends it again and reinforces
  that row, and the last line counts it as merged.
- **One `memory.set` per new or changed concept**, through the conflict, blacklist and
  audit checks of any other write, with the token as its actor. A row the source marked
  `derived` or `external` keeps that class; every other row is stored as `imported`,
  which the server derives from the `okf:` trace. With the same org and namespace on both
  sides the copy carries no `origin:` tags.
- **One `memory.delete_version` per pruned row.** `--prune` deletes only rows under the
  import's `--agent`, written by its token, whose `source_trace` starts with `okf:` and
  whose key is no longer in the bundle. Rows anyone else wrote in the namespace stay. Give
  each bundle imported into the namespace its own `--agent` (`--prune` refuses without
  one). A delete is a tombstone: `memory.restore` brings the row back within its 14-day
  window.

The last line reports the counts:
`500 rows from the bundle into acme/shared-lessons: 0 written, 500 unchanged, 0 failed …,
0 pruned`.
Exit 1 means the target could not be read or a write, link, delete, evidence batch or
`--verify` failed; exit 2 means the import refused before any request: a lint finding, a
prune guard, a path that is not a directory, or a missing `--agent`, token or org.

## Guards

- `--prune` refuses a bundle with no concepts, and one whose concept files do not hash to
  the `loom.corpus_digest` the export wrote into `index.md`: an edited bundle, or one
  exported into a directory that held an older bundle, never drives deletes. A bundle
  whose `index.md` does not carry `loom.complete: true` is written but not pruned, and the
  last line says so. Export leaves the marker off when it stopped at `--limit`, ran with
  `--include-tombstoned` (deleted rows stay in such a bundle), or the source changed while
  it was read (a write while it paged, or a row deleted before its full read), and prints
  which; a bundle an older release wrote has no marker.
- The export carries the source's superseded and duplicate rows as `status: deprecated`,
  so a key the source folds into another stays in the copy instead of being pruned.
- Export pages the source by offset, so a write to the source mid-listing can shift the
  pages and skip a live row. After the pages it reads `memory.keys`, every live row's
  identity and key in one statement (the namespace at one moment), and calls the listing
  whole only when each of those rows was listed. A row written after that moment is a
  later change, which the next run carries. It lists up to three times in all, and
  otherwise leaves `loom.complete` off, so that run copies without pruning and a later,
  quieter run prunes. A source too old to answer `memory.keys`, or one with more than
  100,000 live rows, is never marked complete.
- Before each delete it reads the row under its own identity again, and goes on only if
  that row is under its `--agent`, was written by its own token (the agent name alone is
  the caller's choice), is still the version the import's listing saw with an `okf:`
  trace, and has no `memory.lock`: anything else is counted as kept, and a row that is
  gone or under another agent by then is not counted. A copy whose key starts with
  `chunk:` is counted as failed: `memory.get` reads such a key as a search chunk, so prune
  cannot check it; delete it with `memory.delete`. It deletes with
  `memory.delete_version`, which removes only that version, under that exact identity, and
  never another agent's row. A `memory.set` or `memory.bulk_set` of the key that is in
  flight when the delete lands keeps its row; the exceptions are in the [memory
  architecture](../architecture/memory-architecture.md#lifecycle).
- A target replica too old to know `memory.delete_version` answers 404 and deletes
  nothing; the prune counts the failure and the import exits 1. A rolling upgrade of
  either Loom needs no pause.
- The import runs only when the export exited 0 (`sh -e`), and each run writes its bundle
  to a fresh `emptyDir`, so a bundle left by an earlier run is never imported.
- `--dry-run` lists what would be written and deleted, after the same checks, and writes
  nothing.
- `concurrencyPolicy: Forbid` keeps two runs from interleaving.

There is no cap on how many rows one run may prune. A source namespace that loses most of
its rows on purpose propagates on the next run without an operator. One that loses all of
them does not: the import refuses the empty bundle and the job fails on every run.
Suspend the CronJob (`kubectl patch cronjob okf-sync -p '{"spec":{"suspend":true}}'`) and
delete the copies on the public Loom with `memory.delete`, or through erasure when the
content must go.

A key pruned by mistake is written again by the next run whose bundle holds it, as a new
row: its `memory_id` changes. `memory.restore` brings back the original row instead.

## What a run costs

A run that finds nothing to change sends only reads. For a namespace of N rows:

| Side                 | Requests                                                                                                                                                                                                                                                                                                                     |
| -------------------- | ---------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| Export, private Loom | `memory.list` pages of 100 rows (`LOOM_MEMORY_LIST_VIEWER_LIMIT` caps a viewer token's page), one `memory.keys`, one `GET /v1/health` for the build commit, one `memory.bulk_get` per 500 rows of each (agent, user, session), and one `memory.export_evidence` page per 2,000 typed edges                                   |
| Import, public Loom  | `memory.list` pages of 1,000 rows, one `memory.bulk_get` per 500 rows, and the edge pages again; then one `memory.set` per changed row, one `memory.link` per new edge, a `memory.get` per prune candidate (a row with an `okf:` trace whose key left the bundle, kept or not), and a `memory.delete_version` per pruned row |

A 501-row namespace with one writer takes 15 requests in all (6 `memory.list` calls, 1
`memory.keys`, 1 health read, 2 `memory.bulk_get` and 1 edge page on the export side; 1, 2
and 1 on the import side). At 50,000 rows under one identity a run is about 750 requests,
fewer than one a second over a 15-minute schedule; a namespace spread over many sessions,
as hooks write it, takes one `memory.bulk_get` per session instead, and the export lists
the source up to three times in all when it changed while it was paged.

## Tokens

The job holds two tokens, each scoped to the one namespace:

```bash
# On the private Loom: read the namespace, nothing else.
loom mint-token --subject okf-sync --org <org> --namespace-role shared-lessons=viewer \
  --ttl-days 90 --save ./private-viewer.jwt
# On the public Loom: write the namespace, nothing else.
loom mint-token --subject okf-sync --org <org> --namespace-role shared-lessons=writer \
  --ttl-days 90 --save ./public-writer.jwt
```

Store them in the Secret the CronJob reads:

```bash
kubectl create secret generic okf-sync \
  --from-file=private-viewer=./private-viewer.jwt \
  --from-file=public-writer=./public-writer.jwt
```

Re-mint before the TTL runs out, with the same `--subject` and kind (a service account,
the default), and replace the Secret. Prune deletes only rows its token's actor wrote
(`process:<subject>` for a service account, `human:<subject>` for a user token), so a
token with another subject or kind leaves the earlier copies in place (the last line
counts them as kept); `loom revoke-token` retires a leaked one. On a server with
authentication off no row carries an actor, so prune deletes nothing and counts every
candidate as kept.

- On the public Loom, the job's token is the only writer on the namespace. Its readers
  (a bot in public CI) hold `viewer` tokens.
- On the private Loom, the namespace's writers are the people and agents who publish to
  it. Every other principal that reads it holds `viewer`, so nothing reaches the namespace
  that its writers did not mean to publish.

## The copied namespace is published

Everything in the namespace reaches the public Loom within one schedule interval. Treat a
write to it on the private side as publishing: keep anything confidential in another
namespace. Rows marked `sensitivity: high` stay on the private side (the export leaves
them out unless `--include-high`), and a row that becomes `high` is pruned from the public
side on the next run, unless it was the last row the export would carry: the bundle then
has no concepts, the import refuses it, and the public copies stay. Delete them on the
public Loom with `memory.delete`, or through erasure when the content must go. The export
knows a `high` row by its `sensitivity`, which it reads through `memory.bulk_get`, or by
the `sensitivity:high` tag `memory.set` and `memory.update` add. Against a private Loom
whose `memory.bulk_get` does not return `sensitivity`, every row stays home (the export
counts them under "no known sensitivity"), so the bundle has no concepts and `--prune`
refuses it. A prune is a tombstone: the value stays restorable for 14 days and in the
public Loom's version history. Content that must leave the public Loom goes through
[erasure](../features/memory/retention-and-erasure.md).

## Network

The NetworkPolicy allows no ingress. Egress is limited to DNS, the private Loom's web pods
on port 7777, and the public Loom's addresses on 443. A NetworkPolicy matches addresses,
not host names: list the public Loom's addresses in the `ipBlock`, or use your CNI's
host-name policy or an egress proxy when they change.

When the private release sets `networkPolicy.enabled` with `ingressFrom`, add the job's
pods (`app.kubernetes.io/name: okf-sync`) or their namespace to `ingressFrom`. Otherwise
the release's own policy refuses the export.

## Related

- [OKF bundles](../features/integrations/okf.md): the bundle format and the import.
- [Kubernetes](kubernetes.md): the chart whose pod security the CronJob copies.
