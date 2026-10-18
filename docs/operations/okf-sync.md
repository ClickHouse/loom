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
  confidence, `valid_at`, `valid_to`, `source_trace`, and the entities and
  `source_class` when the bundle carries them. Tags `memory.set` adds itself
  (`sensitivity:`, `attribute:`) are not a difference. It skips an edge that is already
  live. When several concepts in the bundle share a key (rows of different writers), the
  most recently updated one is written. Re-sending an identical row would count as a
  reinforcement: a version row, an audit row and a higher ranking boost on every run. A
  concept with `sources:` is the exception: it is written on every run, because no read
  returns the source it links to.
- **One `memory.set` per new or changed concept**, through the conflict, blacklist and
  audit checks of any other write, with the token as its actor. A row the source marked
  `derived`, `imported` or `external` keeps that class. With the same org and namespace
  on both sides the copy carries no `origin:` tags, so any other row is stored as the
  sync token's own statement.
- **One `memory.delete` per pruned row.** `--prune` deletes only rows under the import's
  `--agent`, written by its token, whose `source_trace` starts with `okf:` and whose key is
  no longer in the bundle. Rows anyone else wrote in the namespace stay. Give each bundle
  imported into the namespace its own `--agent` (`--prune` refuses without one). A delete
  is a tombstone: `memory.restore` brings the row back within its 14-day window.

The last line reports the counts:
`500 rows from the bundle into acme/shared-lessons: 0 written, 500 unchanged, 0 failed …,
0 pruned`.
A non-zero exit means a write, link or delete failed.

## Guards

- `--prune` refuses a bundle with no concepts, a bundle exported with `--limit`, and one
  whose concept files do not hash to the `loom.corpus_digest` the export wrote into
  `index.md`. A partial or edited bundle never drives deletes, and neither does one exported
  into a directory that held an older bundle.
- Before each delete it reads the row under its own identity again and deletes only while
  that row is still there under its `--agent` and was written by its own token:
  `memory.delete` falls back to the same key under another agent when the caller's row is
  gone, and the agent name alone is the caller's choice.
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
content must go. A key pruned by
mistake is written again by the next run whose bundle holds it. That also covers the one
race the guards leave: export pages the source by offset, so a source row deleted while a
large export is paging can push one live row out of that bundle, and its copy is pruned
until the next run.

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

Re-mint before the TTL runs out, with the same `--subject`, and replace the Secret. Prune
deletes only rows its token's subject wrote, so a token with another subject leaves the
earlier copies in place (the last line counts them as kept); `loom revoke-token` retires a
leaked one. Prune needs a JWT: with a token that carries no subject (a server with
authentication off) it deletes nothing and counts every candidate as kept.

- On the public Loom, the job's token is the only writer on the namespace. Its readers
  (a bot in public CI) hold `viewer` tokens.
- On the private Loom, the namespace's writers are the people and agents who publish to
  it. Every other principal that reads it holds `viewer`, so nothing reaches the namespace
  that its writers did not mean to publish.

## The copied namespace is published

Everything in the namespace reaches the public Loom within one schedule interval. Treat a
write to it on the private side as publishing: keep anything confidential in another
namespace. Rows marked `sensitivity: high` stay on the private side (the export leaves
them out unless `--include-high`), and a row that becomes `high` is pruned from the
public side on the next run. A prune is a tombstone: the value stays restorable for 14
days and in the public Loom's version history. Content that must leave the public Loom
goes through [erasure](../features/memory/retention-and-erasure.md).

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
