# Agent frameworks and orchestrators

The SDKs ship adapters that present Loom through a framework's own memory or retrieval
interface. An adapter needs its framework: in Python, the SDK extra its section names
(`pip install 'clickhouse-loom[langchain]'` and so on); in TypeScript, the framework
package, an optional peer dependency. The LangGraph and Strands adapters import without
their framework. Identity comes from the Loom `Agent` the adapter wraps, and isolation is
Loom's namespace authorization. Every adapter calls your Loom server through the SDK: the
data is stored in your Loom deployment (ClickHouse, and Postgres for live memory under the
[Postgres memory authority](../../architecture/storage-authority.md)), and no adapter adds
or needs another database, vector store or cache.

| Framework                      | Adapter                                         | SDK                                                       | Framework interface                                                      |
| ------------------------------ | ----------------------------------------------- | --------------------------------------------------------- | ------------------------------------------------------------------------ |
| LangChain                      | `LoomKnowledgeRetriever`, `LoomMemoryRetriever` | Python, `loom.integrations.langchain`                     | `BaseRetriever`                                                          |
| LangChain.js                   | `LoomKnowledgeRetriever`, `LoomMemoryRetriever` | TypeScript, `@clickhouse/loom/integrations/langchain`     | `BaseRetriever` from `@langchain/core`                                   |
| LlamaIndex                     | `LoomRetriever`, `LoomMemoryBlock`              | Python, `loom.integrations.llamaindex`                    | `BaseRetriever`, `BaseMemoryBlock`                                       |
| OpenAI Agents SDK              | `loom_tools`, `LoomSession`                     | Python, `loom.integrations.openai_agents`                 | `FunctionTool`, the `Session` protocol                                   |
| OpenAI Agents SDK (TypeScript) | `loomTools`, `LoomSession`                      | TypeScript, `@clickhouse/loom/integrations/openai-agents` | `tool()` from `@openai/agents`, `Session`                                |
| CrewAI                         | `LoomCrewMemory`                                | Python, `loom.integrations.crewai`                        | `crewai.memory.Memory`                                                   |
| AutoGen                        | `LoomMemory`                                    | Python, `loom.integrations.autogen`                       | `autogen_core.memory.Memory`                                             |
| AG2                            | `LoomRecallPolicy`, `loom_tools`                | Python, `loom.integrations.ag2`                           | `ag2.assembly.AssemblyPolicy`, `tool()` from `ag2.tools`                 |
| Claude API memory tool         | `LoomMemoryTool`, `AsyncLoomMemoryTool`         | Python, `loom.integrations.claude_memory`                 | `BetaAbstractMemoryTool`, `BetaAsyncAbstractMemoryTool` from `anthropic` |
| Claude API memory tool         | `loomMemoryTool`                                | TypeScript, `@clickhouse/loom/integrations/claude-memory` | `betaMemoryTool` from `@anthropic-ai/sdk`                                |
| Microsoft Agent Framework      | `LoomContextProvider`                           | Python, `loom.integrations.agent_framework`               | `agent_framework.ContextProvider`                                        |
| Google ADK                     | `LoomMemoryService`                             | Python, `loom.integrations.google_adk`                    | `google.adk.memory.BaseMemoryService`                                    |
| Google ADK (TypeScript)        | `LoomMemoryService`                             | TypeScript, `@clickhouse/loom/integrations/google-adk`    | `BaseMemoryService` from `@google/adk`                                   |
| Pydantic AI                    | `loom_toolset`                                  | Python, `loom.integrations.pydantic_ai`                   | `FunctionToolset`                                                        |
| Mastra                         | `loomTools`, `LoomMemoryStorage`                | TypeScript, `@clickhouse/loom/integrations/mastra`        | `createTool`, `MemoryStorage` from `@mastra/core`                        |
| AI SDK                         | `loomTools`                                     | TypeScript, `@clickhouse/loom/integrations/ai-sdk`        | `tool()` from `ai`                                                       |
| n8n                            | Loom Chat Memory, Loom                          | `@clickhouse/n8n-nodes-loom`                              | AI Memory sub-node and action/tool node                                  |
| LangGraph                      | `LoomStore`                                     | Python, `loom.integrations.langgraph`                     | `BaseStore`                                                              |
| Strands Agents                 | `LoomMemoryStore`                               | Python, `loom.integrations.strands`                       | the `MemoryStore` protocol                                               |
| Strands Agents                 | `LoomMemoryStore`                               | TypeScript, `@clickhouse/loom/integrations/strands`       | the same protocol, structurally                                          |
| Pipecat                        | `LoomMemoryService`                             | Python, `loom.integrations.pipecat`                       | recall and capture `FrameProcessor`s                                     |
| Dify                           | Remember, Recall, Search knowledge              | `integrations/dify` standalone plugin                     | Native Dify tools                                                        |
| Haystack                       | `LoomDocumentStore`, `LoomRetriever`            | Python, `loom.integrations.haystack`                      | `DocumentStore` protocol, retrieval component                            |
| Agno                           | `LoomMemoryDb`                                  | Python, `loom.integrations.agno`                          | `BaseDb` user-memory methods                                             |
| Langflow                       | Loom                                            | `integrations/langflow` custom component                  | Langflow `Component`                                                     |

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

The Python retrievers retain their bound and ambient `SessionProtection` contributors at
construction and on each callback, including after client rebinding and across worker
threads. When the caller enters `family.automatic()`, retained protection also guards
automatic retrieval; without that context, direct retrieval and retriever tools stay
explicit and observe responses. A mirrored result marks all retained families before
documents return. A failed durable mark withholds the documents, and a marked family
blocks later automatic retrieval. Use separate retrievers for independent conversation
families.

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
from loom.integrations.llamaindex import LoomMemoryBlock, LoomRetriever, flush

with SyncAgent(org="acme", namespace="support") as agent:
    nodes = LoomRetriever(agent, similarity_top_k=3).retrieve("refund policy")
    memory = Memory.from_defaults(
        session_id="s-42", memory_blocks=[LoomMemoryBlock(agent=agent, user_id="alice")]
    )
    ...  # the session's turns go through memory.put / memory.get
    flush(memory)  # at the session's end: hand the rest of the window to the blocks
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
- **`aget`** searches the block's namespace, across every agent writing to it, with the
  latest user message and returns the matching memories, one line each, which `Memory`
  places in the system message. It leaves out memories below Loom's floor, and returns
  nothing when Loom **abstains**.

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

Wrap automatic callbacks in the application's `SessionProtection.automatic()` scope.
The retriever and memory block retain the families bound at construction and contributed
by earlier callbacks, including across client rebinding and sync worker threads. A
mirrored recall marks those families before returning. Later automatic calls refuse
protected work; explicit calls keep their response observers without becoming automatic.
`aflush` retains each Loom block's contributors before reading the native buffer. A
refused flush raises and leaves that window active for retry, without archiving it or
acknowledging an unsent extraction. The native framework owns its overflow archival;
`aflush` does not roll it back.

## Haystack

`LoomDocumentStore` implements Haystack 3.5's `DocumentStore` protocol, and
`LoomRetriever` is a native retrieval component over Loom knowledge search. Install
`pip install 'clickhouse-loom[haystack]'` (`haystack-ai>=3.5,<3.6`).

```python
from haystack import Document, Pipeline
from haystack.components.writers import DocumentWriter
from loom.integrations.haystack import LoomDocumentStore, LoomRetriever

store = LoomDocumentStore("acme", "support")
DocumentWriter(store).run(
    documents=[Document(id="travel", content="Boarding starts 30 minutes before departure.")]
)
pipeline = Pipeline()
pipeline.add_component("recall", LoomRetriever(store, top_k=5))
result = pipeline.run({"recall": {"query": "When does boarding start?"}})
```

Set `LOOM_BASE_URL` and `LOOM_API_KEY` for the deployment. The token is a Haystack
`Secret` referencing the environment by default, so `to_dict` and pipeline exports
carry the variable name. A token supplied with `Secret.from_token` works at runtime
but Haystack refuses to serialize it. The store accepts `base_url=`, `api_key=`,
`agent=` (default `haystack`) and `store=` (default `default`). Explicit endpoint URLs
cannot contain credentials, query strings or fragments.

Each store maps a document id to a hashed URI under its own `haystack://` prefix and
marks its rows with a store tag. Native document serialization preserves ids,
metadata, embeddings and binary fields in Loom's original-file bytes; text is indexed
for search. A document without text indexes its id. Data stays in the user's Loom
deployment, with no additional database, vector store or cache. Documents are shared
by the namespace; use separate namespaces and scoped tokens for untrusted apps.

The store implements count, full-document filtering, writes with Haystack duplicate
policies and deletion. `DuplicatePolicy.NONE` has the same behavior as `FAIL`.
Filters use Haystack's native nested comparison and logic predicates over the full
stored documents. Counting and filtering list and hydrate the store, so large stores
incur a full read. Missing ids raise `MissingDocumentError`; writes and batched
deletions can partially finish when a request fails. Concurrent writes can return a
conflict from Loom; duplicate checks are not a transaction across writers.

The retriever accepts `top_k` from 1 to 100 and `search_mode` of `hybrid` (default),
`semantic` or `lexical`. It searches only this store in this namespace, then hydrates
full documents and removes repeated chunk hits. Results are ranked by their first
matching chunk among at most 100 candidates; repeated chunks or concurrent deletions
can leave fewer than `top_k` documents. Use Haystack's native `FilterRetriever` over
the same store when filtering all documents instead of ranking a query.

Haystack's trusted-module policy also applies to serialized pipelines. Loading a
pipeline containing this adapter requires the application to trust the installed
`loom.integrations.haystack` module according to Haystack's configuration. The adapter
does not change that policy or register trust automatically. A pipeline constructed
directly from the imported component works as in the example.

## OpenAI Agents SDK

`loom_tools` returns Loom's agent verbs as the OpenAI Agents SDK's `FunctionTool`s, so the
model keeps and reads long-term memory in Loom during a run. Install with
`pip install 'clickhouse-loom[openai-agents]'` (`openai-agents` 0.24).

```python
from agents import Agent as OpenAIAgent, Runner
from loom import Agent
from loom.integrations.openai_agents import loom_tools

async with Agent(org="acme", namespace="support") as loom_agent:
    assistant = OpenAIAgent(
        name="assistant",
        instructions="Recall what you know about the user before answering.",
        tools=loom_tools(loom_agent, user_id="alice"),
    )
    result = await Runner.run(assistant, "Book my usual seat to Lisbon")
```

The tools are Loom's `AgentTools`
([sessions and tools](../../reference/python-sdk.md#sessions-and-tools)): `loom_remember`
(`memory.set`), `loom_recall` (`memory.search`), `loom_ask` (`memory.ask`), `loom_offload`
and `loom_fetch` (`context.offload`, `context.fetch`) and `loom_note`
(`working_memory.append`), with the same schemas and results in both SDKs. `user_id`,
`session_id` and `run_id` bind every call to one scope; `only=[...]` keeps a subset and
`max_result_chars` (8,000 by default) caps what a call returns (`loom_recall` leaves out
its lowest-ranked memories whole to fit, counted in `omitted`). The schemas have optional
parameters, so the tools are not strict. A call Loom refuses (a missing argument, a
rejected value, a server error) returns `<tool> failed: <message>` to the model, which can
correct the call; the run goes on.

`LoomSession` is the OpenAI Agents SDK's `Session` over Loom: a run given it reads and
extends the conversation history of one session there. Every process whose Loom agent has
the same name reads the same Python history for the same session id and user, and a
handoff between agents of one run keeps it. Python and TypeScript histories are separate
because the frameworks use different item formats.

Bind the application's existing protection family before constructing either
native session. The instance retains constructor and callback client families,
including after rebinding. Python also retains contributors active in the
callback's context. Use one instance per logical conversation family; restore it
with the same application-owned protection capsule. The native user and session
ids select history, without establishing protection authority. History reads and
additions are automatic work: a marked reply protects every contributor before
returning and suppresses later automatic calls. An initial complete history reply
may return; follow-up pages, hydration and batch writes stop. Explicit pop, clear
and model-selected tools remain available, with reply observation retained for the
session's manual deletions.

```python
from loom.integrations.openai_agents import LoomSession

session = LoomSession(loom_agent, "trip-42", user_id="alice")
result = await Runner.run(assistant, "Book my usual seat to Lisbon", session=session)
```

| `Session`       | Loom                                                                                                                                                                                                                                                          |
| --------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `get_items`     | `memory.list` of the session's items, sorted by writer-generated key; `limit` (or `session_settings.limit`) keeps the latest after scanning the history; an item the list cuts at 2,048 characters, or sends without a value, is read whole with `memory.get` |
| `add_items`     | one `memory.set` per item, an `episodic` memory under `<key_prefix>/<nanoseconds>-<random>`, the item's JSON its value                                                                                                                                        |
| `pop_item`      | the latest item, read as `get_items(limit=1)` reads it, deleted with `memory.delete` and returned                                                                                                                                                             |
| `clear_session` | `memory.delete` of every item of the session                                                                                                                                                                                                                  |

An `episodic` memory is not embedded, so an item is never merged with or judged against
another row, and a repeated item stays two items (one `memory.set` per item, bypassing SDK
coalescing: a batch would fold a value identical to a live row). A read fails if an item
disappears before its full value can be fetched. Items are stored under a Loom session derived from
the user and the session id (`oai-py-` and a digest of the pair in Python, `oai-ts-`
in TypeScript). Keys follow writer clocks; independent writers do not share a server
sequence. Reads, `pop_item` and `clear_session` scan the whole history: server arrival
times cannot bound keys from different writer clocks. The history is the user's:
unset, `user_id` writes the namespace's shared
rows; an empty or `-` user raises `ValueError`. To keep what a conversation
established beyond its history, give the model `loom_remember`, or send the turns to
`memory.set_from_messages`.

| `LoomSession` argument | Default         | Effect                                                          |
| ---------------------- | --------------- | --------------------------------------------------------------- |
| `agent`                | required        | the `loom.Agent` the session calls                              |
| `session_id`           | required        | the session's id                                                |
| `user_id`              | none            | the end user the history belongs to                             |
| `key_prefix`           | `openai-agents` | the items' key prefix                                           |
| `session_settings`     | the SDK's       | `limit`: the default number of latest items `get_items` returns |

In TypeScript, `loomTools(agent, scope)` from `@clickhouse/loom/integrations/openai-agents`
returns the same tools built with `tool()` from `@openai/agents`, an optional peer
dependency:

```ts
import { Agent as OpenAIAgent, run } from "@openai/agents";
import { loomTools } from "@clickhouse/loom/integrations/openai-agents";

const assistant = new OpenAIAgent({
  name: "assistant",
  tools: loomTools(agent, { userId: "alice" }),
});
const result = await run(assistant, "Book my usual seat to Lisbon");
```

`new LoomSession(agent, sessionId, { userId, keyPrefix })` is the TypeScript `Session`, over
its native item format and separate session rows. The user is fixed when the session is
constructed. Binary image and file tool results keep their bytes and media information
when stored and read back:

```ts
import { LoomSession } from "@clickhouse/loom/integrations/openai-agents";

const session = new LoomSession(agent, "trip-42", { userId: "alice" });
const result = await run(assistant, "Book my usual seat to Lisbon", { session });
```

## CrewAI

`LoomCrewMemory` is a `crewai.memory.Memory`, so a crew takes it as `memory=` and its
agents share the namespace's memory. Set it on the `Crew`, not on an `Agent`: when CrewAI
copies a crew (`kickoff_for_each`, `train`, `test`) it rebuilds each agent's memory as its
own `Memory`, with a local store, and keeps only the crew's. Install with
`pip install 'clickhouse-loom[crewai]'` (`crewai` 1.15, Python 3.11 to 3.13).

```python
from crewai import Crew
from loom import SyncAgent
from loom.integrations.crewai import LoomCrewMemory

with SyncAgent(org="acme", namespace="research") as agent:
    crew = Crew(agents=[...], tasks=[...], memory=LoomCrewMemory(agent=agent))
    crew.kickoff()
```

Use one `LoomCrewMemory` per conversation, bound to that conversation's logical session
family. Constructor and callback contributors remain bound after client rebinding,
including contributors seen by `extract_memories`. Inside an application-owned automatic
scope, protected contributors refuse recall and capture before HTTP. Recalled protection
is observed before matches return to the crew. Native memory tools called outside an
automatic scope remain explicit. `user_id` and the native `private` flag do not select
session protection.

| A crew run asks memory to                               | Loom                                                                                                                 |
| ------------------------------------------------------- | -------------------------------------------------------------------------------------------------------------------- |
| recall what bears on a task (`recall`)                  | `memory.search` of the namespace the crew writes to; nothing when Loom abstains, `score` is the similarity           |
| extract facts from a task's output (`extract_memories`) | the output, unchanged: Loom extracts on write                                                                        |
| save them (`remember_many`)                             | `memory.set_from_messages`, deferred by default; Loom resolves duplicates and conflicts                              |
| save one fact, from the memory tools (`remember`)       | one `memory.set` under `crewai/<first 20 hex of sha256(text)>`; a save of several items goes through `remember_many` |

CrewAI's own `Memory` analyses every save with an LLM, embeds it with its embedder and
keeps it in a local store; `LoomCrewMemory` needs none of them, since Loom embeds and
extracts on the server. `tags` and the `categories` of a save become Loom tags; a recall
keeps memories carrying any of `tags` and any of its `categories`; when both are given,
the categories narrow the best `5 × limit` matches (at least 50, at most 500), and only
fact hits carrying one of them are kept. The scope tree, slices, record listing, `update`
and private records (`private=True`) are not offered and raise, and so do `reset` and
`forget`: Loom deletes a memory when its key is named (`memory.delete`) or through
[erasure](../memory/retention-and-erasure.md). `scope`, `root_scope`, `metadata`,
`importance` and `source` are not kept or used, and a `read_only` memory saves nothing.
Crews call their memory synchronously, so the adapter takes a `SyncAgent`. Do not
checkpoint a crew that holds this memory: CrewAI saves the memory as its own `Memory` and
restores that, with a local store.

| Field            | Default  | Effect                                                                                          |
| ---------------- | -------- | ----------------------------------------------------------------------------------------------- |
| `agent`          | required | the `SyncAgent` the memory calls                                                                |
| `user_id`        | none     | the end user the memories belong to; unset, recalls read every user's memories in the namespace |
| `tags`           | none     | stamped on every save; recalls keep memories carrying any of them                               |
| `honour_abstain` | `True`   | leave out memories below Loom's floor; nothing when Loom abstains                               |
| `defer`          | `True`   | queue the extraction on the server instead of waiting                                           |
| `key_prefix`     | `crewai` | key prefix of `remember`'s saves                                                                |

## AutoGen

`LoomMemory` implements AutoGen's `Memory` protocol (`autogen_core.memory`), which an
`AssistantAgent` takes as `memory=[...]`. Install with
`pip install 'clickhouse-loom[autogen]' autogen-agentchat` (`autogen-core` 0.7;
`AssistantAgent` comes from `autogen-agentchat`, which the extra does not install).

```python
from autogen_agentchat.agents import AssistantAgent
from autogen_core.memory import MemoryContent, MemoryMimeType
from loom import Agent
from loom.integrations.autogen import LoomMemory

async with Agent(org="acme", namespace="support") as loom_agent:
    memory = LoomMemory(loom_agent, user_id="alice")
    await memory.add(
        MemoryContent(content="Alice prefers aisle seats", mime_type=MemoryMimeType.TEXT)
    )
    assistant = AssistantAgent("assistant", model_client=model_client, memory=[memory])
```

| AutoGen          | Loom                                                                                                                                                                                                                                                                        |
| ---------------- | --------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `add`            | one `memory.set` under `autogen/<first 20 hex of sha256(text)>` of the stripped text; text and JSON content, not bytes or images; `metadata` and `mime_type` are not kept                                                                                                   |
| `query`          | `memory.search` of the namespace it writes to; with `honour_abstain`, leaves out memories below Loom's floor and returns nothing when Loom abstains; each result's metadata carries `memory_key`, `memory_type`, `similarity`, `source` and `valid_at` when the row has one |
| `update_context` | once per turn, before the agent's first model call: `query` with the latest user message (its text parts when multimodal), the matches added as one system message                                                                                                          |
| `clear`          | raises: Loom deletes a memory when its key is named (`memory.delete`) or through [erasure](../memory/retention-and-erasure.md)                                                                                                                                              |

| `LoomMemory` argument | Default   | Effect                                                                                    |
| --------------------- | --------- | ----------------------------------------------------------------------------------------- |
| `agent`               | required  | the `loom.Agent` the memory calls; AutoGen awaits its memory, so a `SyncAgent` is refused |
| `user_id`             | none      | the end user the memories belong to; unset, a `query` reads every user's memories         |
| `tags`                | none      | stamped on every `add`; a `query` keeps memories carrying any of them                     |
| `top_k`               | 5         | memories per `query`, at least 1 and capped at 500                                        |
| `honour_abstain`      | `True`    | leave out memories below Loom's floor; nothing when Loom abstains                         |
| `key_prefix`          | `autogen` | the prefix of `add`'s keys                                                                |

An `AssistantAgent` holding a `LoomMemory` cannot be dumped with `dump_component`: the Loom
agent and its credential are not component config.

Bind one memory instance per conversation to its application-owned `SessionProtection`
family. Constructor and callback contributors remain bound across context exit and
client rebinding. `update_context` snapshots them before awaiting native messages and
runs as automatic work. A marked recall is durably observed before system-message
injection; later automatic work stops. Direct `query` and `add` remain explicit unless
their caller owns automatic work, while their responses still observe known families.
Native user and session ids do not select protection.

## AG2

An AG2 1.3 agent gets context through assembly policies, which run before each model call,
and acts through tools. `LoomRecallPolicy` is a policy that adds the Loom memories matching
the latest user message to the prompt, and `loom_tools` returns Loom's agent verbs as AG2
tools, the same `AgentTools` as the [OpenAI Agents SDK](#openai-agents-sdk) adapter, so the
model writes memories with `loom_remember`. Install with
`pip install --upgrade --pre 'clickhouse-loom[ag2]'` (`ag2` 1.3).

```python
from ag2 import Agent as AG2Agent, Middleware
from ag2.assembly import AssemblerMiddleware
from loom import Agent
from loom.integrations.ag2 import LoomRecallPolicy, loom_tools

async with Agent(org="acme", namespace="support") as loom_agent:
    recall = LoomRecallPolicy(loom_agent, user_id="alice")
    assistant = AG2Agent(
        "assistant",
        config=config,
        tools=loom_tools(loom_agent, user_id="alice"),
        middleware=[Middleware(AssemblerMiddleware, policies=[recall])],
    )
    reply = await assistant.ask("Book my usual seat to Lisbon")
```

| `LoomRecallPolicy` argument | Default  | Effect                                                                                               |
| --------------------------- | -------- | ---------------------------------------------------------------------------------------------------- |
| `agent`                     | required | the `loom.Agent` the policy searches with; the policy awaits its search, so a `SyncAgent` is refused |
| `user_id`                   | none     | the end user whose memories to read; unset, every user's memories in the namespace                   |
| `tags`                      | none     | keep memories carrying any of them                                                                   |
| `top_k`                     | 5        | memories per model call, at least 1 and capped at 500                                                |
| `honour_abstain`            | `True`   | leave out memories below Loom's floor; nothing when Loom abstains                                    |

The policy runs `memory.search` of the namespace it is bound to before each model call of
a turn, with the text of the latest request a user sent (a framework inbox message is not
one), and adds the matches as one prompt block, or nothing. It only reads; the model
writes through `loom_remember`. `loom_tools` binds `user_id`, `session_id` and `run_id` to
every call and takes `only=[...]` and `max_result_chars` (8,000 by default; `loom_recall`
leaves out its lowest-ranked memories whole to fit, counted in `omitted`). A call Loom
refuses is a tool error whose text is `<tool> failed: <message>`, in place of the
traceback AG2 sends for an exception, and the run goes on. Put the policy before a
trimming policy (`SlidingWindowPolicy`, `TokenBudgetPolicy`), which can cut the user's
request out of the events the policy reads. Those policies trim events, not the prompt, so
`top_k` is what bounds the memory block.

Use one recall policy per conversation, with its Loom client bound to the
application-owned `SessionProtection` family. The policy retains constructor and
callback contributors across context exit and client rebinding. Native user and session
ids do not select protection. A protected contributor stops automatic recall with
`AutomaticCaptureSuppressed` before dispatch. A marked recall is durably observed before
its content reaches the current prompt; later automatic recalls stop. Model-selected
`loom_tools` calls remain explicit operations.

Find more projects using AG2 in [Build with AG2](https://github.com/ag2ai/build-with-ag2).

## Microsoft Agent Framework

`LoomContextProvider` implements Agent Framework's `ContextProvider`, which an `Agent`
takes in `context_providers=[...]`: before each run it adds the Loom memories that match
the user's input to the run's instructions, and after the run it sends the turn's text to
Loom, which extracts the facts. Install with `pip install 'clickhouse-loom[agent-framework]'`
(`agent-framework-core` 1.26.x). Agent Framework succeeds AutoGen; a new project
uses this provider, and the [AutoGen](#autogen) memory stays for AutoGen projects.

```python
from agent_framework import Agent
from loom import Agent as LoomAgent
from loom.integrations.agent_framework import LoomContextProvider

async with LoomAgent(org="acme", namespace="support") as loom_agent:
    agent = Agent(
        client=chat_client,
        instructions="You are a helpful assistant.",
        context_providers=[LoomContextProvider(loom_agent, user_id="alice")],
    )
    session = agent.create_session()
    await agent.run("I prefer aisle seats", session=session)
```

The agent keeps its own conversation history: for a run given a session, with no history
provider configured and a client that does not store the conversation itself, it adds its
in-memory one beside Loom's provider. Loom holds the memories across sessions. A caller
that passes the whole conversation to each run sends Loom only the turn after its last
assistant message. Inside an `AgentLoopMiddleware` loop, the turn is sent once, when the loop
ends, without the loop's own prompts: the user's input and every reply, or only the last reply
when the loop streams or returns its final response only. Each iteration recalls for its own
input, so after the first the memories match the loop's prompt, not the user's request.

| Agent Framework | Loom                                                                                                                                                                                                                                                         |
| --------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------ |
| `before_run`    | `memory.search` with the run's latest user message; with `honour_abstain`, leaves out memories below Loom's floor and adds nothing when Loom abstains; the matches are added as one block of instructions                                                    |
| `after_run`     | the user text of the run's new turn and the assistant text of its response, sent to `memory.set_from_messages` (400 messages a call, the op's cap), where Loom extracts the facts and resolves duplicates and conflicts; tool calls and results are not sent |

Extraction runs under a Loom session derived from the user and the Agent Framework session
(`maf-` and a digest of the pair): Loom files a turn's verbatim chunks by session alone, and
a caller may choose its session ids.

With an application-owned `SessionProtection` bound to the Loom agent, use one
provider per logical conversation family. The provider retains constructor and callback
contributors, including ambient automatic contributors, across callbacks and client
rebinding. Native user and session ids provide routing, not protection authority. The
application reopens the same durable capsule when restoring a family.

A first marked recall can reach the model after durable protection. Later automatic
recall and extraction raise `AutomaticCaptureSuppressed`; a marked extraction response
stops later batches. Suppression after the model answers fails the run without undoing
that answer or any tool effects.

| `LoomContextProvider` argument | Default  | Effect                                                                                                                                                                                      |
| ------------------------------ | -------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `agent`                        | required | the `loom.Agent` the provider calls; Agent Framework awaits its providers, so a `SyncAgent` is refused                                                                                      |
| `user_id`                      | none     | the end user the memories belong to; unset, recall reads all namespace memories the token permits and extraction writes shared rows; empty, `-` or whitespace-padded ids raise `ValueError` |
| `source_id`                    | `loom`   | the provider's id in Agent Framework's attribution                                                                                                                                          |
| `top_k`                        | 5        | memories per run, from 1 to 500                                                                                                                                                             |
| `honour_abstain`               | `True`   | leave out memories below Loom's floor; nothing when Loom abstains                                                                                                                           |
| `defer`                        | `True`   | queue the extraction on the server instead of waiting                                                                                                                                       |

## Claude API memory tool

The Claude API memory tool (`memory_20250818`) lets Claude keep files under `/memories`
across conversations, and the application runs its commands. `AsyncLoomMemoryTool` (over a
`loom.Agent`) and `LoomMemoryTool` (over a `loom.SyncAgent`) subclass the `anthropic` SDK's
memory tool bases and run those commands against Loom: each file is a Loom document, so
every agent of the namespace reads the same files, `knowledge.search` finds what they say,
and the files stay in the Loom deployment. Install with
`pip install 'clickhouse-loom[claude-memory]'` (`anthropic` 2.11.x).

```python
import anthropic
from loom import Agent
from loom.integrations.claude_memory import AsyncLoomMemoryTool

client = anthropic.AsyncAnthropic()
async with Agent(org="acme", namespace="support") as loom_agent:
    memory = AsyncLoomMemoryTool(loom_agent, store="alice")
    runner = client.beta.messages.tool_runner(
        model="claude-opus-5-5",
        max_tokens=16000,
        tools=[memory],
        messages=[{"role": "user", "content": "Remember that I prefer aisle seats"}],
    )
    async for message in runner:
        ...
```

In TypeScript, `loomMemoryTool(agent, { store }, extras)` returns the SDK's `betaMemoryTool`
(with `extras` such as `cache_control`) over the same documents (`@anthropic-ai/sdk` is an
optional peer dependency, 1.8.x):

```ts
import { loomMemoryTool } from "@clickhouse/loom/integrations/claude-memory";

const memory = loomMemoryTool(agent, { store: "alice" });
// client.beta.messages.toolRunner({ model, max_tokens, tools: [memory], messages })
```

`loomMemoryHandlers(agent, { store })` returns the six command handlers alone, for a loop that
dispatches the tool's calls itself.

A file `/memories/<path>` is the document `loom-memory://<store>/memories/<path>`, tagged
`claude-memory:<store>`; directories are the paths' prefixes. Both SDKs read and write the
same documents and answer each command with the same text: the text the Python SDK's local
memory tool answers, except that a directory view lists paths without sizes and that two
refusals are Loom's own (an empty `old_str`, and a path under a file).

| Command       | Loom                                                                                                                                                                              |
| ------------- | --------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `view`        | a file: `knowledge.get`, its lines numbered, `view_range` honoured; a directory: `knowledge.list` of the store's tag, its files and directories two levels deep                   |
| `create`      | `knowledge.upsert` of the text; refused when the file or a directory of that path exists                                                                                          |
| `str_replace` | `knowledge.get`, then `knowledge.upsert` with the one occurrence replaced; refused when `old_str` is empty, does not appear, or appears more than once                            |
| `insert`      | `knowledge.get`, then `knowledge.upsert` with the line inserted at `insert_line`                                                                                                  |
| `delete`      | `knowledge.bulk_delete` of the file, or of every file under the directory, 500 a call                                                                                             |
| `rename`      | each file under the old path copied to the new one, then `knowledge.bulk_delete` of the old; refused when the destination exists; a failure partway leaves the copies made so far |

Every path must be `/memories` or under it, after `.` and `..` are resolved; any other path
is refused before Loom is called. A refusal is a `ToolError`, which the tool runner returns
to Claude as an error result. An edit reads the file and writes it back, so two agents that
edit one file at once keep the later write, as with the SDK's local memory tool; a
directory view, delete or rename that pages while another agent rewrites a file under it
can miss that file.

| Argument | Default   | Effect                                                                                                 |
| -------- | --------- | ------------------------------------------------------------------------------------------------------ |
| `agent`  | required  | a `loom.Agent` for `AsyncLoomMemoryTool` and `loomMemoryTool`, a `loom.SyncAgent` for `LoomMemoryTool` |
| `store`  | `default` | the file tree, one path segment: one per application, or per user                                      |

A store is not a boundary: every reader of the namespace can read every store's documents.
Give each user their own namespace when users must not read each other's files.

## Google ADK

`LoomMemoryService` implements ADK's `BaseMemoryService`, which a `Runner` takes as
`memory_service=`. ADK's own memory tools read it: `preload_memory` searches with the
user's message before each model call and adds the matches to the prompt, and
`load_memory` lets the model search. A callback saves each turn. Install with
`pip install 'clickhouse-loom[google-adk]'` (`google-adk` 2.14.x).

```python
from google.adk.agents import LlmAgent
from google.adk.runners import Runner
from google.adk.sessions import InMemorySessionService
from google.adk.tools import preload_memory
from google.genai import types
from loom import Agent
from loom.integrations.google_adk import LoomMemoryService


async def save_turn(ctx):
    turn = [e for e in ctx.session.events if e.invocation_id == ctx.invocation_id]
    await ctx.add_events_to_memory(events=turn)


async with Agent(org="acme", namespace="support") as loom_agent:
    sessions = InMemorySessionService()
    runner = Runner(
        app_name="support",
        agent=LlmAgent(
            name="assistant",
            model="gemini-flash-latest",
            tools=[preload_memory],
            after_agent_callback=save_turn,
        ),
        session_service=sessions,
        memory_service=LoomMemoryService(loom_agent, app_name="support"),
    )
    session = await sessions.create_session(app_name="support", user_id="alice")
    message = types.Content(role="user", parts=[types.Part(text="I prefer aisle seats")])
    async for event in runner.run_async(
        user_id="alice", session_id=session.id, new_message=message
    ):
        ...
```

ADK scopes memory by app and user. Loom's authorization boundary is the namespace:
a write can merge with another agent's row for the same user, so apps sharing a
namespace also share that user's memory context. A service therefore serves one ADK
app, named by `app_name`, in the namespace of its Loom agent, and refuses a call for
another app; give each app its own namespace and service. The ADK user is the Loom
`user_id`; empty, `-`, `*` and whitespace-padded ids raise `ValueError`.
Extraction runs under a Loom session derived from the user and the ADK session (`adk-`
and a digest of the pair): Loom files a turn's verbatim chunks by session alone, and ADK
makes a session id unique per user only. A search reads all of the user's memories in the
namespace, from every session. Remaining result slots hold up to 100 knowledge documents shared by
the app namespace; documents carry no fact-deletion id.

The Python service retains the application-owned `SessionProtection` families present
at construction and method entry. Use one service per logical session family; native
app, user and session ids do not select protection. Inside `family.automatic()`, a
protected contributor suppresses further content requests, including later extraction
batches and the optional knowledge search. If that second search is suppressed after
a marked memory result, the callback raises `AutomaticCaptureSuppressed` and returns
no memory response. Calls outside automatic context remain explicit and observe
responses through the retained families. Exact deletion remains a reference operation.

| ADK                     | Loom                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                  |
| ----------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `add_session_to_memory` | the text of the session's events, without the ones a rewind took back, sent to `memory.set_from_messages` (400 messages a call, the op's cap); Loom extracts the facts and resolves duplicates and conflicts. A session added again is extracted again, so a callback that runs every turn, as `after_agent_callback` does, saves that turn's events, as in the example                                                                                                                                                               |
| `add_events_to_memory`  | the same for the events given, such as the latest turn; a call without a `session_id` raises `ValueError`, since Loom files a turn's verbatim chunks by session. A turn saved before a later rewind keeps its memories: the app deletes its facts by the ids a search returns, and its verbatim passages stay until [erasure](../memory/retention-and-erasure.md) removes them                                                                                                                                                        |
| `add_memory`            | one `memory.set` per entry with text (an entry without text is skipped) under `<key_prefix>/<first 20 hex of sha256(user and text)>`, its `timestamp` as the memory's `valid_at` when it is ISO 8601 (one without a zone is read as local time, as ADK writes its own)                                                                                                                                                                                                                                                                |
| `search_memory`         | `memory.search` of the user's memories, followed by `knowledge.search` for remaining slots; with `honour_abstain`, leaves out memories below Loom's floor and abstaining memory results; each entry's `custom_metadata` carries `similarity` and `source`, and its `timestamp` is `valid_at` when the row has one                                                                                                                                                                                                                     |
| `delete_memory`         | the entry id carries its key, session, writer and searched version. `memory.delete_version` deletes that exact row only; a rewritten or missing version is a no-op. Passages, summaries, knowledge hits and facts without writer/version metadata have no deletion id. Use [erasure](../memory/retention-and-erasure.md) for their content. A call without an id does nothing. Adding the session again can extract the fact again; [`blacklist.add`](../memory/conflicts-and-refusal.md#refusal-the-blacklist) refuses future writes |

Events keep their text parts only: the model's thoughts and tool calls and results are not
sent, and neither is `custom_metadata` or an entry's `author` and `id`.

| `LoomMemoryService` argument | Default  | Effect                                                                                         |
| ---------------------------- | -------- | ---------------------------------------------------------------------------------------------- |
| `agent`                      | required | the `loom.Agent` the service calls; ADK awaits its memory service, so a `SyncAgent` is refused |
| `app_name`                   | required | the ADK app the service serves; a call for another app raises `ValueError`                     |
| `top_k`                      | 10       | memories per search, from 1 to 500                                                             |
| `honour_abstain`             | `True`   | filter the memory leg below Loom's floor or on abstention; knowledge remains available         |
| `defer`                      | `True`   | queue the extraction on the server instead of waiting for it                                   |
| `key_prefix`                 | `adk`    | the prefix of `add_memory`'s keys; an empty one is refused                                     |

The TypeScript service implements `@google/adk` 2.5's `BaseMemoryService` interface
(`npm install @google/adk@~2.5`). It exposes `addSessionToMemory` and `searchMemory`;
that interface has no individual-memory deletion or event-append method. Both SDKs
use the same derived Loom session for the same user and ADK session id. TypeScript
options are `appName`, `topK`, `honourAbstain` and `defer`, with the Python defaults.

```ts
import { InMemorySessionService, LlmAgent, PRELOAD_MEMORY, Runner } from "@google/adk";
import { LoomMemoryService } from "@clickhouse/loom/integrations/google-adk";

const sessions = new InMemorySessionService();
const memory = new LoomMemoryService(loomAgent, { appName: "support" });
const runner = new Runner({
  appName: "support",
  agent: new LlmAgent({ name: "assistant", model: "gemini-flash-latest", tools: [PRELOAD_MEMORY] }),
  sessionService: sessions,
  memoryService: memory,
  autoCreateSession: true,
});
for await (const event of runner.runAsync({
  userId: "alice",
  sessionId: "travel",
  newMessage: { role: "user", parts: [{ text: "I prefer aisle seats" }] },
})) {
  /* consume the turn */
}
const session = await sessions.getSession({
  appName: "support",
  userId: "alice",
  sessionId: "travel",
});
if (session) await memory.addSessionToMemory(session);
```

Save a completed TypeScript session once. Saving it again extracts it again; the
TypeScript interface has no per-turn append operation. ADK's in-memory session
service in these examples holds the running conversation only; Loom stores the
extracted memories and searchable documents in the user's deployment.

## Agno

`LoomMemoryDb` implements Agno 3.2's `BaseDb` user-memory methods for one user and
store. Install `pip install 'clickhouse-loom[agno]'` (`agno>=3.2,<3.3`). Pass it to a
`MemoryManager`; configure the agent's session database separately if needed.

```python
from agno.db.schemas.memory import UserMemory
from agno.memory import MemoryManager
from loom import SyncAgent
from loom.integrations.agno import LoomMemoryDb

with SyncAgent(org="acme", namespace="support", agent="assistant") as loom_agent:
    manager = MemoryManager(db=LoomMemoryDb(loom_agent, user_id="alice"))
    manager.add_user_memory(UserMemory(memory="Prefers aisle seats"), user_id="alice")
    memories = manager.get_user_memories(user_id="alice")
```

The database stores each native memory, including its id, topics, agent/team attribution,
input, feedback and timestamps, as an episodic Loom row. This keeps native identifiers
and updates intact without fact extraction or semantic duplicate merging. The native
JSON payload follows Loom's sensitivity classification; epoch timestamps can receive
a contact-detail label. Rows stay in
the user's Loom deployment; the adapter adds no database, cache or vector store.

Use one `LoomMemoryDb` per end user. An omitted user in a database method means its
bound user; a different user is refused. Agno's manager defaults an omitted user to
`default`, so pass the user to manager calls as in the example. Optional `store=`
(default `default`) separates applications sharing that user. Keys and the derived
Loom session include both user and store digests. Separate namespaces and restricted
tokens remain the boundary for mutually untrusted applications.

Bind one database per conversation to its application-owned `SessionProtection`
family. Constructor and callback contributors remain bound across context exit and
client rebinding. Automatic invocations join those retained contributors, so a protected
family stops later automatic reads and writes before dispatch. Explicit database calls
remain explicit after the automatic invocation ends, and every response still observes
the known families. Native user and store ids do not select protection.

The database implements single/bulk upsert, get, list, content/topic/agent/team filters,
sorting, pagination, topic listing, statistics and deletion. Reads hydrate full values
and page through the bound store. Filters and sorting run over those rows locally;
large stores therefore incur a full read. `clear_memories` clears only this bound store
and user. Concurrent writes of one native id keep the later write; a bulk write or
clear can partially finish when a request fails. Other `BaseDb` capabilities, including
sessions, tracing and evaluation storage, raise `NotImplementedError`.

Agno's memory manager can make model calls when it extracts or edits memories. This
adapter makes no model call itself; its tests supply native memory objects directly.

## Pydantic AI

Pydantic AI extends an agent through tools and toolsets. `loom_toolset` returns Loom's
agent verbs as a `FunctionToolset`, the same `AgentTools` as the
[OpenAI Agents SDK](#openai-agents-sdk) adapter. Install with
`pip install 'clickhouse-loom[pydantic-ai]' 'pydantic-ai-slim[anthropic]'` (`pydantic-ai-slim`
2.68 or a later 2.x, plus the extra for your model's provider).

```python
from loom import Agent
from loom.integrations.pydantic_ai import loom_toolset
from pydantic_ai import Agent as PydanticAgent

async with Agent(org="acme", namespace="support") as loom_agent:
    assistant = PydanticAgent(
        "anthropic:claude-sonnet-5-5",
        instructions="Recall what you know about the user before answering.",
        toolsets=[loom_toolset(loom_agent, user_id="alice")],
    )
    result = await assistant.run("Book my usual seat to Lisbon")
```

`user_id`, `session_id` and `run_id` bind every call to one scope; `only=[...]` keeps a
subset and `max_result_chars` (8,000 by default) caps what a call returns (`loom_recall`
leaves out its lowest-ranked memories whole to fit, counted in `omitted`). A call Loom
refuses raises Pydantic AI's `ToolFailed` with `<tool> failed: <message>`: the model reads
it as a failed tool result and the run goes on, where any other exception would fail the
run. The toolset's tools await the Loom agent's calls, so it takes a `loom.Agent` and
refuses a `SyncAgent`. Message history stays with the caller, who passes it to each run as
`message_history`.

## Mastra

`loomTools(agent, scope)` returns Loom's agent verbs as Mastra tools built with
`createTool`, keyed by name as a Mastra `Agent` takes them: the same `AgentTools` as the
[OpenAI Agents SDK](#openai-agents-sdk) adapter. `@mastra/core` 1.83.x is an
optional peer dependency, and needs Node 22.13 or later.

```ts
import { Agent as MastraAgent } from "@mastra/core/agent";
import { loomTools } from "@clickhouse/loom/integrations/mastra";

const assistant = new MastraAgent({
  id: "assistant",
  name: "assistant",
  instructions: "Recall what you know about the user before answering.",
  model,
  tools: loomTools(agent, { userId: "alice" }),
});
```

`LoomMemoryStorage` implements the native `MemoryStorage` domain. It keeps mutable
threads, complete message payloads and resource working memory in Loom through the SDK.
Install `@mastra/core` 1.83.x and `@mastra/memory` alongside `@clickhouse/loom`.

For automatic native memory, the app supplies `conversationCapture`, a `SessionCapture`
restored for the complete logical conversation family:

```ts
import { MastraCompositeStore } from "@mastra/core/storage";
import { Memory } from "@mastra/memory";
import { LoomMemoryStorage } from "@clickhouse/loom/integrations/mastra";

const memory = new Memory({
  storage: new MastraCompositeStore({
    id: "loom",
    domains: {
      memory: new LoomMemoryStorage(agent, {
        resourceId: "alice",
        storeId: "assistant",
        capture: conversationCapture,
      }),
    },
  }),
  options: {
    semanticRecall: false,
    observationalMemory: false,
    workingMemory: { enabled: true, scope: "resource" },
  },
});
```

Construct one storage and `Memory` instance per authenticated end-user. The canonical
`resourceId` is the actual Loom `user_id` on every thread, message and resource row, so
[user erasure](../memory/retention-and-erasure.md) covers them together. Reads without a
resource argument remain scoped to that bound user. Cross-resource writes are refused;
cross-resource reads return missing or empty results. Keep the same Loom agent and
`storeId` across processes to resume the same data. A store must not be shared across
users, and resource reassignment is unsupported.

With `capture`, construct a separate storage and `Memory` instance for each complete
conversation family. Keep its durable protection state across reconnects and restarts;
resource, store and thread IDs identify rows and do not restore protection authority.
The adapter retains the supplied capture and the client's contributing families before
asynchronous work. A marked recall returns only after every contributing family commits
protection. Later automatic callbacks raise `AutomaticCaptureSuppressed`, including
batch continuation, conditional-update retries and pending thread timestamp updates.
A marked write also refuses the callback, so a failed batch can leave earlier writes
stored. The refusal propagates through native `Memory` methods.

Omit `capture` only for explicitly owned CRUD. Those calls retain response observers
without becoming automatic. Model-selected `loomTools` calls remain explicit. The
adapter does not supply durable protection storage or create a conversation family.

Native listing supports thread/resource filters, date and metadata filters, ordering,
pagination and included message context across that user's threads. Lists scan the
store's scoped rows and retrieve full values by key before filtering; large histories
cost multiple HTTP calls. Partial updates use Loom's hash precondition and retry a
conflict up to three times. A missing resource is created with
`memory.set.if_match_sha256="absent"`; this requires a server that supports the absence
precondition. Older servers return `PRECONDITION_FAILED` instead of creating it, and the
adapter never retries unconditionally. Concurrent partial resource patches preserve
omitted working memory and merge metadata; whole-resource saves remain replacements.
Writes clear the default expiry. Server retention policies, explicit deletion, user
erasure and server policy refusals still apply.

Deletes use `memory.delete_version` with the exact identity and lossless version from
the validated read. A missing row is a no-op; a concurrent rewrite is preserved and
returns an error. Thread deletion captures the parent before deleting its observed
messages and still removes own orphan messages when that parent is missing. Deletes are
logical Loom tombstones; later explicit saves can recreate an entity. Batches, thread
timestamp updates and cascading deletes are separate operations: a failure can leave
partial work and is returned to the caller. A cascade cannot prevent a new message
after enumeration. The adapter provides no transaction, local fallback or additional
storage.

Semantic recall, observational memory, run fencing, atomic working-memory merges and
thread cloning are unsupported. Other storage domains are absent. The existing tools
remain available alongside native history and working memory. Mastra checks
each call against Loom's schema first and answers one outside it (a missing argument, an
extra key, a `top_k` above 20, a quoted number) with its own validation message; a call
within the schema that Loom refuses, or a server error, reaches the model as Mastra's
failed tool result with Loom's message. Either way the model reads the message and the run
goes on.

`make test-sdk-mastra-memory` runs the native tests with a fake Loom SDK transport.
`make test-sdk-mastra-memory-live` uses `LOOM_BASE_URL` and `LOOM_API_KEY`, with optional
`LOOM_ORG` and `LOOM_NAMESPACE`, to run the native memory lifecycle on a test deployment.

## AI SDK

`loomTools(agent, scope)` returns Loom's agent verbs as an AI SDK `ToolSet`, the record
`generateText`, `streamText` and the SDK's agents take as `tools`: the same `AgentTools` as
the [OpenAI Agents SDK](#openai-agents-sdk) adapter. `ai` 7.x is an optional peer
dependency, and needs Node 22.12 or later (the package is ESM-only).

```ts
import { generateText, stepCountIs } from "ai";
import { loomTools } from "@clickhouse/loom/integrations/ai-sdk";

const { text } = await generateText({
  model,
  tools: loomTools(agent, { userId: "alice" }),
  stopWhen: stepCountIs(5),
  prompt: "Book my usual seat to Lisbon",
});
```

The AI SDK has no memory store of its own: the messages of a conversation are the
caller's, and the tools give the model Loom's long-term memory. The AI SDK passes the
model's arguments through unchecked, and Loom checks them: it reads a quoted number as a
number and ignores an extra key. A call Loom refuses reaches the model as the AI SDK's
tool-error result with Loom's message, and the run goes on.

## n8n

The `@clickhouse/n8n-nodes-loom` community package gives self-hosted n8n a Loom Chat
Memory sub-node for its AI Agent and a Loom action node for Remember, Recall and Search
Knowledge. The action node can also connect as an agent tool. Both call your Loom
server through the SDK; data stays in your Loom deployment.

Install the package through n8n's Community Nodes settings after it is published, or
build a tarball from [`integrations/n8n`](https://github.com/ClickHouse/loom/tree/main/integrations/n8n)
and install it into the community nodes directory. Its README gives the checked build
and installation commands. The package bundles its SDK and framework libraries and is
not verified for n8n Cloud.

Create a Loom API credential with the server URL and a namespace-scoped writer token.
Set Organization, Namespace, Agent Name and User ID on each node. Connect Loom Chat
Memory to the AI Agent's Memory input and set Session ID to the conversation identifier
(such as the Chat Trigger's session ID). The workflow, user and session identify the
history. Context Window Length limits the interactions sent to the agent; older
messages stay in Loom until cleared. Remember stores durable facts separately from
conversation history. Use separate namespaces and restricted tokens for mutually
untrusted users.

Without a community package, n8n's MCP Client Tool node can connect to Loom's MCP URL
with `?toolset=core` and a bearer token, and expose `loom_memory_set` and `loom_search`
to its AI Agent.
[The MCP client setup](./mcp-clients.md#other-editors-and-agents) describes that route.

## Dify

The [Loom tools plugin](../../../integrations/dify/README.md) runs Remember, Recall and
Search knowledge as native Dify tools. Install its `.difypkg` from a local package
file and configure a reachable Loom URL, a scoped writer token and the deployment's
organization, namespace and agent. Bind the User ID form field to your app's user;
it is separate from model-generated arguments.

Remember stores a fact, Recall searches that user's memories across agents, and
Search knowledge searches shared app documents. Both searches stay in the configured
namespace. The plugin calls the Python SDK, adds no store and keeps data in your Loom
deployment. [Privacy](../../../integrations/dify/PRIVACY.md) describes credentials,
network access, provider processing and retention. The package is tested through the
native plugin SDK against Loom; a Dify application and Dify Cloud have not been tested.

The [MCP/OpenAPI setup](mcp-clients.md#hosted-builders-and-chat-apps) also works without
the package.

## LangGraph

A store retains application-owned constructor and callback conversation contributors
across client rebinding. Bind complete contributor authority before construction and
use one store per logical conversation family; restore that family's existing capsule.
An existing automatic SDK context makes only that invocation automatic, with every
retained family checked before dispatch. Direct calls and model-selected tools remain
explicit, including after an automatic invocation ends. All calls retain response
observation, so a marked or malformed reply protects earlier contributors before
returning or refusing. Namespace, thread and user ids supply no protection authority.

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

## Pipecat

`LoomMemoryService` owns two Pipecat `FrameProcessor`s that share one Loom `Agent` and
user. The recall processor sits after the user context aggregator and before the LLM; the
capture processor sits after the assistant aggregator. On each `LLMContextFrame` the recall
processor runs `memory.search` with the turn's latest user text and inserts the matches into
the shared `LLMContext`. Audio frames, and a second context frame for the same user text,
pass through with no further Loom call. After the turn, the capture processor sends the
turn's user and assistant text to `memory.set_from_messages` when it sees the assistant
aggregator's `LLMContextAssistantTurnFrame`.

```python
from pipecat.pipeline.pipeline import Pipeline
from pipecat.processors.aggregators.llm_context import LLMContext
from pipecat.processors.aggregators.llm_response_universal import LLMContextAggregatorPair
from loom import Agent
from loom.integrations.pipecat import LoomMemoryService

async with Agent(org="acme", namespace="support") as loom_agent:
    memory = LoomMemoryService(loom_agent, user_id="alice")
    context = LLMContext([{"role": "system", "content": "Be helpful."}])
    user, assistant = LLMContextAggregatorPair(context)
    pipeline = Pipeline(
        [
            transport.input(),
            stt,
            user,
            memory.recall_processor(),
            llm,
            tts,
            transport.output(),
            assistant,
            memory.capture_processor(),
        ]
    )
```

Install with `pip install 'clickhouse-loom[pipecat]'` (`pipecat-ai` 1.16.x).

The [text-only example](https://github.com/ClickHouse/loom/blob/main/sdk/python/examples/pipecat_text_memory.py)
uses a canned reply and requires the application's existing trusted session pointer.

| Pipecat                        | Loom                                                                                                                                                                                         |
| ------------------------------ | -------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `LLMContextFrame` (recall)     | `memory.search` with the turn's latest user text; with `honour_abstain`, leaves out memories below Loom's floor and adds nothing when Loom abstains; the matches replace one developer block |
| `LLMContextAssistantTurnFrame` | the user text of the new turn and the assistant text of its response, sent to `memory.set_from_messages` (400 messages a call, the op's cap); memory blocks and empty text are not sent      |

Extraction runs under a Loom session derived from the user and the service's `session_id`
(`pipecat-` and a digest of the pair): Loom files a turn's verbatim chunks by session alone,
and a caller may choose its session ids. When `session_id` is omitted, the service mints one
for its lifetime, so the turns of one call share a session.

Bind the application's existing `SessionProtection` before creating the service, and
use one service per logical conversation family. The service retains constructor and
callback contributors before native processing awaits, including committed context
frames whose capture happens later. Client rebinding cannot drop an earlier contributor.
Native user and session ids provide routing, not protection authority.

A first marked recall can enter the native context after durable protection. Later
automatic recall and capture are suppressed while native frames keep flowing. Protection
discards only the service's retry references and write tracking; it does not clear
application-owned history or acknowledge unsent extraction. These controls do not declare
fleet support or initialize a capsule during restore.

| `LoomMemoryService` argument | Default  | Effect                                                                                                                                                                                      |
| ---------------------------- | -------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `agent`                      | required | the `loom.Agent` the processors call; Pipecat awaits its processors, so a `SyncAgent` is refused                                                                                            |
| `user_id`                    | none     | the end user the memories belong to; unset, recall reads all namespace memories the token permits and extraction writes shared rows; empty, `-` or whitespace-padded ids raise `ValueError` |
| `session_id`                 | minted   | the caller's conversation id, folded into the Loom session; unset, a fresh id is used for this service instance                                                                             |
| `top_k`                      | 5        | memories per turn, from 1 to 500                                                                                                                                                            |
| `honour_abstain`             | `True`   | leave out memories below Loom's floor; nothing when Loom abstains                                                                                                                           |
| `defer`                      | `True`   | queue the extraction on the server instead of waiting                                                                                                                                       |

## Langflow

The [Loom Langflow component](../../../integrations/langflow/README.md) saves a memory,
searches memories, and searches knowledge documents from a Langflow flow. It is a
Langflow `Component` with typed inputs for the namespace, the user, and the query, plus
an action dropdown (`save`, `search memory`, `search documents`). Install
`clickhouse-loom` in Langflow's Python environment and load
`integrations/langflow/loom_component.py` as a custom component. The component
constructs a `SyncAgent` and calls `memory.set`, `memory.search`, or `knowledge.search`;
the user input is `user_id` on the memory write and search. Knowledge search is
namespace-scoped. Data stays in your Loom deployment; the component adds no store.

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
- **CrewAI carries on without memory when a memory call fails.** A crew runs the task
  anyway: a failed save prints only for a `verbose` agent, and a failed recall shows only as
  a `MemoryRetrievalFailedEvent` on CrewAI's event bus, so an unreachable Loom is otherwise
  silent.
- **An AG2 turn fails when Loom is unreachable.** `LoomRecallPolicy` runs before each
  model call of the turn, and `AssemblerMiddleware` does not catch a policy's error.
- **An Agent Framework run fails when Loom is unreachable.** The `Agent` does not catch a
  provider's error, before the run or after it. A failure after the run comes once the model
  has answered: the run raises, a history provider that runs after Loom's (the agent's
  in-memory one) does not record the turn, any tool's effect stays, and a streamed reply has
  already been shown.
- **ADK carries on without memory when `preload_memory`'s search fails.** It logs a
  warning and the model call goes on; a failed `load_memory` search or save callback fails
  the run.
- **An AutoGen turn fails when Loom is unreachable.** `AssistantAgent` calls
  `update_context` once per turn, before its first model call, and does not catch its
  errors.
- **A refused tool call reaches the model, and the run goes on.** The OpenAI Agents SDK tools
  return Loom's error to the model as the call's result; the Pydantic AI, Mastra and AI SDK
  tools return it as a failed tool result (Mastra answers a call outside Loom's schema with
  its own validation message first), and the AG2 tools as a tool error.
- **Write verdicts are hidden by the framework interface.** A `put` or `add` that lands as
  `conflicted` or `duplicate` returns normally; read the namespace's conflicts in the
  dashboard or with `conflicts.list` ([conflicts and refusal](../memory/conflicts-and-refusal.md)).
- **One store per event loop** for LangGraph's sync API: mixing sync and async calls on
  one store runs the agent on two loops.
- **A Pipecat turn continues when Loom is unreachable.** The recall and capture processors
  still push the frame they were given: the context frame reaches the model, and audio
  frames stay on their path. A failed capture leaves the turn in the conversation but not
  in Loom.

## Related

- [Python SDK](../../reference/python-sdk.md) and [TypeScript SDK](../../reference/typescript-sdk.md).
- [Writing memory](../memory/writing.md) and [search and ask](../memory/search-and-ask.md).
- [Integrations overview](./index.md).

- [Context-provider architecture](../../architecture/agent-framework-adapter.md).
- [Memory-tool architecture](../../architecture/claude-memory-adapter.md).
- [Session history architecture](../../architecture/openai-agents-sessions.md).
- [ADK adapter architecture](../../architecture/google-adk-adapter.md).
