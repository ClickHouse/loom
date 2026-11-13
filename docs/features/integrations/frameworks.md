# Agent frameworks and orchestrators

The SDKs ship adapters that present Loom through a framework's own memory or retrieval
interface. The LangChain and LlamaIndex adapters need their framework: install the SDK's
extra for it (`pip install 'clickhouse-loom[langchain]'`, `[llamaindex]`), or, in
TypeScript, `@langchain/core`, an optional peer dependency. The LangGraph and Strands
adapters import without their framework. Identity comes from the Loom `Agent` the adapter
wraps, and isolation is Loom's namespace authorization. Every adapter calls your Loom
server through the SDK: the data is stored in your Loom deployment (ClickHouse, and
Postgres for live memory under the [Postgres memory
authority](../../architecture/storage-authority.md)), and no adapter adds or needs another
database, vector store or cache.

| Framework      | Adapter                                         | SDK                                                   | Framework interface                    |
| -------------- | ----------------------------------------------- | ----------------------------------------------------- | -------------------------------------- |
| LangChain      | `LoomKnowledgeRetriever`, `LoomMemoryRetriever` | Python, `loom.integrations.langchain`                 | `BaseRetriever`                        |
| LangChain.js   | `LoomKnowledgeRetriever`, `LoomMemoryRetriever` | TypeScript, `@clickhouse/loom/integrations/langchain` | `BaseRetriever` from `@langchain/core` |
| LlamaIndex     | `LoomRetriever`, `LoomMemoryBlock`              | Python, `loom.integrations.llamaindex`                | `BaseRetriever`, `BaseMemoryBlock`     |
| LangGraph      | `LoomStore`                                     | Python, `loom.integrations.langgraph`                 | `BaseStore`                            |
| Strands Agents | `LoomMemoryStore`                               | Python, `loom.integrations.strands`                   | the `MemoryStore` protocol             |
| Strands Agents | `LoomMemoryStore`                               | TypeScript, `@clickhouse/loom/integrations/strands`   | the same protocol, structurally        |

Any other framework that can call an HTTP endpoint or an MCP server uses Loom directly:
see [agent integration](../../guides/agent-integration.md) and
[MCP clients](./mcp-clients.md).

## LangChain

`LoomKnowledgeRetriever` searches the namespace's documents (`knowledge.search`) and
`LoomMemoryRetriever` its memories (`memory.search`). Both are LangChain `BaseRetriever`s,
so they work with `invoke`, in a chain, and as an agent tool through
`create_retriever_tool`. Install with `pip install 'clickhouse-loom[langchain]'`
(`langchain-core` 1.x).

```python
from loom import Agent, SyncAgent
from loom.integrations.langchain import LoomKnowledgeRetriever, LoomMemoryRetriever

with SyncAgent(org="acme", namespace="support") as agent:
    docs = LoomKnowledgeRetriever(agent=agent, k=3).invoke("refund policy for annual plans")

async with Agent(org="acme", namespace="support") as agent:
    memories = await LoomMemoryRetriever(agent=agent, user_id="alice").ainvoke("seat preference")
```

`invoke` needs a `SyncAgent`; `ainvoke` takes an `Agent` or a `SyncAgent`.

| Retriever                | `page_content`  | `id`                                                                                | `metadata`                                                                                        |
| ------------------------ | --------------- | ----------------------------------------------------------------------------------- | ------------------------------------------------------------------------------------------------- |
| `LoomKnowledgeRetriever` | the chunk       | `<namespace>/<source_uri>#<chunk_idx>`                                              | `source_uri`, `chunk_idx`, `similarity`, `section`, `namespace`, `tags`                           |
| `LoomMemoryRetriever`    | the memory text | none: a key is unique per agent, user and session, and a search reads every agent's | `namespace`, `memory_key`, `memory_type`, `similarity`, `source`, `valid_at` when the row has one |

| Field                                         | Retriever | Default                                   | Effect                                                                               |
| --------------------------------------------- | --------- | ----------------------------------------- | ------------------------------------------------------------------------------------ |
| `k`                                           | both      | 4                                         | documents per query                                                                  |
| `search_tags`                                 | both      | none                                      | only rows carrying at least one of these Loom tags (`tags` is LangChain's own field) |
| `uri_prefix`                                  | knowledge | none                                      | only documents whose `source_uri` starts with one of these                           |
| `namespace_scope`                             | knowledge | `this`                                    | `all_mine` adds the caller's other namespaces, up to the server's fan-out cap        |
| `search_kwargs`                               | knowledge | none                                      | further `knowledge.search` fields, such as `search_mode` or `rerank`                 |
| `memory_type`                                 | memory    | every searchable type except `reflection` | only memories of this type                                                           |
| `user_id`, `session_id`, `run_id`, `agent_id` | memory    | none                                      | only that user's, session's, run's or agent's memories                               |
| `honour_abstain`                              | memory    | `True`                                    | leave out memories below Loom's floor; no documents when Loom abstains               |

A LangChain agent built with `create_agent` runs on LangGraph: pass `LoomStore` (below)
as its `store` for long-term memory.

The TypeScript retrievers extend `BaseRetriever` from `@langchain/core` (an optional peer
dependency, 1.x) and take the same options in camelCase, with the memory retriever's
user, session, run and agent in one `scope`:

```ts
import {
  LoomKnowledgeRetriever,
  LoomMemoryRetriever,
} from "@clickhouse/loom/integrations/langchain";

const docs = await new LoomKnowledgeRetriever(agent, { k: 3 }).invoke("refund policy");
const memories = await new LoomMemoryRetriever(agent, { scope: { userId: "alice" } }).invoke(
  "seat preference",
);
```

## LlamaIndex

`LoomRetriever` searches the namespace's documents (`knowledge.search`) as a LlamaIndex
`BaseRetriever`, for a query engine, a chat engine or a retriever tool. `LoomMemoryBlock`
is a long-term memory block for LlamaIndex's `Memory`. Install with
`pip install 'clickhouse-loom[llamaindex]'` (`llama-index-core` 0.14).

```python
from llama_index.core.memory import Memory
from loom import SyncAgent
from loom.integrations.llamaindex import LoomMemoryBlock, LoomRetriever

with SyncAgent(org="acme", namespace="support") as agent:
    nodes = LoomRetriever(agent, similarity_top_k=3).retrieve("refund policy")
    memory = Memory.from_defaults(
        session_id="s-42", memory_blocks=[LoomMemoryBlock(agent=agent, user_id="alice")]
    )
```

`retrieve` (and a sync query engine) needs a `SyncAgent`; `aretrieve` takes an `Agent` or
a `SyncAgent`. A node's text is the chunk, its id `<namespace>/<source_uri>#<chunk_idx>`,
its score the similarity, and its metadata `source_uri`, `chunk_idx`, `section` (when
the chunk has one), `namespace` and `tags`, of which only `source_uri` and `section` reach
the text a synthesizer sends the model. A query engine takes the retriever with
`RetrieverQueryEngine.from_args(LoomRetriever(agent), llm=...)` and the LLM you use.

| `LoomRetriever` argument | Default | Effect                                                                        |
| ------------------------ | ------- | ----------------------------------------------------------------------------- |
| `similarity_top_k`       | 4       | nodes per query                                                               |
| `tags`                   | none    | only chunks carrying any of these Loom tags                                   |
| `uri_prefix`             | none    | only documents whose `source_uri` starts with one of these                    |
| `namespace_scope`        | `this`  | `all_mine` adds the caller's other namespaces, up to the server's fan-out cap |
| `search_kwargs`          | none    | further `knowledge.search` fields, such as `search_mode` or `rerank`          |

`Memory` keeps the recent messages itself and hands the block the ones that leave its
short-term window, which by default is 70% of a 30,000-token limit; a shorter session hands
it nothing. Call `await aflush(memory)` (`flush(memory)` from sync code), both from
`loom.integrations.llamaindex`, when a session ends: like `Memory`'s own hand-over, it gives
what is still in the window to every block of that `Memory` and then archives it, so the
window starts empty. If a block fails, nothing is archived and the next flush hands the same
messages over again. The block's two calls:

- **`aput`** sends the user and assistant turns to `memory.set_from_messages` under the
  `Memory`'s session id; Loom extracts the facts worth keeping and resolves duplicates and
  conflicts with what it already holds. With `defer` (the default) the server queues the
  extraction, so a flush is one request and an extraction failure does not fail the turn.
- **`aget`** searches with the latest user message and returns the matching memories, one
  line each, which `Memory` places in the system message. It leaves out memories below
  Loom's floor, and returns nothing when Loom **abstains**.

| `LoomMemoryBlock` field | Default           | Effect                                                                                              |
| ----------------------- | ----------------- | --------------------------------------------------------------------------------------------------- |
| `agent`                 | required          | the `Agent` or `SyncAgent` the block reads and writes through                                       |
| `top_k`                 | 5                 | memories per read                                                                                   |
| `tags`                  | none              | stamped on extracted memories; reads keep memories carrying any of them                             |
| `user_id`               | none              | the end user the memories belong to; unset, reads every user's memories in the namespace            |
| `honour_abstain`        | `True`            | leave out memories below Loom's floor; nothing when Loom abstains                                   |
| `defer`                 | `True`            | queue the extraction on the server; `False` extracts inline whatever the operator's defer list says |
| `name`, `priority`      | `LoomMemories`, 0 | LlamaIndex's block name and truncation priority (0: never truncated)                                |

Give the block a `SyncAgent` when `Memory` is used through its sync methods (`get`, `put`)
or `flush` is called, which may run a call on another event loop than the one an `Agent`
was opened on; an `Agent` serves `aget` and `aput` on its own loop. `Memory` keeps its
short-term messages in its own chat store (in process by default); only what the block
receives reaches Loom.

## LangGraph

`LoomStore` (`sdk/python/loom/integrations/langgraph.py`) is a LangGraph `BaseStore`, so
every graph node reaches it through `get_store()`.

```python
from loom import Agent
from loom.integrations.langgraph import LoomStore

store = LoomStore(Agent(org="acme", namespace="support", agent="assistant"))
graph = builder.compile(store=store)  # nodes: store = get_store()

# inside a node
store.put(("users", user_id), "prefs", {"seat": "aisle"})
store.search(("users", user_id), query="seating preference", limit=3)

store.close()  # after the sync API: closes the agent and the loop the sync methods ran on
```

Use one store either through the sync methods or through the `a*` ones; the sync methods
run the agent on an event loop the store owns.

| LangGraph                                    | Loom                                                                                                                           |
| -------------------------------------------- | ------------------------------------------------------------------------------------------------------------------------------ |
| namespace `("users", "alice")` + key `prefs` | memory key `lg/users/alice/prefs`, tags `lg:users` and `lg:users/alice` (one per prefix, so a prefix search is one tag filter) |
| `put(value)`                                 | `memory.set`; the JSON object is the memory's value                                                                            |
| `put(..., index=False)`                      | `memory_type="episodic"`: kept, not ranked                                                                                     |
| `put(..., ttl=minutes)`                      | `ttl_seconds`                                                                                                                  |
| `search(query=…)`                            | `memory.search`, then one `memory.bulk_get` for the values; `SearchItem.score` is the similarity                               |
| `search()` without a query                   | `memory.list`, by recency                                                                                                      |
| `filter`                                     | applied to the stored value with LangGraph's operators (`$eq $ne $gt $gte $lt $lte`)                                           |
| `list_namespaces`                            | derived from the keys under `lg/`                                                                                              |

`LoomStore(agent, tags=[...], search_top_k_cap=50)` also stamps extra tags on every
write and caps the rows one ranked search asks for.

**Limits.** A ranked search asks for at most `search_top_k_cap` (50) rows, so
`offset + limit` past 50 returns fewer items. A search without a query and
`list_namespaces` page through `memory.list`; `filter` applies to each page. Without
`langgraph` installed, the module still imports (structural twins of `Item`,
`SearchItem` and the op types).

## Strands Agents

`LoomMemoryStore` implements the `MemoryStore` protocol (`add` / `search`) that the
Strands Memory Manager folds into the model input before each call.

```python
from loom import Agent
from loom.integrations.strands import LoomMemoryStore
from strands import Agent as StrandsAgent
from strands.memory import MemoryManager

async with Agent(org="acme", namespace="support", agent="assistant") as loom_agent:
    store = LoomMemoryStore(loom_agent, tags=["customer"])
    agent = StrandsAgent(memory_manager=MemoryManager(stores=[store], add_tool_config=True))
    agent("Which seat should I pick for my December trip?")
```

The TypeScript version behaves the same and does not depend on the Strands SDK:

```ts
import { Datapack } from "@clickhouse/loom";
import { LoomMemoryStore } from "@clickhouse/loom/integrations/strands";

const agent = new Datapack("acme", "support", { baseUrl, apiKey }).agent("assistant");
const store = new LoomMemoryStore(agent, { tags: ["customer"] });
// new MemoryManager({ stores: [store] })
```

- **`add`** writes `memory.set` under a content-addressed key,
  `<key_prefix>/<first 20 hex of sha256(content)>`, so the same fact added twice is one
  row and the server's supersession handles near-duplicates. Metadata values under `kind`,
  `source`, `topic` and `category` (strings up to 64 characters) become `key:value` tags.
- **`search`** runs `memory.search` limited to the store's tags and memory type. When Loom
  **abstains** (no hit above its calibrated floor), the store returns nothing rather than
  a weak match, so the Memory Manager injects no context at all; a hit below the floor
  in a response that does not abstain is left out too. Each entry's metadata carries
  `memory_key`, `memory_type`, `similarity`, `source` and `valid_at`.

| Option (Python / TypeScript)              | Default    | Effect                                                        |
| ----------------------------------------- | ---------- | ------------------------------------------------------------- |
| `memory_type` / `memoryType`              | `semantic` | the type every write carries and every search is limited to   |
| `tags`                                    | none       | stamped on every write; searches require them                 |
| `max_search_results` / `maxSearchResults` | 3          | results per search when the caller does not say               |
| `honour_abstain` / `honourAbstain`        | `true`     | leave out hits below Loom's floor; nothing when Loom abstains |
| `key_prefix` / `keyPrefix`                | `strands`  | key prefix                                                    |
| — / `scope`                               | none       | TypeScript only: a per-store user, session or run scope       |

## Agent orchestrators

A harness that runs several agents gives them shared, durable memory by mounting Loom's
MCP endpoint in each:

```yaml
mcp_servers:
  loom:
    type: streamable-http # some clients call it `http`
    url: ${LOOM_BASE_URL}/mcp
    headers:
      Authorization: Bearer ${LOOM_API_KEY}
```

- Agents in one namespace share everything. A sub-agent that should read a team
  namespace but write only its own gets a `viewer` grant on the team namespace
  ([tokens and grants](../../guides/tokens-and-grants.md)).
- Checkpoint task state with `working_memory.*` under a `run_id`; the next agent reloads
  it through `context.pack` ([trace and resume](../../guides/trace-and-resume.md)).
- Hand work between agents with the [task inbox](../tasks-and-decisions.md#task-inbox).

With the TypeScript SDK, `agentId` in a call's `Scope` overrides the agent name, so one
client can speak for several agents.

## Failure modes

- **Abstain is not an error.** An empty search means nothing cleared Loom's floor.
- **Write verdicts are hidden by the framework interface.** A `put` or `add` that lands as
  `conflicted` or `duplicate` returns normally; read the namespace's conflicts in the
  dashboard or with `conflicts.list` ([conflicts and refusal](../memory/conflicts-and-refusal.md)).
- **One store per event loop** for LangGraph's sync API: mixing sync and async calls on
  one store runs the agent on two loops.

## Related

- [Python SDK](../../reference/python-sdk.md) and [TypeScript SDK](../../reference/typescript-sdk.md).
- [Writing memory](../memory/writing.md) and [search and ask](../memory/search-and-ask.md).
- [Integrations overview](./index.md).
