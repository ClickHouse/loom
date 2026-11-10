# Frequently asked questions

Short answers to the first questions people ask. Each links to the page that explains it.

## Using it

### What is the fastest way to try Loom?

If a Loom server already exists, install the Claude Code or Codex plugin and point it at the
server: [connect a coding agent](getting-started/connect-a-coding-agent.md). To run a server
on a laptop, `loom up` boots one in a few minutes: [quickstart](getting-started/quickstart.md).

### Why are my search results nonsense?

The install runs a fake embedder: its `.env` (a checkout's, or `~/.loom/.env`) names
`fake-ngram-*`, whose vectors make retrieval meaningless. Switch its embedder, which
re-embeds what is stored ([Models and keys](getting-started/models-and-keys.md)); a fresh
install of the released stack embeds with a local model already. A hosted embedder
retrieves better on paraphrase-heavy questions
([providers](operations/deployment.md#providers)).

### Why does `memory.search` say `abstain` instead of returning something?

No hit cleared the similarity floor calibrated for your embedder, so Loom says it doesn't
know rather than hand back the nearest weak match. The reason is in the response. See
[retrieval and ranking](concepts/retrieval-and-ranking.md).

### Why was my write flagged as a conflict, or refused?

A write close in meaning to an existing memory is marked `conflicted` and linked to it; a
close write against a pinned memory is refused with `409 PINNED_CONFLICT`; a write matching a
blacklist pattern gets `422 BLACKLISTED`. Resolve conflicts in the dashboard's Review queue or
with `conflicts.resolve`. See [conflicts and refusal](features/memory/conflicts-and-refusal.md).

### Can I use Loom just for myself?

Yes. Run the [local stack](getting-started/quickstart.md), which has no login and one
namespace, and connect your agents with the [plugin or `loom install`](getting-started/connect-a-coding-agent.md).
On a server with login, each person gets a `personal-<slug>` namespace that their MCP
clients write to by default and nobody else reads without a grant. Memory saved from
Claude Code is then there for Codex, Cursor and the dashboard.

### Can two agents see each other's memories?

Every member of a namespace can read its rows. Each agent writes into its own layer, and an
admin can copy a memory into the namespace's shared layer with `memory.promote`. A token can read several
namespaces while writing one. See [identities and namespaces](concepts/identities-and-namespaces.md).

### When should I use knowledge documents instead of memories?

A memory is one short claim that can be revised. A document, a long page or anything over a
couple of kilobytes goes in the knowledge plane with `knowledge.add`, which chunks and embeds
it. See [memory](concepts/memory.md#what-goes-where) and [knowledge documents](features/knowledge.md).

### How do I make Loom forget something?

`memory.delete` tombstones one memory (restorable for a grace window);
`erasure.request` removes everything a user or namespace owns, across every table, and
verifies nothing is left. Loom deletes nothing on its own schedule unless an operator sets a
retention window. See [retention and erasure](features/memory/retention-and-erasure.md).

### How do I find out why a result ranked where it did?

Call `memory.search` with `explain=true`: each hit's `score_components` carry the ranking
signals and, after a reranker, its move (`rerank_from`, `rerank_pos`, `rerank_delta`) and,
when the reranker changed the order, its own fused score (`rank_score_pre_rerank`).
`memory.search_trace` scores the vector leg alone. See [search and
ask](features/memory/search-and-ask.md).

### Can Loom answer questions about my code?

Yes, for repositories the server indexes: `code.search`, `code.symbol`, `code.callers`,
`code.impact` and more, each answer saying which commit and index tier backs it. See the
[code index](features/code-index/overview.md) and [adding a repository](features/code-index/repositories.md).

### Why doesn't my coding agent use the code index?

The plugin attaches the index only when the session starts inside a git checkout whose
remote is an indexed repository you can read. Start the session from the repository's
checkout, check the repository is listed under **Repositories** in the dashboard (`<server>/repositories`), and see
[when the code index is used](getting-started/connect-a-coding-agent.md#when-the-code-index-is-used).

## Running it

### Do I need ClickHouse Cloud?

No. Loom runs against ClickHouse OSS too, and the laptop stack starts one in Docker. Cloud is
the intended production path because single-node OSS has no replication or point-in-time
recovery. See [deployment](operations/deployment.md).

### Can I use Anthropic or Gemini models?

Yes. The LLM provider can be OpenAI or Anthropic, and embeddings can come from OpenAI,
Gemini or a local CPU model. Changing the embedding model changes the vector space, so run
`loom reembed` afterwards. See [LLM and embeddings](architecture/llm-architecture.md).

### How do I give someone access?

Mint a token (`loom mint-token`, or the dashboard's Settings → Tokens) and grant a role on a
namespace. See [tokens and grants](guides/tokens-and-grants.md).

### Something is wrong. Where do I start?

`loom doctor` on the server, `/loom:loom-status` in Claude Code, and `/v1/health`. Then
[troubleshooting](operations/troubleshooting.md), which is organised by symptom.

## The project

### Why is an operation missing from my MCP tool list?

The MCP listing depends on the toolset (`?toolset=` or `LOOM_MCP_TOOLSET`, default
`core`), and code tools are listed only with `LOOM_MCP_CODE_TOOLS=true`. Under `all`, an
op that neither the core nor the atomic roster lists and that is marked `experimental` is
left out. Every op is still callable by name through `loom_op`, over HTTP and from the
SDKs. See [MCP toolsets](reference/mcp.md#toolsets).

### How good is the retrieval, and how do you know?

Loom ships its benchmark suite with the method, datasets and exact commands. Only results
reproducible from committed files are stated. See [benchmarks](benchmarks/overview.md).

### How is Loom different from putting a vector database next to my app?

Under the default ClickHouse authority, one ClickHouse database holds the rows, vectors,
text index, graph, audit and erasure state, so nothing has to be kept in sync. The
[Postgres memory authority](architecture/storage-authority.md#why-two-stores) holds live
memory and knowledge state in Postgres, and Loom keeps the two stores in step itself.
Each write is checked against what is stored. The
[overview](getting-started/overview.md) lists what each property costs and when Loom
does not fit.
