# Search and ask

`memory.search` returns ranked hits for the agent to read. `memory.ask` runs the same
retrieval and has an LLM write an answer grounded in those memories, citing their keys.
Both abstain when memory does not support an answer, and say why in `abstain_code`. This
page owns the abstain codes. The ranking model is in
[retrieval and ranking](../../concepts/retrieval-and-ranking.md); the query path is in
[memory architecture](../../architecture/memory-architecture.md).

Over MCP, agents use the `loom_search` tool, which searches memory, profile, knowledge
and conversations in one call. `memory.search` is not an MCP tool of its own. See
[unified search](../unified-search.md).

## The read operations

| Operation                 | Returns                                                                                                                              |
| ------------------------- | ------------------------------------------------------------------------------------------------------------------------------------ |
| `memory.search`           | Ranked hits with scores, abstain and risk.                                                                                           |
| `memory.ask`              | A synthesised, cited answer, or an abstain.                                                                                          |
| `memory.search_trace`     | The vector leg of `memory.search`, with a per-hit scoring breakdown (no fusion, assembly or reranker).                               |
| `memory.search_trace_log` | Past searches of the namespace: keys and scores returned, abstain, risk, latency.                                                    |
| `memory.get`              | One row by exact key.                                                                                                                |
| `memory.bulk_get`         | Up to 1,000 keys in one round trip, each as `memory.get` answers it (without `conflict_verdict` and `code`); `missing` lists misses. |
| `memory.list`             | Keys and types under the identity, by recency or score; at most 1,000 per call.                                                      |
| `memory.timeline`         | Rows about an entity, tag set or key prefix, in time order.                                                                          |
| `memory.related`          | Rows that share entities with a known key, ranked by overlap.                                                                        |
| `memory.recall`           | The raw conversation turns behind the facts. See [extraction](./extraction.md).                                                      |
| `memory.brief`            | Orientation on a task: an answer, its grounding, unknowns, what to read next.                                                        |

All are viewer ops. Reads default to `agent="*"` (every author in the namespace):
membership is the read boundary, and `agent` records authorship. Pass `agent` only to
narrow to one author.

## `memory.search`

### What a hit carries

| Field                           | Meaning                                                                                                                                                                                                                                                                                                                                                                                     |
| ------------------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `memory_key`, `content_excerpt` | The row and the text it matched on.                                                                                                                                                                                                                                                                                                                                                         |
| `similarity`                    | Cosine to the query in [0, 1]. Threshold on this; no boost is added to it.                                                                                                                                                                                                                                                                                                                  |
| `rank_score`                    | The score the list is sorted by: the fused score, under `rrf` a reciprocal-rank sum (about 0.03). After a reranker that changed the order it is the pool's score for the rank the row now holds (above 1.0 where the pool's own was, as a parent-boosted chunk's can be), and its own fused score is `score_components.rank_score_pre_rerank`; a kept order leaves every row its own score. |
| `below_threshold`               | Under the similarity floor but kept (`floor_mode="readmit"`).                                                                                                                                                                                                                                                                                                                               |
| `source`                        | `fact` (memory embeddings), `chunk` (a passage of a long value) or `summary` (a session digest).                                                                                                                                                                                                                                                                                            |
| `parent_memory_key`             | For a chunk, the memory it belongs to.                                                                                                                                                                                                                                                                                                                                                      |
| `status`, `superseded_by`       | Lifecycle state; a superseded row served on request names its successor.                                                                                                                                                                                                                                                                                                                    |
| `source_class`, `verified`      | How the content entered memory, and whether a live attestation stands.                                                                                                                                                                                                                                                                                                                      |
| `valid_at`, `created_at`        | Validity time and record time.                                                                                                                                                                                                                                                                                                                                                              |
| `score_components`              | The main ranking terms (`explain=true` for more; `foreign=1.0` marks a hit from another namespace).                                                                                                                                                                                                                                                                                         |
| `namespace`                     | The source namespace on a cross-namespace search.                                                                                                                                                                                                                                                                                                                                           |

The response adds `abstain`, `abstain_code`, `retrieval_risk`, `candidates_total`,
`namespaces_searched`, `pending_extraction`, `degraded` and `next`.

### Modes and recipes

`search_mode` picks the fusion:

| Mode          | Ranks by                                         |
| ------------- | ------------------------------------------------ |
| `semantic`    | Cosine only. The server default.                 |
| `rrf`         | Reciprocal-rank fusion of lexical and semantic.  |
| `lexical`     | Token overlap only (identifiers, exact phrases). |
| `hybrid`      | `alpha` × lexical + (1 − `alpha`) × cosine.      |
| `bm25_hybrid` | IDF-weighted BM25 fused with cosine.             |
| `graph`       | Entity-edge expansion plus triple-match boosts.  |
| `related`     | Facet overlap (tags and `entity_filter`).        |
| `temporal`    | Date-aware ordering.                             |

A request that sets neither `search_mode` nor `recipe` gets the namespace's
`retrieval_defaults.search_mode`, else `LOOM_MEMORY_SEARCH_DEFAULT_MODE` (`semantic`).
The Python SDK's `search` always sends `search_mode` (default `rrf`), so the server
default applies to HTTP and MCP callers that leave it out.

`recipe` is a named preset. It fills only fields the caller left unset, so
`{"recipe": "quality", "hyde": false}` runs without HyDE:

| Recipe     | Sets                                                       |
| ---------- | ---------------------------------------------------------- |
| `fast`     | `semantic`, no HyDE, no unmatched candidates.              |
| `balanced` | `rrf`, no HyDE.                                            |
| `quality`  | `bm25_hybrid`, HyDE allowed, at least 50 extra candidates. |
| `verbatim` | `lexical`, `alpha` 0.85.                                   |
| `code`     | `bm25_hybrid`, `alpha` 0.75.                               |
| `temporal` | `temporal`.                                                |
| `graph`    | `graph`.                                                   |

`retrieval_budget` (`fast`, `standard`, `deep`) sets the cost. `standard` runs cheap
recall first and adds the LLM, graph and rerank legs only when the planner or a
weak-result fallback calls for them. `fast`, like the `fast` recipe, makes no LLM call,
whichever field asks for one: no HyDE, graph triples, retry judge or `builtin:openai`
rerank. A count or sum question still gets its computed answer when the
query parser reads its intent, matched on the predicate without LLM synonyms; when the
parser cannot, the search answers from recall alone. `context.pack` runs its memory search the
same way in every mode, keeping its recipe's planes.

`LOOM_READS_LLM_FREE=true` applies the same rule to every search, whatever its recipe or budget, and
to every other read: a search also skips the spelling-fix and set-expansion embeddings and runs no
namespace reranker (a reranker is a model call, so only one the request names runs, and never
`builtin:openai`), `memory.ask` answers 409 `READS_LLM_FREE` unless it asks with
`allow_synthesis=false`, and `memory.brief` returns its evidence with no answer
([LLM-free reads](../../architecture/llm-architecture.md#llm-free-reads)).

### Filters and scope

| Field                                                          | Effect                                                                                                                                   |
| -------------------------------------------------------------- | ---------------------------------------------------------------------------------------------------------------------------------------- |
| `memory_type`                                                  | One type. Unset: every searchable type except `reflection`, which only an explicit filter returns.                                       |
| `tags` / `tags_soft`                                           | OR filter on tags; with `tags_soft`, a boost (`tag_boost`) instead.                                                                      |
| `category` / `category_soft`                                   | The same for a registered category.                                                                                                      |
| `demote_tags`                                                  | Sinks matching rows to the bottom without dropping them.                                                                                 |
| `entity_filter`                                                | Rows naming these entities.                                                                                                              |
| `user_id`, `session_id`, `run_id`                              | Narrow to one end user, session or run; `-` means no filter.                                                                             |
| `read_scope`                                                   | `own_user` or `own_session`. A token's `read_scope` claim is a ceiling a request can only narrow.                                        |
| `namespace_scope`                                              | `this` (default) or `all_mine` (also the caller's own namespaces, up to the fan-out cap; the rest come back in `namespaces_skipped`).    |
| `exclude_keys`                                                 | Up to 200 keys the caller already holds, removed before the window is cut.                                                               |
| `include_duplicates`, `include_superseded`, `include_proposed` | Serve rows the default hides.                                                                                                            |
| `as_of`, `as_of_basis`                                         | Point-in-time replay: `valid_time` (default) or `knowledge_time`. See [validity time](./writing.md#validity-time-valid_at-and-valid_to). |
| `observation_date`                                             | `YYYY-MM-DD` anchor for relative dates in the query ("last week").                                                                       |
| `top_k`                                                        | Window size, default 5, at most 500.                                                                                                     |
| `bodies`, `body_budget_tokens`                                 | How much of each value rides the hit (`full`, `excerpt`, `none`, `unit`, `source`).                                                      |
| `render="reader"`                                              | Also return the hits as `memory.ask` reads them (`reader_block`).                                                                        |

### Ranking terms beyond relevance

Each has a request field and a deployment default. `personalization="none"` turns every
user-derived term off for one request.

| Term          | Control                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                     |
| ------------- | ----------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| Reinforcement | Use lifts a row, decaying with a half-life (`LOOM_MEMORY_SEARCH_REINFORCEMENT_WEIGHT` 0.15, `…_HALF_LIFE_DAYS` 90). Use comes from `run.complete(memories_used=…)`, `memory.outcome_run`, `memory.outcome` and the keys `memory.ask` cites; the `usage_fold` worker folds it. A `harm` event takes back earned boost but never ranks a row below its relevance.                                                                                                                                                                                             |
| Confidence    | `confidence_weight` (default 0.1) × the row's `confidence`.                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                 |
| Metadata      | Soft tags and categories, `demote_tags`.                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                    |
| Key match     | Rows whose key covers the question's content words, as a tie-break (`LOOM_MEMORY_SEARCH_KEY_MATCH_MODE`).                                                                                                                                                                                                                                                                                                                                                                                                                                                   |
| Provenance    | `LOOM_MEMORY_SEARCH_TRUST_WEIGHT` (default 0, off): a bounded term for source class and attestation.                                                                                                                                                                                                                                                                                                                                                                                                                                                        |
| Recency       | `recency_pool_weight` re-ranks a "latest" / "most recent" query by date.                                                                                                                                                                                                                                                                                                                                                                                                                                                                                    |
| Reranker      | `rerank`, or the namespace's `rerank_endpoint`: `builtin:cohere[:model]`, `builtin:openai[:model]` or the name of an endpoint in `LOOM_RERANK_ENDPOINTS_JSON` (a URL is a 422). It orders the fact pool the results are cut from (the abstain and confidence gates still read the fused order, and the similarity floor still puts rows under it last or drops them); an endpoint names rows by key, so the rows of a key it names twice keep their fused order. On timeout it keeps the input order and says so in `degraded`; it never drops a candidate. |

### Query spelling

An embedder reads `datalakecatalog` and `DataLakeCatalog` as different strings. So a query
of at most `LOOM_MEMORY_SEARCH_QUERY_SPELLING_MAX_WORDS` words (default 6) embeds each
all-lowercase word of four or more letters in the identifier spelling the caller's rows in
the addressed namespace use: one with a capital past the first letter (`DataLakeCatalog`,
`PREWHERE`), used by at least two rows and by twice as many as write the word plainly. A
sentence-initial capital is not evidence of how the corpus spells a word, so title case
counts as plain and `what` never becomes `What`. Lexical matching is case-insensitive
already and is unchanged.

The lookup runs beside the embed of the query as typed, and its answer is cached for a
minute. A rewrite shows in `retrieval_work.query_spellings` (`memory.search_trace`:
`query_spellings`). The first time, it costs a second embed, of the respelled text; while
cached, only the respelled text is embedded, and the text as typed only if that embed
fails. After a timeout the try as typed gets 0.25 s, time for an embedding-cache hit, so a
hung provider still answers `EMBED_TIMEOUT` within one embed bound plus 0.25 s. A search
fails only when every embed it tried failed, so a provider that refuses fails it once the
lookup has answered (at most 0.4 s). The query embeds as typed, with
`retrieval_work.query_spellings_skipped` set (`memory.search_trace`:
`query_spellings_skipped`), when the lookup fails or runs past 0.4 s, when the namespace
is backing off after three such failures in a row (a minute, doubling with each further
failure up to an hour; a success ends the streak), or when the respelled embed fails.
`retrieval_cost_estimate` is made before the lookup answers, so it counts one embed even
when a rewrite adds a second. `loom.search_trace_log` keeps the query as typed, so a
logged query can score differently from a fresh embed of the same text.

### Similarity floor and abstain

With `LOOM_ADAPTIVE_THRESHOLDS` on (the default), a boot probe derives the search gates
from the live embedder's score distribution; `calibration.get` shows a namespace's
calibration. The scalar settings (`LOOM_MEMORY_SEARCH_MIN_SIMILARITY` 0.22,
`LOOM_MEMORY_SEARCH_ABSTAIN_THRESHOLD` 0.30) apply when adaptivity is off or the embedder
is fake; then re-bench after an embedder change.

A namespace (`retrieval_defaults`) or request may set `min_similarity` and `floor_mode`:
`readmit` keeps sub-floor hits labelled `below_threshold`, `ids_only` returns them
without bodies, `drop` removes them.

When the best hit is not good enough, the response says `abstain=true`:

| `abstain_code`              | Meaning                                              |
| --------------------------- | ---------------------------------------------------- |
| `no_match`                  | Nothing in scope matched.                            |
| `below_threshold`           | The best hit is under the similarity floor.          |
| `low_calibrated_confidence` | The calibrated confidence of the top hit is too low. |
| `entity_absent`             | The entity the question is about is not in memory.   |
| `page_expired`              | A `page=` handle named a parked list that is gone.   |

`retrieval_risk` grades the answer `low`, `medium`, `high` or `abstain`. Treat an abstain
as "Loom does not know". Branch on `abstain_code`, never on the free-text
`abstain_reason`. If `pending_extraction > 0`, a deferred `memory.set_from_messages` for
this scope has not run yet, so a thin result is not evidence of absence.

### Paging

`candidates_total` is the size of the fused pool, counted in rows. One key holds a row
per writer and session, so a search can serve several rows of one key, each with its own
`session_id` and text; `memory_search_per_key_cap` bounds how many. With
`paging=true` (the `loom_search` tool always asks), a larger pool is parked and
`next_page` returns a handle to pass as `page=`.

### Example

```python
from loom import Agent

async with Agent(org="acme", namespace="support", agent="triage-bot") as agent:
    hits = await agent.memory.search(
        "when can we deploy to production?",
        tags=["deploy"],
        top_k=5,
    )
    if hits.abstain:
        print("memory does not know:", hits.abstain_code)
    else:
        for hit in hits:
            print(hit.memory_key, round(hit.similarity, 3), hit.content_excerpt)
```

The SDK returns a `MemorySearchResult`: a list of `MemorySearchHit` with the envelope
(`abstain`, `abstain_code`, `retrieval_risk`, `pending_extraction`, `degraded`, `next`)
as attributes. Over HTTP it is `POST /v1/memory.search`; from a terminal,
`loom memory-search --namespace support --query "deploy window"`.

## `memory.search_trace`

`memory.search_trace` takes the same fields as `memory.search` and returns, per hit, the
cosine `distance` and `similarity`, `entity_boost`, `rank_score` and `rank`. It scores the
vector leg alone: no fusion, assembly or reranker, so use it to inspect what the vector
leg found. When a hit you expected ranks below one you did not, run the search with
`explain=true`: its `score_components` carry the ranking signals and, after a reranker,
each row's move (`rerank_from`, `rerank_pos`, `rerank_delta`) and, when it changed the
order, the row's own fused score (`rank_score_pre_rerank`).

With `LOOM_SEARCH_TRACE_LOG_ENABLED=true`, every search appends a row to
`loom.search_trace_log` (keys and scores returned, abstain, risk, latency, run id,
calling op), kept 180 days and read with `memory.search_trace_log`. The query text is
stored only when the namespace consents to text capture. A non-admin sees only its own
rows. `run.get` uses this log to report which keys a run recalled.

## `memory.ask`

`memory.ask` retrieves as `memory.search` does (default `top_k` 15) and writes an answer,
citing keys in `memory_keys` and sessions in `cited_sessions`. `source` says how it
answered:

| `source`         | Meaning                                           |
| ---------------- | ------------------------------------------------- |
| `synthesized`    | An LLM answer grounded in the retrieved memories. |
| `retrieval_only` | `allow_synthesis=false`: hits only, no LLM call.  |
| `abstained`      | Retrieval did not support an answer.              |
| `record`         | Answered from typed records.                      |
| `precedent`      | Answered from a recorded decision precedent.      |

Superseded rows leave synthesis and sentences restating a retired value are masked
(`superseded_dropped`, `stale_sentences_masked`); `include_superseded=true` keeps them as
former values. A question whose premise no memory records is answered with that fact
first (per request `presupposition_rule`). Cited keys get a `used_cited` usage event, so
ranking follows use.

Behaviour, each a setting in the
[MemoryAsk section](../../reference/configuration.md#memoryask):

| Setting                                       | Behaviour                                                                     |
| --------------------------------------------- | ----------------------------------------------------------------------------- |
| `LOOM_MEMORY_ASK_EXCLUDE_UNVERIFIED_EXTERNAL` | Unattested `external` rows stay out of synthesis; they still surface as hits. |

Knowledge documents are searched too and cited as `[knowledge:<source>#<chunk>]` (per
request `knowledge=false` opts out).

`recent_messages` (up to 40 turns, never stored) lets a follow-up resolve its referents.
`max_answer_chars` bounds the answer. Synthesis needs a real LLM provider; the stock fake
provider's answer text is not meaningful.

```bash
curl -sX POST http://localhost:7777/v1/memory.ask \
  -H "Authorization: Bearer $LOOM_API_KEY" -H 'Content-Type: application/json' \
  -d '{"org":"dev","namespace":"local","agent":"ops-bot",
       "query":"Can we deploy to production on a Friday?"}' \
  | jq '{answer, source, abstain, memory_keys}'
```

## `memory.timeline` and `memory.related`

`memory.timeline` returns rows in time order by `coalesce(valid_at, created_at)`, newest
first unless `order="asc"`. It needs one of `entity`, `tags` or `memory_key_prefix`;
`since`, `until` and `top_k` (default 50) window it, and `total_matching` says whether
there is more.

`memory.related` finds rows that share entities with a known `memory_key`, ranked by the
number shared (`min_overlap`, default 1). It depends on entity extraction
(`LOOM_ENTITY_EXTRACT_ENABLED`, on by default). A source row with no entities answers
`results=[]` with `reason="source_has_no_entities"`. For multi-hop walks use the
[entity graph](../entity-graph.md).

## After changing the embedding model

Search reads only vectors of the live `LOOM_EMBEDDING_MODEL`. Rows embedded by another
model are invisible until re-embedded:

```bash
loom reembed --dry-run          # count off-model rows
loom reembed                    # re-embed them with the live embedder
loom reembed --namespace NS     # one namespace
loom reembed --resume TOKEN     # continue after the last `resume:` line
loom reembed --delete-old       # drop old-model vectors once verified
```

`loom reembed` regenerates vectors from stored content and keeps metadata. Adaptive
thresholds recalibrate at boot; `loom recalibrate` does it at once (the `recalibration`
worker otherwise runs weekly).

## Settings

| Setting                                       | Default    | Effect                                                                              |
| --------------------------------------------- | ---------- | ----------------------------------------------------------------------------------- |
| `LOOM_MEMORY_SEARCH_DEFAULT_MODE`             | `semantic` | Mode for a request that sets neither mode nor recipe.                               |
| `LOOM_MEMORY_SEARCH_DEFAULT_NAMESPACE_SCOPE`  | `this`     | `all_mine` also searches the caller's own namespaces.                               |
| `LOOM_MEMORY_SEARCH_FANOUT_MAX_NAMESPACES`    | 32         | Cap on the caller's namespaces an `all_mine` search reads (plus the addressed one). |
| `LOOM_ADAPTIVE_THRESHOLDS`                    | true       | Derive similarity gates from the live embedder.                                     |
| `LOOM_MEMORY_SEARCH_MIN_SIMILARITY`           | 0.22       | Scalar floor when adaptivity is off or the embedder is fake.                        |
| `LOOM_MEMORY_SEARCH_ABSTAIN_THRESHOLD`        | 0.30       | Scalar abstain gate under the same conditions.                                      |
| `LOOM_MEMORY_SEARCH_REINFORCEMENT_WEIGHT`     | 0.15       | Weight of the decayed-use term. 0 disables.                                         |
| `LOOM_MEMORY_SEARCH_TRUST_WEIGHT`             | 0          | Provenance term. 0 disables.                                                        |
| `LOOM_SEARCH_TRACE_LOG_ENABLED`               | false      | Record each search in `loom.search_trace_log`.                                      |
| `LOOM_MEMORY_SEARCH_QUERY_SPELLING_MAX_WORDS` | 6          | Longest query whose lowercase words embed in the corpus spelling. 0 disables.       |

Per namespace, `namespace.register` sets `retrieval_defaults` (`search_mode`, `top_k`,
`bodies`, `min_similarity`, `floor_mode`, `chunk_max_share`, `recency_pool_weight`,
`reinforcement_weight`, …) and `rerank_endpoint`. See the
[configuration reference](../../reference/configuration.md#memorysearch).

## Limits

- **Fake providers make recall meaningless.** Judge ranking and `memory.ask` only with
  real providers.
- **Writes are eventually consistent.** Deferred extraction reports `pending_extraction`.
- **Hidden by default:** duplicates (the canonical takes the slot), superseded rows,
  proposals, `reflection` rows, expired rows, `high` sensitivity rows (never embedded,
  readable by key only), and `chunk` and `summary` hits whose text reads as high (a
  credential, card number, SSN or stated PIN). A `chunk:` key still reads by
  `memory.get`, with an audit row ([sensitivity](../../architecture/security.md#sensitivity)).
- **Degraded providers are reported** in `degraded` (a reranker timeout, a skipped HyDE
  call).
- **Off-model vectors are invisible** until `loom reembed` runs.

## Related

- [Retrieval and ranking](../../concepts/retrieval-and-ranking.md)
- [Unified search](../unified-search.md): `loom_search` and `context.pack`.
- [Writing memory](./writing.md)
- [Runs](../runs.md): reporting which memories a task relied on.
- [Performance tuning](../../guides/performance-tuning.md)
