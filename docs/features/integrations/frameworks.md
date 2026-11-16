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

| Framework                      | Adapter                                         | SDK                                                       | Framework interface                                      |
| ------------------------------ | ----------------------------------------------- | --------------------------------------------------------- | -------------------------------------------------------- |
| LangChain                      | `LoomKnowledgeRetriever`, `LoomMemoryRetriever` | Python, `loom.integrations.langchain`                     | `BaseRetriever`                                          |
| LangChain.js                   | `LoomKnowledgeRetriever`, `LoomMemoryRetriever` | TypeScript, `@clickhouse/loom/integrations/langchain`     | `BaseRetriever` from `@langchain/core`                   |
| LlamaIndex                     | `LoomRetriever`, `LoomMemoryBlock`              | Python, `loom.integrations.llamaindex`                    | `BaseRetriever`, `BaseMemoryBlock`                       |
| OpenAI Agents SDK              | `loom_tools`                                    | Python, `loom.integrations.openai_agents`                 | `FunctionTool`                                           |
| OpenAI Agents SDK (TypeScript) | `loomTools`                                     | TypeScript, `@clickhouse/loom/integrations/openai-agents` | `tool()` from `@openai/agents`                           |
| CrewAI                         | `LoomCrewMemory`                                | Python, `loom.integrations.crewai`                        | `crewai.memory.Memory`                                   |
| AutoGen                        | `LoomMemory`                                    | Python, `loom.integrations.autogen`                       | `autogen_core.memory.Memory`                             |
| AG2                            | `LoomRecallPolicy`, `loom_tools`                | Python, `loom.integrations.ag2`                           | `ag2.assembly.AssemblyPolicy`, `tool()` from `ag2.tools` |
| Pydantic AI                    | `loom_toolset`                                  | Python, `loom.integrations.pydantic_ai`                   | `FunctionToolset`                                        |
| Mastra                         | `loomTools`                                     | TypeScript, `@clickhouse/loom/integrations/mastra`        | `createTool` from `@mastra/core`                         |
| AI SDK                         | `loomTools`                                     | TypeScript, `@clickhouse/loom/integrations/ai-sdk`        | `tool()` from `ai`                                       |
| LangGraph                      | `LoomStore`                                     | Python, `loom.integrations.langgraph`                     | `BaseStore`                                              |
| Strands Agents                 | `LoomMemoryStore`                               | Python, `loom.integrations.strands`                       | the `MemoryStore` protocol                               |
| Strands Agents                 | `LoomMemoryStore`                               | TypeScript, `@clickhouse/loom/integrations/strands`       | the same protocol, structurally                          |

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

Conversation history stays in the session store the run is given: the OpenAI Agents
SDK's `Session` protocol removes and clears items, and Loom's conversations are
append-only. To keep what a conversation established, give the model `loom_remember`, or
send the turns to `memory.set_from_messages`.

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

## AG2

An AG2 1.3 agent gets context through assembly policies, which run before each model call,
and acts through tools. `LoomRecallPolicy` is a policy that adds the Loom memories matching
the latest user message to the prompt, and `loom_tools` returns Loom's agent verbs as AG2
tools, the same `AgentTools` as the [OpenAI Agents SDK](#openai-agents-sdk) adapter, so the
model writes memories with `loom_remember`. Install with
`pip install 'clickhouse-loom[ag2]'` (`ag2` 1.3).

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
[OpenAI Agents SDK](#openai-agents-sdk) adapter. `@mastra/core` 1.83 or a later 1.x is an
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

Mastra's own memory keeps threads and messages in a storage adapter that edits and deletes
them; Loom's conversations are append-only, so the adapter offers Loom as tools rather
than as that store. A Mastra `Memory` can keep its thread history in its own store while
the model reads and writes long-term memory in Loom through these tools. Mastra checks
each call against Loom's schema first and answers one outside it (a missing argument, an
extra key, a `top_k` above 20, a quoted number) with its own validation message; a call
within the schema that Loom refuses, or a server error, reaches the model as Mastra's
failed tool result with Loom's message. Either way the model reads the message and the run
goes on.

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
- **CrewAI carries on without memory when a memory call fails.** A crew runs the task
  anyway: a failed save prints only for a `verbose` agent, and a failed recall shows only as
  a `MemoryRetrievalFailedEvent` on CrewAI's event bus, so an unreachable Loom is otherwise
  silent.
- **An AG2 turn fails when Loom is unreachable.** `LoomRecallPolicy` runs before each
  model call of the turn, and `AssemblerMiddleware` does not catch a policy's error.
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

## Related

- [Python SDK](../../reference/python-sdk.md) and [TypeScript SDK](../../reference/typescript-sdk.md).
- [Writing memory](../memory/writing.md) and [search and ask](../memory/search-and-ask.md).
- [Integrations overview](./index.md).
