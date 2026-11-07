# Entity graph

Entity communities are off by default (`LOOM_ENTITY_COMMUNITIES_ENABLED`).

Loom keeps an entity graph beside the memory rows, in the same ClickHouse database. Entities are nodes. Two
entities named in the same memory share a co-occurrence edge; extraction adds typed `(subject, relation, object)`
triples as a second edge set. The graph is a projection of memory: every edge traces back to the rows that produced
it. Use it for questions vector search cannot answer, such as "what is connected to the billing service".

## What the graph holds

| Table                                                                    | Holds                                                                                                                                   |
| ------------------------------------------------------------------------ | --------------------------------------------------------------------------------------------------------------------------------------- |
| `loom.entity_edges` (+ `entity_edge_memories`, `entity_edge_rows`)       | One row per co-occurrence observation (`co_occur = 1`); readers sum per pair. Source key per pair, and the row and value that named it. |
| `loom.entity_triples` (+ `entity_triple_memories`, `entity_triple_rows`) | Typed triples with `confidence` and the extractor model as `adapter_name`. Source key per triple, and the row and value that stated it. |
| `loom.entity_degree`                                                     | Each entity's total co-occurrence degree (`SummingMergeTree`); the hub rules read it.                                                   |
| `loom.entity_canonical_map`                                              | Mention text → canonical entity.                                                                                                        |
| `loom.entity_communities`                                                | Community id per entity, per level.                                                                                                     |
| `loom.entity_vectors`, `loom.entity_summaries`                           | Embeddings read by the entity legs of search.                                                                                           |
| `loom.derived_facts`                                                     | Numeric facts with unit and date, for computed answers in search.                                                                       |
| `memory.link` edges                                                      | Edges an agent or person asserts (`supports`, `depends_on`, `caused`, …); read by graph-mode search.                                    |

Edges and triples carry `valid_from` / `valid_to` (from the source memory's `valid_at` / `valid_to`) and an
`invalid_at` stamp that retires them. A read with `as_of` sums only observations valid at that instant.

The graph lives in ClickHouse so that edges are written in the same request as their row, erased by the same saga,
and joined against memory without a network hop. The cost: a walk is a series of bounded aggregate queries, so every
walk is capped.

## How the graph is built

The memory row lands first; the graph rows are projections of it. A failed projection is logged to
`loom.memory_projection_failures` and retried, only while the row still has the value that failed to project. It
never fails the write. A `high`-sensitivity bulk entry projects nothing; a `high` `memory.set` projects only the
edges of the entities its writer declared, never its text.

1. **Entities.** The writer passes `entities` on `memory.set`, or `memory.set_from_messages` extracts them. The
   `entity_extractor` worker (`LOOM_ENTITY_EXTRACT_ENABLED`, on) tags rows written without them, off the write path; it
   needs a real LLM provider. Entity text is canonicalised the same way on write and read (articles and possessives
   stripped, head noun singularised, first person mapped to `user`), so "The Billing Service's" and "billing service"
   are one node. A `commit:<sha>` or `error:<name>` name is an identifier, canonicalised the same way but never
   singularised: `error:Timeouts`, or the same name in backticks or quotes, is the node `error:timeouts`. A blank right
   after the colon (`Error: the build failed`) makes it prose. Pronouns, interjections, light verbs and two-character
   tokens are dropped, and so is text longer than 256 characters as written or once normalised (NFKC turns `ﬁ` into
   `fi`), on write and in an `entity_filter`; a point write refuses such a fact subject or object instead.
2. **Edges.** Every pair of a memory's entities becomes one observation row, at most 100 pairs per memory.
3. **Triples.** An extracted fact's relations become triples, at most 8 per memory. Verbs are normalised to
   `lower_snake`. `namespace.register_ontology` (admin) registers preferred entity types, verbs and attributes with a
   cardinality. Names outside the set produce `ontology_warnings`; nothing is rejected.
4. **Supersession closes triples.** When a row supersedes another, the old row's open triples that the new one does
   not attest get `invalid_at`. For a `one` relation (employer, location), the newer value stays current and the older
   one closes when the newer starts. A write that states the older value again while that close runs keeps it open.
   - Values order by the day they start (an undated value starts when it arrives), then by arrival to the
     microsecond, so racing writers agree on one current value. Within one write, the later-stated value is newer.
   - Within one write, an undated value in the past tense (`lived_in`, `worked_at`) is older than a dated one, in
     either order: "user lived_in london" next to Paris from Oct 1 leaves Paris current.
   - A value is judged at its newest row: "London from Oct 15, Paris from Oct 14, London from Oct 13" leaves London
     current.
   - A value stated with its own end ("London until December") is never closed early. It stays current beside the
     newer value until that end.
   - An Undo (`memory.restore`) makes the restored value current from the restore on, or from the fact's own later
     start ("moving to Berlin on Dec 1"). The other value closes, and its row stays live. A value dated on a later
     day still wins, and so does a value stated after the Undo. Undoing back and forth adds no weight.
   - A failed close is replayed as of the write or Undo that made it, so a value stated since stays current.

   How an Undo restates its facts and edges, weighs them and is replayed is in
   [memory architecture](../architecture/memory-architecture.md#undo).

A point write (`memory.set`, `memory.update`, `memory.update_by_id`) writes the triples the caller passes in
`relations`, at most 8, over HTTP and in the SDKs' typed operations (not in the MCP listing); it extracts none.
Automatic extraction runs on the bulk path: `memory.set_from_messages`, or a bulk writer that supplies relations.
With the stock fake providers, only the entities you pass form edges.

## Operations

| Operation                    | Role   | Answers                                                                                                       |
| ---------------------------- | ------ | ------------------------------------------------------------------------------------------------------------- |
| `facts.list`                 | viewer | Triples filtered by `subject`, `object`, `relation` (AND) or `entity` (either end).                           |
| `facts.related`              | viewer | Entities that co-occur with one entity, strongest first, and which side the seed is on.                       |
| `facts.traverse`             | viewer | The neighbourhood N hops out from a seed.                                                                     |
| `facts.path`                 | viewer | The shortest chain of edges between two entities within `max_hops`.                                           |
| `facts.expire_by_adapter`    | admin  | Tombstone the triples one extractor wrote. The memories stay.                                                 |
| `memory.search` (graph mode) | viewer | `search_mode="graph"`: hits one hop from the query's entities ([search and ask](./memory/search-and-ask.md)). |
| `memory.related`             | viewer | Memories that share entities with a given memory.                                                             |
| `memory.timeline`            | viewer | An entity's memories in time order (`entity=…`).                                                              |
| `memory.backlinks`           | viewer | Memories that share a row's entities.                                                                         |
| `namespace.taxonomy`         | viewer | A namespace's top entities, and communities when computed.                                                    |

Field lists: [facts](../reference/operations/facts.md), [memory](../reference/operations/memory.md). Outside graph
mode, `entity_filter` boosts rows about the named entities, and search's entity legs find rows about an entity the
query names in other words ([retrieval and ranking](../concepts/retrieval-and-ranking.md)).

### `facts.list`

Unfiltered, it returns the top triples by `co_occur`; `sort_by="recency"` orders by `last_seen`.

| Field                   | Effect                                                                                               |
| ----------------------- | ---------------------------------------------------------------------------------------------------- |
| `include_sources`       | Adds up to about ten source memory keys per triple.                                                  |
| `include_source_status` | Reads each source row; marks a triple `stale` when it is open but every row behind it is superseded. |
| `provenance`            | `extracted` (both ends in the source text), `inferred`, or `facet` (a navigation edge Loom minted).  |
| `as_of`                 | Keeps triples valid at that instant.                                                                 |

### `facts.traverse`

The walk trims before it expands. Each hop sends the frontier as one query, sums observations per pair inside the
validity window, drops visited entities, and keeps each parent's `top_k_per_hop` strongest pairs. A hop-h frontier
has at most K^h entities. Each entity is reported once, at the first hop that reaches it, through its strongest
parent, so the result draws as a tree.

One recursive query that expands everything and trims afterwards would materialise the whole neighbourhood; on a large
namespace a three-hop walk can exceed the ClickHouse memory limit. Trimming per hop bounds the cost by the answer size.

| Field                   | Default                                  | Effect                                                                                                         |
| ----------------------- | ---------------------------------------- | -------------------------------------------------------------------------------------------------------------- |
| `max_hops`              | 2 (1–4)                                  | Walk depth.                                                                                                    |
| `top_k_per_hop`         | 20 (up to 100)                           | Children kept per parent per hop.                                                                              |
| `min_co_occur`          | 1                                        | Minimum observations per pair.                                                                                 |
| `max_nodes`             | 400 (up to 5,000)                        | Hard bound; `truncated=true` when hit.                                                                         |
| `as_of`                 | unset                                    | Keep observations valid at that instant.                                                                       |
| `labeled` / `relations` | false / unset                            | Walk typed triples (all, or only these verbs); nodes carry `via_relation`, `via_direction`, `via_provenance`.  |
| `hub_degree`            | 0 (off)                                  | A node with total degree at least this is a hub.                                                               |
| `hub_share`             | `LOOM_FACTS_TRAVERSE_HUB_SHARE` (0.01)   | A node is a hub when the edge that reached it is at most this share of its degree.                             |
| `rank_by`               | `LOOM_FACTS_TRAVERSE_RANK_BY` (`cosine`) | Per-hop trim: `co_occur`, `specificity` (`via_co_occur / degree`) or `cosine` (`via_co_occur / sqrt(degree)`). |

Each node reports `hop`, `cumulative_weight`, `via_co_occur`, `path` (the chain from the seed), `valid_from` /
`valid_to`, `degree`, and `community_id` when communities exist. Nodes are ordered by hop, then weight.

**Hubs.** Generic entities ("user", "pr") co-occur with everything. A hub is reported (`hub: true`, listed in `hubs`)
but not expanded. The seed always expands, so seed from a hub to see its neighbourhood. `hub_share` is the default
rule because it does not depend on the namespace's degree scale. `hub_degree` is namespace-specific; read the degree
tail before setting it.

### `facts.path`

A bounded bidirectional breadth-first search over the same edges, meeting in the middle. `max_hops` (1–4, default 3)
bounds the total length. The answer is `found`, `path` and `edges` with each pair's `co_occur` and validity.
`truncated=true` means the answer may be incomplete. `found=false` with `truncated=false` means no connection within
`max_hops`.

## Example

```python
from loom import Agent

async with Agent("dev", "local", "sre-bot", base_url="http://localhost:7777") as agent:
    await agent.memory.set(
        "billing/depends_on",
        "The billing service reads invoices from the ledger database.",
        entities=["billing service", "ledger database"],
    )
    walk = await agent.facts.traverse("billing service", max_hops=2, top_k_per_hop=10)
    for node in walk["nodes"]:
        print(node["hop"], " > ".join(node["path"]), node["via_co_occur"])

    route = await agent.facts.path("billing service", "ledger outage")
    print(route["found"], route["path"], route["truncated"])
```

Over HTTP, a walk as the graph stood at a past instant:

```bash
curl -s localhost:7777/v1/facts.traverse -H "Authorization: Bearer $LOOM_API_KEY" \
  -H 'content-type: application/json' \
  -d '{"org":"dev","namespace":"local","seed_entity":"billing service","max_hops":2,"as_of":"'"$AS_OF"'"}'
```

Over MCP, call `loom_op` with `op="facts.traverse"`.

## Maintaining the graph

| Task                                | How                                                                                                            |
| ----------------------------------- | -------------------------------------------------------------------------------------------------------------- |
| Roll back a bad extractor's triples | `facts.expire_by_adapter(adapter_name=…, max_confidence=…)`. The memories stay.                                |
| Export the graph                    | `loom entity-export --org O --namespace N --format graphml` (or `jsonld`; `--all` includes invalidated edges). |
| Cluster entities into communities   | Set `LOOM_ENTITY_COMMUNITIES_ENABLED=true` (below).                                                            |
| See it                              | The dashboard's Graph view ([dashboard](./dashboard.md)).                                                      |

The `entity_communities` worker runs label propagation per namespace into `loom.entity_communities`, read by
`namespace.taxonomy`, graph-mode expansion and `community_id`. It covers namespaces with consolidation on plus those
walked in the last `LOOM_ENTITY_COMMUNITIES_WALKED_DAYS` (14). It drops the highest-degree entities first
(`LOOM_ENTITY_COMMUNITIES_HUB_DROP_FRACTION`) and doubles that fraction while one community holds more than
`LOOM_ENTITY_COMMUNITIES_GIANT_SHARE` of the entities, so generic tokens cannot fuse the graph into one community.

## Settings

| Setting                                      | Section                                                                                                                      |
| -------------------------------------------- | ---------------------------------------------------------------------------------------------------------------------------- |
| `LOOM_FACTS_TRAVERSE_HUB_SHARE`, `…_RANK_BY` | [Namespace health, knowledge graph, communities](../reference/configuration.md#namespace-health-knowledge-graph-communities) |
| `LOOM_ENTITY_COMMUNITIES_*`                  | Same section.                                                                                                                |
| `LOOM_ENTITY_EXTRACT_*`                      | [Consolidation](../reference/configuration.md#consolidation)                                                                 |
| `LOOM_MEMORY_SEARCH_GRAPH_SEED`              | [MemorySearch](../reference/configuration.md#memorysearch)                                                                   |

## Limits

- **Co-occurrence is not a relation.** Two entities in one memory are linked whatever the sentence says. Use typed
  triples (`labeled`, `relations`) or `memory.link` when the relation matters.
- **The graph is only as good as the entities.** Rows without entities add no edges until the `entity_extractor`
  worker reaches them.
- **Walks are bounded.** Read `truncated` before concluding two entities are not connected.
- **Edges can lag a superseded row.** `facts.list(include_source_status=true)` shows triples still open.
- **Validity is valid time.** `as_of` reads when a fact held, not when Loom learned it.

## Related

- [Search and ask](./memory/search-and-ask.md): graph mode.
- [Writing memories](./memory/writing.md): passing `entities`.
- [Extraction](./memory/extraction.md): where extracted entities and triples come from.
- [Memory architecture](../architecture/memory-architecture.md): the projection writers.
- [Workers reference](../reference/workers.md).
