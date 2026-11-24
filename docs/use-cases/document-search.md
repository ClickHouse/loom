# Document search and RAG for agents

Loom indexes your documents (Markdown and text, and PDF, `.docx` and `.pptx` files that its
extractor service converts) and lets agents search them with citations over MCP, HTTP or
the SDKs. It is the retrieval half of retrieval-augmented generation (RAG): your agent or
model writes the answer from the passages Loom returns.

## What you get

- **Chunks that follow the document's structure.** `knowledge.add` stores the text and,
  optionally, the original file; Loom splits it on its headings, windows each section and
  embeds every chunk. [Knowledge documents](../features/knowledge.md)
- **Citations.** A hit names its document's `source_uri` and passage, so an agent can quote
  and link the source.
- **One search across memory, documents and conversations.** `loom_search` runs the memory,
  knowledge, profile and conversation searches in one call, across the caller's memory
  namespaces. [Unified search](../features/unified-search.md)
- **Document sources.** A namespace's admins can register sources Loom pulls documents
  from; the sync worker needs `LOOM_KNOWLEDGE_SOURCE_SYNC_ENABLED`.
  [Document sources](../features/knowledge.md#document-sources)
- **Documents beside the code.** Documents from a source on a repository's trunk code
  namespace carry its `repo:<id>` tag, so that repository's searches read them with its
  code. [Document sources](../features/knowledge.md#document-sources) ·
  [Memory linked to code](memory-linked-to-code.md)
- **A similarity floor.** `loom_search` drops knowledge hits below a similarity floor
  instead of handing the model a weak passage. [`loom_search`](../features/unified-search.md#loom_search)
- **Access control on every passage.** Documents live in namespaces with viewer, writer and
  admin roles. [Namespaces and sharing](../features/namespaces-and-sharing.md)

## Build a knowledge base agents can cite

Start with documents the team already maintains, such as runbooks, architecture
decisions and policies. Group sources in namespaces with the same readership. Give
each agent access to the namespaces it needs and register sources for the documents
that change outside Loom. A search hit gives the agent a passage, source and revision
to check against the question. Store a decision an agent makes while applying a
policy in [shared agent memory](agent-memory.md) so future agents can find it alongside
the original evidence. Follow the [knowledge factory guide](knowledge-factory.md) to review and publish answers to recurring gaps.

## Measured

On a committed corpus of PDF, DOCX and PPTX files (27 held-out files), Loom's extractor,
with OCR on (its default), scored 0.999 text recall and 0.991 table cell F1 in the
measured run ([document parsing](../benchmarks/results.md#document-parsing)).
Retrieval quality over documents is measured only in part;
[coverage and known gaps](../benchmarks/coverage.md) says what is still missing.

## Start

The [quickstart](../getting-started/quickstart.md) runs the stack with the extractor
service. Add a document from the dashboard's Library page, with `knowledge.add` for text,
or, for a file the extractor converts, with `knowledge.upload` then `knowledge.ingest`
([ingesting files](../features/knowledge.md#ingesting-files)).
[`document_search.py`](../../sdk/python/examples/document_search.py) runs the whole loop
from Python: it adds two documents, cites the passages a search returns, and gets nothing
back for a question they do not answer.

## Inspect a citation over MCP

Start with documents already ingested into a namespace you may read. File ingestion
needs the extractor service; document ingestion and semantic search need the server's
configured embedding provider. Check [models and API keys](../getting-started/models-and-keys.md)
before using internal documents: self-hosting Loom does not keep document text local
if you configure a hosted embedding provider, reranker or answer model. Those providers
can charge for calls. Source connectors need their own configuration and credentials;
connecting an MCP client does not turn them on.

1. [Connect an MCP client](../features/integrations/mcp-clients.md) with a Loom token
   that has the `viewer` role on the document namespace. Limit the token's grants to
   the namespaces you intend to search. In that client, call `loom_search` directly
   with these arguments:

   ```json
   {
     "query": "What checks are required before deploying a service?",
     "top_k": 3
   }
   ```

   The server selects the authorized search scope. This unified search can return
   memory and conversation results as well as document passages; the request does
   not disable the deployment's configured reranker.

2. Inspect each entry in `structuredContent.knowledge.results`. Read its `content`
   before using it to support an answer. Keep its returned `source_uri`, `revision`
   and any `section` or `locator` with the passage. `source_uri` is a document
   identifier, not necessarily a URL the reader can open.
3. Use `locator` when it supplies a source location, such as a PDF page range.
   Location fields can be absent, and older chunks can have `revision: 0`.
   Report missing location information rather than inventing page or line numbers.
   Check extracted passages against the original document, especially tables and scans.
4. Have the agent quote the supporting passage and cite its source beside each claim.
   An empty document result or a passage that does not answer the question is grounds
   to abstain. `loom_search` returns retrieved information; it does not generate an
   answer or verify that a passage supports the agent's conclusion. Access to stored
   passages also does not grant access to an external source's original file.

The request above is an example to run against your own indexed documents, not a
captured search result. See the [unified search contract](../features/unified-search.md#loom_search)
for the other request fields and result sections.

Some ops on this page are marked experimental on the [status page](../reference/status.md).
