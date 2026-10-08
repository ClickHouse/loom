# Import ClickGap memory

`loom import-clickgap` loads one SQLite export shape, a ClickGap `lessons` store, into a Loom
namespace; skip this page unless you have such an export. A lesson is a short, deduplicated
review memory with typed entities, categories, a kind, a per-row TTL and a `recall_count`.

Two commands do the work:

- `loom import-clickgap`: the importer (`server/cli/import_clickgap.py`), run where the `loom`
  CLI reaches ClickHouse (the server container or a clone with the server's `.env`).
- `python -m bench.clickgap_migration_validate`: the validator
  (`bench/clickgap_migration_validate.py`).

## What the importer preserves

| ClickGap                    | Loom                                                                    | Note                                                                                                                                                                                                                                                                            |
| --------------------------- | ----------------------------------------------------------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `lessons.text`              | `loom.memory.value` + embedded `content_excerpt`                        | the assertion is embedded; the reasoning is not                                                                                                                                                                                                                                 |
| `lessons.reasoning`         | `value_json.reasoning` (with `.text` = assertion)                       | `get` returns `value_json`, so the assertion travels with the reasoning, never concatenated into it                                                                                                                                                                             |
| `lessons.kind`              | `memory_type` (mostly 1:1) **+** tag `kind:<kind>`                      | `summary → episodic` (non-searchable): summaries stay reachable through `memory.get` / `memory.list` and the `kind:summary` tag but don't rank in `memory.search`. The `kind:<kind>` tag preserves the original taxonomy. See `_clickgap_memory_type` / `_CLICKGAP_TYPE_REMAP`. |
| `lessons.source`            | `source_trace='clickgap://<class>/<source>'` + tag `source:<class>`     | classes: pipeline / session / human / reviewer / eval                                                                                                                                                                                                                           |
| `lessons.created_at`        | `created_at`, `valid_at`, `temporal_anchor`                             | the original recording time, not the import time                                                                                                                                                                                                                                |
| `lessons.expires_at`        | `expires_at` **verbatim, per row**                                      | never recomputed from a global TTL                                                                                                                                                                                                                                              |
| `lessons.recall_count`      | `recall_count` **and** `memory_embeddings.reinforcement_count`          | see [recall signal](#recall-signal)                                                                                                                                                                                                                                             |
| `lessons.last_recalled_at`  | `last_recalled_at` **and** `last_reinforced_at`                         | the recall-recency axis                                                                                                                                                                                                                                                         |
| `lessons.evidence_count`    | `evidence_count`                                                        |                                                                                                                                                                                                                                                                                 |
| `lesson_entities`           | `entities` (bare) + `entity:<kind>:<text>` tags + `loom.entity_vectors` | the bare `entities` array drops the kind; the tag carries it                                                                                                                                                                                                                    |
| `lesson_categories` + items | `loom.categories` + `loom.memory_categories`                            | names case-folded before the deterministic `category_id`                                                                                                                                                                                                                        |
| `false_positive` lessons    | tag `polarity:negative`                                                 | suppression ("do NOT re-raise") memories                                                                                                                                                                                                                                        |
| human-attributed sources    | tag `pii:human-attributed`                                              | erasure-by-tag candidates                                                                                                                                                                                                                                                       |

**The import is idempotent.** `memory_key='lesson_<id>'` and a UUIDv5 `memory_id` (from
`clickgap://<org>/<namespace>/lesson/<id>`) are deterministic, and each run stamps one fresh
`version`. A re-import supersedes under `ReplacingMergeTree(version)` and the row count stays
the same.

## 1. Dry-run

No writes. The dry run checks that every timestamp parses, prints the kind → type and TTL
crosstab and the category count, and probes the embedder so rows do not land unembedded.

```bash
loom import-clickgap \
  --sqlite ~/Downloads/clickgap-memory-YYYYMMDD/memory.db \
  --org <org> --namespace clickgap --dry-run
```

Confirm the crosstab matches the source and that the embedder is reachable.

## 2. Import

Use a **dedicated namespace** (`clickgap`) so rollback is a single `namespace.delete`. The
export carries no vectors, so every row is embedded on import with the live
`LOOM_EMBEDDING_MODEL`.

```bash
loom import-clickgap \
  --sqlite .../memory.db --org <org> --namespace clickgap --agent clickgap
```

`--agent` defaults to `clickgap`; `--batch-size` sets rows per ClickHouse insert (default
1000). The verification block at the end prints, and fails the run (exit 1) on:

- live memory rows against the source's lesson count;
- wrong-model embedding rows (must be 0);
- the permanent (no-TTL) row count;
- the highest `recall_count`, read back.

`lesson_recalls` (the per-recall event log) is **not** imported by default: the
denormalized `recall_count` columns already carry the ranking signal. Pass
`--seed-recall-events` only if you need the per-event log in `loom.memory_usage_events` for
analytics.

> **Warning:** `--seed-recall-events` is additive and not idempotent: run it once, into a
> fresh namespace. It **drops `pr_number`**, because Loom usage events have no PR column.

## Recall signal

The importer mirrors `recall_count` onto `memory_embeddings.reinforcement_count` and
`last_recalled_at` onto `last_reinforced_at`. In `memory.search` that count feeds the
decayed-reinforcement rank term: `LOOM_MEMORY_SEARCH_REINFORCEMENT_WEIGHT` sizes it
(0 disables), `LOOM_MEMORY_SEARCH_REINFORCEMENT_HALF_LIFE_DAYS` sets its decay, and a
request can override both with `reinforcement_weight` / `reinforcement_half_life_days`
([configuration](../reference/configuration.md#memorysearch)).

The term is capped below relevance, so Loom does **not** reproduce ClickGap's
`recall_count DESC` ordering: it ranks primarily by similarity with entity, recency and
reinforcement terms ([retrieval and ranking](../concepts/retrieval-and-ranking.md)). If your
workload depends on recall-frequency ordering, validate that the resulting order is
acceptable (the validator reports recall-order τ) before cutover.

## 3. Validate before cutover

The validator mints its own admin token with `server.auth.jwt.mint_token`, so it needs
`LOOM_JWT_SECRET` and an explicit `LOOM_BASE_URL`: there is no default target. Its probes
are evaluation reads (`auto_touch: false`, the `bench/_gate.py` measurement fields), so a
run records no recall usage on the imported lessons and repeated runs do not feed the
reinforcement signal the τ gate measures.

> **Warning:** point it at a trusted endpoint. The probe queries are the lessons'
> assertions; the import data must not leave your infrastructure through a third-party
> embedder. `--show-queries` prints truncated probe text and is off by default for the same
> reason.

```bash
LOOM_BASE_URL=http://localhost:7777 LOOM_JWT_SECRET=... \
python -m bench.clickgap_migration_validate \
  --sqlite .../memory.db --org <org> --namespace clickgap --agent clickgap \
  --probes 200 --top-k 10 \
  --min-recall 0.90 --min-recallw 0.90 --min-tau 0.2
```

Each gate fails the run with a non-zero exit when set above 0 (0 = report only):

| Gate            | Measures                                                                                                                                                            |
| --------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `--min-recall`  | Known-item recall@k: is the probe lesson itself in Loom's top k? Per-kind and per-entity-type breakdowns catch a single slice collapsing (a wrong type remap, say). |
| `--min-recallw` | Recall-weighted recall@k: each probe weighs by its `recall_count`, so missing a hot lesson costs more.                                                              |
| `--min-tau`     | Recall-order τ: Kendall agreement between Loom's order and `recall_count`. A diagnostic: Loom does not sort by `recall_count`, so don't expect a high τ.            |

A failed search is reported and excluded from the ratios, never scored as a miss, and any
failed search makes the run exit non-zero, gates or not. A run with no computable τ prints `n/a` and
fails `--min-tau`.

Summaries (`summary → episodic`) are excluded from probes, since they are non-searchable by
design. `--seed` (default 42) makes the probe sample reproducible; `--concurrency`
(default 8) bounds concurrent searches.

## Use suppression memories

The `false_positive` lessons ("looks like a bug, do NOT re-raise") are the suppression
layer. They are searchable and carry the `polarity:negative` tag. A detector deciding
whether to raise a candidate finding runs a tag-filtered search over the finding's text:

```python
hits = await agent.memory.search(finding_text, tags=["polarity:negative"], top_k=5)
if hits and hits[0].similarity >= SUPPRESS_THRESHOLD:  # calibrate per embedder
    skip_or_downweight(finding)  # a known false positive
```

The `tags` filter applies to every retrieval leg (vector, lexical and the recency/confidence
backfill), so the result holds only suppressions
(`test_lexical_fallback_applies_caller_filters` in `tests/unit/test_memory_search_service.py`).
A leaked positive would hide a real finding.

Filter any kind the same way through its `kind:<kind>` tag: `tags=["kind:principle"]`, or
`tags=["kind:false_positive", "kind:maintainer_preference"]` for a multi-kind kill-list
(`tags` is an OR filter). `memory.search`'s `memory_type` parameter accepts only the coarse
types (`semantic`, `procedural`, `preference`, `reflection`, `decision`), so the full kind
taxonomy lives on tags.

## Roll back

- **Full revert:** `namespace.delete` on `clickgap` (admin; over MCP call it through
  `loom_op`). The import targets a dedicated namespace, so nothing else is removed.
- **Erasure or cohort scrub:** `erasure.request` with a namespace scope (checkpointed and
  resumed by the recovery worker), or erase only the `pii:human-attributed` rows for a
  maintainer's removal request ([retention and erasure](../features/memory/retention-and-erasure.md)).
- **Mapping fix:** re-run the importer. Deterministic keys and a higher `version` supersede
  the bad rows in place.

## Consolidate (optional, after validation)

If you compact entity-set near-duplicates, run `memory.consolidate` **after** validation.
The guards in `server/memory/app/consolidate.py` exclude **pinned** rows and never merge
across `memory_type` or across **polarity**, so a `false_positive` suppression can't be
absorbed into a positive observation, and a principle can't absorb an observation.

Keep `consolidation_enabled=false` on the namespace (`namespace.register`) so the
background consolidation worker doesn't consolidate ClickGap's already rolled-up `summary`
lessons a second time.

## Related

- [Writing memory](../features/memory/writing.md)
- [Retrieval and ranking](../concepts/retrieval-and-ranking.md)
- [Namespaces and sharing](../features/namespaces-and-sharing.md)
