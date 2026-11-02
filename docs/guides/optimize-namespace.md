# Optimize a namespace from its audit

A writer that uses `memory.set` as an object store (JSON blobs under hash keys, leases as
memories, documents as claims) leaves every table consistent and recall useless. This
procedure reworks such a namespace into the layout in the
[investigation agent guide](investigation-agent-memory.md). Every move leaves a redirect, so
a running writer that still reads the old keys keeps getting answers.

The `loom-optimize` skill in the [Claude Code](../features/integrations/claude-code.md) and
[Codex](../features/integrations/codex.md) plugins runs the same steps.

## The two operations

**`namespace.audit`** (`POST /v1/namespace.audit`, MCP `loom_namespace_audit`, CLI
`loom audit-namespace`). Read-only.

| Input              | Default         |
| ------------------ | --------------- |
| `org`, `namespace` | required        |
| `window_seconds`   | 604800 (7 days) |
| `sample_limit`     | 5               |

It returns `summary` (`live_rows`, `live_bytes`, `excerpt_bytes`, `version_rows`,
`version_bytes`, `unembedded_rows`, `gets`, `searches`, `sets`, `get_search_ratio`, and
`inspector_calls`: the dashboard's and the assistant toolset's gets and searches, left
out of the counts; a write from the dashboard is counted in `sets`),
`findings[]` (`code`, `severity`, `count`, `share`, `bytes`, `samples[]`, `detail`,
`remedy`), `skipped` (rules that could not run: checks, summary reads, the evidence
read; the rest still return), `coverage`, `healthy` ("no critical finding"), and the
`criticals` / `warnings` / `infos` counts. Samples carry keys, row ids, identity columns
and counts, never a value. Each finding also carries the [evidence
fields](../features/namespaces-and-sharing.md#reading-a-namespaces-health); a plan reads
`kind` (a usage heuristic is not proof of misuse), `samples_complete` and `evidence[]`,
the live `version` and `content_sha256` of each sampled row.

**`memory.move`** (`POST /v1/memory.move`, MCP `loom_memory_move`).

| Input                                                                        | Default                     |
| ---------------------------------------------------------------------------- | --------------------------- |
| `org`, `namespace`, `agent`, `from_key`, `to_key`                            | required                    |
| `value`, `memory_type`, `tags`, `entities`, `valid_at`, `source_resource_id` | carried from the source row |
| `keep_redirect`                                                              | `true`                      |
| `if_match_sha256`                                                            | none                        |
| `dry_run`                                                                    | `false`                     |

It returns `{from_key, to_key, redirected, memory_id, version, content_sha256, advisories,
dry_run}`; a dry run adds `memory_type` and `value_preview`. After a move:

- `memory.get(from_key)` still answers with the old value plus `canonical_key`;
- `memory.get(from_key, follow=true)` serves the new row;
- `memory.search` and `memory.list` show only the new row.

## 1. Audit

```bash
loom audit-namespace --org <org> --namespace <ns> --window-days 7 --samples 5
```

The CLI runs on the server (it reads ClickHouse directly). It prints a table (code, severity,
kind, count, share, first sample), or the output model with `--json`, and exits 1 when the
namespace is not healthy. From anywhere else, call `namespace.audit` over HTTP or MCP.

Read `summary` first:

- a `get_search_ratio` in the hundreds means the namespace is used as a key-value store;
- `unembedded_rows` above zero means rows search can never find.

Then read the findings. Stop when `healthy` is true and nothing is `warning` or
`critical`: an `info` finding is a note for the writer, not a rework. A `system_state` finding (an indexing
backlog, missing telemetry) is about the deployment, never the writer; while
`telemetry_unavailable` is a warning, the ledger checks are in `skipped`, not clean.

The dashboard's namespace audit panel shows the same audit. `context.pack` carries a
`namespace_health` block when the namespace is unhealthy (one critical finding, or two
warnings among `exact_key_store`, `double_encoded_values`, `unembedded_rows` and
`coordination_as_memory`), so an agent sees it at session start.

## 2. Plan

Write one section per finding. Only codes the audit returned get a section.

| Finding                                                                 | What changes                                                             | How                                                                                                                                                    |
| ----------------------------------------------------------------------- | ------------------------------------------------------------------------ | ------------------------------------------------------------------------------------------------------------------------------------------------------ |
| `double_encoded_values`, `json_prefix_excerpts`, `hash_keys`            | the row moves to a readable key with a claim-first value                 | decode once; claim sentence, source line, the rest as short lines; `memory.move` with `value`, `tags`, `entities`, `valid_at`, `if_match_sha256`       |
| `oversized_values`, `episodic_blobs`                                    | the body becomes a document or a blob; a one-paragraph row keeps the key | `knowledge.upsert` (Markdown, `source_uri=loom://<org>/<ns>/<key>`) or `context.offload`, then `memory.move` the old key with the paragraph as `value` |
| `unembedded_rows`                                                       | the value is split or shortened so it embeds                             | as `oversized_values`; `namespace.validate` lists the same rows under `unembedded`                                                                     |
| `coordination_as_memory`                                                | the block goes to working memory; the old key keeps answering            | `working_memory.set`, then `memory.move` with `keep_redirect=true`                                                                                     |
| `hand_rolled_index_rows`                                                | the rows stay readable and gain `tags`; the writer stops producing them  | tags only (`memory.update_by_id`); no move                                                                                                             |
| `exact_key_store`, `version_churn`                                      | the writer changes                                                       | list the keys for the owner                                                                                                                            |
| `telemetry_unavailable`, `index_backlog`, `intentional_exact_key_reads` | nothing: the deployment's state, or a workflow                           | no rework; for `telemetry_unavailable` rerun once the ledger covers the window, for a stalled `index_backlog` check the embedding worker               |

The mentor (`mentor.brief`) names three more shapes that are data changes. They are
reworked under the same rules, with the hint's `evidence` as the count:

| Mentor hint              | What changes                                                                                    | How                                                                                                 |
| ------------------------ | ----------------------------------------------------------------------------------------------- | --------------------------------------------------------------------------------------------------- |
| `documents_as_memories`  | as `oversized_values`                                                                           | `knowledge.upsert`, then `memory.move` with the paragraph as `value`                                |
| `checkpoints_as_memory`  | a run dump becomes a `context.offload` blob; one short episodic row keyed by run id replaces it | `context.offload`, then `memory.move` with a one-line ledger `value`, `memory_type=episodic`        |
| `records_for_identities` | rows keyed `<prefix>-<id>` with scalar fields become one schema and one record per row          | `memory_schema.register` once, `memory.record.write` per row, then `memory.move` onto a pointer row |

Each section states: the count and bytes, three example keys before → after (from the
finding's `samples`), the exact calls with their arguments, and what it will **not** do:

- no `memory.delete`;
- no in-place value change: a rewritten value only lands under a new key;
- no row written in the last hour: the writer may be mid-flight.

Store the plan as a knowledge document at `loom://<org>/<ns>/_optimize/plan-<YYYYMMDD>`
(`knowledge.upsert` with that `source_uri`), so the owner's approval and the before/after
numbers live in one place.

## 3. Get approval

The namespace owner approves in writing. A plan shown is not a plan approved. A partial
approval applies the named sections only. If the owner's writer is running (the audit's
`sets` in the window says so), tell them; the last hour of rows is out of scope either way.

## 4. Apply

Back up first if the namespace matters: `memory.list … fields=["value"]` to a JSONL file,
`namespace.snapshot` (HTTP or SDK; it is not served over MCP), or on the server
`CREATE TABLE … AS loom.memory` plus `INSERT SELECT`
([backup and recovery](../operations/backup-and-recovery.md)).

1. Run the first three moves of each section with `dry_run=true`; read back `to_key`,
   `redirected` and `advisories`.
2. Then move in batches of at most 50, in `created_at` order, so revisions land as versions
   of the new key.
3. Pass `if_match_sha256` from the audit sample or a fresh `memory.get`. A 409 means the
   row changed under you: pause the section, re-read and ask.
4. A reply without `memory_id`, `version` and `content_sha256` is a move that did not
   happen. Do not count it.

```bash
curl -s "$LOOM_BASE_URL/v1/memory.move" -H "Authorization: Bearer $LOOM_API_KEY" \
  -H 'Content-Type: application/json' -d '{
    "org": "acme", "namespace": "agent-sre", "agent": "sre-bot",
    "from_key": "3f9a0c1e5b7d2a4c6e8f0a1b3c5d7e9f",
    "to_key": "sre/case/c-42/evidence/e-7",
    "value": "Disk on ingest-3 filled at 02:10 UTC.\nSource: alert A-981.",
    "tags": ["case:c-42", "kind:evidence"],
    "dry_run": true}'
```

## 5. Measure

Run the audit again over the same window and append a before/after table to the plan
document (`knowledge.upsert` on the same `source_uri`). Give the owner the
[writer rules](investigation-agent-memory.md#writer-rules).

## 6. Retire nothing

Old keys stay readable through the redirect for as long as the owner wants. Once the writer
uses the new keys, the owner may tombstone the old ones with `memory.delete` (soft:
`memory.history` keeps them). This procedure never does it.

> **Warning:** retiring keys a live writer still reads splits its view: its `memory.get`
> answers `found: false`, a client may fall back to local state, and two stores then
> disagree. The redirect exists to prevent that.

## Related

- [Investigation agent memory](investigation-agent-memory.md): the target layout
- [Agent integration](agent-integration.md#mistakes-to-avoid)
- [History and provenance](../features/memory/history-and-provenance.md)
