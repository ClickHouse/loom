# Compare memory tools and code intelligence for coding agents

Choose memory for a coding agent or a software factory by deciding what must survive:
conversation context, shared team decisions, evidence from source code, or all three.
Then choose who operates the storage and who may read or change it.

The Loom project publishes this guide. It covers memory services and coding-session
plugins alongside code-intelligence tools that can complement them. The rows describe
the linked primary sources and their deployment boundaries, rather than a ranked list.
Loom capabilities below refer to release 1.3.0. Check the edition and package you deploy.

## Where Loom fits

Loom combines an agent memory service with a repository index. An authorized agent can
find an implementation and its callers, retain a team decision with its evidence, and
retrieve that decision in a later session or from another runner. These are different
jobs: source retrieval finds the implementation; durable memory retains what the team
learned about it.

- **Shared facts across agents:** authorized agents can use a shared namespace. Tokens,
  principals and namespace grants control access; an agent label alone does not.
- **Attribution and revision:** the server stamps the writer's `actor` and
  `source_class`. Memory versions retain change history, so a correction has a record.
- **Code context and change impact:** search, symbol and caller tools expose indexed
  repository context. Check the index's revision and freshness before relying on it.
- **Memory linked to code:** resolvable references can receive fingerprint-based link
  states. A changed, missing or unresolved reference calls for review of the claim.

Choose this combination when several agents need both repository evidence and durable
shared knowledge. It requires operating Loom and its stores: ClickHouse, with Postgres
an option for live memory state. The released quickstart uses local embeddings and a
development LLM; configure a real model for meaningful extraction and generated answers.
See the [quickstart][loom-quickstart] and [identity model][loom-identity].

Code freshness does not prove a remembered claim true. A harmless refactor can preserve
a lesson; a behavior change can invalidate it. Link status does not rewrite the memory,
and optional revision produces a suggestion. Symbol following depends on supported
extractors and reference resolution. Treat unresolved references as unresolved rather
than assuming the implementation still matches. See [provenance][loom-provenance] and
[code-linked memory][loom-code-links].

## Memory architectures, deployment and sharing

| Choice                     | Good fit and operating boundary                                                                                                                                                                      | Sharing, provenance and staleness to consider                                                                                                                                                                                                                                                                                                          |
| -------------------------- | ---------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------ |
| **Loom**                   | A shared memory service plus repository discovery and impact context. Operate the server and stores; choose model providers.                                                                         | Namespace grants scope access. Writer stamps, version history and supported code-link states help a reviewer connect a lesson to its evidence. The default single-user quickstart is loopback-only; team authentication requires configuration. [Setup][loom-quickstart], [provenance][loom-provenance].                                               |
| **Mem0**                   | Add memory to an existing application through its library, self-hosted server or separate hosted platform. Configure the chosen storage and model providers.                                         | The library example supplies `user_id` to add/search; the self-hosted server documents authentication and API keys. Its README describes ADD-only extraction and time-aware retrieval. Keep the source identifiers your own review process needs. [Source][mem0].                                                                                      |
| **Graphiti**               | Build an application around a temporal knowledge graph. Operate the graph database and providers when embedding the Python library.                                                                  | Entities and relationships trace to episodes; temporal validity and invalidation retain changing facts. `group_id` separates graph data. Its reference HTTP/MCP servers require the production application to supply authentication, authorization and tenant isolation. Evaluate the hosted service separately from this library. [Source][graphiti]. |
| **Letta Code**             | Adopt a stateful coding-agent harness. It defaults to Letta Cloud and offers an explicit local backend choice.                                                                                       | Agent identity, memory and conversations persist across machines. MemFS versions context through Git and can synchronize a custom repository; agents can rewrite memory and prompts. Decide who may edit that context. Context versioning alone does not verify a lesson against current code. [Source][letta].                                        |
| **Supermemory**            | Add memory, profiles and document retrieval through an API, coding-agent plugins or MCP. Its README documents hosted access and a local server with embedded storage and local embeddings.           | Container tags organize project/user context. The documented engine handles updates, contradictions and expiry. Local requests still require an API key; hosted access and local operation have separate configuration. Check source retention and sharing for the chosen deployment. [Source][supermemory].                                           |
| **Cognee**                 | Build connected memory from documents, conversations and code. Use local models, configured providers, a self-hosted API or its managed service.                                                     | Datasets organize memory; retrieval can use graph, vector or code context, and session distillation curates lessons. The API defaults to authenticated multi-tenant mode; its explicit single-user demo disables that control. Review dataset permissions and deletion for your deployment. [Source][cognee].                                          |
| **claude-mem**             | Capture coding-session activity through hooks and a local worker, then recall observations in later sessions. Supported agents differ in whether they capture activity or only search it.            | SQLite stores observations/summaries, with semantic search alongside keyword search. Observation IDs point back to past work. Cloud sync and TeamBrain sharing are optional; observer-provider choice determines where activity is processed. Session history is evidence to recheck, not automatic validation of a code claim. [Source][claude-mem].  |
| **Cipher / ByteRover CLI** | Resolve the product identity before adoption: the former Cipher repository redirects to ByteRover CLI and is archived. Its cited snapshot describes a local context tree and optional cloud sharing. | The snapshot documents Git-like context versions, curate/review operations and push/pull. An archived source snapshot does not establish a maintained current package or service. Verify the current product, license and migration path before deployment. [Source][cipher].                                                                          |

Self-hosted software can still send text to configured model providers. Infrastructure,
model calls and hosted plans have separate costs and terms. A source license does not
grant access to a hosted service. None of the rows establishes comparative quality or
an absence of capabilities beyond the cited documentation.

## Code intelligence and repository context

A code index answers where behavior lives and what depends on it. A memory layer can
retain why a team chose that behavior, which evidence supported it and what needs
rechecking. You may want both. These tools have broader capabilities than indexing
alone; this table does not classify all of them as code-index-only products.

| Choice          | Primary workflow and deployment                                                                                                                                                                                                   | Evidence, freshness and memory boundary                                                                                                                                                                                                                                |
| --------------- | --------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- | ---------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| **Sourcegraph** | Organization-wide code search, navigation, Deep Search and MCP access. Its docs describe an internal deployment and Enterprise offerings; the public instance is for open-source code.                                            | Search can target branches/commits; Deep Search exposes searches and files behind sourced answers. Navigation, insights and batch changes extend beyond retrieval. Check the chosen edition's deployment and access controls. [Docs][sourcegraph].                     |
| **Serena**      | Symbol-level retrieval, editing and refactoring through an MCP server launched by the client or served over HTTP. The default backend uses language servers; the separate JetBrains backend has different terms and capabilities. | It also has a memory system for sharing knowledge across sessions, users and projects. It is not merely a code index. Language/backend support determines navigation and edit behavior; review both its memory workflow and authority to edit files. [Source][serena]. |
| **ChunkHound**  | Local-first code indexing, cited research across code and Git history, plus technical web research. Regex search needs no provider; semantic search and deep research have provider requirements.                                 | Queries can target commits and ranges, and generated research cites supporting context. Provider configuration determines code egress. Decide separately how lasting team decisions are stored, corrected and authorized. [Source][chunkhound].                        |
| **Codanna**     | A local repository index served through MCP or a one-shot CLI. Search, call graphs, dependencies, document retrieval and impact analysis share that index.                                                                        | Results carry file/line coordinates and symbol IDs. Local embeddings are the default; remote embeddings are opt-in. Watch mode updates the index. Review index freshness separately from the validity of a remembered team decision. [Source][codanna].                |

For Loom's combined workflow, start with [repository questions](repository-questions.md),
[coding-agent memory](../use-cases/agent-memory.md) and
[shared software-factory memory](../use-cases/multi-agent.md).

## Measured Loom results and their limits

The existing [paired coding-agent results][coding-results] compare the same Claude Code
`claude-sonnet-5` agent with and without Loom's plugin and code tools on ClickHouse
repository questions. Both arms have Bash/Read/Grep/Glob; subagents are disabled.
The answer judge is `claude-opus-5-5`. These are with/without measurements, not a
head-to-head comparison with the products above.

| Task and repetitions                                     | Without Loom → with Loom                                      | Agent model bill per question                                                                                 |
| -------------------------------------------------------- | ------------------------------------------------------------- | ------------------------------------------------------------------------------------------------------------- |
| Answer-graded real work: 24 questions × 3 paired runs    | Mean judged score **0.78 → 0.84**                             | **$0.212 → $0.170**, about **20% lower**. [Raw rows][work-raw].                                               |
| Conceptual file discovery: 36 questions × 10 paired runs | Right file among up to three returned paths **91.4% → 96.4%** | **$0.096 → $0.076**, about **21% lower**. [Raw rows][conceptual-raw].                                         |
| Multihop work: 24 questions × 3 paired runs              | Mean judged score **0.72 → 0.76**                             | **$0.423 → $0.396**; the bug-to-fix subset is **$0.811 → $0.811**, with no savings. [Raw rows][multihop-raw]. |

The bill figures exclude Loom server-side model calls and infrastructure: they are not
total-cost savings. Wall-clock time is longer with Loom in these runs. Judged scores
are not percentages of issues fixed, and finding the right file is not answer quality.
The multihop artifact retains eight error rows per arm. The tasks, model, tool setup
and run-to-run noise limit generalization; see the
[protocol and limitations][coding-results]. The withdrawn SWE agent sample is excluded.

A separate [memory-context measurement][memory-results] uses 100 LongMemEval-S questions
(seed 13), the same `gpt-5` reader and the official judge. Full history uses **103,879**
reader prompt tokens per question versus **9,443** for the top-200-memory arm, about
**91% fewer reader prompt tokens**. Memory construction is excluded. Accuracy is
90% versus 94%, but paired McNemar **p = 0.34** does not establish an accuracy gain.
This is not a coding-task or total-token result. [Raw summary][memory-raw].

A comparable named-tool head-to-head result is **unmeasured here**. Different vendor
benchmarks do not form a common scoreboard. A fair evaluation would hold the repository,
tasks, model and tools constant, compare a code-intelligence baseline with Loom code
context and then shared memory, and report correctness, stale-lesson handling, tokens,
wall-clock time and total cost including construction and server inference.

## Check the boundary before adoption

1. Connect an authorized reader and verify retrieval from the intended shared scope.
   Verify that a different principal cannot read it without access.
2. Save a lesson with its source revision and location. Make a harmless refactor and a
   separate behavior change. Observe whether the reviewer rechecks the claim.
3. Correct and delete the lesson. Check history, derived data and retention separately.

These are adoption checks for your deployment, rather than comparative results.

[loom-quickstart]: https://github.com/ClickHouse/loom/blob/v1.3.0/docs/getting-started/quickstart.md
[loom-identity]: https://github.com/ClickHouse/loom/blob/v1.3.0/docs/concepts/identities-and-namespaces.md
[loom-provenance]: https://github.com/ClickHouse/loom/blob/v1.3.0/docs/features/memory/history-and-provenance.md
[loom-code-links]: https://github.com/ClickHouse/loom/blob/v1.3.0/docs/concepts/memory.md#memories-linked-to-code
[mem0]: https://github.com/mem0ai/mem0/blob/fff8a6472512d1bc1a4bccab8c645b65a1df7fe5/README.md
[graphiti]: https://github.com/getzep/graphiti/blob/9b8a4630092084734b080f2e6f409c2c23bdcc3a/README.md
[letta]: https://github.com/letta-ai/letta-code/blob/76428c1b0ee7efc9b4865c2b318db806070efb63/README.md
[supermemory]: https://github.com/supermemoryai/supermemory/blob/9547bd0f571c7e03d679bade5513876119048945/README.md
[cognee]: https://github.com/topoteretes/cognee/blob/1961133d0c68b450fe8e71941a756371e94f7c04/README.md
[claude-mem]: https://github.com/thedotmack/claude-mem/blob/f1bb834527637231e23575e2a70c57375efdac04/README.md
[cipher]: https://github.com/campfirein/byterover-cli/blob/1052ac1a5dd0fde4da8693d4712064f7876c269c/README.md
[serena]: https://github.com/oraios/serena/blob/7f7d6245bf96b60fdf4e752a779917372fd1ee6e/README.md
[chunkhound]: https://github.com/chunkhound/chunkhound/blob/70c1cac3d11b286a515660e518cabc60ea8918b1/README.md
[codanna]: https://github.com/bartolli/codanna/blob/639917d868eda7dc789774c9c9d69b3753dd1b75/README.md
[sourcegraph]: https://sourcegraph.com/docs/getting-started
[coding-results]: ../benchmarks/results.md#with-and-without-loom
[memory-results]: ../benchmarks/results.md#with-and-without-loom
[work-raw]: https://github.com/ClickHouse/loom/blob/5b63495c10b313662b43d9538f669129b1137abd/bench/results/agent_clickhouse_work.jsonl
[conceptual-raw]: https://github.com/ClickHouse/loom/blob/5b63495c10b313662b43d9538f669129b1137abd/bench/results/agent_conceptual_ab.jsonl
[multihop-raw]: https://github.com/ClickHouse/loom/blob/5b63495c10b313662b43d9538f669129b1137abd/bench/results/agent_clickhouse_multihop.jsonl
[memory-raw]: https://github.com/ClickHouse/loom/blob/5b63495c10b313662b43d9538f669129b1137abd/bench/results/longmemeval_s_full_history.json
