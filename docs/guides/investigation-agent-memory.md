# Store an investigation agent's memory

An investigation agent produces several kinds of state, and Loom has a plane for each. This
page says which plane each kind goes to. Over MCP, an op your toolset does not list is called
by name through `loom_op`.

> **Warning:** one JSON blob per checkpoint through `memory.set` breaks recall. The embedded
> text is a JSON prefix, the claim sits past the embed clip, and questions abstain.
> `namespace.audit` flags this shape; see [optimize a namespace](optimize-namespace.md).

## Where each kind of state goes

| The agent has…                                                                                                | Plane           | Op                                               | Shape                                                                                                                                          |
| ------------------------------------------------------------------------------------------------------------- | --------------- | ------------------------------------------------ | ---------------------------------------------------------------------------------------------------------------------------------------------- |
| a **claim** it established (an evidence finding, an answered question, a node of its model, a timeline entry) | memory          | `memory.set`                                     | one plain-text sentence or short paragraph: the claim first, then its source line; `tags` and `entities` carry the facets                      |
| a **document** it wrote or collected (notes, a report, a review body, anything over a couple of KB)           | knowledge       | `knowledge.upsert`                               | Markdown with headings, `source_uri = loom://<org>/<namespace>/<key>`; plus a one-paragraph `memory.set` row under the same key pointing at it |
| a **table** of facts with identity (PR reviews by number, catalogue nodes by id, service identities)          | records         | `memory_schema.register` + `memory.record.write` | scalar fields and an identity; a later write folds in field by field                                                                           |
| **coordination state** (a lease, a pointer for the next agent, a freeze flag)                                 | working memory  | `working_memory.set` / `working_memory.get`      | small mutable blocks read by `block_key`; never embedded or ranked                                                                             |
| a **checkpoint** it resumes from (partial results, step outputs)                                              | context blobs   | `context.offload` / `context.fetch`              | the blob with a `summary` and a `ttl_seconds`                                                                                                  |
| a **run ledger** entry                                                                                        | episodic memory | `memory.set memory_type=episodic`, or `run.*`    | one short row keyed by run id                                                                                                                  |

A small resume cursor (the ids already done) fits a working-memory block instead of a blob;
see [trace and resume](trace-and-resume.md).

Put the agent's **read contract** (how to navigate all of this) in a `profile.set` section, so
every session loads it through `context.pack`.

## Keys and tags

Keys are paths a person can read, shaped `<scope>/<kind>/<id>`:

```
sre/case/<case_id>/evidence/<evidence_id>
sre/case/<case_id>/question/<question_id>
sre/case/<case_id>/model/<model_id>                 (headline row + document)
sre/case/<case_id>/model/<model_id>/node/<node_id>
sre/pr-review/<repo>/<number>                       (headline row + document + record)
```

**A claim that changes is written again under the same key.** Loom keeps the versions
(`memory.history`), `memory.get` returns the current one, and `memory.list` / `memory.search`
hide superseded rows unless `include_superseded` is set. Do not mint a new key for a
revision, and do not keep an index row that names the current key: the key is the index, and
`memory.list` with `prefix=` groups keys.

Tags carry what a query filters on: `case:<id>`,
`kind:<evidence|question|model-node|pr-review|…>`, `status:<observed|answered|open>`,
`repo:<owner/name>`. `entities` carry component names. `valid_at` is when the observation
was made (when an excerpt was retrieved, when a PR merged), not when the row was written.

## Writer rules

- **Serialise once.** Send text, or the object itself, as `value`, never
  `json.dumps(json.dumps(doc))`. `memory.set` returns an advisory when a value is
  double-encoded.
- **Claim first.** Only the start of a value is embedded: the excerpt is clipped at 1,200
  characters (`EXCERPT_LIMIT` in `server/memory/domain/excerpt.py`). A claim behind a
  long excerpt is not what the vector represents.
- **Documents go to `knowledge.upsert`.** `memory.set` advises on values over
  `LOOM_MEMORY_VALUE_ADVISORY_CHARS`, and a value past the embedder's input limit is never
  embedded at all.
- **Leases and pointers go to working memory.** A key read over and over by exact key is
  coordination state, not a memory.
- **Readable keys**, never hashes, and never base64.

## Reshape a namespace that already holds blobs

Run `namespace.audit` and follow [optimize a namespace](optimize-namespace.md) (or the
`loom-optimize` skill). It reworks rows with `memory.move`, so every old key keeps answering
while the writer is changed. It deletes nothing.

## Retrieval

| Question                           | Call                                                                                                                                               |
| ---------------------------------- | -------------------------------------------------------------------------------------------------------------------------------------------------- |
| "What do we know about X?"         | `memory.search` with a natural-language query and `tags=["case:<id>"]`; documents are lifted by the chunk leg                                      |
| "What is field F of record R?"     | `memory.record.query`                                                                                                                              |
| "Walk the model"                   | `memory.backlinks` / `memory.related` from a node row; edges are `memory.link` rows (`relation=derived_from` from a node to the evidence it cites) |
| "Where are we?"                    | `working_memory.get block_key=lease/<case>`                                                                                                        |
| "Resume attempt N"                 | `context.fetch blob_key=sre/steps/<case>/<attempt>`                                                                                                |
| "Load everything for this session" | `context.pack` with a `token_budget`                                                                                                               |

Over MCP, `loom_search` answers the first row: it searches memory, knowledge and
conversations in one call.

## Related

- [Agent integration](agent-integration.md): what to write, from any client
- [Writing memory](../features/memory/writing.md)
- [Knowledge](../features/knowledge.md)
- [Working memory and profiles](../features/memory/working-memory-and-profiles.md)
- [Trace and resume](trace-and-resume.md)
