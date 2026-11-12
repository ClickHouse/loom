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
  knowledge, profile and conversation searches in one call, across the namespaces the
  caller may read. [Unified search](../features/unified-search.md)
- **A similarity floor.** `loom_search` drops knowledge hits below a similarity floor
  instead of handing the model a weak passage. [`loom_search`](../features/unified-search.md#loom_search)
- **Access control on every passage.** Documents live in namespaces with viewer, writer and
  admin roles. [Namespaces and sharing](../features/namespaces-and-sharing.md)

## Measured

Parse fidelity for PDF, DOCX and PPTX is measured on a committed corpus
([document parsing](../benchmarks/results.md#document-parsing)). Retrieval quality over
documents is measured only in part; [coverage and known gaps](../benchmarks/coverage.md)
says what is still missing.

## Start

The [quickstart](../getting-started/quickstart.md) runs the stack with the extractor
service. Add a document from the dashboard's Library page or with `knowledge.add`.
