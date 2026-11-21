# Compare agent memory architectures

Compare deployment, sharing, provenance and staleness handling when choosing memory for
coding agents or a software factory. The boundary matters: a memory service, a library,
a temporal graph and an agent harness give your application different responsibilities.

This page is published by the Loom project. It compares documented mechanisms, with
primary sources below. No comparative deployment or quality, speed or cost benchmark is
reported here. The Loom row describes release 1.3.0; the other rows describe the linked
source snapshots. Check the edition and package you intend to deploy.

## Deployment and sharing

| Choice         | Deployment and operation                                                                                                                                                                                                   | Sharing and access                                                                                                                                                                                                                                                                                   |
| -------------- | -------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- | ---------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| **Loom**       | Operate the server and ClickHouse; Postgres is an option for live memory state. The released quickstart uses local embeddings and a development LLM. Configure a real LLM for meaningful extraction and generated answers. | The default quickstart is single-user and loopback-only. Team deployments use authenticated principals and namespace grants. A shared namespace can serve several authorized agents; an agent label is not an authorization boundary. [Quickstart][loom-quickstart], [identity][loom-identity].      |
| **Mem0**       | Choose its embedded library, self-hosted server or separate hosted platform. Configure the storage, embedding and model providers for the chosen mode.                                                                     | The library example passes `user_id` when adding and searching memory. The self-hosted server documents authentication and API keys. Application filters and server authentication need to be configured together. [Source][mem0].                                                                   |
| **Graphiti**   | Embed the Python library and operate its graph database and model providers. Its reference HTTP/MCP servers are examples, rather than production service deployments.                                                      | `group_id` separates graph data. The cited reference-server documentation explicitly requires a production application to supply authentication, authorization and tenant isolation; its reference servers do not enforce those boundaries. [Source][graphiti].                                      |
| **Letta Code** | Adopt a stateful agent harness. It defaults to Letta Cloud and also offers an explicit local backend choice. Configure model access for the selected backend.                                                              | Cloud holds an agent's memory, identity and conversations across machines. The harness supports subagents and agent-to-agent calls. Decide which agents and machines may access that state; this is a different boundary from adding a shared memory service to an existing runner. [Source][letta]. |

Self-hosted software can still send text to configured model providers. Infrastructure,
model access and hosted plans have separate costs and terms. A source license does not
grant access to a vendor's hosted service.

## Provenance and staleness

| Choice         | What the cited source establishes                                                                                                                                                                               | What to verify for a coding workflow                                                                                                                                                                                                                                                                                 |
| -------------- | --------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- | -------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| **Loom**       | The server stamps the writer's `actor` and `source_class`; memory versions retain change history. Indexed, resolvable code references can receive fingerprint-based link states from the code-link worker.      | Confirm indexing, reference resolution and worker progress. A changed code fingerprint calls for review of the claim. It does not prove the claim false. Code-link status does not rewrite the stored memory; optional revision produces a suggestion. [Provenance][loom-provenance], [code links][loom-code-links]. |
| **Mem0**       | The example passes conversation messages into extraction and recalls memories using an application-supplied user filter. Its README describes ADD-only extraction and time-aware retrieval over dated memories. | Determine which source identifiers and review records your application must retain. Automatic repository-revision validation is not established by this README; test the exact extraction and retrieval path you use. [Source][mem0].                                                                                |
| **Graphiti**   | Derived entities and relationships trace back to ingested episodes. Relationships have temporal validity windows, and ingestion can invalidate superseded facts while retaining history.                        | Temporal invalidation of an extracted relationship does not establish the truth of a code-related lesson. Keep the supporting episode and check the relevant repository revision when applying it. [Source][graphiti].                                                                                               |
| **Letta Code** | MemFS tracks context, including memory blocks, through Git. Agents can rewrite their memory, skills and prompts; a custom repository can synchronize MemFS.                                                     | Versioning the agent's context is different from checking the implementation that a memory describes. Automatic source-code fingerprint validation is not established by this README. Review the authority to edit context as well as its history. [Source][letta].                                                  |

These provenance records describe where a claim came from or how it changed. They do
not independently establish that the claim is correct. A harmless refactor can leave a
lesson valid; a behavior change can invalidate it. Resolve that distinction against the
current source and a relevant check.

## Check the boundary before adoption

1. Connect an authorized reader and verify retrieval from the intended shared scope.
   Verify that a different principal cannot read it without access.
2. Save a lesson with its source revision and location. Make a harmless refactor and a
   separate behavior change. Observe which metadata changes and whether the reviewer
   rechecks the underlying claim.
3. Correct and delete the lesson. Check history, derived data and the deployment's
   retention policy separately.

These are adoption checks to perform on your deployment, not comparative results. For
Loom setup, start with the [quickstart](../getting-started/quickstart.md). For the existing
workflow, see [coding-agent memory](../use-cases/agent-memory.md),
[multi-agent memory](../use-cases/multi-agent.md) and
[memory linked to code](../use-cases/memory-linked-to-code.md).

[loom-quickstart]: https://github.com/ClickHouse/loom/blob/v1.3.0/docs/getting-started/quickstart.md
[loom-identity]: https://github.com/ClickHouse/loom/blob/v1.3.0/docs/concepts/identities-and-namespaces.md
[loom-provenance]: https://github.com/ClickHouse/loom/blob/v1.3.0/docs/features/memory/history-and-provenance.md
[loom-code-links]: https://github.com/ClickHouse/loom/blob/v1.3.0/docs/concepts/memory.md#memories-linked-to-code
[mem0]: https://github.com/mem0ai/mem0/blob/fff8a6472512d1bc1a4bccab8c645b65a1df7fe5/README.md
[graphiti]: https://github.com/getzep/graphiti/blob/9b8a4630092084734b080f2e6f409c2c23bdcc3a/README.md
[letta]: https://github.com/letta-ai/letta-code/blob/76428c1b0ee7efc9b4865c2b318db806070efb63/README.md
