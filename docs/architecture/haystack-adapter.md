# Haystack document store and retriever

The optional Haystack 3.5 adapter implements its document-store protocol and a native
retrieval component through the synchronous Loom SDK. The core SDK and server do not
depend on the framework. Each HTTP call has an SDK-managed client lifetime; the
adapter leaves no server, local database, cache or persistent background process.

## Invariants

A store maps each native document id to a hashed source URI under a store-specific
prefix. A matching tag scopes listing and retrieval. The full native document
serialization, including metadata, embeddings and binary fields, lives in the
document's original-file bytes. Chunk metadata carries a small format marker only;
text is indexed as document content, or the id when no text exists. Loom's
normal document write, provenance, namespace-grant, versioning and erasure paths apply
to every write and deletion. Documents are namespace-shared rather than user-private.

Counting and filtering hydrate the whole store through cursor pages. Native filter
helpers apply comparisons and Boolean logic. Retrieval searches up to 100 chunks with
explicit namespace, tag and URI boundaries, hydrates full documents and deduplicates
by source URI, preserving each document's best-ranked hit. These operations have no
cross-request snapshot. Concurrent writes can return a conflict from Loom. Duplicate
checks and multi-document operations are not transactions.

Serialized connections carry Haystack environment-variable Secret references; raw
Secret tokens refuse serialization. Explicit endpoint URLs reject embedded secrets.
The adapter preserves Haystack's pipeline deserialization trust policy and never
registers trusted modules or selects unsafe deserialization.

## Source entry points

- `sdk/python/loom/integrations/haystack.py`: `LoomDocumentStore` and `LoomRetriever`.
- `sdk/python/tests/test_haystack.py`: native document-store and pipeline behavior.
- `tests/integration/test_sdk_haystack.py`: live document persistence and retrieval.
